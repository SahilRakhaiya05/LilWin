"""Regression tests for the greeting user-name resolver.

The old popover displayed the character's greeting prefixed with the
character's own display name ("Jazz: Hey, Jazz …"), which read as if the
character were addressing themselves. The resolver now substitutes the
real OS user name in at the template level so greetings read naturally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _import_resolver():
    # Importing character_window requires PyQt6; the resolver helpers are
    # module-level pure-Python, but the top-level imports still pull Qt.
    # Guard so CI without PyQt can at least try, and skip gracefully if it
    # can't.
    try:
        from ui import character_window as cw  # type: ignore
    except Exception:  # pragma: no cover - exercised only when PyQt missing
        import pytest

        pytest.skip("PyQt6 not available — cannot import character_window")
    return cw


def test_user_placeholder_is_substituted():
    cw = _import_resolver()
    with mock.patch.dict(os.environ, {"USERNAME": "admin", "USER": ""}, clear=False):
        out = cw._resolve_greeting_for_user("Hey {user}, Bruce here")
    assert out == "Hey Admin, Bruce here"


def test_domain_prefix_is_stripped():
    cw = _import_resolver()
    with mock.patch.dict(os.environ, {"USERNAME": "CORP\\alice", "USER": ""}, clear=False):
        out = cw._resolve_greeting_for_user("Hey {user}!")
    assert out == "Hey Alice!"


def test_legacy_character_greeting_gets_user_inserted():
    """Old-style 'Hey, Jazz. ...' should read like 'Hey, <User>. ...'.

    This protects users who haven't yet migrated their characters.json
    greetings to the {user} placeholder style from seeing the awkward
    "Jazz addressing Jazz" output.
    """
    cw = _import_resolver()
    with mock.patch.dict(os.environ, {"USERNAME": "pat", "USER": ""}, clear=False):
        out = cw._resolve_greeting_for_user("Hey, Jazz. Let's make something cool.")
    assert out.startswith("Hey, Pat.")
    assert "Jazz" not in out  # the awkward self-address is gone


def test_legacy_here_greeting_preserves_character_name():
    """'Hey, Bruce here — what are we building?' should still mention Bruce
    but address the user by name up front."""
    cw = _import_resolver()
    with mock.patch.dict(os.environ, {"USERNAME": "sam", "USER": ""}, clear=False):
        out = cw._resolve_greeting_for_user("Hey, Bruce here — what are we building?")
    assert "Sam" in out
    assert "Bruce" in out


def test_empty_greeting_returns_empty():
    cw = _import_resolver()
    assert cw._resolve_greeting_for_user("") == ""
    assert cw._resolve_greeting_for_user(None) == ""


def test_user_name_falls_back_when_env_empty():
    cw = _import_resolver()
    with mock.patch.dict(os.environ, {"USERNAME": "", "USER": ""}, clear=False):
        # Even without env vars we should get a non-empty, title-case name
        # (whatever getpass.getuser() returns on this host, or "friend").
        name = cw._current_os_user_name()
    assert isinstance(name, str) and name


def test_partial_directory_auto_match(tmp_path):
    cw = _import_resolver()
    (tmp_path / "Documents").mkdir()
    resolved, suggestions = cw._resolve_existing_dirish_path("Doc", str(tmp_path))
    assert resolved == str(tmp_path / "Documents")
    assert suggestions == []


def test_partial_directory_ambiguous_returns_suggestions(tmp_path):
    cw = _import_resolver()
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Downloads").mkdir()
    resolved, suggestions = cw._resolve_existing_dirish_path("Do", str(tmp_path))
    assert resolved is None
    assert str(tmp_path / "Documents") in suggestions
    assert str(tmp_path / "Downloads") in suggestions
