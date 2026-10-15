"""
lock_replay.py — S24b. What a closed daily trade would have made under a
different stop rule, replayed on 5-minute bars. Reporting only.

Two alternatives for the Discord weekly line:
  "cushion 0.25%" - the owner ladder with cushion_pct 0.25 instead of 0.50
  "old +1R rule"  - the S16 rule: stop to breakeven once a close is +1R

Same entry, original stop and 3R target as the real trade; the stop is
decided on each COMPLETED bar's close and applies from the next bar; a bar
opening through a level fills at its open; a bar spanning stop and target
hits the stop first. A trade neither rule has closed by the last bar is
valued at that bar's close and flagged open.
"""

import pandas as pd

import position_mgmt as pm


def ladder_policy(cushion_pct, start_pct=0.50):
    params = dict(pm.LADDER_DEFAULTS, cushion_pct=cushion_pct,
                  start_pct=start_pct)

    def policy(entry, original_stop, close, highest_close):
        return pm.ladder_target(entry, close, highest_close, None, params)[0]
    return policy


def plus_one_r_policy(entry, original_stop, close, highest_close):
    state = {"entry_price": entry, "original_stop": original_stop}
    return pm.profit_lock_target(state, close, ((1.0, 0.0),))[0]


def replay(bars, entry_ts, entry, stop, target, policy):
    """(exit_price, exit_ts, reason). exit_ts None = still open at the end."""
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    start = pd.Timestamp(entry_ts)
    if start.tzinfo is None:
        start = start.tz_localize("America/New_York")
    after = bars[idx > start]
    if after.empty:
        return None, None, "no bars"
    live_stop, hc, first = stop, entry, True
    for ts, bar in after.iterrows():
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        if not first and o <= live_stop:
            return o, ts, "stop (gap)"
        if not first and target is not None and o >= target:
            return o, ts, "target (gap)"
        first = False
        if l <= live_stop:
            return live_stop, ts, "stop"
        if target is not None and h >= target:
            return target, ts, "target"
        hc = max(hc, c)
        candidate = policy(entry, stop, c, hc)
        if candidate is not None and live_stop < candidate < c:
            live_stop = candidate
    return float(after["close"].iloc[-1]), None, "open"


def alternatives(trades, bars_for):
    """{label: (total P&L, n compared, n still open)} over closed daily
    trades with a known stop. `bars_for(ticker, since)` returns 5-minute
    bars or None."""
    rules = {"cushion 0.25%": ladder_policy(0.25), "old +1R rule": plus_one_r_policy}
    out = {label: [0.0, 0, 0] for label in rules}
    for t in trades:
        if t.get("stop") is None or t.get("sell_ts") is None:
            continue
        try:
            bars = bars_for(t["ticker"], t["buy_ts"])
        except Exception:
            bars = None
        if bars is None or getattr(bars, "empty", True):
            continue
        for label, policy in rules.items():
            px, ts, how = replay(bars, t["buy_ts"], t["buy"], float(t["stop"]),
                                 t.get("target"), policy)
            if px is None:
                continue
            out[label][0] += (px - t["buy"]) * t["qty"]
            out[label][1] += 1
            out[label][2] += 1 if ts is None else 0
    return {k: tuple(v) for k, v in out.items()}
