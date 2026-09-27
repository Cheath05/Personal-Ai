# Next session: Phase 1.6 (voice) or Phase 7 (hardening)

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
  - **Mac:** `m3-air` (`100.113.113.70`), `qwen3:4b-instruct` at ~29 tok/s. Online through `tailscale serve`.
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

## Done in the Mac session (27 Sep 2026)

- `tailscale serve --bg --tcp 11434 tcp://localhost:11434` on the Mac. Ollama stays on `127.0.0.1`; the tailnet reaches it at `100.113.113.70:11434`. The setting persists across restarts.
- **Gotcha:** Ollama answers **403** to any `Host` header that isn't localhost or an IP (DNS-rebinding protection). So the hub's `brains.yaml` uses the Mac's Tailscale IP, not `m3-air`. The G14 doesn't hit this, because it listens on `0.0.0.0`.
- The hub shows all three brains **online**: g14, mac and server. `setup-hub.sh`'s template matches.

## Phase 2a (built 27 Sep 2026): reading your day

- **Code:**
  - `cardinal/sources/google.py`: OAuth, Calendar and Gmail over REST, read-only scopes.
  - `cardinal/sources/blackboard.py`: the iCal feed.
  - `cardinal/today.py`: one cached snapshot. It also builds each agent's data block, filtered by `access` in agents.yaml.
  - `cardinal/briefing.py`: Ordinal's briefing, plus a 30-second scheduler loop. It writes at `CARDINAL_BRIEFING_TIME`, catches up if the hub was down, and never writes when nothing is connected.
  - `cardinal/vault.py`: Fernet encryption. The key is in `data/secret.key`, not in the DB or the backups.
- **API:** `/api/today`, `/api/briefing`, `/api/google/{connect,callback,disconnect}`. Web: the Today view.
- **The hub's .env** has `CARDINAL_PUBLIC_URL=https://cardinal.tailaf3b0c.ts.net`. The user adds the Google client ID, the Google secret and the Blackboard link with `infra/set-secret.sh` (README → "Connect Google and Blackboard").
- **Tested:** 40 tests with mocked Google and Blackboard. A demo run on the Mac's `qwen3:4b-instruct` wrote an accurate briefing in 13 s.
- **Not yet tested against real Google.** The first real sign-in is the test. The UMBC account may be blocked by its admins.

## Calendar and syllabus import (built 27 Sep 2026)

- **Today → Calendar:** a day time-grid.
  - A strip of days from 7 back to 14 ahead; the API allows 7 back to 120 ahead.
  - Morning, afternoon, evening and night tints, colored blocks, a now-line, and all-day and due chips.
  - Code: `cardinal/calendar.py`, `apps/web/js/calendar.js`.
- **Your own items** (`CalendarItem`): add and remove in the app.
  - Mirrored to a **"Cardinal" Google calendar** created with the `calendar.app.created` scope, so Cardinal can't touch any other calendar. It shows in Apple Calendar wherever the Google account is added.
  - Items saved before that permission is granted sync on reconnect (`sync_unsynced`).
- **Calendar links** (`CalendarFeed`, URL encrypted): e.g. an iCloud public calendar. Repeats are expanded with `recurring-ical-events`.
- **Syllabus import** (`cardinal/syllabus.py`): a link (web page, public Google Doc export, PDF), a photo or PDF upload, or pasted text.
  - OCR runs on the hub with RapidOCR. `opencv-python` is overridden to the headless build, because the VM has no libGL.
  - The text is split into ~5k-character chunks, then the `syllabus` job (local, JSON schema) extracts the dates.
  - The code parses human dates and times itself ("Sep 12", "11:59pm"), because small models ignore the requested format. It also fills in a missing time from the item's own spot on the line.
  - The user reviews and ticks the proposals; nothing is added without that.
  - Measured on the Mac's 4B model: 16/16 dates from a pasted schedule in 27 s, and 5/5 from a photo in 13 s. Plain topic rows are sometimes skipped.
