"""
goals.py — S26. The desk's goal sheet: targets from bot_config.json "goals",
actuals and the reasons behind them from the journal, the broker and
logs/worker.log. REPORTING ONLY.

HARD RULE (CEO, S26): goals never reach the gatekeeper, prompts.py, the
filters, sizing or order code. Nothing in this module is imported by them,
and tests/test_s26_goals.py fails if a goal key ever appears there. The
daily memo gets this section appended AFTER the model has written sections
1-5 - no goal is ever put in a prompt.

Definitions (also printed in every report):
  trades       BUYs opened on the $5,000 account in the period (intern
               excluded)
  P&L          realized P&L of trades CLOSED in the period; open P&L shown
               beside it
  win rate     closed trades with P&L > 0, over closed trades
  capture      realized P&L / peak open profit, where the peak is the
               highest 5-minute high between entry and exit (IEX bars)
  on time      a session whose worker started by 09:30 ET (logs/worker.log)
               or was already journaling decisions by 09:35 ET - the log on
               this Mac does not hold the laptop-era mornings
  giveback     a closed trade that was up >= +1R open and closed at a loss

A period still in progress is judged against the target pro-rated to the
sessions elapsed so far, and says so.
"""

import json
import os
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

import trade_table

DEFAULT_GOALS = {
    "trades_per_week": 3,
    "monthly_pnl_usd": 100,
    "win_rate_pct": 40,
    "capture_ratio_pct": 35,
    "on_time_sessions_pct": 100,
    "max_profit_to_loss_trades": 0,
}
GOAL_KEYS = tuple(DEFAULT_GOALS)
GIVEBACK_MIN_R = 1.0     # CEO 2026-10-17 (D): was +0.5R
ON_TIME = "09:30:00"            # a logged worker start at or before this
ON_TIME_ACTIVITY = "09:35:00"   # ...or worker decisions journaled by this
# Goals belong to the $5,000 account: no period starts before it took over.
ACCOUNT_START_DATE = date.fromisoformat(trade_table.ACCOUNT_START[:10])
LIVE_SETUPS = ("trend_continuation", "momentum_continuation",
               "mean_reversion_reclaim", "pullback_in_uptrend",
               "post_earnings_continuation")
POST_APPROVAL = {
    "probation_position_open": "probation slot",
    "size_zero": "size", "price_too_high_for_account": "size",
    "notional_exceeds_max_position_pct": "size",
    "insufficient_cash_no_margin": "size",
    "max_positions": "slots", "max_positions_reached": "slots",
    "duplicate_entry_blocked": "duplicate",
}
PROPOSAL = "proposal, CEO to decide"


def load_goals(config: dict = None) -> dict:
    if config is None:
        try:
            with open("bot_config.json", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    goals = dict(DEFAULT_GOALS)
    goals.update({k: v for k, v in (config.get("goals") or {}).items()
                  if k in DEFAULT_GOALS})
    return goals


# ============================================================ periods

def week_bounds(asof: date):
    monday = asof - timedelta(days=asof.weekday())
    return monday, monday + timedelta(days=4)


def month_bounds(asof: date):
    first = asof.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return first, nxt - timedelta(days=1)


def trading_sessions(start: date, end: date, broker=None) -> list:
    """ET session dates in [start, end]: the broker's calendar, else weekdays."""
    if broker is not None:
        try:
            from alpaca.trading.requests import GetCalendarRequest
            cal = broker.trading.get_calendar(
                GetCalendarRequest(start=start, end=end))
            return [str(c.date)[:10] for c in cal]
        except Exception:
            pass
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ============================================================ sources

def worker_starts(log_path: str = os.path.join("logs", "worker.log")) -> dict:
    """{ET date: first 'Live bot worker started' time} from the worker log
    (the Mac runs on New York time, so its stamps are ET)."""
    first = {}
    pat = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] \w+: "
                     r"Live bot worker started")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.match(line)
                if m and m.group(1) not in first:
                    first[m.group(1)] = m.group(2)
    except OSError:
        pass
    return first


