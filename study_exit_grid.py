"""
study_exit_grid.py — S29 (CEO 2026-10-17, order C). Study only: no config
or trading change.

    python study_exit_grid.py
    python study_exit_grid.py --s34 [--append]      # S34 target grid, below
    python study_exit_grid.py --s35 [--append]      # S35 pullback replay
    python study_exit_grid.py --s36 [--append]      # S36 catalyst vs rest
    python study_exit_grid.py --s44                 # S44 morning momentum
    python study_exit_grid.py --s48 [--append]      # S48 gap-free intraday rallies
    python study_exit_grid.py --s45 [--fetch] [--append]  # S45 funnel per blocker

A 5-minute replay of the LIVE daily rule as built, then six variants:

  +1R step   a) entry - 0.25R (current)      b) breakeven (entry)
  after the S28 ATR target cap (target = fill + min(3R, 3.0 x ATR14)):
             1) current (initial stop clamped 1.5-2.5 ATR)
             2) initial stop clamped at most 2.0 ATR
             3) skip the trade if the capped reward:risk < 1.5

The live rule replayed: initial stop = structural low - 0.25 x ATR14,
distance clamped to [1.5, max] ATR; +1R -> (step), +1.5R -> entry,
+2R -> entry + 1R, beyond -> highest completed daily close - 2.5 x ATR14;
steps on completed 5-minute closes of bars starting >= 10:00 ET; time stop
at the 15:55 ET bar of day 5 if MFE < +0.5R, day 10 regardless. Fills: a
bar opening through a level fills at its open; a bar spanning stop and
target hits the stop first. Earnings exits are NOT modelled (no historical
calendar). ATR14 is the daily ATR as of the previous completed day.

Entries: every backtest signal for the two daily setups whose entry day is
in the 5-minute bar window (data/m5_<SYM>.csv, ~6 months), entered at that
day's open. One position per symbol at a time per variant.

Metrics: trades, skipped, win %, expectancy (R), PF, MaxDD (R), capture
(sum realized R / sum MFE R over trades that went green), give-back (% of
trades that reached >= +1R and closed at a loss - the goals.py definition).
"""

import math
import os
import sys
from datetime import date

import pandas as pd

import position_mgmt as pm
import study_common as sc

SETUPS = ("momentum_continuation", "mean_reversion_reclaim")
M5_DIR = "data"
ET = "America/New_York"
VARIANTS = [
    (f"{step_label} / {rr_label}", step_r, max_atr, skip_rr)
    for step_label, step_r in (("+1R->-0.25R", -0.25), ("+1R->BE", 0.0))
    for rr_label, max_atr, skip_rr in (("current", 2.5, None),
                                       ("stop<=2.0ATR", 2.0, None),
                                       ("skip R:R<1.5", 2.5, 1.5))
]


def load_m5(symbol):
    path = os.path.join(M5_DIR, f"m5_{symbol}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    return df


def atr_by_day(daily):
    """{date: ATR14 as of the previous completed day}."""
    import pandas_ta as ta
    series = ta.atr(daily["high"], daily["low"], daily["close"], length=14)
    days = [pd.Timestamp(i).date() for i in daily.index]
    out, prev = {}, None
    for d, v in zip(days, series):
        out[d] = prev                          # value known BEFORE day d
        prev = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    return out


def simulate(m5, daily_close, atr_day, entry_day, entry, stop, target, step_r):
    """(exit_price, reason, mfe_r) or None while still open at data end."""
    risk = entry - stop
    steps = [(1.0, step_r), (1.5, 0.0), (2.0, 1.0)]
    bars = m5[m5.index.normalize() >= pd.Timestamp(entry_day).tz_convert(ET).normalize()]
    if bars.empty:
        return None
    days = sorted({t.date() for t in bars.index})
    day_no = {d: i + 1 for i, d in enumerate(days)}
    live, hc, mfe, first = stop, entry, 0.0, True
    entry_date = days[0]
    for ts, b in bars.iterrows():
        o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]),
                      float(b["close"]))
        d = ts.date()
        if not first and o <= live:
            return o, "stop (gap)", mfe
        if not first and o >= target:
            return o, "target (gap)", max(mfe, (o - entry) / risk)
        n = day_no[d]
        if (ts.hour, ts.minute) == (15, 55) and (n >= 10 or (n >= 5 and mfe < 0.5)):
            return o, f"time stop day {n}", mfe
        first = False
        mfe = max(mfe, (h - entry) / risk)
        if l <= live:
            return live, "stop", mfe
        if h >= target:
            return target, "target", mfe
        if (ts.hour, ts.minute) >= (10, 0):
            hc = max(hc, c)
            cands = [entry + s * risk for t, s in steps if (hc - entry) / risk >= t]
            if (hc - entry) / risk >= 2.0:
                prior = [v for dd, v in daily_close.items() if entry_date <= dd < d]
                a = atr_day.get(d)
                if prior and a:
                    cands.append(max(prior) - 2.5 * a)
            if cands:
                cand = math.ceil(max(cands) * 100 - 1e-6) / 100
                if live < cand < c:
                    live = cand
    return None


def run():
    import backtest
    daily_bars, regime = sc.load_bars()
    profile, config = sc.study_profile(), sc.study_config()
    signals = []
    for setup in SETUPS:
        for sym, df in daily_bars.items():
            if sym == "SPY":
                continue
            m5 = load_m5(sym)
            if m5 is None or m5.empty:
                continue
            first5 = m5.index[0].normalize()
            sigs, _ = backtest.collect_signals(sym, df, setup, regime, profile, config)
            for sig in sigs:
                day = pd.Timestamp(df.index[sig["entry_i"]])
                day = day.tz_localize(ET) if day.tzinfo is None else day.tz_convert(ET)
                if day.normalize() < first5 or day.normalize() > m5.index[-1].normalize():
                    continue
                signals.append((setup, sym, day, float(sig["entry"]), float(sig["stop"])))
    results = {}
    for label, step_r, max_atr, skip_rr in VARIANTS:
        params = dict(pm.R_ATR_DEFAULTS, initial_stop_max_atr=max_atr)
        trades, skipped, busy = [], 0, {}
        for setup, sym, day, entry, structural in sorted(signals, key=lambda s: s[2]):
            if sym in busy and day.date() <= busy[sym]:
                continue
            daily = daily_bars[sym]
            atrs = atr_by_day(daily)
            atr = atrs.get(day.date())
            if not atr:
                continue
            stop = pm.initial_stop(entry, structural, atr, params)
            if stop >= entry:
                continue
            target = pm.capped_target(entry, stop, atr, 3.0, 3.0)
            if skip_rr and (target - entry) / (entry - stop) < skip_rr:
                skipped += 1
                continue
            closes = {pd.Timestamp(i).date(): float(v)
                      for i, v in daily["close"].items()}
            out = simulate(load_m5(sym), closes, atrs, day, entry, stop, target, step_r)
            if out is None:
                busy[sym] = load_m5(sym).index[-1].date()
                continue
            px, why, mfe = out
            busy[sym] = day.date()
            trades.append({"setup": setup, "r": (px - entry) / (entry - stop),
                           "mfe_r": mfe, "why": why})
        results[label] = (trades, skipped)
    return results, len(signals)


def metrics(trades):
    st = sc.aggregate(trades)
    green = [t for t in trades if t["mfe_r"] > 0]
    capture = (sum(t["r"] for t in green) / sum(t["mfe_r"] for t in green)
               if green and sum(t["mfe_r"] for t in green) > 0 else None)
    giveback = (100 * sum(1 for t in trades if t["mfe_r"] >= 1.0 and t["r"] < 0)
                / len(trades)) if trades else None
    return st, capture, giveback


def render(results, n_signals):
    lines = [f"**S29 (order C) - 5-minute replay of the live daily rule, "
             f"{n_signals} backtest signals in the bar window.** Study only.", ""]
    rows = []
    for setup in (None,) + SETUPS:
        for label, *_ in VARIANTS:
            trades, skipped = results[label]
            ts = [t for t in trades if setup is None or t["setup"] == setup]
            st, cap, gb = metrics(ts)
            rows.append([setup or "all", label, st["trades"], skipped if setup is None else "",
                         f"{st['win_rate']}%" if st["trades"] else "-",
                         f"{st['expectancy_r']:+.3f}" if st["trades"] else "-",
                         st["profit_factor"] if st["trades"] else "-",
                         st["max_drawdown_r"] if st["trades"] else "-",
                         f"{cap * 100:.0f}%" if cap is not None else "-",
                         f"{gb:.1f}%" if gb is not None else "-"])
    lines += sc.table(["Setup", "Variant", "Trades", "Skipped", "Win %",
                       "Expectancy (R)", "PF", "MaxDD (R)", "Capture",
                       "Give-back (+1R then loss)"], rows)
    return lines


# =========================================================== S34 (2026-10-20)
# CEO order S34 item 1. Study only. The LIVE daily rule as it stands today
# (S29 adopted: +1R -> breakeven; +1.5R -> entry; +2R -> entry + 1R; then
# highest daily close - 2.5 x ATR14; time stop day 5 if MFE < +0.5R, day 10
# regardless; S28 cap target = fill + min(kR, 3.0 x ATR14)), with the target
# k in {1.5, 2, 2.5, 3}R, each with and without "sell half at +1R" (half the
# position exits at entry + 1R the first time a bar trades there - at the
# open if it gaps through; the other half runs under the same rule). R of a
# split trade = 0.5 x first half + 0.5 x second half.
#
# Data, zero API: a year of 5-minute IEX bars (the S33 cache, data/s33,
# 2025-08-15..2026-10-19) and daily bars resampled from them (RTH), so the
# detectors, the ATR and the exits all read one feed. Signals: the live daily
# setups the backtest can replay - momentum_continuation, mean_reversion_
# reclaim, pullback_in_uptrend (post_earnings_continuation needs an earnings
# calendar and is left out). Live SPY gate: continuation setups only when SPY
# closed above its 20-day EMA; the reclaim is exempt (Rule #5). Entry at the
# entry day's open. One position per symbol per variant.

S34_SETUPS = ("momentum_continuation", "mean_reversion_reclaim",
              "pullback_in_uptrend")
S34_SPY_EXEMPT = ("mean_reversion_reclaim",)
S34_TARGETS = (1.5, 2.0, 2.5, 3.0)
S34_CURRENT = (3.0, False)
S34_START = "2025-10-14"
S34_CACHE = os.path.join("data", "s33")
S34_MIN_TRADES = 200


def s34_variants():
    return [(f"{k:g}R{' + half@1R' if half else ''}", k, half)
            for k in S34_TARGETS for half in (False, True)]


def load_m5_rth(symbol, cache=S34_CACHE):
    path = os.path.join(cache, f"m5_{symbol}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    hm = df.index.hour * 60 + df.index.minute
    return df[(hm >= 570) & (hm < 960)]


def daily_from_m5(m5):
    days = m5.index.normalize()
    d = m5.groupby(days).agg(open=("open", "first"), high=("high", "max"),
                             low=("low", "min"), close=("close", "last"),
                             volume=("volume", "sum"))
    d.index = pd.DatetimeIndex(d.index)
    return d


def simulate_half(m5, daily_close, atr_day, entry_day, entry, stop, target,
                  steps, half):
    """(r, reason, mfe_r) or None while open at data end. `steps` are the
    ladder's [(trigger_r, stop_r)]; `half` sells half at +1R."""
    risk = entry - stop
    bars = m5[m5.index.normalize() >= pd.Timestamp(entry_day).tz_convert(ET).normalize()]
    if bars.empty:
        return None
    days = sorted({t.date() for t in bars.index})
    day_no = {d: i + 1 for i, d in enumerate(days)}
    live, hc, mfe, first = stop, entry, 0.0, True
    entry_date, banked = days[0], None          # banked = R of the sold half
    one_r = entry + risk

    def done(px, why):
        r = (px - entry) / risk
        return ((0.5 * banked + 0.5 * r) if banked is not None else r), why, mfe

    for ts, b in bars.iterrows():
        o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]),
                      float(b["close"]))
        d = ts.date()
        if not first and o <= live:
            return done(o, "stop (gap)")
        if half and banked is None and not first and o >= one_r:
            banked = (o - entry) / risk
        if not first and o >= target:
            mfe = max(mfe, (o - entry) / risk)
            return done(o, "target (gap)")
        n = day_no[d]
        if (ts.hour, ts.minute) == (15, 55) and (n >= 10 or (n >= 5 and mfe < 0.5)):
            return done(o, f"time stop day {n}")
        first = False
        mfe = max(mfe, (h - entry) / risk)
        if l <= live:
            return done(live, "stop")
        if half and banked is None and h >= one_r:
            banked = 1.0
        if h >= target:
            return done(target, "target")
        if (ts.hour, ts.minute) >= (10, 0):
            hc = max(hc, c)
            cands = [entry + sr * risk for t, sr in steps if (hc - entry) / risk >= t]
            if (hc - entry) / risk >= 2.0:
                prior = [v for dd, v in daily_close.items() if entry_date <= dd < d]
                a = atr_day.get(d)
                if prior and a:
                    cands.append(max(prior) - 2.5 * a)
            if cands:
                cand = math.ceil(max(cands) * 100 - 1e-6) / 100
                if live < cand < c:
                    live = cand
    return None


def s34_signals(symbols, cache=S34_CACHE, start=S34_START, setups=S34_SETUPS):
    """[(setup, sym, entry day, entry, structural stop)] plus the per-symbol
    frames, with the live SPY gate applied."""
    import backtest
    spy5 = load_m5_rth("SPY", cache)
    spy_d = daily_from_m5(spy5)
    regime = backtest.spy_regime_series(spy_d)
    regime.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in regime.index])
    profile, config = sc.study_profile(), sc.study_config()
    frames, signals = {}, []
    lo = pd.Timestamp(start).date()
    for sym in symbols:
        m5 = load_m5_rth(sym, cache)
        if m5 is None or m5.empty:
            continue
        daily = daily_from_m5(m5)
        daily.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in daily.index])
        frames[sym] = (m5, daily)
        for setup in setups:
            sigs, _ = backtest.collect_signals(sym, daily, setup, regime, profile, config)
            for sig in sigs:
                day = pd.Timestamp(daily.index[sig["entry_i"]]).tz_localize(ET)
                if day.date() < lo:
                    continue
                if setup not in S34_SPY_EXEMPT and sig["regime"] != "trending":
                    continue
                signals.append((setup, sym, day, float(sig["entry"]), float(sig["stop"])))
    return signals, frames