- **Needs the user once:** Today → Personal Google → **Reconnect**, to grant the Cardinal-calendar permission.

## Phase 2b (built 27 Sep 2026): acting, with approval

- **`cardinal/actions.py`** is the only code path that changes anything.
  - Agents `propose()` an `Action`. It runs only on Authorize, or when an active `TrustRule` matches.
  - A rule-run action still shows as a note with Undo on Today and in the Activity Log.
  - `ALWAYS_ASK` (email.send, calendar.remove_item, files.delete, payment, settings.security, device.operator) can never match a rule, and can't become one.
  - Rules expire after 60 days unused. Undo works for 14 days.
  - Sigma offers a rule after 3 approvals of the same kind within 30 days.
- **Action kinds:** `calendar.add_block` only, which writes a `CalendarItem` and its Google mirror. Add a kind by giving it preview, rule_from, matches and an execute branch.
- **`cardinal/planner.py`**: Axiom's study planner. It's plain code, no LLM.
  - Input: 7 days of due dates (Blackboard, plus syllabus items of kind due/exam/quiz), de-duplicated.
  - Grouping:
    - Work due the same day → one block of 60–150 min.
    - Quiz → 45 min.
    - Exam → 2 × 90 min.
  - Placement: free time between 08:00 and 22:00, afternoons and evenings first, with a 10-minute buffer. At most 2 blocks or 180 min per day.
  - Suggestions are de-duplicated by `dedupe_key`, so a denied one never comes back.
  - Runs daily at the briefing time (and at hub start if that time has passed), or from "Suggest study time".
- **UI:**
  - Today → "Needs your OK" cards (Authorize, Always allow… with the exact rule shown first, Deny) and ghost "PROPOSED" blocks on the calendar.
  - Access → Trust rules (with Revoke) and the Activity log (with Undo).
  - Top bar → "N to OK" chip.
- **Tested:** 63 tests. A demo run covered proposals around events, the rule dialog, auto-add by a rule, and the log.

## Phase 3 (built 27 Sep 2026): Executive Assistant and Core Memory

- **`cardinal/review.py`**:
  - **Delta's morning check-in:** top 3 plus energy 1–5. It creates `Task`s, and Delta replies using the calendar.
  - **Evening review:**
    - Planned vs done is pre-filled from today's tasks and study blocks.
    - You answer went well / didn't / why, and set tomorrow's first task. Unfinished tasks carry over.
  - Both check-ins are also posted to Delta's chat.
  - **`week_stats`** are computed in code: check-ins, completion, study blocks, energy by day and vs last week, streak.
  - **Weekly rollup** (Sun `CARDINAL_ROLLUP_TIME` 18:00):
    - Delta writes JSON (summary, wins, blockers, 3 experiments, focus).
    - Next week's plan becomes a `plan.set_week` Action for the user to authorize; that's when the `Experiment`s are created.
    - The rollup uses the interactive lane (G14 first) for quality.
  - **Sigma's nightly pass** (02:00) proposes `memory.add` Actions:
    - Arithmetic patterns: weekday energy, check-in time, completion.
    - Up to 2 model patterns from evening notes, which must cite at least 2 real dates.
    - Deduplicated forever.
  - **Core Memory** (`Memory`): the user can add, edit and delete entries. Active ones go into every agent's prompt except Radix (`MEMORY_BLOCK` in agents.py).
- **Agents with the `tasks` access** (Cardinal, Ordinal, Delta, Sigma) see today's priorities, energy, last night's review and this week's experiments. The briefing includes them too.
- **UI:**
  - The Review view: morning card, evening card, This week tiles with energy by day, experiments (Kept / Partly / Skipped), rollup, and Core Memory.
  - The top bar shows a green "Morning check-in" / "Evening review" chip when one is due.
- **Tested:** 71 tests. The demo on the Mac's 4B model gave a good calendar-aware morning reply and a good evening tip.
  - The rollup was fixed to write to "you" and to use numbers exactly as given; the 4B model still overreaches a little.
