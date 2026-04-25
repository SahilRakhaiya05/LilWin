# Bruce — deployment recipe

## When to spawn
- Feature/infra work where a calm, builder-voice walker keeps focus.
- Default home for the Claude provider.

## Session defaults
- Provider: **Claude** (matches `providerCharacterMap.Claude = bruce`).
- Working directory: your current repo. Use `/cd <repo>` in the popover if
  Bruce is spawned outside it.
- Session mode: **app-managed** (LilWin spawns `claude.exe` for Bruce
  directly). Only switch to a linked terminal via `/link` if you need the
  bridge.

## Prompt style
- Bruce opens each session with a builder greeting ("Hey {user}, Bruce
  here — what are we building?"). Keep instructions actionable.
- Prefer command-first answers, then the rationale.

## Recommended flags
- Claude: rely on default streaming JSON output (auto-detected).
- Leave `ANTHROPIC_API_KEY` in your environment; LilWin does not
  relay it.

## Multi-walker tips
- If you already have Bruce running on repo A, spawn a second Bruce for
  repo B via Tray → Add walker → Bruce, then `/cd <repo-B>` in the new
  walker's popover. Each walker has its own cwd and history.
