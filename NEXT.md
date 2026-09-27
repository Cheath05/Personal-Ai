# Next session: ROG G14 (Windows 11)

Start here when picking the project up on another machine. PLAN.md has the full design; README.md has run steps.

## Where things stand (27 Sep 2026)

- Phase 0 is done: API, brain router, usage readout, Nexus web app. 27 tests pass.
- Phase 1 on the Mac is done: Ollama runs at login with `qwen3:4b-instruct` (~29 tok/s). A real chat ran locally in ~4 s for $0.
- Branch: `cardinal-foundation` (pushed). `main` is still the initial commit.
- No Claude API key is set, so everything runs locally.

## Goal for the Windows session

Make the G14 Cardinal's fast brain (Phase 1, second half):

1. Install Ollama for Windows and run `ollama pull qwen3:8b`. The 8B model fits the RTX 4060's 8 GB of VRAM.
2. Check that thinking can be turned off. `qwen3:8b` is a hybrid model, so `"think": false` should give direct answers. If it doesn't, try the `qwen3:8b` instruct variant, as we did on the Mac, where plain `qwen3:4b` turned out to be thinking-only.
3. Let other machines reach it:
   - Set the user environment variable `OLLAMA_HOST=0.0.0.0` and restart Ollama.
   - Allow TCP 11434 in Windows Firewall, **Private networks only**.
4. Measure tokens per second, so the numbers can be compared with the Mac's.
5. On the Mac, copy `brains.example.yaml` to `brains.yaml` and set the g14 `url` to the G14's LAN IP. Then check the Access view shows it Online and that chats route to "ROG G14 · RTX 4060".
6. Optional: run the web app on Windows to test it in Edge/Chrome. `scripts/dev.sh` is bash, so add a PowerShell version (`scripts/dev.ps1`). uv works on Windows.

## Things to keep in mind

- **One memory.** The hub (API + `data/cardinal.db`) lives on the Mac until Phase 1.5. A Cardinal started on Windows gets its own empty database. That's fine for testing, but real use should point at the one hub.
- **Don't keep the G14 on all the time.** When it's off, the router falls back to the Mac, then the server, then Claude within the $20 cap.
- **Hub decision pending.** The choice is between the Proxmox VM (free, PLAN.md §2.6) and an M6 Mac mini:
  - With 24–32 GB, the mini would be the always-on hub *and* the main brain, able to run models up to ~30B.
  - With only 16 GB, it mainly adds "always on".
  - Either way, the code change is a new entry in `brains.yaml`.
- **Ground rules:** never commit `.env` or API keys, and don't touch OPNsense/WireGuard. Every agent action must be previewed and approved (trust rules come in Phase 2).
