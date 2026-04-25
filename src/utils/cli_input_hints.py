"""Build input-completion strings from installed CLIs (--help) plus curated hints."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set

from utils.cli_binary import find_cli_command

# lil-agents UI shortcuts (handled before the message is sent to the provider CLI)
APP_SHORTCUTS = [
    "/clear",
    "/copy",
    "/help",
    "/resetpos",
    "/restart",
    "/session",
    "/cd",
    "/pwd",
    "/ls",
    "/run",
    "/link",
    "/unlink",
    "/who",
    "/theme",
    "/size",
    "/spawn",
    "/provider",
    "/channel",
    "/tell",
    "/collab",
    "//clear",
    "//copy",
    "//help",
    "//restart",
    "//session",
]

# Common Claude Code–style slash messages (skills / built-ins); sent through to the CLI as user text.
_PROVIDER_SLASH_HINTS: dict[str, List[str]] = {
    "Claude": [
        "/compact",
        "/model",
        "/memory",
        "/permissions",
        "/mcp",
        "/config",
        "/cost",
        "/init",
        "/bug",
        "/login",
        "/logout",
    ],
    "Gemini": [],
    "Codex": [],
    "Copilot": [],
    "OpenCode": [],
}

_CLI_BINARY_NAME: dict[str, str] = {
    "Claude": "claude",
    "Gemini": "gemini",
    "Codex": "codex",
    "Copilot": "copilot",
    "OpenCode": "opencode",
}


def _popen_help_argv(exe: str) -> List[str]:
    low = exe.lower()
    if sys.platform == "win32" and low.endswith((".bat", ".cmd", ".ps1")):
        return ["powershell.exe", "-NoProfile", "-Command", f"& '{exe}' --help"]
    return [exe, "--help"]


def _run_help_text(exe: str, timeout_sec: float = 4.0) -> str:
    argv = _popen_help_argv(exe)
    env = os.environ.copy()
    if sys.platform == "win32":
        appdata = env.get("APPDATA", "")
        if appdata:
            npm_bin = str(Path(appdata) / "npm")
            env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
    kwargs: dict = dict(
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
        cwd=str(Path.home()),
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(argv, **kwargs)
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


_SLASH_IN_HELP = re.compile(r"/[a-z][a-z0-9-]*", re.IGNORECASE)
_LONG_OPT = re.compile(r"(?:^|\s)(--[a-z][a-z0-9-]*(?:-[a-z][a-z0-9-]*)*)", re.IGNORECASE)


def _tokens_from_help(help_text: str) -> Set[str]:
    found: Set[str] = set()
    for m in _SLASH_IN_HELP.finditer(help_text):
        found.add(m.group(0).lower())
    for m in _LONG_OPT.finditer(help_text):
        found.add(m.group(1).lower())
    return found


def build_completion_list(provider_name: str) -> List[str]:
    """Ordered, deduped strings for the chat-line completer (slash + real CLI flags from --help)."""
    seen: Set[str] = set()
    out: List[str] = []

    def add(s: str) -> None:
        t = s.strip()
        if not t or t in seen:
            return
        seen.add(t)
        out.append(t)

    for s in APP_SHORTCUTS:
        add(s)
    for s in _PROVIDER_SLASH_HINTS.get(provider_name, []):
        add(s)

    bin_name = _CLI_BINARY_NAME.get(provider_name)
    if not bin_name:
        return out

    exe = find_cli_command(bin_name)
    if not exe:
        return out

    blob = _run_help_text(exe)
    if blob:
        for tok in sorted(_tokens_from_help(blob)):
            add(tok)

    return out
