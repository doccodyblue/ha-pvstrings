"""The bundled dashboard card: vendored file, resource registration, cleanup."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from custom_components.pvstrings import card

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "pvstrings"
FOREIGN = "/hacsfiles/ha-pvstrings-dash/pvstrings-dash.js?hacstag=1"


class _StorageResources:
    """The storage collection's surface that ``card`` touches."""

    def __init__(self, items):
        self.items = {str(i): dict(item, id=str(i)) for i, item in enumerate(items)}
        self.next = len(items)

    async def async_get_info(self):
        return {"resources": len(self.items)}

    def async_items(self):
        return list(self.items.values())

    async def async_create_item(self, data):
        assert data["res_type"] == "module"
        item = {"id": f"n{self.next}", "type": "module", "url": data["url"]}
        self.next += 1
        self.items[item["id"]] = item
        return item

    async def async_update_item(self, item_id, updates):
        self.items[item_id].update(updates)

    async def async_delete_item(self, item_id):
        del self.items[item_id]


class _YamlResources:
    def __init__(self, items):
        self.data = items

    def async_items(self):
        return self.data


class _Lovelace:
    def __init__(self, resources):
        self.resources = resources


class _Http:
    def __init__(self):
        self.paths = []

    async def async_register_static_paths(self, configs):
        # aiohttp refuses a second route on the same path; so does HA.
        for config in configs:
            assert config.url_path not in [p.url_path for p in self.paths], "registered twice"
        self.paths.extend(configs)


class _Hass:
    def __init__(self, resources=None):
        self.data = {}
        if resources is not None:
            self.data["lovelace"] = _Lovelace(resources)
        self.http = _Http()


@pytest.fixture
def side_effects(monkeypatch):
    seen = {"issues": {}, "extra_js": []}

    def create(hass, domain, issue_id, **kwargs):
        seen["issues"][issue_id] = kwargs

    def delete(hass, domain, issue_id):
        seen["issues"].pop(issue_id, None)

    monkeypatch.setattr(card.ir, "async_create_issue", create)
    monkeypatch.setattr(card.ir, "async_delete_issue", delete)
    monkeypatch.setattr(card, "add_extra_js_url", lambda hass, url: seen["extra_js"].append(url))
    return seen


def _urls(resources):
    return sorted(item["url"] for item in resources.async_items())


def _run(coro):
    return asyncio.run(coro)


