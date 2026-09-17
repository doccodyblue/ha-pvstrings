"""How much of the light is direct right now -- what "shading now" scales by.

A differential sky map holds the clear-day loss.  The forecast applies it to
the beam component only, so under a closed cloud deck it subtracts almost
nothing; the "shading now" sensor used to report the full clear-day loss in
the same weather.  These tests pin the input that closes that gap: the beam
share, measured where the sensor allows, from the source otherwise, and
absent -- never zero -- where neither can be computed.
"""

from __future__ import annotations

import pytest

from core.config import PlantConfig
from core.forecast import HOUR, ForecastEngine
from core.persistence import SkyState
from core.store import Store

from test_forecast_engine import DAY_START, clear_sky_forecast
from test_nowcast_engine import NOON, seed_bias, with_sensor, write_measured_ghi


def run_nowcast(engine: ForecastEngine, now_ts: int) -> None:
    """The coordinator runs the forecast first; the nowcast state comes from it."""
    engine.forecast(now_ts, hours=24, start_ts=DAY_START)


@pytest.fixture
def sensor_engine(seeded_store: Store, plant: PlantConfig) -> ForecastEngine:
    engine = ForecastEngine(with_sensor(plant), seeded_store)
    engine.load_models()
    clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 48, scale=1.0)
    return engine


class TestMeasured:
    def test_overcast_leaves_little_direct_light(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.15)
        run_nowcast(sensor_engine, NOON)
        assert sensor_engine.last_nowcast is not None

        measured, _forecast = sensor_engine.beam_share_now(NOON)

        # Erbs on a clearness of 0.15 is essentially all diffuse.
        assert measured["s1"] < 0.15

    def test_a_clear_sky_is_mostly_direct(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)
        run_nowcast(sensor_engine, NOON)

        measured, _forecast = sensor_engine.beam_share_now(NOON)

        assert measured["s1"] > 0.6

    def test_the_measurement_decides_not_the_forecast(
        self, sensor_engine, seeded_store
    ):
        """Clear forecast, dark sensor: the measured side must follow the sensor."""
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.15)
        run_nowcast(sensor_engine, NOON)

        measured, forecast = sensor_engine.beam_share_now(NOON)

        assert forecast["s1"] > 0.6
        assert measured["s1"] < forecast["s1"] - 0.4

    def test_no_measurement_without_a_usable_nowcast(
        self, sensor_engine, seeded_store
    ):
        """A stale or frozen sensor is not a measurement of now."""
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.15)
        sensor_engine.last_nowcast = None

        measured, forecast = sensor_engine.beam_share_now(NOON)

        assert measured == {}
        assert "s1" in forecast

    def test_no_measurement_without_weather_rows(self, seeded_store, plant):
        """The nowcast can run without forecast rows; the reconstruction cannot.

        The label "measured" is earned by a computed share, not by the nowcast
        state being present.
        """
        engine = ForecastEngine(with_sensor(plant), seeded_store)
        engine.load_models()
        write_measured_ghi(engine, seeded_store, NOON, factor=0.15)
        engine.last_nowcast = SkyState(
            kt=0.15, spread=None, intervals=3, halflife_s=1800.0
        )

        measured, forecast = engine.beam_share_now(NOON)

        assert measured == {}
        assert forecast == {}

    def test_a_window_inside_an_hour_still_finds_its_rows(
        self, sensor_engine, seeded_store
    ):
        """Weather rows are keyed on the hour; 12:31 must still reach 12:00."""
        seed_bias(sensor_engine, NOON)
        odd_now = NOON + 31 * 60
        write_measured_ghi(sensor_engine, seeded_store, odd_now, factor=0.15)
        run_nowcast(sensor_engine, odd_now)

        measured, forecast = sensor_engine.beam_share_now(odd_now)

        assert measured["s1"] < 0.15
        assert forecast["s1"] > 0.6


class TestForecast:
    def test_without_a_sensor_only_the_source_speaks(self, seeded_store, plant):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 48)
        run_nowcast(engine, NOON)

        measured, forecast = engine.beam_share_now(NOON)

        assert measured == {}
        assert 0.6 < forecast["s1"] <= 1.0

    def test_an_uncovered_hour_is_unknown_not_diffuse(self, seeded_store, plant):
        """No row for the running hour: absent, so the clear-day loss stays.

        Reporting 0 beam here would read as "no shade" -- optimism from
        ignorance.
        """
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 6)

        _measured, forecast = engine.beam_share_now(NOON)

        assert forecast == {}


class TestNight:
    def test_nothing_on_the_plane_is_nothing_reported(self, seeded_store, plant):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 48)

        _measured, forecast = engine.beam_share_now(DAY_START + HOUR)

        assert forecast == {}