def first_activity(conn, start: str, end: str) -> dict:
    """{ET date: time of the first worker decision (rules/gatekeeper)}."""
    out = {}
    for day, first in conn.execute(
            "SELECT substr(timestamp,1,10), min(substr(timestamp,12,8)) "
            "FROM decisions WHERE source IN ('rules','claude') AND "
            "substr(timestamp,1,10) BETWEEN ? AND ? GROUP BY 1", (start, end)):
        out[day] = first
    return out


def _account_trades(conn):
    return [t for t in trade_table.load_trades(conn)
            if t["buy_ts"] >= trade_table.ACCOUNT_START
            and not str(t.get("setup") or "").upper().startswith("INTERN")]


def funnel(conn, start: str, end: str) -> dict:
    """Signals and where they stopped, unique per (date, ticker, setup, bar)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, source, approved, "
        "conviction_score, context, verdict FROM decisions "
        "WHERE source IN ('rules','claude') AND substr(timestamp,1,10) "
        "BETWEEN ? AND ?", (start, end)).fetchall()
    import outcomes
    seen, rules, gk_block, skipped = set(), Counter(), Counter(), Counter()
    approved = set()
    for r in rows:
        if r["setup_name"] not in LIVE_SETUPS:
            continue
        try:
            ctx = json.loads(r["context"] or "{}")
            verdict = json.loads(r["verdict"] or "{}")
        except ValueError:
            ctx, verdict = {}, {}
        day = r["timestamp"][:10]
        bar = ctx.get("bar") or (f"stop={round(float(ctx['stop']), 2)}"
                                 if ctx.get("stop") is not None else day)
        key = (day, r["ticker"], r["setup_name"], str(bar)[:19])
        reason = verdict.get("rejection_reason")
        if r["source"] == "claude":
            seen.add(key)
            if r["approved"] and (r["conviction_score"] or 0) >= 70:
                approved.add((day, r["ticker"], r["setup_name"]))
            else:
                gk_block[("conviction < 70" if r["approved"]
                          else outcomes.reason_key("claude", reason))] += 1
        elif verdict.get("tag") == "chop_reclaim" or r["approved"]:
            continue
        elif reason in POST_APPROVAL:
            skipped[POST_APPROVAL[reason]] += 1
        else:
            seen.add(key)
            rules[reason or "unspecified"] += 1
    return {"signals": len(seen), "rule_rejections": rules,
            "approvals": len(approved), "gatekeeper_blocks": gk_block,
            "skipped_after_approval": skipped}


def peak_profit(trade: dict, bars) -> tuple:
    """(peak open profit $, peak in R) from 5-minute highs between entry and
    exit, or (None, None) without bars."""
    if bars is None or len(bars) == 0:
        return None, None
    import pandas as pd
    start = pd.Timestamp(trade["buy_ts"]).tz_localize("America/New_York")
    end = (pd.Timestamp(trade["sell_ts"]).tz_localize("America/New_York")
           if trade.get("sell_ts") else None)
    idx = bars.index.tz_convert("America/New_York")
    mask = idx >= start.floor("5min")
    if end is not None:
        mask &= idx <= end
    window = bars[mask]
    if window.empty:
        return None, None
    high = float(window["high"].max())
    peak_usd = max(0.0, (high - trade["buy"]) * trade["qty"])
    risk = (trade["buy"] - float(trade["stop"])) if trade.get("stop") else None
    peak_r = ((high - trade["buy"]) / risk) if risk and risk > 0 else None
    return peak_usd, peak_r


# ============================================================ the sheet

def evaluate(conn, start: date, end: date, asof: date, goals: dict,
             broker=None, bars_by_ticker=None, starts: dict = None,
             open_pnl: float = None) -> dict:
    """Actuals for one period. `end` is the period's last day; data runs
    through `asof`."""
    start = max(start, ACCOUNT_START_DATE)
    s, e = start.isoformat(), min(end, asof).isoformat()
    sessions_all = trading_sessions(start, end, broker)
    sessions = [d for d in sessions_all if d <= asof.isoformat()]
    frac = (len(sessions) / len(sessions_all)) if sessions_all else 1.0
    trades = _account_trades(conn)
    opened = [t for t in trades if s <= t["buy_ts"][:10] <= e]
    closed = [t for t in trades if t["sell_ts"] and t["reason"] != "partial"
              and s <= t["sell_ts"][:10] <= e]
    realized = sum(t["pnl"] or 0.0 for t in closed)
    wins = [t for t in closed if (t["pnl"] or 0) > 0]

    peaks, givebacks, captured, peak_total = {}, [], 0.0, 0.0
    for t in closed:
        pk_usd, pk_r = peak_profit(t, (bars_by_ticker or {}).get(t["ticker"]))
        peaks[id(t)] = (pk_usd, pk_r)
        if pk_usd:
            captured += t["pnl"] or 0.0
            peak_total += pk_usd
        if pk_r is not None and pk_r >= GIVEBACK_MIN_R and (t["pnl"] or 0) < 0:
            givebacks.append(t)
    have_peaks = any(p[0] is not None for p in peaks.values())

    starts = starts if starts is not None else worker_starts()
    activity = first_activity(conn, s, e)
    on_time_days, late, missed, seen_at = [], [], [], {}
    for d in sessions:
        log_t, act_t = starts.get(d), activity.get(d)
        seen_at[d] = min(t for t in (log_t, act_t) if t) if (log_t or act_t) else None
        if (log_t and log_t <= ON_TIME) or (act_t and act_t <= ON_TIME_ACTIVITY):
            on_time_days.append(d)
        elif log_t or act_t:
            late.append(d)
        else:
            missed.append(d)
    on_time = len(sessions) - len(late) - len(missed)

    weeks = max(len(sessions) / 5.0, 1e-9)
    actual = {
        "trades_per_week": round(len(opened) / weeks, 2) if sessions else 0.0,
        "monthly_pnl_usd": round(realized, 2),
        "win_rate_pct": (round(100 * len(wins) / len(closed), 1)
                         if closed else None),
        "capture_ratio_pct": (round(100 * captured / peak_total, 1)
                              if peak_total > 0 else None),
        "on_time_sessions_pct": (round(100 * on_time / len(sessions), 1)
                                 if sessions else None),
        "max_profit_to_loss_trades": len(givebacks) if have_peaks or not closed
        else None,
    }
    exits = Counter(trade_table.result_for(t["reason"], t["pnl"] or 0)
                    for t in closed)
    return {"start": s, "end": e, "sessions": sessions,
            "sessions_in_period": len(sessions_all), "fraction": frac,
            "opened": opened, "closed": closed, "wins": wins,
            "realized": realized, "open_pnl": open_pnl,
            "captured": captured, "peak_total": peak_total,
            "givebacks": givebacks, "late": late, "missed": missed,
            "starts": seen_at, "exits": exits, "actual": actual,
            "funnel": funnel(conn, s, e), "ops": ops_events(conn, s, e)}


def judge(key: str, target, actual, frac: float, monthly: bool):
    """MET / MISSED / NO DATA, against the pro-rated target where the
    period is still running."""
    if actual is None:
        return "NO DATA", target
    if key == "monthly_pnl_usd":
        goal = round(target * (frac if monthly else 1.0), 2)
        return ("MET" if actual >= goal else "MISSED"), goal
    if key == "max_profit_to_loss_trades":
        return ("MET" if actual <= target else "MISSED"), target
    return ("MET" if actual >= target else "MISSED"), target


def _probation_slots(config: dict = None) -> int:
    """The live setup_probation.max_concurrent, read from the config FILE so
    a proposal never repeats a change that has already shipped."""
    if config is None:
        try:
            with open("bot_config.json", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    try:
        return int((config.get("setup_probation") or {}).get("max_concurrent", 1))
    except (TypeError, ValueError):
        return 1


# ============================================================ exposure guard
# CEO, S26: a proposal that INCREASES EXPOSURE (slots, max_positions,
# risk %, sizing, a wider entry window, a looser filter) must cite the
# outcome tracker's replay of the signals it would have let through -
# n, win %, avg R - and with fewer than MIN_EVIDENCE replayed outcomes it
# proposes nothing. A study, an exit change that takes risk off, or an
# uptime fix is not exposure.

MIN_EVIDENCE = 10
INSUFFICIENT = "insufficient evidence, no change proposed"
EXPOSURE_PATTERN = re.compile(
    r"max_concurrent|max_positions|risk_per_trade|risk %|risk_pct|"
    r"\bsizing\b|position size|conviction_size_mult|entry window|"
    r"use_time_filter|outside_hours|conviction threshold|"
    r"claude_conviction_threshold|adx_threshold|rsi_threshold|"
    r"volume_multipliers|spy_filter_exempt|loosen|relax|lower the",
    re.I)


def exposure(text: str, evidence: tuple, label: str) -> dict:
    """An exposure-increasing proposal and the tracker reason keys whose
    replays are its evidence."""
    return {"text": text, "exposure": True, "evidence": tuple(evidence),
            "label": label}


def tracker_evidence(conn, reason_keys) -> dict:
    """n / win % / avg R of RESOLVED replays in rejection_outcomes for these
    reason keys (target, stop, or neither at the 10-session horizon)."""
    if not reason_keys or conn is None:
        return {"n": 0, "win_pct": None, "avg_r": None}
    marks = ",".join("?" * len(reason_keys))
    try:
        n, wins, avg = conn.execute(
            f"SELECT count(*), sum(r_achieved > 0), avg(r_achieved) "
            f"FROM rejection_outcomes WHERE reason_key IN ({marks}) "
            f"AND resolved = 1 AND r_achieved IS NOT NULL "
            f"AND outcome IN ('target','stop','neither')",
            tuple(reason_keys)).fetchone()
    except sqlite3.Error:
        return {"n": 0, "win_pct": None, "avg_r": None}
    n = n or 0
    return {"n": n, "win_pct": (100.0 * (wins or 0) / n) if n else None,
            "avg_r": avg if n else None}


def blocker_stats(conn, start: str, end: str) -> list:
    """CEO 2026-10-22: per blocker (rejection reason) for signals decided in
    [start, end]: rejected n, graded n (the tracker has a resolved replay -
    needs entry/stop/target, journaled on every rules rejection from
    2026-10-22), win % and avg R of the graded ones. Most rejected first."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT reason_key, count(*), "
            "sum(CASE WHEN resolved=1 AND r_achieved IS NOT NULL AND outcome IN "
            "('target','stop','neither') THEN 1 ELSE 0 END), "
            "sum(CASE WHEN resolved=1 AND r_achieved > 0 AND outcome IN "
            "('target','stop','neither') THEN 1 ELSE 0 END), "
            "avg(CASE WHEN resolved=1 AND r_achieved IS NOT NULL AND outcome IN "
            "('target','stop','neither') THEN r_achieved END) "
            "FROM rejection_outcomes WHERE decided_date BETWEEN ? AND ? "
            "GROUP BY reason_key ORDER BY count(*) DESC", (start, end)).fetchall()
    except sqlite3.Error:
        return []
    return [{"blocker": k, "rejected": n, "graded": g or 0,
             "win_pct": (100.0 * (w or 0) / g) if g else None,
             "avg_r": a if g else None} for k, n, g, w, a in rows]


