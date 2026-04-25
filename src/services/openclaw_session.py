"""OpenClaw v3 WebSocket session.

Protocol (client → gateway):

    {
        "type": "chat",
        "message": "<user text>",
        "token": "<auth token>",
        "timestamp": <unix seconds>,
        "nonce": "<hex>",
        "public_key": "<base64 Ed25519 public key>",
        "signature": "<base64 Ed25519 signature over message||.||timestamp||.||nonce>"
    }

The client holds an Ed25519 private key stored in ``%LOCALAPPDATA%/lil-agents/secrets.json``.
Its public key is written to ``openclaw_public_key.txt`` next to the secrets
file so the user can register it with the gateway.

Reconnect: if the socket closes unexpectedly (not via ``terminate()``), we
retry with exponential backoff 1s, 2s, 4s, … capped at 30s, for a maximum of
10 attempts. A user-initiated ``terminate()`` cancels further retries.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as py_secrets
import threading
import time
from typing import Optional

import websocket

from services.agent_session import AgentSession
from utils.secrets import SecretsManager

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAVE_CRYPTO = False

logger = logging.getLogger(__name__)

_MAX_RECONNECTS = 10
_BACKOFF_CAP_S = 30.0


class OpenClawSession(AgentSession):
    def __init__(self, gateway_url: str, auth_token: str = "") -> None:
        super().__init__()
        self.gateway_url = gateway_url
        self._secrets = SecretsManager()
        self.auth_token = auth_token or self._secrets.auth_token()
        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self._intentional_close = False
        self._reconnect_attempts = 0
        self._reconnect_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._signer: Optional[ed25519.Ed25519PrivateKey] = None
        self._public_key_b64: str = ""
        if _HAVE_CRYPTO:
            try:
                priv, pub = self._secrets.ensure_ed25519_keypair()
                self._signer = ed25519.Ed25519PrivateKey.from_private_bytes(priv)
                self._public_key_b64 = base64.b64encode(pub).decode()
            except Exception:
                logger.exception("Could not initialize Ed25519 signer")

    def start(self) -> None:
        if self.is_running:
            return
        if not self.gateway_url or not (
            self.gateway_url.startswith("ws://") or self.gateway_url.startswith("wss://")
        ):
            self.error_occurred.emit(
                f"Invalid OpenClaw gateway URL: {self.gateway_url!r}. Use ws:// or wss://"
            )
            return
        self._intentional_close = False
        self._reconnect_attempts = 0
        self._connect()

    def _connect(self) -> None:
        logger.info("Connecting to OpenClaw gateway %s", self.gateway_url)
        try:
            self.ws = websocket.WebSocketApp(
                self.gateway_url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
        except Exception as exc:
            self.error_occurred.emit(f"Failed to create OpenClaw socket: {exc}")
            logger.exception("WebSocketApp creation failed")
            self._schedule_reconnect()
            return
        self.ws_thread = threading.Thread(
            target=self.ws.run_forever, kwargs={"ping_interval": 30}, daemon=True
        )
        self.ws_thread.start()
        self.is_running = True

    def _sign(self, message: str, timestamp: int, nonce: str) -> str:
        if not self._signer:
            return ""
        canonical = f"{message}.{timestamp}.{nonce}".encode("utf-8")
        return base64.b64encode(self._signer.sign(canonical)).decode()

    def send(self, message: str) -> None:
        if not self.is_running or not self.ws:
            self.error_occurred.emit("OpenClaw is not connected.")
            return
        try:
            self.is_busy = True
            self.busy_state_changed.emit(True)
            timestamp = int(time.time())
            nonce = py_secrets.token_hex(12)
            signature = self._sign(message, timestamp, nonce)
            payload = {
                "type": "chat",
                "message": message,
                "token": self.auth_token,
                "timestamp": timestamp,
                "nonce": nonce,
                "public_key": self._public_key_b64,
                "signature": signature,
            }
            self.ws.send(json.dumps(payload))
        except Exception as exc:
            logger.exception("OpenClaw send failed")
            if self.is_busy:
                self.is_busy = False
                self.busy_state_changed.emit(False)
            self.error_occurred.emit(f"Failed to send message: {exc}")

    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self.text_received.emit(message)
            return
        msg_type = data.get("type")
        if msg_type == "text":
            self.text_received.emit(str(data.get("text", "")))
        elif msg_type == "done":
            self.is_busy = False
            self.busy_state_changed.emit(False)
            self.turn_completed.emit()
        elif msg_type == "error":
            self.error_occurred.emit(str(data.get("message", "Unknown error")))
            if self.is_busy:
                self.is_busy = False
                self.busy_state_changed.emit(False)
        else:
            logger.debug("OpenClaw: unknown message type %r", msg_type)

    def _on_error(self, _ws, error) -> None:
        logger.warning("OpenClaw socket error: %s", error)
        was_busy = self.is_busy
        self.is_busy = False
        if was_busy:
            self.busy_state_changed.emit(False)
        self.error_occurred.emit(f"WebSocket error: {error}")

    def _on_close(self, _ws, close_status_code, close_msg) -> None:
        logger.info(
            "OpenClaw socket closed (code=%s, msg=%r, intentional=%s)",
            close_status_code, close_msg, self._intentional_close,
        )
        was_busy = self.is_busy
        self.is_busy = False
        if was_busy:
            self.busy_state_changed.emit(False)
        if self._intentional_close:
            self.is_running = False
            return
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._lock:
            if self._intentional_close:
                return
            self._reconnect_attempts += 1
            if self._reconnect_attempts > _MAX_RECONNECTS:
                self.is_running = False
                self.error_occurred.emit(
                    "OpenClaw gateway unreachable after multiple retries. Check settings."
                )
                logger.error("OpenClaw: gave up after %d attempts", _MAX_RECONNECTS)
                return
            delay = min(_BACKOFF_CAP_S, 2.0 ** (self._reconnect_attempts - 1))
            logger.info("Reconnecting in %.1fs (attempt %d)", delay, self._reconnect_attempts)
            self.error_occurred.emit(f"Reconnecting in {delay:.0f}s (attempt {self._reconnect_attempts})…")
            if self._reconnect_timer:
                self._reconnect_timer.cancel()
            self._reconnect_timer = threading.Timer(delay, self._connect)
            self._reconnect_timer.daemon = True
            self._reconnect_timer.start()

    def _on_open(self, _ws) -> None:
        logger.info("OpenClaw gateway connected (%s)", self.gateway_url)
        self._reconnect_attempts = 0

    def terminate(self) -> None:
        self._intentional_close = True
        with self._lock:
            if self._reconnect_timer:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
        ws = self.ws
        self.ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                logger.debug("ws.close() raised", exc_info=True)
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=3.0)
        self.ws_thread = None
        self.is_running = False
        if self.is_busy:
            self.is_busy = False
            self.busy_state_changed.emit(False)
