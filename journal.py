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
        # Migration: older DBs may lack the source/agreement columns.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
            conn.execute("UPDATE decisions SET source='claude' WHERE source IS NULL OR source=''")
        if "agreement" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN agreement INTEGER")
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
              reason: str = "", decision_id=None) -> int:
    """Journal one fill (BUY or SELL). Returns the trade id."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (timestamp, ticker, action, qty, price, pnl_usd, pnl_pct, reason, decision_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now_et(), ticker, action.upper(), qty, price, pnl_usd, pnl_pct,
             reason, decision_id),
        )
        conn.commit()
        return cur.lastrowid


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
