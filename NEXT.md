# Next session: hub on the Proxmox server (Phase 1.5)

Start here when picking the project up on another machine. PLAN.md has the full design; README.md has run steps.

## Where things stand (27 Sep 2026)

- **Phase 0 done:** API, brain router, usage readout, Nexus web app. 27 tests pass on the Mac and on Windows.
- **Mac:** Ollama runs at login with `qwen3:4b-instruct` (~29 tok/s). A real chat ran locally in ~4 s for $0.
- **G14:**
  - `qwen3:8b` runs fully on the GPU at ~44–46 tok/s with `"think": false`.
  - A loaded model uses ~7.2 of the 8 GB of VRAM, so GPU voice won't fit beside it.
  - `OLLAMA_HOST=0.0.0.0` is set. The firewall rule "Ollama (Cardinal, Mac over WireGuard)" allows TCP 11434 only from 10.10.20.3.
  - The tray app didn't start the server on first launch, so `ollama serve` was started by hand. Check that Ollama starts after a reboot.
- **Mac → G14 test failed.** `curl http://10.10.20.6:11434` timed out from the Mac (10.10.20.3), although the Mac reaches Proxmox (10.10.10.5) fine over the same WireGuard tunnel. Two WireGuard clients can't reach each other, either because of the OPNsense rules or the G14 tunnel's AllowedIPs.
  - **Decision:** don't change WireGuard or OPNsense. Put Tailscale on every device instead, which the hub needs anyway.
- **Hub decision:** the Proxmox Ubuntu VM hosts the hub for now. An M6 Mac mini (24–32 GB) may replace it later; that only means re-running the setup and editing `brains.yaml`.
- Branch: `cardinal-foundation` (pushed). `main` is still the initial commit. No Claude API key is set.

## Steps for this session

PLAN.md §3 has the details.

1. **Resize the VM in Proxmox:** 4 vCPU, 10 GB RAM with ballooning off, and 100 GB disk. Then grow the disk inside Ubuntu.
2. **Tailscale admin console:** turn on MagicDNS and HTTPS Certificates.
3. **On the VM:** clone the repo to `~/cardinal` and run `infra/setup-hub.sh`. If the repo is private, git asks for a GitHub username and a personal access token.
4. **Tailscale on the G14, Mac and iPhone:**
   - Rename the machines `g14` and `mac` in the admin console.
   - Add the G14 firewall rule for `100.64.0.0/10` (command in PLAN §3).
5. **Check:**
   - Open `https://cardinal.<tailnet>.ts.net` from the Mac. The Access view should show the G14 and the server Online.
   - Send a chat and confirm it routes to the G14.
   - Turn the G14 off and confirm the server answers.
6. **iPhone:** Add to Home Screen from Safari.
7. The hub is now the only place memory lives. Stop using `./scripts/dev.sh` on the laptops except for development. The Mac's `data/cardinal.db` is empty, so there's nothing to copy.

## Things to keep in mind

- Don't keep the G14 on all the time. When it's off, the router falls back to the Mac, then the server, then Claude within the $20 cap.
- Never commit `.env` or API keys. Don't touch OPNsense/WireGuard; ask first if something seems to need it.
- Every agent action must be previewed and approved. Trust rules come in Phase 2.
