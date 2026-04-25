"""Windows-only: focus a likely IDE host window and send Ctrl+V (paste from clipboard)."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Iterable, List, Optional

if sys.platform != "win32":
    _user32 = None
else:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

PUL = ctypes.POINTER(ctypes.c_ulong)


class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", PUL),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", PUL),
    ]


class Input_I(ctypes.Union):
    _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ii", Input_I)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D


def _send_keyboard_sequence(inputs: List[Input]) -> bool:
    if not inputs or _user32 is None:
        return False
    n = len(inputs)
    arr = (Input * n)(*inputs)
    r = _user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(Input))
    return int(r) == n


def send_ctrl_v() -> bool:
    """Synthesize Ctrl+V (paste). Clipboard must already hold the desired text."""
    if _user32 is None:
        return False
    extra = ctypes.c_ulong(0)
    ii_extra = ctypes.pointer(extra)

    def ki(vk: int, flags: int = 0) -> Input:
        u = Input_I()
        u.ki = KeyBdInput(vk, 0, flags, 0, ii_extra)
        return Input(INPUT_KEYBOARD, u)

    seq = [
        ki(VK_CONTROL, 0),
        ki(VK_V, 0),
        ki(VK_V, KEYEVENTF_KEYUP),
        ki(VK_CONTROL, KEYEVENTF_KEYUP),
    ]
    return _send_keyboard_sequence(seq)


def send_enter() -> bool:
    """Synthesize Enter / Return (submit line in most CLIs and terminals)."""
    if _user32 is None:
        return False
    extra = ctypes.c_ulong(0)
    ii_extra = ctypes.pointer(extra)

    def ki(vk: int, flags: int = 0) -> Input:
        u = Input_I()
        u.ki = KeyBdInput(vk, 0, flags, 0, ii_extra)
        return Input(INPUT_KEYBOARD, u)

    seq = [
        ki(VK_RETURN, 0),
        ki(VK_RETURN, KEYEVENTF_KEYUP),
    ]
    return _send_keyboard_sequence(seq)


def _window_title(hwnd: int) -> str:
    if _user32 is None:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def try_set_foreground_window(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    fg = _user32.GetForegroundWindow()
    cur_tid = _kernel32.GetCurrentThreadId()
    target_tid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_tid))
    fg_tid = wintypes.DWORD(0)
    if fg:
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_tid))
    attached = False
    try:
        if fg and fg_tid.value and target_tid.value:
            attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid.value, True))
        return bool(_user32.SetForegroundWindow(hwnd))
    finally:
        if attached and fg and fg_tid.value:
            _user32.AttachThreadInput(cur_tid, fg_tid.value, False)


def find_ide_host_window(title_needles: Iterable[str] = ()) -> Optional[int]:
    """Return HWND of a top-level visible window whose title matches any needle (case-insensitive)."""
    if _user32 is None:
        return None
    needles = tuple(n.lower() for n in (title_needles or ()) if n)
    if not needles:
        needles = (
            "cursor", "visual studio code", "vscodium",
            "windows terminal", "command prompt", "powershell", "pwsh",
            "alacritty", "wezterm", "conemu", "mintty", "tabby", "hyper",
        )

    found: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd).lower()
        if not title:
            return True
        if any(n in title for n in needles):
            found.append(hwnd)
        return True

    _user32.EnumWindows(enum_proc, 0)
    return found[0] if found else None


def send_paste_to_hwnd(hwnd: int, press_enter: bool = True) -> bool:
    """Focus a specific window by HWND, paste clipboard, optionally press Enter.

    Used when we already know the exact terminal HWND (process-backed tier-2 sessions).
    Avoids the title-heuristic ambiguity of ``find_ide_host_window``.
    """
    if _user32 is None or not hwnd:
        return False
    if not _user32.IsWindow(hwnd):
        return False
    if not try_set_foreground_window(hwnd):
        # Still try — some terminals accept SendInput even without foreground
        pass
    # Small delay to let the foreground change settle
    _kernel32.Sleep(40)
    ok = send_ctrl_v()
    if not ok:
        return False
    if press_enter:
        _kernel32.Sleep(30)
        send_enter()
    return True


def paste_to_best_target(hwnd: Optional[int], title_needles: Iterable[str] = (), press_enter: bool = True) -> bool:
    """Try to paste to a specific HWND; fall back to title-needle window search."""
    if hwnd and _user32 is not None and _user32.IsWindow(hwnd):
        return send_paste_to_hwnd(hwnd, press_enter=press_enter)
    target = find_ide_host_window(title_needles)
    if not target:
        return False
    return send_paste_to_hwnd(target, press_enter=press_enter)
