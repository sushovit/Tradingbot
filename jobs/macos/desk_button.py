"""
desk_button.py — the owner's two Finder buttons (S12b).

    python jobs/macos/desk_button.py start    # "Start TradingBot.command"
    python jobs/macos/desk_button.py status   # "TradingBot Status.command"

The desk starts itself on trading days (launchd, com.tradingbot.start_worker
at 09:25 ET). START exists only for a missed auto-start: it never launches
the worker directly. It refuses on a weekend, on a market holiday, when a
worker is already running, or when the launchd job is not installed, and
otherwise asks launchd to run that same job now. Everything the scheduled
start does - the calendar gate, the process-group launch, the
single-instance guard - therefore still applies.

STATUS is read-only.

Every check is a parameter so the tests can replace the broker and the
process table; the defaults are the real ones.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

ET = ZoneInfo("America/New_York")
START_LABEL = "com.tradingbot.start_worker"
START_WAIT_SECS = 120
START_LOG = os.path.join("logs", "start_worker.log")
WORKER_LOG = os.path.join("logs", "worker.log")


# ------------------------------------------------------------ real checks

def _domain() -> str:
    return f"gui/{os.getuid()}"


def job_installed(label: str = START_LABEL) -> bool:
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{label}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def market_open_today() -> bool:
    """Broker calendar. Broker.is_open_today() already fails open on a
    calendar error; a Broker() that cannot even be built is treated the
    same way (the scheduled start fails open too)."""
    from dotenv import load_dotenv
    load_dotenv()
    try:
        from broker import Broker
        return Broker().is_open_today()
    except Exception as e:
        print(f"(calendar unavailable: {type(e).__name__}: {e} - "
              f"treating today as open)")
        return True


def running_worker_pids() -> list:
    """The lock's live owner PID, plus any run_worker.py process found."""
    import run_worker
    import watchdog
    pids = []
    owner = run_worker.running_owner_pid()
    if owner is not None:
        pids.append(owner)
    for pid in watchdog.find_worker_pids():
        if pid not in pids:
            pids.append(pid)
    return pids


def kickstart(label: str = START_LABEL) -> int:
    return subprocess.run(["launchctl", "kickstart",
                           f"{_domain()}/{label}"]).returncode


def tail(path: str, n: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return f"({path} not found)\n"


# ------------------------------------------------------------ start

def refusal(now_et=None, is_open=market_open_today,
            worker_pids=running_worker_pids,
            installed=job_installed):
    """Why the button must not start the desk, or None if it may.

    Order matters: the cheap, certain checks first, so a weekend never
    costs a broker call."""
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return (f"It is {now_et:%A} in New York - the market is closed on "
                f"weekends. The desk is not started on weekends.")
    if not is_open():
        return (f"Today ({now_et:%Y-%m-%d}) is not a trading day - the "
                f"market is closed. Nothing to start.")
    pids = worker_pids()
    if pids:
        return (f"The desk is already running (PID "
                f"{', '.join(str(p) for p in pids)}). Never start it twice.")
    if not installed():
        return (f"The launchd job {START_LABEL} is not installed. Run "
                f"jobs/macos/install.sh first.")
    return None


def wait_until_running(wait_secs: int = START_WAIT_SECS, poll: float = 2.0,
                       sleep=time.sleep, clock=time.time):
    """PID of a live worker with a lock AND a fresh heartbeat, or None."""
    import run_worker
    deadline = clock() + wait_secs
    while True:
        pid = run_worker.running_owner_pid()
        if pid is not None and run_worker.another_worker_is_alive():
            return pid
        if clock() >= deadline:
            return None
        sleep(poll)


def start(refuse=refusal, launch=kickstart, wait=wait_until_running,
          out=print) -> int:
    reason = refuse()
    if reason:
        out(f"NOT STARTED: {reason}")
        return 1
    out("Asking launchd to run the scheduled start now...")
    rc = launch()
    if rc != 0:
        out(f"launchctl kickstart failed (exit {rc}).")
        out(f"Last 20 lines of {START_LOG}:\n{tail(START_LOG, 20)}")
        return 1
    pid = wait()
    if pid is None:
        out(f"The desk did not come up within {START_WAIT_SECS} s.")
        out(f"Last 20 lines of {START_LOG}:\n{tail(START_LOG, 20)}")
        return 1
    out(f"Desk running, PID {pid}")
    return 0


# ------------------------------------------------------------ status

def _floor_positions() -> str:
    import floor
    return "\n".join(floor.positions_section({})).strip()


def status(worker_pids=running_worker_pids, is_open=market_open_today,
           positions=_floor_positions, out=print) -> int:
    import clockline
    import run_worker
    out(clockline.two_zone_line())
    pids = worker_pids()
    out(f"Worker: {'PID ' + ', '.join(map(str, pids)) if pids else 'not running'}")
    age = run_worker.live_heartbeat_age()
    out(f"Heartbeat: {f'{int(age)} s ago' if age is not None else 'none'}")
    out(f"Market today ({datetime.now(ET):%Y-%m-%d} ET): "
        f"{'OPEN (trading day)' if is_open() else 'CLOSED'}")
    out("")
    out(positions())
    out("")
    out(f"Last 10 lines of {WORKER_LOG}:\n{tail(WORKER_LOG, 10)}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(REPO)
    if argv[:1] == ["start"]:
        return start()
    if argv[:1] == ["status"]:
        return status()
    print("usage: desk_button.py start|status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