def s34_run(signals, frames, steps=None, variants=None):
    params = dict(pm.r_atr_params(_live_profile()))
    steps = [tuple(x) for x in (steps or params["steps"])]
    cap = pm.target_cap_atr(_live_config())
    results = {}
    for label, k, half in (variants or s34_variants()):
        trades, busy = [], {}
        for setup, sym, day, entry, structural in sorted(signals, key=lambda x: x[2]):
            if sym in busy and day.date() <= busy[sym]:
                continue
            m5, daily = frames[sym]
            atrs = atr_by_day(daily)
            atr = atrs.get(day.date())
            if not atr:
                continue
            stop = pm.initial_stop(entry, structural, atr, params)
            if stop >= entry:
                continue
            target = pm.capped_target(entry, stop, atr, cap, k)
            closes = {pd.Timestamp(i).date(): float(v) for i, v in daily["close"].items()}
            out = simulate_half(m5, closes, atrs, day, entry, stop, target, steps, half)
            if out is None:
                busy[sym] = m5.index[-1].date()
                continue
            r, why, mfe = out
            busy[sym] = day.date()
            trades.append({"setup": setup, "r": r, "mfe_r": mfe, "why": why,
                           "sym": sym, "day": day.date()})
        results[label] = trades
    return results


def _live_config():
    import json
    with open("bot_config.json", encoding="utf-8") as f:
        return json.load(f)


def _live_profile():
    return _live_config()["risk_profiles"]["Moderate"]


def s34_metrics(trades):
    st, capture, _gb = metrics(trades)
    at_target = (100 * sum(1 for t in trades if t["why"].startswith("target"))
                 / len(trades)) if trades else None
    return st, capture, at_target


def s34_adopt(results):
    """The adopt rule: expectancy >= current AND higher capture, over >= 200
    trades (all setups). Returns (label or None, reason)."""
    cur_label = s34_label(*S34_CURRENT)
    cst, ccap, _ = s34_metrics(results[cur_label])
    best = None
    for label, *_ in s34_variants():
        if label == cur_label:
            continue
        st, cap, _ = s34_metrics(results[label])
        if (st["trades"] >= S34_MIN_TRADES and cst["trades"]
                and st["expectancy_r"] >= cst["expectancy_r"]
                and cap is not None and ccap is not None and cap > ccap):
            if best is None or st["expectancy_r"] > best[1]:
                best = (label, st["expectancy_r"])
    if best:
        return best[0], "meets the rule"
    return None, "no variant has expectancy >= current AND higher capture over >= 200 trades"


def s34_label(k, half):
    return f"{k:g}R{' + half@1R' if half else ''}"


def s34_render(results, n_signals, n_symbols):
    lines = [f"**S34 (CEO 2026-10-20) - target grid, study only.** 5-minute "
             f"replay of the live daily rule, {S34_START}..2026-10-19, "
             f"{n_signals} signals on {n_symbols} universe symbols; current = "
             f"3R target (S28-capped), no partial.", ""]
    rows = []
    for setup in (None,) + S34_SETUPS:
        for label, *_ in s34_variants():
            ts = [t for t in results[label] if setup is None or t["setup"] == setup]
            st, cap, tgt = s34_metrics(ts)
            rows.append([setup or "all", label + (" (current)" if label == s34_label(*S34_CURRENT) else ""),
                         st["trades"],
                         f"{st['win_rate']}%" if st["trades"] else "-",
                         f"{st['expectancy_r']:+.3f}" if st["trades"] else "-",
                         st["profit_factor"] if st["trades"] else "-",
                         st["max_drawdown_r"] if st["trades"] else "-",
                         f"{cap * 100:.0f}%" if cap is not None else "-",
                         f"{tgt:.1f}%" if tgt is not None else "-"])
    lines += sc.table(["Setup", "Target", "Trades", "Win %", "Expectancy (R)",
                       "PF", "MaxDD (R)", "Capture", "Exits at target"], rows)
    pick, why = s34_adopt(results)
    lines += ["", (f"**Adopt rule met: {pick}** (expectancy >= current and higher "
                   f"capture over >= {S34_MIN_TRADES} trades). Config change for NEW "
                   f"entries only, merged after 16:15 ET." if pick else
                   f"**Adopt rule not met** - {why}. No change."),
              "", "**Method.** Live ladder (+1R -> entry, +1.5R -> entry, +2R -> "
              "entry + 1R, then highest daily close - 2.5 x ATR14) on completed "
              "5-minute closes from 10:00 ET; time stop day 5 if MFE < +0.5R, day 10 "
              "regardless; target = fill + min(kR, 3 x ATR14). Half at +1R: half "
              "exits at entry + 1R on the first touch. Fills: a bar opening through "
              "a level fills at its open; a bar spanning stop and target takes the "
              "stop. Capture = realized R / MFE R over trades that went green. "
              "Reproduce with `python study_exit_grid.py --s34`.",
              "", "**Limits.** Today's universe (survivorship); IEX bars, daily "
              "resampled from them; earnings exits not modelled; "
              "post_earnings_continuation excluded (no historical calendar); "
              "signals, not gatekeeper-approved trades.", "", sc.UPPER_BOUND]
    return lines


