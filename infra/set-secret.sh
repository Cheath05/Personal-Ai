#!/usr/bin/env bash
# Save settings (like a Google client secret) in the hub's .env without them passing through chat,
# your shell history or the screen, then restart Cardinal so they take effect.
#
#   ssh -t usr1@cardinal '~/Personal-Ai/infra/set-secret.sh'                        # asks for the Phase 2 settings
#   ssh -t usr1@cardinal '~/Personal-Ai/infra/set-secret.sh ANTHROPIC_API_KEY'      # or just one
set -euo pipefail

ALLOWED="GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET CARDINAL_BLACKBOARD_ICS_URL ANTHROPIC_API_KEY CARDINAL_PUBLIC_URL"
declare -A HINT=(
  [GOOGLE_CLIENT_ID]="Google Cloud → Clients → your client → Client ID (ends in .apps.googleusercontent.com)"
  [GOOGLE_CLIENT_SECRET]="Same page → Client secret"
  [CARDINAL_BLACKBOARD_ICS_URL]="Blackboard → Calendar → settings → Get external calendar link"
  [ANTHROPIC_API_KEY]="console.anthropic.com → API Keys"
  [CARDINAL_PUBLIC_URL]="The hub's address, e.g. https://cardinal.tailaf3b0c.ts.net"
)

if [[ $# -eq 0 ]]; then
  set -- GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET CARDINAL_BLACKBOARD_ICS_URL
fi
for key in "$@"; do
  [[ " $ALLOWED " == *" $key "* ]] || { echo "Unknown setting: $key (allowed: $ALLOWED)" >&2; exit 1; }
done

ENV="$(cd "$(dirname "$0")/.." && pwd)/.env"
touch "$ENV"
chmod 600 "$ENV"
saved=0

for KEY in "$@"; do
  echo
  echo "$KEY: ${HINT[$KEY]}"
  read -r -s -p "Paste it (hidden) and press Enter, or just Enter to skip: " VALUE
  echo
  VALUE="$(printf '%s' "$VALUE" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -z "$VALUE" ]]; then
    echo "Skipped."
    continue
  fi
  tmp="$(mktemp "$ENV.XXXXXX")"
  grep -v "^${KEY}=" "$ENV" >"$tmp" || true
  printf '%s=%s\n' "$KEY" "$VALUE" >>"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV"
  echo "Saved (${#VALUE} characters)."
  saved=$((saved + 1))
done

if [[ $saved -gt 0 ]] && systemctl is-active --quiet cardinal 2>/dev/null; then
  sudo -n systemctl restart cardinal && echo && echo "Cardinal restarted. Open Today and press Connect."
fi
