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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intern_grades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                stance TEXT,
                conviction INTEGER,
                grade TEXT,
                grade_note TEXT,
                UNIQUE(date, ticker)
            )
        """)
        # Migration: older DBs may lack the source/agreement columns.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
            conn.execute("UPDATE decisions SET source='claude' WHERE source IS NULL OR source=''")
        if "agreement" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN agreement INTEGER")
        tcols_t = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        if "tier" not in tcols_t:
            # B-book (Goal 19): tier tags every trade so A-book stats stay pure.
            conn.execute("ALTER TABLE trades ADD COLUMN tier TEXT DEFAULT 'A'")
            conn.execute("UPDATE trades SET tier='A' WHERE tier IS NULL")
        if "replays" not in cols:
            # How many times the re-ask loop replayed this same decision
            # before the per-bar gatekeeper cache existed (analytics only).
            conn.execute("ALTER TABLE decisions ADD COLUMN replays INTEGER DEFAULT 1")
        # Migration: broker_order_id ties a journaled fill to its Alpaca order
        # (sync idempotency).
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        if "broker_order_id" not in tcols:
            conn.execute("ALTER TABLE trades ADD COLUMN broker_order_id TEXT")
        conn.commit()
    run_data_migrations()


def run_data_migrations():
    """One-off data repairs, each guarded by a meta key so it runs exactly
    once per database. Safe on fresh/temp DBs (no matching rows = no-op)."""

    # 2026-07-14: delete the phantom FCX exit from launch day. The bot's
    # fallback path journaled a SELL at the last price with NO order id;
    # sync then journaled the REAL stop fill (-$1.80) under its order id.
    if get_meta("mig_20260714_phantom_fcx") is None:
        with _lock, _connect() as conn:
            cur = conn.execute(
                "DELETE FROM trades WHERE ticker='FCX' AND action='SELL' "
                "AND broker_order_id IS NULL "
                "AND reason='Exit (fill details unavailable)' "
                "AND timestamp LIKE '2026-07-13%'")
            if cur.rowcount:
                logger.info(f"Migration: deleted {cur.rowcount} phantom FCX exit row(s).")
            # Ensure the decision outcome points at a surviving SELL.
            conn.execute("""
                UPDATE decisions SET outcome_trade_id = NULL,
                       outcome_pnl_usd = NULL, outcome_pnl_pct = NULL
                WHERE outcome_trade_id IS NOT NULL
                  AND outcome_trade_id NOT IN (SELECT id FROM trades)""")
            conn.commit()
        set_meta("mig_20260714_phantom_fcx", "done")

    # 2026-07-14: collapse gatekeeper rows replicated by the re-ask loop —
    # ONE decision (e.g. XOM's daily bar) was re-sent to the gatekeeper every
    # cycle. Group by (source, ticker, setup, day, approved, stop): the stop
    # is bar-anchored for every strategy, so replays share it while genuinely
    # distinct signals differ. First occurrence kept, replay count recorded.
    # Rows referenced by trades are never deleted.
    if get_meta("mig_20260714_collapse_gatekeeper_replays") is None:
        _collapse_gatekeeper_replays()
        set_meta("mig_20260714_collapse_gatekeeper_replays", "done")

    # 2026-08-13: exits synced before record_exit knew about tiers were all
    # booked to the A-book. Re-point every SELL at its entry's tier so
    # A-book statistics stop carrying B-book results (PLTR, 2026-08-11).
    if get_meta("mig_20260813_exit_tier_backfill") is None:
        fixed = 0
        with _lock, _connect() as conn:
            sells = conn.execute(
                "SELECT id, ticker, decision_id, tier FROM trades "
                "WHERE action='SELL'").fetchall()
            for s in sells:
                row = None
                if s["decision_id"] is not None:
                    row = conn.execute(
                        "SELECT tier FROM trades WHERE action='BUY' AND "
                        "decision_id=? ORDER BY id DESC LIMIT 1",
                        (s["decision_id"],)).fetchone()
                if row is None or not row["tier"]:
                    row = conn.execute(
                        "SELECT tier FROM trades WHERE action='BUY' AND "
                        "ticker=? AND id<? ORDER BY id DESC LIMIT 1",
                        (s["ticker"], s["id"])).fetchone()
                want = str(row["tier"]).upper() if row and row["tier"] else "A"
                if str(s["tier"] or "A").upper() != want:
                    conn.execute("UPDATE trades SET tier=? WHERE id=?",
                                 (want, s["id"]))
                    fixed += 1
            conn.commit()
        if fixed:
            logger.info(f"Migration: re-tiered {fixed} exit row(s) to match "
                        f"their entries.")
        set_meta("mig_20260813_exit_tier_backfill", "done")

    # 2026-07-26: purge the gatekeeper ERROR rows from the 2026-07-22 key
    # outage (import-order bug). They are noise in the decision record; the
    # incident itself is documented in git history. Guarded + idempotent.
    if get_meta("mig_20260726_purge_key_outage_errors") is None:
        with _lock, _connect() as conn:
            cur = conn.execute(
                "DELETE FROM decisions WHERE source='claude' "
                "AND timestamp LIKE '2026-07-22%' "
                "AND json_extract(verdict, '$.error') LIKE '%ANTHROPIC_API_KEY%'")
            if cur.rowcount:
                logger.info(f"Migration: purged {cur.rowcount} key-outage ERROR rows.")
            conn.commit()
        set_meta("mig_20260726_purge_key_outage_errors", "done")

    # 2026-07-14: purge the pass-log flood — the same deterministic rejection
    # re-journaled every 30s cycle. Keep the FIRST row per
    # (ticker, setup, filter, day); the worker now dedupes at write time.
    if get_meta("mig_20260714_purge_pass_flood") is None:
        with _lock, _connect() as conn:
            cur = conn.execute("""
                DELETE FROM decisions WHERE source='rules' AND id NOT IN (
                    SELECT MIN(id) FROM decisions WHERE source='rules'
                    GROUP BY ticker, setup_name,
                             json_extract(verdict, '$.rejection_reason'),
                             substr(timestamp, 1, 10))""")
            if cur.rowcount:
                logger.info(f"Migration: purged {cur.rowcount} duplicate rules-pass rows.")
            conn.commit()
        set_meta("mig_20260714_purge_pass_flood", "done")


def _decision_bar_key(row) -> tuple:
    """Proxy for 'same signal bar': the stop is bar-anchored for every
    strategy (reclaim/breakout bar low, ATR at the cross), while entry
    drifts with each cycle's price."""
    try:
        stop = json.loads(row["context"] or "{}").get("stop")
    except json.JSONDecodeError:
        stop = None
    stop_key = round(float(stop), 2) if isinstance(stop, (int, float)) else None
    return (row["ticker"], row["setup_name"], row["timestamp"][:10], stop_key)


