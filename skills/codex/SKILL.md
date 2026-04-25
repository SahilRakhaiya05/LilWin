# Codex — provider reference

> Provider-wide notes. For *per-character* deployment recipes see
> `skills/deci/SKILL.md`.

## CLI detection
- Use the stable `codex` CLI (OpenAI's `codex` binary). LilWin
  prefers the `.exe` over `.cmd` wrappers.
- Set `OPENAI_API_KEY` in the environment.

## Session behavior
- One-shot per message. The working directory follows `/cd <dir>`.
- Errors from the CLI are surfaced into the popover as an error line so
  you don't have to look in a separate terminal.

## Output conventions
- Ask Codex for patches / diffs explicitly — it's happy to emit them in
  fenced blocks, which LilWin renders with Pygments highlighting.
