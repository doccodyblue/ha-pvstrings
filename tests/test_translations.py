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
        # _attr_translation_key = "..."
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_attr_translation_key"
                    and isinstance(node.value, ast.Constant)
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
        # Plant and string sensors legitimately share names -- they live on
        # different devices -- so only flag collisions within one prefix group.
        plant = [
            v["name"]
            for k, v in source["entity"]["sensor"].items()
            if not k.startswith("string_")
        ]
        clashes = {n for n in plant if plant.count(n) > 1}
        assert not clashes, f"{label}: plant sensors share a name: {clashes} ({duplicates})"
