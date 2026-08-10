"""The smallest useful plant: one string, no extras.

SPEC.md names two target systems, and the second one is a Zendure all-in-one
with one to n strings.  Such a site has no weather station, no grid meter, no
curtailment entity and no battery telemetry -- everything the integration knows
comes from Open-Meteo and a single power sensor.

These tests exist because "optional" is easy to claim and easy to break: one
attribute access on a None entity is enough to take the whole forecast down for
a user who never had that sensor in the first place.
"""

from __future__ import annotations

import pytest

from core.config import GeometrySegment, PlantConfig, StringConfig
from core.forecast import HOUR, ForecastEngine
from core.store import Store

from test_forecast_engine import DAY_START, clear_sky_forecast


@pytest.fixture
def minimal_plant() -> PlantConfig:
    """No groups, no weather sensors, no plant-state entities."""
    return PlantConfig(
        name="Balkon",
        latitude=52.52,
        longitude=13.405,
        time_zone="Europe/Berlin",
        strings=(
            StringConfig(
                string_id="only",
                name="Balkonmodul",
                power_entity="sensor.zendure_solar_power",
            ),
        ),
    )


@pytest.fixture
def minimal_engine(store: Store, minimal_plant: PlantConfig) -> ForecastEngine:
    store.add_geometry("only", GeometrySegment(0, 180, 75, 0.8, note="Balkonbrüstung"))
    engine = ForecastEngine(minimal_plant, store)
    engine.load_models()
    return engine


class TestConfiguration:
    def test_no_optional_entity_is_required(self, minimal_plant: PlantConfig):
        sources = minimal_plant.weather_sources
        assert sources.ghi_entity is None
        assert sources.temperature_entity is None
        assert minimal_plant.plant_state.grid_power_entity is None
        assert minimal_plant.groups == ()

    def test_only_the_power_sensor_is_tracked(self, minimal_plant: PlantConfig):
        assert minimal_plant.tracked_entities == ("sensor.zendure_solar_power",)

    def test_a_string_without_a_group_is_valid(self, minimal_plant: PlantConfig):
        assert minimal_plant.group_of("only") is None


class TestForecastWithoutSensors:
    def test_forecast_works_from_open_meteo_alone(
        self, minimal_engine: ForecastEngine, store: Store
    ):
        clear_sky_forecast(minimal_engine, store, DAY_START, DAY_START, 24)
        rows = minimal_engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        assert len(rows) == 24
        assert sum(r.potential_kwh for r in rows) > 0

    def test_temperature_and_wind_come_from_the_forecast(
        self, minimal_engine: ForecastEngine, store: Store
    ):
        """Local sensors only ever refine; they are never the source."""
        clear_sky_forecast(minimal_engine, store, DAY_START, DAY_START, 24)
        index = minimal_engine._midpoint_index(DAY_START, DAY_START + HOUR)
        rows = store.latest_forecast(DAY_START, DAY_START + HOUR, "open_meteo")
        conditions = minimal_engine._downscale(index, minimal_engine._hourly_frame(rows))
        assert conditions["temp_c"].notna().all()
        assert conditions["wind_ms"].notna().all()

    def test_measured_ghi_is_absent_not_broken(self, minimal_engine: ForecastEngine):
        assert minimal_engine._measured_ghi(DAY_START, DAY_START + HOUR) is None


class TestLearningWithoutSensors:
    def _measure(self, engine: ForecastEngine, store: Store, ratio: float) -> None:
        index = engine._midpoint_index(DAY_START, DAY_START + 24 * HOUR)
        conditions = engine._actual_conditions(index, DAY_START, DAY_START + 24 * HOUR)
        power = engine._interval_power(index, conditions)
        rows = [
            (ts, sid, watts * ratio * 300 / 3600, watts * ratio, 1.0, 10, None, None,
             "measured")
            for sid, series in power.items()
            for ts, watts in series.items()
        ]
        store.upsert_5min(rows)

    def test_full_cycle_without_any_optional_entity(
        self, minimal_engine: ForecastEngine, store: Store
    ):
        clear_sky_forecast(minimal_engine, store, DAY_START - HOUR, DAY_START, 24)
        self._measure(minimal_engine, store, ratio=0.85)
        stats = minimal_engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        assert stats.observations_used > 0
        assert minimal_engine.model.factor("only", "clear", "midday") < 1.0

    def test_ghi_bias_still_learns_from_the_sources_own_runs(
        self, minimal_engine: ForecastEngine, store: Store
    ):
        """No pyranometer: the shortest-horizon run stands in for truth."""
        clear_sky_forecast(
            minimal_engine, store, DAY_START - 26 * HOUR, DAY_START, 24, scale=1.25
        )
        clear_sky_forecast(
            minimal_engine, store, 0, DAY_START, 24, scale=1.0, lead_time_h=1
        )
        stats = minimal_engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        assert stats.bias_observations > 0

    def test_curtailment_evaluation_is_a_no_op_without_limits(
        self, minimal_engine: ForecastEngine, store: Store
    ):
        clear_sky_forecast(minimal_engine, store, DAY_START - HOUR, DAY_START, 24)
        self._measure(minimal_engine, store, ratio=1.0)
        minimal_engine.evaluate_curtailment(DAY_START, DAY_START + 24 * HOUR)

        rows = store.fivemin_range("only", DAY_START, DAY_START + 24 * HOUR)
        assert rows
        assert all(row["limit_binding"] is None for row in rows)
        assert all(row["value_kind"] == "measured" for row in rows)


class TestEconomicsWithoutGridMeter:
    def test_everything_counts_as_self_used(self, minimal_engine: ForecastEngine):
        from core.economics import savings

        result = savings(8.0, None, minimal_engine.plant.economics)
        assert result.self_used_kwh == pytest.approx(8.0)
        assert result.saved_eur > 0

    def test_seasonal_weights_need_no_sensors(self, minimal_engine: ForecastEngine):
        weights = minimal_engine.monthly_weights()
        assert len(weights) == 12
        assert sum(weights) == pytest.approx(1.0)
