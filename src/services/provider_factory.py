from dataclasses import dataclass
from typing import Optional

from utils.cli_binary import find_cli_command


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    command: Optional[str]


PROVIDER_SPECS = {
    "Claude": ProviderSpec("Claude", "claude"),
    "Codex": ProviderSpec("Codex", "codex"),
    "Copilot": ProviderSpec("Copilot", "copilot"),
    "Gemini": ProviderSpec("Gemini", "gemini"),
    "OpenCode": ProviderSpec("OpenCode", "opencode"),
    "OpenClaw": ProviderSpec("OpenClaw", None),
}


def is_provider_available(name: str) -> bool:
    spec = PROVIDER_SPECS.get(name)
    if spec is None:
        return False
    if spec.command is None:
        return True
    return find_cli_command(spec.command) is not None


def unavailable_message(name: str) -> str:
    spec = PROVIDER_SPECS.get(name)
    if spec is None:
        return f"Unknown provider: {name}"
    if spec.command is None:
        return ""
    return (
        f"{name} CLI was not found in PATH. Install it, then restart the app. "
        f"Expected command: {spec.command}"
    )
