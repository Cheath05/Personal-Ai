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
~/cardinal/infra/setup-hub.sh
```

Cardinal then runs around the clock at `https://cardinal.<your-tailnet>.ts.net`, with nightly database backups.

**Updates are automatic.** Every 5 minutes the hub checks the `cardinal-foundation` branch for new commits. When it finds some, it:

1. backs up the database,
2. pulls the new code,
3. restarts Cardinal.

If Cardinal doesn't come back healthy, the hub rolls back to the previous commit and skips the bad one. See what happened with `journalctl -u cardinal-update`. Changes to `setup-hub.sh` itself, such as new services or packages, still need you to run it by hand. See PLAN.md §3 for the VM size and the Tailscale steps.

## Connect Google and Blackboard (Phase 2)

Cardinal reads your calendar, both inboxes (senders and subjects) and Blackboard due dates. It can't change anything yet.

**1. Create a Google Cloud project (free, once).** Go to [console.cloud.google.com](https://console.cloud.google.com), signed in as your personal Gmail. Don't add a billing account; these APIs don't need one.

1. Create a project named **Cardinal**.
2. **APIs & Services → Library:** enable the **Google Calendar API** and the **Gmail API**.
3. **Google Auth Platform → Get started:**
   - App name `Cardinal`, with your Gmail as the support email.
   - Audience: **External**.
4. **Audience → Test users:** add your personal and UMBC addresses. The app stays in **Testing**, so Google asks you to reconnect every 7 days, which is one tap on Today.
5. **Clients → Create client:**
   - Type: **Web application**.
   - Authorized redirect URI: `https://cardinal.tailaf3b0c.ts.net/api/google/callback`

**Optional, to stop the weekly reconnects:** switch the app to "In production".

1. **Branding:**
   - Homepage: `https://cardinal.tailaf3b0c.ts.net/`
   - Privacy policy: `https://cardinal.tailaf3b0c.ts.net/privacy.html`
   - Authorized domain: `tailaf3b0c.ts.net`
   - Don't upload a logo, because that forces Google's review.
2. Then **Audience → Publish app**. If Google refuses the domain, stay in Testing.

**2. Give them to the hub.** From a terminal on the Mac, run:

```bash
ssh -t usr1@cardinal '~/Personal-Ai/infra/set-secret.sh'
```

It asks for the Client ID, the Client secret and your Blackboard calendar link, with hidden typing, so they never show on screen or in chat. Press Enter to skip any of them. The Blackboard link is at **Blackboard → Calendar → settings → Get external calendar link**.

**3. Connect.** Open Cardinal → **Today** → **Connect** next to Personal Google, then UMBC Google.

- Google may say "Google hasn't verified this app". That's expected, because you're its developer. Choose **Continue**, or **Advanced → Go to Cardinal**.
- Leave both boxes (Calendar and Gmail) ticked.
- If UMBC shows "Access blocked", its admins don't allow outside apps. Instead, forward UMBC mail to your Gmail and share your UMBC calendar with your Gmail account.

Sign-in tokens are encrypted on the hub. The key is in `data/secret.key`, which is not in the backups. Disconnect any time from Today, or at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Lock it with passkeys, and get notifications (Phase 7)

**Passkeys.** On each device you use, open Cardinal → **Access → Security → Add a passkey on this device** (Face ID, Touch ID or Windows Hello). Then press **Turn the lock on**. After that, every browser needs its passkey.

- **A new device:** on a signed-in one, press **Sign in another device** to get a one-time code. On the new device, open Cardinal → **New device? Use a one-time code**.
- A passkey made on the iPhone also works on the Mac through iCloud Keychain.
- **Locked out?** SSH to the hub and run:

  ```bash
  ssh usr1@cardinal
  cd ~/Personal-Ai/services/api
  .venv/bin/python -m cardinal.admin code     # a one-time code for a new passkey
  .venv/bin/python -m cardinal.admin unlock   # or turn the lock off
  ```

**Notifications.** On the iPhone, open Cardinal **from its Home Screen icon** (iOS only allows notifications there). Then go to **Access → Notifications → Turn on for this device**. Do the same on the Mac and G14 browsers.

- You get pings for the morning briefing, check-ins, anything waiting 10 minutes for your OK, and urgent email.
- Quiet hours default to 22:00–06:00.

**Backups.** The hub copies its memory and your uploaded files every night, and checks weekly that the newest copy restores (**Access → Backups**). For a second copy on the Mac: **Access → Backups → Copy to this Mac → Make a secret**, then run `./scripts/install-backup-copy-mac.sh` and paste it.

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
