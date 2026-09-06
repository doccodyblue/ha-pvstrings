"""Every entity name must actually resolve.

A translation key whose value is a bare string instead of ``{"name": ...}``
does not fail anywhere: Home Assistant simply does not find it and falls back
to the device-class name.  Two power sensors then both end up called "Power",
collide, and one gets a ``_2`` suffix in its entity id -- which is permanent,
because the entity registry keeps the id it was first given.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components/pvstrings"
STRINGS = json.loads((ROOT / "strings.json").read_text())
GERMAN = json.loads((ROOT / "translations/de.json").read_text())
ENGLISH = json.loads((ROOT / "translations/en.json").read_text())


def _used_translation_keys() -> set[str]:
    source = (ROOT / "sensor.py").read_text()
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "translation_key":
            if isinstance(node.value, ast.Constant):
                keys.add(node.value.value)
        # _attr_translation_key = "..."  (class level and self.-assignments)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else None
                )
                if name == "_attr_translation_key" and isinstance(
                    node.value, ast.Constant
                ):
                    keys.add(node.value.value)
    return keys


@pytest.mark.parametrize("key", sorted(_used_translation_keys()))
def test_key_is_declared_with_a_name(key: str):
    entry = STRINGS["entity"]["sensor"].get(key)
    assert entry is not None, f"{key} missing from strings.json"
    assert isinstance(entry, dict), (
        f"{key} maps to a bare string; Home Assistant looks for "
        f"entity.sensor.{key}.name and will silently fall back to the "
        "device-class name"
    )
    assert entry.get("name"), f"{key} has no name"


@pytest.mark.parametrize("key", sorted(_used_translation_keys()))
def test_key_is_translated_to_german(key: str):
    entry = GERMAN["entity"]["sensor"].get(key)
    assert isinstance(entry, dict) and entry.get("name"), f"{key} not in de.json"


def test_no_declared_key_is_unused():
    declared = set(STRINGS["entity"]["sensor"])
    unused = declared - _used_translation_keys()
    assert not unused, f"declared but never used: {sorted(unused)}"


def test_english_mirrors_the_source_strings():
    assert ENGLISH["entity"] == STRINGS["entity"]


def test_two_sensors_never_share_a_name():
    """Colliding names produce a permanent _2 suffix in the entity id."""
    for label, source in (("de", GERMAN), ("en", ENGLISH)):
        names = [v["name"] for v in source["entity"]["sensor"].values()]
        duplicates = {n for n in names if names.count(n) > 1}
        # Sensors on different devices legitimately share a name -- Home
        # Assistant prefixes the device -- so collisions only matter within one
        # device kind. Plant, string and curtailment group are three kinds.
        for kind, belongs in (
            ("plant", lambda k: not k.startswith(("string_", "group_"))),
            ("string", lambda k: k.startswith("string_")),
            ("group", lambda k: k.startswith("group_")),
        ):
            group = [
                v["name"] for k, v in source["entity"]["sensor"].items() if belongs(k)
            ]
            clashes = {n for n in group if group.count(n) > 1}
            assert not clashes, (
                f"{label}: {kind} sensors share a name: {clashes} ({duplicates})"
            )


# --------------------------------------------------------------------------- #
# flow errors
# --------------------------------------------------------------------------- #

FILES = {"strings.json": STRINGS, "en.json": ENGLISH, "de.json": GERMAN}


def _raised_error_keys() -> set[str]:
    """Every literal assigned into an ``errors`` dict in the config flow."""
    tree = ast.parse((ROOT / "config_flow.py").read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "errors"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                keys.add(node.value.value)
    return keys


def _error_blocks(data: dict) -> dict[str, dict]:
    """Every ``error`` block, keyed by where it sits."""
    blocks: dict[str, dict] = {}
    for section in ("config", "options"):
        if "error" in data.get(section, {}):
            blocks[f"{section}.error"] = data[section]["error"]
    for name, sub in data.get("config_subentries", {}).items():
        if "error" in sub:
            blocks[f"config_subentries.{name}.error"] = sub["error"]
    return blocks


@pytest.mark.parametrize("key", sorted(_raised_error_keys()))
@pytest.mark.parametrize("filename", sorted(FILES))
def test_every_flow_error_key_is_translated(filename: str, key: str):
    """An untranslated error key is not an error anywhere -- Home Assistant
    simply shows the raw key to the user, in a dialog that was already
    refusing to save."""
    resolved = any(key in block for block in _error_blocks(FILES[filename]).values())
    assert resolved, f"{key} has no message in {filename}"


@pytest.mark.parametrize("filename", sorted(set(FILES) - {"strings.json"}))
def test_the_translations_carry_the_same_error_blocks(filename: str):
    assert _error_blocks(FILES[filename]).keys() == _error_blocks(STRINGS).keys()
    for path, block in _error_blocks(STRINGS).items():
        assert _error_blocks(FILES[filename])[path].keys() == block.keys()


@pytest.mark.parametrize("filename", sorted(FILES))
def test_a_price_can_be_refused_in_both_dialogs(filename: str):
    """The price checks fire in the setup wizard and in the options dialog,
    and the options section had no error block at all before they existed."""
    blocks = _error_blocks(FILES[filename])
    for section in ("config.error", "options.error"):
        for key in (
            "price_unit_missing",
            "price_unit_unreadable",
            "price_entity_unknown",
        ):
            assert key in blocks[section], f"{key} missing from {section} in {filename}"
