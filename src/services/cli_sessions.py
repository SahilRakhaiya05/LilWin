"""
Provider-specific CLI sessions aligned with the macOS LilAgents Swift implementation.

- Claude: long-lived `claude -p --output-format stream-json --input-format stream-json ...`
- Gemini / Codex / Copilot / OpenCode: one subprocess per user message (matches macOS).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.agent_session import AgentSession
from utils.cli_binary import find_cli_command

logger = logging.getLogger(__name__)


def _graceful_stop_proc(proc: Optional[subprocess.Popen], label: str) -> None:
    """Terminate then kill. Emits DEBUG logs on each escalation step."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        logger.debug("%s: terminate() raised", label, exc_info=True)
    try:
        proc.wait(timeout=3.0)
        return
    except subprocess.TimeoutExpired:
        logger.debug("%s: terminate did not exit in 3s; killing", label)
    try:
        proc.kill()
    except OSError:
        logger.debug("%s: kill() raised", label, exc_info=True)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        logger.warning("%s: process still alive after kill()", label)


def _popen_kwargs(
    *,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    env = os.environ.copy()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_bin = str(Path(appdata) / "npm")
            env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
    resolved_cwd = cwd
    if not resolved_cwd or not os.path.isdir(resolved_cwd):
        resolved_cwd = str(Path.home())
    kwargs: Dict[str, Any] = dict(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
        env=env,
        cwd=resolved_cwd,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _spawn(
    argv: List[str],
    *,
    stderr: Any = subprocess.DEVNULL,
    cwd: Optional[str] = None,
) -> subprocess.Popen:
    kwargs = _popen_kwargs(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        cwd=cwd,
    )
    exe = argv[0]
    low = exe.lower()
    if sys.platform == "win32" and low.endswith((".bat", ".cmd")):
        kwargs["shell"] = True
        return subprocess.Popen(subprocess.list2cmdline(argv), **kwargs)
    kwargs["shell"] = False
    return subprocess.Popen(argv, **kwargs)


class ClaudeCliSession(AgentSession):
    """Stream-json session (same flags as macOS ClaudeSession)."""

    def __init__(self, cwd: Optional[str] = None) -> None:
        super().__init__()
        self._exe: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._current_response = ""
        self._pending_messages: List[str] = []
        self._session_lock = threading.Lock()
        self._disk_session_id: Optional[str] = None
        self._cwd: Optional[str] = cwd

    def set_cwd(self, cwd: Optional[str]) -> None:
        """Change the working directory; takes effect on next start/restart."""
        self._cwd = cwd if cwd else None

    def get_cwd(self) -> str:
        return self._cwd if (self._cwd and os.path.isdir(self._cwd)) else str(Path.home())

    def start(self) -> None:
        if self.is_running:
            return
        self._exe = find_cli_command("claude")
        if not self._exe:
            self.error_occurred.emit(
                "Claude CLI was not found. Install from https://claude.ai/download "
                "and ensure `claude` is on PATH."
            )
            return
        args = [
            self._exe,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        try:
            kwargs = _popen_kwargs(
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
            )
            low = self._exe.lower()
            if sys.platform == "win32" and low.endswith((".bat", ".cmd")):
                kwargs["shell"] = True
                self._proc = subprocess.Popen(
                    subprocess.list2cmdline(args),
                    **kwargs,
                )
            else:
                kwargs["shell"] = False
                self._proc = subprocess.Popen(args, **kwargs)
        except Exception as exc:
            self.error_occurred.emit(f"Failed to launch Claude: {exc}")
            return

        self.is_running = True
        self._reader = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

        pending = self._pending_messages
        self._pending_messages = []
        for msg in pending:
            self.is_busy = True
            self.busy_state_changed.emit(True)
            self._current_response = ""
            self._write_user_json(msg)

    def send(self, message: str) -> None:
        # Queue the message when the subprocess isn't ready yet. This covers
        # both "start() hasn't run" (``_exe`` still None) and "start() is in
        # progress but the pipe isn't up" paths — start() drains the queue
        # once the reader threads are alive.
        if not self.is_running or not self._proc or not self._proc.stdin:
            self._pending_messages.append(message)
            return
        self.is_busy = True
        self.busy_state_changed.emit(True)
        self._current_response = ""
        self._write_user_json(message)

    def _write_user_json(self, message: str) -> None:
        payload = {"type": "user", "message": {"role": "user", "content": message}}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except Exception as exc:
            self.error_occurred.emit(f"Failed to send to Claude: {exc}")
            self._finish_turn()

    def _read_stderr_loop(self) -> None:
        if not self._proc:
            return
        try:
            for line in iter(self._proc.stderr.readline, ""):
                if not line:
                    break
                text = line.rstrip()
                if text:
                    self.error_occurred.emit(text)
        except (OSError, ValueError):
            logger.debug("Claude stderr reader stopped", exc_info=True)

    def _read_stdout_loop(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        was_running = self.is_running
        try:
            for raw_line in iter(self._proc.stdout.readline, ""):
                if not self.is_running:
                    break
                line = raw_line.strip()
                if line:
                    self._handle_json_line(line)
        finally:
            # If the CLI exited while we were still supposed to be running,
            # surface a useful diagnostic so the user isn't staring at a
            # silent popover. ``rc != 0`` usually means auth / argument /
            # crash — say so plainly and encourage a /restart.
            try:
                proc = self._proc
                if was_running and proc is not None and proc.poll() is not None:
                    rc = getattr(proc, "returncode", None)
                    if rc is not None and rc != 0:
                        self.error_occurred.emit(
                            f"Claude CLI exited with code {rc}. Try /restart, or run "
                            f"`{self._exe or 'claude'} --version` in a terminal to "
                            "check the install."
                        )
            except Exception:
                logger.debug("Claude exit-code diagnostic failed", exc_info=True)
            self.is_running = False
            self._finish_turn()

    def _note_session_id(self, obj: Dict[str, Any]) -> None:
        sid = obj.get("session_id")
        if sid is None:
            return
        s = str(sid).strip()
        if not s:
            return
        with self._session_lock:
            self._disk_session_id = s

    def get_disk_session_id(self) -> Optional[str]:
        """Claude Code session UUID from stream-json (for `claude --resume` in a real terminal)."""
        with self._session_lock:
            return self._disk_session_id

    def _handle_json_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        t = obj.get("type", "")
        if t == "system" and (obj.get("subtype") or "") == "init":
            self._note_session_id(obj)
        elif t == "assistant":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    piece = str(block["text"])
                    self._current_response += piece
                    self.text_received.emit(piece)
        elif t == "result":
            self._note_session_id(obj)
            self._finish_turn()
        elif t == "error":
            self.error_occurred.emit(str(obj.get("error") or obj.get("message") or "Claude error"))
            self._finish_turn()

    def _finish_turn(self) -> None:
        if not self.is_busy:
            return
        self.is_busy = False
        self.busy_state_changed.emit(False)
        self.turn_completed.emit()

    def terminate(self) -> None:
        self.is_running = False
        proc = self._proc
        self._proc = None
        if proc:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (OSError, ValueError):
                logger.debug("Claude stdin close raised", exc_info=True)
            _graceful_stop_proc(proc, "Claude CLI")
        with self._session_lock:
            self._disk_session_id = None
        self._finish_turn()


class _OneShotCliSession(AgentSession):
    """Base for providers that spawn a new process per send (Gemini, Codex, ...)."""

    role_user = "user"
    role_assistant = "assistant"
    role_tool_use = "tool_use"
    role_tool_result = "tool_result"
    role_error = "error"

    def __init__(self, cwd: Optional[str] = None) -> None:
        super().__init__()
        self._binary: Optional[str] = None
        self._history: List[Tuple[str, str]] = []
        self._active_proc: Optional[subprocess.Popen] = None
        self._send_lock = threading.Lock()
        self._cwd: Optional[str] = cwd

    def set_cwd(self, cwd: Optional[str]) -> None:
        self._cwd = cwd if cwd else None

    def get_cwd(self) -> str:
        return self._cwd if (self._cwd and os.path.isdir(self._cwd)) else str(Path.home())

    def start(self) -> None:
        if self.is_running:
            return
        self._binary = self._find_binary()
        if not self._binary:
            self.error_occurred.emit(self._missing_binary_message())
            return
        self.is_running = True

    def _find_binary(self) -> Optional[str]:
        raise NotImplementedError

    def _missing_binary_message(self) -> str:
        raise NotImplementedError

    def _argv_for_message(self, message: str) -> List[str]:
        raise NotImplementedError

    def _process_stdout_line(self, line: str) -> None:
        raise NotImplementedError

    def _flush_tail(self) -> None:
        pass

    def send(self, message: str) -> None:
        if not self.is_running or not self._binary:
            self.error_occurred.emit(f"{self.__class__.__name__} is not ready.")
            return
        threading.Thread(target=self._run_send, args=(message,), daemon=True).start()

    def _run_send(self, message: str) -> None:
        with self._send_lock:
            self.is_busy = True
            self.busy_state_changed.emit(True)
            self._history.append((self.role_user, message))
            argv = self._argv_for_message(message)
            try:
                proc = _spawn(argv, cwd=self._cwd)
                self._active_proc = proc
            except Exception as exc:
                self.error_occurred.emit(f"Failed to start CLI: {exc}")
                self._finish_busy()
                return

            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    one = line.strip()
                    if one:
                        self._process_stdout_line(one)
                proc.wait(timeout=600)
            except Exception as exc:
                self.error_occurred.emit(str(exc))
            finally:
                self._active_proc = None
                self._flush_tail()
                self._finish_busy()

    def _finish_busy(self) -> None:
        if not self.is_busy:
            return
        self.is_busy = False
        self.busy_state_changed.emit(False)
        self.turn_completed.emit()

    def terminate(self) -> None:
        self.is_running = False
        proc = self._active_proc
        self._active_proc = None
        if proc:
            _graceful_stop_proc(proc, f"{self.__class__.__name__}")
        self._finish_busy()

    def restart(self) -> None:
        """Drop local conversation history and, for providers that track it,
        reset the "first turn" flag. One-shot sessions don't hold a persistent
        subprocess so there's no process to bounce — just make the next send
        look like a fresh conversation.
        """
        self.terminate()
        self._history.clear()
        self.is_running = False
        self.is_busy = False
        # Providers that keep a "first turn" flag reset it in their override.
        self.start()


class GeminiCliSession(_OneShotCliSession):
    def __init__(self, cwd: Optional[str] = None) -> None:
        super().__init__(cwd=cwd)
        self._first_turn = True
        self._json_seen = False
        self._plain_buffer: List[str] = []

    def _find_binary(self) -> Optional[str]:
        return find_cli_command("gemini")

    def _missing_binary_message(self) -> str:
        return (
            "Gemini CLI was not found. Install with:\n"
            "  npm install -g @google/gemini-cli\n"
            "Then run: gemini auth\n"
            "Ensure npm global bin is on PATH (often %APPDATA%\\npm)."
        )

    def _argv_for_message(self, message: str) -> List[str]:
        exe = self._binary or "gemini"
        if self._first_turn:
            return [exe, "--yolo", "-p", message]
        return [exe, "--yolo", "--resume", "latest", "-p", message]

    def restart(self) -> None:
        super().restart()
        self._first_turn = True
        self._json_seen = False

    def _noise_line(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        low = t.lower()
        if "yolo mode is enabled" in low:
            return True
        if "automatically approved" in low:
            return True
        if "keychain initialization encountered an error" in low:
            return True
        # Gemini CLI occasionally crawls user-profile roots looking for
        # context and emits a burst of EPERM scandir warnings for locked /
        # sandboxed directories. They are not actionable inside the chat UI
        # and can arrive multiple times per prompt, so drop them.
        if "skipping unreadable directory:" in low:
            return True
        if "could not read directory" in low and "scandir" in low:
            return True
        if "operation not permitted, scandir" in low:
            return True
        if t[0] in "✓→◆⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
            return True
        return False

    def _run_send(self, message: str) -> None:
        self._json_seen = False
        self._plain_buffer = []
        with self._send_lock:
            self.is_busy = True
            self.busy_state_changed.emit(True)
            self._history.append((self.role_user, message))
            argv = self._argv_for_message(message)
            try:
                proc = _spawn(argv, stderr=subprocess.PIPE, cwd=self._cwd)
                self._active_proc = proc
            except Exception as exc:
                self.error_occurred.emit(f"Failed to start Gemini: {exc}")
                self._finish_busy()
                return

            def read_stderr() -> None:
                if not proc.stderr:
                    return
                try:
                    for line in iter(proc.stderr.readline, ""):
                        if not line:
                            break
                        if not self._noise_line(line):
                            self.error_occurred.emit(line.rstrip())
                except (OSError, ValueError):
                    logger.debug("Gemini stderr reader stopped", exc_info=True)

            threading.Thread(target=read_stderr, daemon=True).start()

            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    one = line.strip()
                    if one:
                        self._process_stdout_line(one)
                proc.wait(timeout=600)
            except Exception as exc:
                self.error_occurred.emit(str(exc))
            finally:
                # If Gemini never switched to JSON stream events, surface the
                # buffered plain text once (instead of printing duplicate lines
                # while JSON bootstrap noise is still flowing).
                if not self._json_seen and self._plain_buffer:
                    merged = "\n".join(self._plain_buffer).strip()
                    if merged:
                        self.text_received.emit(merged + "\n")
                self._active_proc = None
                self._finish_busy()
            self._first_turn = False

    def _process_stdout_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if self._noise_line(line):
                return
            if not self._json_seen:
                self._plain_buffer.append(line)
            return
        self._json_seen = True
        if self._plain_buffer:
            self._plain_buffer = []
        t = str(obj.get("type") or obj.get("event") or "")
        data = obj.get("data") or obj
        if t in ("content", "text", "delta", "message"):
            text = (
                data.get("text")
                or data.get("content")
                or obj.get("text")
                or obj.get("content")
                or ""
            )
            if text:
                self.text_received.emit(str(text))
        elif t in ("done", "end", "complete", "turn_end", "result"):
            pass
        elif t == "error":
            self.error_occurred.emit(str(data.get("message") or data.get("error") or "Gemini error"))
        else:
            text = obj.get("text") or obj.get("content")
            if text:
                self.text_received.emit(str(text))


class CodexCliSession(_OneShotCliSession):
    def _find_binary(self) -> Optional[str]:
        return find_cli_command("codex")

    def _missing_binary_message(self) -> str:
        return "Codex CLI was not found. Install with: npm install -g @openai/codex"

    def _exec_prompt(self, latest_user_message: str) -> str:
        prior = self._history[:-1]
        if not prior:
            return latest_user_message
        parts = []
        for role, text in prior:
            if role == self.role_user:
                parts.append(f"User: {text}")
            elif role == self.role_assistant:
                parts.append(f"Assistant: {text}")
            elif role == self.role_tool_use:
                parts.append(f"Tool: {text}")
            elif role == self.role_tool_result:
                parts.append(f"Tool result: {text}")
            else:
                parts.append(f"Error: {text}")
        return (
            "Conversation so far (for context; respond only to the follow-up):\n\n"
            + "\n\n".join(parts)
            + "\n\n---\n\nUser (follow-up): "
            + latest_user_message
        )

    def _argv_for_message(self, message: str) -> List[str]:
        prompt = self._exec_prompt(message)
        exe = self._binary or "codex"
        return [
            exe,
            "exec",
            "--json",
            "--full-auto",
            "--skip-git-repo-check",
            prompt,
        ]

    def _process_stdout_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        t = obj.get("type", "")
        if t == "item.completed":
            item = obj.get("item") or {}
            it = item.get("type", "")
            if it == "agent_message":
                text = item.get("text") or ""
                if text:
                    self._history.append((self.role_assistant, text))
                    self.text_received.emit(text)
            elif it == "command_execution":
                status = item.get("status", "")
                cmd = item.get("command", "")
                summary = cmd[:80] if cmd else status
                self.tool_used.emit(f"Bash: {summary}")
        elif t == "turn.completed":
            pass
        elif t == "turn.failed":
            self.error_occurred.emit(str(obj.get("message") or "Codex turn failed"))
        elif t == "error":
            self.error_occurred.emit(str(obj.get("message") or obj.get("error") or "Codex error"))


class CopilotCliSession(_OneShotCliSession):
    def __init__(self, cwd: Optional[str] = None) -> None:
        super().__init__(cwd=cwd)
        self._first_turn = True
        self._use_json = True

    def _find_binary(self) -> Optional[str]:
        return find_cli_command("copilot")

    def _missing_binary_message(self) -> str:
        return (
            "Copilot CLI was not found. Install with:\n"
            "  npm install -g @github/copilot-cli\n"
            "or: winget install GitHub.copilot-cli (if available in your region)."
        )

    def _argv_for_message(self, message: str) -> List[str]:
        exe = self._binary or "copilot"
        args: List[str] = [exe, "-p", message, "--allow-all"]
        if not self._first_turn:
            args.insert(1, "--continue")
        if self._use_json:
            args.extend(["--output-format", "json"])
        else:
            args.append("-s")
        return args

    def restart(self) -> None:
        super().restart()
        self._first_turn = True
        self._use_json = True

    def _run_send(self, message: str) -> None:
        with self._send_lock:
            self.is_busy = True
            self.busy_state_changed.emit(True)
            self._history.append((self.role_user, message))
            argv = self._argv_for_message(message)
            collected_plain = ""
            try:
                proc = _spawn(argv, cwd=self._cwd)
                self._active_proc = proc
            except Exception as exc:
                self.error_occurred.emit(f"Failed to start Copilot: {exc}")
                self._finish_busy()
                return
            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    if self._use_json:
                        one = line.strip()
                        if one:
                            self._process_stdout_line(one)
                    else:
                        collected_plain += line
                proc.wait(timeout=600)
            except Exception as exc:
                self.error_occurred.emit(str(exc))
            finally:
                if not self._use_json and collected_plain.strip():
                    t = collected_plain.strip()
                    self._history.append((self.role_assistant, t))
                    self.text_received.emit(t)
                self._active_proc = None
                self._finish_busy()
                self._first_turn = False

    def _process_stdout_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if len(self._history) <= 1:
                self._use_json = False
                if line.strip():
                    self.text_received.emit(line.strip())
            return
        if obj.get("ephemeral") is True:
            t = obj.get("type", "")
            if t == "assistant.message_delta":
                data = obj.get("data") or {}
                delta = data.get("deltaContent") or ""
                if delta:
                    self.text_received.emit(delta)
            return
        t = obj.get("type", "")
        data = obj.get("data") or {}
        if t == "assistant.message":
            content = data.get("content") or ""
            if content:
                self._history.append((self.role_assistant, content))
        elif t in ("assistant.turn_end", "result"):
            pass
        elif t == "assistant.tool_call":
            tool = data.get("name") or data.get("tool") or "Tool"
            cmd = (data.get("input") or {}).get("command") or ""
            self.tool_used.emit(f"{tool}: {cmd}" if cmd else tool)
        elif t == "error":
            self.error_occurred.emit(str(data.get("message") or data.get("error") or "Copilot error"))


class OpenCodeCliSession(_OneShotCliSession):
    def _find_binary(self) -> Optional[str]:
        return find_cli_command("opencode")

    def _missing_binary_message(self) -> str:
        return "OpenCode CLI was not found. See https://opencode.ai for install instructions."

    def _argv_for_message(self, message: str) -> List[str]:
        exe = self._binary or "opencode"
        return [exe, "run", message, "--format", "json"]

    def _process_stdout_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        t = obj.get("type", "")
        if t == "text":
            part = obj.get("part") or {}
            text = part.get("text") or ""
            if text:
                self.text_received.emit(text)
        elif t == "assistant.tool_call":
            part = obj.get("part") or {}
            name = part.get("name") or "Tool"
            self.tool_used.emit(name)
        elif t == "error":
            self.error_occurred.emit(str(obj.get("message") or "OpenCode error"))


def create_cli_session(
    provider_name: str,
    *,
    cwd: Optional[str] = None,
) -> Optional[AgentSession]:
    mapping: Dict[str, type] = {
        "Claude": ClaudeCliSession,
        "Gemini": GeminiCliSession,
        "Codex": CodexCliSession,
        "Copilot": CopilotCliSession,
        "OpenCode": OpenCodeCliSession,
    }
    cls = mapping.get(provider_name)
    if not cls:
        return None
    try:
        inst: AgentSession = cls(cwd=cwd) if cwd is not None else cls()
    except TypeError:
        # Safety net: session classes without a cwd kwarg.
        inst = cls()
    return inst
