#!/bin/bash
# uninstall.sh - remove every com.tradingbot.* launchd job for this user.
# Unloads the schedules and deletes their plists from ~/Library/LaunchAgents.
# Does not touch a worker that is already running; stop that separately.
set -uo pipefail

AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

labels="$( { launchctl list | awk '{print $3}' | grep '^com\.tradingbot\.';
             ls "$AGENTS" 2>/dev/null | grep '^com\.tradingbot\..*\.plist$' \
                 | sed 's/\.plist$//'; } | sort -u )"

if [ -z "$labels" ]; then
    echo "No com.tradingbot.* jobs installed."
    exit 0
fi

for label in $labels; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null \
        && echo "booted out $label" || echo "$label was not loaded"
    rm -f "$AGENTS/$label.plist"
done
