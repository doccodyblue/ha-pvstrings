"""The two look-back services: schemas under real HA, and their documentation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import voluptuous as vol
import yaml

from custom_components import pvstrings

ROOT = Path(pvstrings.__file__).parent


def test_get_day_takes_a_calendar_date():
    data = pvstrings.GET_DAY_SCHEMA({"config_entry_id": "abc", "date": "2025-06-21"})
    assert data["date"] == date(2025, 6, 21)


@pytest.mark.parametrize("value", ["2025-13-01", "yesterday", ""])
def test_get_day_refuses_what_is_not_a_date(value):
    with pytest.raises(vol.Invalid):
        pvstrings.GET_DAY_SCHEMA({"config_entry_id": "abc", "date": value})


def test_get_day_needs_both_fields():
    with pytest.raises(vol.Invalid):
        pvstrings.GET_DAY_SCHEMA({"config_entry_id": "abc"})


@pytest.mark.parametrize("service", ["get_day", "get_weeks"])
def test_every_service_is_described(service):
    """HA shows an undescribed service as a bare name and logs a warning."""
    import json

    assert service in yaml.safe_load((ROOT / "services.yaml").read_text())
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        described = json.loads((ROOT / path).read_text())["services"]
        assert set(described[service]["fields"]) >= {"config_entry_id"}
