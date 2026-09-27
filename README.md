# Cardinal

A personal AI workforce: eight agents led by Cardinal, a chief of staff. They run on your own machines first, with Claude as a capped backup. The design and roadmap are in [PLAN.md](PLAN.md).

## What works today (Phase 0)

- **Nexus** web app: the animated agent map. Select any agent and chat with it.
- **Brain router**: every message goes to the first local brain that's online (G14, then this Mac, then the server). If the answer fails its checks, it can go to Claude, but only within budget.
- **Usage readout**: the small `tok · $` text at the top right. Click it for tokens, Claude spend, credit left and a projection. The Access view has the full breakdown.
- **Budget guard**: at $15 of Claude spend a month, only essential jobs use Claude. At $20 everything runs locally until the 1st.

No agent can read your accounts or change anything yet. Integrations start in Phase 2.

## Run it on the Mac

You need [uv](https://docs.astral.sh/uv/). It's already installed.

```bash
cp .env.example .env        # set your name; leave ANTHROPIC_API_KEY empty to stay fully local
./scripts/dev.sh            # http://localhost:8000
```

On Windows (PowerShell), use `.\scripts\dev.ps1` (or `.\scripts\dev.ps1 -Lan`).

**iPhone, for now:**

1. Run `./scripts/dev.sh --lan` on the Mac.
2. In Safari on the same Wi-Fi, open `http://<mac-ip>:8000`. Find the Mac's IP with `ipconfig getifaddr en0`.

There's no login yet, so only do this on your home network. Installing to the home screen needs HTTPS, which comes when the hub moves to the Proxmox server (PLAN.md §2.5–2.7).

**One memory for every device:** conversations, habits and usage live in the hub's database (`data/cardinal.db`). Devices only display it, so whatever you teach Cardinal from one device is known on all of them.

## Give it a brain (Ollama)

**On this Mac (8 GB):**

Already set up. Ollama runs in the background and starts at login.

```bash
brew install ollama
brew services start ollama
ollama pull qwen3:4b-instruct   # about 2.5 GB; answers directly (the plain qwen3:4b tag "thinks" for ~25 s first)
```

**On the ROG G14 (Windows 11, RTX 4060):**

1. Install Ollama: `winget install Ollama.Ollama`.
2. In PowerShell, run `ollama pull qwen3:8b`.
3. In an admin PowerShell, run `.\scripts\g14-ollama-task.ps1`. It runs the Ollama server from boot (before login, with no window) and restarts it within 5 minutes if it stops.
4. Allow Ollama from your Tailscale devices only (admin PowerShell):
   ```powershell
   New-NetFirewallRule -DisplayName "Ollama (Cardinal, Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow
   ```

Ollama has no password. If Windows asks whether to let `ollama.exe` through the firewall, click **Cancel**: allowing it opens Ollama to everyone on public Wi-Fi.

**Point Cardinal at your machines:**

```bash
cp services/api/config/brains.example.yaml services/api/config/brains.yaml
# edit the g14 url, e.g. http://192.168.1.50:11434
```

`brains.yaml` is git-ignored. The Access view shows which brains are online.

## Host it on the Proxmox server (the hub)

On the Ubuntu VM, as your normal user:

```bash
sudo apt install -y git
git clone -b cardinal-foundation https://github.com/Cheath05/Personal-Ai.git ~/cardinal
~/cardinal/infra/setup-hub.sh      # run again any time to update
```

Cardinal then runs around the clock at `https://cardinal.<your-tailnet>.ts.net`, with nightly database backups. See PLAN.md §3 for the VM size and the Tailscale steps.

## Tests

```bash
cd services/api && uv run pytest
```

## Layout

```
apps/web/            Web app (plain HTML/CSS/JS modules, no build step), served by the API
services/api/        FastAPI app, agents, brain router, usage tracking (Python, uv)
  config/            agents.yaml, routing.yaml, brains.example.yaml
scripts/dev.sh       Run everything locally
design/              Concept page and (local-only) reference images
data/                Local SQLite database (git-ignored)
```
