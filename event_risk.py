"""
event_risk.py — CEO 2026-10-31 (agenda 39, from the report "Buying the
October 2026 tech dip"). Pure functions; the worker supplies the data.

1. Event-window sizing: bot_config `event_risk_windows` -
   [{start, end, risk_per_trade_pct, why}]; an entry dated inside a window
   risks min(base, window) - a window never RAISES risk.
2. Earnings-gap cooldown: bot_config `earnings_gap_cooldown` - a session that
   OPENS <= gap_pct below the prior close within one session of the stock's
   own earnings report (report dated that session or the one before: bmo or
   amc) blocks new entries for `block_sessions` sessions (the gap session is
   session 1); for the next `reclaim_only_sessions` sessions only
   mean_reversion_reclaim may enter. If the calendar is unknown, a qualifying
   price gap is treated as an earnings gap (the safe direction).
3. Gatekeeper context: the stock's group(s) and the scheduled events
   (bot_config `scheduled_events`) inside the hold.
     gap            - own earnings within the last 3 sessions and a >= 3% gap
     sympathy       - a >= 3% gap within the last 3 sessions WITHOUT own
                      earnings (moved with its sector / peers / news)
     earnings-ahead - own earnings inside the hold (max_hold_days sessions)
"""

import datetime

RECLAIM = "mean_reversion_reclaim"
GAP_COOLDOWN = {"enabled": True, "gap_pct": -8.0, "block_sessions": 3,
                "reclaim_only_sessions": 7}
GROUP_GAP_PCT, GROUP_LOOKBACK = 3.0, 3


def _d(x) -> datetime.date:
    if isinstance(x, datetime.datetime):
        return x.date()
    if isinstance(x, datetime.date):
        return x
    return datetime.date.fromisoformat(str(x)[:10])


# ===================================================================== 1. sizing

def event_window(config: dict, day):
    """The event-risk window covering `day`, or None."""
    day = _d(day)
    for w in (config or {}).get("event_risk_windows") or []:
        try:
            if _d(w["start"]) <= day <= _d(w["end"]):
                return w
        except (KeyError, ValueError, TypeError):
            continue
    return None


def event_window_risk_pct(base_pct: float, config: dict, day) -> float:
    w = event_window(config, day)
    if w is None:
        return base_pct
    try:
        return min(float(base_pct), float(w["risk_per_trade_pct"]))
    except (KeyError, TypeError, ValueError):
        return base_pct


# ===================================================================== 2. cooldown

def cooldown_params(config: dict) -> dict:
    p = dict(GAP_COOLDOWN)
    p.update(((config or {}).get("earnings_gap_cooldown") or {}))
    return p


def _sessions(daily, today):
    """[(date, open, close)] of daily bars up to and including `today`."""
    out = []
    for i, row in daily.iterrows():
        d = _d(i)
        if d <= today:
            out.append((d, float(row["open"]), float(row["close"])))
    return sorted(out)


def recent_gaps(daily, today, lookback):
    """[(session_index_from_today (1 = today), date, gap_pct, prev_date)] for
    the last `lookback` sessions."""
    s = _sessions(daily, _d(today))
    out = []
    for k in range(max(1, len(s) - lookback), len(s)):
        d, o, _c = s[k]
        prev_d, _po, prev_c = s[k - 1]
        if prev_c > 0:
            out.append((len(s) - k, d, 100.0 * (o / prev_c - 1.0), prev_d))
    return out


def earnings_gap_state(daily, earnings_dates, today, config=None):
    """(status, gap_date, gap_pct, session_no): status None | 'cooldown' |
    'reclaim_only'. `earnings_dates` = the stock's report dates (iso), or None
    when the calendar is unknown."""
    p = cooldown_params(config)
    if not p.get("enabled", True) or daily is None or getattr(daily, "empty", True):
        return None, None, None, None
    span = int(p["block_sessions"]) + int(p["reclaim_only_sessions"])
    dates = None if earnings_dates is None else {str(x)[:10] for x in earnings_dates}
    for n, d, gap, prev_d in sorted(recent_gaps(daily, today, span), key=lambda g: g[0]):
        if gap > float(p["gap_pct"]):
            continue
        if dates is not None and d.isoformat() not in dates and prev_d.isoformat() not in dates:
            continue                                   # a gap, but not an earnings gap
        if n <= int(p["block_sessions"]):
            return "cooldown", d, round(gap, 2), n
        return "reclaim_only", d, round(gap, 2), n
    return None, None, None, None


def earnings_gap_block(setup_name: str, state) -> str:
    """Pass reason for this setup, or None."""
    status = state[0] if state else None
    if status == "cooldown":
        return "earnings_gap_cooldown"
    if status == "reclaim_only" and setup_name != RECLAIM:
        return "earnings_gap_reclaim_only"
    return None


# ===================================================================== 3. context

def stock_groups(daily, today, past_dates=None, upcoming_dates=None, hold_sessions=10):
    """Sorted group labels: gap / sympathy / earnings-ahead (may be empty)."""
    today = _d(today)
    groups = set()
    past = {str(x)[:10] for x in (past_dates or [])}
    if daily is not None and not getattr(daily, "empty", True):
        for n, d, gap, prev_d in recent_gaps(daily, today, GROUP_LOOKBACK):
            if abs(gap) >= GROUP_GAP_PCT:
                own = d.isoformat() in past or prev_d.isoformat() in past
                groups.add("gap" if own else "sympathy")
    horizon = add_sessions(today, hold_sessions)
    for x in upcoming_dates or []:
        if today <= _d(x) <= horizon:
            groups.add("earnings-ahead")
    return sorted(groups)


def add_sessions(day, n):
    d, left = _d(day), int(n)
    while left > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def events_in_hold(config, today, hold_sessions=10):
    """[(date, event)] of bot_config scheduled_events from today through the
    last session of the hold."""
    today = _d(today)
    end = add_sessions(today, hold_sessions)
    out = []
    for e in (config or {}).get("scheduled_events") or []:
        try:
            d = _d(e["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if today <= d <= end:
            out.append((d, str(e.get("event", ""))))
    return sorted(out)


def context_text(groups, events, own_earnings=None, risk_note=None) -> str:
    """The gatekeeper prompt's EVENT CONTEXT section (informational)."""
    lines = [f"- Group: {', '.join(groups) if groups else 'none (no recent gap, no earnings in the hold)'}"]
    if own_earnings:
        lines.append(f"- Own earnings inside the hold: {own_earnings}")
    if events:
        lines.append("- Scheduled market events inside the hold: "
                     + "; ".join(f"{d:%a %m-%d} {name}" for d, name in events))
    else:
        lines.append("- Scheduled market events inside the hold: none")
    if risk_note:
        lines.append(f"- {risk_note}")
    return "\n".join(lines)
