"""
study_trade_funnel.py — S21. Where do signals die between detection and fill,
what would the dead ones have done, and how many trades a week would the desk
take under five rule variants?

    python study_trade_funnel.py              # print the report
    python study_trade_funnel.py --out reports/s21_trade_funnel.md

STUDY ONLY. Reads journal.db (read-only URI) and bar caches in data/s21/
(fetched once from Alpaca market data if absent). Writes nothing to the
journal, bot_config.json, positions or any strategy file.

Window: the 30 NYSE sessions ending 2026-10-12 (2026-08-31 .. 2026-10-12).

THREE PARTS
-----------
1. FUNNEL. Every worker decision row (sources rules + claude, the five live
   setup names - CEO order-sheet rows are excluded) is grouped into a unique
   SIGNAL = (ET date, ticker, setup, signal bar). Each signal is assigned the
   FURTHEST gate it reached, in the live pipeline order mapped from
   streamlit_app._worker_loop. A gatekeeper "approval" below the conviction
   threshold (70) is a rejection - the worker treats it as one.

2. REPLAY. Each rejected signal is given entry/stop/target - from the journal
   where it carries them (gatekeeper rows, check_signal/size_zero details),
   otherwise RECONSTRUCTED with the detector's own geometry formula on the
   same bar - and walked forward 10 sessions:
     daily setups: entry ~ the decision session's open, the decision session
       is bar 1 (a bar spanning both levels hits the STOP first), live exit
       rules: bracket + the ratified 1R -> breakeven profit lock.
     trend_continuation: 5-minute bars (the live timeframe and IEX feed),
       entry at the open of the bar after the signal bar, stop = entry -
       2.0 x ATR14, 3R target, static bracket, regular-hours bars only.
   Not 10 sessions old by 2026-10-12 -> marked to the last close and counted
   as `unresolved`.

3. SCENARIOS. A capacity simulation over 30 sessions at full uptime: each
   simulated session is a resampled real session's stream of signals that
   cleared the gatekeeper (with their replayed outcome and holding time),
   pushed through probation (one open position per probation setup),
   max_positions (4), one position per ticker and whole-share sizing at the
   $5,000 cap via risk.position_size. 2,000 runs per scenario.

Everything here is an estimate built on 17 gatekeeper passes. Read the
ranges, not the decimals.
"""

import argparse
import json
import math
import os
import pickle
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_ta as ta

import backtest
import outcomes
import risk

ET = ZoneInfo("America/New_York")
WINDOW_END = "2026-10-12"
N_SESSIONS = 30
HORIZON = 10
CACHE = os.path.join("data", "s21")
EQUITY = 5000.0
SETUPS = ("trend_continuation", "momentum_continuation",
          "mean_reversion_reclaim", "pullback_in_uptrend",
          "post_earnings_continuation")
DAILY = {s for s in SETUPS if s != "trend_continuation"}
GK_THRESHOLD = 70
LOCK_STEPS = ((1.0, 0.0),)          # daily_profit_lock, ratified S16
TREND_ATR_MULT = 2.0                # Moderate profile (every ticker here)
RUNS = 2000
# The live book has no time stop: a position holds until stop, target or
# lock. The CAPACITY simulation therefore walks each trade to the end of the
# data (LONG), and a trade still open there holds at least MIN_OPEN_HOLD.
LONG = 60
MIN_OPEN_HOLD = 10