def s34_main(append=False, universe_file="universe_today.json"):
    import json
    with open(universe_file, encoding="utf-8") as f:
        symbols = [c["symbol"] for c in json.load(f).get("candidates", [])
                   if c.get("symbol") != "SPY"]
    signals, frames = s34_signals(symbols)
    results = s34_run(signals, frames)
    lines = s34_render(results, len(signals), len(frames))
    print("\n".join(lines))
    if append:
        n = sc.append_agenda("Study: S34 target grid - 1.5R/2R/2.5R/3R, with and "
                             "without half at +1R (2026-10-20)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


# =========================================================== S35 (2026-10-20)
# pullback_in_uptrend, never replayed before (the 60-bar window starved it;
# backtest.DETECT_WINDOW is now 70). Same data and LIVE daily exit rule as
# S34, current target (3R, S28-capped), no partial; SPY-gated (the setup is
# trending-only). The probation bar: >= +0.10R over >= 30 trades.

S35_BAR_R, S35_BAR_TRADES = 0.10, 30


def s35_verdict(st):
    ok = (st["trades"] >= S35_BAR_TRADES and st["expectancy_r"] is not None
          and st["expectancy_r"] >= S35_BAR_R)
    return ok


def s35_main(append=False, universe_file="universe_today.json"):
    import json
    with open(universe_file, encoding="utf-8") as f:
        symbols = [c["symbol"] for c in json.load(f).get("candidates", [])
                   if c.get("symbol") != "SPY"]
    signals, frames = s34_signals(symbols, setups=("pullback_in_uptrend",))
    cur = [(s34_label(*S34_CURRENT), *S34_CURRENT)]
    trades = s34_run(signals, frames, variants=cur)[cur[0][0]]
    st, cap, tgt = s34_metrics(trades)
    first = min((x[2] for x in signals), default=None)
    ok = s35_verdict(st)
    lines = [f"**S35 (CEO 2026-10-20) - pullback_in_uptrend, first replay.** "
             f"5-minute replay of the live daily exit rule, {len(signals)} "
             f"signals on {len(frames)} universe symbols, entries "
             f"{first.date() if first is not None else '-'}..2026-10-19.", ""]
    lines += sc.table(["Setup", "Trades", "Win %", "Expectancy (R)", "PF",
                       "MaxDD (R)", "Capture", "Exits at target"],
                      [["pullback_in_uptrend", st["trades"],
                        f"{st['win_rate']}%" if st["trades"] else "-",
                        f"{st['expectancy_r']:+.3f}" if st["trades"] else "-",
                        st["profit_factor"] if st["trades"] else "-",
                        st["max_drawdown_r"] if st["trades"] else "-",
                        f"{cap * 100:.0f}%" if cap is not None else "-",
                        f"{tgt:.1f}%" if tgt is not None else "-"]])
    lines += ["", (f"**Bar met** (>= +{S35_BAR_R}R over >= {S35_BAR_TRADES} trades)."
                   if ok else
                   f"**Bar not met** (>= +{S35_BAR_R}R over >= {S35_BAR_TRADES} "
                   f"trades): the setup stays on probation, max 1 concurrent.")]
    lines += ["", "**Method.** As S34 (agenda 21): daily bars resampled from the "
              "S33 5-minute IEX cache; live ladder, time stops and S28 target cap; "
              "SPY above its 20-day EMA. The detector needs 62 daily bars, so the "
              "first possible entry is ~62 sessions into the cache (2025-08-15), "
              "not 2025-10-14. Reproduce with `python study_exit_grid.py --s35`.",
              "", sc.UPPER_BOUND]
    print("\n".join(lines))
    if append:
        n = sc.append_agenda("Study: S35 pullback_in_uptrend - bar window fixed, "
                             "first replay (2026-10-20)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return st, ok


# =========================================================== S36 (2026-10-21)
# Catalyst names vs the rest. The morning catalyst scanner adds a name when
# |pre-market gap| >= 3% AND company news in the last 18 h AND price 5-1200
# AND ADV >= $20M, at most 15 names a day; catalyst names only get the normal
# setups. Replayed here: the S34 trades at the current target (3R, no
# partial), split by whether the entry day was a catalyst day for the symbol.
# Gap = day's first RTH 5-minute open vs the previous session's RTH close;
# ADV = mean close x volume of the 20 prior resampled daily bars (the same
# IEX feed the live universe ADV comes from); news = Finnhub company news
# with `datetime` in [09:30 ET - 18 h, 09:30 ET).

S36_GAP = 0.03
S36_NEWS_HOURS = 18
S36_PRICE = (5.0, 1200.0)
S36_ADV = 20e6
S36_ADV_DAYS = 20
S36_MAX_NAMES = 15
S36_NEWS_DIR = os.path.join("data", "s36_news")
S36_CALLS_PER_MIN = 55
S36_CHUNK_DAYS = 31


def s36_gaps(daily):
    """Per day: open / previous session's close - 1 (NaN on the first day)."""
    return daily["open"] / daily["close"].shift(1) - 1.0


def s36_adv(daily, n=S36_ADV_DAYS):
    """Per day: mean close x volume over the n PRIOR sessions (NaN until n)."""
    return (daily["close"] * daily["volume"]).rolling(n).mean().shift(1)


def s36_open_utc(day):
    """09:30 ET on `day` as a UTC timestamp (DST-correct)."""
    d = pd.Timestamp(day)
    d = d.tz_localize(None) if d.tzinfo is not None else d
    return (pd.Timestamp(d.date()) + pd.Timedelta(hours=9, minutes=30)) \
        .tz_localize(ET).tz_convert("UTC")


def s36_news_window(day, hours=S36_NEWS_HOURS):
    """(start, end) unix seconds: [09:30 ET - hours, 09:30 ET)."""
    end = s36_open_utc(day)
    return int((end - pd.Timedelta(hours=hours)).timestamp()), int(end.timestamp())


def s36_has_news(articles, day, hours=S36_NEWS_HOURS):
    lo, hi = s36_news_window(day, hours)
    for a in articles or ():
        try:
            t = int(a.get("datetime") or 0)
        except (TypeError, ValueError):
            continue
        if lo <= t < hi:
            return True
    return False


def s36_candidates(frames, start=S34_START, gap=S36_GAP, price=S36_PRICE,
                   adv_min=S36_ADV):
    """{date: [(sym, gap)]} for every symbol-day on/after `start` passing the
    gap, price-on-open and ADV conditions (news not yet applied)."""
    lo = pd.Timestamp(start).date()
    out = {}
    for sym, (_m5, daily) in frames.items():
        g, a = s36_gaps(daily), s36_adv(daily)
        for i, ts in enumerate(daily.index):
            d = pd.Timestamp(ts).date()
            gv, av, px = g.iloc[i], a.iloc[i], float(daily["open"].iloc[i])
            if d < lo or pd.isna(gv) or pd.isna(av):
                continue
            if abs(gv) >= gap and price[0] <= px <= price[1] and av >= adv_min:
                out.setdefault(d, []).append((sym, float(gv)))
    return out


def s36_cap(cands, keep=lambda sym, d: True, n=S36_MAX_NAMES):
    """{(sym, date)}: per day, the n largest |gap| among names `keep` passes."""
    out = set()
    for d, names in cands.items():
        kept = [x for x in names if keep(x[0], d)]
        kept.sort(key=lambda x: (-abs(x[1]), x[0]))
        out.update((sym, d) for sym, _ in kept[:n])
    return out


def s36_split(trades, catalyst):
    """(catalyst trades, rest) by (sym, entry day) membership."""
    cat = [t for t in trades if (t["sym"], t["day"]) in catalyst]
    rest = [t for t in trades if (t["sym"], t["day"]) not in catalyst]
    return cat, rest


def s36_windows(dates, pad=1, chunk=S36_CHUNK_DAYS):
    """Merged [(from, to)] date ranges covering each date +/- pad days, none
    longer than `chunk` days."""
    from datetime import timedelta
    spans = []
    for d in sorted(set(dates)):
        a, b = d - timedelta(days=pad), d + timedelta(days=pad)
        if spans and a <= spans[-1][1] + timedelta(days=1) \
                and (b - spans[-1][0]).days < chunk:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return [(a, b) for a, b in spans]


S36_API_CAP = 240        # Finnhub returns at most ~250 articles a call


def s36_needs_narrow(windows, d, cap=S36_API_CAP):
    """True when no cached window shows news for day d but a capped window
    covering d starts after d's news window opens (so it may be truncated)."""
    lo, _hi = s36_news_window(d)
    covering = []
    for key, arts in windows.items():
        a, b = (date.fromisoformat(x) for x in key.split("|"))
        if a <= d <= b:
            covering.append(arts)
            if s36_has_news(arts, d):
                return False
    for arts in covering:
        ts = [int(x.get("datetime") or 0) for x in arts if x.get("datetime")]
        if len(arts) >= cap and ts and min(ts) > lo:
            return True
    return False


class S36Limiter:
    """At most `per_min` calls a minute (even spacing)."""

    def __init__(self, per_min=S36_CALLS_PER_MIN, sleep=None, clock=None):
        import time
        self.gap = 60.0 / per_min
        self.sleep, self.clock = sleep or time.sleep, clock or time.monotonic
        self.last = None

    def wait(self):
        if self.last is not None:
            dt = self.gap - (self.clock() - self.last)
            if dt > 0:
                self.sleep(dt)
        self.last = self.clock()


def s36_news(sym, dates, client, limiter, cache_dir=S36_NEWS_DIR):
    """(articles, failed): Finnhub company news covering `dates` +/- 1 day,
    cached raw per window in <cache_dir>/<SYM>.json. Any failed window marks
    the symbol failed (the caller treats it as no-news)."""
    import json
    from datetime import timedelta
    path = os.path.join(cache_dir, f"{sym}.json")
    cache = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    windows = cache.setdefault("windows", {})
    failed, dirty = False, False

    def fetch(a, b):
        nonlocal failed, dirty
        key = f"{a.isoformat()}|{b.isoformat()}"
        if key in windows:
            return
        got = None
        for attempt in range(2):
            limiter.wait()
            try:
                got = client.company_news(sym, _from=a.isoformat(), to=b.isoformat())
                break
            except Exception as e:                       # noqa: BLE001
                msg = str(e).lower()
                if attempt == 0 and ("429" in msg or "limit" in msg):
                    limiter.sleep(61)
                    continue
                break
        if not isinstance(got, list):
            failed = True
            return
        windows[key] = got
        dirty = True

    for a, b in s36_windows(dates):
        fetch(a, b)
    # A window at the API's per-call cap (newest first) may have dropped the
    # pre-open hours of a gap day: refetch just that day's two UTC dates.
    for d in sorted(set(dates)):
        if s36_needs_narrow(windows, d):
            fetch(d - timedelta(days=1), d)
    if dirty:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    seen, arts = set(), []
    for lst in windows.values():
        for x in lst:
            k = (x.get("id"), x.get("datetime"), x.get("headline"))
            if k not in seen:
                seen.add(k)
                arts.append(x)
    return arts, failed


def s36_classify(frames, client, limiter, start=S34_START,
                 cache_dir=S36_NEWS_DIR, log=print):
    """Catalyst sets and coverage stats: {"news": {(sym, date)}, "gap":
    {(sym, date)}, ...}. News is fetched only for symbols with a candidate
    gap day, only around those days."""
    cands = s36_candidates(frames, start)
    by_sym = {}
    for d, names in cands.items():
        for sym, _ in names:
            by_sym.setdefault(sym, []).append(d)
    news, failed, n_articles = {}, [], 0
    for i, sym in enumerate(sorted(by_sym)):
        arts, bad = s36_news(sym, by_sym[sym], client, limiter, cache_dir)
        if bad:
            failed.append(sym)
            arts = []
        news[sym] = arts
        n_articles += len(arts)
        if log and (i + 1) % 20 == 0:
            log(f"  news: {i + 1}/{len(by_sym)} symbols", file=sys.stderr)
    has = {(s, d) for s, ds in by_sym.items() for d in ds
           if s36_has_news(news.get(s), d)}
    dates = [pd.Timestamp(int(a["datetime"]), unit="s").date()
             for arts in news.values() for a in arts if a.get("datetime")]
    return {
        "news": s36_cap(cands, lambda s, d: (s, d) in has),
        "gap": s36_cap(cands),
        "gap_days": sum(len(v) for v in by_sym.values()),
        "gap_with_news": len(has),
        "symbols": len(by_sym),
        "failed": failed,
        "articles": n_articles,
        "first_article": min(dates) if dates else None,
    }


def s36_row(split, setup, group, trades):
    st, cap, tgt = s34_metrics(trades)
    return [split, setup, group, st["trades"],
            f"{st['win_rate']}%" if st["trades"] else "-",
            f"{st['expectancy_r']:+.3f}" if st["trades"] else "-",
            st["profit_factor"] if st["trades"] else "-",
            st["max_drawdown_r"] if st["trades"] else "-",
            f"{cap * 100:.0f}%" if cap is not None else "-",
            f"{tgt:.1f}%" if tgt is not None else "-"]


def s36_render(trades, info, n_signals, n_symbols):
    traded = {(t["sym"], t["day"]) for t in trades}
    first = min((t["day"] for t in trades), default=None)
    last = max((t["day"] for t in trades), default=None)
    lines = [f"**S36 (CEO 2026-10-21) - catalyst names vs the rest, study "
             f"only.** 5-minute replay of the live daily setups through the "
             f"live daily exit rule at the current target (3R, S28-capped, no "
             f"partial), {n_signals} signals on {n_symbols} universe symbols, "
             f"entries {first or '-'}..{last or '-'}. Catalyst = entry day had "
             f"|gap| >= {S36_GAP:.0%}, Finnhub company news in the "
             f"{S36_NEWS_HOURS} h before 09:30 ET, open price "
             f"{S36_PRICE[0]:g}-{S36_PRICE[1]:g}, 20-day ADV >= "
             f"${S36_ADV / 1e6:.0f}M, top {S36_MAX_NAMES} |gap| a day.", ""]
    rows = []
    for split, key in (("gap + news (scanner rule)", "news"),
                       ("gap only (no news test)", "gap")):
        for setup in (None,) + S34_SETUPS:
            sel = [t for t in trades if setup is None or t["setup"] == setup]
            cat, rest = s36_split(sel, info[key])
            rows.append(s36_row(split, setup or "all", "catalyst", cat))
            rows.append(s36_row(split, setup or "all", "rest", rest))
    lines += sc.table(["Split", "Setup", "Group", "Trades", "Win %",
                       "Expectancy (R)", "PF", "MaxDD (R)", "Capture",
                       "Exits at target"], rows)
    n_news, n_gap = len(info["news"]), len(info["gap"])
    t_news = len(info["news"] & traded)
    t_gap = len(info["gap"] & traded)
    lines += ["", f"**Catalyst days.** Scanner rule: {n_news} symbol-days "
              f"(after the {S36_MAX_NAMES}/day cap), {t_news} of them produced "
              f"a trade. Gap only: {n_gap} symbol-days, {t_gap} produced a "
              f"trade. Gap/price/ADV symbol-days before the news test: "
              f"{info['gap_days']} on {info['symbols']} symbols, "
              f"{info['gap_with_news']} with news in the window.",
              "", f"**Finnhub coverage.** {info['symbols']} symbols queried "
              f"(gap days +/- 1 day), {info['articles']} articles, earliest "
              f"{info['first_article'] or '-'}; "
              f"{len(info['failed'])} symbol(s) failed and count as no-news"
              + (f" ({', '.join(info['failed'])})" if info["failed"] else "")
              + ". Free tier: about one year of history, so the first entry "
              "days of the window can lack news.",
              "", "**Method.** As S34 (agenda 21): daily bars resampled from "
              "the S33 5-minute IEX cache (RTH), live SPY gate, live ladder, "
              "time stops and S28 target cap, one position per symbol. Gap = "
              "entry day's first RTH 5-minute open vs the previous cached "
              "session's RTH close; ADV = mean close x volume of the 20 prior "
              "sessions (IEX volume, the same feed the live universe ADV uses). "
              "Reproduce with `python study_exit_grid.py --s36`.",
              "", sc.UPPER_BOUND]
    return lines


def s36_main(append=False, universe_file="universe_today.json", client=None,
             limiter=None):
    import json
    with open(universe_file, encoding="utf-8") as f:
        symbols = [c["symbol"] for c in json.load(f).get("candidates", [])
                   if c.get("symbol") != "SPY"]
    signals, frames = s34_signals(symbols)
    cur = [(s34_label(*S34_CURRENT), *S34_CURRENT)]
    trades = s34_run(signals, frames, variants=cur)[cur[0][0]]
    if client is None:
        import finnhub
        from dotenv import load_dotenv
        load_dotenv("/Users/ranasaradb/TradingBot/.env")
        client = finnhub.Client(api_key=os.environ.get("FINNHUB_API_KEY", ""))
    info = s36_classify(frames, client, limiter or S36Limiter())
    lines = s36_render(trades, info, len(signals), len(frames))
    print("\n".join(lines))
    if append:
        n = sc.append_agenda("Study: S36 catalyst replay - catalyst names vs "
                             "the rest (2026-10-21)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return trades, info


# =========================================================== S37 (2026-10-21)
# CEO 2026-10-21, S37: pyramiding on winners. Study only - no live change.
# The live daily rule (S34 current: 3R target S28-capped, no partial; ladder
# +1R -> entry, +1.5R -> entry, +2R -> entry + 1R, then highest daily close
# - 2.5 ATR; time stops) on the live daily setups (pullback retired;
# post-earnings has no historical calendar).
#
# The add: once a completed 5-minute close (from 10:00 ET) is at or above
# entry + kR AND the ladder has the stop at breakeven or better, buy at the
# next bar's open. Size: combined open risk on the symbol <= 1% of equity -
# the first unit's open risk is 0 once its stop is at breakeven, so the add
# may risk the whole 1% budget (one original R) against its own stop at the
# ORIGINAL ENTRY (raised with the ladder thereafter, like the first unit).
# R is in units of the original trade's risk; $ at $5,000 equity, 1% = $50.

S37_SETUPS = ("momentum_continuation", "mean_reversion_reclaim")
S37_ADD_AT = (None, 1.0, 1.5)
S37_EQUITY, S37_RISK_PCT, S37_POSITION_CAP = 5000.0, 1.0, 0.25


def s37_label(k):
    return "no add (current)" if k is None else f"add at +{k:g}R"


def simulate_pyramid(m5, daily_close, atr_day, entry_day, entry, stop, target,
                     steps, add_at=None):
    """{r, r_add, size_add, add_px, why, mfe} or None while open at the end of
    data. size_add is the add's share count as a multiple of the first
    unit's; r_add is its contribution in original R."""
    risk = entry - stop
    bars = m5[m5.index.normalize() >= pd.Timestamp(entry_day).tz_convert(ET).normalize()]
    if bars.empty:
        return None
    days = sorted({t.date() for t in bars.index})
    day_no = {d: i + 1 for i, d in enumerate(days)}
    live, hc, mfe, first = stop, entry, 0.0, True
    entry_date = days[0]
    pending, add_px, size_add = False, None, 0.0

    def done(px, why):
        r1 = (px - entry) / risk
        r2 = size_add * (px - add_px) / risk if add_px is not None else None
        return {"r": r1 + (r2 or 0.0), "r_add": r2, "size_add": size_add,
                "add_px": add_px, "why": why, "mfe": mfe}

    for ts, b in bars.iterrows():
        o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]),
                      float(b["close"]))
        d = ts.date()
        if not first and o <= live:
            return done(o, "stop (gap)")
        if not first and o >= target:
            mfe = max(mfe, (o - entry) / risk)
            return done(o, "target (gap)")
        if pending:                                   # the add fills at the open
            pending = False
            own_stop = max(entry, live)
            if o > own_stop:
                add_px = o
                # combined open risk <= one original R: unit 1 risks
                # max(0, entry - live) = 0 here, so the add gets the rest
                budget = risk - max(0.0, entry - live)
                size_add = budget / (o - own_stop)
        n = day_no[d]
        if (ts.hour, ts.minute) == (15, 55) and (n >= 10 or (n >= 5 and mfe < 0.5)):
            return done(o, f"time stop day {n}")
        first = False
        mfe = max(mfe, (h - entry) / risk)
        if l <= live:
            return done(live, "stop")
        if h >= target:
            return done(target, "target")
        if (ts.hour, ts.minute) >= (10, 0):
            hc = max(hc, c)
            cands = [entry + sr * risk for t, sr in steps if (hc - entry) / risk >= t]
            if (hc - entry) / risk >= 2.0:
                prior = [v for dd, v in daily_close.items() if entry_date <= dd < d]
                a = atr_day.get(d)
                if prior and a:
                    cands.append(max(prior) - 2.5 * a)
            if cands:
                cand = math.ceil(max(cands) * 100 - 1e-6) / 100
                if live < cand < c:
                    live = cand
            if (add_at and add_px is None and not pending and live >= entry
                    and (c - entry) / risk >= add_at):
                pending = True
    return None


def s37_run(signals, frames):
    params = dict(pm.r_atr_params(_live_profile()))
    steps = [tuple(x) for x in params["steps"]]
    cap = pm.target_cap_atr(_live_config())
    budget = S37_EQUITY * S37_RISK_PCT / 100
    out = {}
    for k in S37_ADD_AT:
        trades, busy = [], {}
        for setup, sym, day, entry, structural in sorted(signals, key=lambda x: x[2]):
            if sym in busy and day.date() <= busy[sym]:
                continue
            m5, daily = frames[sym]
            atrs = atr_by_day(daily)
            atr = atrs.get(day.date())
            if not atr:
                continue
            stop = pm.initial_stop(entry, structural, atr, params)
            if stop >= entry:
                continue
            target = pm.capped_target(entry, stop, atr, cap, 3.0)
            closes = {pd.Timestamp(i).date(): float(v) for i, v in daily["close"].items()}
            res = simulate_pyramid(m5, closes, atrs, day, entry, stop, target, steps, k)
            if res is None:
                busy[sym] = m5.index[-1].date()
                continue
            busy[sym] = day.date()
            shares = budget / (entry - stop)              # first unit, fractional
            notional = shares * entry
            if res["add_px"] is not None:
                notional += shares * res["size_add"] * res["add_px"]
            trades.append({"setup": setup, "r": res["r"], "r_add": res["r_add"],
                           "mfe_r": res["mfe"], "why": res["why"],
                           # $ risked over the trade: the first unit's 1%,
                           # plus the add's (its own 1% budget, never both open)
                           "risk_usd": budget * (2 if res["add_px"] is not None else 1),
                           "peak_notional": notional})
        out[s37_label(k)] = trades
    return out


def s37_rows(results):
    rows, cap_usd = [], S37_EQUITY * S37_POSITION_CAP
    for setup in (None,) + S37_SETUPS:
        for label, trades in results.items():
            ts = [t for t in trades if setup is None or t["setup"] == setup]
            st = sc.aggregate(ts)
            adds = [t["r_add"] for t in ts if t["r_add"] is not None]
            n = len(ts)
            rows.append({
                "setup": setup or "all", "variant": label, "trades": st["trades"],
                "adds": len(adds), "win": st["win_rate"], "exp": st["expectancy_r"],
                "total": round(sum(t["r"] for t in ts), 1), "pf": st["profit_factor"],
                "maxdd": st["max_drawdown_r"],
                "risk_usd": round(sum(t["risk_usd"] for t in ts) / n, 2) if n else None,
                "add_total": round(sum(adds), 1) if adds else 0.0,
                "add_avg": round(sum(adds) / len(adds), 3) if adds else None,
                "over_cap": (round(100 * sum(1 for t in ts if t["peak_notional"] > cap_usd)
                                   / n, 1) if n else None)})
    return rows


