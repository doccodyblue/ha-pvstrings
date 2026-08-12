"""End-to-end tests of the orchestrator against a synthetic plant.

The database is real, the physics is real; only the weather and the meter
readings are synthesised.  That is enough to exercise the parts most likely to
be wrong in a way unit tests cannot see: the downscaling, the interaction of
curtailment with learning, and the scoring split.
"""

from __future__ import annotations

import math

import pytest

from core.config import GeometrySegment, PlantConfig
from core.forecast import HOUR, ForecastEngine, floor_hour
from core.physics import PhysicsEngine, to_index
from core.store import Store

#: 2025-06-21, local midnight in Europe/Berlin, on the hour grid.
DAY_START = 1_750_456_800
NOON = DAY_START + 12 * HOUR


@pytest.fixture
def engine(seeded_store: Store, plant: PlantConfig) -> ForecastEngine:
    engine = ForecastEngine(plant, seeded_store)
    engine.load_models()
    return engine


def clear_sky_forecast(
    engine: ForecastEngine,
    store: Store,
    issued_at: int,
    start_ts: int,
    hours: int,
    scale: float = 1.0,
    clouds: float = 0.0,
    lead_time_h: int | None = None,
) -> None:
    """Write a physically consistent forecast derived from the clear-sky model.

    With ``lead_time_h`` every hour gets its own issue that many hours before
    the target, which is what a coordinator polling every 30 minutes actually
    produces.  Without it, one issue covers the whole window.
    """
    stamps = [start_ts + index * HOUR + HOUR / 2 for index in range(hours)]
    idx = to_index(stamps)
    solar_position = engine.physics.solar_position(idx)
    clear = engine.physics.clearsky(idx, solar_position=solar_position)

    rows = []
    for offset in range(hours):
        ts = start_ts + offset * HOUR
        issue = ts - lead_time_h * HOUR if lead_time_h is not None else issued_at
        rows.append(
            (
                issue,
                ts,
                "open_meteo",
                int((ts - issue) / HOUR),
                float(clear["ghi"].iloc[offset]) * scale,
                float(clear["dni"].iloc[offset]) * scale,
                float(clear["dhi"].iloc[offset]) * scale,
                20.0,
                clouds,
                2.0,
                60.0,
                0.0,
                1013.0,
                1,
            )
        )
    store.upsert_weather_forecast(rows)


def write_measurements(
    store: Store,
    string_id: str,
    hour_ts: int,
    power_w: float,
    limit_w: float | None = None,
    coverage: float = 1.0,
) -> None:
    """Twelve five-minute rows making up one hour at constant power."""
    rows = []
    for step in range(12):
        ts = hour_ts + step * 300
        rows.append(
            (
                ts,
                string_id,
                power_w * 300 / 3600,
                power_w,
                coverage,
                10,
                limit_w,
                None,
                "measured",
            )
        )
    store.upsert_5min(rows)


