# LilWin

<img width="2012" height="781" alt="LilWin" src="https://github.com/user-attachments/assets/6673ba19-161e-463a-99ed-9f510734a649" />

---
**AI companions on your Windows desktop** — tiny animated walkers on the taskbar line, each with a **real CLI** behind it (Claude, Gemini, Codex, Copilot, OpenCode, OpenClaw). One click opens chat; multiple walkers can **share a channel**, **ping each other’s models**, and **hand off turns** like a mini agent room.

> *Same vibe as a desktop pet — except it runs your actual terminal tools and can team up with your other walkers.*

---

## What you get

- **Tray-first** — lives in the notification area; **double-click** the icon to open chat.
- **One primary walker** by default; spawn more from the tray or `/spawn`.
- **Shared channels** — name a room, join from each walker, then `/tell` or **auto-collab** so models pass work between providers.
- **Markdown + Pygments** in the popover for readable replies and code blocks.
- **In-app CLI sessions** or optional **link** to Cursor / VS Code / Windows Terminal.
- **Themes:** Peach, Midnight, Cloud, Moss.
- **`skills/<character>/SKILL.md`** — optional playbooks per character (descriptive docs, not executed by the app).

---

## Requirements

- **Windows 10/11**
- **Python 3.11+**

### Install provider CLIs first (before expecting chat to work)

