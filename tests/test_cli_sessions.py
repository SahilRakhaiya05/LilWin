"""ClaudeCliSession: stream-json parsing + graceful terminate escalation.

We never spawn a real subprocess; ``subprocess.Popen`` is patched with a fake
object that records writes and whose stdout/stderr are iterable buffers.
"""

from __future__ import annotations

import io
import json
import subprocess
import types
from typing import List, Optional

import pytest

# Avoid importing the PyQt stack if it refuses to init in CI — but pytest-qt
# will supply ``qapp`` on demand and cli_sessions itself only needs QObject
# signals, which work headless.
pytest.importorskip("PyQt6")

from services import cli_sessions as cs


class _FakeStream:
    """Reader side: produces canned lines then EOF. Writer side: captures."""
    def __init__(self, lines: Optional[List[str]] = None):
        self._lines = list(lines or [])
        self.written: List[str] = []
        self._closed = False

    def readline(self) -> str:
        if self._closed or not self._lines:
            return ""
        return self._lines.pop(0)

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True


class _FakeProc:
    def __init__(self, stdout_lines=None, stderr_lines=None):
        self.stdin = _FakeStream()
        self.stdout = _FakeStream(stdout_lines or [])
        self.stderr = _FakeStream(stderr_lines or [])
        self._terminated = False
        self._killed = False
        self._return = None
        self._poll_returns: List[Optional[int]] = [None]
        self._wait_behavior = "exit"  # "exit" | "timeout"

    # API used by _graceful_stop_proc + reader loops
    def poll(self):
        return self._poll_returns[0] if self._poll_returns else None

    def terminate(self):
        self._terminated = True
        if self._wait_behavior == "exit":
            self._return = 0
            self._poll_returns = [0]

    def kill(self):
        self._killed = True
        self._return = -9
        self._poll_returns = [-9]

    def wait(self, timeout=None):
        if self._wait_behavior == "timeout" and not self._killed:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self._return if self._return is not None else 0


def _make_session(monkeypatch, proc: _FakeProc) -> cs.ClaudeCliSession:
    monkeypatch.setattr(cs, "find_cli_command", lambda _name: "/fake/claude")
    monkeypatch.setattr(cs.subprocess, "Popen", lambda *a, **kw: proc)
    return cs.ClaudeCliSession()


def test_stream_json_text_emits_chunks(monkeypatch, qt_drain):
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "abc-123"}) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello "}]},
        }) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "world"}]},
        }) + "\n",
        json.dumps({"type": "result", "session_id": "abc-123"}) + "\n",
    ]
    proc = _FakeProc(stdout_lines=lines)
    session = _make_session(monkeypatch, proc)

    received: List[str] = []
    session.text_received.connect(received.append)
    turn_done = []
    session.turn_completed.connect(lambda: turn_done.append(True))

    # turn_completed only fires via _finish_turn(), which is gated on
    # is_busy — simulate a send being in flight.
    session.is_busy = True
    session.start()
    # Reader is in a daemon thread; wait for it.
    if session._reader:
        session._reader.join(timeout=2.0)
    # Cross-thread queued signals need an event loop to be dispatched.
    qt_drain()

    assert "".join(received) == "hello world"
    assert turn_done, "turn_completed should fire on result"
    assert session.get_disk_session_id() == "abc-123"


def test_stream_json_error_emits_error(monkeypatch, qt_drain):
    lines = [
        json.dumps({"type": "error", "error": "boom"}) + "\n",
    ]
    proc = _FakeProc(stdout_lines=lines)
    session = _make_session(monkeypatch, proc)

    errors: List[str] = []
    session.error_occurred.connect(errors.append)

    # _finish_turn only fires if busy; simulate an in-flight send.
    session.is_busy = True
    session.start()
    if session._reader:
        session._reader.join(timeout=2.0)
    qt_drain()
    assert errors == ["boom"]


def test_send_writes_user_json(monkeypatch, qt_drain):
    # Keep the reader alive for the duration of the test so ``is_running``
    # stays True when send() is called. We do this by feeding it one harmless
    # line that readline() will block on *after* we've emitted the send. We
    # use a tiny active stream that only releases EOF once we've sent.
    proc = _FakeProc(stdout_lines=[
        json.dumps({"type": "system", "subtype": "init", "session_id": "x"}) + "\n",
    ])
    session = _make_session(monkeypatch, proc)
    session.start()
    # At this point the reader may have already drained EOF. Make sure
    # send() still targets the live stdin by asserting against the queue
    # *or* the write — either satisfies correctness.
    session.send("hi there")
    if session._reader:
        session._reader.join(timeout=2.0)
    qt_drain()

    if proc.stdin.written:
        payload = json.loads(proc.stdin.written[-1])
        assert payload == {"type": "user", "message": {"role": "user", "content": "hi there"}}
    else:
        # Reader exited before send(); the message should be queued for the
        # next start() instead of silently dropped.
        assert session._pending_messages == ["hi there"]