def s37_render(rows, n_signals, n_symbols, first_day):
    lines = [f"**S37 (CEO 2026-10-21) - pyramiding on winners, study only.** "
             f"5-minute replay of the live daily rule, {n_signals} signals on "
             f"{n_symbols} universe symbols, entries {first_day}..2026-10-20. "
             f"Expectancy is R per ORIGINAL trade; $ at ${S37_EQUITY:,.0f} "
             f"equity, {S37_RISK_PCT:g}% = ${S37_EQUITY * S37_RISK_PCT / 100:,.0f} "
             f"per R.", ""]
    lines += sc.table(
        ["Setup", "Variant", "Trades", "Adds", "Win %", "Expectancy (R)", "Total R",
         "PF", "MaxDD (R)", "Avg $ at risk / trade", "Adds' own total R",
         "Over 25% position cap"],
        [[r["setup"], r["variant"], r["trades"], r["adds"],
          f"{r['win']}%" if r["win"] is not None else "-",
          f"{r['exp']:+.3f}" if r["exp"] is not None else "-",
          f"{r['total']:+.1f}", r["pf"] if r["pf"] is not None else "-",
          r["maxdd"] if r["maxdd"] is not None else "-",
          f"${r['risk_usd']:,.2f}" if r["risk_usd"] is not None else "-",
          f"{r['add_total']:+.1f}",
          f"{r['over_cap']}%" if r["over_cap"] is not None else "-"] for r in rows])
    lines += ["", "**Method.** As S34 (agenda 21): daily bars resampled from "
              "the 5-minute IEX bars; live ladder, time stops, S28 target cap; "
              "SPY gate for momentum (the reclaim is exempt). The add is bought "
              "at the open of the bar after a completed close at or above "
              "entry + kR with the stop at breakeven or better; it is sized so "
              "the symbol's combined open risk stays within 1% of equity, with "
              "its own stop at the original entry, raised with the ladder. "
              "'Avg $ at risk / trade' counts the first unit's $50 plus the "
              "add's $50 when one was made (the two are never at risk at the "
              "same time: the first unit is at breakeven when the add is "
              "bought). 'Over 25% position cap' is the share of trades whose "
              "combined position would exceed $1,250 - a live add would be "
              "trimmed or refused there. Reproduce with `python "
              "study_exit_grid.py --s37`.",
              "", "**Limits.** Fractional shares (live sizing is whole shares); "
              "today's universe (survivorship); IEX bars; earnings exits not "
              "modelled; signals, not gatekeeper-approved trades.", "",
              sc.UPPER_BOUND]
    return lines


def s37_main(append=False, universe_file="universe_today.json"):
    import json
    with open(universe_file, encoding="utf-8") as f:
        symbols = [c["symbol"] for c in json.load(f).get("candidates", [])
                   if c.get("symbol") != "SPY"]
    signals, frames = s34_signals(symbols, setups=S37_SETUPS)
    results = s37_run(signals, frames)
    first = min((x[2] for x in signals), default=None)
    lines = s37_render(s37_rows(results), len(signals), len(frames),
                       first.date() if first is not None else "-")
    print("\n".join(lines))
    if append:
        n = sc.append_agenda("Study: S37 pyramiding on winners (2026-10-21)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


# =========================================================== S44 (2026-10-22)
# CEO 2026-10-22 item 2: the "S36 morning-momentum setup" replay. No such
# setup exists in the live code; it is DEFINED here (study only) as a long
# gap-and-go / opening-range breakout on the catalyst scanner's names:
#   candidates: gap UP >= 3% (first RTH 5-min open vs previous RTH close),
#     Finnhub company news in the 18 h before 09:30 ET, open price 5-1200,
#     20-session ADV (close x volume, IEX, prior sessions) >= $20M, top 15
#     by gap per day;
#   opening range: the 09:30-09:45 ET 5-minute bars (09:30, 09:35, 09:40);
#   entry: the first completed 5-minute bar starting 09:45..10:55 ET (done
#     by 11:00) that CLOSES above the OR high; fill at the NEXT bar's open;
#     skip if that open is already above the target planned at the signal
#     (trigger close + k x (trigger close - OR low));
#   stop: the OR low (skip if fill - stop <= 0); target: fill + k x R;
#   exit at the 15:55 ET bar's open if neither level is hit; a bar opening
#     through a level fills at its open, a bar spanning both takes the stop;
#   one trade per symbol per day; no SPY filter (split reported).

S44_START = S34_START
S44_TARGETS = (2.0, 1.5, 3.0)
S44_BASE_K = 2.0
S44_OR_END = 585          # 09:45 ET, minutes after midnight
S44_ENTRY_LAST = 655      # last trigger bar starts 10:55 (completes 11:00)
S44_EXIT = 955            # 15:55 ET
S44_BAR_MIN = 50          # trades needed for the bar
S44_BAR_R = 0.15


def _hm(ts):
    return ts.hour * 60 + ts.minute


def s44_candidates(frames, start=S44_START, end=None, gap=S36_GAP,
                   price=S36_PRICE, adv_min=S36_ADV):
    """{date: [(sym, gap)]}: gap UP >= `gap`, open price in range, prior-20
    ADV >= adv_min (news not yet applied). Long side only."""
    lo = pd.Timestamp(start).date()
    hi = pd.Timestamp(end).date() if end else None
    out = {}
    for sym, (_m5, daily) in frames.items():
        g, a = s36_gaps(daily), s36_adv(daily)
        for i, ts in enumerate(daily.index):
            d = pd.Timestamp(ts).date()
            gv, av, px = g.iloc[i], a.iloc[i], float(daily["open"].iloc[i])
            if d < lo or (hi and d > hi) or pd.isna(gv) or pd.isna(av):
                continue
            if gv >= gap and price[0] <= px <= price[1] and av >= adv_min:
                out.setdefault(d, []).append((sym, float(gv)))
    return out


def s44_day_bars(m5, day):
    """The RTH 5-minute bars of one session (ET index)."""
    d = pd.Timestamp(day).date()
    return m5[m5.index.date == d]


def s44_opening_range(bars):
    """(OR high, OR low) over the bars starting 09:30..09:40 ET, or None."""
    orb = bars[[570 <= _hm(t) < S44_OR_END for t in bars.index]]
    if orb.empty:
        return None
    return float(orb["high"].max()), float(orb["low"].min())


def s44_trigger(bars, or_high):
    """Index position of the first bar starting 09:45..10:55 ET whose close
    is above the OR high, or None."""
    for i, (ts, b) in enumerate(bars.iterrows()):
        m = _hm(ts)
        if S44_OR_END <= m <= S44_ENTRY_LAST and float(b["close"]) > or_high:
            return i
        if m > S44_ENTRY_LAST:
            break
    return None


def s44_simulate(bars, i, entry, stop, target):
    """(exit price, reason) walking from bar i (filled at its open)."""
    last = None
    for ts, b in bars.iloc[i:].iterrows():
        o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        if _hm(ts) >= S44_EXIT:
            return o, "time 15:55"
        if o <= stop:
            return o, "stop (gap)"
        if o >= target:
            return o, "target (gap)"
        if l <= stop:
            return stop, "stop"
        if h >= target:
            return target, "target"
        last = c
    return last, "time (last bar)"


def s44_trade(m5, day, k=S44_BASE_K):
    """One symbol-day through the setup. Returns a dict with `status`:
    no_bars / no_or / no_trigger / no_fill_bar / above_target / no_risk /
    traded (then r, why, entry, stop, target, risk_pct, t_entry)."""
    bars = s44_day_bars(m5, day)
    if bars.empty:
        return {"status": "no_bars"}
    orr = s44_opening_range(bars)
    if orr is None:
        return {"status": "no_or"}
    or_high, or_low = orr
    i = s44_trigger(bars, or_high)
    if i is None:
        return {"status": "no_trigger", "or_high": or_high, "or_low": or_low}
    if i + 1 >= len(bars):
        return {"status": "no_fill_bar"}
    sig = float(bars["close"].iloc[i])
    planned = sig + k * (sig - or_low)
    entry = float(bars["open"].iloc[i + 1])
    if entry > planned:
        return {"status": "above_target", "entry": entry, "planned": planned}
    risk = entry - or_low
    if risk <= 0:
        return {"status": "no_risk", "entry": entry, "stop": or_low}
    target = entry + k * risk
    px, why = s44_simulate(bars, i + 1, entry, or_low, target)
    return {"status": "traded", "r": (px - entry) / risk, "why": why,
            "entry": entry, "stop": or_low, "target": target,
            "risk_pct": 100 * risk / entry, "t_entry": bars.index[i + 1]}


def s44_spy_trend(spy_daily):
    """{date: SPY close > 20d EMA as of the PREVIOUS session} (known pre-open)."""
    import backtest
    reg = backtest.spy_regime_series(spy_daily).shift(1)
    return {pd.Timestamp(i).date(): bool(v) for i, v in reg.items() if not pd.isna(v)}


def s44_replay(frames, picks, k=S44_BASE_K, spy=None, gaps=None):
    """Trades for every (sym, date) in `picks`, sorted by entry time."""
    trades = []
    for sym, d in sorted(picks, key=lambda x: (x[1], x[0])):
        out = s44_trade(frames[sym][0], d, k)
        if out["status"] != "traded":
            continue
        out.update(sym=sym, day=d, k=k,
                   spy=(spy or {}).get(d), gap=(gaps or {}).get((sym, d)))
        trades.append(out)
    trades.sort(key=lambda t: (t["t_entry"], t["sym"]))
    return trades


def s44_stats(trades, weeks=None):
    """The report row: trades, /week, win %, exp R, PF, MaxDD, median risk %."""
    st = sc.aggregate(trades)
    if not trades:
        return [0, "-", "-", "-", "-", "-", "-"]
    risk = sorted(t["risk_pct"] for t in trades)
    n = len(risk)
    med = risk[n // 2] if n % 2 else (risk[n // 2 - 1] + risk[n // 2]) / 2
    pf = st["profit_factor"]
    return [st["trades"], f"{st['trades'] / weeks:.2f}" if weeks else "-",
            f"{st['win_rate']}%", f"{st['expectancy_r']:+.3f}",
            "inf" if pf == float("inf") else pf, st["max_drawdown_r"],
            f"{med:.2f}%"]


def s44_verdict(trades):
    st = sc.aggregate(trades)
    ok = st["trades"] >= S44_BAR_MIN and (st["expectancy_r"] or 0) >= S44_BAR_R
    return ("proposed for probation (max 1 position)" if ok else "not proposed"), ok


def s44_explain(sym, d, frames, cands, picks, has_news, news_known, k=S44_BASE_K):
    """One missed-mover line: (candidate?, why not, entered?, R)."""
    if sym not in frames:
        return "no", "no 5-min bars cached", "no", "-"
    m5, daily = frames[sym]
    days = [pd.Timestamp(i).date() for i in daily.index]
    if d not in days:
        return "no", "no bars that day", "no", "-"
    i = days.index(d)
    gv = s36_gaps(daily).iloc[i]
    av = s36_adv(daily).iloc[i]
    px = float(daily["open"].iloc[i])
    fails = []
    if pd.isna(gv) or gv < S36_GAP:
        fails.append(f"gap {gv * 100:+.1f}% < +3%")
    if not (S36_PRICE[0] <= px <= S36_PRICE[1]):
        fails.append(f"price {px:.2f}")
    if pd.isna(av) or av < S36_ADV:
        fails.append(f"ADV ${(av or 0) / 1e6:.0f}M < $20M")
    if not fails:
        if (sym, d) not in news_known:
            fails.append(f"gap {gv * 100:+.1f}%; news not checked")
        elif not has_news:
            fails.append(f"gap {gv * 100:+.1f}%; no Finnhub news in 18 h")
        elif (sym, d) not in picks:
            fails.append(f"gap {gv * 100:+.1f}%; outside top {S36_MAX_NAMES}")
    out = s44_trade(m5, d, k)
    gap_txt = f"gap {gv * 100:+.1f}%"
    if fails:
        hyp = (f"would-be {out['r']:+.2f}R" if out["status"] == "traded"
               else out["status"])
        return "no", "; ".join(fails), "no", f"({hyp} if it were)"
    if out["status"] != "traded":
        return "yes", gap_txt + ", news", f"no ({out['status']})", "-"
    return "yes", gap_txt + ", news", \
        f"yes {out['t_entry'].strftime('%H:%M')} @ {out['entry']:.2f}", \
        f"{out['r']:+.2f}R ({out['why']})"


def s44_main(append=False, universe_file="universe_today.json", client=None,
             limiter=None, end="2026-10-21", movers=None, cache=S34_CACHE):
    import json
    with open(universe_file, encoding="utf-8") as f:
        symbols = [c["symbol"] for c in json.load(f).get("candidates", [])
                   if c.get("symbol") != "SPY"]
    frames = {}
    for sym in symbols:
        m5 = load_m5_rth(sym, cache)
        if m5 is None or m5.empty:
            continue
        daily = daily_from_m5(m5)
        daily.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in daily.index])
        frames[sym] = (m5, daily)
    spy5 = load_m5_rth("SPY", cache)
    spy = s44_spy_trend(daily_from_m5(spy5)) if spy5 is not None else {}
    if client is None:
        import finnhub
        from dotenv import load_dotenv
        load_dotenv("/Users/ranasaradb/TradingBot/.env")
        client = finnhub.Client(api_key=os.environ.get("FINNHUB_API_KEY", ""))
    limiter = limiter or S36Limiter()
    cands = s44_candidates(frames, S44_START, end)
    by_sym = {}
    for d, names in cands.items():
        for sym, _ in names:
            by_sym.setdefault(sym, []).append(d)
    news, failed, n_art = {}, [], 0
    for i, sym in enumerate(sorted(by_sym)):
        arts, bad = s36_news(sym, by_sym[sym], client, limiter)
        if bad:
            failed.append(sym)
            arts = []
        news[sym] = arts
        n_art += len(arts)
        if (i + 1) % 20 == 0:
            print(f"  news: {i + 1}/{len(by_sym)} symbols", file=sys.stderr)
    has = {(s, d) for s, ds in by_sym.items() for d in ds if s36_has_news(news.get(s), d)}
    picks = s36_cap(cands, lambda s, d: (s, d) in has)
    gaps = {(s, d): g for d, ns in cands.items() for s, g in ns}
    art_days = [pd.Timestamp(int(a["datetime"]), unit="s").date()
                for arts in news.values() for a in arts if a.get("datetime")]
    first_news = min(art_days) if art_days else None
    runs = {k: s44_replay(frames, picks, k, spy, gaps) for k in S44_TARGETS}
    base = runs[S44_BASE_K]
    first = min((t["day"] for t in base), default=None)
    span_lo = max(pd.Timestamp(S44_START).date(), first_news) if first_news else \
        pd.Timestamp(S44_START).date()
    weeks = max((pd.Timestamp(end) - pd.Timestamp(span_lo)).days / 7, 1e-9)
    hdr = ["Rows", "Trades", "Trades/week", "Win %", "Expectancy (R)", "PF",
           "MaxDD (R)", "Median risk %"]
    rows = [[f"**{S44_BASE_K:g}R target (the setup)**"] + s44_stats(base, weeks)]
    for k in S44_TARGETS[1:]:
        rows.append([f"sensitivity: {k:g}R target"] + s44_stats(runs[k], weeks))
    for lab, flag in (("SPY trending (prior close > 20d EMA)", True),
                      ("SPY not trending", False)):
        rows.append([f"split @2R: {lab}"] +
                    s44_stats([t for t in base if t["spy"] is flag], None))
    months = sorted({t["day"].strftime("%Y-%m") for t in base})
    mrows = []
    for m in months:
        sel = [t for t in base if t["day"].strftime("%Y-%m") == m]
        mrows.append([m] + s44_stats(sel, None))
    verdict, _ok = s44_verdict(base)
    st = sc.aggregate(base)
    whys = {}
    for t in base:
        w = t["why"].split(" ")[0]
        whys[w] = whys.get(w, 0) + 1
    statuses = {}
    for s, d in picks:
        o = s44_trade(frames[s][0], d, S44_BASE_K)["status"]
        statuses[o] = statuses.get(o, 0) + 1
    lines = [
        "**S44 (CEO 2026-10-22, item 2) - morning-momentum setup replay, study "
        "only, no live change.** No such setup exists in the code; it is "
        "defined here as:",
        "",
        "> - Candidates per day: catalyst names per the scanner rule, LONG side "
        "only: gap UP >= 3% (first RTH 5-min open vs previous RTH close), "
        "Finnhub company news with datetime in the 18 h before 09:30 ET, price "
        "on the open 5-1200, 20-session ADV (close x volume, IEX, prior "
        "sessions) >= $20M, top 15 by gap per day.",
        "> - Opening range = the 09:30-09:45 ET bars (3 x 5-min). Entry: the "
        "first completed 5-min bar between 09:45 and 11:00 ET that CLOSES above "
        "the opening-range high; fill at the NEXT bar's open. Skip if that open "
        "is already above the target.",
        "> - Stop: the opening-range low. Skip only if risk <= 0; report the "
        "median risk %.",
        "> - Target: entry + 2R. Exit at 15:55 ET same day if neither hit "
        "(intraday only). Fills: a bar opening through a level fills at its "
        "open; a bar spanning stop and target takes the stop.",
        "> - One trade per symbol per day; no SPY filter (report the "
        "SPY-trending vs not split as a sensitivity).",
        "",
        "Implementation notes: the \"already above the target\" check uses the "
        "target planned at the signal (trigger close + kR from the trigger "
        "close to the OR low), since a target measured from the fill can never "
        "be below the fill; the traded target is fill + kR. Trigger bars start "
        "09:45..10:55 (complete by 11:00). The 15:55 exit is the 15:55 bar's "
        "open. SPY trending = SPY close above its 20-day EMA as of the "
        "previous session.",
        "",
        f"Window {span_lo}..{end} ({weeks:.1f} weeks; Finnhub news starts "
        f"{first_news or '-'}), {len(frames)} universe symbols. Candidates: "
        f"{sum(len(v) for v in cands.values())} gap-up/price/ADV symbol-days on "
        f"{len(by_sym)} symbols, {len(has)} with news, {len(picks)} after the "
        f"15/day cap; outcomes {', '.join(f'{k} {v}' for k, v in sorted(statuses.items()))}. "
        f"Exits at 2R: {', '.join(f'{k} {v}' for k, v in sorted(whys.items()))}.",
        "",
    ]
    lines += sc.table(hdr, rows)
    lines += ["", "**By month (2R target).**", ""]
    lines += sc.table(["Month", "Trades", "Trades/week", "Win %", "Expectancy (R)",
                       "PF", "MaxDD (R)", "Median risk %"], mrows)
    lines += ["", f"**Bar** (expectancy >= +{S44_BAR_R:.2f}R over >= "
              f"{S44_BAR_MIN} trades): {st['trades']} trades at "
              f"{(st['expectancy_r'] or 0):+.3f}R -> **{verdict}**.", ""]
    if movers:
        mr = []
        for d, names in movers:
            d = pd.Timestamp(d).date()
            for sym, note in names:
                frm = frames
                if sym not in frames:
                    m5 = load_m5_rth(sym, cache)
                    if m5 is not None and not m5.empty:
                        dl = daily_from_m5(m5)
                        dl.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in dl.index])
                        frm = dict(frames, **{sym: (m5, dl)})
                known = {(s, dd) for s, ds in by_sym.items() for dd in ds}
                hn = (sym, d) in has
                if sym in frm and (sym, d) not in known:
                    # gap-up name outside the replay pool: check its news too
                    m5x, dlx = frm[sym]
                    days = [pd.Timestamp(x).date() for x in dlx.index]
                    if d in days and (s36_gaps(dlx).iloc[days.index(d)] or 0) >= S36_GAP:
                        arts, bad = s36_news(sym, [d], client, limiter)
                        hn = (not bad) and s36_has_news(arts, d)
                        known = known | {(sym, d)}
                c, why, ent, r = s44_explain(sym, d, frm, cands, picks, hn, known)
                mr.append([str(d), sym, note, c, why, ent, r])
        lines += ["**Missed movers 10-19..10-21.**", ""]
        lines += sc.table(["Day", "Ticker", "Source", "Candidate", "Detail",
                           "Entered", "R (2R target)"], mr)
        lines += [""]
    lines += [
        "### Reading",
        "",
        "_(Reading: written on BOARDROOM_AGENDA.md item 26.)_",
        "",
        "### Limits",
        "",
        "- Today's universe (survivorship): the names are today's 162-name "
        "universe, not each day's; names that left the list are missing.",
        "- IEX 5-minute bars (the live feed): IEX volume is a slice of the "
        "tape, bars can be missing and the 09:30 print can differ from the "
        "consolidated open, so the gap, OR and ADV are IEX-based.",
        "- Finnhub free tier: about one year of company news, so coverage "
        "starts ~2025-10-26; the news test is Finnhub's view only (no PR "
        "wires, no earnings calendar).",
        "- No gatekeeper, no whole-share sizing, no slippage or spread on the "
        "next-bar-open fill, no max-positions or daily-loss breaker: an upper "
        "bound on what the desk would have realised.",
        "",
        "Reproduce with `python study_exit_grid.py --s44`.",
    ]
    return lines, {"base": base, "runs": runs, "verdict": verdict, "picks": picks,
                   "cands": cands, "has": has, "frames": frames}


