#!/usr/bin/env bash
# Save a setting (like a Google client secret) in the hub's .env without it passing through chat,
# your shell history or the screen, then restart Cardinal so it takes effect.
#
#   ssh -t usr1@cardinal '~/Personal-Ai/infra/set-secret.sh GOOGLE_CLIENT_SECRET'
set -euo pipefail

KEY="${1:-}"
case "$KEY" in
  GOOGLE_CLIENT_ID|GOOGLE_CLIENT_SECRET|CARDINAL_BLACKBOARD_ICS_URL|ANTHROPIC_API_KEY|CARDINAL_PUBLIC_URL) ;;
  *) echo "Usage: $0 GOOGLE_CLIENT_ID | GOOGLE_CLIENT_SECRET | CARDINAL_BLACKBOARD_ICS_URL | ANTHROPIC_API_KEY | CARDINAL_PUBLIC_URL" >&2
     exit 1 ;;
esac

ENV="$(cd "$(dirname "$0")/.." && pwd)/.env"
touch "$ENV"
chmod 600 "$ENV"

read -r -s -p "Paste the value for $KEY (hidden), then press Enter: " VALUE
echo
VALUE="$(printf '%s' "$VALUE" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
[[ -n "$VALUE" ]] || { echo "Nothing entered; .env unchanged." >&2; exit 1; }

tmp="$(mktemp "$ENV.XXXXXX")"
grep -v "^${KEY}=" "$ENV" >"$tmp" || true
printf '%s=%s\n' "$KEY" "$VALUE" >>"$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV"
echo "Saved $KEY (${#VALUE} characters)."

if systemctl is-active --quiet cardinal 2>/dev/null; then
  sudo -n systemctl restart cardinal && echo "Cardinal restarted."
fi
