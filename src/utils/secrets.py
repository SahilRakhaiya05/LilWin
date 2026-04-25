"""Secrets storage, separate from ``config/settings.json``.

Holds:
 - OpenClaw auth token
 - OpenClaw Ed25519 signing keypair (private key bytes + public key bytes, base64)

File lives at ``%LOCALAPPDATA%/lil-agents/secrets.json`` on Windows, or
``~/.lil-agents/secrets.json`` elsewhere. On Windows we tighten the ACL to the
current user so only they can read the file.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAVE_CRYPTO = False

from utils.logging_config import app_data_dir

logger = logging.getLogger(__name__)


def secrets_path() -> str:
    override = os.environ.get("LIL_AGENTS_SECRETS_PATH")
    if override:
        return override
    return os.path.join(app_data_dir(), "secrets.json")


def _restrict_acl(path: str) -> None:
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.debug("chmod failed", exc_info=True)
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("icacls failed", exc_info=True)


class SecretsManager:
    """Load / save a small JSON secrets file with tightened ACL."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or secrets_path()
        self._cache: Optional[Dict[str, Any]] = None

    def load(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not os.path.isfile(self.path):
            self._cache = {}
            return self._cache
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._cache = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read secrets at %s; starting fresh", self.path, exc_info=True)
            self._cache = {}
        return self._cache

    def save(self, data: Dict[str, Any]) -> None:
        self._cache = dict(data)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            logger.error("Could not create secrets dir")
            return
        new_file = not os.path.isfile(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2)
        if new_file:
            _restrict_acl(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = dict(self.load())
        data[key] = value
        self.save(data)

    def auth_token(self) -> str:
        return str(self.get("authToken", "") or "")

    def set_auth_token(self, token: str) -> None:
        self.set("authToken", token or "")

    def ensure_ed25519_keypair(self) -> Tuple[bytes, bytes]:
        """Return (private_key_bytes, public_key_bytes), generating if absent."""
        if not _HAVE_CRYPTO:
            raise RuntimeError("cryptography library not installed")
        data = self.load()
        priv_b64 = data.get("openclawPrivateKeyB64")
        pub_b64 = data.get("openclawPublicKeyB64")
        if priv_b64 and pub_b64:
            return base64.b64decode(priv_b64), base64.b64decode(pub_b64)

        sk = ed25519.Ed25519PrivateKey.generate()
        pk = sk.public_key()
        priv = sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub = pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        data["openclawPrivateKeyB64"] = base64.b64encode(priv).decode()
        data["openclawPublicKeyB64"] = base64.b64encode(pub).decode()
        self.save(data)
        self._export_public_key(base64.b64encode(pub).decode())
        logger.info("Generated new Ed25519 keypair for OpenClaw")
        return priv, pub

    def public_key_b64(self) -> str:
        return str(self.get("openclawPublicKeyB64", "") or "")

    def _export_public_key(self, pub_b64: str) -> None:
        try:
            path = os.path.join(os.path.dirname(self.path), "openclaw_public_key.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(pub_b64 + "\n")
        except OSError:
            logger.debug("public key export failed", exc_info=True)