# Pipeline order (streamlit_app._worker_loop). A signal's stage is the
# furthest of these it reached.
STAGES = [
    ("rules filter", {"adx_low", "rsi_low", "no_confirmation", "volume_low",
                      "invalid_atr", "invalid_stop", "below_ema9",
                      "gap_below_reclaim_mid", "change_too_small",
                      "not_first_close_above", "no_earnings_event",
                      "earnings_calendar_unavailable", "emas_not_rising",
                      "pullback_volume_not_declining", "not_first_trigger",
                      "gap_below_signal_mid"}),
    ("circuit breaker", {"circuit_breaker"}),
    ("regime (SPY chop)", {"spy_bearish"}),
    ("entry window", {"outside_hours"}),
    ("max positions / check_signal",
     {"max_positions", "max_positions_reached", "missing_stop",
      "missing_target", "invalid_entry", "stop_not_below_entry",
      "reward_risk_below_1.5"}),
    ("gatekeeper", set()),           # claude rows
    ("approved, no fill row", set()),  # cleared >=70; terminal if nothing later
    ("probation", {"probation_position_open"}),
    ("size", {"size_zero", "price_too_high_for_account",
              "notional_exceeds_max_position_pct",
              "insufficient_cash_no_margin"}),
    ("duplicate", {"duplicate_entry_blocked"}),
    ("filled", set()),
]
STAGE_RANK = {name: i for i, (name, _) in enumerate(STAGES)}
REASON_STAGE = {r: name for name, rs in STAGES for r in rs}


# ============================================================ data

def load_bars(refresh=False):
    """{ticker: daily df}, {ticker: 5-min df} - cached in data/s21/."""
    dpath, fpath = (os.path.join(CACHE, "daily.pkl"),
                    os.path.join(CACHE, "five.pkl"))
    if refresh or not (os.path.exists(dpath) and os.path.exists(fpath)):
        from broker import Broker
        os.makedirs(CACHE, exist_ok=True)
        rows = load_rows()
        tickers = sorted({r["ticker"] for r in rows})
        trend = sorted({r["ticker"] for r in rows
                        if r["setup"] == "trend_continuation"})
        b, daily, five = Broker(), {}, {}
        allt = tickers + ["SPY"]
        for i in range(0, len(allt), 50):
            daily.update(b.get_daily_bars(allt[i:i + 50], lookback_days=200))
        for i in range(0, len(trend), 20):
            five.update(b.get_bars(trend[i:i + 20], timeframe_minutes=5,
                                   lookback_days=52))
        pickle.dump(daily, open(dpath, "wb"))
        pickle.dump(five, open(fpath, "wb"))
    return pickle.load(open(dpath, "rb")), pickle.load(open(fpath, "rb"))


def session_dates(spy):
    days = [str(t)[:10] for t in spy.index if str(t)[:10] <= WINDOW_END]
    return days[-N_SESSIONS:], days


def load_rows():
    conn = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    first = "2026-08-31"
    q = ("SELECT id, timestamp, ticker, setup_name, source, approved, "
         "conviction_score, context, verdict FROM decisions "
         "WHERE source IN ('rules','claude') AND substr(timestamp,1,10) "
         "BETWEEN ? AND ? ORDER BY id")
    rows = []
    for r in conn.execute(q, (first, WINDOW_END)):
        if r["setup_name"] not in SETUPS:
            continue                    # CEO order-sheet rules rows
        try:
            ctx = json.loads(r["context"] or "{}")
        except ValueError:
            ctx = {}
        try:
            verdict = json.loads(r["verdict"] or "{}")
        except ValueError:
            verdict = {}
        rows.append({
            "id": r["id"], "ts": r["timestamp"], "date": r["timestamp"][:10],
            "ticker": r["ticker"], "setup": r["setup_name"],
            "source": r["source"], "approved": r["approved"],
            "conviction": r["conviction_score"], "ctx": ctx,
            "context_raw": r["context"],
            "reason": verdict.get("rejection_reason"),
            "tag": verdict.get("tag"),
        })
    fills = {r["decision_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE action='BUY' AND decision_id IS NOT NULL "
        "AND substr(timestamp,1,10) BETWEEN ? AND ?", (first, WINDOW_END))}
    conn.close()
    load_rows.fills = fills
    return rows


def _db():
    import journal
    return journal.DB_FILE


# ============================================================ funnel

def _prev_session(all_days, d):
    i = all_days.index(d) if d in all_days else None
    return all_days[i - 1] if i else None


