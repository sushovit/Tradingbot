"""
jobs/awake_week.py — CEO 2026-10-21: keep the Mac awake for the whole
trading week, with no sudo and no manual step.

launchd (com.tradingbot.awake_week) starts this at Sun 20:00 ET and at every
load (RunAtLoad: a reboot or a re-login restarts it). It replaces itself
with `caffeinate -i -t <seconds left until Fri 16:30 ET>`, so the hold ends
on its own at Friday 16:30 whatever time it started. Outside the window
(Fri 16:30 -> Sun 20:00) it exits 0 and holds nothing.

`-i` holds off idle system sleep. It cannot stop a lid-close sleep: the
lid-open gate stays.
"""

import os
import shutil
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
START = (6, 20, 0)        # Sunday (Mon=0 .. Sun=6), 20:00
END = (4, 16, 30)         # Friday, 16:30


def week_end(now):
    """The Friday 16:30 ET that closes the window `now` is in, or None if
    `now` is outside the Sun 20:00 -> Fri 16:30 window."""
    now = now.astimezone(ET)
    day, t = now.weekday(), (now.hour, now.minute)
    if day == 5 or (day == 6 and t < START[1:]) or (day == 4 and t >= END[1:]):
        return None
    days_ahead = (END[0] - day) % 7
    if day == 6:
        days_ahead = 5
    end = (now + timedelta(days=days_ahead)).replace(
        hour=END[1], minute=END[2], second=0, microsecond=0)
    return end


def seconds_left(now=None):
    now = now or datetime.now(ET)
    end = week_end(now)
    if end is None:
        return 0
    return max(0, int(end.timestamp() - now.timestamp()))     # DST-safe


def main() -> int:
    secs = seconds_left()
    now = datetime.now(ET)
    if secs < 60:
        print(f"{now:%a %Y-%m-%d %H:%M %Z}: outside Sun 20:00-Fri 16:30 ET; "
              f"nothing to hold.", flush=True)
        return 0
    caffeinate = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    print(f"{now:%a %Y-%m-%d %H:%M %Z}: holding the Mac awake for {secs}s "
          f"(until Fri 16:30 ET).", flush=True)
    os.execv(caffeinate, [caffeinate, "-i", "-t", str(secs)])
    return 0                     # not reached


if __name__ == "__main__":
    sys.exit(main())
