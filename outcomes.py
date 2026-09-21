"""
outcomes.py — what would the trades we DIDN'T take have done?

    python outcomes.py                 # yesterday and any unresolved rows
    python outcomes.py --date 2026-09-18
    python outcomes.py --backfill 30   # the last 30 sessions of rejections

S3 (PM_PLAN.md / agenda 10.11). For every decision journaled with
approved=0 from `rules` or `claude` that carries entry/stop/target, replay
the bracket forward on daily bars for up to 10 sessions and record the
outcome — target, stop, neither, or no_geometry — with the R achieved.
Over months this is how a boardroom decides which gate is earning its keep,
instead of arguing about it.

NO TRADING BEHAVIOUR CHANGES. This reads the journal and daily bars and
writes to its own table. It places no orders and touches no position state.

TWO THINGS TO KNOW BEFORE READING THE TABLE
-------------------------------------------
1. COVERAGE IS THIN, AND IT IS THIN WHERE IT MATTERS. Only about 8% of
   rejections carry entry/stop/target — essentially the gatekeeper rows.
   A deterministic filter (`adx_low`, `volume_low`) rejects BEFORE geometry
   exists, so it journals a reason and nothing to replay. Those rows land in
   `no_geometry`, and that count is itself the finding: the journal cannot
   currently say whether the ADX filter is earning its keep. Making it able
   to would mean journaling the prospective entry/stop on deterministic
   rejections too — a change to the worker, so not in this order.

2. A REJECTION RESOLVES OVER TIME, NOT TONIGHT. Today's rejection has zero
   forward sessions tonight. Rows stay unresolved and are re-evaluated on
   later runs until they hit a level or the 10-session horizon passes, so
   running this nightly is what makes it correct, not a single pass.
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime

import pytz

import journal

EASTERN_TZ = pytz.timezone("US/Eastern")
HORIZON_SESSIONS = 10
SOURCES = ("rules", "claude")
OUTCOMES = ("target", "stop", "neither", "no_geometry")


# ============================================================ storage

def init_table(db_file: str = None):
    """Its own table, deliberately. These are hypothetical fills; writing
    them into `trades` would corrupt realised PnL, and writing them into
    `decisions` would put them in the training export."""
    db_file = db_file or journal.DB_FILE
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rejection_outcomes (
            decision_id INTEGER PRIMARY KEY,
            decided_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            setup_name TEXT,
            source TEXT NOT NULL,
            reason_key TEXT NOT NULL,
            reason_raw TEXT,
            entry REAL, stop REAL, target REAL,
            outcome TEXT NOT NULL,
            r_achieved REAL,
            sessions_used INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_rejection_outcomes_month
                    ON rejection_outcomes (decided_date)""")
    conn.commit()
    conn.close()


# ============================================================ reading

def _num(text, key):
    m = re.search(rf"{key}=([0-9]*\.?[0-9]+)", text or "")
    return float(m.group(1)) if m else None


def geometry(context: str):
    """(entry, stop, target) from a decision's context, or None.

    Gatekeeper rows carry them as JSON fields. Enriched `size_zero` rows
    carry entry/stop in free text (no target — a rejection that never got
    that far), so the target is derived at the setup's 3R floor to make the
    replay comparable rather than dropping the row."""
    if not context:
        return None
    try:
        data = json.loads(context)
    except (ValueError, TypeError):
        data = {}
    entry, stop, target = (data.get("entry"), data.get("stop"),
                           data.get("target"))
    if entry is None or stop is None:
        details = data.get("details") if isinstance(data, dict) else None
        entry = entry if entry is not None else _num(details, "entry")
        stop = stop if stop is not None else _num(details, "stop")
    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or stop <= 0 or stop >= entry:
        return None
    if target is None:
        target = entry + (entry - stop) * 3.0
    try:
        target = float(target)
    except (TypeError, ValueError):
        return None
    if target <= entry:
        return None
    return entry, stop, target


GATEKEEPER_PATTERNS = (
    ("gatekeeper_adx", ("adx",)),
    ("gatekeeper_rsi", ("rsi",)),
    ("gatekeeper_volume", ("volume",)),
    ("gatekeeper_resistance", ("resistance", "room", "overhead")),
    ("gatekeeper_conviction", ("conviction",)),
)