def _trend_bar_for(ts_et: str) -> str:
    """The signal bar a trend gatekeeper call judged: the completed 5-min bar
    before the one forming at call time, as the UTC key the rules rows use."""
    t = datetime.fromisoformat(ts_et).replace(tzinfo=ET)
    start = t - timedelta(minutes=t.minute % 5 + 5, seconds=t.second,
                          microseconds=t.microsecond)
    return str(pd.Timestamp(start).tz_convert("UTC"))


def signal_key(row, all_days):
    if row["setup"] in DAILY:
        bar = (row["ctx"].get("bar") or "")[:10] or _prev_session(
            all_days, row["date"])
    else:
        # Day-deduped rules rows (spy_bearish, outside_hours...) carry no
        # bar; the first row of the day was written on the signal bar's
        # successor, exactly like a gatekeeper call.
        bar = row["ctx"].get("bar") or _trend_bar_for(row["ts"])
    return (row["date"], row["ticker"], row["setup"], bar)


def classify(row):
    """(stage, reason) for one row, or None for a non-gating row."""
    if row["source"] == "claude":
        if row["approved"] and (row["conviction"] or 0) >= GK_THRESHOLD:
            return "approved, no fill row", "gatekeeper approved"
        if row["approved"]:
            return "gatekeeper", f"conviction < {GK_THRESHOLD}"
        return "gatekeeper", outcomes.reason_key("claude", row["reason"])
    if row["tag"] == "chop_reclaim" or row["approved"]:
        return None                     # Rule #5 tag, not a gate
    reason = row["reason"] or "unspecified"
    return REASON_STAGE.get(reason, "rules filter"), reason


def build_signals(rows, all_days):
    """{key: signal} with the furthest stage reached and every row seen."""
    fills = load_rows.fills
    signals = {}
    for row in rows:
        key = signal_key(row, all_days)
        sig = signals.setdefault(key, {
            "key": key, "date": key[0], "ticker": key[1], "setup": key[2],
            "bar": key[3], "rows": [], "stage": None, "reason": None,
            "chop_tag": False, "conviction": None, "first_ts": row["ts"]})
        sig["rows"].append(row)
        if row["tag"] == "chop_reclaim":
            sig["chop_tag"] = True
        got = classify(row)
        if got is None:
            continue
        stage, reason = got
        if row["source"] == "claude" and row["id"] in fills:
            stage, reason = "filled", "filled"
        if row["source"] == "claude":
            sig["conviction"] = row["conviction"]
            sig["gk_ts"] = row["ts"]
        if sig["stage"] is None or STAGE_RANK[stage] >= STAGE_RANK[sig["stage"]]:
            sig["stage"], sig["reason"] = stage, reason
    # A post-gatekeeper rules row (probation, size...) for a trend signal is
    # keyed to its own bar; fold daily ones onto their gatekeeper signal.
    by_day = defaultdict(list)
    for s in signals.values():
        by_day[(s["date"], s["ticker"], s["setup"])].append(s)
    for group in by_day.values():
        gk = [s for s in group if any(r["source"] == "claude" for r in s["rows"])]
        post = [s for s in group if s["stage"] in
                ("probation", "size", "duplicate") and s not in gk]
        for p in post:
            if gk:
                target = gk[-1]
                if STAGE_RANK[p["stage"]] > STAGE_RANK[target["stage"]]:
                    target["stage"], target["reason"] = p["stage"], p["reason"]
                target["rows"] += p["rows"]
                p["stage"] = "merged"
    return {k: s for k, s in signals.items()
            if s["stage"] not in (None, "merged")}


# ============================================================ geometry

def journal_geometry(sig):
    for row in sig["rows"]:
        geo = outcomes.geometry(row["context_raw"])
        if geo is not None:
            return geo, "journal"
    return None, None


def daily_geometry(sig, daily):
    """The detector's formula on the same bar. entry ~ decision-day open."""
    df = daily.get(sig["ticker"])
    if df is None:
        return None
    dates = [str(t)[:10] for t in df.index]
    if sig["date"] not in dates or sig["bar"] not in dates:
        return None
    d, s = dates.index(sig["date"]), dates.index(sig["bar"])
    if d != s + 1 or s < 21:
        return None
    entry = float(df["open"].iloc[d])
    stop = float(df["low"].iloc[s])
    if stop >= entry:
        return None
    if sig["setup"] == "mean_reversion_reclaim":
        high_20 = float(df["high"].iloc[s - 20:s].max())
        target = max(high_20, entry + (entry - stop) * 3.0)
    elif sig["setup"] == "momentum_continuation":
        target = entry + (entry - stop) * 3.0
    else:
        return None                     # pullback / earnings: not rebuilt
    return entry, stop, target


