# MrCat — deployment recipe

## When to spawn
- Generalist companion. Sits between Bruce and Deci — useful for
  non-code help (docs, checklists, explaining errors).
- Default home for the Copilot provider.

## Session defaults
- Provider: **Copilot** (matches `providerCharacterMap.Copilot = MrCat`).
- Working directory: the folder Copilot should look at for file tools.
- Session mode: **app-managed**. Switch to `/link` when you want the
  walker to mirror an IDE terminal you're already driving.

## Prompt style
- MrCat greets with "Hey {user}, what do you need?". Treat like a
  friendly pair-programmer; no persona to play against.
- Works well with short prompts.

## Recommended flags
- GitHub Copilot CLI: log in once via `gh copilot` before spawning.
- Authentication is stored in the gh config, not in LilWin.

## Multi-walker tips
- MrCat is the safest choice for experimental walkers — spawn a temporary
  MrCat to try a new provider mapping without disturbing Bruce/Jazz
  sessions.
