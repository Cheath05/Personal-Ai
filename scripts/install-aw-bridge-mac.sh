#!/usr/bin/env bash
# Mac: install ActivityWatch (free, open source) and have this laptop send app usage to Cardinal every 15 min.
# Get the secret first: Cardinal → Review → Focus → "Connect a laptop". Run: ./scripts/install-aw-bridge-mac.sh
set -euo pipefail

HUB="${CARDINAL_HUB:-https://cardinal.tailaf3b0c.ts.net}"
DEVICE="${CARDINAL_DEVICE:-Mac}"
DIR="$HOME/Library/Application Support/Cardinal"
PLIST="$HOME/Library/LaunchAgents/com.cardinal.awbridge.plist"
PY="$(command -v python3)"

if [[ ! -d "/Applications/ActivityWatch.app" ]]; then
  echo "Installing ActivityWatch…"
  brew install --cask activitywatch
fi
open -a ActivityWatch || true
echo "ActivityWatch needs Accessibility permission to see which app is in front:"
echo "System Settings → Privacy & Security → Accessibility → turn on ActivityWatch (aw-watcher-window)."

read -r -s -p "Paste the secret from Cardinal (hidden), then Enter: " TOKEN; echo
[[ -n "$TOKEN" ]] || { echo "No secret entered."; exit 1; }
umask 077
printf '{"hub": "%s", "token": "%s", "device": "%s"}\n' "$HUB" "$TOKEN" "$DEVICE" > "$HOME/.cardinal-aw.json"

mkdir -p "$DIR"
cp "$(dirname "$0")/aw_bridge.py" "$DIR/aw_bridge.py"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cardinal.awbridge</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/aw_bridge.py</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$DIR/aw_bridge.log</string>
  <key>StandardOutPath</key><string>$DIR/aw_bridge.log</string>
</dict></plist>
PLIST_EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Done. This Mac now sends app usage every 15 minutes. Log: $DIR/aw_bridge.log"
echo "To stop: launchctl unload \"$PLIST\""
