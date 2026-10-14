"""rebase_shading asks for a device; the picker has to offer the strings."""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
import yaml
from homeassistant.helpers import selector

from custom_components.pvstrings import REBASE_SHADING_SCHEMA

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components/pvstrings"
SERVICES = yaml.safe_load((ROOT / "services.yaml").read_text())
FIELDS = SERVICES["rebase_shading"]["fields"]


def _string_device_models() -> list[str]:
    tree = ast.parse((ROOT / "__init__.py").read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "string_device_info"
    )
    return [
        kw.value.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "model"
    ]


def test_every_selector_is_accepted_by_home_assistant():
    for name, field in FIELDS.items():
        selector.validate_selector(field["selector"])


def test_the_picker_offers_the_model_string_devices_carry():
    """A filter on a model no device has leaves the list empty, silently."""
    offered = FIELDS["device_id"]["selector"]["device"]["filter"]["model"]
    assert _string_device_models() == [offered]


def test_the_service_schema_takes_what_the_form_sends():
    data = REBASE_SHADING_SCHEMA(
        {"device_id": "abc", "azimuth": 68, "corrected_on": "2026-09-21"}
    )
    assert data["azimuth"] == 68.0
    assert set(FIELDS) >= set(map(str, REBASE_SHADING_SCHEMA.schema))


@pytest.mark.parametrize(
    "path", ["strings.json", "translations/en.json", "translations/de.json"]
)
def test_every_field_is_labelled(path):
    labels = json.loads((ROOT / path).read_text())["services"]
    assert set(labels["rebase_shading"]["fields"]) == set(FIELDS)
