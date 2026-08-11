"""Unit normalisation.

Every one of these is a real reading from a real station: feeding km/h into a
model that expects m/s is silent and costs a factor of 3.6 in wind cooling.
"""

from __future__ import annotations

import pytest

from core import units


class TestSpeed:
    def test_metres_per_second_pass_through(self):
        assert units.convert(3.1, "m/s", units.SPEED) == pytest.approx(3.1)

    def test_kilometres_per_hour(self):
        """An Ecowitt GW2000A reports km/h."""
        assert units.convert(11.2, "km/h", units.SPEED) == pytest.approx(3.111, abs=1e-3)

    def test_miles_per_hour(self):
        assert units.convert(10.0, "mph", units.SPEED) == pytest.approx(4.4704)

    def test_knots(self):
        assert units.convert(10.0, "kn", units.SPEED) == pytest.approx(5.14444)


class TestTemperature:
    def test_celsius_pass_through(self):
        assert units.convert(19.7, "°C", units.TEMPERATURE) == pytest.approx(19.7)

    def test_fahrenheit(self):
        assert units.convert(68.0, "°F", units.TEMPERATURE) == pytest.approx(20.0)

    def test_kelvin(self):
        assert units.convert(293.15, "K", units.TEMPERATURE) == pytest.approx(20.0)

    def test_unit_variants(self):
        for symbol in ("F", "°F", "degF", " °f "):
            assert units.convert(32.0, symbol, units.TEMPERATURE) == pytest.approx(0.0)


class TestPressure:
    def test_hpa_pass_through(self):
        assert units.convert(1015.4, "hPa", units.PRESSURE) == pytest.approx(1015.4)

    def test_inches_of_mercury(self):
        assert units.convert(29.92, "inHg", units.PRESSURE) == pytest.approx(1013.2, abs=0.5)

    def test_pascal(self):
        assert units.convert(101_325.0, "Pa", units.PRESSURE) == pytest.approx(1013.25)


class TestIrradianceAndRest:
    def test_irradiance(self):
        assert units.convert(488.95, "W/m²", units.IRRADIANCE) == pytest.approx(488.95)
        assert units.convert(0.5, "kW/m2", units.IRRADIANCE) == pytest.approx(500.0)

    def test_illuminance(self):
        assert units.convert(61892.4, "lx", units.ILLUMINANCE) == pytest.approx(61892.4)

    def test_rain_rate_counts_as_millimetres(self):
        """mm/h over an hour is numerically the hourly total."""
        assert units.convert(2.3, "mm/h", units.PRECIPITATION) == pytest.approx(2.3)
        assert units.convert(0.1, "in", units.PRECIPITATION) == pytest.approx(2.54)


class TestRobustness:
    def test_none_stays_none(self):
        assert units.convert(None, "km/h", units.SPEED) is None

    def test_missing_unit_is_passed_through(self):
        assert units.convert(5.0, None, units.SPEED) == pytest.approx(5.0)

    def test_unknown_unit_is_passed_through_not_dropped(self):
        """Refusing a reading over an unfamiliar symbol loses more than it saves."""
        assert units.convert(5.0, "furlongs/fortnight", units.SPEED) == pytest.approx(5.0)

    def test_canonical_units_are_named(self):
        assert units.canonical_unit(units.SPEED) == "m/s"
        assert units.canonical_unit(units.TEMPERATURE) == "°C"


class TestPower:
    def test_watts_pass_through(self):
        assert units.convert(1625.0, "W", units.POWER) == pytest.approx(1625.0)

    def test_kilowatts_are_scaled(self):
        """Some inverter integrations report kW, and 1.6 read as 1.6 W would
        make a string look dead."""
        assert units.convert(1.625, "kW", units.POWER) == pytest.approx(1625.0)

    def test_canonical_unit(self):
        assert units.canonical_unit(units.POWER) == "W"
