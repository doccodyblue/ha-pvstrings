"""Hourly buckets, and the move onto them that must not cost anybody anything.

Every installation already out there holds factors learned per daypart.  The
move to one-hour slots seeds each slot from its daypart; these tests pin the
promise that makes that safe: on the day of the upgrade no forecast changes,
nothing learned is dropped, and nothing is counted twice.
"""

from __future__ import annotations

import math

import pytest

from core.forecast import ForecastEngine
from core.learning import (
    COARSE_BACKUP_SUFFIX,
    CURSOR_DAYPART_SCHEME,
    DAYPART_SCHEME_COARSE,
    DAYPART_SCHEME_HOURLY,
    DAYPARTS,
    HOUR_SLOTS,
    WEATHER_CLASSES,
    Effect,
    LogRatioModel,
    coarse_of,
    daypart,
    hour_slot,
    part_for,
)
from core.store import Store

from test_forecast_engine import DAY_START, HOUR, clear_sky_forecast

STRINGS = ("s1", "s2", "s3")


def coarse_model() -> LogRatioModel:
    """A daypart model with a different value and evidence in every bucket."""
    model = LogRatioModel()
    for i, weather in enumerate(WEATHER_CLASSES):
        for j, part in enumerate(DAYPARTS):
            model.plant[f"{weather}|{part}"] = Effect(0.03 * (i - j) + 0.01, 4.0 + i + 3 * j)
    for i, sid in enumerate(STRINGS):
        model.string[sid] = Effect(-0.02 * i + 0.05, 9.0 + i)
        for j, part in enumerate(DAYPARTS):
            model.string_daypart[f"{sid}|{part}"] = Effect(0.04 * (j - i), 2.0 + 5 * j)
    return model


# offsets on both sides of every daypart edge, plus a dense sweep
EDGE_OFFSETS = [-12.0, -2.0 - 1e-9, -2.0, -2.0 + 1e-9, -1e-9, 0.0, 1e-9,
                2.0 - 1e-9, 2.0, 2.0 + 1e-9, 11.999999]
SWEEP = [-12.0 + i * 0.01 for i in range(2400)] + EDGE_OFFSETS


class TestSlots:
    def test_every_slot_lies_inside_one_daypart(self):
        wrong = [o for o in SWEEP if coarse_of(hour_slot(o)) != daypart(o)]
        assert not wrong, wrong[:5]

    def test_the_daypart_edges_fall_on_slot_edges(self):
        # -2 h belongs to midday, +2 h too; just beyond each is the next part.
        assert hour_slot(-2.0) == "h-2" and daypart(-2.0) == "midday"
        assert hour_slot(-2.0 - 1e-9) == "h-3" and daypart(-2.0 - 1e-9) == "morning"
        assert hour_slot(2.0) == "h+1" and daypart(2.0) == "midday"
        assert hour_slot(2.0 + 1e-9) == "h+2" and daypart(2.0 + 1e-9) == "afternoon"

    def test_slots_are_one_hour_wide(self):
        assert hour_slot(-4.5) == "h-5"
        assert hour_slot(-4.0) == "h-4"
        assert hour_slot(4.0) == "h+3"
        assert hour_slot(4.5) == "h+4"
        assert len({hour_slot(o) for o in SWEEP}) == 24

    def test_every_offset_lands_on_a_known_slot(self):
        assert {hour_slot(o) for o in SWEEP} <= set(HOUR_SLOTS)

    def test_the_scheme_picks_the_bucket(self):
        assert part_for(-3.3, DAYPART_SCHEME_COARSE) == "morning"
        assert part_for(-3.3, DAYPART_SCHEME_HOURLY) == "h-4"


class TestSeeding:
    def test_no_correction_changes(self):
        coarse = coarse_model()
        hourly = coarse.seeded_hourly()
        for sid in (*STRINGS, "unknown"):
            for weather in (*WEATHER_CLASSES, "never_seen"):
                for offset in SWEEP:
                    before = coarse.log_correction(
                        sid, weather, part_for(offset, DAYPART_SCHEME_COARSE)
                    )
                    after = hourly.log_correction(
                        sid, weather, part_for(offset, DAYPART_SCHEME_HOURLY)
                    )
                    assert after == before, (sid, weather, offset)

    def test_the_observation_count_does_not_jump(self):
        coarse = coarse_model()
        assert coarse.seeded_hourly().observations_seen == coarse.observations_seen

    def test_string_offsets_are_untouched(self):
        coarse = coarse_model()
        hourly = coarse.seeded_hourly()
        assert hourly.to_rows("string") == coarse.to_rows("string")

    def test_seeding_twice_changes_nothing(self):
        once = coarse_model().seeded_hourly()
        twice = once.seeded_hourly()
        for scope in ("plant", "string", "string_daypart"):
            assert twice.to_rows(scope) == once.to_rows(scope)

    def test_no_daypart_keys_survive(self):
        hourly = coarse_model().seeded_hourly()
        assert not hourly.has_coarse_buckets
        assert all(k.rpartition("|")[2] in HOUR_SLOTS for k in hourly.plant)

    def test_slots_learn_on_their_own(self):
        """After seeding, an observation moves its own hour, not its siblings."""
        from core.learning import Observation

        hourly = coarse_model().seeded_hourly()
        before = {k: e.value for k, e in hourly.plant.items()}
        hourly.observe(
            Observation(
                string_id="s1", weather="clear", part="h-4",
                measured_kwh=1.5, physics_kwh=1.0, weight=1.0,
            )
        )
        changed = {k for k, e in hourly.plant.items() if e.value != before[k]}
        assert changed == {"clear|h-4"}