def _collapse_gatekeeper_replays():
    with _lock, _connect() as conn:
        referenced = {r["decision_id"] for r in conn.execute(
            "SELECT DISTINCT decision_id FROM trades WHERE decision_id IS NOT NULL")}
        rows = conn.execute(
            "SELECT id, source, ticker, setup_name, timestamp, approved, context "
            "FROM decisions WHERE source IN ('claude','local','local_shadow') "
            "ORDER BY id").fetchall()
        groups = {}
        for r in rows:
            key = (r["source"], r["approved"]) + _decision_bar_key(r)
            groups.setdefault(key, []).append(r["id"])

        deleted = 0
        for ids in groups.values():
            if len(ids) < 2:
                continue
            keep = ids[0]
            dupes = [i for i in ids[1:] if i not in referenced]
            conn.execute("UPDATE decisions SET replays=? WHERE id=?",
                         (len(ids), keep))
            if dupes:
                conn.execute(
                    f"DELETE FROM decisions WHERE id IN ({','.join('?' * len(dupes))})",
                    dupes)
                deleted += len(dupes)
        if deleted:
            logger.info(f"Migration: collapsed {deleted} replayed gatekeeper rows.")
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


def log_integrity_event(kind: str, details: str) -> int:
    """Operational integrity events (write degradation, etc.). Journaled as
    source='ops' so the daily reviewer sees infrastructure problems, not
    just trading ones. Deduped per (kind, target, ET day) so a persistent
    fault logs once a day instead of flooding the decision record."""
    today = _today_et()
    target = details.split(":")[0].strip()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id FROM decisions WHERE source='ops' AND ticker=? "
            "AND setup_name=? AND timestamp LIKE ? LIMIT 1",
            (target[:40], kind, f"{today}%")).fetchone()
        if row:
            return row["id"]
    return log_decision(
        target[:40], kind, {"details": details},
        {"approved": False, "rejection_reason": kind, "reasoning": details},
        source="ops")


