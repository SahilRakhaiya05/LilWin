# OpenCode — provider reference

> Provider-wide notes. Pair with any character via
> `config/settings.json → providerCharacterMap.OpenCode`.

## CLI detection
- Install the OpenCode CLI and make sure the binary is on `PATH`.

## Session behavior
- One-shot per message. Respects `/cd <dir>` just like the other
  one-shot providers.

## Notes
- Use `/restart` if the session ever gets stuck — LilWin will kill
  and respawn the process cleanly.
