"""
watchdog.py — restart the worker when it dies without releasing its lock.

    python watchdog.py [--dry-run]

Scheduled every 15 minutes during market hours (19:00-02:00 Nepal) AND at
boot, because the failure mode this exists for is the machine itself dying:
four unclean shutdowns in 48h (2026-07-28/29, Kernel-Power 41 / EventLog
6008), one of which killed the worker mid-write at 08:07 Nepal and left
bot.run behind with no process.

Logic: if the lock file exists but the heartbeat is older than 5 minutes,
kill any stale run_worker process, clear the lock, relaunch run_worker.py,
and post ONE Discord alert. No lock = the desk was stopped deliberately;
the watchdog does nothing.
"""

import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()

LOCK_FILE = "bot.run"
STATUS_FILE = "bot_status.log"
WORKER_SCRIPT = "run_worker.py"
PYTHON = os.path.join("tradingbot", "Scripts", "python.exe")
STALE_SECS = 300                 # 5 minutes
# A worker needs ~90s to reach its first cycle (universe scan, Ollama
# and Anthropic pre-flights, SPY regime). Below this age a live owner
# PID is starting up, not wedged.
STARTUP_GRACE_SECS = 300
ALERT_MARKER = ".watchdog_alert"  # suppresses repeat alerts for one restart


def heartbeat_age(status_file: str = STATUS_FILE):
    """Seconds since the worker last wrote status, or None if never."""
    try:
        return time.time() - os.path.getmtime(status_file)
    except OSError:
        return None


def lock_age(lock_file: str = LOCK_FILE):
    """Seconds since the lock file was written, or None if there is none.

    write_lock() is called ONCE at startup and never refreshed, so this is
    the owning process's age. That is what makes it usable as a startup
    grace window: it expires on its own."""
    try:
        return time.time() - os.path.getmtime(lock_file)
    except OSError:
        return None


def needs_restart(lock_exists: bool, age_secs, stale_secs: int = STALE_SECS,
                  lock_pid_alive: bool = None, lock_age_secs=None,
                  startup_grace_secs: int = STARTUP_GRACE_SECS):
    """Pure decision function. Returns (restart: bool, reason: str).

    STARTUP GRACE (2026-09-21). A worker takes about 90 seconds to reach its
    first cycle. Before this, the watchdog read `lock exists` + `heartbeat
    ancient` and restarted two healthy workers that had just been started,
    because the heartbeat it was reading was the PREVIOUS session's -- 235k
    seconds old, from Friday. The relaunch then inherited the same window,
    so the failure repeated instead of resolving.

    A live owner PID beside a lock file younger than the grace window is a
    worker that is STARTING, not one that is wedged. Both conditions are
    required: the PID proves something is actually there, and the lock age
    bounds the exemption so a genuinely hung worker is still killed once the
    window passes."""
    if not lock_exists:
        return False, "no lock file — desk is stopped deliberately"
    if (lock_pid_alive and lock_age_secs is not None
            and lock_age_secs < startup_grace_secs):
        return False, (f"starting up (owner PID alive, lock "
                       f"{int(lock_age_secs)}s old < {startup_grace_secs}s "
                       f"grace)")
    if age_secs is None:
        return True, "lock present but no status file at all"
    if age_secs > stale_secs:
        return True, f"heartbeat stale ({int(age_secs)}s > {stale_secs}s)"
    return False, f"healthy (heartbeat {int(age_secs)}s old)"


def lock_owner_pid():
    """The PID recorded in the lock file — the precise kill target."""
    import run_worker
    return run_worker.read_lock_pid(LOCK_FILE)


def find_worker_pids() -> list:
    """PIDs of running run_worker.py processes. Used as a FALLBACK when the
    lock records no PID (legacy lock) and as a survivor check after the
    targeted kill."""
    pids = []
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "processid,commandline", "/format:csv"],
            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if WORKER_SCRIPT in line:
                parts = [p for p in line.strip().split(",") if p]
                if parts and parts[-1].isdigit():
                    pids.append(int(parts[-1]))
    except Exception:
        pass
    return pids


