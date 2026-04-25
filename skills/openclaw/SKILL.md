# OpenClaw — provider reference

> Provider-wide notes. OpenClaw uses the **gateway URL + auth token**
> from Settings, not a local CLI binary.

## Connection
- Configure **Gateway URL** (e.g. `ws://localhost:3001`) and an optional
  **Auth token** in Settings. Tokens are stored in the local secrets
  file (see `utils/secrets.py`).

## Session behavior
- Runs over a WebSocket to your OpenClaw gateway. Terminal linking and
  the process-scan detection are disabled for OpenClaw automatically —
  it's always an app-managed session.

## Notes
- Errors from the gateway (auth, disconnect, malformed frames) are
  surfaced as popover error lines. Use `/restart` to re-establish the
  WebSocket.
