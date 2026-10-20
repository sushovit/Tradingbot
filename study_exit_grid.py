"""
study_exit_grid.py — S29 (CEO 2026-10-17, order C). Study only: no config
or trading change.

    python study_exit_grid.py
    python study_exit_grid.py --s34 [--append]      # S34 target grid, below

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


def s34_signals(symbols, cache=S34_CACHE, start=S34_START):
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
        for setup in S34_SETUPS:
            sigs, _ = backtest.collect_signals(sym, daily, setup, regime, profile, config)
            for sig in sigs:
                day = pd.Timestamp(daily.index[sig["entry_i"]]).tz_localize(ET)
                if day.date() < lo:
                    continue
                if setup not in S34_SPY_EXEMPT and sig["regime"] != "trending":
                    continue
                signals.append((setup, sym, day, float(sig["entry"]), float(sig["stop"])))
    return signals, frames


def s34_run(signals, frames, steps=None):
    params = dict(pm.r_atr_params(_live_profile()))
    steps = [tuple(x) for x in (steps or params["steps"])]
    cap = pm.target_cap_atr(_live_config())
    results = {}
    for label, k, half in s34_variants():
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
            trades.append({"setup": setup, "r": r, "mfe_r": mfe, "why": why})
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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--s34", action="store_true")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--universe-file", default="universe_today.json")
    a = ap.parse_args()
    if a.s34:
        return s34_main(a.append, a.universe_file)
    results, n = run()
    print("\n".join(render(results, n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
