"""Registry loads manifest, rejects malformed entries, maps providers."""

from __future__ import annotations

import json

import pytest

from services.character_registry import CharacterRegistry, get_registry, reset_registry


def test_loads_valid_manifest(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    assert reg.names() == ["bruce", "jazz"]
    assert reg.display_names() == ["Bruce", "Jazz"]
    assert reg.get("BRUCE").name == "bruce"  # case-insensitive lookup
    assert reg.get("nope") is None


def test_default_for_provider(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    assert reg.default_for_provider("Claude").name == "bruce"
    assert reg.default_for_provider("Gemini").name == "jazz"
    assert reg.default_for_provider("OpenClaw") is None
    assert reg.default_for_provider("") is None


def test_provider_character_map(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    assert reg.provider_character_map() == {"Claude": "bruce", "Gemini": "jazz"}


def test_walk_timing_parsed(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    bruce = reg.get("bruce")
    assert bruce.walk_timing.walk_amount == (0.4, 0.65)
    assert bruce.walk_timing.y_offset_px == -3


def test_personality_parsed(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    jazz = reg.get("jazz")
    assert jazz.personality.accent_color == "#8E7CFF"
    assert jazz.personality.sound_variant == "bb"
    assert "riffing..." in jazz.personality.thinking_phrases


def test_missing_manifest_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    reg = CharacterRegistry(str(missing))
    assert reg.names() == []
    assert reg.default_for_provider("Claude") is None


def test_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    reg = CharacterRegistry(str(path))
    assert reg.names() == []


def test_skips_nameless_entries(tmp_path):
    path = tmp_path / "chars.json"
    path.write_text(json.dumps({
        "characters": [
            {"display_name": "Anon"},
            {"name": "valid", "display_name": "Valid"},
        ]
    }), encoding="utf-8")
    reg = CharacterRegistry(str(path))
    assert reg.names() == ["valid"]


def test_skips_non_dict_entries(tmp_path):
    path = tmp_path / "chars.json"
    path.write_text(json.dumps({
        "characters": [
            "not a dict",
            {"name": "keeper"},
        ]
    }), encoding="utf-8")
    reg = CharacterRegistry(str(path))
    assert reg.names() == ["keeper"]


def test_duplicate_names_first_wins(tmp_path):
    path = tmp_path / "chars.json"
    path.write_text(json.dumps({
        "characters": [
            {"name": "bruce", "display_name": "BruceOne"},
            {"name": "BRUCE", "display_name": "BruceTwo"},
        ]
    }), encoding="utf-8")
    reg = CharacterRegistry(str(path))
    assert len(reg.names()) == 1
    assert reg.get("bruce").display_name == "BruceOne"


def test_get_or_first_fallback(minimal_manifest):
    reg = CharacterRegistry(minimal_manifest)
    assert reg.get_or_first("bruce").name == "bruce"
    assert reg.get_or_first("nope").name == "bruce"  # first entry


def test_get_or_first_raises_when_empty(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"characters": []}), encoding="utf-8")
    reg = CharacterRegistry(str(path))
    with pytest.raises(RuntimeError):
        reg.get_or_first("whatever")


def test_singleton_reuses_instance(minimal_manifest):
    reset_registry()
    a = get_registry(minimal_manifest)
    b = get_registry()
    assert a is b


def test_singleton_replaced_when_path_given(minimal_manifest, tmp_path):
    reset_registry()
    a = get_registry(minimal_manifest)

    other = tmp_path / "other.json"
    other.write_text(json.dumps({
        "characters": [{"name": "solo", "display_name": "Solo"}]
    }), encoding="utf-8")
    b = get_registry(str(other))
    assert b is not a
    assert b.names() == ["solo"]


def test_walk_amount_invalid_falls_back(tmp_path):
    path = tmp_path / "chars.json"
    path.write_text(json.dumps({
        "characters": [{
            "name": "odd",
            "walk_timing": {"walk_amount": "not a tuple"},
        }]
    }), encoding="utf-8")
    reg = CharacterRegistry(str(path))
    odd = reg.get("odd")
    assert odd.walk_timing.walk_amount == (0.4, 0.65)  # default
