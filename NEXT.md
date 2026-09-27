# Next session: Windows (G14) check-up

Start here when picking the project up on another machine. PLAN.md has the full design; README.md has run steps.

## Where things stand (27 Sep 2026)

- **The hub is live on Proxmox:** https://cardinal.tailaf3b0c.ts.net (Tailscale only).
  - Ubuntu 26.04 VM `ubun`, user `usr1`, repo at `~/Personal-Ai`.
  - Runs as the `cardinal` systemd service, with a nightly database backup to `~/cardinal-backups`.
  - To reach it: `ssh usr1@cardinal` (Tailscale SSH, no password).
  - `sudo` needs the user's password. Ask them to run anything that needs it.
- **Brains the hub uses:**
  - **G14:** Tailscale name `alex-windows`, `qwen3:8b` on the GPU at ~45 tok/s. Interactive chats go here first.
  - **Server:** `qwen3:4b-instruct` on the CPU at ~10 tok/s, with CPU type `host` for AVX2. Kept loaded.
  - The Mac brain is left out: its Ollama only listens on the Mac itself.
- **Tailscale devices:** `cardinal`, `alex-windows`, `m3-air`, `iphone-17-pro-max`.
- **Web app:**
  - Motion is now a per-device choice (System Call → Motion), set to full by default.
  - Windows used to freeze every animation, because "Animation effects: off" makes browsers report `prefers-reduced-motion`.
  - The thinking "clock hand" sweep was removed.

## Steps for the Windows session

1. `git pull` on branch `cardinal-foundation`.
2. **Animations:** open the hub URL in Edge or Chrome and press Ctrl+Shift+R. The Nexus should animate: orbiting nodes, rotating rings, and pulses when sending a message.
   - If it's still frozen, check the DevTools console (F12) for errors.
   - Check that `document.documentElement.dataset.motion` is `"full"`.
   - Check whether hardware acceleration is off in the browser settings.
   - Then fix the app, not the Windows setting.
3. **Install it as an app:** Edge → ⋯ → Apps → Install this site as an app.
4. **Make Ollama reliable after a reboot.** On first launch the tray app didn't start the server, so `ollama serve` was started by hand.
   - Make Ollama start at login with `OLLAMA_HOST=0.0.0.0`.
   - Reboot, then confirm the hub sees the G14: `curl.exe https://cardinal.tailaf3b0c.ts.net/api/brains` should show `g14` `online: true`.
5. **Firewall tidy-up:** the old rule "Ollama (Cardinal, Mac over WireGuard)" is no longer used. The Tailscale rule for `100.64.0.0/10` replaces it. Ask before removing it.
6. Push, and update this file.

## Things to keep in mind

- **One memory.** Real use goes through the hub URL. `scripts/dev.ps1` starts a separate, empty local copy for development only.
- **The G14 doesn't need to stay on.** When it's off, the hub uses the server brain, and Claude within the $20 cap if a key is ever added.
- **Voice (Phase 1.6) is next after this.** `qwen3:8b` already uses ~7.2 of the G14's 8 GB of VRAM, so GPU voice needs a plan: a smaller model while talking, or voice on the Mac and iPhone. Discuss it with the user before building.
- Never commit `.env` or API keys. Don't touch OPNsense or WireGuard. Every agent action must be previewed and approved.
