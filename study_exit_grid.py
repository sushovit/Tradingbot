"""
study_exit_grid.py — S29 (CEO 2026-10-17, order C). Study only: no config
or trading change.

    python study_exit_grid.py

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


def main():
    results, n = run()
    print("\n".join(render(results, n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