LilWin is a **front end** to real terminal tools. **Install the CLIs** for the providers you want (Claude, Gemini, Codex, Copilot, OpenCode, OpenClaw, etc.), make sure they run from a normal **Command Prompt or PowerShell**, then put them on your **`PATH`** the same way you do for daily dev work. If the CLI is missing or not on `PATH`, the app cannot talk to that provider — see [Providers](#providers) and [Troubleshooting](#troubleshooting).

**Stack:** Python, **PyQt6** (+ WebEngine for the popover), Markdown, Pygments.

---

## Install & run

From the repo root:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python lilwin.py
```

**Entry points (equivalent):** from the repo root, `python lilwin.py` is the same as `python src/main.py`. For development you can run `main` directly: `python src/main.py`.

The tray icon shows immediately. The **primary** walker appears on the desktop unless Settings require an external CLI first.

> **Why a venv?** Keeps PyQt6 and the rest isolated from your global Python. If you already manage environments another way, `pip install -r requirements.txt` and `python lilwin.py` (or `python src/main.py`) is enough.

---

## Build standalone `.exe`

PyInstaller is listed in `requirements.txt`. From the repo root:

```powershell
python scripts/build.py
```

Use `python scripts/build.py --clean` to wipe `build/` and `dist/` first.

Output: **`build/dist/LilWin-Bros-agent.exe`**, with default `settings.json` and `characters.json` next to the exe for a first run without the full repo. See `scripts/build.py` for details.

---

## Configuration & data

| Location | Role |
|----------|------|
| `config/settings.json` | App preferences (relative to repo root when developing). |
| `config/characters.json` | Character definitions used by the app. |
| `skills/<character>/SKILL.md` | Optional human-written notes per character (not executed). |

**Channels are in-memory** inside this process only — they are not persisted or synced to the network. **Provider authentication** is whatever each CLI already uses (API keys, browser login, etc.); see each vendor’s documentation. Optional local data (logs, some secrets) may live under `%LOCALAPPDATA%\lil-agents\` on Windows.

---

## Privacy

LilWin does **not** require an app account, does not run a lil-agents “cloud” for your chats, and is designed to keep **normal use local** to your machine, with **provider traffic** going through **the CLIs and vendors you choose**. Full plain-language details: **[PRIVACY.md](./PRIVACY.md)** (last updated **March 22, 2026**).

---

## System tray

| Menu | What it does |
|------|----------------|
| **Open chat** | Focus the primary walker’s popover. |
| **Default Provider** | Default CLI for **new** spawns; each walker can still **`/provider`** locally. |
| **Walkers** | Per **character:** **Show on desktop** (extra companion) + **provider** actions to spawn **another session** for that character. |
| **Theme** | Peach · Midnight · Cloud · Moss |
| **Settings** | Preferences. |
| **Exit** | Quit. |

The **primary** walker stays on screen; its **Show on desktop** stays checked. Extras: toggle other characters or spawn more sessions under **Walkers**.

---

## Channels & collaboration

Walkers can join the **same named channel** (in-memory, this app only). That enables **`/tell`** (one line to another walker’s provider) and **`/collab`** (bounded **auto handoffs** between paired walkers on the channel).

### `/channel`

| Usage | Effect |
|--------|--------|
| `/channel` or `/channel status` | Current channel, or hint to create/join. |
| `/channel create <name>` | Create and join a channel (normalized to lowercase). |
| `/channel join <name>` | Join an existing channel. |
| `/channel leave` | Leave the current channel. |
| `/channel members` | List members with **display name**, **provider**, and **(auto)** if auto-collab is on. |

**`/tell`** and **pairing for `/collab`** only work when **both walkers are in the same channel**. Normal typing still goes to **this** walker’s CLI only unless you turn on **auto-collab** (see below).

### `/tell`

```text
/tell <character> <message>
```

Sends **one line** to **that** walker’s provider as a **delegated channel task**. Does **not** use the current tab’s provider for that line. The UI shows a small handoff card. Target must be on the **same channel**.

### `/collab`

| Usage | Effect |
|--------|--------|
| `/collab` | Status: channel, auto-collab on/off, role, max rounds, partner (if any). |
| `/collab on` | Enable auto-collaboration (optional: partner name, role, max rounds — see below). |
| `/collab on Jazz` | Shorthand: pair with walker **Jazz**, enable auto-collab (same channel required). |
| `/collab off` | Disable auto-collab and clear partner link. |
| `/collab clear` | Clear partner only; leaves auto-collab flag as-is. |
| `/collab <name>` | Same idea as **`/collab on <name>`** — pair with that walker. |

**Join a channel first** on both walkers before pairing (`/channel join myroom` on each). When **auto-collab** is **on**, your **next user message** starts a **collab round**: peers get a system line, and after **this** walker’s assistant finishes, the hub can **forward** the reply and **prompt the next** walker (paired or round-robin, depending on setup). **Max rounds** caps how long the ping-pong runs.

**Linked popover** (external terminal): walkers linked that way **cannot** accept inbound channel prompts — use in-app sessions for full channel features.

---

## Popover commands (quick reference)

Use **`/help`** in the popover for the live list. Summary:

**Shell:** `/pwd`, `/cd`, `/ls`, `/run`  
**Sessions:** `/link`, `/link N`, `/unlink`, `/session`, `/who`, `/restart`  
**UI:** `/help`, `/clear`, `/copy`, `/resetpos`, `/theme`, `/size`, `/spawn`, `/provider`  
**Multi-walker:** `/channel …`, `/tell …`, `/collab …`  

**Linked mode:** popover-only commands use **`//`** (e.g. `//clear`); single **`/`** goes to the real CLI.

---

## Providers

**Claude**, **Codex**, **Copilot**, **Gemini**, **OpenCode**, **OpenClaw** — install each CLI you need and ensure it runs from a normal shell. On Windows, **Claude** often works best with the native **`.exe`** on `PATH`, not only a `.cmd` shim.

---

## Skills folder

`skills/<character>/SKILL.md` describes how **that** character should be used (provider, cwd, flags, tone). **Descriptive only** — the app does not run them. Templates live under `skills/`; open the folder from **Settings**.

---

## Settings (essentials)

- **Desktop character visibility** — always, or only when an external CLI runs.  
- **External CLI monitor** — surface outside activity.  
- **Extra walkers** — optional “auto-deploy every character on startup” (off by default; tray **Walkers** is the manual path).  
- **Provider ↔ character** mapping, walk timing, **OpenClaw** gateway (Advanced), config path, **Open skills folder**.  

Tabs: **Essentials**, **Advanced**, **Characters**.

---

## Tests

```bash
pytest tests/
```

For UI-related tests, `pytest-qt` is in `requirements.txt`. Run from the repo root with your venv active.

---

## Troubleshooting

| Problem | Try |
|---------|-----|
| **CLI not found** | Install the provider CLI; same `PATH` as the shell you use to start LilWin. |
| **Walker never shows** | Settings → desktop visibility; start an external CLI if required; use **Walkers** after launch. |
| **Wrong cwd** | `/pwd`, then `/cd <path>`. |
| **`/tell` / collab errors** | Both walkers **`/channel join`** the **same** name; spawn the target first. |
| **Linked terminal** | `/unlink` for in-app session; linked targets won’t take channel prompts. |
| **Too many windows** | Close extras; disable roster auto-deploy. |

---

## Roadmap

The list below is **not a commitment** — it groups ideas so contributors and users can see direction. Items move as the project learns what matters most.

### Near term (quality & polish)

- Tray UX polish (icons, state clarity, “busy” vs idle).
- Onboarding: first-launch tips (tray, `/help`, one channel example).
- Popover performance on long transcripts (virtualization, chunking, or cap warnings).
- Session cards: clearer per-session history in the popover or side panel.
- Hardening: more automated tests around sessions, channels, and edge providers.

### Medium term (distribution & integration)

- **Installer** (e.g. signed MSI/EXE, Start menu shortcut, auto-start option).
- Optional **auto-update** channel for the built exe.
- Deeper **Windows Terminal / IDE** link presets and docs per editor.
- Accessibility pass (focus order, high-contrast theme tweaks, screen-reader where feasible).

### Ideas / later (bigger bets)

- Optional **persistence** for channel state (file or local DB) — with clear privacy model.
- **Plugin or script hooks** for power users (export transcript, custom slash commands).
- Theming API (user CSS or palette packs) if demand is there.
- Community **provider adapters** (documented contract + examples).

### Already shipped (so you know what’s in scope)

- Multi-walker, shared channels, `/tell`, `/collab`, linked vs in-app sessions, themes, skills folder as documentation, PyInstaller build path.

*Want to add something?* Open an issue with **use case** + **acceptance** (one paragraph each); PRs that match existing style and tests are welcome.

---

## Credit

Windows **Python / PyQt6** port inspired by **lil-agents (macOS)** by **Ryan Stephen:**

The “Jazz and Bruce characters beside the taskbar” idea comes from there; this repo is an independent build. See [LICENSE](./LICENSE) (MIT + notice).

---

## License

**MIT** — [LICENSE](./LICENSE).  
**Privacy** — [PRIVACY.md](https://lilwin.projectagent.tech/privacy.html).
