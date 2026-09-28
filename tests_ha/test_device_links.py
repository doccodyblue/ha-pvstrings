"""Strings and groups hang under the plant device without a deprecated call.

Issue #8: newer Home Assistant deprecates ``via_device`` (an identifier tuple)
for ``via_device_id`` (the parent's registry id) and drops it in 2027.8. The
integration supports back to 2025.9, which only knows the tuple.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

import custom_components.pvstrings as pvs


class _Registry:
    def __init__(self):
        self.calls = []

    def async_get_or_create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="plant-device-id")


@pytest.fixture
def entry():
    return SimpleNamespace(entry_id="E1", title="PV Test")


def test_the_registry_takes_a_device_id(monkeypatch):
    params = inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
    assert "via_device_id" in params


def test_devices_link_by_registry_id_on_current_ha(monkeypatch, entry):
    registry = _Registry()
    monkeypatch.setattr(pvs.dr, "async_get", lambda _hass: registry)
    for info in (
        pvs.string_device_info(object(), entry, "s1", "String 1"),
        pvs.group_device_info(object(), entry, "g1", "Group 1"),
    ):
        assert info["via_device_id"] == "plant-device-id"
        assert "via_device" not in info
    assert registry.calls[0]["identifiers"] == {(pvs.DOMAIN, "E1")}
    assert registry.calls[0]["config_entry_id"] == "E1"


def test_older_ha_keeps_the_identifier_tuple(monkeypatch, entry):
    annotations = dict(DeviceInfo.__annotations__)
    annotations.pop("via_device_id", None)
    monkeypatch.setattr(DeviceInfo, "__annotations__", annotations)
    monkeypatch.setattr(pvs.dr, "async_get", lambda _hass: pytest.fail("registry used"))
    info = pvs.string_device_info(object(), entry, "s1", "String 1")
    assert info["via_device"] == (pvs.DOMAIN, "E1")
    assert "via_device_id" not in info
