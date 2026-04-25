"""Pick on-screen position for a window anchored to a character sprite.

Placement rules (in order of importance):
 1. Stay fully inside the screen's available area (i.e. NEVER under the
    taskbar, NEVER above the top of the monitor, NEVER off the left/right
    edges). ``QScreen.availableGeometry()`` is the source of truth here —
    it's DPI-correct on Windows in a way that the raw taskbar rect is not.
 2. Do not overlap the anchor rect (the walker sprite) if any side of the
    screen has enough room.
 3. Prefer the side opposite the taskbar (so the popover never looks like
    it's hanging off the same edge the walker docks to).
 4. Break remaining ties by preferring "above" on bottom-docked taskbars
    (this is the most natural "speech bubble above the character" layout).
"""

from __future__ import annotations

from typing import Optional, Tuple

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QGuiApplication


def point_for_window_near_anchor(
    anchor_rect: QRect,
    width: int,
    height: int,
    margin: int,
    taskbar_edge: Optional[int],
) -> QPoint:
    """Place a ``width × height`` window near ``anchor_rect``.

    ``taskbar_edge``: 0 = taskbar on left, 1 = top, 2 = right, else bottom.
    """
    w = max(1, width)
    h = max(1, height)
    m = max(4, margin)

    sample = anchor_rect.center()
    screen = QGuiApplication.screenAt(sample) or QGuiApplication.primaryScreen()
    if screen is None:
        return QPoint(int(anchor_rect.center().x() - w // 2), int(anchor_rect.top() - m - h))

    avail = screen.availableGeometry()
    gl, gt, gr, gb = avail.left(), avail.top(), avail.right(), avail.bottom()

    # Max valid top-left positions so the window stays inside the available area.
    max_x = gr - w + 1
    max_y = gb - h + 1

    def clamp_xy(ix: float, iy: float) -> Tuple[int, int, float]:
        xi = int(round(ix))
        yi = int(round(iy))
        cx = max(gl, min(xi, max_x))
        cy = max(gt, min(yi, max_y))
        penalty = float(abs(cx - xi) + abs(cy - yi))
        return cx, cy, penalty

    def overlap(pos: Tuple[int, int]) -> int:
        """Pixel area where the proposed popover rect intersects the anchor."""
        r1 = QRect(pos[0], pos[1], w, h)
        r2 = r1.intersected(anchor_rect)
        if r2.isEmpty():
            return 0
        return r2.width() * r2.height()

    cx = anchor_rect.center().x()
    cy = anchor_rect.center().y()

    # Room available on each side (can be negative → no room).
    room_below = gb - anchor_rect.bottom() - m
    room_above = anchor_rect.top() - m - gt
    room_right = gr - anchor_rect.right() - m
    room_left = anchor_rect.left() - m - gl

    # Ideal top-left coordinates for each side (before clamping).
    candidates: Tuple[Tuple[str, float, float, int, int], ...] = (
        # side,        ix,                           iy,                           min_w, min_h
        ("above", cx - w / 2.0, anchor_rect.top() - m - h,    w,    h),
        ("below", cx - w / 2.0, anchor_rect.bottom() + m,     w,    h),
        ("right", anchor_rect.right() + m,         cy - h / 2.0,     w,    h),
        ("left",  anchor_rect.left() - m - w,      cy - h / 2.0,     w,    h),
    )

    # Does the side have enough room for the whole popover, without clamping
    # into the anchor?
    fits: dict = {
        "above": room_above >= h,
        "below": room_below >= h,
        "right": room_right >= w,
        "left":  room_left  >= w,
    }

    def side_hint_rank(side: str) -> int:
        """Smaller rank = more preferred."""
        # Prefer the side OPPOSITE the taskbar so the popover visually
        # "floats up" away from the dock the walker sits on.
        if taskbar_edge == 0:  # taskbar on left → popover on right
            order = ("right", "above", "below", "left")
        elif taskbar_edge == 1:  # taskbar on top → popover below
            order = ("below", "right", "left", "above")
        elif taskbar_edge == 2:  # taskbar on right → popover on left
            order = ("left", "above", "below", "right")
        else:  # bottom (default)
            order = ("above", "right", "left", "below")
        return order.index(side)

    best_pos: Optional[Tuple[int, int]] = None
    best_rank: Optional[Tuple[int, int, int, int]] = None

    for side, ix, iy, _, _ in candidates:
        cxx, cyy, penalty = clamp_xy(ix, iy)
        ov = overlap((cxx, cyy))
        # Rank is lexicographic and smaller-is-better:
        #   1) does it fit without clamping? (0=yes, 1=no)
        #   2) does the placed rect avoid the anchor? (0=yes, 1=no)
        #   3) taskbar-aware preferred side (0..3)
        #   4) raw overlap area as a final tiebreaker
        rank = (
            0 if fits[side] else 1,
            0 if ov == 0 else 1,
            side_hint_rank(side),
            int(ov + penalty),
        )
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_pos = (cxx, cyy)

    assert best_pos is not None

    # Final hard clamp (defence in depth — never let the popover spill off
    # the usable screen, even if every proposed side was bad).
    fx = max(gl, min(best_pos[0], max_x))
    fy = max(gt, min(best_pos[1], max_y))
    return QPoint(fx, fy)
