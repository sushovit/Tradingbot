"""run_worker.py — start the live bot worker as a standalone process.

SINGLE-INSTANCE ENFORCEMENT (2026-08-10 priority override):

  1. The heartbeat is read BEFORE anything starts. A heartbeat fresher
     than 60s means another worker is alive -> print and EXIT non-zero.
     The only way past it is --force-takeover, which KILLS the recorded
     owner first and verifies the kill before proceeding.
  2. The lock file holds the owning process's PID. The watchdog and any
     takeover kill THAT pid — no guessing at python processes.
  3. Status/heartbeat writes are atomic (safe_io: temp + os.replace), so
     two processes can never interleave into a corrupted file (the
     null-bytes incident of 2026-07-29).
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import time

HEARTBEAT_FRESH_SECS = 60
LOG_DIR = "logs"
WORKER_LOG = os.path.join(LOG_DIR, "worker.log")
LOG_MAX_BYTES = 5 * 1024 * 1024        # 5 MB
LOG_BACKUPS = 5
LOG_FORMAT = "[%(asctime)s] %(levelname)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
STATUS_FILE = "bot_status.log"
LOCK_FILE = "bot.run"
KILL_WAIT_SECS = 15


# ----------------------------------------------------------- logging

def setup_logging(log_file: str = WORKER_LOG) -> logging.Logger:
    """Attach a rotating file handler to the ROOT logger, before
    streamlit_app is imported.

    Root, not a named logger, so BOTH the supervisor and the cycle loop land
    in the same file — they log through streamlit_app's module logger, which
    propagates to root.

    A CONSOLE handler is attached alongside it deliberately. streamlit_app
    calls logging.basicConfig() at import, and basicConfig is a NO-OP once
    the root logger already has handlers: attaching only the file handler
    would silently take the console output away, including the
    "SPY regime refreshed:" line the desk reads on the first cycle.

    Idempotent — calling it twice does not double up handlers or duplicate
    every line in the file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    def ours(kind):
        return [h for h in root.handlers
                if getattr(h, "_worker_handler", None) == kind]

    # Identify our own handlers by tag, not by type. Root may already carry
    # somebody else's StreamHandler (a library's, or pytest's capture
    # handler); type-sniffing would read that as "console already covered"
    # and silently skip ours.
    if not ours("file"):
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler._worker_handler = "file"
        root.addHandler(file_handler)

    if not ours("console"):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console._worker_handler = "console"
        root.addHandler(console)
    return root


# ----------------------------------------------------------- heartbeat

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


# ----------------------------------------------------------- lock / PID

def read_lock_pid(lock_file: str = LOCK_FILE):
    """PID recorded in the lock file, or None if absent/unreadable/empty."""
    try:
        with open(lock_file, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None                      # legacy empty lock from older builds
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def write_lock(pid: int = None, lock_file: str = LOCK_FILE):
    """Claim the lock, recording the owning PID (atomic)."""
    import safe_io
    safe_io.atomic_write_text(lock_file, str(pid if pid is not None
                                             else os.getpid()))


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
        return str(pid) in out
    except Exception:
        return False


def kill_pid(pid: int, wait_secs: int = KILL_WAIT_SECS) -> bool:
    """Kill a PID (and its children) and CONFIRM it died."""
    if not pid:
        return True
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"taskkill failed for PID {pid}: {e}")
    for _ in range(wait_secs):
        if not pid_alive(pid):
            return True
        time.sleep(1)
    return not pid_alive(pid)


# ----------------------------------------------------------- entry point

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    takeover = "--force-takeover" in argv

    age = live_heartbeat_age()
    if another_worker_is_alive():
        owner = read_lock_pid()
        if not takeover:
            print(f"worker already running (heartbeat {int(age)}s ago) — "
                  f"refusing to start."
                  + (f" Owner PID {owner}." if owner else
                     " No PID recorded in the lock file.")
                  + "\nUse --force-takeover to kill it and take over.")
            return 1
        print(f"--force-takeover: heartbeat {int(age)}s ago, "
              f"owner PID {owner if owner else 'unknown'}")
        if owner:
            if not kill_pid(owner):
                print(f"ABORT: could not kill owner PID {owner} — refusing to "
                      f"start a second instance beside it.")
                return 2
            print(f"owner PID {owner} killed; taking over.")
        else:
            # Legacy lock with no PID: fall back to the command-line scan.
            # Proceeding blindly here would create the very duplicate this
            # guard exists to prevent.
            import watchdog
            survivors = watchdog.find_worker_pids()
            # Exclude SELF and our PARENT: a launch creates a parent+child
            # python pair that both match "run_worker.py", so excluding only
            # os.getpid() made the takeover kill itself (observed live).
            mine = {os.getpid(), os.getppid()}
            targets = [p for p in survivors if p not in mine]
            print(f"no PID recorded (legacy lock); scanned and found "
                  f"{len(targets)} worker process(es) to kill")
            for pid in targets:
                if not kill_pid(pid):
                    print(f"ABORT: could not kill worker PID {pid}.")
                    return 2

    write_lock(os.getpid())

    try:
        import json
        import session_clock
        with open("bot_config.json") as f:
            cfg = json.load(f)
        print(f"worker starting (PID {os.getpid()}) — will shut itself down "
              f"at {cfg.get('session_end_et', '16:15')} ET "
              f"({session_clock.local_str(cfg)} local).")
    except Exception:
        print(f"worker starting (PID {os.getpid()})")

    # Logging BEFORE the import: streamlit_app's basicConfig() is a no-op
    # once root has handlers, so this decides where every later line goes.
    setup_logging()
    logging.getLogger(__name__).info(
        f"worker process starting (PID {os.getpid()})")

    import streamlit_app  # noqa: E402  (bare-mode import)
    streamlit_app.live_bot_worker()
    print("worker stopped (session ended or lock removed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