def blocker_lines(conn, start: str, end: str) -> list:
    rows = blocker_stats(conn, start, end)
    out = [f"### Blockers ({start} .. {end})", "",
           "| Blocker | Rejected | Graded | Win % | Avg R |", "|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['blocker']} | {r['rejected']} | {r['graded']} | "
                   + (f"{r['win_pct']:.0f}%" if r["win_pct"] is not None else "-")
                   + " | " + (f"{r['avg_r']:+.2f}" if r["avg_r"] is not None else "-")
                   + " |")
    if not rows:
        out.append("| (no rejections) | 0 | 0 | - | - |")
    out += ["", "_Graded = the outcome tracker replayed the rejected signal "
            "through its would-be entry/stop/target to a target, a stop or the "
            "10-session horizon. Rejections journaled before 2026-10-22 carry "
            "no geometry and stay ungraded._"]
    return out


def guard(fix, conn) -> str:
    """The printed Fix line for one miss. Exposure proposals - flagged, or
    caught by EXPOSURE_PATTERN - need >= MIN_EVIDENCE tracker replays."""
    if isinstance(fix, dict):
        text, evidence, label = fix["text"], fix["evidence"], fix["label"]
        is_exposure = fix.get("exposure", False)
    else:
        text, evidence, label = fix, (), "the change"
        is_exposure = bool(EXPOSURE_PATTERN.search(text))
    if not is_exposure:
        return f"Fix ({PROPOSAL}): {text}"
    ev = tracker_evidence(conn, evidence)
    if ev["n"] < MIN_EVIDENCE:
        keys = ", ".join(evidence) or "none recorded"
        return (f"Fix: {INSUFFICIENT} (exposure increase; outcome tracker "
                f"has n={ev['n']} replayed outcome(s) of signals blocked by "
                f"{label} [{keys}]; >= {MIN_EVIDENCE} required)")
    return (f"Fix ({PROPOSAL}): {text}. Evidence (outcome tracker, signals "
            f"blocked by {label}): n={ev['n']} replayed, win "
            f"{ev['win_pct']:.0f}%, avg R {ev['avg_r']:+.2f}")