# =========================================================== S45 (2026-10-22)
# Funnel replay PER BLOCKER (CEO 2026-10-22, item B). Study only.
#
# For each deterministic blocker X: the signals X stopped and NOTHING else
# stopped - they pass every other filter of the detector (before AND after X
# in the live order) and every worker gate - replayed through the LIVE exits,
# against the signals that passed everything over the same window.
#
# The detectors in strategies/ return their FIRST failing filter. The study
# copies below (s45_daily_check, s45_trend_fails) evaluate EVERY filter in the
# same order and return the full list of failures; "blocked by X" means that
# list is exactly [X]. tests/test_s45_funnel_replay.py proves the copies
# agree with the live detectors (same trigger, same first failure, same
# entry/stop on a pass) on synthetic bars.
#
# Gates after the detector, in worker order (streamlit_app.py):
#   daily:  Rule #3 gap_below_signal_mid; spy_bearish (Rule #5, the reclaim
#           exempt per spy_filter_exempt); outside_hours does NOT bite - the
#           worker only scans 09:30-16:00 and a daily setup is evaluated once,
#           on the first cycle after today's daily bar exists, i.e. at the
#           open, inside the 09:30-11:30 window; the S25 ATR initial stop;
#           whole-share sizing on the live capital (backtest._live_sizing).
#   trend:  spy_bearish (last completed SPY close vs its 20-day EMA, unknown
#           regime fails closed); outside_hours (09:30-11:30 / 14:00-15:00 ET,
#           the bar after the cross); sizing.
# Not modelled: watch_only catalysts, the circuit breaker, max positions, the
# gatekeeper.
#
# Exits: daily setups = S34 'current' (s34_run, 3R S28-capped, no partial);
# trend_continuation = the S33 intraday replay (study_adx_band.simulate_
# intraday). For each blocker the population "passed + blocked by X" is
# replayed together (one position per symbol), as if X were loosened, and the
# blocked subset is reported; PASSED = the passed-only replay.

S45_START, S45_END = "2025-10-14", "2026-10-21"
S45_BAR_R, S45_BAR_N = 0.15, 50
S45_DAILY_SETUPS = ("momentum_continuation", "mean_reversion_reclaim")
S45_TREND = "trend_continuation"
S45_APPLIES = {
    "volume_low": ("momentum_continuation", "mean_reversion_reclaim", S45_TREND),
    "change_too_small": ("momentum_continuation",),
    "below_ema9": ("mean_reversion_reclaim",),
    "spy_bearish": ("momentum_continuation", S45_TREND),
    "rsi_low": (S45_TREND,),
    "outside_hours": (S45_TREND,),
}


def s45_daily_check(setup, window, vol_mult):
    """Study copy of the live daily detector `setup`, every filter evaluated.
    None = no trigger; else {"fails": [filter names, live order], "entry",
    "stop"} (entry/stop as the detector would set them)."""
    import pandas_ta as ta
    from strategies import mean_reversion_reclaim as mr
    from strategies import momentum_continuation as mc
    if setup == "momentum_continuation":
        lb = mc.LOOKBACK
        if window is None or len(window) < lb + 2:
            return None
        bar = window.iloc[-2]
        prior = window.iloc[-(lb + 2):-2]
        prior_high = float(prior["high"].max())
        avg_volume = float(prior["volume"].mean())
        prev_close = float(window["close"].iloc[-3])
        change = ((float(bar["close"]) / prev_close) - 1) * 100 if prev_close > 0 else 0.0
        if not float(bar["close"]) > prior_high:
            return None
        fails = []
        if avg_volume <= 0 or float(bar["volume"]) <= avg_volume * vol_mult:
            fails.append("volume_low")
        if change <= mc.MIN_CHANGE_PCT:
            fails.append("change_too_small")
        entry, stop = float(window["close"].iloc[-1]), float(bar["low"])
        if stop >= entry:
            fails.append("invalid_stop")
        return {"fails": fails, "entry": entry, "stop": stop}
    if setup == "mean_reversion_reclaim":
        lb = mr.LOOKBACK
        if window is None or len(window) < lb + 2:
            return None
        ema9 = ta.ema(window["close"], length=9)
        bar, prior_bar = window.iloc[-2], window.iloc[-3]
        win = window.iloc[-(lb + 2):-2]
        high_20, low = float(win["high"].max()), float(win["low"].min())
        if high_20 <= 0:
            return None
        washed = (high_20 - low) / high_20 * 100 >= mr.DRAWDOWN_PCT
        if not (washed and float(bar["close"]) > float(prior_bar["high"])):
            return None
        fails = []
        e9 = float(ema9.iloc[-2]) if ema9 is not None else float("nan")
        if not float(bar["close"]) > e9:
            fails.append("below_ema9")
        avg_volume = float(win["volume"].mean())
        if avg_volume <= 0 or (float(bar["volume"]) / avg_volume
                               < min(mr.SOFT_VOLUME_FLOOR, vol_mult)):
            fails.append("volume_low")
        if float(window["open"].iloc[-1]) < (float(bar["high"]) + float(bar["low"])) / 2.0:
            fails.append("gap_below_reclaim_mid")
        entry, stop = float(window["close"].iloc[-1]), float(bar["low"])
        if stop >= entry:
            fails.append("invalid_stop")
        return {"fails": fails, "entry": entry, "stop": stop}
    raise ValueError(setup)


def _vol_mult(setup, config):
    from strategies import mean_reversion_reclaim as mr
    from strategies import momentum_continuation as mc
    default = mc.VOLUME_MULT if setup == "momentum_continuation" else mr.VOLUME_MULT
    return (config.get("volume_multipliers") or {}).get(setup, default)


def s45_sizes(entry, stop):
    import backtest
    import risk
    return risk.position_size(backtest.BASE_EQUITY, backtest.RISK_PCT, entry, stop,
                              position_cap_pct=backtest.POSITION_CAP) >= 1


def s45_entry_window(daily, i):
    """The detector's view at the entry-day evaluation: bars up to i-1
    completed, bar i the session's partial bar AT THE OPEN (o=h=l=c=open)."""
    import backtest
    w = daily.iloc[max(0, i - backtest.DETECT_WINDOW + 1):i + 1].copy()
    o = float(w["open"].iloc[-1])
    w.loc[w.index[-1], ["high", "low", "close"]] = o
    return w


def s45_daily_candidates(frames, regime, config, start=S45_START, end=S45_END):
    """Every daily trigger in the window with its full list of failures
    (detector filters, then worker gates). frames {sym: (m5, daily)}."""
    import backtest
    exempt = set(config.get("spy_filter_exempt", []))
    params = dict(pm.r_atr_params(config["risk_profiles"]["Moderate"]))
    lo, hi = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    out = []
    for sym, (_m5, daily) in frames.items():
        atrs = atr_by_day(daily)
        for i in range(backtest.WARMUP_BARS, len(daily)):
            day = pd.Timestamp(daily.index[i])
            if not lo <= day.date() <= hi:
                continue
            w = s45_entry_window(daily, i)
            for setup in S45_DAILY_SETUPS:
                res = s45_daily_check(setup, w, _vol_mult(setup, config))
                if res is None:
                    continue
                fails = list(res["fails"])
                entry, structural = float(w["open"].iloc[-1]), res["stop"]
                sig = daily.iloc[i - 1]
                if entry < (float(sig["high"]) + float(sig["low"])) / 2.0 \
                        and "gap_below_reclaim_mid" not in fails:
                    fails.append("gap_below_signal_mid")
                trending = bool(regime.reindex([daily.index[i - 1]]).fillna(False).iloc[0])
                if setup not in exempt and not trending:
                    fails.append("spy_bearish")
                atr = atrs.get(day.date())
                if not atr:
                    fails.append("no_atr")
                elif "invalid_stop" not in fails:
                    stop = pm.initial_stop(entry, structural, atr, params)
                    if stop >= entry:
                        fails.append("invalid_stop")
                    elif not s45_sizes(entry, stop):
                        fails.append("zero_size")
                out.append({"setup": setup, "sym": sym, "day": day.tz_localize(ET),
                            "entry": entry, "stop": structural, "fails": fails})
    return out