def _rth(df):
    idx = df.index.tz_convert(ET)
    mins = idx.hour * 60 + idx.minute
    return df[(mins >= 570) & (mins < 960)]


def trend_geometry(sig, five):
    df = five.get(sig["ticker"])
    if df is None:
        return None, None
    try:
        bar = pd.Timestamp(sig["bar"])
    except ValueError:
        return None, None
    if bar not in df.index:
        return None, None
    j = df.index.get_loc(bar)
    if j + 1 >= len(df) or j < 30:
        return None, None
    window = df.iloc[max(0, j - 400):j + 1]
    atr = ta.atr(window["high"], window["low"], window["close"], length=14)
    if atr is None or atr.dropna().empty:
        return None, None
    atr_v = float(atr.iloc[-1])
    entry = float(df["open"].iloc[j + 1])
    stop = entry - atr_v * TREND_ATR_MULT
    if atr_v <= 0 or stop >= entry:
        return None, None
    return (entry, stop, entry + (entry - stop) * 3.0), df.index[j + 1]


# ============================================================ replay

def walk(bars, entry, stop, target, steps=(), first_is_entry=True):
    """bars: [(o,h,l,c)]. Bar 0 is the entry bar (entry ~ its open). Returns
    (R, exit, bars_used). Same fill rules as backtest.simulate_bracket."""
    risk_ps = entry - stop
    live = stop
    for n, (o, h, l, c) in enumerate(bars):
        if n > 0 and o <= live:
            return (o - entry) / risk_ps, "stop" if live < entry else "lock", n
        if n > 0 and o >= target:
            return (o - entry) / risk_ps, "target", n
        if l <= live:
            return ((live - entry) / risk_ps,
                    "stop" if live < entry else "lock", n)
        if h >= target:
            return (target - entry) / risk_ps, "target", n
        close_r = (c - entry) / risk_ps
        for trig, to in steps:
            cand = entry + to * risk_ps
            if close_r >= trig and live < cand < c:
                live = cand
    if not bars:
        return None, "no_bars", 0
    return (bars[-1][3] - entry) / risk_ps, "time", len(bars) - 1


def replay_daily(sig, geo, daily, all_days, horizon=HORIZON):
    df = daily.get(sig["ticker"])
    if df is None:
        return None
    dates = [str(t)[:10] for t in df.index]
    if sig["date"] not in dates:
        return None
    d = dates.index(sig["date"])
    seg = df.iloc[d:d + horizon]
    bars = list(zip(seg["open"], seg["high"], seg["low"], seg["close"]))
    entry, stop, target = geo
    if bars and bars[0][0] <= stop:
        return {"r": None, "exit": "gapped_through_stop", "sessions": 0,
                "resolved": True}
    r, how, n = walk(bars, entry, stop, target, LOCK_STEPS)
    resolved = how != "time" or len(bars) >= horizon
    return {"r": r, "exit": how if resolved else "unresolved",
            "sessions": n + 1, "resolved": resolved}


def replay_trend(sig, geo, five, start_ts, horizon=HORIZON):
    df = five.get(sig["ticker"])
    if df is None:
        return None
    rth = _rth(df)
    seg = rth[rth.index >= start_ts]
    if seg.empty:
        return None
    days = sorted({str(t.tz_convert(ET))[:10] for t in seg.index})[:horizon]
    seg = seg[[str(t.tz_convert(ET))[:10] in set(days) for t in seg.index]]
    bars = list(zip(seg["open"], seg["high"], seg["low"], seg["close"]))
    entry, stop, target = geo
    r, how, n = walk(bars, entry, stop, target, ())
    resolved = how != "time" or len(days) >= horizon
    used = len({str(t.tz_convert(ET))[:10] for t in seg.index[:n + 1]})
    return {"r": r, "exit": how if resolved else "unresolved",
            "sessions": used, "resolved": resolved}