def kill_stale_workers() -> int:
    """Kill the RECORDED owner by PID first (precise), then sweep for any
    straggler that still matches run_worker.py."""
    import run_worker
    killed = 0
    owner = lock_owner_pid()
    if owner:
        print(f"killing recorded owner PID {owner}")
        if run_worker.kill_pid(owner):
            killed += 1
        else:
            print(f"WARNING: owner PID {owner} survived the kill")
    for pid in find_worker_pids():
        if pid == owner:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=30)
            killed += 1
        except Exception:
            pass
    return killed


def relaunch_worker() -> bool:
    """Start run_worker.py detached. It creates its own lock file."""
    try:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # --force-takeover is safe here: main() has already CONFIRMED no
        # worker survives, so the heartbeat guard would be a false block.
        subprocess.Popen([PYTHON, WORKER_SCRIPT, "--force-takeover"],
                         cwd=os.getcwd(), creationflags=creation)
        return True
    except Exception as e:
        print(f"relaunch failed: {e}")
        return False


def post_alert(message: str):
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured)")
        return
    try:
        from discord_webhook import DiscordWebhook
        DiscordWebhook(url=url, content=message[:1900]).execute()
        print("Discord alerted.")
    except Exception as e:
        print(f"(alert failed: {e})")


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    # The worker shuts itself down at session end. Outside the session
    # window there is nothing that SHOULD be running, so the watchdog must
    # never resurrect it — that would be the "service running behind" the
    # shutdown exists to prevent.
    try:
        import json as _json
        import session_clock
        try:
            with open("bot_config.json") as f:
                cfg = _json.load(f)
        except (OSError, ValueError):
            cfg = {}
        if not session_clock.in_session_window(cfg):
            print("watchdog: outside the session window — nothing should be "
                  "running; no action.")
            return 0
    except Exception as e:
        print(f"watchdog: session-window check failed ({e}); continuing.")

    import run_worker

    lock_exists = os.path.exists(LOCK_FILE)
    age = heartbeat_age()
    owner = lock_owner_pid()
    owner_alive = bool(owner) and run_worker.pid_alive(owner)
    lock_secs = lock_age()
    restart, reason = needs_restart(lock_exists, age,
                                    lock_pid_alive=owner_alive,
                                    lock_age_secs=lock_secs)

    print(f"watchdog: lock={lock_exists} lock_age="
          f"{int(lock_secs) if lock_secs is not None else 'n/a'}s "
          f"owner={owner or 'none'} alive={owner_alive} heartbeat_age="
          f"{int(age) if age is not None else 'n/a'}s -> {reason}")
    if not restart:
        if os.path.exists(ALERT_MARKER):
            os.remove(ALERT_MARKER)          # healthy again: re-arm alerts
        return 0
    if dry_run:
        print("(dry run — no action taken)")
        return 0

    # KILL-THEN-LAUNCH, never launch-beside (2026-07-29 duplicate-worker
    # incident): every existing worker dies and the kill is CONFIRMED before
    # a new one starts.
    killed = kill_stale_workers()
    for _ in range(10):
        if not find_worker_pids():
            break
        time.sleep(1)
    else:
        print("ABORT: worker processes survived the kill — refusing to launch "
              "a second instance beside them.")
        post_alert("⚠️ Watchdog could not kill the stale worker — NOT "
                   "launching a second instance. Manual intervention needed.")
        return 1

    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError as e:
        print(f"could not clear lock: {e}")
        return 1

    launched = relaunch_worker()
    import clockline
    msg = (f"🔁 Worker restarted by watchdog — {reason}. "
           f"Killed {killed} stale process(es); relaunch "
           f"{'OK' if launched else 'FAILED'}.\n{clockline.two_zone_line()}")
    print(msg)
    if not os.path.exists(ALERT_MARKER):     # one alert per outage
        post_alert(msg)
        try:
            with open(ALERT_MARKER, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
