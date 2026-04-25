# Jazz — deployment recipe

## When to spawn
- Creative exploration, prototyping, quick-and-loose sessions.
- Default home for the Gemini provider.

## Session defaults
- Provider: **Gemini** (matches `providerCharacterMap.Gemini = jazz`).
- Working directory: project or scratch folder. Jazz is happy in
  `~/Downloads`-style sandboxes.
- Session mode: **app-managed**. Jazz benefits from LilWin's
  duplicate-output filter for Gemini's plain-text stream.

## Prompt style
- Jazz greets the user by name ("Hey {user}, Jazz on the mic…"). Keep
  the session conversational; good for brainstorming.
- Ask Jazz to reason in short riffs before committing to a plan.

## Recommended flags
- Gemini: install Google's official `gemini` CLI; LilWin prefers the
  `.exe` over `.cmd` shim when both exist.
- Set `GEMINI_API_KEY` in your environment.

## Multi-walker tips
- Jazz pairs well with Bruce on a shared desktop: Bruce drives Claude for
  implementation, Jazz drives Gemini for ideation. Both use separate cwds
  so file operations don't collide.