class TestVendoredFile:
    def test_version_matches_the_file(self):
        text = (ROOT / "www" / card.CARD_FILE).read_text()
        match = re.search(r'^const PVS_VERSION = "([^"]+)";$', text, re.MULTILINE)
        assert match, "vendored card has no PVS_VERSION"
        assert match.group(1) == card.CARD_VERSION

    def test_static_path_serves_the_www_directory(self, side_effects):
        hass = _Hass(_StorageResources([]))
        _run(card.async_register_card(hass))
        (config,) = hass.http.paths
        assert config.url_path == card.URL_BASE
        assert (Path(config.path) / card.CARD_FILE).is_file()

    def test_manifest_declares_what_card_imports(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        assert {"frontend", "http"} <= set(manifest.get("dependencies", []))


class TestRegistration:
    def test_creates_the_resource_once(self, side_effects):
        resources = _StorageResources([{"type": "module", "url": "/hacsfiles/other/x.js"}])
        hass = _Hass(resources)
        _run(card.async_register_card(hass))
        _run(card.async_register_card(hass))  # a second entry
        assert _urls(resources) == sorted(
            ["/hacsfiles/other/x.js", card.card_resource_url()]
        )
        assert not side_effects["issues"]

    def test_updates_an_older_version_and_drops_duplicates(self, side_effects):
        resources = _StorageResources(
            [
                {"type": "module", "url": f"{card.CARD_URL}?v=0.0.1"},
                {"type": "module", "url": f"{card.CARD_URL}?v=0.0.2"},
            ]
        )
        _run(card.async_register_card(_Hass(resources)))
        assert _urls(resources) == [card.card_resource_url()]

    def test_separate_copy_wins_and_raises_an_issue(self, side_effects):
        resources = _StorageResources(
            [
                {"type": "module", "url": FOREIGN},
                {"type": "module", "url": card.card_resource_url()},
            ]
        )
        _run(card.async_register_card(_Hass(resources)))
        assert _urls(resources) == [FOREIGN]
        issue = side_effects["issues"][card.ISSUE_SEPARATE_CARD]
        assert issue["translation_placeholders"] == {"url": FOREIGN}
        assert not side_effects["extra_js"]

    def test_issue_clears_once_the_copy_is_gone(self, side_effects):
        side_effects["issues"][card.ISSUE_SEPARATE_CARD] = {}
        _run(card.async_register_card(_Hass(_StorageResources([]))))
        assert not side_effects["issues"]

    def test_yaml_resources_are_not_written(self, side_effects):
        resources = _YamlResources([{"type": "module", "url": "/local/other.js"}])
        _run(card.async_register_card(_Hass(resources)))
        assert resources.data == [{"type": "module", "url": "/local/other.js"}]
        assert side_effects["extra_js"] == [card.card_resource_url()]

    def test_yaml_with_a_separate_copy_loads_nothing_extra(self, side_effects):
        resources = _YamlResources([{"type": "module", "url": "/local/pvstrings-dash.js"}])
        _run(card.async_register_card(_Hass(resources)))
        assert not side_effects["extra_js"]
        assert card.ISSUE_SEPARATE_CARD in side_effects["issues"]

    def test_without_lovelace_loads_as_extra_module(self, side_effects):
        _run(card.async_register_card(_Hass()))
        assert side_effects["extra_js"] == [card.card_resource_url()]


class TestRemoval:
    def test_removes_only_our_resource(self, side_effects):
        resources = _StorageResources(
            [
                {"type": "module", "url": card.card_resource_url()},
                {"type": "module", "url": "/hacsfiles/other/x.js"},
            ]
        )
        _run(card.async_unregister_card(_Hass(resources)))
        assert _urls(resources) == ["/hacsfiles/other/x.js"]


class TestUrls:
    @pytest.mark.parametrize(
        ("url", "own", "foreign"),
        [
            (f"{card.CARD_URL}?v=1", True, False),
            (card.CARD_URL, True, False),
            (FOREIGN, False, True),
            ("/local/pvstrings-dash.js", False, True),
            ("http://192.168.1.10:5173/pvstrings-dash.js?v=9", False, True),
            ("/hacsfiles/mushroom/mushroom.js", False, False),
        ],
    )
    def test_classification(self, url, own, foreign):
        assert card.is_own(url) is own
        assert card.is_foreign_copy(url) is foreign


def test_issue_is_translated_everywhere():
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        issues = json.loads((ROOT / name).read_text()).get("issues", {})
        entry = issues.get(card.ISSUE_SEPARATE_CARD)
        assert entry and entry["title"] and "{url}" in entry["description"], name


class TestRegisteredBeforeAnyEntry:
    """A dashboard opened while an entry is still refreshing must find the card."""

    def test_async_setup_registers(self, side_effects):
        import custom_components.pvstrings as integration

        resources = _StorageResources([])
        assert _run(integration.async_setup(_Hass(resources), {})) is True
        assert _urls(resources) == [card.card_resource_url()]

    def test_entry_setup_does_not_wait_for_it(self):
        import ast
        import inspect

        import custom_components.pvstrings as integration

        tree = ast.parse(inspect.getsource(integration.async_setup_entry))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "async_register_card" not in called

    def test_a_failure_does_not_stop_the_integration(self, side_effects, monkeypatch):
        import custom_components.pvstrings as integration

        async def boom(hass):
            raise RuntimeError("lovelace unavailable")

        monkeypatch.setattr(integration, "async_register_card", boom)
        assert _run(integration.async_setup(_Hass(_StorageResources([])), {})) is True
