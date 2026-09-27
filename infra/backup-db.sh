#!/usr/bin/env bash
# Copy Cardinal's memory database to ~/cardinal-backups and keep the last 14 days, plus your uploaded
# school files (data/files) as a mirror. secret.key and push.pem are left out on purpose: a stolen backup
# can't sign in anywhere. Cardinal checks the newest backup weekly (a restore drill, Access → Backups).
# Run nightly by cardinal-backup.timer (see setup-hub.sh). Safe while Cardinal is running.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DB="$REPO/data/cardinal.db"
DEST="$HOME/cardinal-backups"

[[ -f "$DB" ]] || { echo "No database yet at $DB"; exit 0; }
mkdir -p "$DEST"
chmod 700 "$DEST"
sqlite3 "$DB" ".backup '$DEST/cardinal-$(date +%F).db'"
find "$DEST" -name 'cardinal-*.db' -mtime +14 -delete
if [[ -d "$REPO/data/files" ]]; then
  mkdir -p "$DEST/files"
  rsync -a --delete "$REPO/data/files/" "$DEST/files/"
fi
echo "Backed up to $DEST/cardinal-$(date +%F).db"
