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
        hourly = coarse.with_hour_slots()
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

    def test_the_daypart_rows_stay_exactly_as_they_were(self):
        """What a daypart-only version reads after a downgrade."""
        coarse = coarse_model()
        hourly = coarse.with_hour_slots()
        for sid in STRINGS:
            for weather in WEATHER_CLASSES:
                for part in DAYPARTS:
                    assert hourly.log_correction(sid, weather, part) == coarse.log_correction(
                        sid, weather, part
                    )

    def test_the_observation_count_does_not_jump(self):
        coarse = coarse_model()
        assert coarse.with_hour_slots().observations_seen == coarse.observations_seen

    def test_frozen_dayparts_do_not_count_once_slots_learn(self):
        hourly = coarse_model().with_hour_slots()
        for key in list(hourly.plant):
            if key.startswith("clear|h"):
                hourly.plant[key] = Effect(hourly.plant[key].value, 20.0)
        expected = 3 * 20.0 + sum(
            e.n_eff for k, e in coarse_model().plant.items() if not k.startswith("clear|")
        )
        assert hourly.observations_seen == pytest.approx(expected)

    def test_string_offsets_are_untouched(self):
        coarse = coarse_model()
        assert coarse.with_hour_slots().to_rows("string") == coarse.to_rows("string")

    def test_seeding_twice_changes_nothing(self):
        once = coarse_model().with_hour_slots()
        twice = once.with_hour_slots()
        for scope in ("plant", "string", "string_daypart"):
            assert twice.to_rows(scope) == once.to_rows(scope)

    def test_existing_slots_are_never_overwritten(self):
        model = coarse_model().with_hour_slots()
        model.plant["clear|h-4"] = Effect(0.33, 7.0)
        model.plant["clear|morning"] = Effect(-0.5, 20.0)  # a downgrade learned on
        again = model.with_hour_slots()
        assert again.plant["clear|h-4"].as_tuple() == (0.33, 7.0)

    def test_only_reachable_slots_are_seeded(self):
        hourly = coarse_model().with_hour_slots(("h-3", "h+0"))
        slots = {k.rpartition("|")[2] for k in hourly.plant if k.rpartition("|")[2] in HOUR_SLOTS}
        assert slots == {"h-3", "h+0"}

    def test_slots_learn_on_their_own(self):
        """After seeding, an observation moves its own hour, not its siblings."""
        from core.learning import Observation

        hourly = coarse_model().with_hour_slots()
        before = {k: e.value for k, e in hourly.plant.items()}
        hourly.observe(
            Observation(
                string_id="s1", weather="clear", part="h-4",
                measured_kwh=1.5, physics_kwh=1.0, weight=1.0,
            )
        )
        changed = {k for k, e in hourly.plant.items() if e.value != before[k]}
        assert changed == {"clear|h-4"}

    def test_the_summary_keeps_dayparts_readable(self):
        """A dashboard that only knows dayparts must not go blank."""
        coarse = coarse_model()
        hourly = coarse.with_hour_slots()
        summary = hourly.summary()
        for weather in WEATHER_CLASSES:
            for part in DAYPARTS:
                key = f"{weather}|{part}"
                assert summary["plant"][key] == coarse.summary()["plant"][key]
        assert any(k.endswith("|h+0") for k in summary["plant"])


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
        for scope in ("plant", "string", "string_daypart"):
            stored = seeded_store.load_effects(scope)
            for key, row in coarse.to_rows(scope).items():
                assert stored[key] == pytest.approx(row), (scope, key)
        assert any(k.endswith("|h+0") for k in seeded_store.load_effects("plant"))

    def test_night_slots_are_not_seeded(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        slots = {k.rpartition("|")[2] for k in seeded_store.load_effects("plant")}
        assert "h+0" in slots and "h-12" not in slots and "h+11" not in slots

    def test_a_downgrade_finds_its_own_model(self, seeded_store, plant):
        """The previous version, installed over this one, reads only dayparts."""
        coarse = coarse_model()
        _store_coarse(seeded_store, coarse)
        ForecastEngine(plant, seeded_store).load_models()
        old_view = LogRatioModel.from_rows(
            plant=seeded_store.load_effects("plant"),
            string=seeded_store.load_effects("string"),
            string_daypart=seeded_store.load_effects("string_daypart"),
        )
        for sid in STRINGS:
            for weather in WEATHER_CLASSES:
                for part in DAYPARTS:
                    assert old_view.factor(sid, weather, part) == coarse.factor(sid, weather, part)

    def test_a_round_trip_carries_on_from_the_newest_state(self, seeded_store, plant):
        """Downgrade, learn on dayparts, upgrade again: the forecast must follow
        what the old version learned, not jump back to the day of the downgrade."""
        import time

        _store_coarse(seeded_store, coarse_model())
        first = ForecastEngine(plant, seeded_store)
        first.load_models()
        first.save_models(int(time.time()))
        untouched = seeded_store.load_effects("plant")["overcast|h+0"]
        # the old version, installed over this one, learns clear|midday and
        # meets a weather class never seen before; it writes with its own clock
        # A daypart version saves every row it loaded -- hourly ones included,
        # untouched -- under one clock reading.
        later = int(time.time()) + 60
        old_rows = seeded_store.load_effects("plant")
        old_rows.update({"clear|midday": (0.4, 30.0), "snow|midday": (0.1, 3.0)})
        seeded_store.save_effects("plant", old_rows, later)
        again = ForecastEngine(plant, seeded_store)
        again.load_models()
        stored = seeded_store.load_effects("plant")
        assert stored["clear|h+0"] == pytest.approx((0.4, 30.0))
        assert stored["clear|h-1"] == pytest.approx((0.4, 30.0))
        assert stored["snow|h+0"] == pytest.approx((0.1, 3.0))
        # a daypart it did not change still counts as rewritten: its slots are
        # reseeded from it, which gives back the same values
        assert stored["overcast|h+0"][0] == pytest.approx(untouched[0])
        assert again.model.factor("s1", "clear", "h+0") == pytest.approx(
            LogRatioModel.from_rows(
                plant={"clear|midday": (0.4, 30.0)},
                string=seeded_store.load_effects("string"),
                string_daypart=seeded_store.load_effects("string_daypart"),
            ).factor("s1", "clear", "midday")
        )

    def test_the_hourly_model_never_rewrites_the_frozen_rows(self, seeded_store, plant):
        import time

        def stamps():
            return {
                r["key"]: r["updated_at"]
                for r in seeded_store._query(
                    "SELECT key, updated_at FROM model_effects WHERE scope = 'plant'", ()
                )
            }

        _store_coarse(seeded_store, coarse_model())
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        before = stamps()
        engine.save_models(int(time.time()) + 120)
        after = stamps()
        for key in before:
            if key.rpartition("|")[2] in DAYPARTS:
                assert after[key] == before[key], key
            else:
                assert after[key] > before[key], key

    def test_an_unchanged_rewrite_keeps_learned_slots(self, seeded_store, plant):
        """An older version saves what it loaded without learning: slots stay."""
        import time

        _store_coarse(seeded_store, coarse_model())
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        engine.model.plant["clear|h+0"] = Effect(0.61, 12.0)
        engine.save_models(int(time.time()))
        seeded_store.save_effects(
            "plant", seeded_store.load_effects("plant"), int(time.time()) + 3600
        )
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant")["clear|h+0"] == pytest.approx((0.61, 12.0))

    def test_learning_on_an_older_version_is_found_whatever_the_clock(
        self, seeded_store, plant
    ):
        _store_coarse(seeded_store, coarse_model())
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        engine.save_models(2_000_000_000)
        # the older version learns, with a clock that went backwards
        seeded_store.save_effects("plant", {"clear|midday": (0.4, 30.0)}, 1_000_000_000)
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant")["clear|h+0"] == pytest.approx((0.4, 30.0))

    def test_slots_without_a_record_are_kept(self, seeded_store, plant):
        """No record means nothing to compare against: learned slots stay."""
        _store_coarse(seeded_store, coarse_model())
        seeded_store.save_effects("plant", {"clear|h+0": (0.77, 15.0)}, DAY_START)
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant")["clear|h+0"] == pytest.approx((0.77, 15.0))

    def test_an_orphaned_record_is_cleared(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant~seeded_from")
        seeded_store.clear_effects("plant")
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant~seeded_from") == {}

    def test_a_restart_in_the_same_second_keeps_the_slots(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        ForecastEngine(plant, seeded_store).load_models()
        again = ForecastEngine(plant, seeded_store)
        again.load_models()
        assert "clear|h+0" in again.model.plant
        assert again.daypart_scheme == DAYPART_SCHEME_HOURLY

    def test_normal_restarts_do_not_reseed(self, seeded_store, plant):
        import time

        _store_coarse(seeded_store, coarse_model())
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        engine.model.plant["clear|h+0"] = Effect(0.55, 9.0)  # learned on hours
        engine.save_models(int(time.time()) + 180)
        ForecastEngine(plant, seeded_store).load_models()
        assert seeded_store.load_effects("plant")["clear|h+0"] == pytest.approx((0.55, 9.0))

    def test_a_second_start_changes_nothing(self, seeded_store, plant):
        _store_coarse(seeded_store, coarse_model())
        ForecastEngine(plant, seeded_store).load_models()
        snapshot = {s: seeded_store.load_effects(s) for s in ("plant", "string", "string_daypart")}
        ForecastEngine(plant, seeded_store).load_models()
        for scope, rows in snapshot.items():
            assert seeded_store.load_effects(scope) == rows

    def test_a_new_installation_only_gets_the_stamp(self, seeded_store, plant):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        assert engine.daypart_scheme == DAYPART_SCHEME_HOURLY
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME) == DAYPART_SCHEME_HOURLY
        assert seeded_store.load_effects("plant") == {}

    def test_each_branch_moves_in_its_own_namespace(self, seeded_store, plant):
        live, cal = coarse_model(), coarse_model()
        cal.plant["clear|midday"] = Effect(-0.25, 11.0)
        _store_coarse(seeded_store, live)
        _store_coarse(seeded_store, cal, ns="cal")

        ForecastEngine(plant, seeded_store).load_models()
        shade = ForecastEngine(plant, seeded_store, variant="cal", shadow=True)
        shade.load_models()

        assert seeded_store.get_cursor("daypart_scheme#cal") == DAYPART_SCHEME_HOURLY
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

    def test_a_failed_move_stays_on_dayparts(self, seeded_store, plant, monkeypatch):
        coarse = coarse_model()
        _store_coarse(seeded_store, coarse)

        def boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(seeded_store, "seed_effects", boom)
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        assert engine.daypart_scheme == DAYPART_SCHEME_COARSE
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME, default=0) == 0
        assert seeded_store.load_effects("plant") == pytest.approx(coarse.to_rows("plant"))
        assert engine.model.factor("s1", "clear", "midday") == coarse.factor("s1", "clear", "midday")

    def test_a_crash_inside_the_transaction_leaves_nothing(self, seeded_store):
        with pytest.raises(Exception):
            seeded_store.seed_effects(
                {"plant": {"clear|h+0": (0.1, 2.0)}, "string_daypart": {"s1|h+0": (None, 1.0)}},
                cursor=CURSOR_DAYPART_SCHEME, scheme=DAYPART_SCHEME_HOURLY, now_ts=DAY_START,
            )
        assert seeded_store.load_effects("plant") == {}
        assert seeded_store.get_cursor(CURSOR_DAYPART_SCHEME, default=0) == 0
