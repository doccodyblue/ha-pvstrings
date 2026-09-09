"""Station temperature and wind refine the reconstruction of past hours.

The cell-temperature model reads air temperature and wind.  A configured
station sensor must reach that chain: before this the collector wrote both to
the store and the physics never read them back, so a still 35 degC afternoon
was reconstructed at whatever the forecast had said.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from core.config import INTERVAL_SECONDS, PlantConfig, WeatherSources
from core.forecast import ForecastEngine
from core.store import Store
from test_forecast_engine import DAY_START, clear_sky_forecast

HOUR = 3600
NOON = DAY_START + 12 * HOUR

#: What ``clear_sky_forecast`` writes for every hour.
FORECAST_TEMP_C = 20.0
FORECAST_WIND_MS = 2.0


def with_air(
    plant: PlantConfig, temperature: bool = True, wind: bool = True
) -> PlantConfig:
    return dataclasses.replace(
        plant,
        weather_sources=WeatherSources(
            temperature_entity="sensor.temp" if temperature else None,
            wind_speed_entity="sensor.wind" if wind else None,
        ),
    )


def write_air(
    store: Store,
    start_ts: int,
    end_ts: int,
    temp_c: float | None,
    wind_ms: float | None,
) -> None:
    store.upsert_weather_actual(
        [
            (ts, temp_c, None, wind_ms, None, None, None, None)
            for ts in range(start_ts, end_ts, INTERVAL_SECONDS)
        ]
    )


def _engine(plant: PlantConfig, store: Store) -> ForecastEngine:
    engine = ForecastEngine(plant, store)
    engine.load_models()
    clear_sky_forecast(engine, store, DAY_START - HOUR, DAY_START, 24)
    return engine


def _conditions(engine: ForecastEngine):
    index = engine._midpoint_index(DAY_START, DAY_START + 24 * HOUR)
    conditions = engine._actual_conditions(index, DAY_START, DAY_START + 24 * HOUR)
    assert conditions is not None
    starts = np.array(
        [int(value.timestamp()) - INTERVAL_SECONDS // 2 for value in index]
    )
    inside = (starts >= NOON) & (starts < NOON + HOUR)
    return index, conditions, inside


class TestStationAirReachesThePhysics:
    def test_station_values_replace_the_forecast_where_present(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = _engine(with_air(plant), seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)

        _index, conditions, inside = _conditions(engine)

        assert (conditions.loc[inside, "temp_c"] == 35.0).all()
        assert (conditions.loc[inside, "wind_ms"] == 0.0).all()
        # Everything outside the measured hour is still the forecast.
        assert (conditions.loc[~inside, "temp_c"] == FORECAST_TEMP_C).all()
        assert (conditions.loc[~inside, "wind_ms"] == FORECAST_WIND_MS).all()

    def test_air_alone_leaves_the_irradiance_split_intact(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Only a measured GHI invalidates the forecast's DNI/DHI split."""
        engine = _engine(with_air(plant), seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)

        _index, conditions, inside = _conditions(engine)

        assert conditions.loc[inside, "dni"].notna().all()
        assert conditions.loc[inside, "dhi"].notna().all()

    def test_a_hot_still_hour_lowers_the_reconstructed_power(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """The replacement must reach the cell-temperature model, not just the frame."""
        engine = _engine(with_air(plant), seeded_store)
        index, baseline, inside = _conditions(engine)
        power_before, _ = engine._interval_power(index, baseline)

        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)
        _index, hot, _inside = _conditions(engine)
        power_after, _ = engine._interval_power(index, hot)

        starts = [
            int(value.timestamp()) - INTERVAL_SECONDS // 2 for value in index
        ]
        noon_starts = [ts for ts, flag in zip(starts, inside) if flag]
        before = sum(power_before["s1"][ts] for ts in noon_starts)
        after = sum(power_after["s1"][ts] for ts in noon_starts)
        assert before > 0
        # 15 K warmer air with no wind: a few percent, and unmistakably lower.
        assert after < before * 0.98
        assert after > before * 0.85

        untouched = [ts for ts, flag in zip(starts, inside) if not flag]
        assert sum(power_after["s1"][ts] for ts in untouched) == pytest.approx(
            sum(power_before["s1"][ts] for ts in untouched)
        )


class TestStationAirIsOnlyBelievedWhenConfigured:
    def test_unconfigured_sensors_are_history_not_evidence(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Rows from a sensor that was since removed must not be read back."""
        engine = _engine(plant, seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)

        _index, conditions, _inside = _conditions(engine)

        assert (conditions["temp_c"] == FORECAST_TEMP_C).all()
        assert (conditions["wind_ms"] == FORECAST_WIND_MS).all()

    def test_only_the_configured_quantity_is_used(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = _engine(with_air(plant, temperature=True, wind=False), seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)

        _index, conditions, inside = _conditions(engine)

        assert (conditions.loc[inside, "temp_c"] == 35.0).all()
        assert (conditions.loc[inside, "wind_ms"] == FORECAST_WIND_MS).all()

    def test_a_gap_falls_back_to_the_forecast(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """A station that reports wind but no temperature leaves no hole."""
        engine = _engine(with_air(plant), seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=None, wind_ms=0.0)

        _index, conditions, inside = _conditions(engine)

        assert conditions["temp_c"].notna().all()
        assert conditions["wind_ms"].notna().all()
        assert (conditions.loc[inside, "temp_c"] == FORECAST_TEMP_C).all()
        assert (conditions.loc[inside, "wind_ms"] == 0.0).all()

    def test_out_of_range_readings_are_dropped_not_clamped(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = _engine(with_air(plant), seeded_store)
        write_air(seeded_store, NOON, NOON + HOUR, temp_c=85.0, wind_ms=-3.0)

        _index, conditions, inside = _conditions(engine)

        assert (conditions.loc[inside, "temp_c"] == FORECAST_TEMP_C).all()
        assert (conditions.loc[inside, "wind_ms"] == FORECAST_WIND_MS).all()

    def test_no_rows_at_all_is_absent_not_broken(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = _engine(with_air(plant), seeded_store)
        assert engine._measured_air(DAY_START, DAY_START + 24 * HOUR) is None
        _index, conditions, _inside = _conditions(engine)
        assert (conditions["temp_c"] == FORECAST_TEMP_C).all()
