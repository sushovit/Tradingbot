#!/bin/bash
# run_job.sh <script.py> [args...] - what each jobs/*.bat does, for launchd:
# cd to the repo, write a dated banner, run one entry point with the venv
# python. launchd supplies PYTHONUTF8/TZ and the logs/<job>.log redirect.
cd "$(dirname "$0")/../.." || exit 1
echo "==== $(date '+%Y-%m-%d %H:%M:%S %Z') ===="
exec venv/bin/python "$@"
