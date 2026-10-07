#!/bin/bash
# install.sh - install the com.tradingbot.* launchd jobs for this user (S12).
#
# launchd StartCalendarInterval times are LOCAL time, and every schedule in
# these plists is written in US Eastern. So the Mac's own timezone must be
# America/New_York, or every job fires at the wrong hour. Checked first.
#
# This script does NOT start the worker or run any job: the plists set
# RunAtLoad false, so bootstrap only registers the schedules.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

TZ_LINK="$(readlink /etc/localtime || true)"
case "$TZ_LINK" in
    *America/New_York) ;;
    *)
        echo "ABORT: system timezone is '${TZ_LINK:-unknown}', not America/New_York."
        echo "launchd calendar times are local; the jobs would fire at the wrong hour."
        echo "Fix:  sudo systemsetup -settimezone America/New_York   (then re-run)"
        exit 1 ;;
esac

if [ ! -x "$REPO/venv/bin/python" ]; then
    echo "ABORT: $REPO/venv/bin/python not found - create the venv first."
    exit 1
fi

mkdir -p "$REPO/logs" "$AGENTS"
chmod +x "$HERE"/*.sh

# sed replacement: escape the characters sed treats specially.
REPO_SED="$(printf '%s' "$REPO" | sed 's/[&#\]/\\&/g')"

labels=()
for src in "$HERE"/com.tradingbot.*.plist; do
    label="$(basename "$src" .plist)"
    dst="$AGENTS/$label.plist"
    sed "s#__REPO__#$REPO_SED#g" "$src" > "$dst"
    plutil -lint "$dst"
    # Re-install replaces: drop any loaded copy first.
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$dst"
    labels+=("$label")
done

# The owner's two buttons (S12b), as Desktop shortcuts. Symlinks: each
# .command resolves its own real path, so it still finds the repo.
for button in "Start TradingBot.command" "TradingBot Status.command"; do
    chmod +x "$HERE/$button"
    ln -sfn "$HERE/$button" "$HOME/Desktop/$button"
done

echo
echo "Desktop: 'Start TradingBot' (only for a missed auto-start) and 'TradingBot Status'."
echo "Installed (repo: $REPO):"
for label in "${labels[@]}"; do
    echo "--- $label"
    launchctl print "$DOMAIN/$label" \
        | grep -E '^[[:space:]]*(state|path|program|working directory|last exit code) =' \
        | sed 's/^[[:space:]]*/    /' || true
done
echo
echo "Nothing was started. The first scheduled run is the next calendar slot."