def reason_key(source: str, raw) -> str:
    """A GROUPABLE reason.

    Deterministic filters already journal a key (`adx_low`). The gatekeeper
    journals a sentence, and 121 of its rejections journal nothing at all —
    grouping on the raw text would produce a table of ~180 one-off rows,
    which is not a table. Gatekeeper reasons are bucketed by what they cite,
    because "the gate keeps rejecting on ADX" is the finding a boardroom can
    act on."""
    text = str(raw or "").strip().lower()
    if source != "claude":
        return text or "unspecified"
    for key, needles in GATEKEEPER_PATTERNS:
        if any(n in text for n in needles):
            return key
    return "gatekeeper_other" if text else "gatekeeper_unstated"


def is_daily_setup(setup_name: str, config: dict = None) -> bool:
    """Can a DAILY-bar replay answer this rejection?

    Only for a daily setup. trend_continuation is judged on 5-minute bars
    and its stops are 5-minute-scale — RKLB 2026-09-09 was entry 64.27 /
    stop 64.19, a 1R of EIGHT CENTS. Replaying that against daily bars is
    not a conservative estimate, it is a category error: one daily bar's
    range swamps the whole stop, so every such row "stops" at an absurd
    R (-32.9 in that case) and poisons the averages. Those rows are
    reported as no_geometry — the honest meaning being "this journal cannot
    replay it", which is the same class of gap as a filter that rejected
    before geometry existed."""
    import daily_eval
    if config is None:
        try:
            with open("bot_config.json", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    default = "daily" if setup_name in daily_eval.DEFAULT_TIMEFRAMES else "daily"
    return daily_eval.strategy_timeframe(setup_name, config, default) == "daily"


def pending_decisions(date_str: str = None, db_file: str = None,
                      include_unresolved: bool = True) -> list:
    """Rejections to evaluate: those decided on `date_str`, plus any row
    still unresolved from an earlier run."""
    db_file = db_file or journal.DB_FILE
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows, seen = [], set()

    def collect(sql, params):
        for r in conn.execute(sql, params):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            rows.append(dict(r))

    base = ("SELECT d.id, d.timestamp, d.ticker, d.setup_name, d.source, "
            "d.context, json_extract(d.verdict,'$.rejection_reason') AS reason "
            "FROM decisions d WHERE d.approved=0 AND d.source IN ('rules','claude') ")
    if date_str:
        collect(base + "AND substr(d.timestamp,1,10)=?", (date_str,))
    if include_unresolved:
        collect(base + "AND d.id IN (SELECT decision_id FROM "
                "rejection_outcomes WHERE resolved=0)", ())
    conn.close()
    return rows


# ============================================================ replay

def replay(bars, decided_date: str, entry: float, stop: float, target: float,
           horizon: int = HORIZON_SESSIONS):
    """Walk daily bars forward from the session AFTER `decided_date`.

    Fill conventions match backtest.simulate_bracket, so an outcome here is
    comparable with the study tables: a bar opening through a level fills at
    that open, and a bar spanning both is assumed to hit the STOP first.

    Returns (outcome, r_achieved, sessions_used, resolved)."""
    if bars is None or len(bars) == 0:
        return "neither", None, 0, False
    dates = [str(i)[:10] for i in bars.index]
    forward = [i for i, d in enumerate(dates) if d > decided_date][:horizon]
    if not forward:
        return "neither", None, 0, False          # no sessions yet

    risk = entry - stop
    last_close = entry
    for n, i in enumerate(forward, start=1):
        bar = bars.iloc[i]
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        last_close = c
        if o <= stop:
            return "stop", (o - entry) / risk, n, True
        if o >= target:
            return "target", (o - entry) / risk, n, True
        if l <= stop:
            return "stop", (stop - entry) / risk, n, True
        if h >= target:
            return "target", (target - entry) / risk, n, True

    used = len(forward)
    r_now = (last_close - entry) / risk
    # Only final once the horizon has actually elapsed.
    return "neither", r_now, used, used >= horizon


# ============================================================ evaluation

def evaluate(date_str: str = None, bars_by_ticker=None, db_file: str = None,
             horizon: int = HORIZON_SESSIONS, broker=None) -> dict:
    """Evaluate rejections and upsert their outcomes. Idempotent."""
    db_file = db_file or journal.DB_FILE
    init_table(db_file)
    rows = pending_decisions(date_str, db_file)
    if not rows:
        return {"evaluated": 0, "resolved": 0, "by_outcome": {}}
    try:
        with open("bot_config.json", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}

    if bars_by_ticker is None:
        bars_by_ticker = _fetch_bars({r["ticker"] for r in rows}, broker)

    conn = sqlite3.connect(db_file)
    counts, resolved_n = {}, 0
    for row in rows:
        decided = str(row["timestamp"])[:10]
        geo = geometry(row["context"])
        if geo is not None and not is_daily_setup(row["setup_name"], config):
            geo = None          # intraday geometry, daily bars: not answerable
        if geo is None:
            outcome, r_val, used, resolved = "no_geometry", None, 0, True
            entry = stop = target = None
        else:
            entry, stop, target = geo
            outcome, r_val, used, resolved = replay(
                bars_by_ticker.get(row["ticker"]), decided, entry, stop,
                target, horizon)
        counts[outcome] = counts.get(outcome, 0) + 1
        resolved_n += 1 if resolved else 0
        conn.execute("""
            INSERT INTO rejection_outcomes
              (decision_id, decided_date, ticker, setup_name, source,
               reason_key, reason_raw, entry, stop, target, outcome,
               r_achieved, sessions_used, resolved, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(decision_id) DO UPDATE SET
               outcome=excluded.outcome, r_achieved=excluded.r_achieved,
               sessions_used=excluded.sessions_used,
               resolved=excluded.resolved, updated_at=excluded.updated_at
        """, (row["id"], decided, row["ticker"], row["setup_name"],
              row["source"], reason_key(row["source"], row["reason"]),
              str(row["reason"] or "")[:300], entry, stop, target, outcome,
              r_val, used, 1 if resolved else 0,
              datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return {"evaluated": len(rows), "resolved": resolved_n,
            "by_outcome": counts}


def _fetch_bars(tickers, broker=None):
    if not tickers:
        return {}
    if broker is None:
        from broker import Broker
        broker = Broker()
    try:
        return broker.get_daily_bars(sorted(tickers), lookback_days=60)
    except Exception as e:
        print(f"Could not fetch daily bars: {e}")
        return {}


# ============================================================ reporting

def monthly_table(month: str = None, db_file: str = None) -> list:
    """[{reason_key, outcome, count, avg_r}] for one month, worst first."""
    db_file = db_file or journal.DB_FILE
    init_table(db_file)
    month = month or datetime.now(EASTERN_TZ).strftime("%Y-%m")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT reason_key, outcome, COUNT(*) AS n, AVG(r_achieved) AS avg_r,
               GROUP_CONCAT(r_achieved) AS rs
        FROM rejection_outcomes WHERE substr(decided_date,1,7)=?
        GROUP BY reason_key, outcome ORDER BY reason_key, outcome
    """, (month,)).fetchall()
    conn.close()

    out = []
    for r in rows:
        # MEDIAN as well as mean, because a single pathological stop can own
        # the average: RKLB 2026-09-09 was a reclaim with an EIGHT CENT stop
        # on a $64 stock, which replays to -32.9R and dragged a 7-row mean to
        # -5.8R. The geometry is real, so the row is not discarded — but a
        # table whose averages one row decides is not evidence.
        values = sorted(float(v) for v in (r["rs"] or "").split(",") if v)
        median = None
        if values:
            mid = len(values) // 2
            median = (values[mid] if len(values) % 2
                      else (values[mid - 1] + values[mid]) / 2)
        out.append({"reason_key": r["reason_key"], "outcome": r["outcome"],
                    "count": int(r["n"]),
                    "avg_r": round(float(r["avg_r"]), 3)
                    if r["avg_r"] is not None else None,
                    "median_r": round(median, 3) if median is not None
                    else None})
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="evaluate this ET date (default: yesterday)")
    p.add_argument("--backfill", type=int, default=0,
                   help="evaluate the last N calendar days of rejections")
    p.add_argument("--month", default=None, help="print this month's table")
    args = p.parse_args()

    if args.month:
        for row in monthly_table(args.month):
            print(f"{row['reason_key']:26} {row['outcome']:12} "
                  f"{row['count']:5} {row['avg_r']}")
        return 0

    dates = []
    if args.backfill:
        import datetime as dt
        today = datetime.now(EASTERN_TZ).date()
        dates = [(today - dt.timedelta(days=i)).isoformat()
                 for i in range(1, args.backfill + 1)]
    else:
        import datetime as dt
        dates = [args.date or (datetime.now(EASTERN_TZ).date()
                               - dt.timedelta(days=1)).isoformat()]

    total = {"evaluated": 0, "resolved": 0, "by_outcome": {}}
    for date_str in dates:
        result = evaluate(date_str)
        total["evaluated"] += result["evaluated"]
        total["resolved"] += result["resolved"]
        for k, v in result["by_outcome"].items():
            total["by_outcome"][k] = total["by_outcome"].get(k, 0) + v
    print(f"Evaluated {total['evaluated']} rejection(s); "
          f"{total['resolved']} resolved. " + ", ".join(
              f"{k}={v}" for k, v in sorted(total["by_outcome"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
