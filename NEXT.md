# Next session: Mac (add the Mac brain)

Start here when picking the project up on another machine. PLAN.md has the full design; README.md has run steps.

## Where things stand (27 Sep 2026)

- **The hub is live on Proxmox:** https://cardinal.tailaf3b0c.ts.net (Tailscale only).
  - Ubuntu 26.04 VM `ubun`, user `usr1`, repo at `~/Personal-Ai`.
  - Runs as the `cardinal` systemd service, with a nightly database backup to `~/cardinal-backups`.
  - To reach it: `ssh usr1@cardinal` (Tailscale SSH, no password).
  - `sudo` needs the user's password. Ask them to run anything that needs it.
  - **Updates itself:** `cardinal-update.timer` checks `cardinal-foundation` every 5 minutes. When there are new commits it backs up the database, pulls, restarts Cardinal and checks `/api/health`. If that fails, it rolls back and skips the bad commit.
    - Logs: `journalctl -u cardinal-update`.
    - A sudoers rule, `/etc/sudoers.d/cardinal-update`, lets `usr1` run only `systemctl restart cardinal` without a password.
    - Changes to `setup-hub.sh` still need a manual run.
    - **Anything pushed to `cardinal-foundation` goes live within 5 minutes**, so run the tests before pushing.
- **Brains:** the hub's `brains.yaml` now lists `g14` → `mac` → `server`. The `mac` entry was added on 27 Sep; the old file is `brains.yaml.bak-2026-09-27`.
  - **G14:** Tailscale name `alex-windows`, `qwen3:8b` on the GPU at ~40–45 tok/s. Online.
  - **Mac:** `m3-air`, `qwen3:4b-instruct` at ~29 tok/s. Listed on the hub (it has restarted), Offline until `tailscale serve` is set up on the Mac.
  - **Server:** `qwen3:4b-instruct` on the CPU at ~10 tok/s. Kept loaded.
  - **iPhone:** not a brain, and it can't be one. iOS can't run Ollama or serve a model to other devices. It's a client that shows up as a device tag on messages, and in Phase 1.6 it runs voice (Whisper and Kokoro in Safari), not thinking.
- **Tailscale devices:** `cardinal`, `alex-windows`, `m3-air`, `iphone-17-pro-max`.

## Done in the Windows session (27 Sep 2026)

- **Animations:** they work in Edge. Headless screenshots 1.5 s apart show the rings and nodes moving. Windows "Animation effects" is off on the G14, and the per-device Motion setting ignores it as intended. Hardware acceleration is on. No app fix was needed.
- **Ollama after reboot:** the tray app logged "starting ollama server" but never started one once `OLLAMA_HOST` was set. The scheduled task **Cardinal Ollama** (`scripts/g14-ollama-task.ps1`) now owns the server:
  - It runs at boot as the user (S4U, no window, no stored password), with `OLLAMA_HOST=0.0.0.0`.
  - A 5-minute watchdog trigger restarts it. Tested: killed at 5:39:28, back at 5:44:29, and the hub showed `g14 online: true`.
  - The tray app can stay installed for updates.
- **Firewall:** removed two auto-created `ollama.exe` allow rules on the **Public** profile (TCP + UDP, any address). They exposed Ollama to everyone on eduroam. The Tailscale rule (`100.64.0.0/10`) is what the hub uses.
  - The old rule "Ollama (Cardinal, Mac over WireGuard)" was **kept**, at the user's choice.
- **Still to check:** after the G14's next real reboot, run `curl.exe https://cardinal.tailaf3b0c.ts.net/api/brains` and look for `g14` `online: true`. It hadn't been rebooted during the session.
- **Edge app install:** opened for the user. Edge → ⋯ → Apps → Install this site as an app.

## Steps for the Mac session

1. `git pull` on branch `cardinal-foundation`.
2. **Let the hub reach the Mac's Ollama over Tailscale only.** Keep Ollama on localhost; it has no password and the Mac is often on eduroam, so don't set `OLLAMA_HOST=0.0.0.0` there. Instead:
   ```bash
   tailscale serve --bg --tcp 11434 tcp://localhost:11434
   # CLI path if `tailscale` isn't on PATH: /Applications/Tailscale.app/Contents/MacOS/Tailscale
   ```
   Then from the Mac or G14: `curl http://m3-air:11434/api/version`.
3. Check `curl https://cardinal.tailaf3b0c.ts.net/api/brains` shows `mac` `online: true`, and that the Access view lists three brains.
4. Push, and update this file.

## Things to keep in mind

- **One memory.** Real use goes through the hub URL. `scripts/dev.ps1` and `scripts/dev.sh` start a separate, empty local copy for development only.
- **The G14 doesn't need to stay on.** When it's off, the hub uses the Mac (once added), then the server, then Claude within the $20 cap if a key is ever added.
- **Voice (Phase 1.6) is next after this.** `qwen3:8b` already uses ~7.2 of the G14's 8 GB of VRAM, so GPU voice needs a plan: a smaller model while talking, or voice on the Mac and iPhone. Discuss it with the user before building.
- Never commit `.env` or API keys. Don't touch OPNsense or WireGuard. Every agent action must be previewed and approved.
