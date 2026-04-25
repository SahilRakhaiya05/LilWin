"""Locate AI CLIs on PATH and common install locations (Windows + Unix)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional


def _home() -> Path:
    return Path.home()


def _windows_cli_candidates(name: str) -> List[Path]:
    """Preferred Windows install locations for a provider CLI.

    Order matters: native ``.exe`` binaries come first, then the npm ``.cmd``
    shim (cmd.exe wrapper — functional but adds a shell hop), then ``.ps1``.
    Native ``.exe`` tends to be more reliable for long-lived stdin piping
    (used by ``ClaudeCliSession`` with ``--input-format stream-json``).
    """
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    home = _home()
    stem = name
    if stem.endswith(".exe"):
        stem = stem[:-4]
    out: List[Path] = []
    if appdata:
        npm = Path(appdata) / "npm"
        # Native exes bundled inside the npm-global package (Claude Code
        # ships one; other CLIs increasingly do too). These bypass the
        # cmd.exe shim and keep stdin/stdout clean for stream-json mode.
        if stem == "claude":
            out.append(
                npm
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
        elif stem == "gemini":
            out.append(
                npm / "node_modules" / "@google" / "gemini-cli" / "bin" / "gemini.exe"
            )
        elif stem == "codex":
            out.append(
                npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.exe"
            )
        # Fall-through candidates (shims + bare name).
        out.extend(
            [
                npm / f"{stem}.exe",
                npm / f"{stem}.cmd",
                npm / f"{stem}.ps1",
                npm / stem,
            ]
        )
    if stem == "claude":
        # Claude Code also drops a per-user versioned copy here that is kept
        # up to date by the desktop app auto-updater.
        if appdata:
            cc_root = Path(appdata) / "Claude" / "claude-code"
            if cc_root.is_dir():
                try:
                    versions = sorted(
                        [p for p in cc_root.iterdir() if p.is_dir()],
                        key=lambda p: p.name,
                        reverse=True,
                    )
                except OSError:
                    versions = []
                for ver in versions:
                    out.append(ver / "claude.exe")
    if localappdata:
        out.append(Path(localappdata) / "Programs" / "Microsoft VS Code" / "bin" / f"{stem}.cmd")
    out.extend(
        [
            home / ".local" / "bin" / f"{stem}.exe",
            home / ".local" / "bin" / stem,
        ]
    )
    return out


def _prefer_native_exe_sibling(path: str) -> str:
    """If ``path`` is a Windows ``.cmd`` / ``.ps1`` shim, find a sibling ``.exe``.

    Long-lived stdin piping (Claude Code stream-json) behaves much better
    without an extra cmd.exe layer wrapping the call, so when ``shutil.which``
    returns ``claude.CMD`` we look for the real ``claude.exe`` that ships next
    to it inside the npm-global ``node_modules`` tree.
    """
    if sys.platform != "win32":
        return path
    p = Path(path)
    low = p.suffix.lower()
    if low not in (".cmd", ".bat", ".ps1"):
        return path
    stem = p.stem
    parent = p.parent
    # Sibling exe — some CLIs install a real .exe right next to the shim.
    direct = parent / f"{stem}.exe"
    if direct.is_file():
        return str(direct)
    # npm-global shims point at a bundled binary inside node_modules. Check
    # the well-known layout for the major providers.
    node_modules = parent / "node_modules"
    candidates: List[Path] = []
    if stem == "claude":
        candidates.append(
            node_modules / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        )
    elif stem == "gemini":
        candidates.append(
            node_modules / "@google" / "gemini-cli" / "bin" / "gemini.exe"
        )
    elif stem == "codex":
        candidates.append(node_modules / "@openai" / "codex" / "bin" / "codex.exe")
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return path


def _unix_cli_candidates(name: str) -> List[Path]:
    home = _home()
    return [
        home / ".local" / "bin" / name,
        home / ".npm-global" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ]


def _first_existing(paths: Iterable[Path]) -> Optional[str]:
    for p in paths:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def find_cli_command(name: str) -> Optional[str]:
    """Return absolute path to a CLI if found.

    On Windows, prefer native ``.exe`` over ``.cmd`` / ``.ps1`` shims so that
    long-lived stdin piping (Claude Code ``--input-format stream-json``)
    doesn't have to hop through an extra cmd.exe process — which can buffer
    or break pipe semantics in subtle ways.
    """
    if sys.platform == "win32":
        # Try our curated list of native locations first so we pick up
        # ``node_modules\@anthropic-ai\claude-code\bin\claude.exe`` rather
        # than the ``claude.CMD`` shim that ``shutil.which`` would return.
        native = _first_existing(_windows_cli_candidates(name))
        if native and native.lower().endswith(".exe"):
            return native
        found = shutil.which(name)
        if found:
            return _prefer_native_exe_sibling(found)
        return native
    found = shutil.which(name)
    if found:
        return found
    return _first_existing(_unix_cli_candidates(name))
