"""
dip_live.py — the LIVE "dip_probation" setup (owner override of the rule
freeze, CEO-approved 2026-11-01; live from 2026-11-02). The S55 paper test's
rule, traded small, with its own exits and a kill switch.

Entry (strategies/dip_probation.py calls detect()):
  * universe price (prior close) >= $50; today's open within +-1% of the
    prior close; no earnings in the next 3 sessions (Finnhub; unknown
    calendar -> no entry); watch-only/catalyst names, the earnings-gap
    cooldown and the sector cap apply downstream in the worker;
  * the FIRST completed 5-minute bar starting before 11:00 ET whose low <=
    prior close x 0.98 -> a market buy on the next cycle (the touch bar must
    be one of the last two completed bars, or the entry is missed);
  * a SEPARATE book (CEO 2026-11-03): max 3 dip positions open, max 1 dip
    entry per day; dip positions do not count toward the main book's
    max_positions or its sector cap (the daily loss limit counts both).
Bracket: target = the prior close (limit); stop = entry - 2.0 x daily ATR14
(re-anchored to the broker fill once confirmed). Time exit at 15:55 ET on
session 3 (the entry session is session 1). No ladder, no trail, no target
cap. Risk 0.5% ($25 on $5,000), 25% notional cap. Rules only: the Claude and
shadow verdicts are logged as advisory and never veto.
Kill switch: cumulative realized P&L <= -$75 or 5 straight losses -> the
setup stops entering (Discord + ops event, once).
"""

import math
from datetime import datetime, time as dtime, timedelta

DIP = "dip_probation"
DEFAULTS = {"enabled": True, "start": "2026-11-02", "risk_per_trade_pct": 0.5,
            "min_price": 50.0, "open_band_pct": 1.0, "dip_pct": 2.0,
            "trigger_before_et": "11:00", "fresh_bars": 2, "stop_atr": 2.0,
            "hold_sessions": 3, "earnings_block_sessions": 3, "max_open": 3,
            "max_entries_per_day": 1, "kill_pnl_usd": -75.0, "kill_streak": 5,
            "spy_gate": False}
EXIT_REASON = "Time exit (session 3)"


def params(config) -> dict:
    p = dict(DEFAULTS)
    p.update((config or {}).get(DIP) or {})
    return p


def _hm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def floor2(x):
    return math.floor(x * 100 + 1e-6) / 100


def add_sessions(day, n):
    d, left = day, int(n)
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


# ===================================================================== gates

def kill_state(closed_pnls, cfg=None):
    """(killed, cumulative $, losing streak, reason) from the dip trades'
    realized P&L in exit order."""
    p = params(cfg)
    cum = round(sum(closed_pnls), 2)
    streak = 0
    for x in reversed(closed_pnls):
        if x < 0:
            streak += 1
        else:
            break
    if cum <= float(p["kill_pnl_usd"]):
        return True, cum, streak, f"cumulative P&L ${cum:+,.2f} <= ${float(p['kill_pnl_usd']):,.0f}"
    if streak >= int(p["kill_streak"]):
        return True, cum, streak, f"{streak} straight losses"
    return False, cum, streak, None


def cycle_gate(today, positions, dip_buys_today, closed_pnls, config):
    """Reason the dip setup may NOT enter this cycle, or None."""
    p = params(config)
    if not p.get("enabled", True):
        return "disabled"
    if today.isoformat() < str(p["start"]):
        return "not_started"
    killed, _cum, _st, why = kill_state(closed_pnls, config)
    if killed:
        return f"killed: {why}"
    open_n = sum(1 for s in (positions or {}).values()
                 if s.get("in_position") and s.get("setup") == DIP)
    if open_n >= int(p["max_open"]):
        return "dip_position_open"
    if dip_buys_today >= int(p["max_entries_per_day"]):
        return "dip_entry_done_today"
    return None


DEAD_STATES = {"canceled", "cancelled", "rejected", "expired", "done_for_day", "replaced"}


def _status(o) -> str:
    return str(getattr(o, "status", "")).lower().split(".")[-1]


def presubmit_block(positions, journal_dip_buys_today, broker_orders_today, config=None):
    """The LAST check before a dip order is submitted (2026-11-02 bug: ABBV,
    PSX and ZS bought in one cycle). Counts, at that moment: open dip
    positions, today's dip BUYs in the journal, and today's dip-tagged orders
    at the broker (client_order_id "dip-..."), pending ones included.
    `broker_orders_today` None = the broker could not be read -> block."""
    p = params(config)
    if broker_orders_today is None:
        return "dip_check_unavailable"
    open_n = sum(1 for s in (positions or {}).values()
                 if s.get("in_position") and s.get("setup") == DIP)
    dip_orders = [o for o in broker_orders_today
                  if str(getattr(o, "client_order_id", "") or "").startswith("dip-")
                  and "buy" in str(getattr(o, "side", "")).lower()
                  and _status(o) not in DEAD_STATES]
    pending = [o for o in dip_orders if _status(o) != "filled"]
    if open_n >= int(p["max_open"]):
        return "dip_position_open"
    if pending:
        return "dip_order_pending"
    if max(int(journal_dip_buys_today), len(dip_orders)) >= int(p["max_entries_per_day"]):
        return "dip_entry_done_today"
    return None


def client_order_id(ticker, now) -> str:
    return f"dip-{ticker}-{now:%Y%m%d%H%M%S}"[:48]


# ===================================================================== detect

def _et(ts, tz="America/New_York"):
    import pandas as pd
    t = pd.Timestamp(ts)
    return (t.tz_localize(tz) if t.tzinfo is None else t.tz_convert(tz))


