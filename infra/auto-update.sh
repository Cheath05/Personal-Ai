#!/usr/bin/env bash
# Pull new commits for the hub and restart Cardinal, rolling back if it doesn't come up healthy.
# Run every 5 minutes by cardinal-update.timer (see install-auto-update.sh). Logs: journalctl -u cardinal-update
# Only code changes are applied. If setup-hub.sh itself changed (new services, packages), it says so: run that by hand.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
API="$REPO/services/api"
PORT=8000
STATE="$HOME/.cache/cardinal-update"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$STATE"

git -C "$REPO" fetch --quiet origin
old="$(git -C "$REPO" rev-parse HEAD)"
new="$(git -C "$REPO" rev-parse '@{u}')"
[[ "$old" == "$new" ]] && exit 0
if [[ "$(cat "$STATE/bad" 2>/dev/null)" == "$new" ]]; then
  exit 0   # already tried this commit and rolled back; wait for a newer one
fi
if ! git -C "$REPO" merge-base --is-ancestor "$old" "$new"; then
  echo "origin has diverged from the hub's checkout ($old); not updating. Fix it by hand." >&2
  exit 1
fi

healthy() {
  for _ in $(seq 1 30); do curl -sf "localhost:$PORT/api/health" >/dev/null && return 0; sleep 1; done
  return 1
}
apply() {   # check out a commit, sync packages, restart
  git -C "$REPO" merge --quiet --ff-only "$1" 2>/dev/null || git -C "$REPO" reset --quiet --hard "$1"
  (cd "$API" && uv sync --quiet --frozen --no-dev)
  sudo -n systemctl restart cardinal
}

echo "Updating ${old:0:7} -> ${new:0:7}:"
git -C "$REPO" log --oneline "$old..$new"
changed="$(git -C "$REPO" diff --name-only "$old" "$new")"

# A copy of the memory from just before the update, in case the new code mishandles it. Keeps the last 5.
DB="$REPO/data/cardinal.db"
if [[ -f "$DB" ]]; then
  mkdir -p "$HOME/cardinal-backups" && chmod 700 "$HOME/cardinal-backups"
  sqlite3 "$DB" ".backup '$HOME/cardinal-backups/pre-update-${new:0:7}.db'"
  ls -1t "$HOME"/cardinal-backups/pre-update-*.db | tail -n +6 | xargs -r rm --
fi

apply "$new"
if healthy; then
  echo "Cardinal is running ${new:0:7}."
  if grep -qx 'infra/setup-hub.sh' <<<"$changed"; then
    echo "NOTE: setup-hub.sh changed. Run $REPO/infra/setup-hub.sh by hand to apply the system-level part." >&2
  fi
  exit 0
fi

echo "Cardinal didn't come up healthy on ${new:0:7}; rolling back to ${old:0:7}." >&2
journalctl -u cardinal -n 30 --no-pager >&2 || true
echo "$new" >"$STATE/bad"
apply "$old"
healthy && echo "Rolled back; Cardinal is running ${old:0:7}." >&2 || echo "Rollback didn't come up either. See: journalctl -u cardinal" >&2
exit 1