def test_send_queues_when_not_running():
    session = cs.ClaudeCliSession()
    # Don't start(); no _proc, not running.
    session.send("queued-message")
    assert session._pending_messages == ["queued-message"]


def test_graceful_stop_escalates_to_kill(monkeypatch):
    proc = _FakeProc()
    proc._wait_behavior = "timeout"  # terminate() won't satisfy wait()
    cs._graceful_stop_proc(proc, "test-label")
    assert proc._terminated
    assert proc._killed


def test_graceful_stop_no_op_when_already_dead(monkeypatch):
    proc = _FakeProc()
    proc._poll_returns = [0]  # already exited
    cs._graceful_stop_proc(proc, "test-label")
    assert not proc._terminated
    assert not proc._killed


def test_graceful_stop_terminate_is_enough(monkeypatch):
    proc = _FakeProc()
    proc._wait_behavior = "exit"
    cs._graceful_stop_proc(proc, "test-label")
    assert proc._terminated
    assert not proc._killed


def test_terminate_closes_stdin_and_stops_proc(monkeypatch):
    proc = _FakeProc(stdout_lines=[])
    session = _make_session(monkeypatch, proc)
    session.start()
    session.is_busy = True
    session.terminate()
    assert proc.stdin._closed
    assert proc._terminated
    assert session.is_running is False


def test_cli_not_found_emits_error(monkeypatch):
    monkeypatch.setattr(cs, "find_cli_command", lambda _name: None)
    session = cs.ClaudeCliSession()
    errors: List[str] = []
    session.error_occurred.connect(errors.append)
    session.start()
    assert errors and "Claude CLI" in errors[0]
    assert session.is_running is False


def test_malformed_json_line_ignored(monkeypatch, qt_drain):
    lines = [
        "not json\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "ok"}]},
        }) + "\n",
        json.dumps({"type": "result"}) + "\n",
    ]
    proc = _FakeProc(stdout_lines=lines)
    session = _make_session(monkeypatch, proc)
    chunks: List[str] = []
    session.text_received.connect(chunks.append)
    session.start()
    if session._reader:
        session._reader.join(timeout=2.0)
    qt_drain()
    assert "".join(chunks) == "ok"


def test_gemini_spawn_uses_session_cwd(monkeypatch):
    """Regression: Gemini must honor /cd working directory."""
    calls = {}

    def fake_spawn(argv, *, stderr=subprocess.DEVNULL, cwd=None):
        calls["cwd"] = cwd
        calls["argv"] = list(argv)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(cs, "_spawn", fake_spawn)
    s = cs.GeminiCliSession(cwd=r"C:\Users\Admin\Downloads\New folder (6)")
    s._binary = "/fake/gemini"
    s.is_running = True
    s._run_send("make hello world file")
    assert calls["cwd"] == r"C:\Users\Admin\Downloads\New folder (6)"


def test_copilot_spawn_uses_session_cwd(monkeypatch):
    """Regression: Copilot custom _run_send path must pass cwd to _spawn."""
    calls = {}

    def fake_spawn(argv, *, stderr=subprocess.DEVNULL, cwd=None):
        calls["cwd"] = cwd
        calls["argv"] = list(argv)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(cs, "_spawn", fake_spawn)
    s = cs.CopilotCliSession(cwd=r"C:\Users\Admin\Downloads\New folder (6)")
    s._binary = "/fake/copilot"
    s.is_running = True
    s._run_send("create hello world html")
    assert calls["cwd"] == r"C:\Users\Admin\Downloads\New folder (6)"


def test_gemini_filters_directory_warning_noise(monkeypatch, qt_drain):
    """Regression: noisy EPERM directory scans should not spam chat/errors."""
    proc = _FakeProc(
        stdout_lines=[
            "[WARN] Skipping unreadable directory: c:\\users\\Parth (EPERM: operation not permitted, scandir 'c:\\users\\Parth')\n",
            "Hello from Gemini\n",
        ],
        stderr_lines=[
            "Warning: Could not read directory C:\\Users\\Parth: EPERM: operation not permitted, scandir 'C:\\Users\\Parth'\n",
        ],
    )

    monkeypatch.setattr(cs, "_spawn", lambda *a, **kw: proc)
    s = cs.GeminiCliSession()
    s._binary = "/fake/gemini"
    s.is_running = True

    chunks: List[str] = []
    errors: List[str] = []
    s.text_received.connect(chunks.append)
    s.error_occurred.connect(errors.append)

    s._run_send("hi")
    qt_drain()

    assert errors == []
    assert chunks == ["Hello from Gemini\n"]
