# Cardinal — Personal AI System Plan

**Cardinal** is the name of the whole system and of its Chief of Staff agent. The other agents have short names drawn from math and code. Each has a unit ID in the Alicization `[XXX0-0000]` format.

Four modules, one app:

1. **Life Dashboard**: calendar, email, tasks, Blackboard deadlines, runs, health and weather on one screen, with an automatic morning briefing.
2. **Second Brain**: one folder for all school work. Cardinal reads it, writes notes and explanations, quizzes you, learns your habits, and coaches your running.
3. **Executive Assistant**: morning and evening check-ins, plus a weekly analysis of what to change.
4. **AI Workforce**: specialist agents led by Cardinal. You can talk to them by voice, and they talk back in expressive voices.

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Look | Frosted-glass panels with slow animated light on their edges. Digital HUD styling. The home screen is the **Nexus**: the selected agent is a large animated node in the middle, with the others orbiting it. Each agent has its own color. References are in `design/reference/`. |
| Voice | Two-way spoken conversation with **free, open-source** models. They run on the GPU of whatever device you're using when it can handle them. See §5. |
| AI health | No durability numbers. Each agent shows one status light: **Nominal**, **Degraded** (with the reason) or **Offline**. |
| School | Blackboard, via its calendar feed, notification emails and a drop folder. |
| Email | UMBC `.edu` (Google Workspace) plus your personal Gmail. Both use the Gmail API. No Yahoo. See §4. |
| Calendar | Google Calendar as the engine, viewed in the **Apple Calendar app**. |
| Tasks | Built into Cardinal. |
| Training | **Running only.** Sunday, Tuesday, Wednesday. Starting point: mile 8:00, 5K 30:00. See §8.2. |
| Your devices | M3 MacBook Air (macOS 27) · ASUS ROG Zephyrus G14 with RTX 4060 8 GB (Windows 11) · iPhone 17 Pro Max (iOS 27) |
| AI | **Local-first hybrid.** Free open-source models on your own hardware handle everything they can. Claude (API) steps in only for jobs local can't do well, with a **hard $20/month cap**. The goal is to move fully local over time. See §2.1–2.2. |
| Device control | Cardinal Link on the Mac and the G14 can act on the computer. The iPhone acts through Shortcuts. New actions are previewed first. **Trust rules** let actions you've already allowed run automatically next time. See §6. |
| Health | Apple Watch → Apple Health → the Health Auto Export app → Cardinal. |
| Device access | Broad read access to what you opt into. Never touches private categories. Every change is previewed and waits for your approval. See §6. |
| Build order | **Everything is built and tested on your Mac first.** The server and Tailscale come last. See §3 and §10. |
| Names | Math and code names. "System Call" is the command language for both typing and voice. |

## 2. How it runs

- **One app, installed everywhere:** a web app (PWA) that installs on your iPhone, Mac and Windows PC.
- **During development:** everything runs on your Mac.
- **When it's ready:** the same containers move to the Ubuntu VM on your Proxmox box, so scheduled agents run while your devices sleep.

### Compute routing: using your device's GPU

The server has no GPU, but your devices do. Each time the app connects, it checks what the device can do and runs voice work in the best place:

| Where you're using it | Hears you (speech-to-text) | Speaks (text-to-speech) |
|---|---|---|
| **ROG G14 (RTX 4060, 8 GB)** — the strongest machine | Whisper large-v3-turbo on CUDA | **Chatterbox-Turbo** on CUDA. Also room for Dia2/Orpheus experiments and optional local models (below) |
| **M3 MacBook Air** | Whisper through MLX / `whisper.cpp` on the Apple GPU | Chatterbox-Turbo on the Apple GPU (MPS). The Air has no fan, so long sessions may slow down a little |
| **iPhone 17 Pro Max**, in Safari or the installed app | Whisper-base or Moonshine in the browser via WebGPU | **Kokoro** in the browser via WebGPU. When the G14 or Mac is online, the phone can stream Chatterbox audio from it instead |
| **Nothing with a GPU online** | faster-whisper on the server CPU | Kokoro on the server CPU (slower, still usable) |

- **What Cardinal Link is:** a small background app for Mac and Windows. It runs the heavier voice models on your GPU and exposes them to Cardinal, only while it's running.
- **Overnight work** (lecture transcription, re-indexing notes) is also sent to your Mac or PC when Link is online, and falls back to the server CPU otherwise.
- **Thinking runs on your hardware first** (§2.1–2.2), with Claude as a capped backup.

### 2.1 What the AI actually is

**Nothing is retrained or rebuilt.** Training a model at Claude's level takes data centers of GPUs, which is out of reach for one person. Each agent is a set of instructions (personality and job), a list of tools it may use, access to your Core Memory, and a schedule. All of that wraps an existing model.

