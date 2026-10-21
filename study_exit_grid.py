"""
study_exit_grid.py — S29 (CEO 2026-10-17, order C). Study only: no config
or trading change.

    python study_exit_grid.py
    python study_exit_grid.py --s34 [--append]      # S34 target grid, below
    python study_exit_grid.py --s35 [--append]      # S35 pullback replay
    python study_exit_grid.py --s36 [--append]      # S36 catalyst vs rest
    python study_exit_grid.py --s44                 # S44 morning momentum

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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--s34", action="store_true")
    ap.add_argument("--s35", action="store_true")
    ap.add_argument("--s37", action="store_true")
    ap.add_argument("--s36", action="store_true")
    ap.add_argument("--s44", action="store_true")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--universe-file", default="universe_today.json")
    a = ap.parse_args()
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
