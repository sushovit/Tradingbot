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


# ============================================================ live vs shadow (S25)

def shadow_ladder_policy(params=None):
    """The owner ladder WITH 0.25% steps, as a shadow (never ordered)."""
    p = dict({"start_pct": 0.50, "cushion_pct": 0.50, "step_pct": 0.25},
             **(params or {}))

    def policy(entry, original_stop, close, highest_close):
        return pm.stepped_ladder_target(entry, close, highest_close, p)
    return policy


def _max_high(bars, start, end=None):
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    s = pd.Timestamp(start)
    s = s.tz_localize("America/New_York") if s.tzinfo is None else s
    mask = idx > s
    if end is not None:
        e = pd.Timestamp(end)
        e = e.tz_localize("America/New_York") if e.tzinfo is None else e
        mask &= idx <= e
    sub = bars[mask]
    return float(sub["high"].max()) if len(sub) else None


def shadow_vs_live(trades, bars_for, params=None):
    """For each CLOSED trade with a stop: the live result (journal) and the
    shadow owner-ladder result (5-minute replay), each with its best move
    (max high during the holding window). Returns
    {"live": {...}, "shadow": {...}} with pnl, trades, wins, win_pct,
    capture (realized / best move, over trades whose best move > 0)."""
    policy = shadow_ladder_policy(params)
    side = {k: {"pnl": 0.0, "trades": 0, "wins": 0, "best": 0.0}
            for k in ("live", "shadow")}
    for t in trades:
        if t.get("stop") is None or t.get("sell_ts") is None:
            continue
        try:
            bars = bars_for(t["ticker"], t["buy_ts"])
        except Exception:
            bars = None
        if bars is None or getattr(bars, "empty", True):
            continue
        entry, qty = t["buy"], t["qty"]
        live_pnl = t.get("pnl") if t.get("pnl") is not None else (t["sell"] - entry) * qty
        live_best = _max_high(bars, t["buy_ts"], t["sell_ts"])
        px, ts, _ = replay(bars, t["buy_ts"], entry, float(t["stop"]),
                           t.get("target"), policy)
        if px is None:
            continue
        shadow_pnl = (px - entry) * qty
        shadow_best = _max_high(bars, t["buy_ts"], ts)
        for key, pnl, best in (("live", live_pnl, live_best),
                               ("shadow", shadow_pnl, shadow_best)):
            side[key]["pnl"] += pnl
            side[key]["trades"] += 1
            side[key]["wins"] += 1 if pnl > 0 else 0
            if best is not None and best > entry:
                side[key]["best"] += (best - entry) * qty
    for v in side.values():
        v["win_pct"] = 100 * v["wins"] / v["trades"] if v["trades"] else None
        v["capture"] = v["pnl"] / v["best"] if v["best"] > 0 else None
    return side



# ============================================================ live R/ATR rule (scoreboard)

def replay_r_atr(bars, entry_ts, entry, stop, target, params, atr=None,
                 exit_params=None, earnings_soon=None):
    """The CURRENT live daily rule's stop ladder (S25 R/ATR, S29 +1R -> BE),
    replayed on 5-minute bars with the same fill rules as `replay`, so the
    scoreboard compares rules with rules. Steps move on COMPLETED closes of
    bars starting at or after `not_before_et` (10:00 ET); beyond the last step
    the stop trails the highest completed DAILY close - trail_atr x ATR14
    (`atr`, as of the entry day). Never lowered; at least market_gap_pct
    below the close. (exit_price, exit_ts, reason).

    CEO 2026-10-31: with `exit_params` (position_mgmt.daily_exit_params) the
    live daily exits are replayed too, exactly as the worker decides them
    (position_mgmt.daily_exit_due): the time stop (day 5 with MFE < +0.5R,
    day 10 regardless) at the first bar at/after exit_at_et, and the
    earnings exit at/after earnings_exit_at_et on a day `earnings_soon(date)`
    returns True (None = calendar unknown: no exit, as live). The exit fills
    at that bar's open."""
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et = idx.tz_convert("America/New_York")
    start = pd.Timestamp(entry_ts)
    if start.tzinfo is None:
        start = start.tz_localize("America/New_York")
    keep = idx > start
    after, et_after = bars[keep], et[keep]
    if after.empty:
        return None, None, "no bars"
    hh, mm = (int(x) for x in str(params.get("not_before_et", "10:00")).split(":"))
    live_stop, hc, first = stop, None, True
    day_close, highest_daily = {}, None
    entry_date, max_high, soon_cache = start.tz_convert("America/New_York").date(), None, {}
    if exit_params:
        eh, em = (int(x) for x in str(exit_params["earnings_exit_at_et"]).split(":"))
    for (ts, bar), t_et in zip(after.iterrows(), et_after):
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        d = t_et.date()
        prior = [v for k, v in day_close.items() if k < d]
        highest_daily = max(prior) if prior else None
        if not first and o <= live_stop:
            return o, ts, "stop (gap)"
        if not first and target is not None and o >= target:
            return o, ts, "target (gap)"
        if exit_params and not first:
            soon = None
            if earnings_soon is not None and (t_et.hour, t_et.minute) >= (eh, em):
                if d not in soon_cache:
                    soon_cache[d] = earnings_soon(d)
                soon = soon_cache[d]
            mfe_r = (None if max_high is None or entry <= stop
                     else (max_high - entry) / (entry - stop))
            why = pm.daily_exit_due(pm.session_day(entry_date, d), mfe_r,
                                    t_et.to_pydatetime(), soon, exit_params)
            if why:
                return o, ts, why
        first = False
        max_high = h if max_high is None else max(max_high, h)
        if l <= live_stop:
            return live_stop, ts, "stop"
        if target is not None and h >= target:
            return target, ts, "target"
        day_close[d] = c                         # last close seen for the day
        if (t_et.hour, t_et.minute) >= (hh, mm):
            hc = c if hc is None else max(hc, c)
            cand, _ = pm.r_atr_target(entry, stop, hc, highest_daily, atr, c, params)
            if cand is not None and live_stop < cand < c:
                live_stop = cand
    return float(after["close"].iloc[-1]), None, "open"
