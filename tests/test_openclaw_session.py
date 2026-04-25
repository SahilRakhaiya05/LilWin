"""OpenClawSession: signed payload structure + reconnect backoff scheduling."""

from __future__ import annotations

import base64
import json
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("websocket")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import ed25519

from services import openclaw_session as ocs


class _FakeWS:
    """Stand-in for websocket.WebSocketApp. Captures sent frames."""
    def __init__(self, url, on_message=None, on_error=None, on_close=None, on_open=None):
        self.url = url
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.on_open = on_open
        self.sent: List[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True

    def run_forever(self, **_kwargs):
        # Block-less; real thread would loop on recv. Tests don't rely on it.
        return


def _patch_ws(monkeypatch, captured: list) -> None:
    def _factory(url, **kw):
        ws = _FakeWS(url, **kw)
        captured.append(ws)
        return ws
    monkeypatch.setattr(ocs.websocket, "WebSocketApp", _factory)


def _no_thread(monkeypatch) -> None:
    # Don't actually spin up a thread running run_forever.
    class _NoThread:
        def __init__(self, *a, **kw):
            self._target = kw.get("target")
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, timeout=None):
            pass
    monkeypatch.setattr(ocs.threading, "Thread", _NoThread)


def test_invalid_url_emits_error(monkeypatch):
    errors: List[str] = []
    s = ocs.OpenClawSession("http://not-a-ws-url")
    s.error_occurred.connect(errors.append)
    s.start()
    assert errors and "Invalid OpenClaw gateway URL" in errors[0]
    assert s.is_running is False


def test_empty_url_emits_error(monkeypatch):
    errors: List[str] = []
    s = ocs.OpenClawSession("")
    s.error_occurred.connect(errors.append)
    s.start()
    assert errors


def test_send_builds_signed_payload(monkeypatch):
    captured: List[_FakeWS] = []
    _patch_ws(monkeypatch, captured)
    _no_thread(monkeypatch)

    s = ocs.OpenClawSession("ws://localhost:3001", auth_token="tok-xyz")
    s.start()
    assert captured, "WebSocketApp should have been constructed"
    ws = captured[0]
    # Simulate opened socket:
    s.is_running = True
    s.ws = ws

    s.send("hello world")
    assert ws.sent, "send() should have pushed one frame"
    payload = json.loads(ws.sent[0])
    for field in ("type", "message", "token", "timestamp", "nonce", "public_key", "signature"):
        assert field in payload, f"missing {field} in payload"
    assert payload["type"] == "chat"
    assert payload["message"] == "hello world"
    assert payload["token"] == "tok-xyz"
    assert isinstance(payload["timestamp"], int)
    # signature should verify against the exported public key.
    pub = base64.b64decode(payload["public_key"])
    sig = base64.b64decode(payload["signature"])
    canonical = f"{payload['message']}.{payload['timestamp']}.{payload['nonce']}".encode()
    ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, canonical)


def test_send_when_not_running_errors(monkeypatch):
    s = ocs.OpenClawSession("ws://localhost:3001")
    errors: List[str] = []
    s.error_occurred.connect(errors.append)
    s.send("test")
    assert errors and "not connected" in errors[0].lower()


def test_reconnect_schedules_timer(monkeypatch):
    _no_thread(monkeypatch)
    captured: List[_FakeWS] = []
    _patch_ws(monkeypatch, captured)

    scheduled = []
    class _FakeTimer:
        def __init__(self, delay, fn):
            scheduled.append((delay, fn))
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(ocs.threading, "Timer", _FakeTimer)

    s = ocs.OpenClawSession("ws://localhost:3001")
    s.start()
    # Simulate an unexpected close → should schedule a reconnect.
    s._on_close(None, 1006, "abnormal")
    assert scheduled, "Expected _schedule_reconnect to arm a Timer"
    delay, _fn = scheduled[0]
    assert 0.5 <= delay <= 5.0  # first backoff is 1s


def test_reconnect_backoff_increases(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    delays: List[float] = []
    class _FakeTimer:
        def __init__(self, delay, fn):
            delays.append(delay)
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(ocs.threading, "Timer", _FakeTimer)

    s = ocs.OpenClawSession("ws://localhost:3001")
    # Simulate 4 consecutive schedule calls.
    for _ in range(4):
        s._schedule_reconnect()
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_reconnect_capped_at_30s(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    delays: List[float] = []
    class _FakeTimer:
        def __init__(self, delay, fn):
            delays.append(delay)
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(ocs.threading, "Timer", _FakeTimer)

    s = ocs.OpenClawSession("ws://localhost:3001")
    for _ in range(8):
        s._schedule_reconnect()
    # After 2^(8-1) = 128s, must be capped at 30s.
    assert delays[-1] == 30.0


def test_reconnect_gives_up_after_max(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    class _FakeTimer:
        def __init__(self, *a, **kw):
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(ocs.threading, "Timer", _FakeTimer)

    s = ocs.OpenClawSession("ws://localhost:3001")
    errs: List[str] = []
    s.error_occurred.connect(errs.append)
    for _ in range(ocs._MAX_RECONNECTS + 1):
        s._schedule_reconnect()
    assert any("unreachable" in e.lower() for e in errs)
    assert s.is_running is False


def test_intentional_close_cancels_reconnect(monkeypatch):
    _no_thread(monkeypatch)
    captured: List[_FakeWS] = []
    _patch_ws(monkeypatch, captured)
    scheduled = []
    class _FakeTimer:
        def __init__(self, *a, **kw):
            scheduled.append(self)
            self.cancelled = False
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(ocs.threading, "Timer", _FakeTimer)

    s = ocs.OpenClawSession("ws://localhost:3001")
    s.start()
    s._schedule_reconnect()
    assert scheduled

    s.terminate()
    # After terminate, an ordinary _on_close should NOT schedule another timer.
    sched_before = len(scheduled)
    s._on_close(None, 1000, "bye")
    assert len(scheduled) == sched_before


def test_on_message_text_emits(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    s = ocs.OpenClawSession("ws://localhost:3001")
    received: List[str] = []
    s.text_received.connect(received.append)
    s._on_message(None, json.dumps({"type": "text", "text": "hi there"}))
    assert received == ["hi there"]


def test_on_message_done_flips_busy(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    s = ocs.OpenClawSession("ws://localhost:3001")
    s.is_busy = True
    done: List[bool] = []
    s.busy_state_changed.connect(lambda b: done.append(b))
    s._on_message(None, json.dumps({"type": "done"}))
    assert done == [False]
    assert s.is_busy is False


def test_on_message_error_type_emits(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    s = ocs.OpenClawSession("ws://localhost:3001")
    errors: List[str] = []
    s.error_occurred.connect(errors.append)
    s._on_message(None, json.dumps({"type": "error", "message": "nope"}))
    assert errors == ["nope"]


def test_non_json_message_becomes_text(monkeypatch):
    _no_thread(monkeypatch)
    _patch_ws(monkeypatch, [])
    s = ocs.OpenClawSession("ws://localhost:3001")
    received: List[str] = []
    s.text_received.connect(received.append)
    s._on_message(None, "just plain text")
    assert received == ["just plain text"]
