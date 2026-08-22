"""The nowcast inside the engine: what it must move, and what it must not.

The guarantees here are the load-bearing ones.  The nowcast writes into the
same conditions frame the whole 48-hour horizon is evaluated from, and two
plausible implementations of it would silently rewrite hours that have already
been graded -- once through the decay formula's sign, once through the
transposition model flag.  Both are pinned below.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.config import GeometrySegment, PlantConfig, WeatherSources
from core.forecast import HOUR, ForecastEngine
from core.persistence import (
    REASON_FROZEN,
    REASON_LEARNING_OFF,
    REASON_NO_SOURCE,
    REASON_STALE,
    WINDOW_SECONDS,
)
from core.physics import to_index
from core.store import Store

from test_forecast_engine import DAY_START, clear_sky_forecast

NOON = DAY_START + 12 * HOUR


def with_sensor(plant: PlantConfig, **kwargs) -> PlantConfig:
    return dataclasses.replace(
        plant,
        weather_sources=WeatherSources(ghi_entity="sensor.ghi"),
        **kwargs,
    )


def write_measured_ghi(
    engine: ForecastEngine,
    store: Store,
    end_ts: int,
    factor: float,
    span_s: int = WINDOW_SECONDS,
) -> None:
    """Fill the trailing window with a fixed fraction of clear sky."""
    rows = []
    end_ts = (end_ts // 300) * 300
    for ts in range(end_ts - span_s, end_ts, 300):
        idx = to_index([ts + 150])
        cs = float(engine.physics.clearsky(idx)["ghi"].iloc[0])
        rows.append((ts, 20.0, 60.0, 2.0, 0.0, 1013.0, cs * factor, None))
    store.upsert_weather_actual(rows)


def seed_bias(engine: ForecastEngine, now_ts: int, n: int = 200) -> None:
    """Give the bias model enough evidence that the nowcast is trusted."""
    from datetime import datetime

    hour_local = datetime.fromtimestamp(now_ts, tz=engine._tz).hour
    for _ in range(n):
        engine.ghi_bias.observe(hour_local, 1.0, 500.0, 500.0)


@pytest.fixture
def sensor_engine(seeded_store: Store, plant: PlantConfig) -> ForecastEngine:
    engine = ForecastEngine(with_sensor(plant), seeded_store)
    engine.load_models()
    clear_sky_forecast(
        engine, seeded_store, DAY_START, DAY_START, 48, scale=0.5
    )
    return engine


def totals(rows) -> dict[int, float]:
    out: dict[int, float] = {}
    for row in rows:
        out[row.ts_utc] = out.get(row.ts_utc, 0.0) + row.potential_kwh
    return out


class TestTheNowcastMoves:
    def test_a_brighter_sky_lifts_the_coming_hours(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        before = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START,
                                   apply_learning=False)
        )
        after = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)
        )

        assert sensor_engine.last_nowcast is not None
        assert after[NOON + HOUR] > before[NOON + HOUR]

    def test_a_darker_sky_lowers_them(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.15)

        before = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START,
                                   apply_learning=False)
        )
        after = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)
        )

        assert after[NOON + HOUR] < before[NOON + HOUR]

    def test_the_effect_fades_with_distance(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        before = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START,
                                   apply_learning=False)
        )
        after = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)
        )

        near = after[NOON + HOUR] - before[NOON + HOUR]
        far = after[NOON + 4 * HOUR] - before[NOON + 4 * HOUR]
        assert near > 0.0
        assert far == pytest.approx(0.0, abs=1e-9)


class TestTheNowcastLeavesAlone:
    def test_the_elapsed_day_is_bit_identical(self, sensor_engine, seeded_store):
        """The guarantee the accuracy scoring rests on.

        Hours before ``now`` have already been logged and graded.  If the
        nowcast reached them, every published accuracy number would be a
        hindcast.
        """
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        before = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START,
                                   apply_learning=False)
        )
        after = totals(
            sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)
        )

        for hour in range(0, 12):
            ts = DAY_START + hour * HOUR
            if ts in before:
                assert after[ts] == before[ts], f"hour {hour} moved"

    def test_tomorrow_is_bit_identical(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        before = totals(
            sensor_engine.forecast(NOON, hours=36, start_ts=DAY_START,
                                   apply_learning=False)
        )
        after = totals(
            sensor_engine.forecast(NOON, hours=36, start_ts=DAY_START)
        )

        for hour in range(24, 36):
            ts = DAY_START + hour * HOUR
            if ts in before:
                assert after[ts] == before[ts]

    def test_the_transposition_model_does_not_switch(
        self, sensor_engine, seeded_store, monkeypatch
    ):
        """Codex' heaviest objection, pinned.

        ``physics.run`` collapses per-interval component plausibility into one
        flag and picks Perez-Driesse or Hay-Davies for the *entire* run.  A
        nowcast that blanked DNI/DHI instead of re-deriving them would flip
        that flag and quietly rewrite the whole horizon -- and every other test
        here would stay green, because they all compare sums.
        """
        seen: list[bool] = []
        original = sensor_engine.physics._transposition_model

        def spy(components_ok: bool) -> str:
            seen.append(components_ok)
            return original(components_ok)

        monkeypatch.setattr(sensor_engine.physics, "_transposition_model", spy)
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)

        assert seen, "transposition model was never chosen"
        assert all(seen), "a nowcast interval made the components implausible"


class TestTheNowcastStaysSilent:
    def test_without_a_sensor(self, seeded_store, plant):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24, scale=0.5)
        write_measured_ghi(engine, seeded_store, NOON, factor=0.95)

        engine.forecast(NOON, hours=24, start_ts=DAY_START)

        assert engine.last_nowcast is None
        assert engine.last_nowcast_reason == REASON_NO_SOURCE

    def test_when_learning_is_off(self, seeded_store, plant):
        engine = ForecastEngine(
            with_sensor(plant, learning_enabled=False), seeded_store
        )
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START, DAY_START, 24, scale=0.5)
        seed_bias(engine, NOON)
        write_measured_ghi(engine, seeded_store, NOON, factor=0.95)

        engine.forecast(NOON, hours=24, start_ts=DAY_START)

        assert engine.last_nowcast is None
        assert engine.last_nowcast_reason == REASON_LEARNING_OFF

    def test_when_the_sensor_went_quiet(self, sensor_engine, seeded_store):
        """A dead sensor must not leave its last reading standing."""
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(
            sensor_engine, seeded_store, NOON - 4 * HOUR, factor=0.95
        )

        sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)

        assert sensor_engine.last_nowcast is None
        assert sensor_engine.last_nowcast_reason in {REASON_STALE, "no_measurement"}

    def test_when_the_sensor_froze_but_kept_reporting(
        self, sensor_engine, seeded_store
    ):
        """Fresh rows, dead value -- the case the staleness check cannot see."""
        seed_bias(sensor_engine, NOON)
        end = (NOON // 300) * 300
        seeded_store.upsert_weather_actual([
            (ts, 20.0, 60.0, 2.0, 0.0, 1013.0, 431.7, None)
            for ts in range(end - WINDOW_SECONDS, end, 300)
        ])

        sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)

        assert sensor_engine.last_nowcast is None
        assert sensor_engine.last_nowcast_reason == REASON_FROZEN

    def test_a_run_without_weather_clears_the_previous_state(
        self, sensor_engine, seeded_store
    ):
        """Diagnostics must not keep showing a nowcast that no longer ran."""
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)
        sensor_engine.forecast(NOON, hours=24, start_ts=DAY_START)
        assert sensor_engine.last_nowcast is not None

        far = DAY_START + 400 * HOUR
        assert sensor_engine.forecast(far, hours=24, start_ts=far) == []

        assert sensor_engine.last_nowcast is None

    def test_bare_physics_stays_bare(self, sensor_engine, seeded_store):
        seed_bias(sensor_engine, NOON)
        write_measured_ghi(sensor_engine, seeded_store, NOON, factor=0.95)

        rows = sensor_engine.forecast(
            NOON, hours=24, start_ts=DAY_START, apply_learning=False
        )

        assert rows
        assert sensor_engine.last_nowcast is None


class TestWindowAlignment:
    def test_a_window_inside_an_hour_still_finds_its_forecast(
        self, sensor_engine, seeded_store
    ):
        """Hourly weather rows are keyed on the hour.

        Asking ``latest_forecast`` from 16:01 would miss the 16:00 row, the
        frame would come back empty and the nowcast would be inert -- a fault
        that breaks nothing and would therefore go unnoticed for months.
        """
        seed_bias(sensor_engine, NOON)
        odd_now = NOON + 31 * 60
        write_measured_ghi(sensor_engine, seeded_store, odd_now, factor=0.95)

        sensor_engine.forecast(odd_now, hours=24, start_ts=DAY_START)

        state = sensor_engine.last_nowcast
        assert state is not None
        # ``kt`` needs no forecast at all, so its presence proves nothing --
        # the tell is the spread.  Measured and forecast are both fixed
        # multiples of clear sky here, so their ratio is constant and the
        # spread must come out near zero.  Query the window without flooring
        # to the hour and the rows go missing, the spread silently falls back
        # to its default, and only this assertion notices.
        assert state.spread < 0.05
        assert state.spread != pytest.approx(0.143)
