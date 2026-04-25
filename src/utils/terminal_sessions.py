"""Unified terminal-session discovery (tier 1 + tier 2).

Tier 1 — IDE file-backed sessions (``kind="ide_file"``)
    Cursor / VS Code / VS Code Insiders / VSCodium write live terminal buffer
    files to disk. Existing scanner at :mod:`utils.cursor_terminals` handles
    this path and gives us a real transcript via ``QFileSystemWatcher``.

Tier 2 — Process/window-backed sessions (``kind="process"``)
    ANY visible top-level window whose host is a known terminal emulator
    (Windows Terminal, ConEmu, Alacritty, WezTerm, mintty, plain cmd/
    powershell/pwsh console, conhost) AND whose descendant process tree
    contains a provider CLI (``claude``, ``gemini``, ``codex``, ``copilot``,
    ``opencode``). These sessions have NO disk transcript — the popover
    becomes a send-only bridge (clipboard + Ctrl+V + Enter against the
    session's own HWND).

The two tiers are concatenated into a single list, tier 1 first, so IDE
terminals keep their live-mirror priority.

Results are cached for a short TTL so the 450 ms UI poller stays cheap.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from utils.cursor_terminals import (
    TerminalSessionInfo as _LegacyTerminalSessionInfo,
    list_active_integrated_provider_sessions as _list_ide_active,
    list_terminal_sessions as _list_ide_all,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass — a superset of the legacy one.
# ---------------------------------------------------------------------------


@dataclass
class TerminalSessionInfo:
    """Unified session info covering both IDE-file and process-backed terminals.

    Fields kept identical to :class:`utils.cursor_terminals.TerminalSessionInfo`
    so every existing call site keeps compiling. New fields are optional with
    safe defaults.
    """

    session_key: str
    session_id: str
    path: str
    active_command: str
    cwd: str
    provider: str
    title: str
    source: str
    # --- tier-2 additions (default-safe for tier-1 callers) ---
    kind: str = "ide_file"
    hwnd: int = 0
    pid: int = 0
    exe_name: str = ""
    parent_pid: int = 0

    @property
    def is_process_backed(self) -> bool:
        return self.kind == "process"

    @property
    def has_live_mirror(self) -> bool:
        return self.kind == "ide_file" and bool(self.path)


def _from_legacy(info: _LegacyTerminalSessionInfo) -> TerminalSessionInfo:
    return TerminalSessionInfo(
        session_key=info.session_key,
        session_id=info.session_id,
        path=info.path,
        active_command=info.active_command,
        cwd=info.cwd,
        provider=info.provider,
        title=info.title,
        source=info.source,
        kind="ide_file",
    )


# ---------------------------------------------------------------------------
# Tier 2 — Win32 process + window enumeration.
# ---------------------------------------------------------------------------


# Provider CLI executable basenames (lowercase, no extension).
_PROVIDER_EXES: Dict[str, str] = {
    "claude": "Claude",
    "gemini": "Gemini",
    "codex": "Codex",
    "copilot": "Copilot",
    "opencode": "OpenCode",
    "openclaw": "OpenClaw",
}

# Known terminal-emulator host executables (lowercase, with .exe).
_HOST_EXES: Set[str] = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "conhost.exe",
    "alacritty.exe",
    "mintty.exe",
    "wezterm-gui.exe",
    "wezterm.exe",
    "conemu.exe",
    "conemu64.exe",
    "tabby.exe",
    "hyper.exe",
}

# IDE host executables — already covered by tier-1 scanning. Skip so we don't
# double-count the same terminal from two sources.
_IDE_EXES: Set[str] = {
    "code.exe",
    "code - insiders.exe",
    "cursor.exe",
    "codium.exe",
    "vscodium.exe",
    "devenv.exe",
    "rider64.exe",
    "pycharm64.exe",
    "idea64.exe",
    "webstorm64.exe",
    "clion64.exe",
}


# Pretty display names for titles.
_PRETTY_HOST: Dict[str, str] = {
    "cmd.exe": "cmd",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell 7",
    "windowsterminal.exe": "Windows Terminal",
    "conhost.exe": "Console",
    "alacritty.exe": "Alacritty",
    "mintty.exe": "Mintty",
    "wezterm-gui.exe": "WezTerm",
    "wezterm.exe": "WezTerm",
    "conemu.exe": "ConEmu",
    "conemu64.exe": "ConEmu",
    "tabby.exe": "Tabby",
    "hyper.exe": "Hyper",
}


# --- Cache ----------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_cache_entry: Optional[Tuple[float, List[TerminalSessionInfo]]] = None
_CACHE_TTL_S = 0.4


def _cached(now: float) -> Optional[List[TerminalSessionInfo]]:
    with _CACHE_LOCK:
        if _cache_entry and (now - _cache_entry[0]) < _CACHE_TTL_S:
            return list(_cache_entry[1])
    return None


def _store_cache(items: List[TerminalSessionInfo]) -> None:
    global _cache_entry
    with _CACHE_LOCK:
        _cache_entry = (time.monotonic(), list(items))


# --- Win32 primitives (compiled at import time on non-Windows this is a no-op).


if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _psapi = ctypes.windll.psapi

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _Process32FirstW = _kernel32.Process32FirstW
    _Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _Process32FirstW.restype = wintypes.BOOL
    _Process32NextW = _kernel32.Process32NextW
    _Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _Process32NextW.restype = wintypes.BOOL
    _CreateToolhelp32Snapshot = _kernel32.CreateToolhelp32Snapshot
    _CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = wintypes.HANDLE(-1).value


def _snapshot_processes() -> Dict[int, Tuple[int, str]]:
    """Return ``{pid: (parent_pid, exe_name_lower)}`` for every running process."""
    if sys.platform != "win32":
        return {}
    out: Dict[int, Tuple[int, str]] = {}
    snap = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not _Process32FirstW(snap, ctypes.byref(entry)):
            return out
        while True:
            pid = int(entry.th32ProcessID)
            ppid = int(entry.th32ParentProcessID)
            exe = str(entry.szExeFile or "").lower()
            out[pid] = (ppid, exe)
            if not _Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        _CloseHandle(snap)
    return out


def _build_child_index(procs: Dict[int, Tuple[int, str]]) -> Dict[int, List[int]]:
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _exe) in procs.items():
        children.setdefault(ppid, []).append(pid)
    return children


def _descendants(root_pid: int, children: Dict[int, List[int]], max_depth: int = 6) -> List[int]:
    """Iterative BFS — depth-limited to avoid pathological loops."""
    if root_pid not in children:
        return []
    out: List[int] = []
    frontier: List[Tuple[int, int]] = [(c, 1) for c in children.get(root_pid, [])]
    while frontier:
        pid, depth = frontier.pop()
        if depth > max_depth:
            continue
        out.append(pid)
        for c in children.get(pid, ()):
            frontier.append((c, depth + 1))
    return out


def _find_provider_in_tree(
    host_pid: int,
    procs: Dict[int, Tuple[int, str]],
    children: Dict[int, List[int]],
) -> Optional[str]:
    """Return the canonical provider name (e.g. ``"Claude"``) if a descendant matches."""
    for descendant in _descendants(host_pid, children):
        _, exe = procs.get(descendant, (0, ""))
        if not exe:
            continue
        stem = os.path.splitext(exe)[0]
        canonical = _PROVIDER_EXES.get(stem)
        if canonical:
            return canonical
    return None


def _list_top_windows() -> List[Tuple[int, int, str]]:
    """Return ``[(hwnd, pid, title)]`` for every visible, non-tool top-level window."""
    if sys.platform != "win32":
        return []
    results: List[Tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW:
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if not title.strip():
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            results.append((int(hwnd), int(pid.value), title))
        except OSError:
            logger.debug("EnumWindows filter raised", exc_info=True)
        return True

    _user32.EnumWindows(enum_proc, 0)
    return results


def _process_session_key(pid: int, provider: str) -> str:
    raw = f"proc:{pid}:{provider.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _make_process_session(
    hwnd: int,
    pid: int,
    exe: str,
    parent_pid: int,
    provider: str,
    title: str,
) -> TerminalSessionInfo:
    pretty = _PRETTY_HOST.get(exe, exe.replace(".exe", "").title() or "Terminal")
    short_title = title[:60].strip()
    display = f"{pretty} · PID {pid} — {provider}"
    if short_title:
        display += f" [{short_title}]"
    return TerminalSessionInfo(
        session_key=_process_session_key(pid, provider),
        session_id=f"pid{pid}",
        path="",
        active_command=exe,
        cwd="",
        provider=provider,
        title=display,
        source=pretty,
        kind="process",
        hwnd=hwnd,
        pid=pid,
        exe_name=exe,
        parent_pid=parent_pid,
    )


# Runtime executables that commonly host a provider CLI (Claude, Gemini are
# Node scripts; some are Python). When tier-2 sees one of these in the process
# tree, we fall back to scanning its command line for a provider needle.
_RUNTIME_EXES: Set[str] = {
    "node.exe",
    "python.exe",
    "python3.exe",
    "deno.exe",
    "bun.exe",
    "npx.exe",
    "npm.exe",
    "pnpm.exe",
    "yarn.exe",
}

# Substrings we look for inside a runtime process's command line to identify
# the provider it's hosting. Keyed by canonical provider name.
_PROVIDER_CMDLINE_NEEDLES: Dict[str, Tuple[str, ...]] = {
    "Claude":   ("claude-code", "anthropic-ai/claude-code", "\\claude", "/claude"),
    "Gemini":   ("@google/gemini", "gemini-cli", "\\gemini", "/gemini"),
    "Codex":    ("openai-codex", "\\codex", "/codex"),
    "Copilot":  ("github-copilot-cli", "\\copilot", "/copilot"),
    "OpenCode": ("opencode-ai", "\\opencode", "/opencode"),
    "OpenClaw": ("openclaw",),
}


# Command-line markers that indicate a process is a non-CLI subprocess of a
# desktop Electron / Chromium-shell app (Claude Desktop, VS Code, Cursor, etc).
# When we see one of these, the process is NOT a provider CLI even if its
# basename matches ``claude.exe`` — skip it.
_ELECTRON_SUBPROCESS_MARKERS: Tuple[str, ...] = (
    "--type=renderer",
    "--type=gpu-process",
    "--type=utility",
    "--type=crashpad-handler",
    "--type=zygote",
    "--type=broker",
    "--type=ppapi",
    "--type=plugin",
    "--service-sandbox-type=",
    "--mojo-platform-channel-handle",
    "--field-trial-handle=",
    "--app-path=",
)

# Desktop-app install locations that host the Claude Desktop Electron shell.
# A process whose full image path lives under one of these roots is the desktop
# app, never the Claude Code CLI.
_DESKTOP_APP_PATH_NEEDLES: Tuple[str, ...] = (
    "\\windowsapps\\claude_",
    "\\program files\\windowsapps\\claude_",
    "\\localcache\\roaming\\claude\\chromenativehost\\",
    "\\appdata\\local\\packages\\claude_",
    "chrome-native-host.exe",
)


def _looks_like_desktop_app_subprocess(cmdline: str) -> bool:
    if not cmdline:
        return False
    low = cmdline.lower()
    if any(m in low for m in _ELECTRON_SUBPROCESS_MARKERS):
        return True
    if any(m in low for m in _DESKTOP_APP_PATH_NEEDLES):
        return True
    return False


_WMI_CACHE_LOCK = threading.Lock()
_wmi_cache: Optional[Tuple[float, Dict[int, str]]] = None
_WMI_CACHE_TTL_S = 1.5  # Separate, longer TTL — WMI is expensive.


def _wmi_cmdlines() -> Dict[int, str]:
    """Return ``{pid: cmdline_lower}``. Cached for ~1.5 s. Windows-only."""
    if sys.platform != "win32":
        return {}
    global _wmi_cache
    now = time.monotonic()
    with _WMI_CACHE_LOCK:
        if _wmi_cache and (now - _wmi_cache[0]) < _WMI_CACHE_TTL_S:
            return dict(_wmi_cache[1])
    import subprocess as _subprocess
    try:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
        ]
        raw = _subprocess.check_output(
            cmd,
            text=True,
            stderr=_subprocess.DEVNULL,
            timeout=3,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
        import json as _json
        data = _json.loads(raw) if raw.strip() else []
    except Exception:
        logger.debug("WMI cmdline scan failed", exc_info=True)
        data = []
    if isinstance(data, dict):
        data = [data]
    out: Dict[int, str] = {}
    for entry in data:
        try:
            pid = int(entry.get("ProcessId") or 0)
            cmd = str(entry.get("CommandLine") or "").lower()
        except Exception:
            continue
        if pid and cmd:
            out[pid] = cmd
    with _WMI_CACHE_LOCK:
        _wmi_cache = (now, dict(out))
    return out


def _provider_from_cmdline(cmdline: str) -> Optional[str]:
    if not cmdline:
        return None
    for provider, needles in _PROVIDER_CMDLINE_NEEDLES.items():
        for needle in needles:
            if needle in cmdline:
                return provider
    return None


def _find_provider_in_tree_or_cmdline(
    host_pid: int,
    procs: Dict[int, Tuple[int, str]],
    children: Dict[int, List[int]],
    cmdlines: Dict[int, str],
) -> Optional[str]:
    """Provider match by exe (fast) OR by cmdline on runtime descendants."""
    for descendant in _descendants(host_pid, children):
        _, exe = procs.get(descendant, (0, ""))
        if not exe:
            continue
        cmd = cmdlines.get(descendant, "")
        # Never report a desktop-app / Electron subprocess as a CLI, even if
        # its basename happens to be ``claude.exe``.  Claude Desktop spawns
        # many such renderer / gpu-process / utility children that would
        # otherwise trigger a false "Claude CLI session is running" signal.
        if _looks_like_desktop_app_subprocess(cmd):
            continue
        stem = os.path.splitext(exe)[0]
        canonical = _PROVIDER_EXES.get(stem)
        if canonical:
            return canonical
        if exe in _RUNTIME_EXES:
            if cmd:
                hit = _provider_from_cmdline(cmd)
                if hit:
                    return hit
    return None


def _find_ancestor_window(
    pid: int,
    procs: Dict[int, Tuple[int, str]],
    window_by_pid: Dict[int, Tuple[int, str]],
    max_depth: int = 8,
) -> Optional[Tuple[int, int, str]]:
    """Walk up from ``pid`` to find an ancestor that owns a visible window.

    Returns ``(ancestor_pid, hwnd, title)`` or ``None``.
    """
    cur = pid
    for _ in range(max_depth):
        if cur in window_by_pid:
            hwnd, title = window_by_pid[cur]
            return (cur, hwnd, title)
        parent_entry = procs.get(cur)
        if not parent_entry:
            break
        parent_pid = parent_entry[0]
        if parent_pid == 0 or parent_pid == cur:
            break
        cur = parent_pid
    return None


def enumerate_process_terminals(
    provider_name: Optional[str] = None,
) -> List[TerminalSessionInfo]:
    """Tier-2: visible terminal-host windows with a provider CLI in their tree.

    Matches are found both by direct exe name (``claude.exe``) and by runtime
    host + cmdline (``node.exe  ...claude-code\\cli.js ...``). Node-hosted CLIs
    like Claude Code and Gemini are invisible to a pure exe-name scan.
    """
    if sys.platform != "win32":
        return []
    try:
        procs = _snapshot_processes()
    except Exception:
        logger.debug("Process snapshot failed", exc_info=True)
        return []
    if not procs:
        return []
    children = _build_child_index(procs)
    cmdlines = _wmi_cmdlines()

    windows = _list_top_windows()
    # Index visible windows by their owning pid for fast ancestor lookup.
    window_by_pid: Dict[int, Tuple[int, str]] = {}
    for hwnd, pid, title in windows:
        window_by_pid.setdefault(pid, (hwnd, title))

    out: List[TerminalSessionInfo] = []
    seen_keys: Set[str] = set()

    # Pass 1 — walk down from every visible top-level window (existing logic
    # extended with the cmdline-aware matcher).
    for hwnd, pid, title in windows:
        entry = procs.get(pid)
        if not entry:
            continue
        parent_pid, exe = entry
        if exe in _IDE_EXES:
            continue
        if exe not in _HOST_EXES:
            parent_exe = procs.get(parent_pid, (0, ""))[1]
            if parent_exe not in _HOST_EXES:
                continue
        provider = _find_provider_in_tree_or_cmdline(pid, procs, children, cmdlines)
        if not provider:
            continue
        if provider_name and provider != provider_name:
            continue
        sess = _make_process_session(hwnd, pid, exe, parent_pid, provider, title)
        if sess.session_key in seen_keys:
            continue
        seen_keys.add(sess.session_key)
        out.append(sess)

    # Pass 2 — walk UP from each runtime process whose cmdline matches. This
    # catches "claude is running as node.exe" even when its visible terminal
    # host wasn't one of our known exes (e.g. exotic shell / wrapper).
    for pid, (ppid, exe) in procs.items():
        if exe not in _RUNTIME_EXES and exe not in _PROVIDER_EXES.keys():
            # Also allow direct provider exes here (they might have been
            # skipped above if no host window matched the ancestor chain).
            stem = os.path.splitext(exe)[0]
            if stem not in _PROVIDER_EXES:
                continue
        stem = os.path.splitext(exe)[0]
        direct = _PROVIDER_EXES.get(stem)
        cmdline = cmdlines.get(pid, "")
        # Filter out the Electron / desktop-app subprocesses before doing
        # anything else.  Otherwise Claude Desktop's renderer / gpu / utility
        # children all get treated as "Claude CLI sessions" and the walker
        # pins itself to a random Electron window.
        if _looks_like_desktop_app_subprocess(cmdline):
            continue
        provider = direct or _provider_from_cmdline(cmdline)
        if not provider:
            continue
        if provider_name and provider != provider_name:
            continue
        anc = _find_ancestor_window(pid, procs, window_by_pid)
        if not anc:
            continue
        anc_pid, anc_hwnd, anc_title = anc
        anc_exe = procs.get(anc_pid, (0, ""))[1]
        if anc_exe in _IDE_EXES:
            # Tier-1 file scanning already owns these; skip to avoid dupes.
            continue
        # The ancestor window must be a known terminal host — otherwise we're
        # almost certainly looking at the main window of a desktop Electron
        # shell (Claude Desktop's own window, for example).  Requiring a
        # terminal host here keeps tier-2 honest: "visible terminal + provider
        # CLI inside it" is the contract.
        if anc_exe and anc_exe not in _HOST_EXES:
            continue
        # One more safety net: if the ancestor window's own cmdline looks like
        # a desktop-app Electron shell, skip.  This protects against exotic
        # terminal hosts we add to _HOST_EXES later.
        if _looks_like_desktop_app_subprocess(cmdlines.get(anc_pid, "")):
            continue
        sess = _make_process_session(anc_hwnd, anc_pid, anc_exe or exe, ppid, provider, anc_title)
        if sess.session_key in seen_keys:
            continue
        seen_keys.add(sess.session_key)
        out.append(sess)

    return out


# ---------------------------------------------------------------------------
# Unified listings — used by the UI.
# ---------------------------------------------------------------------------


def list_all_terminal_sessions(
    repo_root: Optional[str] = None,
    provider_name: Optional[str] = None,
) -> List[TerminalSessionInfo]:
    """Tier 1 (IDE files) + Tier 2 (visible processes), in that order."""
    now = time.monotonic()
    cached = _cached(now)
    if cached is not None and not repo_root:
        if provider_name:
            return [s for s in cached if not provider_name or s.provider == provider_name]
        return cached

    ide = [_from_legacy(i) for i in _list_ide_all(repo_root, provider_name)]
    proc = enumerate_process_terminals(provider_name)
    merged = ide + proc
    if not repo_root:
        _store_cache(merged)
    return merged


def list_all_active_provider_sessions(
    repo_root: Optional[str],
    provider_name: str,
) -> List[TerminalSessionInfo]:
    """Active (busy) IDE file sessions for this provider.

    Only tier-1 IDE file sessions with a live ``active_command`` are returned.
    Tier-2 process-backed sessions are intentionally excluded: they represent
    any terminal that *has* a provider CLI in its process tree, regardless of
    whether the AI is actually responding to something.  Including them here
    causes ``_sync_session_walker_slots`` to auto-spawn walkers and pop open
    chat windows whenever a bare ``claude`` / ``gemini`` process is visible in
    the background — which is the "shows without a session" bug.

    Process sessions remain available in :func:`list_all_terminal_sessions` so
    they still appear in the session-picker dropdown inside the chat window.
    """
    return [_from_legacy(i) for i in _list_ide_active(repo_root, provider_name)]


def clear_cache() -> None:
    global _cache_entry
    with _CACHE_LOCK:
        _cache_entry = None
