from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class TerminalSessionInfo:
    """One integrated-terminal buffer file (Cursor / VS Code family)."""

    session_key: str
    session_id: str
    path: str
    active_command: str
    cwd: str
    provider: str
    title: str
    source: str


def _workspace_slug(repo_root: str) -> str:
    root = os.path.abspath(repo_root)
    drive, tail = os.path.splitdrive(root)
    parts: List[str] = []
    if drive:
        parts.append(drive.rstrip(":").replace("\\", "-").replace("/", "-"))
    parts.extend([part for part in tail.replace("/", os.sep).split(os.sep) if part])
    return "-".join(parts)


def get_terminals_dir(repo_root: str) -> Path:
    return Path.home() / ".cursor" / "projects" / _workspace_slug(repo_root) / "terminals"


def _stable_session_key(path: str) -> str:
    """Stable id for a terminal buffer path (full hash — avoids collisions between tabs)."""
    norm = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()


def _infer_source(path: Path) -> str:
    lower = str(path).lower()
    if ".cursor" in lower and "projects" in lower:
        return "Cursor"
    if "code - insiders" in lower:
        return "VS Code Insiders"
    if "vscodium" in lower:
        return "VSCodium"
    if "\\code\\" in lower or "/code/" in lower:
        return "VS Code"
    return "IDE"


def iter_integrated_terminal_txt_files() -> List[Path]:
    """All known integrated-terminal buffer paths (any workspace), newest paths last within each folder."""
    seen: set[str] = set()
    ordered: List[Path] = []

    def add_dir(term_dir: Path) -> None:
        if not term_dir.is_dir():
            return
        for p in sorted(term_dir.glob("*.txt")):
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                ordered.append(p)

    home = Path.home()
    cursor_projects = home / ".cursor" / "projects"
    if cursor_projects.is_dir():
        for term_dir in cursor_projects.rglob("terminals"):
            if term_dir.is_dir() and term_dir.name == "terminals":
                add_dir(term_dir)

    appdata = os.environ.get("APPDATA")
    if appdata:
        for product in ("Code", "Code - Insiders", "VSCodium"):
            ws = Path(appdata) / product / "User" / "workspaceStorage"
            if not ws.is_dir():
                continue
            try:
                for entry in ws.iterdir():
                    if entry.is_dir():
                        add_dir(entry / "terminals")
            except OSError:
                continue

    return ordered


def read_terminal_file(path: str) -> Tuple[Dict[str, str], str]:
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}, ""

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            meta_block = parts[1]
            body = parts[2]
            meta: Dict[str, str] = {}
            lines = meta_block.splitlines()
            idx = 0
            while idx < len(lines):
                line = lines[idx]
                if ":" not in line:
                    idx += 1
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if value == "|":
                    block_lines: List[str] = []
                    idx += 1
                    while idx < len(lines) and (lines[idx].startswith("  ") or not lines[idx].strip()):
                        block_lines.append(lines[idx][2:] if lines[idx].startswith("  ") else "")
                        idx += 1
                    meta[key] = "\n".join(block_lines).strip()
                    continue
                meta[key] = value
                idx += 1
            return meta, body.lstrip("\r\n")
    return {}, raw


# Order matters: longer/more-specific needles first so "opencode" is matched
# before the plain "code" fallback that lives inside VS Code terminal exe names.
_PROVIDER_NEEDLES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("OpenClaw", ("openclaw",)),
    ("OpenCode", ("opencode",)),
    ("Copilot",  ("copilot",)),
    ("Gemini",   ("gemini",)),
    ("Codex",    ("codex",)),
    ("Claude",   ("claude",)),
)