def replay_signal(sig, daily, five, all_days):
    """Attach geometry + outcome. Returns the signal."""
    geo, src = journal_geometry(sig)
    start_ts = None
    if sig["setup"] == "trend_continuation":
        if geo is not None:
            t = datetime.fromisoformat(sig.get("gk_ts") or sig["first_ts"])
            start_ts = pd.Timestamp(t.replace(tzinfo=ET)).tz_convert("UTC")
            start_ts = start_ts.floor("5min")
        else:
            geo, start_ts = trend_geometry(sig, five)
            src = "rebuilt" if geo else None
    elif geo is None:
        geo = daily_geometry(sig, daily)
        src = "rebuilt" if geo else None
    sig["geo"], sig["geo_src"] = geo, src
    if geo is None:
        sig["out"] = None
        return sig
    if sig["setup"] == "trend_continuation":
        sig["out"] = replay_trend(sig, geo, five, start_ts)
        sig["out_long"] = replay_trend(sig, geo, five, start_ts, LONG)
    else:
        sig["out"] = replay_daily(sig, geo, daily, all_days)
        sig["out_long"] = replay_daily(sig, geo, daily, all_days, LONG)
    return sig


# ============================================================ oversold_bounce

def spy_chop_by_session(spy, sessions):
    """Live rule: the last COMPLETED SPY close <= its 20-day EMA -> chop."""
    close = spy["close"]
    ema = close.ewm(span=20, adjust=False).mean()
    dates = [str(t)[:10] for t in spy.index]
    out = {}
    for d in sessions:
        i = dates.index(d)
        out[d] = bool(close.iloc[i - 1] <= ema.iloc[i - 1])
    return out


def bounce_signals(daily, sessions, chop):
    """oversold_bounce fires (backtest.detect_oversold_bounce, entry at the
    session's open, stop = trigger-bar low, 3R) on chop sessions only."""
    out = []
    for tk, df in daily.items():
        if tk == "SPY":
            continue
        dates = [str(t)[:10] for t in df.index]
        for d in sessions:
            if not chop.get(d) or d not in dates:
                continue
            i = dates.index(d)
            if i < 41:
                continue
            got = backtest.detect_oversold_bounce(df.iloc[i - 59:i + 1])
            if not isinstance(got, dict):
                continue
            entry, stop = float(df["open"].iloc[i]), got["stop_level"]
            if stop >= entry:
                continue
            sig = {"date": d, "ticker": tk, "setup": "oversold_bounce",
                   "geo": (entry, stop, entry + (entry - stop) * 3.0)}
            sig["out"] = replay_daily(sig, sig["geo"], daily, None)
            sig["out_long"] = replay_daily(sig, sig["geo"], daily, None, LONG)
            out.append(sig)
    return out


# ============================================================ scenarios

def _candidate(sig, conviction=None):
    o = sig.get("out_long") or {}
    if o.get("r") is None:
        return None
    entry, stop, _ = sig["geo"]
    hold = o["sessions"] if o["resolved"] else max(o["sessions"], MIN_OPEN_HOLD)
    return {"ticker": sig["ticker"], "setup": sig["setup"], "entry": entry,
            "stop": stop, "r": o["r"], "hold": max(1, hold),
            "conviction": conviction or sig.get("conviction") or GK_THRESHOLD,
            "t": (sig.get("gk_ts") or sig.get("first_ts") or "09:31")[-8:]}


