# Skills — per-character session deployment recipes

A "skill" here is a short, MrCat-readable recipe that tells LilWin how
to **deploy a specific character** when it's spawned as a walker. Each
walker runs its own independent CLI session, so the skill covers:

- which AI provider to pair with the character
- a preferred working directory
- whether the session should start app-managed or linked to a terminal
- model flags / env hints
- the prompt / output style this character brings to the session

These docs are **descriptive** — the app reads character mapping from
`config/characters.json` and `config/settings.json`, not from the skill
files. Skills are the source of truth for *you and your team* so every
new walker behaves consistently.

## Layout

```
skills/
  README.md                 <- this file
  bruce/SKILL.md            <- per-character deployment recipes
  jazz/SKILL.md
  deci/SKILL.md
  MrCat/SKILL.md
  claude/SKILL.md           <- provider reference notes (command habits)
  gemini/SKILL.md
  codex/SKILL.md
  copilot/SKILL.md
  opencode/SKILL.md
  openclaw/SKILL.md
```

- `skills/<character>/SKILL.md` — **primary** deployment recipe. Answer
  the questions "when should I spawn this walker?" and "what session
  settings does it expect?"
- `skills/<provider>/SKILL.md` — reference notes for the CLI itself
  (flags, restart policy, paste-bridge quirks). Shared across every
  character that uses that provider.

## How to use

1. Open **Settings → Open skills folder** (or browse `skills/` directly).
2. Edit `skills/<character>/SKILL.md` to describe your deployment recipe
   for that character: provider, cwd, model, prompt norms.
3. Edit `skills/<provider>/SKILL.md` only for provider-wide notes that
   apply to every character using that CLI.
4. When you `/spawn <character>` from the popover (or Tray → Add walker),
   the new walker inherits the primary walker's provider and theme, then
   runs in its own session. Consult the character SKILL.md to know how
   you'd typically use that walker.

## Adding a new character recipe

1. Add the character to `config/characters.json`.
2. Create `skills/<character>/SKILL.md` describing:
   - Default provider and why.
   - Working directory convention (e.g. `C:\dev\repo-a`).
   - Session mode (app-managed vs linked terminal).
   - Prompt style / scope of responsibility.
3. Restart the app; the tray → Add walker submenu auto-lists every
   character from `config/characters.json`.
