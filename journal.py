"""
journal.py — SQLite trade & decision journal.

Every signal the bot considers is journaled: AI gatekeeper verdicts (Claude,
local model, or shadow copies), deterministic rules-based passes, executed
trades and their eventual outcomes. This makes the journal a complete record
of every signal considered — taken or passed.

Tables
------
decisions:  one row per gatekeeper/rules verdict on a signal.
            `source` is one of: claude | local | local_shadow | rules | ceo
trades:     one row per fill (BUY or SELL).
link_outcome() ties an exit trade + realized PnL back to the decision that
opened the position.
"""

import sqlite3
import json
import logging
import threading
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)

DB_FILE = "journal.db"
EASTERN_TZ = pytz.timezone("US/Eastern")

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _now_et() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today_et() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")


def init_db():
    """Create tables if missing, and migrate older schemas (adds the
    `source`/`agreement` columns; pre-existing rows become source='claude')."""
    with _lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                setup_name TEXT,
                context TEXT,
                verdict TEXT,
                approved INTEGER,
                conviction_score INTEGER,
                source TEXT NOT NULL DEFAULT 'claude',
                agreement INTEGER,
                outcome_trade_id INTEGER,
                outcome_pnl_usd REAL,
                outcome_pnl_pct REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                qty REAL,
                price REAL,
                pnl_usd REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                reason TEXT,
                decision_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Migration: older DBs may lack the source/agreement columns.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
            conn.execute("UPDATE decisions SET source='claude' WHERE source IS NULL OR source=''")
        if "agreement" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN agreement INTEGER")
        # Migration: broker_order_id ties a journaled fill to its Alpaca order
        # (sync idempotency).
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        if "broker_order_id" not in tcols:
            conn.execute("ALTER TABLE trades ADD COLUMN broker_order_id TEXT")
        conn.commit()


def log_decision(ticker: str, setup_name: str, context: dict, verdict: dict,
                 source: str = "claude", agreement=None) -> int:
    """Journal one gatekeeper/rules verdict. Returns the decision id."""
    approved = verdict.get("approved")
    if isinstance(approved, str):
        approved = approved.lower() == "true"
    conviction = verdict.get("conviction_score")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO decisions
               (timestamp, ticker, setup_name, context, verdict, approved,
                conviction_score, source, agreement)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now_et(), ticker, setup_name,
             json.dumps(context, default=str), json.dumps(verdict, default=str),
             1 if approved else 0,
             int(conviction) if isinstance(conviction, (int, float)) else None,
             source,
             None if agreement is None else (1 if agreement else 0)),
        )
        conn.commit()
        return cur.lastrowid


def log_rules_pass(ticker: str, setup_name: str, filter_name: str,
                   details: str = "") -> int:
    """Journal a signal that fired but was killed by a DETERMINISTIC filter
    (ADX low, volume low, SPY bearish, circuit breaker, ...). Together with
    gatekeeper rows this makes the journal a complete record of every signal
    considered."""
    return log_decision(
        ticker, setup_name,
        {"details": details},
        {"approved": False, "rejection_reason": filter_name, "source": "rules"},
        source="rules",
    )


def log_trade(ticker: str, action: str, qty: float, price: float,
              pnl_usd: float = 0.0, pnl_pct: float = 0.0,
              reason: str = "", decision_id=None,
              broker_order_id: str = None) -> int:
    """Journal one fill (BUY or SELL). Returns the trade id."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (timestamp, ticker, action, qty, price, pnl_usd, pnl_pct, reason,
                decision_id, broker_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_now_et(), ticker, action.upper(), qty, price, pnl_usd, pnl_pct,
             reason, decision_id, broker_order_id),
        )
        conn.commit()
        return cur.lastrowid


def get_meta(key: str, default=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str):
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))
        conn.commit()


def trade_exists_for_order(broker_order_id: str) -> bool:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT 1 FROM trades WHERE broker_order_id=?",
                           (str(broker_order_id),)).fetchone()
        return row is not None


def last_buy_for_ticker(ticker: str):
    """Most recent journaled BUY for a ticker (dict) or None."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE ticker=? AND action='BUY' "
            "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return dict(row) if row else None


def find_unmatched_sell(ticker: str, qty: float, price: float,
                        price_tol: float = 0.02):
    """A SELL journaled without a broker_order_id that matches this fill
    (same ticker/qty, price within tolerance) — e.g. journaled live by the
    bot loop before sync ran. Returns the trade id or None."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, qty, price FROM trades WHERE ticker=? AND action='SELL' "
            "AND broker_order_id IS NULL ORDER BY id DESC LIMIT 20",
            (ticker,)).fetchall()
    for r in rows:
        if abs((r["qty"] or 0) - qty) < 1e-9 and abs((r["price"] or 0) - price) <= price_tol:
            return r["id"]
    return None


def set_trade_order_id(trade_id: int, broker_order_id: str):
    with _lock, _connect() as conn:
        conn.execute("UPDATE trades SET broker_order_id=? WHERE id=?",
                     (str(broker_order_id), trade_id))
        conn.commit()


def find_buy_without_order_id(ticker: str, qty: float):
    """Most recent journaled BUY for ticker/qty lacking a broker_order_id
    (i.e. journaled at a reference price before the fill was known)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, qty FROM trades WHERE ticker=? AND action='BUY' "
            "AND broker_order_id IS NULL ORDER BY id DESC LIMIT 20",
            (ticker,)).fetchall()
    for r in rows:
        if abs((r["qty"] or 0) - qty) < 1e-9:
            return r["id"]
    return None


