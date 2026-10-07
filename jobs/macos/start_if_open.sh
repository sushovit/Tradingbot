#!/bin/bash
# start_if_open.sh - jobs/start_worker.bat for launchd (S12).
#
# Runs ONLY on a trading day: 2026-09-07 was Labor Day and a blind "start it
# Monday" would have started the desk into a closed market. FAIL-OPEN: if the
# calendar cannot be read, the worker starts anyway - a false start is cheap
# (session_clock refuses to trade outside the window), a false SKIP silently
# loses a session. Broker.is_open_today() already fails open on a calendar
# error; the exit-2 path below also covers Broker() itself failing.
cd "$(dirname "$0")/../.." || exit 1
PY=venv/bin/python
export PYTHONUTF8=1
echo "==== $(date '+%Y-%m-%d %H:%M:%S %Z') ===="

"$PY" -c '
import sys
try:
    from dotenv import load_dotenv
    load_dotenv()
    from broker import Broker
    open_today = Broker().is_open_today()
except Exception as e:
    print(f"calendar check failed ({type(e).__name__}: {e})")
    sys.exit(2)
sys.exit(0 if open_today else 1)
'
rc=$?
if [ "$rc" -eq 1 ]; then
    echo "not a trading day - worker not started"
    exit 0
elif [ "$rc" -ne 0 ]; then
    echo "calendar unknown - starting worker anyway (fail-open)"
else
    echo "trading day - starting worker"
fi

# The worker must lead its own process group (run_worker.spawn_worker sets
# start_new_session), so a watchdog kill of its group never reaches this
# launcher or launchd. Waiting on it keeps the session in this job, as the
# .bat did. run_worker's own single-instance guard still applies.
exec "$PY" -c 'import sys, run_worker; sys.exit(run_worker.spawn_worker([]).wait())'
