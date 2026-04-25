"""walk_timing.movement_position: monotonic 0→1 curve, correct boundary values."""

from __future__ import annotations

from services import character_registry as cr
from ui.walk_timing import (
    WALK_BY_CHARACTER,
    WalkTiming,
    movement_position,
    random_walk_amount,
)


# Standard timeline from characters.json defaults.
ACCEL = 3.0
FULL = 3.75
DECEL = 8.0
STOP = 8.5


def _pos(t: float) -> float:
    return movement_position(t, ACCEL, FULL, DECEL, STOP)


def test_before_accel_is_zero():
    assert _pos(0.0) == 0.0
    assert _pos(1.5) == 0.0
    assert _pos(ACCEL) == 0.0


def test_after_stop_is_one():
    assert _pos(STOP) == 1.0
    assert _pos(STOP + 2.0) == 1.0


def test_monotonic_non_decreasing():
    prev = -1.0
    t = 0.0
    while t <= STOP + 0.5:
        cur = _pos(t)
        assert cur >= prev - 1e-9, f"decreasing at t={t}: {prev}→{cur}"
        prev = cur
        t += 0.01


def test_output_bounded_0_1():
    t = 0.0
    while t <= STOP + 0.5:
        p = _pos(t)
        assert -1e-9 <= p <= 1.0 + 1e-9
        t += 0.05


def test_ease_in_starts_flat():
    # Derivative at accel_start should be ~0, so small step stays small.
    eps = 0.05
    assert _pos(ACCEL + eps) < 0.01


def test_continuous_at_phase_boundaries():
    # Check no jump at the three internal boundaries.
    for boundary in (ACCEL, FULL, DECEL, STOP):
        left = _pos(boundary - 1e-4)
        right = _pos(boundary + 1e-4)
        assert abs(left - right) < 1e-3, f"jump at {boundary}: {left}→{right}"


def test_random_walk_amount_in_range():
    wt = WalkTiming(
        accel_start=3.0,
        full_speed_start=3.75,
        decel_start=8.0,
        walk_stop=8.5,
        walk_amount=(0.2, 0.7),
        y_offset_px=0,
        flip_x_offset_px=0,
    )
    for _ in range(100):
        v = random_walk_amount(wt)
        assert 0.2 <= v <= 0.7


def test_registry_backed_lookup(minimal_manifest):
    cr.reset_registry()
    cr.get_registry(minimal_manifest)
    assert "bruce" in WALK_BY_CHARACTER
    assert "JAZZ" in WALK_BY_CHARACTER  # case-insensitive
    assert "ghost" not in WALK_BY_CHARACTER
    bruce = WALK_BY_CHARACTER["bruce"]
    assert isinstance(bruce, WalkTiming)
    assert bruce.walk_amount == (0.4, 0.65)


def test_registry_lookup_get_default(minimal_manifest):
    cr.reset_registry()
    cr.get_registry(minimal_manifest)
    assert WALK_BY_CHARACTER.get("nope") is None
    sentinel = object()
    assert WALK_BY_CHARACTER.get("nope", sentinel) is sentinel
