#!/bin/bash
# Double-click: sets up, runs once, then schedules a run every 15 minutes.
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
LABEL="com.threadsarchiver.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="/usr/bin/python3"
pause() { read -n1 -rsp $'\nPress any key to close.\n'; }

echo "== Threads Reply Archiver =="

case "$DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*|*"Mobile Documents"*|*"CloudStorage"*)
    echo "This folder is somewhere macOS blocks background jobs (or syncs to iCloud/Dropbox)."
    echo "Move the whole folder to:  $HOME/ThreadsArchive   then double-click Start again."
    pause; exit 1;;
esac

if ! xcode-select -p >/dev/null 2>&1; then
  echo "macOS needs its free Command Line Tools (for Python). An installer will pop up."
  xcode-select --install
  echo "When it finishes, double-click Start again."
  pause; exit 1
fi

if [ ! -f config.env ]; then
  cp config.example.env config.env
  chmod 600 config.env
  echo "Opening config.env. Paste your token after THREADS_ACCESS_TOKEN= , save, close,"
  echo "then double-click Start again."
  open -e config.env
  pause; exit 0
fi
chmod 600 config.env

if grep -q "paste_your_token_here" config.env; then
  echo "config.env still has the placeholder. Paste your token, save, and run Start again."
  open -e config.env
  pause; exit 1
fi

echo "Running first capture..."
if ! "$PY" threads_archiver.py; then
  echo "First run failed. Read the message above (also saved in data/log.txt)."
  pause; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/threads_archiver.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>$DIR/data/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/launchd.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo ""
echo "Done. It now runs every 15 minutes while this Mac is awake."
echo "Your spreadsheet: data/archive.csv"
open "$DIR/data"
pause
