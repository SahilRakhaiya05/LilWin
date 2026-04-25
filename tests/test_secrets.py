"""Secrets round-trip + Ed25519 keypair persistence."""

from __future__ import annotations

import base64
import json
import os

import pytest

from utils.secrets import SecretsManager, secrets_path


def test_default_path_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LIL_AGENTS_SECRETS_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert secrets_path() == os.path.join(str(tmp_path), "lil-agents", "secrets.json")


def test_override_path(tmp_path, monkeypatch):
    custom = tmp_path / "custom-secrets.json"
    monkeypatch.setenv("LIL_AGENTS_SECRETS_PATH", str(custom))
    assert secrets_path() == str(custom)


def test_load_missing_returns_empty(tmp_path):
    sm = SecretsManager(str(tmp_path / "does-not-exist.json"))
    assert sm.load() == {}


def test_load_malformed_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid", encoding="utf-8")
    sm = SecretsManager(str(path))
    assert sm.load() == {}


def test_round_trip_auth_token(tmp_path):
    sm = SecretsManager(str(tmp_path / "s.json"))
    sm.set_auth_token("shh-abc-123")
    # Reinstantiate to prove it survives on disk, not just cache.
    sm2 = SecretsManager(str(tmp_path / "s.json"))
    assert sm2.auth_token() == "shh-abc-123"


def test_auth_token_default_empty(tmp_path):
    sm = SecretsManager(str(tmp_path / "s.json"))
    assert sm.auth_token() == ""


def test_set_and_get_arbitrary_key(tmp_path):
    sm = SecretsManager(str(tmp_path / "s.json"))
    sm.set("customKey", {"nested": 42})
    sm2 = SecretsManager(str(tmp_path / "s.json"))
    assert sm2.get("customKey") == {"nested": 42}
    assert sm2.get("unknown", "fallback") == "fallback"


@pytest.mark.skipif(
    pytest.importorskip("cryptography", reason="cryptography not installed") is None,
    reason="cryptography required",
)
def test_ed25519_keypair_generated_then_reused(tmp_path):
    path = tmp_path / "s.json"
    sm = SecretsManager(str(path))
    priv1, pub1 = sm.ensure_ed25519_keypair()
    assert len(priv1) == 32
    assert len(pub1) == 32

    # Second call returns the same bytes; file has been written.
    priv2, pub2 = sm.ensure_ed25519_keypair()
    assert priv1 == priv2
    assert pub1 == pub2

    data = json.loads(path.read_text(encoding="utf-8"))
    assert base64.b64decode(data["openclawPrivateKeyB64"]) == priv1
    assert base64.b64decode(data["openclawPublicKeyB64"]) == pub1


def test_ed25519_public_key_exported(tmp_path):
    pytest.importorskip("cryptography")
    path = tmp_path / "s.json"
    sm = SecretsManager(str(path))
    sm.ensure_ed25519_keypair()
    exported = tmp_path / "openclaw_public_key.txt"
    assert exported.is_file()
    # Exported line matches stored base64.
    assert exported.read_text(encoding="utf-8").strip() == sm.public_key_b64()


def test_save_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "secrets.json"
    sm = SecretsManager(str(nested))
    sm.set_auth_token("t")
    assert nested.is_file()