**Every agent's brain is a setting.** The app talks to one "brain router", so each job can use a free local model or Claude. **Default: local, with Claude only when needed (§2.2).**

| Where | Free model (via Ollama) | Used for | Notes |
|---|---|---|---|
| **G14, RTX 4060 8 GB** | **Qwen3 8B**, which calls tools well | Live chat and voice conversations when the G14 is on | Fastest brain you own |
| **M3 MacBook Air (8 GB)** | **Qwen3 4B** (8B doesn't fit alongside macOS and voice) | Short chats when the G14 is off | Voice (Whisper and Kokoro) runs here fine. Chatterbox is better left to the G14. |
| **Proxmox server (CPU only)** | Qwen3 4B or Gemma 3 4B | Scheduled jobs while you sleep: 5:30 AM briefing draft, email sorting, nightly memory pass, flashcards | Slow, but these jobs aren't waiting on you. The VM gets 10 GB RAM instead of 8. |
| **iPhone** | None; it uses whichever brain is online | — | The phone runs voice, not thinking |

- **Routing:** the router sends each request to the best brain that's online: G14 first, then the Mac, then the server.
- **If only the server is up:** chat still works, just slower.
- **Swappable models:** free models improve every few months. Model names live in one config file, so upgrading is one line. In Phase 1 I'll test the newest ones on your G14 and pick the best.

**Where local is enough, and where it isn't yet.** Local 4–8B models are capable, but noticeably weaker than Claude. This is what the router uses to decide:

| Works well free | Noticeably weaker free |
|---|---|
| Summarizing emails and sorting them into Urgent / Reply / FYI | Planning a messy week with many conflicting constraints |
| Briefings, check-ins, reflections | Catching subtle patterns in the weekly rollup |
| Flashcards, quizzes, explaining a concept from your notes | Long documents: smaller context, so big PDFs get split up |
| Running plan adjustments, since the pace math is done in code | Reliable multi-step tool use (the approval system catches mistakes) |
| Natural voice chat on the G14 | Nuanced writing in your voice |

**About Fable:** switching to Fable wouldn't make this free. Claude Fable is Anthropic's most capable and **most expensive** model ($10 / $50 per million tokens), and it's only available through the paid API. You don't need it here.

**Your data:** local jobs never leave your hardware, apart from Google (email and calendar) and the weather service. Claude jobs send only what that one job needs, and the redaction step runs first.

### 2.2 Local-first hybrid: the rules

**The brain router decides, in this order:**

1. **Can a local brain do this job?** Every job type has a default: **Local**, **Claude**, or **Local, then Claude if needed**. Most start as Local.
2. **Is a local brain online?** G14 first, then the M3 Air, then the server.
3. **Did local do it right?** The app checks the result automatically:
   - tool calls must be valid
   - answers from your notes must cite a source
   - plans must fit your calendar

   If local fails twice, the job escalates to Claude, **if the budget allows**.
4. **"Ask Claude instead" button:** on any answer, for when local wasn't good enough. Every tap is logged, which teaches the router which jobs local can't handle yet.

**Starting defaults:**

| Job | Default | Why |
|---|---|---|
| Voice chat, quick questions, flashcards, explanations | Local | Local is good at these |
| Email sorting, briefing draft, check-ins, memory pass | Local (server overnight) | Nobody is waiting on these |
| Running plan changes | Local | The pace math is done in code |
| Voice chat when the G14 is off | Local 4B, then Claude Haiku if it's too slow | Keeps voice usable on the phone |
| Weekly rollup (Sunday) | **Claude** (Sonnet 5, or Opus 5 at month-start if budget allows) | Local misses the patterns that matter |
| Planning a packed week or exam week | Local, then Claude if needed | Many constraints to juggle |
| Long PDFs (over about 40 pages) | **Claude Haiku 4.5** | Local context is too small; Haiku is the cheapest Claude model |

**Budget guard:**

| Spend this month | What happens |
|---|---|
| Under $15 | Normal |
| $15 | "Essentials only": only the weekly rollup and anything you tap "Ask Claude" on go to Claude |
| $20 | **Hard stop.** Everything runs locally until the 1st |

- **Second safety net:** a $20 spend limit on your Anthropic account, plus **prepaid credits with auto-reload off**. You can never be charged more than you loaded.
- **Cheap tricks built in:**
  - prompt caching, so repeated context costs about 10%
  - Haiku for simple jobs
  - the Batch API (50% off) for overnight work
- **Estimated spend: about $5–12 a month**, well under the cap.
- **The Access view** shows spend per agent and **what share of requests local handled**.

**Cooldowns: the API doesn't have the claude.ai ones.**

- The 5-hour and weekly usage limits you hit are features of the claude.ai Free and Pro plans.
- The API is pay-per-use, with **per-minute rate limits** that one person won't come close to.
- If a limit is ever hit, the router waits a few seconds or falls back to local, so the app never stalls.

**Claude Pro doesn't help here.**

- Pro covers claude.ai, the Claude apps and Claude Code. It includes no API credit.
- Anthropic requires an API key (not a subscription login) for apps built on Claude. Automated agents running on your Pro plan would also burn the chat limits that cause your cooldowns.
- **Where Pro does help:** your own chats, and using Claude Code to **build** Cardinal.

### 2.3 Path to fully local

- **Shadow testing:** when Claude handles a job, the server's local model quietly tries the same job overnight. The results are compared using the same automatic checks.
- **Monthly report:** "Local would have matched Claude on 78% of escalations." Job types that pass consistently switch to **Local** with one click.
- **Upgrades:** free models improve every few months. Each new one is tested on your G14 against your real job history before it's swapped in.
- **Personalizing a local model:** later, a small free model can be fine-tuned on **your own** data (your notes, your writing, your check-ins, your approvals) on the G14. Claude's answers are **not** used as training data, because Anthropic's terms don't allow using Claude outputs to train other AI models.
- **Hardware:** an eventual always-on GPU box (even a used RTX 3060 12 GB in the server) would let a stronger local model run 24/7 and make Claude unnecessary.

### 2.4 How the agents adapt to you

A model's weights don't change on their own as you use it. Cardinal gets better at being *your* assistant in these ways, and all of them work with free local models:

| Mechanism | What it learns | When |
|---|---|---|
| **Core Memory** (Sigma) | Facts and patterns with evidence, such as "best focus 9–11 AM" or "skips evening runs". The relevant ones go into every agent's instructions. | Nightly, from Phase 3 |
| **Your examples** | Drafts you edited, suggestions you accepted or rejected, how you phrase things. Close examples are shown to the model when it writes something similar. | From Phase 2 |
| **Router feedback** | Which jobs local handles well. Every "Ask Claude instead" tap and every failed check is counted. | Now |
| **Trust rules** | Which actions you're happy to let run automatically | Phase 2 |
| **Fine-tuning (optional)** | A small model trained on *your own* data (notes, writing, check-ins) on the G14, a few times a semester | After Phase 6 |

### 2.5 One hub, one memory

**All your devices share one brain's worth of knowledge.** Cardinal has one **hub**: the machine running the Cardinal API and its database. It holds every conversation, habit, pattern, trust rule and usage record.

- **The iPhone, the Mac and the G14** are windows into the hub. They keep nothing of their own, so a habit learned from your phone is known everywhere the moment it's saved.
- **The brains** (Ollama on the G14, Mac and server, plus Claude) are workers the hub calls. They also keep nothing between requests.
- **Device tags:** each message records which device it came from. That's for context ("replies from the iPhone are short"), not separate memories.

```
 iPhone ──┐                       ┌── G14 brain (fast, when awake)
 Mac ─────┼──▶  HUB (API + DB) ──┼── Mac brain (when awake)
 G14 ─────┘   one shared memory   ├── Server brain (always on, slow)
                                  └── Claude (capped backup)
```

**Where the hub lives:**

- **Now:** on the Proxmox VM (`https://cardinal.tailaf3b0c.ts.net`), always on. The laptops only run a local copy for development.
- **Maybe later:** an M6 Mac mini with 24–32 GB could be both the hub and the main brain. Moving there is the same setup plus a `brains.yaml` edit.

### 2.6 Is the Proxmox server still worth it? Yes, as the hub

The server's job changed. It's no longer the main brain, since its CPU is too slow for chat. It's the part that must never sleep:

| Job | Why a laptop can't do it |
|---|---|
| Hosting the hub and the shared memory | A closed MacBook would cut off the iPhone and the G14 |
| Scheduled jobs: 6:00 briefing, 7:00 and 21:30 check-ins, 2:00 memory pass | They'd silently not run while the laptop sleeps |
| Receiving Apple Watch data every hour | Health Auto Export needs something awake to send to |
| Background brain (Qwen3 4B on CPU) | Email sorting and overnight summaries, so the laptops stay free |
| Reachable from anywhere through Tailscale, with HTTPS | Needed to install the app on the iPhone and use the microphone |
| Backups of the memory database | Your history shouldn't live only on a laptop |

**Without the server:**

- The Mac could stay the hub. It would have to be open and awake for anything to work, and nothing would run overnight.
- The server costs nothing extra, since you already run it. Keep it.

### 2.7 How the iPhone uses it

- **It runs the app, not the AI.** The iPhone opens Cardinal from the hub like an app, from its home-screen icon. Your question goes to the hub, which picks a brain (the G14 if it's awake, otherwise the Mac, the server, or Claude within budget) and sends the answer back.
- **Nothing to install from the App Store.** In Safari, open the hub's address, then Share → **Add to Home Screen**. It gets its own icon, runs full screen, and can send notifications.
- **Before the hub moves to Proxmox:** run `./scripts/dev.sh --lan` on the Mac and open `http://<mac-ip>:8000` in Safari on the same Wi-Fi. Chat works. Installing, notifications and the microphone need the HTTPS address that Tailscale provides in Phase 1.5.
- **Voice (Phase 3):** the iPhone 17 Pro Max does the listening and speaking itself, using Whisper and Kokoro in Safari on its GPU. The thinking still happens on a brain.
- **Apple Watch data** flows iPhone → hub through Health Auto Export.

## 3. Server (Phase 1.5)

Nothing changes on OPNsense or WireGuard. The only work on the server is on the Ubuntu VM.

**1. Resize the idle Ubuntu VM in Proxmox:**

- 4 vCPU (type `host`), **10 GB RAM** with ballooning off (a local model runs here), 100 GB disk (`qm resize <vmid> scsi0 +80G`).
- At the current 2 GB the server brain can't load. Cardinal still runs, using the G14 and Claude.
- Then, inside Ubuntu:
  ```
  sudo growpart /dev/sda 3
  sudo pvresize /dev/sda3
  sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
  ```

**2. Create a free Tailscale account,** then open the DNS page in the admin console and turn on **MagicDNS** and **HTTPS Certificates**.

**3. Run the setup script on the VM.** No Docker is needed.

```bash
sudo apt install -y git
git clone -b cardinal-foundation https://github.com/Cheath05/Personal-Ai.git ~/cardinal
~/cardinal/infra/setup-hub.sh
```

[`infra/setup-hub.sh`](infra/setup-hub.sh) does the following:

- Installs uv, the app, and Ollama with Qwen3 4B, the server brain. Ollama listens on localhost only.
- Creates `.env` and a hub `brains.yaml`.
- Runs Cardinal as a `systemd` service on `127.0.0.1:8000`.
- Adds a nightly database backup to `~/cardinal-backups`, keeping 14 days.
- Installs Tailscale, signs in as `cardinal`, and serves the app over HTTPS at `https://cardinal.<your-tailnet>.ts.net`.

Tailscale only makes outbound connections, so OPNsense needs no port forwards or rule changes. Run the script again whenever you want to update: it pulls the latest code and restarts.

**4. Install the Tailscale app** on your iPhone, Mac and G14, signed in to the same account.

- The hub's `brains.yaml` reaches the G14 by its Tailscale machine name (`alex-windows`).
- On the G14, allow Ollama from Tailscale. In an admin PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "Ollama (Cardinal, Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow
  ```

  Only your own devices have addresses in that range.
- The Mac brain: its Ollama stays on localhost because the Mac is often on campus Wi-Fi. `tailscale serve --bg --tcp 11434 tcp://localhost:11434` shares it with your own devices only. The hub uses the Mac's Tailscale IP (`100.113.113.70`), because Ollama answers 403 to requests addressed by any other hostname.

**5. On the iPhone:** open the `https://cardinal…ts.net` address in Safari, then Share → **Add to Home Screen**.

**Optional:** Tailscale can also reach your Proxmox UI, with nothing changed on OPNsense.

```bash
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf && sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
sudo tailscale up --hostname=cardinal --ssh --advertise-routes=10.10.10.0/24
```

Then approve the route in the admin console. This matters because an iPhone can only have one VPN active at a time. Without this route, you switch between Tailscale and WireGuard when you need Proxmox.

**Backups:** set up a Proxmox scheduled backup of the VM to a USB drive or NAS, plus a nightly encrypted `restic` copy of the database to off-site storage. The single 256 GB SSD is the main risk.

## 4. Integrations

| Source | How it connects |
|---|---|
| **Calendar** | Google Calendar API. Add the Google account on iPhone and Mac so everything shows in the Apple Calendar app. Cardinal's time blocks go in a separate "Cardinal" calendar. |
| **UMBC email + calendar** | UMBC is on Google Workspace, so it uses the **Gmail and Google Calendar APIs**, the same code as personal Gmail. The risk: UMBC's Google admin may block apps it hasn't approved from accessing student accounts. If you see "Access blocked", the fallbacks are:<br>• a Gmail filter that forwards UMBC mail to your personal Gmail<br>• sharing your UMBC calendar to your personal Google account<br>This gets tested first in Phase 2. |
| **Personal email** | Your personal **Gmail** (confirmed). Gmail API. |
| **Blackboard** | Due dates: **Calendar → settings → Get external calendar link**. Announcements and grades: notification emails, read by Relay. Files: the School folder, or later a browser extension that saves them as you browse. |
| **School folder** | Google Drive folder `Cardinal/School`, plus a "Send to Cardinal" Shortcut in the iPhone share sheet. |
| **Apple Watch** | The Health Auto Export app posts runs (distance, pace, splits, heart rate, cadence), sleep, resting heart rate, HRV and VO₂ max to Cardinal every hour. |
| **App usage (Mac/Windows)** | ActivityWatch, read by Cardinal Link. App names and time only, unless you turn on window titles. |
| **Weather** | Open-Meteo (free). |

## 5. Voice

### Free, open-source voice stack

| Job | Model | License | Why |
|---|---|---|---|
| Detect speech | **Silero VAD** (runs in the browser) | MIT | Notices when you start and stop talking, which is what makes interrupting work. |
| Speech-to-text | **Whisper** (in the browser with Transformers.js on WebGPU; `whisper.cpp` or MLX on Mac; `faster-whisper` on the server) | MIT | Accurate, runs everywhere. |
| Speech-to-text, lightweight | **Moonshine** | MIT | Very fast on phones. |
| Speech, expressive | **Chatterbox-Turbo** (Resemble AI, 350M) | MIT | An emotion exaggeration setting, `[laugh]` `[chuckle]` `[sigh]` style tags, and voice cloning from a short sample, so each agent gets a unique voice. In a blind test it was preferred over ElevenLabs. Runs on Apple Silicon (MPS) and NVIDIA. Easy to host with `Chatterbox-TTS-Server`. |
| Speech, runs anywhere | **Kokoro-82M** | Apache 2.0 | Very fast, natural, 50+ preset voices, runs in the iPhone browser. Doesn't do emotion control. |
| Alternates to test | **Dia2** (Apache 2.0; two-speaker dialogue and nonverbal sounds), **Orpheus** (Apache 2.0; very expressive but 3B, needs a strong GPU) | | Kept as options if Chatterbox doesn't suit an agent. |
| Pipeline | **Pipecat** (open source) | BSD-2 | Streams audio in, runs speech-to-text, then the agent's brain (local model or Claude), then speech out. Handles interruptions. |

### How agents sound expressive

- Each agent's prompt tells it to write speech with emotion cues. Examples: `[chuckle]` when you hit a PR, a calmer tone at 9:30 PM, extra energy for Ordinal's morning briefing.
- The speech engine turns those cues into delivery.
- Each agent gets its own cloned or preset voice and its own emotion level.

### Animation states

| State | What the Core does |
|---|---|
| Idle | Breathing particle sphere, slowly drifting rings, light glints circling |
| Listening | A ring of level bars around the sphere reacts to your voice; the rings tighten |
| Thinking | Particles spin into orbiting bands, a radar sweep, flickering hex readouts, and data pulses sent along links to the agents Cardinal is asking for help |
| Speaking | The sphere surface pulses with the voice, level bars spread symmetrically, and ripples flow outward |

- **System Call:** starting with "System Call" in voice or the ⌘K bar gives a direct command:
  - "System Call: generate briefing."
  - "System Call: summon Vector."
- **Hands-free on iPhone:** the web app can't listen in the background. Start voice by tapping the Core, or from an "Ask Cardinal" Siri Shortcut.

## 6. Privacy and approvals

**Data Access Charter** (enforced by the server's code, not just instructions to the AI):

| Allowed (you turn each on) | Never accessed |
|---|---|
| Calendar, both email accounts, Blackboard feed and emails, School folder | Messages, call history |
| Selected health types (runs, sleep, heart, activity) | Photos, contacts |
| App-usage time on Mac and Windows | Passwords, Keychain, banking |
| Folders you choose | Location history |
| Your check-ins, tasks, journal, training log | Keystrokes, screenshots, private browsing |
| The microphone, only while voice is open | Health types you didn't select |

**Action Preview:**

- **What agents can do on their own:** create content inside Cardinal, such as briefings, notes, flashcards and drafts.
- **What waits for approval:** everything that changes your calendar, email, tasks, files or devices. Each proposed change shows:
  - which agent wants to do it
  - exactly what changes (before → after)
  - why
  - whether it can be undone
- **Where you answer:** in the app or from the push notification, with **Authorize**, **Edit** or **Deny**.
- **Every action** is written to the Activity Log.

**Trust rules (remembered approvals).** Action Previews have three choices: **Authorize once**, **Always allow this**, and **Deny**.

- **What "Always allow" saves:** a narrow rule, not blanket permission. Examples:
  - "Vector may move **run events** in the **Cardinal calendar** by up to 3 hours."
  - "Link may move files from **Downloads** into **School** folders on the **M3 Air**."
  - "Study mode may close **Discord and Steam** on the **G14**."
- **What happens next time:** a matching action runs on its own, and you still get a quiet note afterward with an **Undo** button. Example: "Moved Wednesday's run to 6:30 AM (trust rule #3) · Undo".
- **Anything outside the rule still asks.** A different agent, a different calendar or folder, a bigger change or another device all get a normal Action Preview.
- **Sigma suggests rules:** after you approve the same kind of action 3 times, Sigma asks "Make this automatic?".
- **Managing rules:** the **Access** view lists every rule with how often it fired. You can edit or revoke any of them. Rules unused for 60 days expire.
- **These always ask, no matter what:**
  - sending an email or message to another person
  - permanently deleting anything
  - payments
  - security or privacy settings
  - Operator (screen control) mode
  - anything on the Never list

  They either can't be undone or they speak for you.

### 6.1 What agents can do on your devices

**Mac and Windows: Cardinal Link.** A small app you install on each computer. It keeps a secure connection to Cardinal and gives agents a fixed menu of device tools. Every tool that changes something goes through Action Preview.

| Tool | Examples | M3 Air | G14 |
|---|---|---|---|
| Files (only folders you allow) | Sort Downloads into course folders, rename lecture files, find last week's lab notes | ✓ | ✓ |
| Apps and links | "Study mode": close Discord and Steam, open Blackboard, the chem notes and a focus timer | ✓ | ✓ |
| System settings | Focus / Do Not Disturb, volume, dark mode, power mode (Silent on battery) | ✓ (via Shortcuts) | ✓ (PowerShell allow-list) |
| Your own automations | Run any macOS Shortcut or approved script you add to the list | ✓ | ✓ |
| Create documents | Start a lab report from a template, with the outline filled in | ✓ | ✓ |
| App usage learning | ActivityWatch, reporting app names and time | ✓ | ✓ |
| GPU voice and heavy jobs | Chatterbox, Whisper, overnight transcription | ✓ | ✓ (fastest) |

**Operator mode (off by default, needs hybrid mode):**

- Claude can take over the screen, seeing it and clicking and typing, for multi-step jobs no tool covers. Example: "fill in this Blackboard quiz settings page".
- It needs screenshots, which your Charter locks. You'd turn it on per task, watch it work, and it stops for approval before submitting anything.
- Keep it off until the rest is working. Free local models can't do this reliably, so it needs Claude.

**iPhone 17 Pro Max.** Apple doesn't let any app control the iPhone or read other apps. What works:

- **Reads:**
  - Health, via Health Auto Export.
  - Calendar and email, via Google.
  - Files you share to Cardinal.
- **Acts through Shortcuts** that you approve with one tap:
  - Cardinal sends a notification, for example "Set alarm for 6:10 and turn on Sleep Focus for tomorrow's run?"
  - You tap **Run**, and the Shortcut does it.
  - Shortcuts can set alarms, turn on Focus modes, start timers, add reminders, open apps and create notes.
- **Automations:** iOS Personal Automations can trigger Cardinal on their own. For example, on run days at 6:15 AM they open Vector's warm-up briefing.
- **Voice:** the **Action Button** or "Hey Siri, ask Cardinal" starts voice.

## 7. The AI Workforce

| Unit ID | Agent | Color | Job | System Control (sees) | Object Control (can change, with approval) |
|---|---|---|---|---|---|
| `[CRD0-1000]` | **Cardinal** | Blue | Chief of Staff. Plans, delegates, resolves conflicts, time-blocks the calendar. | Everything allowed | Calendar blocks, tasks, sending approved email, run schedule, files, starting other agents |
| `[ORD1-2071]` | **Ordinal** | Amber | Morning briefing and the live dashboard | Calendar, tasks, Blackboard, inbox triage, health, weather, memory | Pin the briefing |
| `[RLY2-4410]` | **Relay** | Teal | Email triage for both accounts, reply drafts, pulls tasks and dates out of email | School email, personal email | Send a reply, label, archive |
| `[AXM3-0314]` | **Axiom** | Violet | Second Brain: notes, flashcards, practice tests, cited answers, exam plans | School folder, Blackboard, notes, calendar | Add study blocks, add deadlines |
| `[VEC4-0098]` | **Vector** | Red | Running coach for mile and 5K | Apple Watch runs, sleep, heart, calendar, training log | Change the plan, move a run |
| `[DLT5-0712]` | **Delta** | Lime | Executive Assistant: check-ins and the weekly rollup | Tasks, calendar, journal, training, app usage | Reschedule unfinished tasks, set the weekly plan |
| `[SGM6-1089]` | **Sigma** | Magenta | Keeps Core Memory, the profile of your habits every agent reads | Everything allowed, read-only | Save a new pattern (shown to you first) |
| `[RDX7-0016]` | **Radix** | Silver | Research with cited sources | The web | Save findings to the Second Brain |

- **Status light:** Nominal when recent runs succeeded and data is fresh; Degraded with a plain reason (for example "Apple Health sync is 2 hours late"); Offline when the agent can't run.
- **Brains (see §2.1–2.2):** local first (Qwen3 8B on the G14, 4B on the Air and server). Claude only for Delta's weekly rollup, long PDFs and escalations, under a $20/month cap.

## 8. Modules

### 8.1 Life Dashboard (Ordinal)

- **Status strip:** weather, sleep, resting heart rate, deadlines this week, approvals waiting.
- **Four panels:** briefing, today's timeline, inbox (school and personal), due soon.
- **Morning briefing (6:00 AM):** a push notification. Tap it to hear Ordinal read it aloud.

### 8.2 Running (Vector)

**Starting point:** mile 8:00, 5K 30:00. That puts your running fitness score (VDOT) at about 31.

**What that tells us:** your mile is faster than your 5K would predict. An 8:00 mile usually pairs with about a 27:30 5K. **Endurance is your limiter, not speed.** Most of the gain will come from easy mileage and tempo runs. Intervals are one of the three runs, not the focus.

**12-week targets:** 5K about **27:30**, mile about **7:30**. Vector adjusts these after each time trial.

**Weekly structure: Sunday, Tuesday, Wednesday.**

- **Why this order:** Tuesday and Wednesday are back-to-back, so only one of them can be hard.
- **Tuesday is the one quality day.** It alternates each week between intervals (mile speed) and tempo (5K).
- **Sunday** comes after three rest days, so it's the long easy run, the biggest aerobic builder. That's what your 5K needs most.
- **Wednesday** is an easy recovery run.

| Run | Purpose | Weeks 1–4 | Builds to (weeks 9–12) |
|---|---|---|---|
| **Sun: Long easy** | Aerobic base | **3 mi at 11:45–12:45 /mi**, plus 4 × 20 s strides | 5–5.5 mi easy. The last mile at tempo from week 7 |
| **Tue (odd weeks): Intervals** | Mile speed, running economy | 10 min warm-up, 6 × 400 m at ~2:15 with 90 s jog, 10 min cool-down | 8 × 400 m at ~2:00 (7:30 mile pace) |
| **Tue (even weeks): Tempo** | Holding pace, the main 5K builder | 1 mi easy, **15 min at ~10:00 /mi**, 1 mi easy | 25 min at tempo, or 2 × 12 min |
| **Wed: Easy recovery** | Mileage without extra strain | 2 mi very easy, 4 × 20 s strides | 3 mi easy |

- **Pace zones (starting estimates from a 30:00 5K):**
  - Easy 11:45–12:45 /mi
  - Tempo about 10:00 /mi
  - Interval 400 m about 2:15
  - Goal-mile 400 m 2:00
- **Why easy runs feel slow:** they're meant to. If you can't hold a conversation, slow down. Vector checks this with your Apple Watch heart rate.
- **Time trials:** a mile every 4 weeks, and a 5K at the end of weeks 6 and 12. Paces recalculate after each one.
- **Adjustments from Apple Watch data:**
  - Poor sleep or high resting heart rate makes the day's run easier.
  - Weekly distance won't rise more than about 10% a week, to limit injury risk.
  - Runs move around exams and rain. Any move comes to you as an Action Preview first.
- **Status window (Solo Leveling style):**
  - Your "level" is your VDOT running fitness score.
  - Stats include mile, 5K, cadence, resting heart rate and VO₂ max.

### 8.3 Second Brain (Axiom and Sigma)

**Ingestion:** files are read, filed by course and unit, and indexed.

**Axiom produces** summaries, flashcards (FSRS spaced repetition), practice questions and glossaries. It also extracts deadlines.

**Features:**

- Ask-your-notes with page citations, by voice too.
- Explanations at three levels.
- Exam mode.

**Core Memory (kept by Sigma):**

- Facts, patterns with evidence and confidence, and a daily log.
- Shown as a ring of square dots, like the Fluctlight screen.
- You can edit or delete anything in it.

### 8.4 Executive Assistant (Delta)

- **Morning check-in (7:00 AM):** your top 3 and an energy rating.
- **Evening review (9:30 PM):**
  - Planned versus done is pre-filled.
  - You answer what went well, what didn't and why, and set tomorrow's first task.
- **Weekly rollup (Sunday 6:00 PM):**
  - Completion rate, running summary and trends.
  - Wins and blockers.
  - Three experiments for next week, with last week's experiments checked.
  - A plan for next week for you to authorize.

## 9. UI design language

| Element | Treatment |
|---|---|
| Surfaces | Frosted glass: dark, translucent, blurred and slightly scratched, like the Solo Leveling window. A slow light moves around each panel's edge and a sheen sweeps across it now and then. |
| Nexus | Full-screen live canvas. The selected agent is the large Core. The other agents orbit it as smaller nodes, connected by links with data pulses. Tapping a node flies it to the center while the old one returns to orbit. The whole interface's accent color changes to the new agent's color. |
| Agent colors | Cardinal blue `#3D8BFF` · Ordinal amber `#FFB13D` · Relay teal `#22E3C4` · Axiom violet `#A47BFF` · Vector red `#FF4D6D` · Delta lime `#9DFF4A` · Sigma magenta `#FF4FD8` · Radix silver `#E0E8FF` |
| Status colors | Nominal `#4DFFA6` · Degraded `#FFC24D` · Offline `#FF5470` (always with a text label) |
| Type | Michroma for display, Oxanium for the interface, JetBrains Mono for readouts |
| Navigation | Six views (Nexus, Today, Training, Brain, Review, Access), each with a few focused panels. The approval queue opens in a drawer. ⌘K opens System Call. |
| Usage readout | A low-key `tokens · $` line at the top right. Click it for local versus Claude tokens, spend against the cap, credit left, a month-end projection and the top agents. |
| Motion | Views fade in with a short blur-in. The node swap uses easing and a burst ring. Everything calms down when your device asks for reduced motion. |

## 10. Build order

| Phase | Where | Deliverable |
|---|---|---|
| 0. Foundation ✅ | Mac | Repo, FastAPI + SQLite, web app (no build step), Nexus canvas, brain router with budget guard, usage tracking and readout, chat with every agent |
| 1. Local brains ✅ | Mac + G14 | Ollama on the Mac (Qwen3 4B instruct, ~29 tok/s) and the G14 (Qwen3 8B on the GPU, ~45 tok/s) |
| 1.5. Hub on Proxmox ✅ | Server | Move the hub and database to the Ubuntu VM, Tailscale HTTPS, install on the iPhone, background brain on the server CPU, nightly backups. Moved up from Phase 7 so all devices share one memory early. |
| 1.6. Voice | Mac + G14 + iPhone | Pipecat voice (Whisper, Kokoro, Chatterbox-Turbo), talking to agents out loud |
| 2. Life Dashboard | Mac | Google Calendar, personal Gmail + UMBC, Blackboard feed, Ordinal's briefing, Action Preview queue + **trust rules** |
| 3. Executive Assistant | Mac | Delta check-ins and weekly rollup, Core Memory v1 |
| 4. Running | Mac + iPhone | Health Auto Export, Vector, pace zones, run plan |
| 5. Second Brain | Mac | Axiom ingestion, notes, flashcards, exam mode |
| 6. Full Workforce | Mac | Relay drafts, Sigma patterns, Radix, ActivityWatch |
| 7. Hardening | Server | Push notifications, login with a passkey, Postgres if SQLite ever gets slow, restore drills |

## 11. Repo layout

```
apps/web/            Web app: plain HTML/CSS/JS modules, no build step or Node.js, served by the API
apps/link/           Cardinal Link (Tauri): local GPU voice, device tools, ActivityWatch bridge (Phase 6)
services/api/        FastAPI app (Python, uv): agents, brain router, usage, later integrations
  config/            agents.yaml, routing.yaml, brains.example.yaml (brains.yaml is git-ignored)
services/voice/      Pipecat pipeline and speech engine adapters (Phase 1)
scripts/dev.sh       Run it locally
infra/               Server setup script (systemd, Tailscale HTTPS, nightly backups)
design/              Concept page; reference screenshots are local-only
data/                Local SQLite database (git-ignored); Postgres on the server
```

## 12. Setup you'll need (before Phase 1)

**Anthropic API key, about 5 minutes.**

1. Go to **console.anthropic.com** and sign up. It's a separate account from claude.ai.
2. **Billing:** buy **$10–20 in prepaid credits**. Leave **auto-reload off**, so you can never spend more than you loaded.
3. **Settings → Limits:** set a monthly spend limit of **$20**.
4. **API Keys → Create Key**, named "cardinal". Copy it once.
5. When we start Phase 1, paste it into the `.env` file on your Mac as `ANTHROPIC_API_KEY=...`. **Never paste it into chat** and never commit it; `.gitignore` already blocks `.env`.

**Local models:** install **Ollama** on the G14 (Windows) and the Mac. I'll give the exact commands in Phase 1.
