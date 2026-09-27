#!/usr/bin/env bash
# Mac: keep a second copy of the hub's memory on this Mac, so a dead server SSD can't take it with it.
# Once a day (and after waking, if it missed the time) it downloads the hub's newest nightly backup over Tailscale
# HTTPS into ~/Library/Application Support/Cardinal/hub-backups, keeping the last 14. Nothing is sent to the hub.
# Get the secret first: Cardinal → Access → Backups → Copy to this Mac → Make a secret.
# Run: ./scripts/install-backup-copy-mac.sh        Remove: ./scripts/install-backup-copy-mac.sh --remove
set -euo pipefail

HUB="${CARDINAL_HUB:-https://cardinal.tailaf3b0c.ts.net}"
DIR="$HOME/Library/Application Support/Cardinal"
DEST="$DIR/hub-backups"
PLIST="$HOME/Library/LaunchAgents/com.cardinal.backupcopy.plist"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST" "$DIR/backup-token" "$DIR/backup-copy.sh"
  echo "Removed the daily copy. Copies already made stay in: $DEST"
  exit 0
fi

read -r -s -p "Paste the secret from Cardinal (hidden), then Enter: " TOKEN; echo
[[ -n "$TOKEN" ]] || { echo "No secret entered."; exit 1; }
umask 077
mkdir -p "$DEST"
printf '%s' "$TOKEN" > "$DIR/backup-token"

cat > "$DIR/backup-copy.sh" <<SCRIPT
#!/bin/sh
# Written by install-backup-copy-mac.sh. Downloads the hub's newest backup and keeps the last 14.
set -e
umask 077
date
tmp="$DEST/.incoming"
/usr/bin/curl -fsS --max-time 300 -H "X-Cardinal-Token: \$(cat "$DIR/backup-token")" -o "\$tmp" "$HUB/api/backups/download"
name="cardinal-\$(date +%F).db"
/usr/bin/sqlite3 "\$tmp" "PRAGMA quick_check" | grep -qx ok && mv "\$tmp" "$DEST/\$name" && echo "saved \$name"
ls -1t "$DEST"/cardinal-*.db 2>/dev/null | tail -n +15 | while read -r old; do rm -f "\$old"; done
SCRIPT
chmod 700 "$DIR/backup-copy.sh"

echo "Testing the download…"
/bin/sh "$DIR/backup-copy.sh" || { echo "The download failed. Check the secret, and that Tailscale is on."; exit 1; }

cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cardinal.backupcopy</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$DIR/backup-copy.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DIR/backup-copy.log</string>
  <key>StandardErrorPath</key><string>$DIR/backup-copy.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Done. A copy is saved every day at 12:00 (or when the Mac wakes after missing it)."
echo "Copies: $DEST"
