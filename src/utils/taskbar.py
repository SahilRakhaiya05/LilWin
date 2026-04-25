
import ctypes
import sys
from ctypes import wintypes
from typing import Any, Dict, Optional, Tuple

# Windows taskbar edges (ABE_*)
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3

ABM_GETTASKBARPOS = 0x00000005


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", RECT),
        ("lParam", wintypes.LPARAM),
    ]


def get_taskbar_info() -> Optional[Dict[str, Any]]:
    """
    Returns taskbar edge and rectangle in screen coordinates, or None if unavailable.
    Callers should fall back to Qt screen.availableGeometry() when this returns None.
    """
    if sys.platform != "win32":
        return None

    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    res = ctypes.windll.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd))
    if not res:
        return None

    rect: Tuple[int, int, int, int] = (
        int(abd.rc.left),
        int(abd.rc.top),
        int(abd.rc.right),
        int(abd.rc.bottom),
    )
    return {"edge": int(abd.uEdge), "rect": rect}


if __name__ == "__main__":
    print(get_taskbar_info())