- **Not yet:** push notifications for check-ins (Phase 7; Web Push works for home-screen apps on iOS). Running data in the rollup (Phase 4).

## Phase 4 (built 27 Sep 2026): Running (Vector)

- **`cardinal/running.py`**, all arithmetic:
  - **VDOT** (Daniels & Gilbert) from the current 5K. Zones: easy 65–72% gives 11:48–12:46/mi at VDOT 30.8, tempo 88% gives 10:05, 400 m at 97.5% gives 2:19, goal 400 m is 1:52.
  - **`WEEKS`**: the 12-week Sun/Tue/Wed plan, with mile time trials in weeks 4 and 8 and 5K time trials in weeks 6 and 12.
  - **The plan start** is stored in `Pref` `run_plan_start`: the first Monday after it was first opened. On the hub that's 2026-09-28.
  - **Time trials** (a checkbox, or distance within 0.9–1.15× of a mile or 5K) set `current_5k_min` / `current_mile_min` via Riegel, so all paces update.
  - **`match_runs`**: each run counts for at most one session, same day first, then ±1 day. `evaluate` flags easy runs faster than the easy zone.
  - **Data in:**
    - Manual log.
    - `POST /api/health/ingest` for Health Auto Export JSON, with header `X-Cardinal-Token`. Only the token's sha256 is stored.
    - `POST /api/health/import` for export.zip, streamed with iterparse, covering running workouts plus resting HR / HRV / VO2 max records.
    - Duplicates are recognized across sources (±3 min, ±5% distance).
  - **Readiness:** under 6 h of sleep, or resting HR at least 5 above the median, means "take it easy today".
  - **`propose_runs`**: Vector proposes each planned run as a `calendar.add_block` (item_kind event, noun "runs") at a free time. It prefers 16:30–20:30, and runs daily with the study planner.
- **Agents with `running` access** (Vector, Delta, Cardinal) get fitness, the week's sessions, recent runs and watch data. The weekly rollup includes runs done/planned and miles.
- **UI:** the Training view shows the status window (level = VDOT), this week's sessions, log a run, recent runs, the Apple Watch setup (token, export import) and the 12-week table.
- **Tested:** 80 tests. The demo covered HAE ingest, manual runs, session matching and the pace check.

## Agents act from chat (built 27 Sep 2026)

