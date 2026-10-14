"""
jobs/run_job.py — S13. Run one scheduled job under a hard wall-clock limit.

    venv/bin/python jobs/run_job.py snapshot.py
    venv/bin/python jobs/run_job.py drop.py --discord

macOS has no `timeout`. A job that outlives its limit is killed - its whole
process group, SIGTERM then SIGKILL - and the expiry is logged, so launchd
(which never starts a job whose last run is still alive) can run it next
time. 2026-10-12: one hung snapshot.py blocked every snapshot for 24 hours.

The job runs as `python -m net <script>`, so S13's network timeouts and
secret redaction are in force for it. Its output is NOT piped through this
wrapper: the watchdog job relaunches the worker, and a worker holding a
pipe to a wrapper that has exited would break. Output goes straight to the
inherited log file; redaction happens inside the job (net.install()).

Limits (seconds): snapshot/drop 600, floor/review/outcomes 900, intern 1200,
anything else 900.
"""

import os
import signal
import subprocess
import sys
import time

LIMITS = {
    "snapshot.py": 600, "drop.py": 600,
    "floor.py": 900, "review_bot.py": 900, "outcomes.py": 900,
    "intern_desk.py": 1200,
}
DEFAULT_LIMIT = 900
KILL_GRACE_S = 10
TIMEOUT_EXIT = 124


def limit_for(script: str) -> int:
    return LIMITS.get(os.path.basename(script), DEFAULT_LIMIT)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _kill_group(proc, grace: float = KILL_GRACE_S) -> str:
    """SIGTERM the job's process group, SIGKILL whatever is left."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return "already gone"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    try:
        proc.wait(timeout=grace)
        return "SIGTERM"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return "SIGKILL"


def run(argv, limit: int = None, python: str = None) -> int:
    script = argv[0]
    limit = limit if limit is not None else limit_for(script)
    python = python or sys.executable
    print(f"==== {_stamp()} ==== {os.path.basename(script)} "
          f"(limit {limit}s)", flush=True)
    proc = subprocess.Popen([python, "-m", "net", *argv],
                            start_new_session=True)
    try:
        return proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        how = _kill_group(proc)
        print(f"[{_stamp()}] TIMEOUT: {os.path.basename(script)} exceeded "
              f"its {limit}s wall-clock limit - killed ({how}). launchd can "
              f"run it again on the next schedule.", flush=True)
        return TIMEOUT_EXIT


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: run_job.py script.py [args...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(sys.argv[1:]))