def log_rules_pass(ticker: str, setup_name: str, filter_name: str,
                   details: str = "") -> int:
    """Journal a signal that fired but was killed by a DETERMINISTIC filter
    (ADX low, volume low, SPY bearish, circuit breaker, ...). Together with
    gatekeeper rows this makes the journal a complete record of every signal
    considered.

    Idempotent AT THE DATABASE, per (ticker, setup, filter, ET day). The
    in-memory per-cycle dedupe lives inside one worker process, so a
    restarted or duplicated worker re-journalled the same pass — 2026-08-13
    carried 35 extra rows, some setups four deep. Returns the existing row's
    id when the pass is already recorded today."""
    today = _today_et()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id FROM decisions WHERE source='rules' AND ticker=? "
            "AND setup_name=? AND timestamp LIKE ? "
            "AND json_extract(verdict,'$.rejection_reason')=? LIMIT 1",
            (ticker, setup_name, f"{today}%", filter_name)).fetchone()
        if row:
            return row["id"]
    return log_decision(
        ticker, setup_name,
        {"details": details},
        {"approved": False, "rejection_reason": filter_name, "source": "rules"},
        source="rules",
    )


def decision_counts(date_str: str = None) -> dict:
    """Self-explaining counters: total, by source, and the gatekeeper subset
    that floor.py reports. Two different numbers for 'decisions today' is
    what the reviewer flagged; this makes each one say what it counts."""
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM decisions WHERE timestamp LIKE ? "
            "GROUP BY source", (f"{date_str}%",)).fetchall()
    by_source = {r["source"]: r["n"] for r in rows}
    gatekeeper_sources = ("claude", "local", "local_shadow", "smoke_test", "ceo")
    return {
        "total": sum(by_source.values()),
        "by_source": by_source,
        "gatekeeper_calls": sum(by_source.get(s, 0) for s in gatekeeper_sources),
        "rules_passes": by_source.get("rules", 0),
    }


def governance_rows(date_str: str = None) -> list:
    """Today's GOVERNANCE decisions — CEO sheet rulings, overrides, review
    memos, tier/exemption tags. The reviewer needs to see the desk being
    run, not only the fills it produced."""
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, ticker, setup_name, source, approved, "
            "conviction_score, verdict, context FROM decisions "
            "WHERE timestamp LIKE ? AND (source IN "
            "('ceo','ceo_override','review_bot','intern','ops') "
            "OR json_extract(verdict,'$.tag') IS NOT NULL) ORDER BY id",
            (f"{date_str}%",)).fetchall()
    out = []
    for r in rows:
        try:
            verdict = json.loads(r["verdict"] or "{}")
        except json.JSONDecodeError:
            verdict = {}
        out.append({
            "time": (r["timestamp"] or "")[11:19],
            "ticker": r["ticker"], "setup": r["setup_name"],
            "source": r["source"],
            "approved": bool(r["approved"]),
            "tag": verdict.get("tag"),
            "reason": verdict.get("rejection_reason"),
            "note": (verdict.get("reasoning") or "")[:200],
        })
    return out


