#!/bin/bash
# run_job.sh - launchd entry for every scheduled job (S12, S13).
#
# S13: the job runs under jobs/run_job.py - a hard wall-clock limit
# (snapshot/drop 10 min, floor/review/outcomes 15 min, intern 20 min) that
# kills the job's process group and logs the expiry, so one hung network
# call can no longer block every later run (launchd never starts a job whose
# last run is still alive). Timeouts and secret redaction come from net.py.
cd "$(dirname "$0")/../.." || exit 1
export PYTHONUTF8=1
exec venv/bin/python jobs/run_job.py "$@"
