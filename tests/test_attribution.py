"""Splitting the forecast error into the weather source and our own chain.

The published score answers "how wrong were we" and cannot say which of two
quite different culprits it means: the irradiance the forecast was handed, or
what the chain made of it.  These tests pin the split, and -- just as
importantly -- what a plant without an irradiance sensor sees instead.
"""

from __future__ import annotations

import pytest

from core.config import PlantConfig
from core.forecast import (
    ATTRIBUTION_MIN_HOURS,
    HOUR,
    REASON_COLLECTING,
    REASON_NO_IRRADIANCE_SENSOR,
    ForecastEngine,
)
from core.store import Store

from test_forecast_engine import DAY_START, clear_sky_forecast
from test_nowcast_engine import with_sensor, write_measured_ghi

DAY = 24 * HOUR
DAYLIGHT = list(range(6, 18))


@pytest.fixture
def engine(seeded_store: Store, plant: PlantConfig) -> ForecastEngine:
    """A plant with no irradiance sensor -- the default, and the case this
    feature must stay invisible for."""
    engine = ForecastEngine(plant, seeded_store)
    engine.load_models()
    return engine


def seed_day(
    engine: ForecastEngine,
    store: Store,
    day_start: int,
    actual_kwh: float,
    predicted_kwh: float,
    chain_kwh: float | None = None,
    string_id: str = "s1",
) -> None:
    """One complete day: what came, what was announced, what the chain says."""
    cutoff = engine.day_ahead_cutoff(day_start)
    n = len(DAYLIGHT)
    store.upsert_hourly(
        [
            (day_start + h * HOUR, string_id, actual_kwh / n, 1.0, 0.0,
             None, None, None, "measured", "exact")
            for h in DAYLIGHT
        ]
    )
    store.log_forecast(
        [
            (cutoff, day_start + h * HOUR, string_id, predicted_kwh / n, "physics")
            for h in DAYLIGHT
        ]
    )
    if chain_kwh is not None:
        store.update_chain_potential(
            [(chain_kwh / n, day_start + h * HOUR, string_id) for h in DAYLIGHT]
        )


def three_days(engine, store, **kwargs) -> int:
    for index in range(3):
        seed_day(engine, store, DAY_START + index * DAY, **kwargs)
    return DAY_START + 3 * DAY + 12 * HOUR