def log_signal_tag(ticker: str, setup_name: str, tag: str,
                   details: str = "") -> int:
    """Tag a signal that was TAKEN under a named exemption (not a rejection).
    Rule #5: reclaim fires accepted while SPY is below its 20-EMA are tagged
    'chop_reclaim' so their outcomes can be analysed separately."""
    return log_decision(
        ticker, setup_name,
        {"details": details, "tag": tag},
        {"approved": True, "tag": tag, "rejection_reason": None,
         "source": "rules"},
        source="rules")


def chop_reclaim_report() -> dict:
    """Outcome scorecard for Rule #5 exemption fires."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ticker, timestamp, outcome_pnl_usd FROM decisions "
            "WHERE source='rules' AND json_extract(verdict,'$.tag')='chop_reclaim' "
            "ORDER BY id").fetchall()
    closed = [r for r in rows if r["outcome_pnl_usd"] is not None]
    return {
        "tagged": len(rows),
        "closed": len(closed),
        "realized_usd": round(sum(r["outcome_pnl_usd"] for r in closed), 2),
        "tickers": [r["ticker"] for r in rows],
    }


def log_trade(ticker: str, action: str, qty: float, price: float,
              pnl_usd: float = 0.0, pnl_pct: float = 0.0,
              reason: str = "", decision_id=None,
              broker_order_id: str = None, tier: str = "A") -> int:
    """Journal one fill (BUY or SELL). Returns the trade id."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (timestamp, ticker, action, qty, price, pnl_usd, pnl_pct, reason,
                decision_id, broker_order_id, tier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_now_et(), ticker, action.upper(), qty, price, pnl_usd, pnl_pct,
             reason, decision_id, broker_order_id, str(tier).upper()),
        )
        conn.commit()
        return cur.lastrowid


def tier_realized_pnl(tier: str = "A") -> float:
    """Realized PnL for ONE tier — A-book stats never include B rows."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS pnl FROM trades "
            "WHERE action='SELL' AND UPPER(COALESCE(tier,'A'))=?",
            (str(tier).upper(),)).fetchone()
        return float(row["pnl"])


def b_entries_since(iso_date: str) -> int:
    """Count of tier-B entries on/after a date (calendar-week gate)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE action='BUY' "
            "AND UPPER(COALESCE(tier,'A'))='B' AND timestamp >= ?",
            (iso_date,)).fetchone()
        return int(row["n"])


def open_b_tickers() -> list:
    """Tickers with a tier-B BUY that has no matching later SELL."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ticker, MAX(id) AS last_buy FROM trades WHERE action='BUY' "
            "AND UPPER(COALESCE(tier,'A'))='B' GROUP BY ticker").fetchall()
        out = []
        for r in rows:
            sold = conn.execute(
                "SELECT 1 FROM trades WHERE ticker=? AND action='SELL' AND id>?",
                (r["ticker"], r["last_buy"])).fetchone()
            if not sold:
                out.append(r["ticker"])
        return out


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


def last_buy_for_ticker(ticker: str, reason_prefix: str = None):
    """Most recent journaled BUY for a ticker (dict) or None. With
    reason_prefix, only BUYs from that desk match (e.g. 'INTERN') — keeps
    intern-account syncs from pairing against main-desk entries."""
    with _lock, _connect() as conn:
        if reason_prefix:
            row = conn.execute(
                "SELECT * FROM trades WHERE ticker=? AND action='BUY' "
                "AND reason LIKE ? ORDER BY id DESC LIMIT 1",
                (ticker, f"{reason_prefix}%")).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM trades WHERE ticker=? AND action='BUY' "
                "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return dict(row) if row else None


def desk_realized_pnl(reason_prefix: str) -> float:
    """Cumulative realized PnL for one desk's exits (reason prefix match)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS pnl FROM trades "
            "WHERE action='SELL' AND reason LIKE ?",
            (f"{reason_prefix}%",)).fetchone()
        return float(row["pnl"])


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