def s45_daily_replay(cands, frames):
    sigs = [(c["setup"], c["sym"], c["day"], c["entry"], c["stop"]) for c in cands]
    trades = s34_run(sigs, frames, variants=[("current", 3.0, False)])["current"] if sigs else []
    for t in trades:
        t["when"] = pd.Timestamp(t["day"]).tz_localize(ET) + pd.Timedelta(minutes=570)
    return trades


def s45_trend_fails(df, j, st):
    """Study copy of the live trend_continuation detector at cross bar j
    (k = j + 1 is the bar the detector sees as the latest): None if no
    golden cross on j, else the detector's failures in live order. `df`
    carries study_adx_band.add_indicators columns."""
    ef, es = df["ema_fast"].values, df["ema_slow"].values
    if not (ef[j] > es[j] and ef[j - 1] <= es[j - 1]):
        return None
    row, k = df.iloc[j], j + 1
    fails = []
    if not row["adx"] >= st["adx"]:
        fails.append("adx_low")
    if st["rsi"] is not None and not row["rsi"] >= st["rsi"]:
        fails.append("rsi_low")
    if st["confirm"] and not (ef[k] > es[k]):
        fails.append("no_confirmation")
    if st["volume"] and not row["volume"] >= row["vol_ema"]:
        fails.append("volume_low")
    if not float(row["atr"]) > 0:
        fails.append("invalid_atr")
    return fails


S45_IND = ("ema_fast", "ema_slow", "adx", "rsi", "atr", "vol_ema")


def s45_trend_candidates(df, sym, st, spy_trend, start=S45_START, end=S45_END):
    """Golden crosses with their full failure list: detector filters, then
    spy_bearish, outside_hours, sizing (worker order)."""
    import numpy as np
    import study_adx_band as ab
    lo, hi = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    ok = ~np.isnan(df[list(S45_IND)].values.astype(float)).any(axis=1)
    ef, es = df["ema_fast"].values, df["ema_slow"].values
    out = []
    for j in range(22, len(df) - 2):
        if not (ef[j] > es[j] and ef[j - 1] <= es[j - 1]):
            continue
        k = j + 1
        if not (ok[j - 1] and ok[j] and ok[k]):
            continue
        ts = df.index[k] + pd.Timedelta(minutes=5)
        if not lo <= ts.date() <= hi:
            continue
        fails = s45_trend_fails(df, j, st)
        if not spy_trend.get(ts.date()):
            fails.append("spy_bearish")
        if not ab.in_entry_window(df.index[k]):
            fails.append("outside_hours")
        entry = float(df["close"].iloc[k])
        atr = float(df["atr"].iloc[j])
        stop = entry - st["atr_mult"] * atr
        if "invalid_atr" not in fails and not s45_sizes(entry, stop):
            fails.append("zero_size")
        out.append({"setup": S45_TREND, "sym": sym, "i": k, "ts": ts, "entry": entry,
                    "atr": atr, "fails": fails})
    return out


def s45_trend_replay(prepared, keep, st):
    """The S33 intraday replay (study_adx_band.s33_replay's inner loop) over
    the candidates `keep` admits. prepared {sym: (df, datr, cands)}."""
    import study_adx_band as ab
    trades = []
    for sym, (df, datr, cands) in prepared.items():
        busy_until = -1
        for c in cands:
            if not keep(c) or c["i"] <= busy_until:
                continue
            entry = c["entry"]
            stop = entry - st["atr_mult"] * c["atr"]
            target = entry + st["rr"] * (entry - stop)
            d_atr = datr.get(c["ts"].date())
            if d_atr:
                target = min(target, entry + st["cap_atr"] * d_atr)
            res = ab.simulate_intraday(df, c["i"], entry, stop, target, st["trail_atr"])
            if res is None:
                busy_until = len(df)
                continue
            px, why, n = res
            busy_until = n
            trades.append({"setup": S45_TREND, "sym": sym, "i": c["i"], "when": c["ts"],
                           "r": (px - entry) / (entry - stop), "why": why})
    return trades


def _key(t):
    return (t["setup"], t["sym"], t["i"] if t["setup"] == S45_TREND else t["day"])


def _ckey(c):
    return (c["setup"], c["sym"], c["i"] if c["setup"] == S45_TREND else c["day"].date())


def s45_run(daily_cands, frames, trend_prepared, st):
    """{blocker: {"blocked": trades, "signals": n blocked signals,
    "passed": trades}} plus the passed-only baseline under "_passed"."""
    trend_cands = [c for _df, _d, cs in trend_prepared.values() for c in cs]
    passed = (s45_daily_replay([c for c in daily_cands if not c["fails"]], frames)
              + s45_trend_replay(trend_prepared, lambda c: not c["fails"], st))
    out = {"_passed": passed}
    for x, setups in S45_APPLIES.items():
        blocked = [c for c in daily_cands + trend_cands
                   if c["setup"] in setups and c["fails"] == [x]]
        keys = {_ckey(c) for c in blocked}
        dsel = [c for c in daily_cands if c["setup"] in setups
                and (not c["fails"] or c["fails"] == [x])]
        trades = s45_daily_replay(dsel, frames)
        if S45_TREND in setups:
            trades += s45_trend_replay(
                trend_prepared, lambda c: not c["fails"] or c["fails"] == [x], st)
        out[x] = {"blocked": [t for t in trades if _key(t) in keys],
                  "signals": len(blocked),
                  "passed": [t for t in passed if t["setup"] in setups]}
    return out


def s45_stats(trades):
    ts = sorted(trades, key=lambda t: pd.Timestamp(t["when"]))
    st = sc.aggregate(ts)
    st["total_r"] = round(sum(t["r"] for t in ts), 2)
    return st


def s45_verdict(st):
    """The CEO rule, verbatim: a blocker with >= +0.15R over >= 50 rejected
    signals -> CEO loosens it for probation next Monday."""
    return bool(st["trades"] >= S45_BAR_N and st["expectancy_r"] is not None
                and st["expectancy_r"] >= S45_BAR_R)


def _cells(st):
    if not st["trades"]:
        return [0, "-", "-", "+0.00", "-", "-"]
    return [st["trades"], f"{st['win_rate']}%", f"{st['expectancy_r']:+.3f}",
            f"{st['total_r']:+.2f}", st["profit_factor"], st["max_drawdown_r"]]


def s45_rows(results):
    rows = []
    for x, setups in S45_APPLIES.items():
        r = results[x]
        groups = [("all", setups)] + ([(s, (s,)) for s in setups] if len(setups) > 1 else [])
        for label, ss in groups:
            b = s45_stats([t for t in r["blocked"] if t["setup"] in ss])
            p = s45_stats([t for t in r["passed"] if t["setup"] in ss])
            rows.append({"blocker": x, "setup": label if label != "all" else
                         " + ".join(setups) if len(setups) > 1 else setups[0],
                         "pooled": label == "all", "blocked": b, "passed": p,
                         "meets": s45_verdict(b)})
    return rows


def s45_render(results, n_symbols, n_daily, n_trend, missing=()):
    rows = s45_rows(results)
    lines = [f"**S45 (CEO 2026-10-22, item B) - funnel replay per blocker, study "
             f"only.** One year of 5-minute IEX bars, signals {S45_START}.."
             f"{S45_END}, {n_symbols} universe symbols ({n_daily} daily triggers, "
             f"{n_trend} trend_continuation crosses). For each deterministic "
             f"blocker: the signals it stopped that passed EVERY other live "
             f"filter and gate, replayed through the live exits, against the "
             f"signals that passed everything (same setups, same window).", ""]
    lines += sc.table(
        ["Blocker", "Setup(s)", "Blocked signals", "Rejected n (replayed)", "Win %",
         "Expectancy (R)", "Total R", "PF", "MaxDD (R)", "|", "Passed n", "Win %",
         "Expectancy (R)", "Total R", "PF", "MaxDD (R)", "Rule met"],
        [[("**" + r["blocker"] + "**") if r["pooled"] else "", r["setup"],
          results[r["blocker"]]["signals"] if r["pooled"] else "",
          *_cells(r["blocked"]), "|", *_cells(r["passed"]),
          ("YES" if r["meets"] else "no")] for r in rows])
    lines += ["", "adx_low (trend_continuation) is not redone here: S33 (item 20) "
              "replayed the signals the ADX 30 gate blocks at +0.022R to +0.054R.", ""]
    met = [r for r in rows if r["pooled"] and r["meets"]]
    sub = [r for r in rows if not r["pooled"] and r["meets"]]
    lines.append(f"**CEO rule** (\"a blocker with >= +{S45_BAR_R}R over >= {S45_BAR_N} "
                 f"rejected signals -> CEO loosens it for probation next Monday\"): "
                 + ("met by " + ", ".join(f"{r['blocker']} ({r['blocked']['expectancy_r']:+.3f}R "
                                          f"over {r['blocked']['trades']})" for r in met)
                    if met else "met by NONE of the blockers") + "."
                 + (" Per-setup rows that clear it on their own: "
                    + ", ".join(f"{r['blocker']} / {r['setup']}" for r in sub) + "."
                    if sub else ""))
    lines += ["", "**Method.** Study copies of the detectors evaluate every filter "
              "(the live ones stop at the first failure); a signal is 'blocked by X' "
              "when X is its ONLY failure - detector filters before and after X, then "
              "the worker gates (Rule #3 gap, SPY Rule #5 with the reclaim exempt, "
              "entry hours for trend_continuation, the S25 ATR initial stop and "
              "whole-share sizing on the live $5,000 / 1% / 25% config). Live "
              "volume multipliers from bot_config.json (momentum 1.0x, reclaim "
              "1.3x with the 1.0x soft floor). Daily setups are evaluated at the "
              "open (the partial daily bar = the open), so outside_hours does not "
              "apply to them: the worker scans only 09:30-16:00 and judges a daily "
              "setup once, on the first cycle after the day's bar exists. Daily "
              "exits = S34 'current' (R/ATR ladder +1R->BE, +1.5R->BE, +2R->+1R, "
              "then highest close - 2.5 ATR; time stops day 5/10; 3R target "
              "S28-capped at 3 ATR); trend_continuation exits = the S33 intraday "
              "rule (2 ATR stop, 3R target S28-capped, 2.5 ATR trail from +1R "
              "floored at entry). For each blocker, passed + blocked-by-X are "
              "replayed together, one position per symbol, as if X were loosened; "
              "the table shows the blocked subset. Reproduce with "
              "`python study_exit_grid.py --s45 --fetch` (fetch after the close).",
              "", "**Limits.** Today's universe (survivorship); IEX bars, daily bars "
              "resampled from them; signals, not gatekeeper-approved trades; one "
              "year; post_earnings_continuation excluded (no historical earnings "
              "calendar), pullback_in_uptrend retired; earnings exits, watch-only "
              "catalysts, max positions and the daily-loss breaker not modelled; "
              "trend indicators on the full series, not the worker's 5-day window."
              + (f" No bars for: {', '.join(missing)}." if missing else ""),
              "", sc.UPPER_BOUND]
    return lines