# Ops events the cause analysis reads BEFORE proposing a rule change
# (CEO 2026-10-17, D). The S23 entry-fill fix journals corrected fills as
# entry_reconciled; fill_corrected is read too, in case a later path uses
# that name.
OPS_KINDS = ("fill_corrected", "entry_reconciled", "target_capped",
             "process_suspended", "network_unavailable")


def ops_events(conn, start: str, end: str) -> Counter:
    """Counter of the OPS_KINDS journaled in [start, end]."""
    if conn is None:
        return Counter()
    marks = ",".join("?" * len(OPS_KINDS))
    try:
        rows = conn.execute(
            f"SELECT setup_name, COUNT(*) FROM decisions WHERE source='ops' "
            f"AND setup_name IN ({marks}) AND substr(timestamp,1,10) BETWEEN ? AND ? "
            f"GROUP BY setup_name", (*OPS_KINDS, start, end)).fetchall()
    except sqlite3.Error:
        return Counter()
    return Counter({k: n for k, n in rows})


def exit_rule_text(config: dict = None) -> str:
    """The daily exit rule IN FORCE, written from bot_config.json - so a
    proposal never describes a rule the desk no longer runs."""
    import position_mgmt as pm
    if config is None:
        try:
            with open("bot_config.json", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    profile = ((config or {}).get("risk_profiles") or {}).get("Moderate") or {}
    ttype = pm.trailing_type_for({"timeframe": "daily"}, profile)
    if ttype == pm.R_ATR:
        p = pm.r_atr_params(profile)
        steps = ", ".join(f"+{a:g}R -> " + ("entry" if b == 0 else
                                            f"entry {b:+g}R")
                          for a, b in p["steps"])
        x = pm.daily_exit_params(profile)
        return (f"R/ATR ladder: {steps}; then highest daily close - "
                f"{p['trail_atr']:g}xATR{int(p['atr_length'])}; time stop day "
                f"{x['time_stop_day']:g} if MFE < +{x['time_stop_min_mfe_r']:g}R, "
                f"day {x['max_hold_days']:g} always")
    if ttype == pm.LADDER:
        p = pm.ladder_params(profile)
        return (f"owner ladder: breakeven at +{p['start_pct']:g}%, trail "
                f"{p['cushion_pct']:g}% under the high close")
    if ttype == pm.PCT_LOCK:
        return "percent lock (step1 +0.75%, step2 +1.25%, trail from +2%)"
    return f"daily rule '{ttype}'"


def _ops_note(ops: Counter, kinds) -> str:
    hits = [f"{k} {ops[k]}" for k in kinds if ops.get(k)]
    return f" Ops: {', '.join(hits)}." if hits else ""


def why_and_fix(key: str, ev: dict, goals: dict) -> tuple:
    """(why with numbers, one proposed fix) for a goal. Ops events are read
    first: when the journal shows an operational cause for the miss, the
    fix addresses it before any rule change is proposed."""
    why, fix = _why_and_fix(key, ev, goals)
    ops = ev.get("ops") or Counter()
    if key == "on_time_sessions_pct":
        why += _ops_note(ops, ("process_suspended", "network_unavailable"))
        if ops.get("network_unavailable") or ops.get("process_suspended"):
            fix = (f"operational first: {ops.get('network_unavailable', 0)} "
                   f"network failure(s) and {ops.get('process_suspended', 0)} "
                   f"machine-sleep suspension(s) in the period - fix the "
                   f"network / sleep cause before anything else")
    elif key in ("monthly_pnl_usd", "capture_ratio_pct",
                 "max_profit_to_loss_trades", "win_rate_pct"):
        why += _ops_note(ops, ("fill_corrected", "entry_reconciled",
                               "target_capped", "process_suspended"))
        fills = ops.get("fill_corrected", 0) + ops.get("entry_reconciled", 0)
        if fills and key in ("monthly_pnl_usd", "win_rate_pct"):
            fix = (f"check fill accuracy first: {fills} entry fill(s) were "
                   f"corrected in the period - confirm the journal P&L "
                   f"before reading it as a strategy result")
        elif ops.get("target_capped") and key in ("capture_ratio_pct",
                                                  "max_profit_to_loss_trades"):
            fix = (f"{ops['target_capped']} target(s) were capped at the ATR "
                   f"cap - measure whether the cap, not the exit rule "
                   f"({exit_rule_text()}), cut these trades before changing it")
        elif ops.get("process_suspended"):
            fix = (f"{ops['process_suspended']} suspension(s): the worker was "
                   f"asleep for part of the period - fix uptime before "
                   f"judging the exit rule")
    return why, fix


def _why_and_fix(key: str, ev: dict, goals: dict) -> tuple:
    f = ev["funnel"]
    top = lambda c, n=3: ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "none"
    skipped = f["skipped_after_approval"]
    sessions = len(ev["sessions"])
    if key == "trades_per_week":
        why = (f"{len(ev['opened'])} opened in {sessions} session(s). Funnel: "
               f"{f['signals']} signals detected; rule rejections: "
               f"{top(f['rule_rejections'])}; gatekeeper {f['approvals']} "
               f"approved vs {sum(f['gatekeeper_blocks'].values())} blocked "
               f"({top(f['gatekeeper_blocks'])}); approvals skipped: "
               f"{top(skipped)}; late/missed sessions "
               f"{len(ev['late'])}/{len(ev['missed'])}.")
        if skipped.get("probation slot"):
            n = _probation_slots()
            fix = exposure(f"raise setup_probation.max_concurrent {n} -> "
                           f"{n + 1}", ("probation_position_open",),
                           "the probation slot")
        elif ev["missed"] or ev["late"]:
            fix = "fix uptime first (missed/late sessions cost whole days)"
        elif f["approvals"] == 0 and sum(f["gatekeeper_blocks"].values()):
            fix = exposure("study lowering the gatekeeper's conviction "
                           "threshold 70", ("conviction < 70",),
                           "the conviction threshold (approvals below 70 are "
                           "not graded by the tracker)")
        else:
            fix = ("commission the journal-only oversold_bounce shadow lane "
                   "(agenda 14) to add chop-session supply")
    elif key == "monthly_pnl_usd":
        why = (f"realized ${ev['realized']:+,.2f} on {len(ev['closed'])} "
               f"closed trade(s) ({len(ev['wins'])} won); open P&L "
               + (f"${ev['open_pnl']:+,.2f}" if ev["open_pnl"] is not None
                  else "n/a")
               + f"; {len(ev['opened'])} opened; exits: {top(ev['exits'], 6)}.")
        fix = ("address the trade count first - at ~+0.3R/trade and $50 "
               "risk, $100/month needs ~7 closed trades"
               if len(ev["closed"]) < 7 else
               "review exits: the lock/trail is giving back gains")
    elif key == "win_rate_pct":
        why = (f"{len(ev['wins'])} of {len(ev['closed'])} closed trade(s) "
               f"won; exits: {top(ev['exits'], 6)}.")
        fix = ("study entry quality on stopped trades (stop distance vs "
               "ATR) before changing any filter")
    elif key == "capture_ratio_pct":
        why = (f"captured ${ev['captured']:+,.2f} of ${ev['peak_total']:,.2f} "
               f"peak open profit on trades that went green.")
        fix = (f"study the first step of the live exit rule "
               f"({exit_rule_text()}) against its S29 variants, for capture "
               f"vs give-back")
    elif key == "on_time_sessions_pct":
        late = ", ".join(f"{d} {ev['starts'][d]}" for d in ev["late"]) or "none"
        why = (f"{sessions - len(ev['late']) - len(ev['missed'])} of "
               f"{sessions} session(s) started by 09:30 ET; late: {late}; "
               f"missed: {', '.join(ev['missed']) or 'none'}.")
        fix = ("check the always-awake job (com.tradingbot.awake_week, "
               "logs/awake_week.log) and that the lid stayed open")
    else:                                        # givebacks
        names = "; ".join(f"{t['ticker']} {t['buy_ts'][:10]} "
                          f"${t['pnl']:+,.2f}" for t in ev["givebacks"]) or "none"
        why = (f"{len(ev['givebacks'])} trade(s) went >= +{GIVEBACK_MIN_R}R "
               f"open and closed red: {names}.")
        fix = (f"study the step that should have protected these trades in "
               f"the live exit rule ({exit_rule_text()}) - S29 variants")
    return why, fix


LABELS = {
    "trades_per_week": "Trades / week",
    "monthly_pnl_usd": "P&L (month, $)",
    "win_rate_pct": "Win rate %",
    "capture_ratio_pct": "Capture %",
    "on_time_sessions_pct": "On-time sessions %",
    "max_profit_to_loss_trades": "Profit->loss trades",
}


def _fmt(key, v, signed=True):
    if v is None:
        return "—"
    if key == "monthly_pnl_usd":
        return f"${v:+,.2f}" if signed else f"${v:,.2f}"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def sheet_lines(ev: dict, goals: dict, title: str, monthly: bool,
                conn=None) -> list:
    partial = ev["fraction"] < 1.0
    lines = [f"### {title} ({ev['start']} .. {ev['end']}, "
             f"{len(ev['sessions'])}/{ev['sessions_in_period']} sessions"
             + (", in progress - targets pro-rated" if partial else "") + ")",
             "", "| Goal | Target | Actual | Status |", "|---|---|---|---|"]
    misses = []
    for key in GOAL_KEYS:
        if key == "monthly_pnl_usd" and not monthly:
            continue
        status, target = judge(key, goals[key], ev["actual"][key],
                               ev["fraction"], monthly)
        lines.append(f"| {LABELS[key]} | {_fmt(key, target, False)} | "
                     f"{_fmt(key, ev['actual'][key])} | **{status}** |")
        if status != "MET":
            misses.append((key, status))
    lines.append("")
    for key in GOAL_KEYS:
        if key == "monthly_pnl_usd" and not monthly:
            continue
        why, fix = why_and_fix(key, ev, goals)
        status = next((st for k, st in misses if k == key), "MET")
        lines.append(f"- **{LABELS[key]} — {status}.** Why: {why}")
        if status == "MISSED":
            lines.append(f"  - {guard(fix, conn)}")
    return lines


DEFINITIONS = (
    "_Definitions: trades = BUYs opened on the $5,000 account; P&L = "
    "realized on trades closed in the period; capture = realized / peak "
    "open profit (5-minute highs); on time = worker started by 09:30 ET "
    f"(logs/worker.log); profit->loss = up >= +{GIVEBACK_MIN_R:g}R open, closed red. "
    "A fix that increases exposure needs >= 10 outcome-tracker replays of "
    "the signals it would admit, else no change is proposed. "
    "Reporting only - goals never reach the gatekeeper, prompts, filters, "
    "sizing or orders._")


def build(asof: date, conn, broker=None, goals: dict = None,
          bars_by_ticker=None, starts: dict = None):
    """(markdown lines for '## 6. Goals', week eval, month eval)."""
    goals = goals or load_goals()
    open_pnl = None
    if broker is not None:
        try:
            open_pnl = sum(float(getattr(p, "unrealized_pl", 0) or 0)
                           for p in broker.get_positions())
        except Exception:
            open_pnl = None
    if bars_by_ticker is None and broker is not None:
        bars_by_ticker = _bars_for_closed(conn, asof, broker)
    wk = evaluate(conn, *week_bounds(asof), asof, goals, broker,
                  bars_by_ticker, starts, open_pnl)
    mo = evaluate(conn, *month_bounds(asof), asof, goals, broker,
                  bars_by_ticker, starts, open_pnl)
    lines = ["## 6. Goals", ""]
    lines += sheet_lines(wk, goals, "Week to date", monthly=False, conn=conn)
    lines += [""] + sheet_lines(mo, goals, "Month to date", monthly=True,
                                conn=conn)
    lines += [""] + blocker_lines(conn, *[d.isoformat() for d in week_bounds(asof)])
    lines += ["", DEFINITIONS]
    return lines, wk, mo


def _bars_for_closed(conn, asof: date, broker):
    first = min(week_bounds(asof)[0], month_bounds(asof)[0]).isoformat()
    tickers = sorted({t["ticker"] for t in _account_trades(conn)
                      if t["sell_ts"] and t["sell_ts"][:10] >= first})
    if not tickers:
        return {}
    days = (asof - date.fromisoformat(first)).days + 3
    try:
        return broker.get_bars(tickers, timeframe_minutes=5,
                               lookback_days=max(days, 5))
    except Exception:
        return {}


def iso_week_tag(asof: date) -> str:
    y, w, _ = asof.isocalendar()
    return f"{y}-{w:02d}"


def weekly_file(asof: date, lines: list, out_dir: str = "reports") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"goals_week_{iso_week_tag(asof)}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Goals — week {iso_week_tag(asof)} (as of {asof})\n\n"
                + "\n".join(lines[2:]) + "\n")
    return path


def discord_table(wk: dict, mo: dict, goals: dict, asof: date) -> str:
    """The weekly goal table, monospace, under Discord's limit."""
    rows = [f"{'GOAL':<20}{'TARGET':>9}{'ACTUAL':>10} STATUS"]
    for key in GOAL_KEYS:
        ev, monthly = (mo, True) if key == "monthly_pnl_usd" else (wk, False)
        status, target = judge(key, goals[key], ev["actual"][key],
                               ev["fraction"], monthly)
        rows.append(f"{LABELS[key][:20]:<20}{_fmt(key, target, False):>9}"
                    f"{_fmt(key, ev['actual'][key]):>10} {status}")
    body = "\n".join(rows)
    return (f"**Goals — week {iso_week_tag(asof)}** (P&L month to date)\n"
            f"```\n{body}\n```\nWhy and proposed fixes: "
            f"reports/goals_week_{iso_week_tag(asof)}.md")