# A shell prompt followed by the provider binary, e.g.::
#
#     PS C:\Users\foo> claude -p ...
#     C:\Users\foo> claude
#     $ claude --help
#     > claude
#
# Only invocations like these in the terminal body count as "this terminal is
# running the provider CLI".  Random log lines that mention the word "claude"
# (e.g. ``provider=Claude`` from lil-agents' own stdout) must NOT match — that
# was the long-standing false-positive that pinned the walker to the terminal
# running lil-agents itself.
_PROMPT_INVOCATION = re.compile(
    r"(?:^|\n)"
    r"(?:[A-Za-z]:\\[^\n]*?|PS\s+[A-Za-z]:\\[^\n]*?|[^\n]*?)"   # optional prompt prefix
    r"[>$#]\s+"                                                  # shell prompt sigil
    r"(?:&\s*[\"']?[^\n\"']*?[\\/])?"                            # optional full-path prefix
    r"(claude|gemini|codex|copilot|opencode|openclaw)"
    r"(?:\.(?:cmd|bat|exe|ps1))?"
    r"(?:\s|$)",
    re.IGNORECASE,
)


def _provider_from_active_command(active_command: str) -> Optional[str]:
    """Match based only on the IDE-reported active shell command.

    The IDE terminal file header line ``active_command: ...`` is the most
    reliable signal: the IDE marks the shell busy with the exact command line
    the user typed, so ``claude -p …`` is unambiguous.  We accept both a bare
    invocation (``claude``) and a path-prefixed one (``C:\\…\\claude.exe``).
    """
    cmd = (active_command or "").strip()
    if not cmd:
        return None
    # Split off shell redirection noise so we can inspect the first "word".
    head = cmd.replace("\t", " ").split(" ", 1)[0].strip('"\'').lower()
    if not head:
        return None
    if head.endswith((".cmd", ".bat", ".exe", ".ps1")):
        head = head.rsplit(".", 1)[0]
    # Keep only the last path component: "C:\\…\\claude" -> "claude".
    head = head.replace("/", "\\").rsplit("\\", 1)[-1]
    for provider, needles in _PROVIDER_NEEDLES:
        if head in needles:
            return provider
    return None


def _provider_from_body_invocation(body: str) -> Optional[str]:
    """Spot a shell-prompt invocation line like ``PS C:\\…> claude`` in the buffer."""
    if not body:
        return None
    # Only scan the tail of the buffer so long sessions remain cheap.
    window = body[-4000:]
    for match in _PROMPT_INVOCATION.finditer(window):
        token = match.group(1).lower()
        for provider, needles in _PROVIDER_NEEDLES:
            if token in needles:
                return provider
    return None


def _provider_for_terminal(meta: Dict[str, str], body: str) -> Optional[str]:
    """Guess which CLI provider (if any) is active in this integrated terminal.

    We require one of two strong signals:

    1. ``meta['active_command']`` starts with the provider binary (IDE marks
       the shell as busy running that command — the gold-standard signal).
    2. The body contains a shell-prompt line that invokes the provider binary.

    We deliberately do NOT fall back to a substring match against the full
    buffer — that matched any stdout that merely *mentioned* the word
    ``claude`` / ``gemini`` / etc.  That produced false positives like the
    terminal running ``python src/main.py`` (which logs ``provider=Claude``)
    being mis-identified as a Claude CLI session.
    """
    cmd_hit = _provider_from_active_command(meta.get("active_command", ""))
    if cmd_hit:
        return cmd_hit
    return _provider_from_body_invocation(body)


def list_terminal_sessions(repo_root: Optional[str] = None, provider_name: Optional[str] = None) -> List[TerminalSessionInfo]:
    """
    Discover integrated-terminal buffers across Cursor and VS Code workspaceStorage.
    When ``repo_root`` is set, sessions under that workspace's Cursor terminals folder are listed first.
    """
    paths = iter_integrated_terminal_txt_files()
    priority_prefix: Optional[str] = None
    if repo_root:
        try:
            priority_prefix = str(get_terminals_dir(repo_root).resolve()) + os.sep
        except OSError:
            priority_prefix = None

    def sort_key(p: Path) -> Tuple[int, str]:
        sp = str(p.resolve())
        if priority_prefix and sp.startswith(priority_prefix):
            return (0, sp)
        return (1, sp)

    paths = sorted(paths, key=sort_key)

    infos: List[TerminalSessionInfo] = []
    for path in paths:
        meta, body = read_terminal_file(str(path))
        provider = _provider_for_terminal(meta, body)
        if provider_name and provider != provider_name:
            continue
        stem = path.stem
        sk = _stable_session_key(str(path))
        active = meta.get("active_command", "").strip()
        cwd = meta.get("cwd", "").strip()
        source = _infer_source(path)
        title = f"{source} · {stem}"
        if active:
            title += f" — {active[:72]}{'…' if len(active) > 72 else ''}"
        elif provider:
            title += f" — {provider}"
        infos.append(
            TerminalSessionInfo(
                session_key=sk,
                session_id=stem,
                path=str(path),
                active_command=active,
                cwd=cwd,
                provider=provider or "",
                title=title,
                source=source,
            )
        )
    return infos


