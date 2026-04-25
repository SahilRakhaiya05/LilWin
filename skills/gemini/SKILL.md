# Gemini — provider reference

> Provider-wide notes. For *per-character* deployment recipes see
> `skills/jazz/SKILL.md`.

## CLI detection
- Install Google's official `gemini` CLI. LilWin prefers the
  native executable over any wrapper shim on Windows.
- Set `GEMINI_API_KEY` (or the equivalent env var for your Gemini
  account) before starting the app.

## Session behavior
- Gemini is treated as a **one-shot-per-message** session: a new process
  is spawned for each prompt. LilWin respects the current `/cd`
  working directory so file operations land in the right place.
- Plain-text output is buffered until LilWin knows whether Gemini is
  streaming JSON events; this prevents the old duplicate-output spam.

## Guardrails
- Ask Gemini to respect the active cwd when writing files.
- If Gemini claims file creation, expect the exact path in the reply so
  it's easy to verify with `/ls`.