class TestTheSplit:
    def test_a_right_chain_blames_the_source(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Announced 15, the chain would have said 10, and 10 came: every bit
        of that miss arrived with the irradiance."""
        now_ts = three_days(
            engine, seeded_store, actual_kwh=10.0, predicted_kwh=15.0, chain_kwh=10.0
        )
        split = engine.score_day_ahead(3, now_ts)["attribution"]

        assert split["wmape_end_to_end"] == pytest.approx(0.5)
        assert split["wmape_source"] == pytest.approx(0.5)
        assert split["wmape_chain"] == pytest.approx(0.0)
        assert split["reason"] is None

    def test_a_right_source_blames_the_chain(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The same total miss, the opposite culprit: with the real irradiance
        the chain still says 15."""
        now_ts = three_days(
            engine, seeded_store, actual_kwh=10.0, predicted_kwh=15.0, chain_kwh=15.0
        )
        split = engine.score_day_ahead(3, now_ts)["attribution"]

        assert split["wmape_end_to_end"] == pytest.approx(0.5)
        assert split["wmape_source"] == pytest.approx(0.0)
        assert split["wmape_chain"] == pytest.approx(0.5)

    def test_the_parts_are_absolute_and_need_not_add_up(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Source and chain err in opposite directions: they cancel in the
        total, and must not cancel in the split."""
        now_ts = three_days(
            engine, seeded_store, actual_kwh=10.0, predicted_kwh=10.0, chain_kwh=12.0
        )
        split = engine.score_day_ahead(3, now_ts)["attribution"]

        assert split["wmape_end_to_end"] == pytest.approx(0.0)
        assert split["wmape_chain"] == pytest.approx(0.2)
        assert split["wmape_source"] == pytest.approx(0.2)

    def test_it_counts_only_the_hours_it_can_split(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Two days with a counterfactual, one without."""
        seed_day(engine, seeded_store, DAY_START, 10.0, 15.0, chain_kwh=10.0)
        seed_day(engine, seeded_store, DAY_START + DAY, 10.0, 15.0, chain_kwh=10.0)
        seed_day(engine, seeded_store, DAY_START + 2 * DAY, 10.0, 15.0)
        now_ts = DAY_START + 3 * DAY + 12 * HOUR

        result = engine.score_day_ahead(3, now_ts)
        assert result["days_scored"] == 3          # the score still sees all three
        assert result["attribution"]["hours"] == 2 * len(DAYLIGHT)
        assert result["attribution"]["hours_scored"] == 3 * len(DAYLIGHT)


class TestWhatAPlantWithoutASensorSees:
    def test_the_reason_is_named_not_left_blank(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The house rule: say why there is nothing, never show an empty tile."""
        now_ts = three_days(engine, seeded_store, actual_kwh=10.0, predicted_kwh=15.0)
        split = engine.score_day_ahead(3, now_ts)["attribution"]

        assert split["wmape_chain"] is None
        assert split["wmape_source"] is None
        assert split["hours"] == 0
        assert split["reason"] == REASON_NO_IRRADIANCE_SENSOR

    def test_the_ordinary_score_is_untouched(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Everything a plant had before this existed keeps working."""
        now_ts = three_days(engine, seeded_store, actual_kwh=10.0, predicted_kwh=15.0)
        result = engine.score_day_ahead(3, now_ts)

        assert result["days_scored"] == 3
        assert result["uncensored"]["wmape"] == pytest.approx(0.5)
        assert len(result["history"]["plant"]) == 3

    def test_writing_the_counterfactual_is_a_no_op(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        assert engine.store_chain_potential(DAY_START, DAY_START + DAY) == 0


class TestWithASensorButTooLittle:
    def test_a_configured_sensor_says_collecting(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = ForecastEngine(with_sensor(plant), seeded_store)
        engine.load_models()
        now_ts = three_days(engine, seeded_store, actual_kwh=10.0, predicted_kwh=15.0)

        split = engine.score_day_ahead(3, now_ts)["attribution"]
        assert split["reason"] == REASON_COLLECTING

    def test_a_handful_of_hours_is_not_published(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = ForecastEngine(with_sensor(plant), seeded_store)
        engine.load_models()
        seed_day(engine, seeded_store, DAY_START, 10.0, 15.0, chain_kwh=10.0)
        now_ts = DAY_START + DAY + 12 * HOUR

        split = engine.score_day_ahead(1, now_ts)["attribution"]
        assert len(DAYLIGHT) < ATTRIBUTION_MIN_HOURS
        assert split["hours"] == len(DAYLIGHT)
        assert split["wmape_chain"] is None
        assert split["reason"] == REASON_COLLECTING


class TestWritingTheCounterfactual:
    def test_measured_hours_are_written(self, seeded_store: Store, plant: PlantConfig):
        engine = ForecastEngine(with_sensor(plant), seeded_store)
        engine.load_models()
        noon = DAY_START + 12 * HOUR
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        write_measured_ghi(engine, seeded_store, noon + HOUR, factor=0.5, span_s=HOUR)
        seeded_store.upsert_hourly(
            [
                (noon, string, 0.5, 1.0, 0.0, None, None, None, "measured", "exact")
                for string in ("s1", "s2", "s3")
            ]
        )

        written = engine.store_chain_potential(noon, noon + HOUR)
        assert written == 3
        rows = seeded_store.hourly_range(noon, noon + HOUR)
        assert all(row.quality == "exact" for row in rows)
        chain = {
            r["string_id"]: r["chain_kwh"]
            for r in seeded_store.forecast_vs_actual(noon, noon + HOUR)
        }
        assert set(chain) == {"s1", "s2", "s3"}
        assert all(value and value > 0 for value in chain.values())

    def test_an_hour_the_sensor_barely_saw_is_skipped(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Ten minutes of measurement do not make an hour known."""
        engine = ForecastEngine(with_sensor(plant), seeded_store)
        engine.load_models()
        noon = DAY_START + 12 * HOUR
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24)
        write_measured_ghi(engine, seeded_store, noon + 600, factor=0.5, span_s=600)

        assert engine.store_chain_potential(noon, noon + HOUR) == 0
