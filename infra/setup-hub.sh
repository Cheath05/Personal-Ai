#!/usr/bin/env bash
# Set up (or update) the Cardinal hub on the Ubuntu VM: API + database, the server brain,
# nightly database backups, and HTTPS through Tailscale. Safe to run again to update.
#
#   git clone -b cardinal-foundation https://github.com/Cheath05/Personal-Ai.git ~/cardinal
#   ~/cardinal/infra/setup-hub.sh
#
# Run it as your normal user (not root); it asks for sudo when it needs it.
# Nothing here touches OPNsense or WireGuard: Tailscale only makes outbound connections.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
API="$REPO/services/api"
RUN_AS="$(id -un)"
MODEL="qwen3:4b-instruct"
PORT=8000

step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not root. It uses sudo where needed." >&2
  exit 1
fi

step "Latest code"
before="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" pull --ff-only || echo "Couldn't fast-forward; keeping the current checkout."
if [[ "$(git -C "$REPO" rev-parse HEAD)" != "$before" ]]; then
  exec "$0" "$@"   # this script may have changed: start over with the new copy
fi

step "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq curl git sqlite3 ca-certificates >/dev/null

step "Python environment (uv)"
if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
(cd "$API" && uv sync --frozen --no-dev)

step "Settings"
if [[ ! -f "$REPO/.env" ]]; then
  cp "$REPO/.env.example" "$REPO/.env"
  chmod 600 "$REPO/.env"
  echo "Created .env from .env.example (no Claude key: fully local)."
fi
BRAINS="$API/config/brains.yaml"
if [[ ! -f "$BRAINS" ]]; then
  # On the hub the server brain is this machine; the laptops are reached by their Tailscale names.
  cat >"$BRAINS" <<'YAML'
# Brains as seen from the hub (Proxmox VM). Laptops are reached by their Tailscale machine names.
# The Mac's Ollama only listens on the Mac itself, so it isn't listed here.
local:
  g14:
    url: "http://alex-windows:11434"     # the G14's Tailscale machine name
    model: "qwen3:8b"
    label: "ROG G14 · RTX 4060"
  server:
    url: "http://localhost:11434"
    model: "qwen3:4b-instruct"
    label: "Proxmox server"

order:
  interactive: [g14, server]
  background: [server, g14]

claude:
  default: "claude-sonnet-5"
  cheap: "claude-haiku-4-5"
  deep: "claude-sonnet-5"
YAML
  echo "Created $BRAINS."
fi

step "Server brain (Ollama, $MODEL on CPU)"
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh   # listens on localhost only
fi
# Keep the model loaded: a cold load on CPU adds up to a minute to the first reply.
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_KEEP_ALIVE=-1"\n' | sudo tee /etc/systemd/system/ollama.service.d/cardinal.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ollama >/dev/null
sudo systemctl restart ollama
if ! grep -qw avx2 /proc/cpuinfo; then
  echo "WARNING: this VM's CPU has no AVX2, so the server brain will be very slow (~4 tok/s)."
  echo "In Proxmox: VM > Hardware > Processors > Type: host, then shut the VM down and start it again."
fi
for _ in $(seq 1 20); do curl -sf localhost:11434/api/version >/dev/null && break; sleep 1; done
ollama pull "$MODEL"

step "Cardinal service"
sudo tee /etc/systemd/system/cardinal.service >/dev/null <<UNIT
[Unit]
Description=Cardinal hub (API, web app, shared memory)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$API
ExecStart=$API/.venv/bin/uvicorn cardinal.main:app --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/cardinal-backup.service >/dev/null <<UNIT
[Unit]
Description=Back up Cardinal's memory database

[Service]
Type=oneshot
User=$RUN_AS
ExecStart=$REPO/infra/backup-db.sh
UNIT

sudo tee /etc/systemd/system/cardinal-backup.timer >/dev/null <<'UNIT'
[Unit]
Description=Nightly backup of Cardinal's memory database

[Timer]
OnCalendar=*-*-* 03:30
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now cardinal-backup.timer >/dev/null
sudo "$REPO/infra/install-auto-update.sh" "$RUN_AS"
sudo systemctl enable cardinal >/dev/null
sudo systemctl restart cardinal
for _ in $(seq 1 20); do curl -sf "localhost:$PORT/api/health" >/dev/null && break; sleep 1; done
curl -sf "localhost:$PORT/api/health" >/dev/null && echo "Cardinal is running." \
  || { echo "Cardinal didn't start. See: journalctl -u cardinal -n 50" >&2; exit 1; }

step "HTTPS through Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Sign in with the link below (same account as your iPhone, Mac and G14)."
  sudo tailscale up --hostname=cardinal --ssh
fi
sudo tailscale serve --bg "$PORT"   # prints a link to enable HTTPS if it's off
URL="https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

step "Done"
echo "Cardinal:  $URL"
echo "Brains:    curl -s localhost:$PORT/api/brains"
echo "Logs:      journalctl -u cardinal -f"
echo "Updates:   automatic every 5 minutes (journalctl -u cardinal-update)"
echo "           $REPO/infra/setup-hub.sh applies system-level changes by hand"
echo
echo "If the HTTPS address doesn't load, turn on MagicDNS and HTTPS Certificates"
echo "in the Tailscale admin console (DNS page), then run: sudo tailscale serve --bg $PORT"