def entry_tier(ticker: str, decision_id=None) -> str:
    """Tier of the BUY this exit closes. Exits must inherit it — booking a
    B-book loss to the A-book corrupts A-book statistics (PLTR, 2026-08-11)."""
    with _lock, _connect() as conn:
        if decision_id is not None:
            row = conn.execute(
                "SELECT tier FROM trades WHERE action='BUY' AND decision_id=? "
                "ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
            if row and row["tier"]:
                return str(row["tier"]).upper()
        row = conn.execute(
            "SELECT tier FROM trades WHERE action='BUY' AND ticker=? "
            "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return str(row["tier"]).upper() if row and row["tier"] else "A"


def record_exit(ticker: str, qty: float, fill_price: float, reason: str,
                decision_id=None, broker_order_id: str = None,
                entry_price: float = None, tier: str = None):
    """SINGLE-AUTHORITY exit journaling. Every path that sees a fill (bot
    loop, orders.py sync, report startup sync) must come through here or
    respect the same rule: idempotence keys on the BROKER's order id.
    Whoever sees the fill first journals it; everyone else returns None.

    Returns (trade_id, pnl_usd, pnl_pct), or (None, None, None) if this fill
    is already journaled."""
    if broker_order_id and trade_exists_for_order(broker_order_id):
        return None, None, None
    entry = entry_price if entry_price else fill_price
    pnl_usd = (fill_price - entry) * qty
    pnl_pct = ((fill_price / entry) - 1) * 100 if entry else 0.0
    # Inherit the entry's tier unless the caller states one.
    resolved_tier = (str(tier).upper() if tier
                     else entry_tier(ticker, decision_id))
    trade_id = log_trade(ticker, "SELL", qty, fill_price,
                         pnl_usd=pnl_usd, pnl_pct=pnl_pct, reason=reason,
                         decision_id=decision_id,
                         broker_order_id=broker_order_id,
                         tier=resolved_tier)
    link_outcome(decision_id, trade_id, pnl_usd, pnl_pct)
    return trade_id, pnl_usd, pnl_pct


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


def intern_record(date_str: str, ticker: str, stance: str, conviction):
    """One row per (date, ticker) intern call. Re-running the desk updates
    stance/conviction but PRESERVES any grade the CEO already gave."""
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO intern_grades (date, ticker, stance, conviction)
               VALUES (?,?,?,?)
               ON CONFLICT(date, ticker) DO UPDATE SET
                 stance=excluded.stance, conviction=excluded.conviction""",
            (date_str, ticker.upper(), stance,
             int(conviction) if isinstance(conviction, (int, float)) else None))
        conn.commit()


def intern_grade(date_str: str, ticker: str, grade: str, note: str = "") -> bool:
    """CEO session grade for one intern call. Returns False if no such row."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE intern_grades SET grade=?, grade_note=? WHERE date=? AND ticker=?",
            (grade, note, date_str, ticker.upper()))
        conn.commit()
        return cur.rowcount > 0


def intern_history(limit: int = 200) -> list:
    """All intern calls, newest first, with grades — the tab's history view."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM intern_grades ORDER BY date DESC, conviction DESC "
            "LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def intern_calls(date_str: str = None) -> list:
    date_str = date_str or _today_et()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM intern_grades WHERE date=? ORDER BY conviction DESC",
            (date_str,)).fetchall()
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

    Stats count UNIQUE decisions — rows are grouped by (ticker, setup, day,
    bar-anchored stop) and only the first occurrence scores. Without this,
    one decision replayed 73 times by the old re-ask loop would count as 73
    agreements and massively inflate the local model's record.
    """
    with _lock, _connect() as conn:
        raw_rows = conn.execute(
            "SELECT * FROM decisions WHERE source='local_shadow' ORDER BY id"
        ).fetchall()

    unique = {}
    for r in raw_rows:
        key = _decision_bar_key(r)
        if key not in unique:
            unique[key] = r
    rows = list(unique.values())

    total = len(rows)
    report = {
        "total_shadow_decisions": total,       # unique decisions
        "raw_shadow_rows": len(raw_rows),      # incl. replays (pre-cache era)
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