class TestForecast:
    def test_no_weather_data_yields_no_forecast(self, engine: ForecastEngine):
        assert engine.forecast(DAY_START, hours=24) == []

    def test_clear_day_produces_every_string(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)

        assert {row.string_id for row in rows} == {"s1", "s2", "s3"}
        assert len(rows) == 24 * 3

        daily = {}
        for row in rows:
            daily[row.string_id] = daily.get(row.string_id, 0.0) + row.potential_kwh
        # 1.8 kWp south at 30 deg on a clear June day in northern Germany.
        assert 6.0 < daily["s1"] < 13.0
        assert all(value > 0 for value in daily.values())

    def test_shallow_tilt_beats_steep_tilt_in_june(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """s1 and s2 face the same way; only the tilt differs."""
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        per_kwp = {}
        for row in rows:
            per_kwp.setdefault(row.string_id, 0.0)
            per_kwp[row.string_id] += row.potential_kwh
        assert per_kwp["s1"] / 1.80 > per_kwp["s2"] / 1.00

    def test_night_hours_are_zero(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        midnight = [row for row in rows if row.ts_utc == DAY_START]
        assert all(row.potential_kwh == pytest.approx(0.0) for row in midnight)

    def test_overcast_forecast_yields_less(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24, scale=1.0)
        bright = sum(
            row.potential_kwh
            for row in engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        )
        clear_sky_forecast(
            engine, seeded_store, DAY_START + 1, DAY_START, 24, scale=0.25, clouds=95
        )
        dull = sum(
            row.potential_kwh
            for row in engine.forecast(DAY_START + 1, hours=24, start_ts=DAY_START)
        )
        assert dull < bright * 0.5

    def test_downscaling_preserves_the_clearsky_shape(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Holding GHI flat across an hour is badly wrong near sunrise; the
        clear-sky index must carry the shape instead."""
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        # The sunrise hour: clear-sky GHI climbs from ~0 to ~46 W/m2 within it.
        sunrise_hour = DAY_START + 5 * HOUR
        index = engine._midpoint_index(sunrise_hour, sunrise_hour + HOUR)
        rows = seeded_store.latest_forecast(
            sunrise_hour, sunrise_hour + HOUR, "open_meteo"
        )
        conditions = engine._downscale(index, engine._hourly_frame(rows))
        values = conditions["ghi"].to_numpy()
        assert values[-1] > values[0] * 10  # a flat hourly value would be constant
        assert values.std() > 0

    def test_geometry_history_is_honoured_per_timestamp(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Old hours must keep using the tilt that was actually installed."""
        switch = DAY_START + 12 * HOUR
        seeded_store.add_geometry(
            "s2", GeometrySegment(switch, 180, 15, 1.0, note="flattened at noon")
        )
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)

        morning = [r for r in rows if r.string_id == "s2" and r.ts_utc == DAY_START + 8 * HOUR]
        afternoon = [r for r in rows if r.string_id == "s2" and r.ts_utc == DAY_START + 14 * HOUR]
        assert morning and afternoon

        seeded_store.delete_geometry("s2", switch)
        engine.store._geometry_cache.clear()
        unchanged = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        afternoon_steep = [
            r for r in unchanged if r.string_id == "s2" and r.ts_utc == DAY_START + 14 * HOUR
        ]
        # A 60 deg panel in June yields less at 14:00 than a 15 deg one.
        assert afternoon_steep[0].potential_kwh < afternoon[0].potential_kwh

    def test_forecast_is_logged_for_later_scoring(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)

        written = engine.log_forecast(DAY_START, rows)
        # The hour already running is not logged -- otherwise the quantised
        # issue time would make it look like a forecast that predates itself.
        assert written == len(rows) - len(engine.plant.strings)

        logged = seeded_store.forecast_vs_actual(DAY_START, DAY_START + 24 * HOUR)
        assert all(row["potential_kwh"] is not None for row in logged) or not logged

    def test_repeated_runs_within_an_hour_do_not_pile_up(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The coordinator recomputes every 15 minutes; the log must not grow
        four horizons per hour."""
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)

        for minute_offset in (0, 900, 1800, 2700):
            engine.log_forecast(DAY_START + minute_offset, rows)

        stored = seeded_store.statistics()["forecast_log"]
        assert stored == len(rows) - len(engine.plant.strings)


class TestCurtailmentEvaluation:
    def test_limit_far_above_output_is_not_binding(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        # s1 and s2 share the "battery" group, so both must be present before
        # the group's total can be compared with its limit.
        write_measurements(seeded_store, "s1", NOON, power_w=300.0, limit_w=1600.0)
        write_measurements(seeded_store, "s2", NOON, power_w=150.0, limit_w=1600.0)
        engine.evaluate_curtailment(NOON, NOON + HOUR)

        rows = seeded_store.fivemin_range("s1", NOON, NOON + HOUR)
        assert all(row["limit_binding"] == 0 for row in rows)
        assert all(row["value_kind"] == "measured" for row in rows)

    def test_the_limit_is_tested_against_the_group_total(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """A 500 W limit covering two strings is reached by their *sum*.

        Neither string alone comes close to it -- testing them individually
        would report "not binding" for a group that is plainly clipped.
        """
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        write_measurements(seeded_store, "s1", NOON, power_w=300.0, limit_w=500.0)
        write_measurements(seeded_store, "s2", NOON, power_w=200.0, limit_w=500.0)
        engine.evaluate_curtailment(NOON, NOON + HOUR)

        for string_id in ("s1", "s2"):
            rows = seeded_store.fivemin_range(string_id, NOON, NOON + HOUR)
            assert all(row["limit_binding"] == 1 for row in rows), string_id
            assert all(row["value_kind"] == "lower_bound" for row in rows)

    def test_incomplete_group_data_yields_no_verdict(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Half a sum understates the group and would hide a binding limit."""
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        write_measurements(seeded_store, "s1", NOON, power_w=400.0, limit_w=500.0)
        engine.evaluate_curtailment(NOON, NOON + HOUR)

        rows = seeded_store.fivemin_range("s1", NOON, NOON + HOUR)
        assert all(row["limit_binding"] is None for row in rows)
        assert all(row["value_kind"] == "measured" for row in rows)

    def test_no_limit_leaves_the_flag_unknown(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        write_measurements(seeded_store, "s3", NOON, power_w=300.0, limit_w=None)
        engine.evaluate_curtailment(NOON, NOON + HOUR)
        rows = seeded_store.fivemin_range("s3", NOON, NOON + HOUR)
        assert all(row["limit_binding"] is None for row in rows)


class TestMaterialisation:
    def test_hourly_is_derived_from_five_minute_rows(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        write_measurements(seeded_store, "s1", NOON, power_w=600.0)
        engine.materialise_hourly(NOON, NOON + HOUR)

        rows = seeded_store.hourly_range(NOON, NOON + HOUR, "s1")
        assert len(rows) == 1
        assert rows[0].energy_kwh == pytest.approx(0.6)
        assert rows[0].quality == "exact"

    def test_partial_coverage_is_reported_not_hidden(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        write_measurements(seeded_store, "s1", NOON, power_w=600.0, coverage=0.5)
        engine.materialise_hourly(NOON, NOON + HOUR)
        assert seeded_store.hourly_range(NOON, NOON + HOUR, "s1")[0].quality == "missing"

    def test_night_hour_is_classified_as_night(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        midnight = DAY_START
        write_measurements(seeded_store, "s1", midnight, power_w=0.0, coverage=0.2)
        engine.materialise_hourly(midnight, midnight + HOUR)
        assert (
            seeded_store.hourly_range(midnight, midnight + HOUR, "s1")[0].quality
            == "night"
        )


class TestLearningCycle:
    def _prepare_day(
        self,
        engine: ForecastEngine,
        store: Store,
        ratio: float = 1.0,
        clip_w: float | None = None,
    ) -> None:
        """A whole clear day of measurements derived from the physics itself.

        ``ratio`` scales the output uniformly.  ``clip_w`` additionally caps it
        the way a real inverter limit does -- ``min(physics, limit)`` -- which
        is the only shape from which the binding test can conclude anything.
        """
        clear_sky_forecast(engine, store, DAY_START - HOUR, DAY_START, 24)
        index = engine._midpoint_index(DAY_START, DAY_START + 24 * HOUR)
        conditions = engine._actual_conditions(
            index, DAY_START, DAY_START + 24 * HOUR
        )
        power = engine._interval_power(index, conditions)

        rows = []
        for string_id, series in power.items():
            for ts, watts in series.items():
                measured = watts * ratio
                # Only clip strings that belong to a group -- a groupless
                # string has no limit entity, so the collector never records a
                # limit for it and nothing could detect the clipping.
                grouped = engine.plant.string(string_id).curtailment_group_id
                if clip_w is not None and grouped:
                    measured = min(measured, clip_w)
                rows.append(
                    (
                        ts,
                        string_id,
                        measured * 300 / 3600,
                        measured,
                        1.0,
                        10,
                        clip_w if grouped else None,
                        None,
                        "measured",
                    )
                )
        store.upsert_5min(rows)

    def test_a_consistent_shortfall_is_learned(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._prepare_day(engine, seeded_store, ratio=0.80)
        before = engine.model.factor("s1", "clear", "midday")
        stats = engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        assert stats.observations_used > 0
        after = engine.model.factor("s1", "clear", "midday")
        assert after < before
        assert after < 1.0

    def test_learning_moves_the_forecast_towards_reality(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._prepare_day(engine, seeded_store, ratio=0.80)
        raw = engine.forecast(
            DAY_START, hours=24, start_ts=DAY_START, apply_learning=False
        )
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        corrected = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)

        raw_total = sum(row.potential_kwh for row in raw)
        corrected_total = sum(row.potential_kwh for row in corrected)
        assert corrected_total < raw_total

    def test_curtailed_day_does_not_drag_the_model_down(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The hinge rule, end to end: a summer of clipping must not teach the
        model that the strings are weak.

        The string tracks physics exactly until the 200 W limit bites, and is
        pinned there for the rest of the day.  Every one of those clipped hours
        is a lower bound, and a lower bound may never push the model down.
        """
        self._prepare_day(engine, seeded_store, ratio=1.0, clip_w=200.0)
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        rows = seeded_store.hourly_range(DAY_START, DAY_START + 24 * HOUR, "s1")
        assert any(row.value_kind == "lower_bound" for row in rows), (
            "the scenario must actually produce censored hours"
        )
        assert engine.model.factor("s1", "clear", "midday") >= 0.99

    def test_state_survives_a_reload(
        self, engine: ForecastEngine, seeded_store: Store, plant: PlantConfig
    ):
        self._prepare_day(engine, seeded_store, ratio=0.80)
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        learned = engine.model.factor("s1", "clear", "midday")

        reloaded = ForecastEngine(plant, seeded_store)
        reloaded.load_models()
        assert reloaded.model.factor("s1", "clear", "midday") == pytest.approx(learned)

    def test_shading_observations_are_collected_raw(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._prepare_day(engine, seeded_store, ratio=0.80)
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        assert seeded_store.shading_count("s1") > 0

    def test_cursor_prevents_relearning_the_same_hours(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._prepare_day(engine, seeded_store, ratio=0.80)
        first = engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        second = engine.learn(DAY_START + 24 * HOUR, max_hours=24)
        assert first.observations_used > 0
        assert second.observations_used == 0

    def test_ghi_bias_learns_from_horizon_disagreement(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """A day-ahead run that was 30 % too optimistic must be corrected, while
        the nowcast bucket stays neutral."""
        clear_sky_forecast(
            engine, seeded_store, DAY_START - 26 * HOUR, DAY_START, 24, scale=1.3
        )
        # What the source itself said an hour out is our stand-in for truth.
        clear_sky_forecast(
            engine, seeded_store, 0, DAY_START, 24, scale=1.0, lead_time_h=1
        )
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        noon_local = 14  # 12:00 UTC in June is 14:00 in Europe/Berlin
        assert engine.ghi_bias.factor(noon_local, 30.0) < 1.0
        assert engine.ghi_bias.factor(noon_local, 1.0) == pytest.approx(1.0)


class TestScoring:
    def _score_day(self, engine: ForecastEngine, store: Store, actual_ratio: float):
        clear_sky_forecast(engine, store, DAY_START - HOUR, DAY_START, 24)
        rows = engine.forecast(
            DAY_START, hours=24, start_ts=DAY_START, apply_learning=False
        )
        engine.log_forecast(DAY_START - HOUR, rows)

        hourly = []
        for row in rows:
            hourly.append(
                (
                    row.ts_utc,
                    row.string_id,
                    row.potential_kwh * actual_ratio,
                    1.0,
                    0.0,
                    None,
                    None,
                    None,
                    "measured",
                    "exact" if row.potential_kwh > 0 else "night",
                )
            )
        store.upsert_hourly(hourly)

    def test_perfect_forecast_scores_zero(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._score_day(engine, seeded_store, actual_ratio=1.0)
        result = engine.score(DAY_START, DAY_START + 24 * HOUR)
        assert result["uncensored"]["wmape"] == pytest.approx(0.0, abs=1e-6)
        assert result["uncensored"]["bias"] == pytest.approx(0.0, abs=1e-9)

    def test_overforecast_shows_positive_bias(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._score_day(engine, seeded_store, actual_ratio=0.8)
        result = engine.score(DAY_START, DAY_START + 24 * HOUR)
        assert result["uncensored"]["wmape"] == pytest.approx(0.25, rel=0.02)
        assert result["uncensored"]["bias"] > 0

    def test_censored_hours_are_reported_separately(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Only the uncensored figure describes model quality; reporting one
        number without saying which is how "78.6 % accuracy" means nothing."""
        self._score_day(engine, seeded_store, actual_ratio=1.0)
        rows = seeded_store.hourly_range(DAY_START, DAY_START + 24 * HOUR)
        clipped = [
            (
                row.ts_utc,
                row.string_id,
                (row.energy_kwh or 0.0) * 0.3,
                row.coverage,
                1.0,
                None,
                None,
                None,
                "lower_bound",
                row.quality,
            )
            for row in rows
            if row.string_id == "s1"
        ]
        seeded_store.upsert_hourly(clipped)

        result = engine.score(DAY_START, DAY_START + 24 * HOUR)
        assert result["hours_uncensored"] < result["hours_scored"]
        assert result["uncensored"]["wmape"] == pytest.approx(0.0, abs=1e-6)
        assert result["all_hours"]["wmape"] > 0.1

    def test_empty_window_returns_nulls_not_crashes(self, engine: ForecastEngine):
        result = engine.score(DAY_START, DAY_START + HOUR)
        assert result["uncensored"]["wmape"] is None
        assert result["hours_scored"] == 0


def test_monthly_weights_are_geometry_weighted(engine: ForecastEngine):
    weights = engine.monthly_weights()
    assert len(weights) == 12
    assert math.isclose(sum(weights), 1.0, abs_tol=1e-9)
    assert sum(weights[3:8]) > 0.45


class TestTrackerCeiling:
    """A micro-inverter caps each tracker far below the module's capability.

    No limit entity ever reports that ceiling, so without modelling it the
    learning layer sees a string that "weakens in bright sun" -- a systematic,
    irradiance-dependent error of exactly the kind this project exists to remove.
    """

    def _plant_with_cap(self, plant: PlantConfig, cap: float) -> PlantConfig:
        import dataclasses

        strings = tuple(
            dataclasses.replace(s, max_power_w=cap) if s.string_id == "s3" else s
            for s in plant.strings
        )
        return dataclasses.replace(plant, strings=strings)

    def test_clipping_is_detected_without_any_limit_entity(
        self, seeded_store: Store, plant: PlantConfig
    ):
        capped = self._plant_with_cap(plant, 200.0)
        engine = ForecastEngine(capped, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        # s3 has no curtailment group at all -- the ceiling is its only limit.
        write_measurements(seeded_store, "s3", NOON, power_w=200.0, limit_w=None)
        engine.evaluate_curtailment(NOON, NOON + HOUR)

        rows = seeded_store.fivemin_range("s3", NOON, NOON + HOUR)
        assert all(row["limit_binding"] == 1 for row in rows)
        assert all(row["value_kind"] == "lower_bound" for row in rows)

    def test_output_below_the_ceiling_stays_a_measurement(
        self, seeded_store: Store, plant: PlantConfig
    ):
        capped = self._plant_with_cap(plant, 200.0)
        engine = ForecastEngine(capped, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, NOON - HOUR, NOON, 1)
        write_measurements(seeded_store, "s3", NOON, power_w=60.0, limit_w=None)
        engine.evaluate_curtailment(NOON, NOON + HOUR)

        rows = seeded_store.fivemin_range("s3", NOON, NOON + HOUR)
        assert all(row["limit_binding"] == 0 for row in rows)
        assert all(row["value_kind"] == "measured" for row in rows)

    def test_the_published_forecast_respects_the_ceiling(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Promising energy the channel physically cannot pass through would
        inflate every accuracy figure afterwards."""
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        free = sum(
            r.potential_kwh
            for r in engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
            if r.string_id == "s3"
        )

        capped_engine = ForecastEngine(self._plant_with_cap(plant, 200.0), seeded_store)
        capped_engine.load_models()
        capped = sum(
            r.potential_kwh
            for r in capped_engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
            if r.string_id == "s3"
        )
        assert capped < free

    def test_a_generous_ceiling_changes_nothing(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        free = sum(
            r.potential_kwh
            for r in engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
            if r.string_id == "s3"
        )
        high = ForecastEngine(self._plant_with_cap(plant, 5000.0), seeded_store)
        high.load_models()
        unchanged = sum(
            r.potential_kwh
            for r in high.forecast(DAY_START, hours=24, start_ts=DAY_START)
            if r.string_id == "s3"
        )
        assert unchanged == pytest.approx(free)

    def test_a_clipped_summer_does_not_teach_weakness(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """The hinge rule must hold for hardware ceilings too."""
        capped = self._plant_with_cap(plant, 200.0)
        engine = ForecastEngine(capped, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 24)
        index = engine._midpoint_index(DAY_START, DAY_START + 24 * HOUR)
        conditions = engine._actual_conditions(index, DAY_START, DAY_START + 24 * HOUR)
        power = engine._interval_power(index, conditions)

        rows = []
        for ts, watts in power["s3"].items():
            measured = min(watts, 200.0)
            rows.append(
                (ts, "s3", measured * 300 / 3600, measured, 1.0, 10, None, None,
                 "measured")
            )
        seeded_store.upsert_5min(rows)
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        hourly = seeded_store.hourly_range(DAY_START, DAY_START + 24 * HOUR, "s3")
        assert any(row.value_kind == "lower_bound" for row in hourly)
        assert engine.model.factor("s3", "clear", "midday") >= 0.99


class TestLearningCursor:
    """The cursor must advance without ever stepping over unprocessed hours."""

    def _prepare(self, engine: ForecastEngine, store: Store, hours: int) -> None:
        clear_sky_forecast(engine, store, DAY_START - HOUR, DAY_START, hours)
        index = engine._midpoint_index(DAY_START, DAY_START + hours * HOUR)
        conditions = engine._actual_conditions(
            index, DAY_START, DAY_START + hours * HOUR
        )
        power = engine._interval_power(index, conditions)
        store.upsert_5min(
            [
                (ts, sid, w * 0.85 * 300 / 3600, w * 0.85, 1.0, 10, None, None,
                 "measured")
                for sid, series in power.items()
                for ts, w in series.items()
            ]
        )

    def test_a_backlog_is_worked_off_instead_of_skipped(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """After downtime longer than the window, the old hours must still be
        learned -- previously the cursor jumped straight past them."""
        self._prepare(engine, seeded_store, hours=48)
        now = DAY_START + 48 * HOUR
        seeded_store.set_cursor("model_learned", DAY_START)

        first = engine.learn(now, max_hours=12)
        cursor_after_first = seeded_store.get_cursor("model_learned")
        assert cursor_after_first == DAY_START + 12 * HOUR, (
            "the cursor must advance by one window, not jump to now"
        )
        assert first.hours_materialised > 0

        total = first.hours_materialised
        for _ in range(6):
            total += engine.learn(now, max_hours=12).hours_materialised
        assert seeded_store.get_cursor("model_learned") >= now - HOUR
        assert total > first.hours_materialised, "later chunks were processed"

    def test_cold_start_stays_bounded(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """With no cursor at all we look back a fixed window, not for ever."""
        self._prepare(engine, seeded_store, hours=48)
        engine.learn(DAY_START + 48 * HOUR, max_hours=6)
        cursor = seeded_store.get_cursor("model_learned")
        assert cursor <= DAY_START + 48 * HOUR
        assert cursor >= DAY_START + 41 * HOUR

    def test_nothing_to_do_leaves_the_cursor_untouched(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        seeded_store.set_cursor("model_learned", DAY_START + 100 * HOUR)
        engine.learn(DAY_START + 48 * HOUR, max_hours=12)
        assert seeded_store.get_cursor("model_learned") == DAY_START + 100 * HOUR


class TestBiasIgnoresHindsight:
    def test_an_analysis_issued_after_the_hour_is_not_scored(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Open-Meteo's ``past_days`` returns rows for hours already over.

        Those are analyses, not forecasts.  They are a fine yardstick, but
        scoring them would flatter the short-horizon buckets with hindsight.
        """
        clear_sky_forecast(
            engine, seeded_store, DAY_START - 26 * HOUR, DAY_START, 24, scale=1.3
        )
        # Issued a day *after* every target hour: horizon is negative.
        clear_sky_forecast(
            engine, seeded_store, DAY_START + 30 * HOUR, DAY_START, 24, scale=1.0
        )
        engine.learn(DAY_START + 24 * HOUR, max_hours=24)

        noon_local = 14
        assert engine.ghi_bias.factor(noon_local, 1.0) == pytest.approx(1.0), (
            "the 0-6h bucket must not be trained on hindsight"
        )


class TestUncoveredHours:
    """A source that stops short must produce a gap, not a confident zero.

    Home Assistant weather entities commonly publish 24 or 48 hours. With a
    72-hour window the missing hours used to become 0 W/m2 and the day-after
    sensor read 0.00 kWh for ever, next to a correct today.
    """

    def test_hours_the_source_never_delivered_are_omitted(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=72, start_ts=DAY_START)
        hours = {row.ts_utc for row in rows}
        assert hours, "the covered day must still be forecast"
        assert max(hours) < DAY_START + 24 * HOUR, (
            "hours beyond the source's horizon must not be emitted"
        )

    def test_a_full_window_is_unaffected(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 72)
        rows = engine.forecast(DAY_START, hours=72, start_ts=DAY_START)
        hours = {row.ts_utc for row in rows}
        assert len(hours) == 72

    def test_night_hours_inside_the_horizon_are_kept_as_zero(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Dark is a real answer; absent is not."""
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        rows = engine.forecast(DAY_START, hours=24, start_ts=DAY_START)
        midnight = [r for r in rows if r.ts_utc == DAY_START]
        assert midnight
        assert all(r.potential_kwh == pytest.approx(0.0) for r in midnight)


class TestSkipReasonsAreRecorded:
    """A bare "skipped" count is not an observation, it is a shrug.

    A plant sat at zero learned observations for two days with nothing in the
    diagnostics to distinguish "it was night" from "the physics came back
    empty in broad daylight" -- four quite different problems behind one
    number.
    """

    def test_a_night_hour_says_night(self, engine: ForecastEngine, store: Store):
        stats = engine.learn(DAY_START + 3 * HOUR)
        assert "zero_physics_in_daylight" not in stats.skipped

    def test_the_reasons_add_up_to_the_total(self, engine: ForecastEngine):
        stats = engine.learn(DAY_START + 5 * HOUR)
        assert sum(stats.skipped.values()) == stats.observations_skipped

    def test_the_breakdown_reaches_the_diagnostics(self, engine: ForecastEngine):
        stats = engine.learn(DAY_START + 5 * HOUR)
        assert "skipped_because" in stats.as_dict()


class TestUnshadedIsCarriedAlongside:
    """So a chart can show what the sky map is actually contributing.

    Without it the only visible gap is forecast-versus-reality, which says
    nothing about whether the map has learned anything useful yet.
    """

    def _sky(self, engine: ForecastEngine, factor: float):
        import math

        from core.shading import Cell, ShadingMap, ShadingModel

        engine.shading = ShadingModel(
            maps={
                "s1": ShadingMap(
                    cells={
                        (azimuth, elevation): Cell(value=math.log(factor), n=10_000.0)
                        for azimuth in range(36)
                        for elevation in range(19)
                    },
                    reference=0.0,
                )
            }
        )

    def _rows(self, engine: ForecastEngine, store: Store):
        start = DAY_START + 11 * HOUR
        clear_sky_forecast(engine, store, start - HOUR, start, hours=2)
        return [
            row
            for row in engine.forecast(start, hours=1, start_ts=start)
            if row.string_id == "s1"
        ]

    def test_without_a_map_the_two_agree(self, engine: ForecastEngine, seeded_store):
        for row in self._rows(engine, seeded_store):
            assert row.unshaded_kwh == pytest.approx(row.potential_kwh)

    def test_the_gap_is_the_shadow(self, engine: ForecastEngine, seeded_store):
        self._sky(engine, 0.5)
        rows = self._rows(engine, seeded_store)
        assert rows
        for row in rows:
            assert row.potential_kwh == pytest.approx(row.unshaded_kwh * 0.5, rel=0.02)

    def test_unshaded_is_never_below_the_forecast(
        self, engine: ForecastEngine, seeded_store
    ):
        """Shading only ever subtracts, so the bare curve is the upper one."""
        self._sky(engine, 0.3)
        for row in self._rows(engine, seeded_store):
            assert row.unshaded_kwh >= row.potential_kwh - 1e-9

    def test_an_unmapped_string_is_unaffected(
        self, engine: ForecastEngine, seeded_store
    ):
        self._sky(engine, 0.5)
        start = DAY_START + 11 * HOUR
        clear_sky_forecast(engine, seeded_store, start - HOUR, start, hours=2)
        others = [
            row
            for row in engine.forecast(start, hours=1, start_ts=start)
            if row.string_id == "s2"
        ]
        assert others
        for row in others:
            assert row.unshaded_kwh == pytest.approx(row.potential_kwh)

    def test_a_capped_tracker_does_not_overstate_the_shadow(
        self, seeded_store, plant: PlantConfig
    ):
        """The ceiling binds with or without the shadow.

        Dividing an already capped value by the shade factor would report a
        430 W channel under half shade as though it could have made 860 W --
        when without the shadow it would simply have sat on its ceiling.
        """
        from dataclasses import replace

        capped = replace(
            plant,
            strings=tuple(
                replace(s, max_power_w=200.0) if s.string_id == "s1" else s
                for s in plant.strings
            ),
        )
        engine = ForecastEngine(capped, seeded_store)
        engine.load_models()
        self._sky(engine, 0.5)
        rows = self._rows(engine, seeded_store)
        assert rows
        ceiling_kwh = 200.0 / 1000.0
        for row in rows:
            assert row.unshaded_kwh <= ceiling_kwh + 1e-6
            assert row.potential_kwh <= ceiling_kwh + 1e-6

    def test_the_bare_curve_never_exceeds_nameplate(self, engine: ForecastEngine, seeded_store):
        """The physics clips at nameplate, and that clip is where linearity ends.

        On a bright, cold interval the shaded value is already sitting on the
        ceiling, so dividing the shade back out would invent power the module
        could never make.
        """
        self._sky(engine, 0.2)
        rows = self._rows(engine, seeded_store)
        assert rows
        nameplate_kwh = 1.80  # s1, one hour at full output
        for row in rows:
            assert row.unshaded_kwh <= nameplate_kwh + 1e-6
