"""energy.py: what the HA Energy dashboard (and evcc) receive."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState

from custom_components.pvstrings import energy
from custom_components.pvstrings.const import DOMAIN

H0 = 1_800_003_600  # on the hour


def _hass(entry):
    entries = {entry.entry_id: entry} if entry is not None else {}
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=entries.get)
    )


def _entry(hourly, state=ConfigEntryState.LOADED, domain=DOMAIN, data=True):
    coordinator = SimpleNamespace(
        data=SimpleNamespace(energy_hourly=hourly) if data else None
    )
    return SimpleNamespace(
        entry_id="e1", domain=domain, state=state, runtime_data=coordinator
    )


def _call(entry, entry_id="e1"):
    return asyncio.run(energy.async_get_solar_forecast(_hass(entry), entry_id))


def test_the_platform_hook_has_the_energy_signature():
    params = list(inspect.signature(energy.async_get_solar_forecast).parameters)
    assert params == ["hass", "config_entry_id"]


def test_keys_are_utc_hour_starts_and_values_wh():
    result = _call(_entry([(H0, 1.2345), (H0 + 3600, 0.0)]))
    keys = list(result["wh_hours"])
    assert keys[0] == "2027-01-15T09:00:00+00:00"
    assert datetime.fromisoformat(keys[0]).timestamp() == H0
    assert result["wh_hours"] == {keys[0]: 1234.5, keys[1]: 0.0}


def test_none_while_there_is_nothing_to_publish():
    assert _call(None) is None
    assert _call(_entry([(H0, 1.0)], domain="forecast_solar")) is None
    assert _call(_entry([(H0, 1.0)], state=ConfigEntryState.SETUP_IN_PROGRESS)) is None
    assert _call(_entry([(H0, 1.0)], state=ConfigEntryState.NOT_LOADED)) is None
    assert _call(_entry([(H0, 1.0)], data=False)) is None
    assert _call(_entry([])) is None


def test_no_coordinator_import_at_module_level():
    """The energy component imports this module; it must stay light."""
    import ast

    tree = ast.parse(inspect.getsource(energy))
    modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any("coordinator" in m or m.startswith("core") for m in modules)


def test_manifest_declares_a_service_integration():
    """The Energy settings dialog lists forecast entries of type service only.

    home-assistant/frontend ha-energy-solar-source-editor.ts filters
    getConfigEntries(..., {type: ["service"]}); as "hub" the backend knew the
    platform but the entry never appeared in the dialog.
    """
    import json
    from pathlib import Path

    manifest = Path(energy.__file__).with_name("manifest.json")
    assert json.loads(manifest.read_text())["integration_type"] == "service"
