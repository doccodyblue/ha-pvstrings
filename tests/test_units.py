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


class TestCollectorScaling:
    """The collector integrates raw buffer samples, so it needs the same unit
    handling as everything else -- otherwise a kW inverter is stored three
    orders of magnitude too small while the live power sensor, which does
    convert, looks perfectly correct."""

    def test_watt_entities_scale_by_one(self):
        assert units.convert(1.0, "W", units.POWER) == pytest.approx(1.0)

    def test_kilowatt_entities_scale_by_a_thousand(self):
        assert units.convert(1.0, "kW", units.POWER) == pytest.approx(1000.0)

    def test_unknown_unit_leaves_the_reading_alone(self):
        assert units.convert(1.0, "Zorkmid", units.POWER) == pytest.approx(1.0)

    def test_missing_unit_leaves_the_reading_alone(self):
        assert units.convert(1.0, None, units.POWER) == pytest.approx(1.0)


class TestEnergyPrice:
    """Every symbol below is one a real tariff integration publishes.  The
    factor of a hundred between EUR/kWh and ct/kWh is the whole reason this
    quantity is parsed rather than passed through: both readings look ordinary
    on a savings sensor, and only one of them is money."""

    def test_major_currency_per_kwh_passes_through(self):
        """Tibber publishes EUR/kWh, and so does the configured flat price."""
        assert units.convert(0.285, "EUR/kWh", units.ENERGY_PRICE) == pytest.approx(0.285)

    def test_any_currency_is_taken_at_face_value(self):
        """Nothing here converts between currencies, so SEK is just a number."""
        assert units.convert(1.42, "SEK/kWh", units.ENERGY_PRICE) == pytest.approx(1.42)
        assert units.convert(0.29, "$/kWh", units.ENERGY_PRICE) == pytest.approx(0.29)

    def test_cents_become_the_major_unit(self):
        """Nord Pool offers c/kWh; German sources write ct/kWh."""
        for symbol in ("ct/kWh", "c/kWh", "Cent/kWh", "cents/kWh", "¢/kWh"):
            assert units.convert(28.5, symbol, units.ENERGY_PRICE) == pytest.approx(0.285)

    def test_pence_and_ore_are_minor_units_too(self):
        """Octopus bills in p/kWh, the Nordics in öre."""
        assert units.convert(24.0, "p/kWh", units.ENERGY_PRICE) == pytest.approx(0.24)
        assert units.convert(24.0, "öre/kWh", units.ENERGY_PRICE) == pytest.approx(0.24)
        assert units.convert(24.0, "ore/kWh", units.ENERGY_PRICE) == pytest.approx(0.24)

    def test_per_megawatt_hour_is_a_thousandth(self):
        """Day-ahead auctions quote per MWh."""
        assert units.convert(85.0, "EUR/MWh", units.ENERGY_PRICE) == pytest.approx(0.085)

    def test_minor_units_per_megawatt_hour_compose(self):
        assert units.convert(8500.0, "ct/MWh", units.ENERGY_PRICE) == pytest.approx(0.085)

    def test_case_and_padding_do_not_matter(self):
        assert units.convert(28.5, " CT/KWH ", units.ENERGY_PRICE) == pytest.approx(0.285)

    def test_a_negative_price_keeps_its_sign(self):
        """A spot market below zero is a real state, not an error to clamp."""
        assert units.convert(-4.2, "ct/kWh", units.ENERGY_PRICE) == pytest.approx(-0.042)

    def test_an_unreadable_unit_yields_nothing_rather_than_a_guess(self):
        """The one quantity that does not pass an unknown unit through: an
        unreadable price would be stored a hundredfold wrong and look fine,
        while nothing at all falls back to the configured flat price."""
        for symbol in ("EUR", "kWh", "Zorkmid", "", None):
            assert units.convert(28.5, symbol, units.ENERGY_PRICE) is None

    def test_the_parser_reports_what_it_could_not_read(self):
        """Shared with the config flow, which blocks on None rather than
        storing prices it cannot scale."""
        assert units.parse_energy_price_unit("EUR/kWh") == pytest.approx(1.0)
        assert units.parse_energy_price_unit("ct/kWh") == pytest.approx(0.01)
        assert units.parse_energy_price_unit("EUR/MWh") == pytest.approx(0.001)
        assert units.parse_energy_price_unit("EUR/Wh") == pytest.approx(1000.0)
        for symbol in ("EUR", "kWh", "", None, "EUR/fortnight"):
            assert units.parse_energy_price_unit(symbol) is None

    def test_the_canonical_unit_names_only_the_denominator(self):
        assert units.canonical_unit(units.ENERGY_PRICE) == "/kWh"