- **The problem:** agents only produced text. "I'll change it" changed nothing. The user hit this with Vector: "I can't run before 8:30" left the 06:00 run where it was.
- **`cardinal/tools.py`** makes each chat message two steps.
  - **1. Plan.** The `tool_plan` job answers in JSON, constrained by `plan_schema()`.
    - Tool names are an enum of that agent's tools, and args are known string fields only. A loose schema let the 4B model write junk inside strings.
    - The prompt includes the next 8 days with dates, one example per tool, and the state with ids (`[item 12]`, `[proposal 9]`, `[task 4]`, `[memory 3]`).
    - It also includes "the most recent thing" from this conversation, so "that" resolves.
  - **2. Run the tools in code.**
    - Calendar changes become Actions (`calendar.add_block`, `calendar.move_item`, `calendar.remove_item`, which always asks), shown as cards in the chat.
    - Settings (`prefs.py`: run hours, including per weekday; study hours; briefing and check-in times), tasks and memories change right away.
    - Proposals that are still waiting are edited in place (`update_pending`).
  - **Then the reply:**
    - The prompt gets `results_block` (done / proposed, not done yet / didn't happen), or an explicit "nothing was changed".
    - `claim_check` rejects "I've moved it"-style claims when nothing happened. If the model insists, the reply gets "(To be clear: nothing was changed.)".
  - **Stored per reply:** `Message.action_ids` and `Message.changes`. The chat shows status lines plus Authorize / Always allow / Deny cards, and they reappear with the history.
- **Tools by agent:**
  - Vector: `set_run_hours` (re-fits upcoming runs; evenings still preferred inside the hours), `move_run`, `replan_runs`, `log_run`.
  - Axiom: calendar add/move/remove, `set_study_hours`, `suggest_study_time`.
  - Delta: tasks, check-in times.
  - Ordinal: briefing time, rewrite.
  - Sigma: remember/forget.
  - Cardinal: calendar, tasks, memory.
  - Relay and Radix: none at the time (Phase 6 added theirs).
- **Tested:** 89 tests. The real 4B model on the demo handled the user's own Vector message: it set the hours and proposed moving Tuesday's 06:00 intervals to 16:30.
- **On the hub:** Tuesday's approved run is still at 06:00. Re-send the request to Vector, and it will now propose the move.

## Phase 5 (built 27 Sep 2026): Second Brain (Axiom)

- **`cardinal/brain.py`:**
  - **Reading:** PDF (by page), .pptx (by slide, plus speaker notes, zip/XML with no extra dependency), .docx, text, HTML, links (`syllabus.fetch_url`) and photos (RapidOCR). Originals are kept in `data/files/`.
  - **Search:** passages of ~900 chars with page numbers, in a SQLite **FTS5** table `passage_fts` (porter tokenizer, BM25), created in `db.create_search_index`. No embedding model is needed.
  - **Digest,** per document, from up to 4 chunks spread across it: the `notes_digest` job (JSON) gives points, key terms with pages, and up to 6 flashcards per chunk; `notes_overview` writes the summary.
  - **Ask** (`/api/brain/ask`): the top 6 passages as numbered sources, levels simple / class / deep, and `[n]` citations that link to pages. Axiom's and Cardinal's chat (`notes` access) include matching passages and store `Message.sources`.
  - **Flashcards:** FSRS via the `fsrs` package; `Flashcard.fsrs` holds the card state and `due` is indexed. Axiom's chat tool `make_flashcard`.
  - **Practice tests** (`write_quiz`):
    - The model writes `right_answer` plus 3 `wrong_answers`; the **code** shuffles them and records the key.
    - Each question is then re-solved from its source alone (the `quiz_check` job), and kept only if it matches.
    - Sources are labeled S1…; letters got confused with answers.
    - Missed questions become flashcards.
    - Measured on the 4B model before this fix: 2 of 4 keys were wrong. After it: 8 of 8 correct across two tests, in about 30–40 s each.
  - **"Find dates"** runs a document through the syllabus reader, then the review dialog opens.
- **Brain view:** Ask your notes, Flashcards (review dialog), Exam mode, Library (upload, drop, paste or link; grouped by course), the document dialog (summary, terms, cards, pages), and the Core Memory ring (SVG squares).
- **Tested:** 100 tests. Real run on the Mac 4B: a lecture was digested into an accurate summary, 5 terms and 6 good cards, with the course detected.
- **Not done:** a Google Drive "School" folder sync (it needs the drive.readonly scope and the Drive API enabled), and Sigma's daily log.

## Phase 6 (built 27 Sep 2026): Full workforce

- **Relay** (`cardinal/relay.py`, `EmailItem`):
  - **Sorting:** hourly (briefing scheduler) or "Sort now". Gmail's Promotions/Social/Forums labels are Noise with no model. The rest go in batches of 6 to the `triage` job (JSON schema) for category (urgent / reply / fyi / noise), a reason, a task and a date.
    - Stored: sender, subject, Gmail's snippet and the verdict. Not the body.
    - **The schema caps string lengths** (`maxLength`). Without them the 4B model looped inside a string ("or go over project or go over…") and never closed the JSON.
    - An unreadable answer leaves that batch unsaved, so the next sort retries it.
    - The prompt includes today's date.
  - **Replies:** `write_draft` reads that one message's full text (the `draft` job) and returns To / Subject / Body in the thread (In-Reply-To set). It's laid out as a real email: greeting, blank line, message, first name.
    - Saving is an `email.draft` Action: undoable, and it can have a rule.
    - Sending is `email.send`: always asks, never a rule. The Inbox buttons count as the OK, because you press them with the text in front of you (`_email_action`).
  - **From an email:** "Add to today" (a Task) and "Put date on calendar" (a `calendar.add_block` proposal, deduped per message).
  - **Scopes:** drafts need `gmail.compose` (`SCOPE_COMPOSE`). Accounts connected before this show a Reconnect button, and `_gmail` refuses with "Reconnect…" until then.
- **Radix** (`cardinal/research.py`): searches DuckDuckGo's HTML page, its lite page when the first answers with a bot check (HTTP 202), then Wikipedia's search (keywords only).
  - It fetches the top pages, keeps only paragraphs that share words with the question, and drops off-topic pages (`on_topic`: a two-word term in the question, like "spacing effect", must appear on the page).
  - The reply cites [n], and the chat shows clickable source chips.
  - "Save to Second Brain" (`/api/research/save`, or Radix's `save_to_brain` tool) files the answer and its links as a document.
- **App usage** (`cardinal/appusage.py`, `AppUsage`): `scripts/aw_bridge.py` (standard library only) sends app minutes per hour from ActivityWatch every 15 minutes, with header `X-Cardinal-Token` (hash in `Pref` `apps_token_hash`).
  - Installers: `scripts/install-aw-bridge-mac.sh` (launchd) and `scripts/install-aw-bridge.ps1` (a scheduled task; **not yet run on Windows**).
  - Apps are grouped as focus / distraction / neutral by name. Browsers are neutral, because there are no titles.
  - Sigma's nightly pass adds best and worst focus hours once there are 5 days of data. The weekly stats include focus and distraction hours.
- **Cardinal** can `ask_teammate` (it runs that agent's own tools). **Status lights:** `/api/agents/status` gives each agent ok / warn / bad with a reason, shown on its card and its Nexus node.
- **UI:**
  - Today → Inbox: To do / FYI / Noise tabs, and a reply dialog. Send takes a second tap ("Confirm").
  - Review → Focus: tiles, an hourly stacked chart with tooltips and a table view, top apps, laptops, and "Connect a laptop".
  - The chart colors were checked with the dataviz validator on the dark panel, including all color-blind pairs: focus #199e70, other #3987e5, distraction #d95926.
- **Also fixed:** Cardinal answered "Do I work out tomorrow?" by proposing changes.
  - Tools now run only when the message asks for a change (`tools.wants_change`). Duplicate adds and no-op moves are refused.
  - The running context states tomorrow's plan outright (`schedule_lines`).
- **Tested:** 111 tests. On the real Mac 4B model:
  - Sorting: 7 emails in 10 s, all sensible.
  - Drafts: about 2 s each, and they read well.
  - Radix: a cited spacing-effect answer from Wikipedia and a university page in 9 s.
- **Needs the user:**
  - Today → Connections → **Reconnect** Personal Google to grant drafts. If Google refuses, add `gmail.compose` under Google Auth Platform → Data Access.
  - Review → Focus → Connect a laptop → Make a secret, then run the Mac installer. The G14 installer is for a Windows session.

## Next

- **Phase 1.6: voice** or **Phase 7: hardening** (push notifications for check-ins and urgent mail, passkey login). Ask the user.

## Things to keep in mind

- **One memory.** Real use goes through the hub URL. `scripts/dev.ps1` and `scripts/dev.sh` start a separate, empty local copy for development only.
- **The G14 doesn't need to stay on.** When it's off, the hub uses the Mac, then the server, then Claude within the $20 cap if a key is ever added.
- **Voice (Phase 1.6) is next after this.** `qwen3:8b` already uses ~7.2 of the G14's 8 GB of VRAM, so GPU voice needs a plan: a smaller model while talking, or voice on the Mac and iPhone. Discuss it with the user before building.
- Never commit `.env` or API keys. Don't touch OPNsense or WireGuard. Every agent action must be previewed and approved.