def simulate(day_streams, sessions_n, probation_setups, max_concurrent,
             max_positions=4, rng=None, draws=1, fixed=None, equity=EQUITY):
    """One run. day_streams: [[candidate...] per real active session] or a
    callable(rng) -> candidates for one simulated session."""
    rng = rng or random.Random(0)
    open_pos, fills = [], []
    for d in range(sessions_n):
        open_pos = [p for p in open_pos if p["until"] > d]
        if fixed is not None:
            cands = list(fixed[d]) if d < len(fixed) else []
        else:
            cands = []
            for k in range(draws):
                pick = day_streams(rng) if callable(day_streams) else \
                    rng.choice(day_streams)
                cands += [dict(c, ticker=f"{c['ticker']}#{k}")
                          for c in pick if c]
        cands.sort(key=lambda c: c["t"])
        for c in cands:
            if c.get("p", 1.0) < 1.0 and rng.random() > c["p"]:
                continue                 # gatekeeper pass, drawn
            if len(open_pos) >= max_positions:
                continue
            if any(p["ticker"] == c["ticker"] for p in open_pos):
                continue
            if c["setup"] in probation_setups and sum(
                    p["setup"] == c["setup"] for p in open_pos) >= max_concurrent:
                continue
            mult = risk.conviction_multiplier(c["conviction"],
                                              {"conviction_size_mult": {"80": 1.25}})
            notional = sum(p["notional"] for p in open_pos)
            qty = risk.position_size(equity, 1.0 * mult, c["entry"], c["stop"],
                                     open_notional_usd=notional,
                                     position_cap_pct=0.25,
                                     min_lot_tolerance=0.15)
            if qty < 1:
                continue
            open_pos.append({"ticker": c["ticker"], "setup": c["setup"],
                             "until": d + c["hold"],
                             "notional": qty * c["entry"]})
            fills.append(c["r"])
    return fills


def run_scenario(streams, probation_setups, max_concurrent, draws=1,
                 runs=RUNS):
    weeks = N_SESSIONS / 5.0
    per_week, rs = [], []
    for k in range(runs):
        fills = simulate(streams, N_SESSIONS, probation_setups, max_concurrent,
                         rng=random.Random(k), draws=draws)
        per_week.append(len(fills) / weeks)
        rs += fills
    per_week.sort()
    return {"mean": sum(per_week) / len(per_week),
            "p10": per_week[int(0.1 * len(per_week))],
            "p90": per_week[int(0.9 * len(per_week)) - 1],
            "avg_r": (sum(rs) / len(rs)) if rs else None,
            "win": (100 * sum(r > 0 for r in rs) / len(rs)) if rs else None}


# ============================================================ report

def _stats(sigs):
    rs = [s["out"]["r"] for s in sigs if s.get("out") and s["out"]["r"] is not None]
    if not rs:
        return "—", "—"
    return f"{100 * sum(r > 0 for r in rs) / len(rs):.0f}%", f"{sum(rs) / len(rs):+.2f}"


