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

To open it on your phone on the same Wi-Fi, run `./scripts/dev.sh --lan` and visit `http://<mac-ip>:8000`. There's no login yet, so only do this on your home network.

## Give it a brain (Ollama)

**On this Mac (8 GB):**

```bash
brew install ollama
brew services start ollama
ollama pull qwen3:4b        # about 2.5 GB
```

**On the ROG G14 (Windows 11, RTX 4060):**

1. Install Ollama from ollama.com.
2. In PowerShell, run `ollama pull qwen3:8b`.
3. Let the Mac reach it: set the user environment variable `OLLAMA_HOST` to `0.0.0.0`, then restart Ollama.
4. Allow TCP port 11434 in Windows Firewall for **Private** networks only.

Ollama has no password, so keep it on your home network. Later, Tailscale replaces this.

**Point Cardinal at your machines:**

```bash
cp services/api/config/brains.example.yaml services/api/config/brains.yaml
# edit the g14 url, e.g. http://192.168.1.50:11434
```

`brains.yaml` is git-ignored. The Access view shows which brains are online.

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