def prior_close_and_atr(daily, today):
    """(prior close, ATR14 as of the last completed day) or (None, None)."""
    import position_mgmt as pm
    if daily is None or getattr(daily, "empty", True):
        return None, None
    past = daily[[str(i)[:10] < today.isoformat() for i in daily.index]]
    if past.empty:
        return None, None
    return float(past["close"].iloc[-1]), pm.daily_atr(daily, 14, today)


def detect(df, context):
    """Signal, Rejection or None (see the module doc)."""
    from strategies.base import Signal, Rejection, would_be
    if context.get("dip_gate"):
        return None
    p = params(context.get("config"))
    ticker = context["ticker"]
    now = _et(context.get("now_et") or datetime.now())
    today = now.date()
    if df is None or getattr(df, "empty", True):
        return None
    idx = [_et(i) for i in df.index]
    bars = [(t, row) for t, (_i, row) in zip(idx, df.iterrows())
            if t.date() == today and dtime(9, 30) <= t.time() < dtime(16, 0)]
    if not bars:
        return None
    completed = [(t, r) for t, r in bars if t + timedelta(minutes=5) <= now]
    pc, atr = prior_close_and_atr(context.get("daily"), today)
    if pc is None:
        return None
    level = pc * (1.0 - float(p["dip_pct"]) / 100.0)
    cut = dtime(*_hm(p["trigger_before_et"]))
    touch = None
    for k, (t, r) in enumerate(completed):
        if t.time() >= cut:
            break
        if float(r["low"]) <= level:
            touch = (k, t)
            break
    if touch is None:
        return None
    if touch[0] < len(completed) - int(p["fresh_bars"]):
        return None                                   # missed: the first touch is stale
    day_open = float(bars[0][1]["open"])
    entry = float(context.get("current_price") or float(bars[-1][1]["close"]))
    stop = floor2(entry - float(p["stop_atr"]) * atr) if atr else None
    target = round(pc, 2)
    geo = would_be(entry, stop, target) if stop else None

    def reject(name, details):
        return Rejection(DIP, ticker, name, details, geometry=geo)
    if pc < float(p["min_price"]):
        return reject("dip_price_floor", f"prior close {pc:.2f} < ${float(p['min_price']):g}")
    if abs(day_open / pc - 1.0) * 100.0 > float(p["open_band_pct"]) + 1e-9:
        return reject("dip_open_outside_band",
                      f"open {day_open:.2f} vs prior close {pc:.2f} "
                      f"({(day_open / pc - 1) * 100:+.2f}%)")
    if not atr:
        return reject("dip_no_atr", "daily ATR14 unavailable")
    end = add_sessions(today, int(p["earnings_block_sessions"]))
    dates_for = context.get("earnings_dates")
    if dates_for is None:
        import earnings
        dates_for = earnings.dates_for
    upcoming = dates_for(ticker, today, end)
    if upcoming is None:
        return reject("dip_earnings_unknown", "earnings calendar unavailable - no entry")
    ahead = [d for d in upcoming if today.isoformat() <= str(d)[:10] <= end.isoformat()]
    if ahead:
        return reject("dip_earnings_ahead", f"reports {ahead[0]} (within "
                                            f"{int(p['earnings_block_sessions'])} sessions)")
    if not (stop < entry < target - 0.01):
        return reject("dip_geometry", f"entry {entry:.2f} stop {stop} target {target:.2f}")
    return Signal(DIP, ticker, entry=round(entry, 4), stop=stop, target=target,
                  confidence_hint="low",
                  reasoning=(f"Dip probation: first touch of prior close -{float(p['dip_pct']):g}% "
                             f"({level:.2f}) at {touch[1]:%H:%M}; open {day_open:.2f} within "
                             f"+-{float(p['open_band_pct']):g}% of {pc:.2f}; target = prior close"),
                  extras={"atr14": atr, "prior_close": pc, "dip_level": round(level, 4),
                          "touch_bar": touch[1].isoformat(),
                          "max_hold_sessions": int(p["hold_sessions"]) - 1,
                          "hard_exit_reason": EXIT_REASON,
                          "structural_stop": stop})


def reanchor_stop(entry_fill, atr, cfg=None):
    """The dip stop from the broker fill: fill - 2.0 x ATR14."""
    return floor2(float(entry_fill) - float(params(cfg)["stop_atr"]) * float(atr))


# ===================================================================== reporting

def dip_trades(conn):
    """The account's dip_probation trades (trade_table rows), oldest first."""
    import trade_table
    return [t for t in trade_table.load_trades(conn) if t.get("setup") == DIP]


def summary_lines(trades, config=None):
    """The live dip probation, reported on its own (scoreboard, 16:20 dip
    post, Saturday report)."""
    closed = [t for t in trades if t["sell"] is not None and t["reason"] != "partial"]
    pnls = [float(t["pnl"] or 0) for t in sorted(closed, key=lambda t: t["sell_ts"] or "")]
    killed, cum, streak, why = kill_state(pnls, config)
    p = params(config)
    wins = sum(1 for x in pnls if x > 0)
    head = (f"🧪 DIP probation (LIVE, rules only, $25 risk): {len(trades)} trade(s), "
            f"{len(closed)} closed, {wins} won · P&L ${cum:+,.2f}")
    kill = (f"KILLED - {why}" if killed else
            f"kill at ${float(p['kill_pnl_usd']):,.0f} or {int(p['kill_streak'])} straight losses "
            f"(now ${cum:+,.2f}, streak {streak})")
    lines = [head, f"  {kill}"]
    for t in trades[-6:]:
        res = ("open" if t["sell"] is None else
               f"{t['reason']} {float(t['pnl'] or 0):+,.2f}")
        lines.append(f"  {t['ticker']} {str(t['buy_ts'])[5:16]} @ {float(t['buy']):.2f} → {res}")
    return lines
