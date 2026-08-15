"""
session_clock.py — when the desk is open, and when it shuts itself off.

The worker is started by hand from the terminal and ends its own session at
`session_end_et` (default 16:15 ET = 02:00 Nepal, fifteen minutes after the
close). It releases its lock on the way out, so nothing is left "running
behind" and the watchdog has nothing to resurrect.
"""

from datetime import datetime, time as dtime

import pytz

EASTERN_TZ = pytz.timezone("US/Eastern")
DEFAULT_SESSION_END = "16:15"          # ET — 02:00 Nepal


def session_end_time(config: dict) -> dtime:
    """Parsed session_end_et from config, falling back to 16:15 ET."""
    raw = str((config or {}).get("session_end_et", DEFAULT_SESSION_END))
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        hh, mm = DEFAULT_SESSION_END.split(":")
        return dtime(int(hh), int(mm))


def session_over(config: dict, now_et: datetime = None) -> bool:
    """True once the trading session has ended for the day.

    Weekends count as over: a worker started on a Saturday has no session
    to wait for and should not idle. On a weekday it is over only after
    session_end_et — before the open the worker waits, because the session
    is still ahead of it."""
    now_et = now_et or datetime.now(EASTERN_TZ)
    if now_et.weekday() >= 5:
        return True
    return now_et.time() >= session_end_time(config)


def in_session_window(config: dict, now_et: datetime = None) -> bool:
    """True while a worker is SUPPOSED to exist — the watchdog only restarts
    inside this window, so it can never resurrect a worker that shut itself
    off for the night."""
    now_et = now_et or datetime.now(EASTERN_TZ)
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 25) <= now_et.time() < session_end_time(config)


def nepal_str(config: dict) -> str:
    """The shutdown time in Nepal terms, for human-facing messages."""
    from zoneinfo import ZoneInfo
    end = session_end_time(config)
    today = datetime.now(EASTERN_TZ).replace(hour=end.hour, minute=end.minute,
                                             second=0, microsecond=0)
    return today.astimezone(ZoneInfo("Asia/Kathmandu")).strftime("%H:%M")