def report(refresh=False):
    daily, five = load_bars(refresh)
    sessions, all_days = session_dates(daily["SPY"])
    rows = load_rows()
    signals = build_signals(rows, all_days)
    for s in signals.values():
        replay_signal(s, daily, five, all_days)
    sigs = list(signals.values())

    active = sorted({r["date"] for r in rows if r["source"] == "rules"})
    active_n = sum(1 for d in sessions if d in active)
    chop = spy_chop_by_session(daily["SPY"], sessions)
    L = []
    P = L.append

    # ---------------------------------------------------------- 1. funnel
    P(f"Window: {sessions[0]} .. {sessions[-1]} ({len(sessions)} NYSE "
      f"sessions). Worker journaled on {active_n}; silent on "
      f"{', '.join(d for d in sessions if d not in active) or 'none'}. "
      f"SPY regime chop on {sum(chop.values())} of {len(sessions)} sessions.")
    P("")
    P("### 1. Funnel (unique signals = date x ticker x setup x signal bar)")
    P("")
    by_stage = Counter(s["stage"] for s in sigs)
    reached = len(sigs)
    P("| Stage | Removed here | Still alive after |")
    P("|---|---|---|")
    P(f"| Detected (a trigger fired) | — | {reached} |")
    alive = reached
    for name, _ in STAGES:
        if name in ("filled",):
            continue
        n = by_stage.get(name, 0)
        alive -= n
        label = {"approved, no fill row":
                 "approved (≥70) but no later row and no fill"}.get(name, name)
        P(f"| {label} | {n} | {alive} |")
    P(f"| **Filled** | | **{by_stage.get('filled', 0)}** |")
    P("")
    P("By reason and setup (signals):")
    P("")
    P("| Stage | Reason | trend | momentum | reclaim | earnings | total |")
    P("|---|---|---|---|---|---|---|")
    grid = defaultdict(Counter)
    for s in sigs:
        grid[(s["stage"], s["reason"])][s["setup"]] += 1
    for (stage, reason), c in sorted(grid.items(),
                                     key=lambda kv: (STAGE_RANK[kv[0][0]],
                                                     -sum(kv[1].values()))):
        P(f"| {stage} | {reason} | {c['trend_continuation'] or ''} | "
          f"{c['momentum_continuation'] or ''} | "
          f"{c['mean_reversion_reclaim'] or ''} | "
          f"{c['post_earnings_continuation'] or ''} | {sum(c.values())} |")
    gk = [s for s in sigs if STAGE_RANK[s["stage"]] >= STAGE_RANK["gatekeeper"]]
    gk_pass = [s for s in gk if s["stage"] not in ("gatekeeper",)]
    P("")
    P(f"Gatekeeper: {len(gk)} signals asked, {len(gk_pass)} cleared "
      f"conviction ≥ {GK_THRESHOLD} ({100 * len(gk_pass) / max(1, len(gk)):.0f}%). "
      f"Chop-reclaim exemptions used: {sum(s['chop_tag'] for s in sigs)}.")
    P("")

    # ---------------------------------------------------------- 2. replay
    P("### 2. Rejections replayed 10 sessions")
    P("")
    P("| Stage | Reason | Signals | Geometry (journal / rebuilt) | "
      "Replayed | Unresolved | Win % | Avg R |")
    P("|---|---|---|---|---|---|---|---|")
    groups = defaultdict(list)
    for s in sigs:
        groups[(s["stage"], s["reason"])].append(s)
    for (stage, reason), g in sorted(groups.items(),
                                     key=lambda kv: (STAGE_RANK[kv[0][0]],
                                                     -len(kv[1]))):
        j = sum(s["geo_src"] == "journal" for s in g)
        rb = sum(s["geo_src"] == "rebuilt" for s in g)
        rep = [s for s in g if s.get("out") and s["out"]["r"] is not None]
        unres = sum(not s["out"]["resolved"] for s in rep)
        win, avg = _stats(rep)
        P(f"| {stage} | {reason} | {len(g)} | {j} / {rb} | {len(rep)} | "
          f"{unres} | {win} | {avg} |")
    P("")
    P("By setup, every replayed rejection (filled trades excluded):")
    P("")
    P("| Setup | Replayed | Win % | Avg R |")
    P("|---|---|---|---|")
    for setup in SETUPS:
        g = [s for s in sigs if s["setup"] == setup and s["stage"] != "filled"
             and s.get("out") and s["out"]["r"] is not None]
        if g:
            win, avg = _stats(g)
            P(f"| {setup} | {len(g)} | {win} | {avg} |")
    P("")

    # ---------------------------------------------------------- 3. scenarios
    probation = {"mean_reversion_reclaim", "post_earnings_continuation",
                 "pullback_in_uptrend"}
    passed = [s for s in sigs if s["stage"] in
              ("probation", "size", "duplicate", "approved, no fill row",
               "filled")]
    gk_rate = {}
    for setup in SETUPS:
        asked = [s for s in gk if s["setup"] == setup]
        ok = [s for s in gk_pass if s["setup"] == setup]
        gk_rate[setup] = len(ok) / len(asked) if asked else 0.0
    by_day = defaultdict(list)
    for s in passed:
        c = _candidate(s)
        if c:
            by_day[s["date"]].append(c)
    active_days = [d for d in sessions if d in active]
    base = [by_day.get(d, []) for d in active_days]

    def in_d_window(s):
        t = (s["first_ts"])[11:16]
        return ("11:30" <= t < "14:00") or ("15:00" <= t < "15:30")
    hours_extra = defaultdict(list)
    for s in sigs:
        if s["stage"] == "entry window" and in_d_window(s):
            c = _candidate(s)
            if c:
                c["p"] = gk_rate.get(s["setup"], 0.0)
                hours_extra[s["date"]].append(c)
    window_d = [by_day.get(d, []) + hours_extra.get(d, []) for d in active_days]

    bounces = bounce_signals(daily, sessions, chop)
    bounce_by_day = defaultdict(list)
    for b in bounces:
        c = _candidate(b)
        if c:
            c["p"] = gk_rate["mean_reversion_reclaim"]
            c["t"] = "09:31:00"
            bounce_by_day[b["date"]].append(c)
    # A resampled session carries its own regime, so the bounce lane rides
    # along with the session it was measured on.
    lane_e = [by_day.get(d, []) + bounce_by_day.get(d, []) for d in active_days]
    lane_all = [by_day.get(d, []) + hours_extra.get(d, []) +
                bounce_by_day.get(d, []) for d in active_days]

    real_order = [by_day.get(d, []) if d in active else [] for d in sessions]
    calib_2k = simulate(None, len(sessions), probation, 1, fixed=real_order,
                        equity=1975.0)
    calib_5k = simulate(None, len(sessions), probation, 1, fixed=real_order)
    P("### 3. Trades per week at $5,000 (simulated, full uptime)")
    P("")
    P(f"Calibration: the real gatekeeper-passed stream, in date order, with "
      f"the {len(sessions) - len(active_days)} silent sessions left empty, "
      f"through the same capacity rules at the equity the desk actually had "
      f"(~$1,975: the cap rose to $5,000 on 09-21 but the old account held "
      f"~$2,000) gives {len(calib_2k)} fills; the desk filled "
      f"{by_stage.get('filled', 0)}. The same stream at $5,000 gives "
      f"{len(calib_5k)} - over these 6 weeks, "
      f"{len(calib_5k) / (N_SESSIONS / 5):.2f}/week.")
    P("")
    P("| Scenario | Trades / week (mean) | 80% range | Win % | Avg R / trade |")
    P("|---|---|---|---|---|")
    scen = [
        ("(a) current rules", base, probation, 1, 1),
        ("(b) watchlist x2", base, probation, 1, 2),
        ("(c) probation max_concurrent 2", base, probation, 2, 1),
        ("(d) entry window 09:30-15:30", window_d, probation, 1, 1),
        ("(e) oversold_bounce lane in chop", lane_e,
         probation | {"oversold_bounce"}, 1, 1),
        ("(c)+(d)+(e) together", lane_all,
         probation | {"oversold_bounce"}, 2, 1),
    ]
    results = {}
    for name, streams, prob, mc, draws in scen:
        r = run_scenario(streams, prob, mc, draws)
        results[name] = r
        P(f"| {name} | {r['mean']:.2f} | {r['p10']:.2f}–{r['p90']:.2f} | "
          f"{r['win']:.0f}% | {r['avg_r']:+.2f} |" if r["avg_r"] is not None
          else f"| {name} | {r['mean']:.2f} | — | — | — |")
    P("")
    P(f"oversold_bounce in this window: {len(bounces)} detector fires on "
      f"{sum(chop.values())} chop sessions across {len(daily) - 1} tickers; "
      f"replayed win {_stats(bounces)[0]}, avg R {_stats(bounces)[1]} "
      f"(before any gatekeeper). Gatekeeper pass rate applied: reclaim's "
      f"{100 * gk_rate['mean_reversion_reclaim']:.0f}%.")
    P(f"Entry-window (d) adds {sum(len(v) for v in hours_extra.values())} "
      f"replayable signals that the current window removed between "
      f"11:30-14:00 or 15:00-15:30, each passing the gatekeeper at its "
      f"setup's observed rate.")
    return "\n".join(L), {"signals": sigs, "results": results,
                          "gk_rate": gk_rate, "chop": chop,
                          "bounces": bounces}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    text, _ = report(a.refresh)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
