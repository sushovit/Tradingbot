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


def local_str(config: dict) -> str:
    """The shutdown time in the MACHINE's local timezone.

    This used to hardcode Asia/Kathmandu, which is only right for one desk
    and silently lies on any other machine (or after a move). astimezone()
    with no argument uses whatever the host is actually set to."""
    end = session_end_time(config)
    today = datetime.now(EASTERN_TZ).replace(hour=end.hour, minute=end.minute,
                                             second=0, microsecond=0)
    return today.astimezone().strftime("%H:%M %Z")


def nepal_str(config: dict) -> str:
    """Deprecated alias — kept so older callers keep working."""
    return local_str(config)


# --------------------------------------------------------------- power

SUSPEND_GAP_SECS = 300      # a cycle gap larger than this is a suspend


def keep_awake(enable: bool = True) -> bool:
    """Ask Windows not to sleep while the desk is trading.

    2026-08-31: the machine slept at 12:36 ET mid-session (Kernel-Power 42)
    and resumed at 21:06 ET. The worker was frozen, not hung; the watchdog
    was frozen too, so nothing restarted; and the auto-shutdown fired the
    instant it resumed, five hours "late" purely because wall-clock time
    had moved on without it. Preventing the sleep is the actual fix.

    Returns True if the request was accepted (Windows only; a no-op
    elsewhere)."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False          # non-Windows or blocked: not fatal


def suspend_gap(previous_wallclock: float, now_wallclock: float,
                expected_secs: float, threshold: float = SUSPEND_GAP_SECS):
    """Seconds of unexplained wall-clock loss between two cycles, or None.

    A cycle sleeps ~30s. If far more time passed, the process was suspended
    (machine sleep) — that must be reported as a SUSPEND, not mistaken for a
    hang, because the two have opposite remedies."""
    if not previous_wallclock:
        return None
    gap = now_wallclock - previous_wallclock - expected_secs
    return gap if gap > threshold else None
