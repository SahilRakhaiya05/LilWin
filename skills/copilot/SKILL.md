# Copilot — provider reference

> Provider-wide notes. For *per-character* deployment recipes see
> `skills/MrCat/SKILL.md`.

## CLI detection
- Requires the GitHub Copilot CLI (`gh copilot`). Authenticate once with
  `gh auth login` before starting LilWin.

## Session behavior
- One-shot per message, spawned with the current `/cd` working
  directory. Copilot is strongest when the cwd contains the repo it
  should reason over.

## Output conventions
- Copilot mixes prose and fenced code; the popover auto-highlights the
  code blocks.
