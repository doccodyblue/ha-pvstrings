"""Every config-flow form must build, serialise and validate under real HA."""

from __future__ import annotations

import pytest
import voluptuous as vol
import voluptuous_serialize
from homeassistant.helpers import config_validation as cv

from custom_components.pvstrings import config_flow as cf


def _schemas(hass):
    return {
        "plant": cf.plant_schema(hass),
        "economics": cf.economics_schema(),
        "advanced": cf.advanced_schema(),
        "entities": cf.entities_schema(),
        "string": cf.string_schema(hass, None),
        "group": cf.group_schema(),
    }


ALL = ["plant", "economics", "advanced", "entities", "string", "group"]


@pytest.mark.parametrize("name", ALL)
def test_schema_builds(hass, name):
    """Selector configs are validated on construction."""
    assert _schemas(hass)[name] is not None


@pytest.mark.parametrize("name", ALL)
def test_schema_serialises_for_the_frontend(hass, name):
    """This is what the REST layer does before the form ever reaches a browser."""
    voluptuous_serialize.convert(
        _schemas(hass)[name], custom_serializer=cv.custom_serializer
    )


class TestRealInput:
    def test_plant_step(self, hass):
        out = cf.plant_schema(hass)(
            {
                "name": "Beispielanlage",
                "latitude": 53.5,
                "longitude": 10.0,
                "elevation": 5,
                "forecast_source": "open_meteo",
                "forecast_model": "best_match",
            }
        )
        assert out["name"] == "Beispielanlage"

    def test_plant_step_without_optional_weather_entity(self, hass):
        """Blank must mean absent, never an empty string.

        An entity selector rejects "", so a ``default=""`` would make every
        submit fail before the user has done anything wrong.
        """
        out = cf.plant_schema(hass)(
            {
                "name": "x",
                "latitude": 0.0,
                "longitude": 0.0,
                "elevation": 0,
                "forecast_source": "open_meteo",
            }
        )
        assert "weather_entity" not in out

    def test_economics_step(self):
        out = cf.economics_schema()(
            {
                "economics_mode": "net_metering",
                "price_per_kwh": 0.38,
                "feed_in_tariff": 0.08,
                "investment_eur": 3500,
                "battery_efficiency": 0.9,
            }
        )
        assert out["economics_mode"] == "net_metering"

    def test_economics_without_commissioning_date(self):
        out = cf.economics_schema()(
            {
                "economics_mode": "self_consumption",
                "price_per_kwh": 0.30,
                "feed_in_tariff": 0.08,
            }
        )
        assert "commissioning_date" not in out

    def test_string_step(self, hass):
        out = cf.string_schema(hass, None)(
            {
                "name": "Strang 1",
                "power_entity": "sensor.mppt1_panel_power",
                "azimuth": 180,
                "tilt": 30,
                "kwp": 1.8,
                "curtailment_group_id": "__none__",
            }
        )
        assert out["kwp"] == 1.8

    def test_string_step_without_optional_energy_entity(self, hass):
        out = cf.string_schema(hass, None)(
            {
                "name": "Strang 3",
                "power_entity": "sensor.ch1_power",
                "azimuth": 110,
                "tilt": 27,
                "kwp": 0.475,
            }
        )
        assert "energy_entity" not in out

    def test_group_step(self):
        out = cf.group_schema()(
            {
                "name": "Speicher",
                "limit_entity": "number.garage_limit_nonpersistent_relative",
                "inverter_max_ac_w": 1600,
                "battery_coupled": True,
                "soc_entity": "sensor.battery_soc",
                "soc_limit": 100,
            }
        )
        assert out["inverter_max_ac_w"] == 1600

    def test_group_step_with_no_entities_at_all(self):
        """A Zendure-style plant with nothing to curtail must still validate."""
        out = cf.group_schema()({"name": "Ohne Limit"})
        assert out["name"] == "Ohne Limit"

    def test_rejects_an_out_of_range_tilt(self, hass):
        with pytest.raises(vol.Invalid):
            cf.string_schema(hass, None)(
                {
                    "name": "x",
                    "power_entity": "sensor.p",
                    "azimuth": 180,
                    "tilt": 120,
                    "kwp": 1.0,
                }
            )


class TestSelectorConstraints:
    def test_unitless_numbers_omit_the_unit(self):
        """``unit_of_measurement=None`` fails the selector schema outright."""
        selector = cf._number(0.0, 1.0, 0.01)
        assert "unit_of_measurement" not in selector.config

    def test_units_survive_when_given(self):
        assert cf._number(0, 90, 1, "°").config["unit_of_measurement"] == "°"

    def test_fine_steps_become_any(self):
        assert cf._number(-90, 90, 0.00001, "°").config["step"] == "any"
