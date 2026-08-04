"""run_worker.py — start the live bot worker as a standalone process.

Equivalent to the dashboard's Start button, but headless: creates the run
lock and runs live_bot_worker() in THIS process.

SINGLETON GUARD (2026-07-29): two instances ran side by side and interleaved
cycle numbers (#88 and #98 in one status file). Startup now REFUSES to launch
if a live heartbeat exists — a status file written in the last 60 seconds
means another worker is alive. Use --force only after killing the other one.
"""

import os
import sys
import time

HEARTBEAT_FRESH_SECS = 60
STATUS_FILE = "bot_status.log"
LOCK_FILE = "bot.run"


def live_heartbeat_age():
    """Seconds since the last status write, or None if there is no file."""
    try:
        return time.time() - os.path.getmtime(STATUS_FILE)
    except OSError:
        return None


def another_worker_is_alive(fresh_secs: int = HEARTBEAT_FRESH_SECS) -> bool:
    """A heartbeat younger than fresh_secs means a worker is running now."""
    age = live_heartbeat_age()
    return age is not None and age < fresh_secs


def main() -> int:
    force = "--force" in sys.argv
    if another_worker_is_alive() and not force:
        age = int(live_heartbeat_age())
        print(f"REFUSING TO START: a live worker heartbeat is {age}s old "
              f"(< {HEARTBEAT_FRESH_SECS}s) — another instance is running.\n"
              f"Kill it first (or use --force if you are certain it is dead).")
        return 1

    with open(LOCK_FILE, "w"):
        pass

    import streamlit_app  # noqa: E402  (bare-mode import)
    streamlit_app.live_bot_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())
