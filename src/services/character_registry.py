"""Data-driven character registry.

Loads ``config/characters.json`` and exposes a typed view used by the tray,
walker windows, thinking bubbles, sound manager and provider mapping. This
replaces the three hardcoded character lists that used to drift out of sync
(``main.py``, ``system_tray.py``, ``walk_timing.py``).

Malformed entries are skipped with a logged warning; the registry never raises
at import time.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalkTimingSpec:
    accel_start: float
    full_speed_start: float
    decel_start: float
    walk_stop: float
    walk_amount: Tuple[float, float]
    y_offset_px: int
    flip_x_offset_px: int


@dataclass(frozen=True)
class Personality:
    thinking_phrases: Tuple[str, ...]
    done_phrases: Tuple[str, ...]
    accent_color: str
    greeting: str
    sound_variant: str


@dataclass(frozen=True)
class Character:
    name: str
    display_name: str
    frames_dir: str
    default_provider: Optional[str]
    walk_timing: WalkTimingSpec
    personality: Personality

    def resolve_frames_dir(self, assets_dir: str) -> str:
        return os.path.join(assets_dir, self.frames_dir)


_DEFAULT_TIMING = WalkTimingSpec(
    accel_start=3.0,
    full_speed_start=3.75,
    decel_start=8.0,
    walk_stop=8.5,
    walk_amount=(0.4, 0.65),
    y_offset_px=-3,
    flip_x_offset_px=0,
)

# Used when the registry is empty (e.g. characters.json missing) so UI still has valid timing.
DEFAULT_WALK_TIMING_SPEC = _DEFAULT_TIMING

_DEFAULT_PERSONALITY = Personality(
    thinking_phrases=("Thinking...",),
    done_phrases=("Done!",),
    accent_color="#FF8C69",
    greeting="Hey!",
    sound_variant="aa",
)


def _tuple_pair(value: Any, fallback: Tuple[float, float]) -> Tuple[float, float]:
    try:
        a, b = value
        return float(a), float(b)
    except (TypeError, ValueError):
        return fallback


def _parse_timing(raw: Dict[str, Any]) -> WalkTimingSpec:
    return WalkTimingSpec(
        accel_start=float(raw.get("accel_start", _DEFAULT_TIMING.accel_start)),
        full_speed_start=float(raw.get("full_speed_start", _DEFAULT_TIMING.full_speed_start)),
        decel_start=float(raw.get("decel_start", _DEFAULT_TIMING.decel_start)),
        walk_stop=float(raw.get("walk_stop", _DEFAULT_TIMING.walk_stop)),
        walk_amount=_tuple_pair(raw.get("walk_amount"), _DEFAULT_TIMING.walk_amount),
        y_offset_px=int(raw.get("y_offset_px", _DEFAULT_TIMING.y_offset_px)),
        flip_x_offset_px=int(raw.get("flip_x_offset_px", _DEFAULT_TIMING.flip_x_offset_px)),
    )


def _parse_personality(raw: Dict[str, Any]) -> Personality:
    return Personality(
        thinking_phrases=tuple(str(x) for x in raw.get("thinking_phrases") or _DEFAULT_PERSONALITY.thinking_phrases),
        done_phrases=tuple(str(x) for x in raw.get("done_phrases") or _DEFAULT_PERSONALITY.done_phrases),
        accent_color=str(raw.get("accent_color") or _DEFAULT_PERSONALITY.accent_color),
        greeting=str(raw.get("greeting") or _DEFAULT_PERSONALITY.greeting),
        sound_variant=str(raw.get("sound_variant") or _DEFAULT_PERSONALITY.sound_variant),
    )


def _parse_character(raw: Dict[str, Any]) -> Optional[Character]:
    name = str(raw.get("name") or "").strip().lower()
    if not name:
        logger.warning("Character entry missing 'name'; skipping: %r", raw)
        return None
    display = str(raw.get("display_name") or name.capitalize())
    frames_dir = str(raw.get("frames_dir") or f"{name}_frames")
    default_provider = raw.get("default_provider")
    return Character(
        name=name,
        display_name=display,
        frames_dir=frames_dir,
        default_provider=str(default_provider) if default_provider else None,
        walk_timing=_parse_timing(raw.get("walk_timing") or {}),
        personality=_parse_personality(raw.get("personality") or {}),
    )


class CharacterRegistry:
    def __init__(self, manifest_path: str) -> None:
        self.manifest_path = manifest_path
        self._characters: List[Character] = []
        self._by_name: Dict[str, Character] = {}
        self._by_provider: Dict[str, Character] = {}
        self.reload()

    def reload(self) -> None:
        self._characters = []
        self._by_name = {}
        self._by_provider = {}
        if not os.path.isfile(self.manifest_path):
            logger.error("characters.json not found at %s", self.manifest_path)
            return
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read characters.json at %s", self.manifest_path)
            return
        entries = data.get("characters") or []
        for raw in entries:
            if not isinstance(raw, dict):
                logger.warning("Skipping non-dict character entry: %r", raw)
                continue
            char = _parse_character(raw)
            if char is None:
                continue
            if char.name in self._by_name:
                logger.warning("Duplicate character '%s'; keeping first entry", char.name)
                continue
            self._characters.append(char)
            self._by_name[char.name] = char
            if char.default_provider:
                self._by_provider.setdefault(char.default_provider, char)
        if not self._characters:
            logger.error("No valid characters loaded from %s", self.manifest_path)

    def list(self) -> List[Character]:
        return list(self._characters)

    def names(self) -> List[str]:
        return [c.name for c in self._characters]

    def display_names(self) -> List[str]:
        return [c.display_name for c in self._characters]

    def get(self, name: str) -> Optional[Character]:
        if not name:
            return None
        return self._by_name.get(name.strip().lower())

    def get_or_first(self, name: str) -> Character:
        c = self.get(name)
        if c is not None:
            return c
        if not self._characters:
            raise RuntimeError("No characters are loaded")
        return self._characters[0]

    def default_for_provider(self, provider: str) -> Optional[Character]:
        if not provider:
            return None
        return self._by_provider.get(provider)

    def provider_character_map(self) -> Dict[str, str]:
        return {p: c.name for p, c in self._by_provider.items()}


_LOCK = threading.Lock()
_instance: Optional[CharacterRegistry] = None


def _default_manifest_path() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller: bundle root is _MEIPASS; config/ is --add-data next to top-level packages.
        base = sys._MEIPASS
    else:
        # Dev tree: src/services/thisfile.py -> repo root
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = os.path.dirname(src_dir)
    return os.path.join(base, "config", "characters.json")


def get_registry(manifest_path: Optional[str] = None) -> CharacterRegistry:
    global _instance
    with _LOCK:
        if _instance is None or manifest_path is not None:
            _instance = CharacterRegistry(manifest_path or _default_manifest_path())
        return _instance


def reset_registry() -> None:
    """Testing helper."""
    global _instance
    with _LOCK:
        _instance = None
