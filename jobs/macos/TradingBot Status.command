#!/bin/bash
# Double-click in Finder (S12b). Desktop shortcuts are symlinks, so resolve
# this file's real location before finding the repo.
cd "$(dirname "$(realpath "$0")")/../.." || exit 1
# Same environment as the launchd jobs: "today" means today in New York
# (Broker.is_open_today reads the local date), whatever the Mac is set to.
export TZ=America/New_York PYTHONUTF8=1
venv/bin/python jobs/macos/desk_button.py status
rc=$?
echo
read -r -p "Press Enter to close this window." _
exit $rc
