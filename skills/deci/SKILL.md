# Deci — deployment recipe

## When to spawn
- Tight, code-first sessions. Deci leans compiler-voice: precise, terse.
- Default home for the Codex provider.

## Session defaults
- Provider: **Codex** (matches `providerCharacterMap.Codex = MrCat` by
  default; swap to `deci` if you want Deci-led Codex).
- Working directory: the repo you're shipping from. Codex is strongest
  when it can read the code it's editing.
- Session mode: **app-managed** unless you want Codex to run inside a
  real terminal — `/link` keeps it behaving normally with LilWin as
  the input surface.

## Prompt style
- Deci opens with "Deci online. Hey {user} — what does the code need?".
  Keep prompts structured (problem, constraints, deliverable).
- Ask for diffs and tests, not prose summaries.

## Recommended flags
- Codex: use the latest stable CLI. Set `OPENAI_API_KEY` in env.

## Multi-walker tips
- Deci + Bruce is a strong pairing for a review loop: Bruce drafts, Deci
  ships the compile-clean version. Spawn both from Tray → Add walker.
