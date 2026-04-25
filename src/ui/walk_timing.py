"""
Walk / video timing from macOS LilAgents (WalkerCharacter + LilAgentsController).

Video is ~10s per walk segment; horizontal motion follows movementPosition(videoTime)
with ease-in, linear, ease-out. See WalkerCharacter.swift movementPosition(at:).

Timings per character are sourced from ``config/characters.json`` via
:mod:`services.character_registry`. The legacy ``WalkTiming`` dataclass and
``WALK_BY_CHARACTER`` mapping are preserved as adapters so older call sites
keep working.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Mapping, Tuple

from services.character_registry import WalkTimingSpec, get_registry


@dataclass(frozen=True)
class WalkTiming:
    """Per-character constants (seconds on the 10s walk timeline)."""

    accel_start: float
    full_speed_start: float
    decel_start: float
    walk_stop: float
    walk_amount: Tuple[float, float]
    y_offset_px: int
    flip_x_offset_px: int

    @classmethod
    def from_spec(cls, spec: WalkTimingSpec) -> "WalkTiming":
        return cls(
            accel_start=spec.accel_start,
            full_speed_start=spec.full_speed_start,
            decel_start=spec.decel_start,
            walk_stop=spec.walk_stop,
            walk_amount=spec.walk_amount,
            y_offset_px=spec.y_offset_px,
            flip_x_offset_px=spec.flip_x_offset_px,
        )


class _WalkTimingLookup(Mapping[str, WalkTiming]):
    """Live mapping backed by the character registry (reloads on registry reload)."""

    def _materialize(self) -> dict[str, WalkTiming]:
        reg = get_registry()
        return {c.name: WalkTiming.from_spec(c.walk_timing) for c in reg.list()}

    def __getitem__(self, key: str) -> WalkTiming:
        return self._materialize()[key.lower()]

    def get(self, key: str, default=None):  # type: ignore[override]
        return self._materialize().get((key or "").lower(), default)

    def __iter__(self):
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def __contains__(self, key) -> bool:  # type: ignore[override]
        if not isinstance(key, str):
            return False
        return key.lower() in self._materialize()


WALK_BY_CHARACTER: Mapping[str, WalkTiming] = _WalkTimingLookup()


def movement_position(
    video_time: float,
    accel_start: float,
    full_speed_start: float,
    decel_start: float,
    walk_stop: float,
) -> float:
    """Normalized 0..1 along the walk ease curve (same math as WalkerCharacter.movementPosition)."""
    d_in = full_speed_start - accel_start
    d_lin = decel_start - full_speed_start
    d_out = walk_stop - decel_start
    v = 1.0 / (d_in / 2.0 + d_lin + d_out / 2.0)

    if video_time <= accel_start:
        return 0.0
    if video_time <= full_speed_start:
        t = video_time - accel_start
        return float(v * t * t / (2.0 * d_in))
    if video_time <= decel_start:
        ease_in_dist = v * d_in / 2.0
        t = video_time - full_speed_start
        return float(ease_in_dist + v * t)
    if video_time <= walk_stop:
        ease_in_dist = v * d_in / 2.0
        linear_dist = v * d_lin
        t = video_time - decel_start
        return float(ease_in_dist + linear_dist + v * (t - t * t / (2.0 * d_out)))
    return 1.0


def random_walk_amount(wt: WalkTiming) -> float:
    lo, hi = wt.walk_amount
    return random.uniform(lo, hi)
