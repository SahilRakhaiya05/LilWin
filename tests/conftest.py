"""Shared pytest fixtures: isolated registries, env overrides, logging silencing."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_app_dirs(tmp_path, monkeypatch):
    """Point logging + secrets at a temp dir so tests never touch %LOCALAPPDATA%."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("LIL_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LIL_AGENTS_SECRETS_PATH", str(tmp_path / "secrets.json"))
    yield


@pytest.fixture
def minimal_manifest(tmp_path) -> str:
    data = {
        "version": 1,
        "characters": [
            {
                "name": "bruce",
                "display_name": "Bruce",
                "frames_dir": "bruce_frames",
                "default_provider": "Claude",
                "walk_timing": {
                    "accel_start": 3.0,
                    "full_speed_start": 3.75,
                    "decel_start": 8.0,
                    "walk_stop": 8.5,
                    "walk_amount": [0.4, 0.65],
                    "y_offset_px": -3,
                    "flip_x_offset_px": 0,
                },
                "personality": {
                    "thinking_phrases": ["thinking..."],
                    "done_phrases": ["done!"],
                    "accent_color": "#FF8C69",
                    "greeting": "hi",
                    "sound_variant": "aa",
                },
            },
            {
                "name": "jazz",
                "display_name": "Jazz",
                "frames_dir": "jazz_frames",
                "default_provider": "Gemini",
                "walk_timing": {
                    "accel_start": 3.9,
                    "full_speed_start": 4.5,
                    "decel_start": 8.0,
                    "walk_stop": 8.75,
                    "walk_amount": [0.35, 0.6],
                    "y_offset_px": -7,
                    "flip_x_offset_px": -9,
                },
                "personality": {
                    "thinking_phrases": ["riffing..."],
                    "done_phrases": ["smooth."],
                    "accent_color": "#8E7CFF",
                    "greeting": "hey jazz",
                    "sound_variant": "bb",
                },
            },
        ],
    }
    path = tmp_path / "characters.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _reset_registry_singleton():
    from services import character_registry
    character_registry.reset_registry()
    yield
    character_registry.reset_registry()


@pytest.fixture(scope="session")
def _qapp_singleton():
    """Create a single QCoreApplication for the test session.

    PyQt6 signals emitted across thread boundaries are delivered via the
    receiving thread's event loop. Without a QCoreApplication (and without
    pumping events), queued signals are silently dropped — which caused
    flaky ``ClaudeCliSession`` tests that exercise the background stdout
    reader. Creating a lightweight app once per session lets callers drain
    events via :func:`QCoreApplication.processEvents`.
    """
    try:
        from PyQt6.QtCore import QCoreApplication
    except Exception:  # pragma: no cover - PyQt not installed
        yield None
        return
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv[:1] or [""])
    yield app


@pytest.fixture
def qt_drain(_qapp_singleton):
    """Return a callable that pumps Qt events briefly so queued cross-thread
    signals are delivered to connected slots in the test thread."""
    try:
        from PyQt6.QtCore import QCoreApplication
    except Exception:  # pragma: no cover
        def _noop(_timeout_ms: int = 100) -> None:
            return
        return _noop

    def _drain(timeout_ms: int = 200) -> None:
        app = QCoreApplication.instance()
        if app is None:
            return
        import time as _time
        deadline = _time.monotonic() + (timeout_ms / 1000.0)
        while _time.monotonic() < deadline:
            app.processEvents()
            _time.sleep(0.005)

    return _drain
