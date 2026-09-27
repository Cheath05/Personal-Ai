#!/usr/bin/env bash
# Install the hub's auto-update: every 5 minutes, auto-update.sh pulls new commits and restarts Cardinal.
# Run with sudo from your normal user (setup-hub.sh does this):  sudo ~/Personal-Ai/infra/install-auto-update.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${1:-${SUDO_USER:-}}"
if [[ $EUID -ne 0 || -z "$RUN_AS" || "$RUN_AS" == root ]]; then
  echo "Run with sudo from your normal user: sudo $0" >&2
  exit 1
fi
SYSTEMCTL="$(command -v systemctl)"

# The updater runs as you, so it may restart Cardinal (and only that) without a password.
RULE=/etc/sudoers.d/cardinal-update
printf '%s ALL=(root) NOPASSWD: %s restart cardinal\n' "$RUN_AS" "$SYSTEMCTL" >"$RULE.tmp"
chmod 440 "$RULE.tmp"
visudo -cf "$RULE.tmp" >/dev/null
mv "$RULE.tmp" "$RULE"

cat >/etc/systemd/system/cardinal-update.service <<UNIT
[Unit]
Description=Pull new Cardinal commits and restart the hub
After=network-online.target cardinal.service
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
ExecStart=$REPO/infra/auto-update.sh
UNIT

cat >/etc/systemd/system/cardinal-update.timer <<'UNIT'
[Unit]
Description=Check for new Cardinal commits every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT

chmod +x "$REPO/infra/auto-update.sh"
systemctl daemon-reload
systemctl enable --now cardinal-update.timer >/dev/null
echo "Auto-update on: every 5 minutes. Logs: journalctl -u cardinal-update"
