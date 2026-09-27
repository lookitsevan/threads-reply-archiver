#!/bin/bash
# Double-click: stop the 15-minute schedule. Your archive is kept.
PLIST="$HOME/Library/LaunchAgents/com.threadsarchiver.agent.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
rm -f "$PLIST"
echo "Stopped. Archive untouched. Double-click Start to resume."
read -n1 -rsp $'Press any key to close.\n'