def _store_coarse(store: Store, model: LogRatioModel, ns: str = "") -> None:
    suffix = f"#{ns}" if ns else ""
    for scope in ("plant", "string", "string_daypart"):
        store.save_effects(scope + suffix, model.to_rows(scope), DAY_START)


class TestMigration:
    def test_an_installation_moves_without_losing_anything(self, seeded_store, plant):
        coarse = coarse_model()
        _store_coarse(seeded_store, coarse)

        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()

        assert engine.daypart_scheme == DAYPART_SCHEME_HOURLY
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME) == DAYPART_SCHEME_HOURLY
        # the daypart state is kept, exactly, for a rollback
        for scope in ("plant", "string_daypart"):
            assert seeded_store.load_effects(scope + COARSE_BACKUP_SUFFIX) == pytest.approx(
                coarse.to_rows(scope)
            )
        # the live scopes hold hours only, and exactly the seeded rows
        seeded = coarse.seeded_hourly()
        for scope in ("plant", "string", "string_daypart"):
            assert seeded_store.load_effects(scope) == pytest.approx(seeded.to_rows(scope))

    def test_a_second_start_changes_nothing(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        ForecastEngine(plant, seeded_store).load_models()
        snapshot = {
            s: seeded_store.load_effects(s)
            for s in ("plant", "string", "string_daypart", "plant@coarse", "string_daypart@coarse")
        }
        again = ForecastEngine(plant, seeded_store)
        again.load_models()
        for scope, rows in snapshot.items():
            assert seeded_store.load_effects(scope) == rows

    def test_a_new_installation_only_gets_the_stamp(self, seeded_store, plant):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        assert engine.daypart_scheme == DAYPART_SCHEME_HOURLY
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME) == DAYPART_SCHEME_HOURLY
        assert seeded_store.load_effects("plant@coarse") == {}

    def test_each_branch_moves_in_its_own_namespace(self, seeded_store, plant):
        live, cal = coarse_model(), coarse_model()
        cal.plant["clear|midday"] = Effect(-0.25, 11.0)
        _store_coarse(seeded_store, live)
        _store_coarse(seeded_store, cal, ns="cal")

        ForecastEngine(plant, seeded_store).load_models()
        shade = ForecastEngine(plant, seeded_store, variant="cal", shadow=True)
        shade.load_models()

        assert seeded_store.get_cursor("daypart_scheme#cal") == DAYPART_SCHEME_HOURLY
        assert seeded_store.load_effects("plant#cal@coarse")["clear|midday"] == pytest.approx((-0.25, 11.0))
        assert shade.model.plant["clear|h+0"].value == pytest.approx(-0.25)
        assert seeded_store.load_effects("plant")["clear|h+0"][0] != pytest.approx(-0.25)

    def test_the_forecast_is_bit_identical_across_the_move(self, seeded_store, plant):
        """The whole point: an owner upgrading sees the same numbers."""
        _store_coarse(seeded_store, coarse_model())
        probe = ForecastEngine(plant, seeded_store)
        clear_sky_forecast(probe, seeded_store, DAY_START - 6 * HOUR, DAY_START, 48, scale=0.8, clouds=30.0)

        before_engine = ForecastEngine(plant, seeded_store)
        before_engine._move_to_hours = lambda: setattr(
            before_engine, "daypart_scheme", DAYPART_SCHEME_COARSE
        )
        before_engine.load_models()
        before = before_engine.forecast(DAY_START, hours=48)

        after_engine = ForecastEngine(plant, seeded_store)
        after_engine.load_models()
        after = after_engine.forecast(DAY_START, hours=48)

        assert after_engine.daypart_scheme == DAYPART_SCHEME_HOURLY
        assert len(before) == len(after) > 0
        assert any(h.potential_kwh > 0 for h in after)
        for b, a in zip(before, after):
            assert (a.ts_utc, a.string_id) == (b.ts_utc, b.string_id)
            assert a.potential_kwh == b.potential_kwh
            assert a.correction == b.correction

    def test_a_failed_move_leaves_the_old_state(self, seeded_store):
        rows = coarse_model().to_rows("plant")
        seeded_store.save_effects("plant", rows, DAY_START)
        bad = {"string_daypart": ({}, {"s1|h+0": (None, 1.0)})}  # NOT NULL violation
        good = {"plant": (rows, coarse_model().seeded_hourly().to_rows("plant"))}
        with pytest.raises(Exception):
            seeded_store.move_effects_to_hours(
                {**good, **bad}, backup_suffix=COARSE_BACKUP_SUFFIX,
                cursor=CURSOR_DAYPART_SCHEME, scheme=DAYPART_SCHEME_HOURLY, now_ts=DAY_START,
            )
        assert seeded_store.load_effects("plant") == pytest.approx(rows)
        assert seeded_store.load_effects("plant@coarse") == {}
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME, default=0) == 0

    def test_a_string_reset_also_clears_its_backup(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        ForecastEngine(plant, seeded_store).load_models()
        assert any(k.startswith("s1|") for k in seeded_store.load_effects("string_daypart@coarse"))
        seeded_store.clear_effects_for_string("s1")
        for scope in ("string_daypart", "string_daypart@coarse"):
            assert not any(k.startswith("s1|") for k in seeded_store.load_effects(scope))
            assert any(k.startswith("s2|") for k in seeded_store.load_effects(scope))