def fix_buy_fill(buy_trade_id: int, actual_price: float) -> bool:
    """Correct a journaled BUY to its ACTUAL average fill price, then
    recompute PnL on any linked SELLs and the decision outcome. Idempotent —
    re-applying the same price is a no-op. Returns True if anything changed."""
    with _lock, _connect() as conn:
        buy = conn.execute("SELECT * FROM trades WHERE id=? AND action='BUY'",
                           (buy_trade_id,)).fetchone()
        if buy is None:
            return False
        if abs((buy["price"] or 0) - actual_price) < 0.005:
            return False

        conn.execute("UPDATE trades SET price=? WHERE id=?",
                     (actual_price, buy_trade_id))

        # Recompute realized PnL on SELLs that closed this entry.
        sells = conn.execute(
            "SELECT * FROM trades WHERE action='SELL' AND ticker=? AND id>? "
            "AND (decision_id IS ? OR decision_id=?)",
            (buy["ticker"], buy_trade_id, buy["decision_id"], buy["decision_id"]),
        ).fetchall()
        for s in sells:
            pnl_usd = (s["price"] - actual_price) * (s["qty"] or 0)
            pnl_pct = ((s["price"] / actual_price) - 1) * 100 if actual_price else 0.0
            conn.execute("UPDATE trades SET pnl_usd=?, pnl_pct=? WHERE id=?",
                         (pnl_usd, pnl_pct, s["id"]))
            conn.execute(
                "UPDATE decisions SET outcome_pnl_usd=?, outcome_pnl_pct=? "
                "WHERE outcome_trade_id=?",
                (pnl_usd, pnl_pct, s["id"]))
        conn.commit()
        return True


def apply_fill_corrections(corrections: dict) -> int:
    """One-off data migration: {ticker: (recorded_bad_price, actual_fill)}.
    Only touches a BUY whose price still equals the known-bad recorded value,
    so it is idempotent and can never clobber a later re-entry in the same
    ticker. Returns rows changed."""
    changed = 0
    for ticker, (bad_price, actual) in (corrections or {}).items():
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT id FROM trades WHERE ticker=? AND action='BUY' "
                "AND ABS(price - ?) < 0.005 ORDER BY id DESC LIMIT 1",
                (ticker, float(bad_price))).fetchone()
        if row and fix_buy_fill(row["id"], float(actual)):
            changed += 1
    return changed


def get_trade_by_order_id(broker_order_id: str):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM trades WHERE broker_order_id=?",
                           (str(broker_order_id),)).fetchone()
        return dict(row) if row else None


def link_outcome(decision_id: int, exit_trade_id: int,
                 pnl_usd: float, pnl_pct: float):
    """Tie a closed trade's realized PnL back to the decision that opened it."""
    if decision_id is None:
        return
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE decisions
               SET outcome_trade_id=?, outcome_pnl_usd=?, outcome_pnl_pct=?
               WHERE id=?""",
            (exit_trade_id, pnl_usd, pnl_pct, decision_id),
        )
        conn.commit()


def daily_realized_pnl(date_str: str = None) -> float:
    """Sum of realized PnL from SELL fills on the given ET date (default today).
    This feeds the daily-loss circuit breaker."""
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) AS pnl FROM trades
               WHERE action='SELL' AND timestamp LIKE ?""",
            (f"{date_str}%",),
        ).fetchone()
        return float(row["pnl"])


def decision_count(date_str: str = None) -> int:
    """Number of decisions journaled on the given ET date (default today)."""
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE timestamp LIKE ?",
            (f"{date_str}%",),
        ).fetchone()
        return int(row["n"])


def todays_trades(date_str: str = None) -> list:
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY id",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_sells(limit: int = 10) -> list:
    """Most recent closed trades (SELL fills), newest first."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE action='SELL' ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def agreement_report() -> dict:
    """Shadow-mode scorecard: how often does the local analyst agree with Claude?

    Shadow rows (source='local_shadow') carry the paired Claude verdict inside
    their context JSON as claude_approved / claude_conviction.
    """
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE source='local_shadow'"
        ).fetchall()

    total = len(rows)
    report = {
        "total_shadow_decisions": total,
        "agreement_pct": None,
        "local_approved_claude_rejected": 0,
        "claude_approved_local_rejected": 0,
        "avg_conviction_gap": None,
        "errors": 0,
    }
    if total == 0:
        return report

    agreements = 0
    scored = 0
    gaps = []
    for r in rows:
        try:
            verdict = json.loads(r["verdict"] or "{}")
            context = json.loads(r["context"] or "{}")
        except json.JSONDecodeError:
            continue
        if "error" in verdict:
            report["errors"] += 1
            continue
        scored += 1
        local_approved = bool(r["approved"])
        claude_approved = bool(context.get("claude_approved"))
        if r["agreement"] is not None:
            if r["agreement"]:
                agreements += 1
        elif local_approved == claude_approved:
            agreements += 1
        if local_approved and not claude_approved:
            report["local_approved_claude_rejected"] += 1
        if claude_approved and not local_approved:
            report["claude_approved_local_rejected"] += 1
        lc = r["conviction_score"]
        cc = context.get("claude_conviction")
        if isinstance(lc, (int, float)) and isinstance(cc, (int, float)):
            gaps.append(abs(lc - cc))

    if scored:
        report["agreement_pct"] = round(100.0 * agreements / scored, 1)
    if gaps:
        report["avg_conviction_gap"] = round(sum(gaps) / len(gaps), 1)
    return report