def count_integrated_provider_sessions_with_active_command(
    repo_root: Optional[str], provider_name: str
) -> int:
    """
    How many Cursor / VS Code integrated-terminal buffers match ``provider_name`` and report
    ``active_command`` in the file header (IDE marks the shell as busy — e.g. a CLI still running).
    """
    return len(list_active_integrated_provider_sessions(repo_root, provider_name))


def list_active_integrated_provider_sessions(
    repo_root: Optional[str], provider_name: str
) -> List[TerminalSessionInfo]:
    """Matching integrated-terminal buffers whose metadata reports a busy shell (``active_command``)."""
    out = [s for s in list_terminal_sessions(repo_root, provider_name) if s.active_command]
    out.sort(key=lambda s: s.path)
    return out


def _extract_text_from_claude_content(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif "text" in block and isinstance(block["text"], str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    if isinstance(content, dict):
        return _extract_text_from_claude_content(content.get("content"))
    return ""


def _parse_claude_code_stream_buffer(body: str) -> List[Tuple[str, str]]:
    """Best-effort parse of Claude Code --output-format stream-json lines in a terminal log."""
    out: List[Tuple[str, str]] = []
    for line in body.splitlines()[-1200:]:
        s = line.strip()
        if len(s) < 10 or not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "assistant":
            msg = obj.get("message") or {}
            text = _extract_text_from_claude_content(msg.get("content"))
            if text:
                out.append(("Claude", text))
        elif t == "user":
            msg = obj.get("message") or {}
            text = _extract_text_from_claude_content(msg.get("content"))
            if text:
                out.append(("User", text))
        elif t == "result" and isinstance(obj.get("result"), str):
            out.append(("System", obj["result"][:2000]))
    return out


def parse_terminal_messages(provider_name: str, body: str) -> List[Tuple[str, str]]:
    provider = provider_name.lower()
    if provider == "gemini":
        return _parse_gemini_messages(body)
    if provider == "claude":
        streamed = _parse_claude_code_stream_buffer(body)
        if streamed:
            merged: List[Tuple[str, str]] = []
            for sender, chunk in streamed:
                if merged and merged[-1][0] == sender:
                    merged[-1] = (sender, (merged[-1][1] + "\n" + chunk).strip())
                else:
                    merged.append((sender, chunk))
            return merged[-80:]
    trimmed = body.strip()
    if not trimmed:
        return []
    return [("System", trimmed[-4000:])]


def _parse_gemini_messages(body: str) -> List[Tuple[str, str]]:
    messages: List[Tuple[str, str]] = []
    sender: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        nonlocal sender, buffer
        if sender and any(part.strip() for part in buffer):
            text = "\n".join(buffer).strip()
            if text:
                messages.append((sender, text))
        sender = None
        buffer = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if sender and buffer and buffer[-1] != "":
                buffer.append("")
            continue
        if stripped.startswith("╭") or stripped.startswith("╰") or stripped.startswith("│"):
            continue
        if stripped.startswith("▀") or stripped.startswith("▄") or stripped.startswith("─"):
            flush()
            continue
        if stripped.startswith("> "):
            flush()
            sender = "User"
            buffer = [stripped[2:].strip()]
            continue
        if stripped.startswith("✦ "):
            flush()
            sender = "Gemini"
            buffer = [stripped[2:].strip()]
            continue
        if stripped.startswith("Shift+Tab") or stripped.startswith("workspace (/directory)"):
            flush()
            continue
        low = stripped.lower()
        if "type your message or @" in low and "[cursor]" in low:
            continue
        if sender:
            buffer.append(stripped)
    flush()
    return messages
