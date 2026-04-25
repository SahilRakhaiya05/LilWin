# Claude — provider reference

> Provider-wide notes. For *per-character* deployment recipes see
> `skills/<character>/SKILL.md` (e.g. `skills/bruce/SKILL.md`).

## CLI detection
- LilWin prefers the native `claude.exe` binary over `.cmd`/`.ps1`
  wrapper scripts on Windows. Install the official Anthropic CLI and
  make sure the `.exe` is on `PATH` first.
- Authenticate via your normal flow (`ANTHROPIC_API_KEY` env var or the
  CLI's login command). LilWin does not proxy auth.

## Session behavior
- The app uses the streaming JSON protocol (long-lived process). Exit
  codes are surfaced into the popover as an error line.
- `/restart` kills and respawns the Claude process cleanly.
- `/cd <dir>` restarts the CLI with the new working directory so file
  tools write to the expected location.

## Linked-mode tips
- When `/link`-bound to a Cursor / VS Code terminal, single `/commands`
  pass through to the CLI. Use `//clear`, `//copy`, `//help`,
  `//restart`, `//session` for LilWin admin commands.