def s45_main(append=False, universe_file="universe_today.json", fetch=False,
             cache=S34_CACHE):
    import backtest
    import study_adx_band as ab
    symbols = [s for s in ab.study_universe(universe_file) if s != "SPY"]
    if not symbols:
        print(f"No universe symbols in {universe_file}.")
        return 2
    if fetch:
        print(f"fetched {ab.fetch_m5_year(['SPY'] + symbols, start=S45_START, end=S45_END, cache=cache)}")
    config = _live_config()
    st = ab.s33_settings(config)
    spy5 = load_m5_rth("SPY", cache)
    if spy5 is None:
        print("No SPY bars - run with --fetch after the close.")
        return 2
    spy_d = daily_from_m5(spy5)
    regime = backtest.spy_regime_series(spy_d)
    regime.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in regime.index])
    spy_trend, _ = ab.spy_regime_maps(ab.load_m5("SPY", cache))
    frames, prepared, missing = {}, {}, []
    for sym in symbols:
        m5 = load_m5_rth(sym, cache)
        if m5 is None or m5.empty:
            missing.append(sym)
            continue
        daily = daily_from_m5(m5)
        daily.index = pd.DatetimeIndex([pd.Timestamp(i).tz_localize(None) for i in daily.index])
        frames[sym] = (m5, daily)
        raw = ab.load_m5(sym, cache)
        df = ab.add_indicators(raw)
        datr = ab.prior_day_map(dict(zip(*ab._daily_atr(raw))))
        prepared[sym] = (df, datr, s45_trend_candidates(df, sym, st, spy_trend))
    daily_cands = s45_daily_candidates(frames, regime, config)
    results = s45_run(daily_cands, frames, prepared, st)
    n_trend = sum(len(c) for _a, _b, c in prepared.values())
    lines = s45_render(results, len(frames), len(daily_cands), n_trend, missing)
    print("\n".join(lines))
    if append:
        n = sc.append_agenda("Study: S45 funnel replay per blocker - are the "
                             "deterministic filters throwing away winners? (2026-10-22)",
                             lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


# =========================================================== S46 (2026-10-23)
# CEO 2026-10-23, agenda 27 decision: re-run trend_continuation's
# outside_hours-blocked signals restricted to the MIDDAY window (entry bar
# 11:30-14:00 ET) with a FORCED SAME-DAY EXIT at the 15:55 ET bar's open - no
# overnight hold. Bar: >= +0.15R over >= 50 trades -> midday probation.
# Two counts: every midday signal (one position per symbol), and as the live
# probation would trade it - at most ONE midday position open at a time.

S46_WINDOW = ((11, 30), (14, 0))
S46_EXIT = (15, 55)


def in_midday(ts):
    t = (ts.hour, ts.minute)
    return S46_WINDOW[0] <= t < S46_WINDOW[1]


def simulate_intraday_eod(df, i, entry, stop, target, trail_atr, exit_at=S46_EXIT):
    """The S33 intraday rule (stop, 3R target S28-capped, 2.5 ATR trail from
    +1R floored at entry) PLUS a forced exit at the open of the `exit_at` bar
    of the entry day - or the first bar of a later day. (px, reason, exit_i)."""
    risk = entry - stop
    live, reached = stop, False
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    atr = df["atr"].values
    day = df.index[i].date()
    for n in range(i + 1, len(df)):
        ts = df.index[n]
        if ts.date() != day or (ts.hour, ts.minute) >= exit_at:
            return o[n], "time exit 15:55", n
        if o[n] <= live:
            return o[n], "stop (gap)", n
        if o[n] >= target:
            return o[n], "target (gap)", n
        if l[n] <= live:
            return live, "stop", n
        if h[n] >= target:
            return target, "target", n
        if not reached and (c[n] - entry) / risk >= 1.0:
            reached = True
        if reached and not math.isnan(atr[n]):
            cand = max(c[n] - trail_atr * atr[n], entry)
            if live < cand < c[n]:
                live = cand
    return None


def s46_trades(prepared, st, max_open=None):
    """Midday outside_hours-only signals, replayed with the forced 15:55 exit.
    max_open=1 models the probation's one-position limit across symbols."""
    cands = []
    for sym, (df, datr, cs) in prepared.items():
        for c in cs:
            if c["fails"] == ["outside_hours"] and in_midday(df.index[c["i"]]):
                cands.append((c["ts"], sym, c))
    cands.sort(key=lambda x: x[0])
    trades, busy_sym, open_until = [], {}, None
    for ts, sym, c in cands:
        df, datr, _ = prepared[sym]
        if c["i"] <= busy_sym.get(sym, -1):
            continue
        if max_open == 1 and open_until is not None and ts < open_until:
            continue
        entry = c["entry"]
        stop = entry - st["atr_mult"] * c["atr"]
        target = entry + st["rr"] * (entry - stop)
        d_atr = datr.get(ts.date())
        if d_atr:
            target = min(target, entry + st["cap_atr"] * d_atr)
        res = simulate_intraday_eod(df, c["i"], entry, stop, target, st["trail_atr"])
        if res is None:
            continue
        px, why, n = res
        busy_sym[sym] = n
        open_until = df.index[n]
        trades.append({"setup": S45_TREND, "sym": sym, "i": c["i"], "when": ts,
                       "r": (px - entry) / (entry - stop), "why": why})
    return trades


def s46_render(all_t, one_t, n_symbols):
    rows = []
    for label, ts in (("all midday signals", all_t), ("max 1 open (probation)", one_t)):
        st_ = sc.aggregate(sorted(ts, key=lambda t: pd.Timestamp(t["when"])))
        weeks = (pd.Timestamp(S45_END) - pd.Timestamp(S45_START)).days / 7
        time_exits = sum(1 for t in ts if t["why"].startswith("time exit"))
        rows.append([label, st_["trades"], f"{st_['trades'] / weeks:.1f}",
                     f"{st_['win_rate']}%" if st_["trades"] else "-",
                     f"{st_['expectancy_r']:+.3f}" if st_["trades"] else "-",
                     f"{sum(t['r'] for t in ts):+.1f}",
                     st_["profit_factor"] if st_["trades"] else "-",
                     st_["max_drawdown_r"] if st_["trades"] else "-",
                     f"{100 * time_exits / len(ts):.0f}%" if ts else "-"])
    lines = [f"**S46 (CEO 2026-10-23) - re-run of agenda 27's outside_hours "
             f"finding, midday only (entries 11:30-14:00 ET), forced exit at "
             f"15:55 ET the same day.** trend_continuation signals whose ONLY "
             f"failure is the entry-window gate, {S45_START}..{S45_END}, "
             f"{n_symbols} symbols; live intraday exit rule otherwise.", ""]
    lines += sc.table(["Count", "Trades", "Trades/week", "Win %", "Expectancy (R)",
                       "Total R", "PF", "MaxDD (R)", "Exited at 15:55"], rows)
    return lines


def s46_main(append=False, universe_file="universe_today.json", cache=S34_CACHE):
    import study_adx_band as ab
    symbols = [s for s in ab.study_universe(universe_file) if s != "SPY"]
    config = _live_config()
    st = ab.s33_settings(config)
    spy_trend, _ = ab.spy_regime_maps(ab.load_m5("SPY", cache))
    prepared = {}
    for sym in symbols:
        raw = ab.load_m5(sym, cache)
        if raw is None or raw.empty:
            continue
        df = ab.add_indicators(raw)
        datr = ab.prior_day_map(dict(zip(*ab._daily_atr(raw))))
        prepared[sym] = (df, datr, s45_trend_candidates(df, sym, st, spy_trend))
    all_t = s46_trades(prepared, st)
    one_t = s46_trades(prepared, st, max_open=1)
    lines = s46_render(all_t, one_t, len(prepared))
    st1 = sc.aggregate(one_t)
    ok = st1["trades"] >= S45_BAR_N and (st1["expectancy_r"] or 0) >= S45_BAR_R
    lines += ["", (f"**Bar met (max 1 open): {st1['expectancy_r']:+.3f}R over "
                   f"{st1['trades']} trades** -> midday probation per the CEO "
                   f"order." if ok else
                   f"**Bar not met (max 1 open): {st1['expectancy_r'] or 0:+.3f}R "
                   f"over {st1['trades']} trades** -> no change.")]
    print("\n".join(lines))
    return lines, ok


# =========================================================== S48 (2026-10-22)
# CEO P4: GAP-FREE INTRADAY RALLIES - the movers our setups never see (e.g.
# DDOG, ZS, NET, CRWD on 2026-10-21 gapped only 0.4-2.8% and ran later).
# Study only, no live change.
#   - Gap-free day: |first RTH open vs prior RTH close| < 3%. No news gap:
#     exclude the day if |gap| >= 1.5% AND Finnhub company news exists in the
#     18 h before 09:30 ET (news fetched only for those days that trigger;
#     a failed fetch counts as no news).
#   - Trigger: within the first two hours (bars starting 09:30-11:25 ET), the
#     first completed 5-min close >= +3.0% above the prior RTH close, AND the
#     symbol's sector ETF is up on the day at that bar's close (ETF close >
#     its prior RTH close; the ETF bar at or before the trigger bar).
#   - Entry: next bar's open. Stop: the day's lowest low 09:30..trigger bar;
#     skip if risk <= 0. Target: entry + 2R. Forced exit at the 15:55 ET bar's
#     open. A bar opening through a level fills at its open; a bar spanning
#     stop and target takes the stop. One trade per symbol per day.

S48_START, S48_END = "2025-10-14", "2026-10-21"
S48_GAP_MAX = 0.03
S48_NEWS_GAP = 0.015
S48_UP = 0.03
S48_TRIG_LAST = 685          # last trigger bar starts 11:25 ET
S48_EXIT = 955               # 15:55 ET
S48_TARGETS = (2.0, 1.5, 3.0)
S48_BASE_K = 2.0
S48_BAR_R, S48_BAR_N = 0.15, 50
S48_ETF_CACHE = os.path.join("data", "s48")
S48_NEWS_DIR = os.path.join("data", "s48_news")
S48_MOVERS = ("DDOG", "ZS", "NET", "CRWD")
S48_MOVER_DAY = "2026-10-21"
# sectors.py tag -> SPDR ETF. sectors.py has no semiconductor tag and one
# combined utilities_reit tag, so two explicit sub-lists split them.
S48_ETF = {"technology": "XLK", "financials": "XLF", "energy": "XLE",
           "healthcare": "XLV", "consumer_discretionary": "XLY",
           "consumer_staples": "XLP", "industrials": "XLI",
           "materials": "XLB", "communication": "XLC"}
S48_SEMIS = {"NVDA", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT", "LRCX",
             "KLAC", "ADI", "NXPI", "MRVL", "ON", "SWKS", "MCHP", "AVGO",
             "ARM", "TER", "ENTG"}
S48_REITS = {"PLD", "AMT", "CCI", "SPG", "O"}


def s48_etf_for(sym):
    """SPDR ETF for a symbol, or None (crypto_dat / unclassified: excluded)."""
    import sectors
    tag = sectors.sector_for(sym)
    if tag == "technology" and sym.upper() in S48_SEMIS:
        return "SMH"
    if tag == "utilities_reit":
        return "XLRE" if sym.upper() in S48_REITS else "XLU"
    return S48_ETF.get(tag)


def s48_gap_free(gap, limit=S48_GAP_MAX):
    return gap is not None and not pd.isna(gap) and abs(gap) < limit


def s48_needs_news(gap, lo=S48_NEWS_GAP):
    """True when the day is only kept if Finnhub shows no news pre-open."""
    return abs(gap) >= lo


def s48_etf_up(etf_bars, etf_prev, ts):
    """ETF close of the bar at or before `ts` (same session) > etf_prev."""
    if etf_bars is None or etf_prev is None or etf_bars.empty:
        return False
    sel = etf_bars[etf_bars.index <= ts]
    if sel.empty:
        return False
    return float(sel["close"].iloc[-1]) > etf_prev


def s48_trigger(bars, prev_close, etf_bars, etf_prev, up=S48_UP, both=False):
    """(i, status). Main rule (both=False): the FIRST bar starting 09:30..11:25
    whose close >= prev_close x (1+up); status 'ok' if the ETF is up at that
    bar, 'etf_down' otherwise. both=True (sensitivity): the first such bar at
    which the ETF is also up. (None, 'no_trigger') when no bar qualifies."""
    lvl = prev_close * (1.0 + up)
    seen = False
    for i, ts in enumerate(bars.index):
        m = _hm(ts)
        if m > S48_TRIG_LAST:
            break
        if m < 570 or float(bars["close"].iloc[i]) < lvl:
            continue
        seen = True
        if s48_etf_up(etf_bars, etf_prev, ts):
            return i, "ok"
        if not both:
            return i, "etf_down"
    return None, ("etf_down" if seen else "no_trigger")


def s48_simulate(bars, i, entry, stop, target):
    """(exit price, reason, exit time) walking from bar i (filled at its open);
    forced exit at the 15:55 ET bar's open."""
    last = None
    for ts, b in bars.iloc[i:].iterrows():
        o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        if _hm(ts) >= S48_EXIT:
            return o, "time 15:55", ts
        if o <= stop:
            return o, "stop (gap)", ts
        if o >= target:
            return o, "target (gap)", ts
        if l <= stop:
            return stop, "stop", ts
        if h >= target:
            return target, "target", ts
        last = (c, ts)
    return last[0], "time (last bar)", last[1]


def s48_trade(bars, prev_close, etf_bars, etf_prev, k=S48_BASE_K, both=False):
    """One symbol-day. dict with status: no_trigger / etf_down / no_fill_bar /
    no_risk / traded (then r, why, entry, stop, target, risk_pct, t_trig,
    t_entry, t_exit)."""
    i, why = s48_trigger(bars, prev_close, etf_bars, etf_prev, both=both)
    if i is None or why != "ok":
        return {"status": why, "t_trig": bars.index[i] if i is not None else None}
    if i + 1 >= len(bars) or _hm(bars.index[i + 1]) >= S48_EXIT:
        return {"status": "no_fill_bar"}
    stop = float(bars["low"].iloc[: i + 1].min())
    entry = float(bars["open"].iloc[i + 1])
    risk = entry - stop
    if risk <= 0:
        return {"status": "no_risk", "entry": entry, "stop": stop}
    target = entry + k * risk
    px, how, t_exit = s48_simulate(bars, i + 1, entry, stop, target)
    return {"status": "traded", "r": (px - entry) / risk, "why": how,
            "entry": entry, "stop": stop, "target": target, "exit": px,
            "risk_pct": 100 * risk / entry, "t_trig": bars.index[i],
            "t_entry": bars.index[i + 1], "t_exit": t_exit}


def s48_max_open(trades, n=1):
    """Trades taken when at most n positions (any symbol) may be open; a
    position exiting at a bar's open frees its slot for an entry at that open."""
    out, open_ = [], []
    for t in sorted(trades, key=lambda t: (t["t_entry"], t["sym"])):
        open_ = [x for x in open_ if x > t["t_entry"]]
        if len(open_) >= n:
            continue
        out.append(t)
        open_.append(t["t_exit"])
    return out


def s48_sessions(m5):
    """{date: that session's RTH bars} and {date: prior session's close}."""
    days = {}
    for d, b in m5.groupby(m5.index.date):
        days[d] = b
    order = sorted(days)
    prev = {order[j]: float(days[order[j - 1]]["close"].iloc[-1])
            for j in range(1, len(order))}
    return days, prev


def s48_scan(frames, etfs, start=S48_START, end=S48_END, ks=S48_TARGETS,
             both=False):
    """Every gap-free symbol-day in the window run through the setup at each
    target k. Returns ({k: [traded dicts]}, counters, unmapped symbols)."""
    lo, hi = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    out = {k: [] for k in ks}
    cnt = {"symbol_days": 0, "gap_excluded": 0, "gap_free": 0}
    unmapped = []
    for sym in sorted(frames):
        etf = s48_etf_for(sym)
        if etf is None or etf not in etfs:
            unmapped.append(sym)
            continue
        days, prev = frames[sym]
        edays, eprev = etfs[etf]
        for d in sorted(days):
            if d < lo or d > hi or d not in prev:
                continue
            bars = days[d]
            cnt["symbol_days"] += 1
            gap = float(bars["open"].iloc[0]) / prev[d] - 1.0
            if not s48_gap_free(gap):
                cnt["gap_excluded"] += 1
                continue
            cnt["gap_free"] += 1
            for k in ks:
                t = s48_trade(bars, prev[d], edays.get(d), eprev.get(d), k, both)
                if k == ks[0]:
                    cnt[t["status"]] = cnt.get(t["status"], 0) + 1
                if t["status"] == "traded":
                    t.update(sym=sym, day=d, gap=gap, etf=etf, k=k)
                    out[k].append(t)
    for k in ks:
        out[k].sort(key=lambda t: (t["t_entry"], t["sym"]))
    return out, cnt, unmapped


def s48_news_filter(trades, has_news):
    """Drop the days with |gap| >= 1.5% and pre-open news."""
    return [t for t in trades
            if not (s48_needs_news(t["gap"]) and (t["sym"], t["day"]) in has_news)]


def s48_fetch_news(need, client, limiter, cache_dir=S48_NEWS_DIR, log=None):
    """need = {(sym, date)}. Returns (has_news set, failed symbols, n articles,
    earliest article date)."""
    by_sym = {}
    for s, d in need:
        by_sym.setdefault(s, []).append(d)
    has, failed, n_art, first = set(), [], 0, None
    for j, sym in enumerate(sorted(by_sym)):
        arts, bad = s36_news(sym, by_sym[sym], client, limiter, cache_dir)
        if bad:
            failed.append(sym)
            arts = []
        n_art += len(arts)
        for a in arts:
            if a.get("datetime"):
                dd = pd.Timestamp(int(a["datetime"]), unit="s").date()
                first = dd if first is None or dd < first else first
        has.update((sym, d) for d in by_sym[sym] if s36_has_news(arts, d))
        if log and (j + 1) % 20 == 0:
            log(f"  news: {j + 1}/{len(by_sym)} symbols")
    return has, failed, n_art, first


def s48_row(label, trades, weeks=None):
    st = sc.aggregate(trades)
    if not trades:
        return [label, 0] + ["-"] * 7
    risk = sorted(t["risk_pct"] for t in trades)
    n = len(risk)
    med = risk[n // 2] if n % 2 else (risk[n // 2 - 1] + risk[n // 2]) / 2
    pf = st["profit_factor"]
    return [label, st["trades"], f"{st['trades'] / weeks:.2f}" if weeks else "-",
            f"{st['win_rate']}%", f"{st['expectancy_r']:+.3f}",
            f"{sum(t['r'] for t in trades):+.1f}",
            "inf" if pf == float("inf") else pf, st["max_drawdown_r"],
            f"{med:.2f}%"]


def s48_verdict(trades):
    st = sc.aggregate(trades)
    ok = st["trades"] >= S48_BAR_N and (st["expectancy_r"] or 0) >= S48_BAR_R
    return ("candidate for probation (CEO decision)" if ok else "not proposed"), ok


def s48_mover(sym, day, frames, etfs, has_news, news_failed, k=S48_BASE_K):
    """One row for the 2026-10-21 movers: gap, trigger, entry, result."""
    d = pd.Timestamp(day).date()
    etf = s48_etf_for(sym)
    if sym not in frames:
        return [sym, "-", etf or "-", "no bars cached", "-", "-", "-"]
    days, prev = frames[sym]
    if d not in days or d not in prev:
        return [sym, "-", etf or "-", "no bars that day", "-", "-", "-"]
    bars = days[d]
    gap = float(bars["open"].iloc[0]) / prev[d] - 1.0
    gtxt = f"{gap * 100:+.2f}%"
    if not s48_gap_free(gap):
        return [sym, gtxt, etf, "not gap-free", "-", "-", "-"]
    edays, eprev = etfs.get(etf, ({}, {}))
    t = s48_trade(bars, prev[d], edays.get(d), eprev.get(d), k)
    hi = float(bars["high"].iloc[[_hm(x) <= S48_TRIG_LAST for x in bars.index]].max())
    if t["status"] != "traded":
        if t["status"] == "etf_down":
            why = f"+3% close at {t['t_trig'].strftime('%H:%M')} but {etf} down"
        elif t["status"] == "no_trigger":
            why = (f"no 5-min close >= +3% by 11:30 (high to 11:30 "
                   f"{(hi / prev[d] - 1) * 100:+.1f}%)")
        else:
            why = t["status"]
        return [sym, gtxt, etf, why, "-", "-", "-"]
    excl = s48_needs_news(gap) and (sym, d) in has_news
    note = " (Finnhub failed: no news assumed)" if sym in news_failed else ""
    trig = t["t_trig"].strftime("%H:%M") + (" - EXCLUDED: gap >= 1.5% + news"
                                            if excl else "") + note
    return [sym, gtxt, etf, trig,
            f"{t['t_entry'].strftime('%H:%M')} @ {t['entry']:.2f} "
            f"(stop {t['stop']:.2f}, risk {t['risk_pct']:.2f}%)",
            f"{t['r']:+.2f}R ({t['why']}, {t['t_exit'].strftime('%H:%M')})",
            "no" if excl else "yes"]


def s48_load(cache, symbols):
    out = {}
    for sym in symbols:
        m5 = load_m5_rth(sym, cache)
        if m5 is None or m5.empty:
            continue
        out[sym] = s48_sessions(m5)
    return out


def s48_main(append=False, cache=S34_CACHE, etf_cache=S48_ETF_CACHE,
             client=None, limiter=None, start=S48_START, end=S48_END,
             log=None):
    log = log or (lambda m: print(m, file=sys.stderr))
    symbols = sorted(f[3:-4] for f in os.listdir(cache)
                     if f.startswith("m5_") and f.endswith(".csv"))
    symbols = [s for s in symbols if s != "SPY"]
    frames = s48_load(cache, symbols)
    etfs = s48_load(etf_cache, sorted(set(S48_ETF.values()) | {"SMH", "XLU", "XLRE"}))
    spy5 = load_m5_rth("SPY", cache)
    spy = s44_spy_trend(daily_from_m5(spy5)) if spy5 is not None else {}
    runs, cnt, unmapped = s48_scan(frames, etfs, start, end)
    both, _c2, _u2 = s48_scan(frames, etfs, start, end, ks=(S48_BASE_K,), both=True)
    need = {(t["sym"], t["day"]) for k in runs for t in runs[k] + both[S48_BASE_K]
            if s48_needs_news(t["gap"])}
    for sym in S48_MOVERS:
        days, prev = frames.get(sym, ({}, {}))
        d = pd.Timestamp(S48_MOVER_DAY).date()
        if d in days and d in prev:
            g = float(days[d]["open"].iloc[0]) / prev[d] - 1
            if s48_needs_news(g) and s48_gap_free(g):
                need.add((sym, d))
    if client is None:
        import finnhub
        from dotenv import load_dotenv
        load_dotenv("/Users/ranasaradb/TradingBot/.env")
        client = finnhub.Client(api_key=os.environ.get("FINNHUB_API_KEY", ""))
    log(f"S48: {len(need)} symbol-days need a news check")
    has, failed, n_art, first_news = s48_fetch_news(
        need, client, limiter or S36Limiter(), log=log)
    kept = {k: s48_news_filter(v, has) for k, v in runs.items()}
    base = kept[S48_BASE_K]
    for t in base:
        t["spy"] = spy.get(t["day"])
    weeks = (pd.Timestamp(end) - pd.Timestamp(start)).days / 7
    hdr = ["Rows", "Trades", "Trades/week", "Win %", "Expectancy (R)", "Total R",
           "PF", "MaxDD (R)", "Median risk %"]
    rows = [s48_row(f"**{S48_BASE_K:g}R target (main definition)**", base, weeks)]
    for k in S48_TARGETS[1:]:
        rows.append(s48_row(f"sensitivity: {k:g}R target", kept[k], weeks))
    rows.append(s48_row("split @2R: SPY trending (prior close > 20d EMA)",
                        [t for t in base if t["spy"] is True], weeks))
    rows.append(s48_row("split @2R: SPY not trending",
                        [t for t in base if t["spy"] is not True], weeks))
    rows.append(s48_row("sensitivity @2R: max 1 open across symbols",
                        s48_max_open(base, 1), weeks))
    rows.append(s48_row("sensitivity @2R: trigger = first bar with +3% AND ETF up",
                        s48_news_filter(both[S48_BASE_K], has), weeks))
    rows.append(s48_row("reference @2R: no news exclusion", runs[S48_BASE_K], weeks))
    mrows = [s48_row(m, [t for t in base if t["day"].strftime("%Y-%m") == m])
             for m in sorted({t["day"].strftime("%Y-%m") for t in base})]
    verdict, _ok = s48_verdict(base)
    st = sc.aggregate(base)
    whys = {}
    for t in base:
        w = t["why"].split(" ")[0]
        whys[w] = whys.get(w, 0) + 1
    n_excl = len(runs[S48_BASE_K]) - len(base)
    mover_rows = [s48_mover(s, S48_MOVER_DAY, frames, etfs, has, failed)
                  for s in S48_MOVERS]
    etf_map = {}
    for s in frames:
        e = s48_etf_for(s)
        if e:
            etf_map.setdefault(e, 0)
            etf_map[e] += 1
    lines = [
        "**S48 (CEO P4, 2026-10-22) - gap-free intraday rallies, study only, "
        "no live change.** The movers our setups never see (DDOG, ZS, NET, "
        "CRWD on 2026-10-21 gapped only 0.4-2.8% and ran later). Definition:",
        "",
        "> - Gap-free day: |first RTH open vs prior RTH close| < 3%. \"No news "
        "gap\": exclude the day if the gap is >= 1.5% AND Finnhub company news "
        "exists in the 18 h before 09:30 ET (fetch news only for those >= 1.5% "
        "gap days; if Finnhub fails, treat as no news and say so).",
        "> - Trigger: within the FIRST TWO HOURS (bars starting 09:30-11:25 ET), "
        "the first completed 5-min close >= +3.0% above the prior RTH close, AND "
        "the symbol's sector ETF is up on the day at that bar's close (ETF close "
        "> its prior RTH close).",
        "> - Entry: the next bar's open. Stop: the day's lowest low from 09:30 "
        "through the trigger bar; skip if risk <= 0. Target: entry + 2R. Forced "
        "exit at the 15:55 ET bar's open (no overnight). Fills: a bar opening "
        "through a level fills at its open; a bar spanning stop and target takes "
        "the stop. One trade per symbol per day.",
        "> - Sensitivities: SPY trending (prior close > 20d EMA) vs not; targets "
        "1.5R and 3R; a max-1-open-across-symbols count.",
        "",
        "Implementation notes: the 1.5% news test uses |gap| (gap up or down). "
        "News is fetched only for >= 1.5% gap days that actually trigger (a "
        "non-triggering day cannot trade, so its news cannot change a result). "
        "\"ETF up at that bar's close\" = the ETF's 5-min close at the trigger "
        "bar's timestamp (or the last ETF bar before it that session) above the "
        "ETF's prior RTH close. If the first +3% close comes with the ETF down, "
        "the day is not traded (main rule); the row \"first bar with +3% AND ETF "
        "up\" instead waits for the ETF within the same window. SPY trending is "
        "as of the previous session.",
        "",
        f"ETF map (sectors.sector_for tag -> ETF): technology -> XLK, except "
        f"semiconductors {', '.join(sorted(S48_SEMIS & set(frames)))} -> SMH; "
        "communication -> XLC; consumer_discretionary -> XLY; consumer_staples "
        "-> XLP; financials -> XLF; healthcare -> XLV; industrials -> XLI; "
        "energy -> XLE; materials -> XLB; utilities_reit split: "
        f"{', '.join(sorted(S48_REITS & set(frames)))} -> XLRE, the rest -> XLU. "
        "AMZN, GOOGL, META, PYPL, SHOP follow the house 'technology' tag (XLK). "
        f"Symbols per ETF: {', '.join(f'{e} {n}' for e, n in sorted(etf_map.items()))}. "
        f"**Unmapped, excluded ({len(unmapped)}):** {', '.join(unmapped) or '-'} "
        "(crypto_dat and unclassified tags).",
        "",
        f"Window {start}..{end} ({weeks:.1f} weeks), {len(frames) - len(unmapped)} "
        f"mapped symbols. {cnt['symbol_days']} symbol-days, {cnt['gap_excluded']} "
        f"with |gap| >= 3% dropped, {cnt['gap_free']} gap-free; outcomes at 2R: "
        f"no trigger {cnt.get('no_trigger', 0)}, +3% with ETF down "
        f"{cnt.get('etf_down', 0)}, no risk {cnt.get('no_risk', 0)}, traded "
        f"{cnt.get('traded', 0)}; {n_excl} of those excluded as >= 1.5% gap + "
        f"news. Finnhub: {len(need)} symbol-days checked on "
        f"{len({s for s, _ in need})} symbols, {len(has)} with news, "
        f"{n_art} articles, earliest {first_news or '-'}; "
        f"{len(failed)} symbol(s) failed and count as no-news"
        + (f" ({', '.join(failed)})" if failed else "") + ". Exits at 2R: "
        + ", ".join(f"{k} {v}" for k, v in sorted(whys.items())) + ".",
        "",
    ]
    lines += sc.table(hdr, rows)
    lines += ["", "**By month (2R target, main definition).**", ""]
    lines += sc.table(["Month"] + hdr[1:], mrows)
    lines += ["", f"**Bar** (expectancy >= +{S48_BAR_R:.2f}R over >= "
              f"{S48_BAR_N} trades, main definition): {st['trades']} trades at "
              f"{(st['expectancy_r'] or 0):+.3f}R -> **{verdict}**.", "",
              f"**Would it have caught the {S48_MOVER_DAY} movers?**", ""]
    lines += sc.table(["Ticker", "Gap", "ETF", "Trigger", "Entry", "Result (2R)",
                       "Counted"], mover_rows)
    lines += [
        "",
        "### Reading",
        "",
        "_(Reading: written on the agenda item.)_",
        "",
        "### Limits",
        "",
        "- Today's universe (survivorship): the 162 names cached for S33, not "
        "each day's list.",
        "- IEX 5-minute bars (the live feed) for the symbols and the ETFs: the "
        "09:30 print and closes can differ from the consolidated tape, so the "
        "gap, the +3% trigger and the ETF test are IEX-based.",
        "- Finnhub free tier: about one year of company news; days before "
        "coverage starts count as no-news. Finnhub is the only news source "
        "(no PR wires, no earnings calendar).",
        "- No gatekeeper, no whole-share sizing, no slippage or spread on the "
        "next-bar-open fill, no daily-loss breaker: an UPPER BOUND.",
        "",
        "Reproduce with `python study_exit_grid.py --s48`.",
    ]
    return lines, {"base": base, "runs": kept, "verdict": verdict, "has": has,
                   "failed": failed, "cnt": cnt, "unmapped": unmapped,
                   "movers": mover_rows}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--s34", action="store_true")
    ap.add_argument("--s35", action="store_true")
    ap.add_argument("--s37", action="store_true")
    ap.add_argument("--s36", action="store_true")
    ap.add_argument("--s44", action="store_true")
    ap.add_argument("--s45", action="store_true")
    ap.add_argument("--s46", action="store_true")
    ap.add_argument("--s48", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--universe-file", default="universe_today.json")
    a = ap.parse_args()
    if a.s48:
        lines, _ = s48_main(a.append)
        print("\n".join(lines))
        if a.append:
            n = sc.append_agenda("Study: S48 gap-free intraday rallies (CEO P4, "
                                 "2026-10-22)", lines)
            print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
        return 0
    if a.s46:
        s46_main(a.append, a.universe_file)
        return 0
    if a.s45:
        return s45_main(a.append, a.universe_file, a.fetch)
    if a.s34:
        return s34_main(a.append, a.universe_file)
    if a.s36:
        s36_main(a.append, a.universe_file)
        return 0
    if a.s44:
        lines, _ = s44_main(a.append, a.universe_file)
        print("\n".join(lines))
        return 0
    if a.s37:
        return s37_main(a.append, a.universe_file)
    if a.s35:
        s35_main(a.append, a.universe_file)
        return 0
    results, n = run()
    print("\n".join(render(results, n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
