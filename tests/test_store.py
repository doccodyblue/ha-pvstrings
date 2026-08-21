"""Persistence, and above all the geometry validity history."""

from __future__ import annotations

import pytest

from core.config import ConfigError, GeometrySegment
from core.store import SCHEMA_VERSION, Store


class TestGeometryHistory:
    """A wrong tilt is not a constant error -- it travels with the sun.  Old
    data must keep being evaluated against the geometry that was installed at
    the time, or the learning layer books a mounting error as weather."""

    def test_seeded_segment_applies_from_the_beginning(self, store: Store):
        store.add_geometry("s1", GeometrySegment(0, 180, 30, 1.8))
        assert store.geometry_at("s1", 1_700_000_000).tilt_deg == 30

    def test_new_segment_does_not_rewrite_the_past(self, store: Store):
        switch = 1_700_000_000
        store.add_geometry("s1", GeometrySegment(0, 180, 60, 1.0, note="summer"))
        store.add_geometry(
            "s1", GeometrySegment(switch, 180, 70, 1.0, note="winter setting")
        )

        assert store.geometry_at("s1", switch - 1).tilt_deg == 60
        assert store.geometry_at("s1", switch).tilt_deg == 70
        assert store.geometry_at("s1", switch + 86400).tilt_deg == 70
        assert len(store.geometry_history("s1")) == 2

    def test_timestamp_before_the_first_segment_falls_back(self, store: Store):
        store.add_geometry("s1", GeometrySegment(1_700_000_000, 180, 30, 1.8))
        assert store.geometry_at("s1", 1_000_000).tilt_deg == 30

    def test_correction_replaces_the_latest_segment(self, store: Store):
        store.add_geometry("s1", GeometrySegment(0, 180, 30, 1.8))
        store.add_geometry("s1", GeometrySegment(1_700_000_000, 180, 60, 1.8))
        store.replace_latest_geometry(
            "s1", GeometrySegment(1_700_000_000, 180, 63, 1.8, note="measured")
        )
        history = store.geometry_history("s1")
        assert len(history) == 2
        assert history[-1].tilt_deg == 63

    def test_unknown_string_has_no_geometry(self, store: Store):
        assert store.geometry_at("nope", 1_700_000_000) is None

    def test_cache_is_invalidated_on_write(self, store: Store):
        store.add_geometry("s1", GeometrySegment(0, 180, 30, 1.8))
        assert store.geometry_at("s1", 100).tilt_deg == 30
        store.add_geometry("s1", GeometrySegment(200, 180, 45, 1.8))
        assert store.geometry_at("s1", 300).tilt_deg == 45

    def test_implausible_geometry_is_rejected(self):
        with pytest.raises(ConfigError):
            GeometrySegment(0, 400, 30, 1.0)
        with pytest.raises(ConfigError):
            GeometrySegment(0, 180, 120, 1.0)
        with pytest.raises(ConfigError):
            GeometrySegment(0, 180, 30, -1.0)
        with pytest.raises(ConfigError):
            GeometrySegment(0, 180, 30, 1.0, temp_coeff=0.5)


class TestFiveMinute:
    def _row(self, ts, sid="s1", wh=50.0, kind="measured", limit=None, binding=None):
        return (ts, sid, wh, wh * 12, 1.0, 10, limit, binding, kind)

    def test_upsert_and_read_back(self, store: Store):
        store.upsert_5min([self._row(300), self._row(600)])
        rows = store.fivemin_range("s1", 0, 900)
        assert [row["ts_utc"] for row in rows] == [300, 600]

    def test_upsert_is_idempotent(self, store: Store):
        store.upsert_5min([self._row(300, wh=50.0)])
        store.upsert_5min([self._row(300, wh=80.0)])
        rows = store.fivemin_range("s1", 0, 900)
        assert len(rows) == 1
        assert rows[0]["energy_wh"] == 80.0

    def test_energy_sum_is_scoped_per_string(self, store: Store):
        store.upsert_5min(
            [self._row(300, "s1", 500.0), self._row(300, "s2", 250.0)]
        )
        assert store.energy_kwh_between(0, 900, "s1") == pytest.approx(0.5)
        assert store.energy_kwh_between(0, 900) == pytest.approx(0.75)

    def test_binding_flag_flips_value_kind(self, store: Store):
        store.upsert_5min([self._row(300, limit=1500.0)])
        store.update_curtailment_flags([(1, 300, "s1")])
        row = store.fivemin_range("s1", 0, 900)[0]
        assert row["limit_binding"] == 1
        assert row["value_kind"] == "lower_bound"

    def test_non_binding_leaves_value_kind_alone(self, store: Store):
        store.upsert_5min([self._row(300, limit=1500.0)])
        store.update_curtailment_flags([(0, 300, "s1")])
        row = store.fivemin_range("s1", 0, 900)[0]
        assert row["limit_binding"] == 0
        assert row["value_kind"] == "measured"


class TestIntervalStats:
    """The daylight clamp happens in the caller; what the store must guarantee
    is that the stats describe exactly the window it was given -- including an
    empty window, which is what "before sunrise" looks like from here."""

    def _row(self, ts, coverage):
        return (ts, "s1", 50.0, 600.0, coverage, 10, None, None, "measured")

    def _night_and_day(self, store: Store):
        # A DTU-style source: unavailable all night (coverage 0), clean by day.
        store.upsert_5min([self._row(ts, 0.0) for ts in range(0, 21600, 300)])
        store.upsert_5min([self._row(ts, 1.0) for ts in range(21600, 43200, 300)])

    def test_night_rows_drag_the_unclamped_mean(self, store: Store):
        self._night_and_day(store)
        assert store.interval_stats("s1", 0, 43200)["coverage_mean"] == 0.5

    def test_daylight_window_sees_only_daytime_quality(self, store: Store):
        self._night_and_day(store)
        stats = store.interval_stats("s1", 21600, 43200)
        assert stats["coverage_mean"] == 1.0
        assert stats["intervals"] == 72

    def test_a_daytime_outage_still_lowers_the_mean(self, store: Store):
        self._night_and_day(store)
        store.upsert_5min([self._row(ts, 0.0) for ts in range(30000, 33600, 300)])
        stats = store.interval_stats("s1", 21600, 43200)
        assert stats["coverage_mean"] < 1.0

    def test_empty_window_returns_the_no_rows_shape(self, store: Store):
        self._night_and_day(store)
        stats = store.interval_stats("s1", 21600, 21600)
        assert stats["intervals"] == 0
        assert stats["coverage_mean"] is None
        assert stats["value_kinds"] == {}


class TestForecastLog:
    def test_scoring_never_uses_hindsight(self, store: Store):
        """A forecast issued during the hour it predicts is not a forecast."""
        hour = 1_700_003_600
        store.upsert_hourly(
            [(hour, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        store.log_forecast([(hour - 7200, hour, "s1", 0.8, "physics")])
        store.log_forecast([(hour + 600, hour, "s1", 1.0, "physics")])

        rows = store.forecast_vs_actual(hour, hour + 3600, lead_time_h=0.0)
        assert rows[0]["potential_kwh"] == pytest.approx(0.8)

    def test_lead_time_selects_the_older_issue(self, store: Store):
        hour = 1_700_003_600
        store.upsert_hourly(
            [(hour, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        store.log_forecast([(hour - 3600, hour, "s1", 0.9, "physics")])
        store.log_forecast([(hour - 86400, hour, "s1", 0.5, "physics")])

        day_ahead = store.forecast_vs_actual(hour, hour + 3600, lead_time_h=24)
        assert day_ahead[0]["potential_kwh"] == pytest.approx(0.5)


class TestForecastAsItStood:
    """Pairing against one fixed instant rather than a rolling lead."""

    HOUR = 1_700_003_600

    def _measured(self, store: Store) -> None:
        store.upsert_hourly(
            [(self.HOUR, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )

    def test_the_issue_before_the_cutoff_wins(self, store: Store):
        self._measured(store)
        cutoff = self.HOUR - 12 * 3600
        store.log_forecast([(cutoff - 3600, self.HOUR, "s1", 0.5, "physics")])
        store.log_forecast([(cutoff, self.HOUR, "s1", 0.6, "physics")])
        # Issued after the cutoff: knows more than the reader did.
        store.log_forecast([(cutoff + 3600, self.HOUR, "s1", 0.9, "physics")])

        rows = store.forecast_vs_actual_before(self.HOUR, self.HOUR + 3600, cutoff)
        assert rows[0]["potential_kwh"] == pytest.approx(0.6)

    def test_a_missing_run_falls_back_to_the_one_before(self, store: Store):
        """HA was down at six; the reader saw the five o'clock run."""
        self._measured(store)
        cutoff = self.HOUR - 12 * 3600
        store.log_forecast([(cutoff - 7200, self.HOUR, "s1", 0.4, "physics")])
        store.log_forecast([(cutoff + 3600, self.HOUR, "s1", 0.9, "physics")])

        rows = store.forecast_vs_actual_before(self.HOUR, self.HOUR + 3600, cutoff)
        assert rows[0]["potential_kwh"] == pytest.approx(0.4)

    def test_compaction_keeps_the_history_the_score_window_needs(self, store: Store):
        """Thinning used to leave only the nowcast, which is not a forecast.

        Every issue but the newest was dropped once a target hour aged past the
        issue horizon -- so a day-ahead lookup found nothing and the hour left
        the score without a word.  The horizon now has to outlive the widest
        window anybody scores over.
        """
        hour = 1_700_000_000
        store.upsert_hourly(
            [(hour, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        for lead in range(72, 0, -1):
            store.log_forecast([(hour - lead * 3600, hour, "s1", 0.5, "physics")])
        cutoff = hour - 12 * 3600

        # Twenty days on: past the old fourteen-day horizon, inside a 30-day window.
        store.compact(hour + 20 * 86400, issue_days=35)

        rows = store.forecast_vs_actual_before(hour, hour + 3600, cutoff)
        assert rows[0]["potential_kwh"] == pytest.approx(0.5)


class TestWeather:
    def test_latest_issue_wins_per_target_hour(self, store: Store):
        hour = 1_700_000_000
        store.upsert_weather_forecast(
            [
                (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 10),
                (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 10),
            ]
        )
        rows = store.latest_forecast(hour, hour + 3600, "open_meteo")
        assert len(rows) == 1
        assert rows[0]["ghi_wm2"] == 550.0

    def test_all_issues_are_kept_for_bias_learning(self, store: Store):
        hour = 1_700_000_000
        store.upsert_weather_forecast(
            [
                (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 10),
                (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 10),
            ]
        )
        rows = store.forecast_for_verification(hour, hour + 3600, "open_meteo")
        assert len(rows) == 2

    def test_actual_upsert_merges_partial_rows(self, store: Store):
        store.upsert_weather_actual([(300, 20.0, None, None, None, None, None, None)])
        store.upsert_weather_actual([(300, None, 60.0, None, None, None, 500.0, None)])
        row = store.weather_actual_range(0, 600)[0]
        assert row["temp_c"] == 20.0
        assert row["humidity_pct"] == 60.0
        assert row["ghi_wm2"] == 500.0


class TestPlantState:
    def test_grid_split_into_import_and_export(self, store: Store):
        store.upsert_plant_state(
            [
                (0, 50.0, 0.0, 1200.0, 1200.0),
                (300, 50.0, 0.0, -600.0, 0.0),
            ]
        )
        imported, exported = store.grid_energy_kwh(0, 600)
        assert imported == pytest.approx(1200 / 12 / 1000)
        assert exported == pytest.approx(600 / 12 / 1000)


class TestModelState:
    def test_effects_roundtrip(self, store: Store):
        store.save_effects("plant", {"clear|midday": (0.1, 5.0)}, 1_700_000_000)
        assert store.load_effects("plant") == {"clear|midday": (0.1, 5.0)}

    def test_reset_clears_effects_and_bias(self, store: Store):
        store.save_effects("plant", {"clear|midday": (0.1, 5.0)}, 0)
        store.save_ghi_bias("open_meteo", {(12, "0-6h"): (0.05, 3.0)}, 0)
        store.clear_effects(None)
        assert store.load_effects("plant") == {}
        assert store.load_ghi_bias("open_meteo") == {}

    def test_cursor_defaults_and_persists(self, store: Store):
        assert store.get_cursor("learn", default=42) == 42
        store.set_cursor("learn", 100)
        assert store.get_cursor("learn") == 100


class TestHousekeeping:
    def test_compaction_keeps_hourly_and_model_state(self, store: Store):
        store.upsert_5min([(0, "s1", 50.0, 600.0, 1.0, 10, None, None, "measured")])
        store.upsert_hourly(
            [(0, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        store.save_effects("plant", {"clear|midday": (0.1, 5.0)}, 0)

        store.compact(now_ts=200 * 86400, raw_days=90)

        assert store.fivemin_range("s1", 0, 3600) == []
        assert len(store.hourly_range(0, 3600)) == 1
        assert store.load_effects("plant") != {}

    def test_raw_rows_survive_while_their_hour_is_unfolded(self, store: Store):
        """Deleting a five-minute row whose hour was never materialised would
        remove energy from the lifetime total with nothing to replace it."""
        store.upsert_5min([(0, "s1", 50.0, 600.0, 1.0, 10, None, None, "measured")])
        store.compact(now_ts=200 * 86400, raw_days=90)
        assert len(store.fivemin_range("s1", 0, 3600)) == 1

    def test_recent_raw_rows_are_untouched(self, store: Store):
        now = 200 * 86400
        store.upsert_5min([(now - 3600, "s1", 50.0, 600.0, 1.0, 10, None, None, "measured")])
        store.upsert_hourly(
            [(now - 3600, "s1", 1.0, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        store.compact(now_ts=now, raw_days=90)
        assert len(store.fivemin_range("s1", now - 7200, now)) == 1

    def test_only_the_closest_forecast_issue_survives(self, store: Store):
        hour = 100 * 86400
        store.upsert_weather_forecast([
            (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 10),
            (hour - 7200, hour, "open_meteo", 2, 500.0, *[None] * 10),
            (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 10),
        ])
        store.compact(now_ts=200 * 86400, issue_days=14)
        rows = store.forecast_for_verification(hour, hour + 3600, "open_meteo")
        assert len(rows) == 1
        assert rows[0]["horizon_h"] == 1, "the run closest to the hour is the best estimate"

    def test_recent_issues_are_all_kept_for_bias_learning(self, store: Store):
        now = 200 * 86400
        hour = now - 3600
        store.upsert_weather_forecast([
            (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 10),
            (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 10),
        ])
        store.compact(now_ts=now, issue_days=14)
        assert len(store.forecast_for_verification(hour, hour + 3600, "open_meteo")) == 2


class TestLongRangeTotals:
    """Lifetime figures are read over the whole period since commissioning, so
    they must not depend on raw rows that compaction is allowed to remove."""

    def test_production_total_survives_compaction(self, store: Store):
        now = 200 * 86400
        hour = now - 200 * 3600
        store.upsert_5min([
            (hour + i * 300, "s1", 100.0, 1200.0, 1.0, 10, None, None, "measured")
            for i in range(12)
        ])
        store.upsert_hourly(
            [(hour, "s1", 1.2, 1.0, 0.0, None, None, None, "measured", "exact")]
        )
        before = store.energy_kwh_between(hour, hour + 3600, "s1")
        store.compact(now_ts=now, raw_days=0)
        after = store.energy_kwh_between(hour, hour + 3600, "s1")
        assert before == pytest.approx(1.2)
        assert after == pytest.approx(before)

    def test_grid_totals_survive_compaction(self, store: Store):
        now = 200 * 86400
        hour = now - 200 * 3600
        store.upsert_plant_state(
            [(hour + i * 300, 50.0, 0.0, 1200.0 if i < 6 else -600.0, 800.0)
             for i in range(12)]
        )
        store.materialise_plant_hourly(hour, hour + 3600)
        before = store.grid_energy_kwh(hour, hour + 3600)
        store.compact(now_ts=now, raw_days=0)
        after = store.grid_energy_kwh(hour, hour + 3600)
        assert before[0] == pytest.approx(0.6)   # 6 x 1200 W x 5 min = 600 Wh
        assert before[1] == pytest.approx(0.3)   # 6 x  600 W x 5 min = 300 Wh
        assert after == pytest.approx(before)

    def test_import_and_export_are_not_netted(self, store: Store):
        """Half an hour drawing and half exporting nets to zero and has two
        very different tariff outcomes."""
        hour = 0
        store.upsert_plant_state(
            [(i * 300, None, None, 600.0 if i < 6 else -600.0, None) for i in range(12)]
        )
        store.materialise_plant_hourly(hour, hour + 3600)
        imported, exported = store.grid_energy_kwh(hour, hour + 3600)
        assert imported == pytest.approx(0.3)
        assert exported == pytest.approx(0.3)

    def test_statistics_report_every_table(self, store: Store):
        stats = store.statistics()
        assert stats["schema_version"] == SCHEMA_VERSION
        assert stats["string_5min"] == 0
        assert "size_bytes" in stats

    def test_reopening_an_existing_database_is_safe(self, tmp_path):
        path = tmp_path / "reopen.db"
        with Store(path) as first:
            first.add_geometry("s1", GeometrySegment(0, 180, 30, 1.8))
        with Store(path) as second:
            assert second.geometry_at("s1", 100).kwp == 1.8


class TestCensoringIsReversible:
    """The binding verdict is recomputed whenever physics is.

    A row that was marked as a lower bound on one pass must be able to return
    to being an exact measurement on the next -- otherwise a single bad physics
    estimate censors that interval for good.
    """

    def _row(self, ts=300):
        return (ts, "s1", 50.0, 600.0, 1.0, 10, 500.0, None, "measured")

    def test_binding_censors(self, store: Store):
        store.upsert_5min([self._row()])
        store.update_curtailment_flags([(1, 300, "s1")])
        assert store.fivemin_range("s1", 0, 900)[0]["value_kind"] == "lower_bound"

    def test_clearing_the_verdict_restores_the_measurement(self, store: Store):
        store.upsert_5min([self._row()])
        store.update_curtailment_flags([(1, 300, "s1")])
        store.update_curtailment_flags([(0, 300, "s1")])
        row = store.fivemin_range("s1", 0, 900)[0]
        assert row["limit_binding"] == 0
        assert row["value_kind"] == "measured"

    def test_unknown_verdict_leaves_the_kind_alone(self, store: Store):
        store.upsert_5min([self._row()])
        store.update_curtailment_flags([(1, 300, "s1")])
        store.update_curtailment_flags([(None, 300, "s1")])
        assert store.fivemin_range("s1", 0, 900)[0]["value_kind"] == "lower_bound"

    def test_reconstructed_is_never_overwritten(self, store: Store):
        """That kind did not come from the binding test, so it is not ours."""
        store.upsert_5min([self._row()])
        store.set_value_kind(300, "s1", "reconstructed")
        store.update_curtailment_flags([(0, 300, "s1")])
        assert store.fivemin_range("s1", 0, 900)[0]["value_kind"] == "reconstructed"


class TestResettingLearning:
    """The sky map is a learned correction and must go with the rest.

    Clearing the effects while leaving the map behind is worse than clearing
    neither: the forecast keeps being multiplied down, without the per-string
    level the model had learned to offset it.  It is also the only way back
    from a backfill built on a mis-scaled sensor.
    """

    def _observe(self, store: Store) -> None:
        store.add_shading_obs(
            [(1_700_000_000 + index * 300, "s1", 180.0, 30.0, 0.5, 1.0) for index in range(20)]
        )

    def test_observations_are_discarded(self, store: Store):
        self._observe(store)
        assert store.shading_count() == 20
        assert store.clear_shading_obs() == 20
        assert store.shading_count() == 0

    def test_clearing_an_empty_table_is_harmless(self, store: Store):
        assert store.clear_shading_obs() == 0

    def test_a_refit_after_clearing_corrects_nothing(self, store: Store):
        self._observe(store)
        store.clear_shading_obs()
        assert store.shading_rows_by_string() == {}

    def test_one_string_can_be_cleared_without_its_siblings(self, store: Store):
        """The repair for one corrected geometry: the siblings' rows were
        never wrong and must survive."""
        self._observe(store)  # 20 rows for s1
        store.add_shading_obs(
            [(1_700_100_000 + i * 300, "s2", 180.0, 30.0, 0.9, 1.0) for i in range(7)]
        )
        assert store.clear_shading_obs("s1") == 20
        assert store.shading_count("s1") == 0
        assert store.shading_count("s2") == 7

    def test_string_effects_go_but_plant_scope_and_bias_stay(self, store: Store):
        store.save_effects("plant", {"clear|midday": (0.1, 20.0)}, 1_700_000_000)
        store.save_effects("string", {"s1": (0.2, 10.0), "s2": (0.3, 10.0)}, 1_700_000_000)
        store.save_effects(
            "string_daypart",
            {"s1|morning": (-0.1, 8.0), "s2|morning": (0.05, 8.0)},
            1_700_000_000,
        )
        store.save_ghi_bias("open_meteo", {(12, "0-6h"): (0.02, 5.0)}, 1_700_000_000)

        store.clear_effects_for_string("s1")

        assert "s1" not in store.load_effects("string")
        assert "s2" in store.load_effects("string")
        assert "s1|morning" not in store.load_effects("string_daypart")
        assert "s2|morning" in store.load_effects("string_daypart")
        assert store.load_effects("plant"), "plant scope must survive"
        assert store.load_ghi_bias("open_meteo"), "ghi bias must survive"


class TestThinningSparesTheBackfill:
    """Two fixes that were each right and together destroyed the feature.

    Backfilled rows are stamped one second off the five-minute grid so they
    cannot overwrite real measurements.  Old rows are thinned to a quarter to
    keep the refit affordable.  Put together, every backfilled row lands on the
    same residue of ``(ts/300) % 4`` -- never zero -- so the thinning deleted
    all of them instead of three quarters, and a 540-day backfill lost
    everything past four months on its first night.
    """

    NOW = 1_800_000_000
    OLD = NOW - 300 * 86400  # well past the thinning horizon

    def _hour(self, index: int) -> int:
        return (self.OLD + index * 3600) // 3600 * 3600

    def _seed(self, store: Store) -> tuple[int, int]:
        live = [
            (self._hour(0) + step, "s1", 180.0, 30.0, 0.9, 1.0)
            for step in range(0, 3600, 300)
        ]
        backfilled = [
            (self._hour(index) + 1801, "s1", 180.0, 30.0, 0.9, 0.35)
            for index in range(12)
        ]
        store.add_shading_obs(live + backfilled)
        return len(live), len(backfilled)

    def test_backfilled_rows_all_survive(self, store: Store):
        _live, backfilled = self._seed(store)
        store.compact(self.NOW)
        remaining = [
            row
            for row in store.shading_rows_by_string()["s1"]
            if int(row[0]) % 300 != 0
        ]
        assert len(remaining) == backfilled

    def test_the_dense_live_grid_is_still_thinned(self, store: Store):
        live, _backfilled = self._seed(store)
        store.compact(self.NOW)
        remaining = [
            row
            for row in store.shading_rows_by_string()["s1"]
            if int(row[0]) % 300 == 0
        ]
        assert 0 < len(remaining) < live

    def test_recent_observations_are_untouched(self, store: Store):
        recent = [
            (self.NOW - 3600 + step, "s1", 180.0, 30.0, 0.9, 1.0)
            for step in range(0, 3600, 300)
        ]
        store.add_shading_obs(recent)
        store.compact(self.NOW)
        assert store.shading_count() == len(recent)


class TestWeatherOutlook:
    """What the sky is expected to do, for a controller that plans overnight.

    Derived from the stored forecast rather than a second source, so the
    outlook and the yield prediction can never describe different runs.
    """

    HOUR = 1_700_000_000

    def _seed(self, store: Store, hours):
        """hours: list of (offset, rain_probability, clouds, rain_mm)."""
        store.upsert_weather_forecast(
            [
                (
                    self.HOUR - 3600,
                    self.HOUR + offset * 3600,
                    "open_meteo",
                    offset,
                    500.0, None, None, None, clouds, None, None, mm, prob, None, None,
                )
                for offset, prob, clouds, mm in hours
            ]
        )

    def test_the_worst_hour_sets_the_rain_figure(self, store: Store):
        """One hour of certain rain makes a day you plan around.

        Averaging it against twenty-three dry ones hides exactly the thing the
        controller needs to see.
        """
        self._seed(store, [(0, 5.0, 10.0, 0.0), (1, 90.0, 80.0, 4.0), (2, 5.0, 10.0, 0.0)])
        out = store.weather_outlook(self.HOUR, self.HOUR + 3 * 3600, "open_meteo")
        assert out["rain_probability_pct"] == 90.0

    def test_cloud_cover_is_a_mean(self, store: Store):
        self._seed(store, [(0, 0.0, 0.0, 0.0), (1, 0.0, 100.0, 0.0)])
        out = store.weather_outlook(self.HOUR, self.HOUR + 2 * 3600, "open_meteo")
        assert out["clouds_pct"] == 50.0

    def test_rain_volume_is_summed(self, store: Store):
        self._seed(store, [(0, 0.0, 0.0, 1.5), (1, 0.0, 0.0, 2.5)])
        out = store.weather_outlook(self.HOUR, self.HOUR + 2 * 3600, "open_meteo")
        assert out["rain_mm"] == 4.0

    def test_an_empty_window_says_nothing_rather_than_zero(self, store: Store):
        out = store.weather_outlook(self.HOUR, self.HOUR + 3600, "open_meteo")
        assert out == {
            "rain_probability_pct": None,
            "clouds_pct": None,
            "rain_mm": None,
        }

    def test_a_source_that_omits_rain_reports_none_not_zero(self, store: Store):
        """"Nobody asked" and "certainly dry" must not look the same."""
        store.upsert_weather_forecast(
            [(self.HOUR - 3600, self.HOUR, "open_meteo", 1, 500.0, *[None] * 10)]
        )
        out = store.weather_outlook(self.HOUR, self.HOUR + 3600, "open_meteo")
        assert out["rain_probability_pct"] is None

    def test_only_the_newest_issue_counts(self, store: Store):
        """The same rule as the yield forecast, or the two would disagree."""
        store.upsert_weather_forecast(
            [
                (self.HOUR - 86400, self.HOUR, "open_meteo", 24,
                 500.0, None, None, None, 10.0, None, None, 0.0, 5.0, None, None),
                (self.HOUR - 3600, self.HOUR, "open_meteo", 1,
                 500.0, None, None, None, 90.0, None, None, 3.0, 95.0, None, None),
            ]
        )
        out = store.weather_outlook(self.HOUR, self.HOUR + 3600, "open_meteo")
        assert out["rain_probability_pct"] == 95.0
        assert out["clouds_pct"] == 90.0

    def test_another_source_is_not_mixed_in(self, store: Store):
        self._seed(store, [(0, 10.0, 10.0, 0.0)])
        store.upsert_weather_forecast(
            [(self.HOUR - 3600, self.HOUR, "ha_weather", 1,
              500.0, None, None, None, 100.0, None, None, 9.0, 99.0, None, None)]
        )
        out = store.weather_outlook(self.HOUR, self.HOUR + 3600, "open_meteo")
        assert out["rain_probability_pct"] == 10.0


class TestMigrationToRainProbability:
    """Adding a column to a table that already holds a year of rows.

    ``CREATE TABLE IF NOT EXISTS`` shapes a *new* database and silently leaves
    an existing one alone, so without an explicit ALTER the first insert after
    the upgrade fails on the arity -- and the weather stops updating on
    precisely the installations that have been running longest.
    """

    #: The v2 table, exactly as it was before the column existed.
    V2_TABLE = """
    CREATE TABLE weather_forecast (
        issued_at_utc        INTEGER NOT NULL,
        ts_utc               INTEGER NOT NULL,
        source               TEXT    NOT NULL,
        horizon_h            INTEGER NOT NULL,
        ghi_wm2              REAL,
        dni_wm2              REAL,
        dhi_wm2              REAL,
        temp_c               REAL,
        clouds_pct           REAL,
        wind_ms              REAL,
        humidity_pct         REAL,
        rain_mm              REAL,
        pressure_hpa         REAL,
        components_plausible INTEGER,
        PRIMARY KEY (issued_at_utc, ts_utc, source)
    );
    """

    def _v2_database(self, tmp_path):
        import sqlite3

        path = tmp_path / "v2.db"
        conn = sqlite3.connect(path)
        conn.executescript(self.V2_TABLE)
        conn.execute(
            "INSERT INTO weather_forecast VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (100, 200, "open_meteo", 1, 500.0, None, None, 18.0,
             40.0, 2.0, 60.0, 0.0, 1013.0, 1),
        )
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()
        return path

    def test_the_column_is_added(self, tmp_path):
        store = Store(self._v2_database(tmp_path))
        store.connect()
        try:
            columns = {
                row[1] for row in store._query("PRAGMA table_info(weather_forecast)", ())
            }
            assert "rain_probability_pct" in columns
        finally:
            store.close()

    def test_existing_rows_survive_with_an_empty_value(self, tmp_path):
        """NULL, not zero: the source was never asked for it back then."""
        store = Store(self._v2_database(tmp_path))
        store.connect()
        try:
            rows = store._query("SELECT * FROM weather_forecast", ())
            assert len(rows) == 1
            assert rows[0]["ghi_wm2"] == 500.0
            assert rows[0]["clouds_pct"] == 40.0
            assert rows[0]["pressure_hpa"] == 1013.0, "columns must not have shifted"
            assert rows[0]["rain_probability_pct"] is None
        finally:
            store.close()

    def test_writing_works_after_the_upgrade(self, tmp_path):
        store = Store(self._v2_database(tmp_path))
        store.connect()
        try:
            store.upsert_weather_forecast(
                [(100, 300, "open_meteo", 2, 400.0, None, None, None,
                  50.0, None, None, 1.0, 70.0, None, None)]
            )
            out = store.weather_outlook(300, 400, "open_meteo")
            assert out["rain_probability_pct"] == 70.0
        finally:
            store.close()

    def test_running_it_twice_is_harmless(self, tmp_path):
        path = self._v2_database(tmp_path)
        for _ in range(2):
            store = Store(path)
            store.connect()
            store.close()
        store = Store(path)
        store.connect()
        try:
            assert len(store._query("SELECT * FROM weather_forecast", ())) == 1
        finally:
            store.close()


class TestMigrationToJointShadingColumns:
    """Schema v4: the shading table grows the joint fit's two nuisance inputs.

    Same trap as the rain probability column: ``CREATE TABLE IF NOT EXISTS``
    leaves an existing table alone, and the first eight-field insert after the
    upgrade would fail on the arity -- on precisely the installations whose
    observation history makes the joint fit worth having.
    """

    V3_TABLE = """
    CREATE TABLE shading_obs (
        ts_utc        INTEGER NOT NULL,
        string_id     TEXT    NOT NULL,
        azimuth_deg   REAL    NOT NULL,
        elevation_deg REAL    NOT NULL,
        ratio         REAL    NOT NULL,
        weight        REAL    NOT NULL,
        PRIMARY KEY (ts_utc, string_id)
    );
    """

    def _v3_database(self, tmp_path):
        import sqlite3

        path = tmp_path / "v3.db"
        conn = sqlite3.connect(path)
        conn.executescript(self.V3_TABLE)
        conn.execute(
            "INSERT INTO shading_obs VALUES (?,?,?,?,?,?)",
            (1_700_000_000, "s1", 180.0, 30.0, 0.8, 1.0),
        )
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()
        return path

    def test_old_rows_read_back_with_empty_trailing_fields(self, tmp_path):
        store = Store(self._v3_database(tmp_path))
        store.connect()
        try:
            rows = store.shading_rows_by_string()["s1"]
            assert rows == [(1_700_000_000.0, 180.0, 30.0, 0.8, 1.0, None, None)]
        finally:
            store.close()

    def test_new_rows_write_into_the_migrated_table(self, tmp_path):
        store = Store(self._v3_database(tmp_path))
        store.connect()
        try:
            store.add_shading_obs(
                [(1_700_000_300, "s1", 181.0, 31.0, 0.7, 1.0, 450.0, 0.85)]
            )
            # A pre-v4 writer may still hand over six-field rows.
            store.add_shading_obs([(1_700_000_600, "s1", 182.0, 32.0, 0.9, 1.0)])
            rows = store.shading_rows_by_string()["s1"]
            assert (1_700_000_300.0, 181.0, 31.0, 0.7, 1.0, 450.0, 0.85) in rows
            assert (1_700_000_600.0, 182.0, 32.0, 0.9, 1.0, None, None) in rows
        finally:
            store.close()


class TestConversionPairs:
    """Measured both sides of a conversion stage: the training set for the
    efficiency curves, kept whole so it outlives the telemetry it came from.
    """

    def _string_row(self, ts, sid="s1", binding=0, kind="measured", limit=1000.0):
        return (ts, sid, 50.0, 600.0, 1.0, 10, limit, binding, kind)

    def test_pairs_round_trip(self, store: Store):
        store.upsert_conversion([(300, "g1", "inverter", 600.0, 570.0, 1.0, "s1,s2", 1)])
        rows = store.conversion_rows(uncensored_only=False)
        assert rows == [(300, "g1", "inverter", 600.0, 570.0, 1.0, None)]

    def test_unjudged_pairs_are_not_training_data(self, store: Store):
        """NULL means "physics has not looked yet", which is not "clean"."""
        store.upsert_conversion([(300, "g1", "inverter", 600.0, 570.0, 1.0, "s1,s2", 1)])
        assert store.conversion_rows() == []

    def test_censoring_follows_the_contributing_strings(self, store: Store):
        store.upsert_5min(
            [self._string_row(300, "s1"), self._string_row(300, "s2", binding=1)]
        )
        store.upsert_conversion(
            [
                (300, "g1", "inverter", 600.0, 570.0, 1.0, "s1,s2", 1),
                (300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1),
            ]
        )
        store.mark_conversion_censored(0, 1000)
        usable = {row[1] for row in store.conversion_rows()}
        # The group contains the curtailed s2, so its pair is out; the s1
        # mppt pair is untouched by its sibling.
        assert usable == {"s1"}

    def test_an_unjudged_interval_is_not_clean(self, store: Store):
        """``limit_binding IS NULL`` means physics never decided.

        SQL makes that easy to get wrong: ``limit_binding = 1`` is not true
        for NULL, so a naive CASE files an unjudged interval under "free"
        and a curtailed hour with no physics behind it becomes training
        data.
        """
        store.upsert_5min([self._string_row(300, "s1", binding=None)])
        store.upsert_conversion([(300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1)])
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_a_scope_that_cannot_be_curtailed_reads_an_unjudged_interval(
        self, store: Store
    ):
        """Without a limit or a battery nothing can bind, so there is no
        verdict to wait for -- and demanding one would starve exactly the
        simple installations of training data."""
        store.upsert_5min(
            [self._string_row(300, "s1", binding=None, limit=None)]
        )
        store.upsert_conversion([(300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 0)])
        store.mark_conversion_censored(0, 1000)
        assert len(store.conversion_rows()) == 1

    def test_a_missing_member_row_is_missing_evidence(self, store: Store):
        """s2 has no row at all: the input sum cannot be vouched for."""
        store.upsert_5min([self._string_row(300, "s1")])
        store.upsert_conversion(
            [(300, "g1", "inverter", 600.0, 570.0, 1.0, "s1,s2", 1)]
        )
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_membership_is_read_from_the_row_not_from_config(self, store: Store):
        """The pair was measured over s1+s2; regrouping later must not make
        s2's curtailment invisible to it."""
        store.upsert_5min(
            [self._string_row(300, "s1"), self._string_row(300, "s2", binding=1)]
        )
        store.upsert_conversion(
            [(300, "g1", "inverter", 600.0, 570.0, 1.0, "s1,s2", 1)]
        )
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_one_groups_curtailment_cannot_censor_another(self, store: Store):
        store.upsert_5min(
            [self._string_row(300, "s1"), self._string_row(300, "s2", binding=1)]
        )
        store.upsert_conversion(
            [
                (300, "gA", "inverter", 600.0, 570.0, 1.0, "s1", 1),
                (300, "gB", "inverter", 600.0, 570.0, 1.0, "s2", 1),
            ]
        )
        store.mark_conversion_censored(0, 1000)
        assert {row[1] for row in store.conversion_rows()} == {"gA"}

    def test_a_reconstructed_interval_is_not_a_measurement(self, store: Store):
        store.upsert_5min([self._string_row(300, "s1", kind="lower_bound")])
        store.upsert_conversion([(300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1)])
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_a_reflush_does_not_undo_the_verdict(self, store: Store):
        store.upsert_5min([self._string_row(300, "s1")])
        store.upsert_conversion([(300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1)])
        store.mark_conversion_censored(0, 1000)
        store.upsert_conversion([(300, "s1", "mppt", 610.0, 590.0, 1.0, "s1", 1)])
        rows = store.conversion_rows()
        assert len(rows) == 1 and rows[0][3] == 610.0

    def test_a_loadless_interval_is_not_evidence(self, store: Store):
        """Night reads 0 in, 0 out. That is not an efficiency of anything,
        and a fit dividing by the input would divide by zero."""
        store.upsert_5min([self._string_row(300, "s1")])
        store.upsert_conversion([(300, "s1", "mppt", 0.0, 0.0, 1.0, "s1", 0)])
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_a_negative_output_is_a_sign_error_not_a_conversion(self, store: Store):
        store.upsert_5min([self._string_row(300, "s1")])
        store.upsert_conversion([(300, "s1", "mppt", 600.0, -20.0, 1.0, "s1", 0)])
        store.mark_conversion_censored(0, 1000)
        assert store.conversion_rows() == []

    def test_counts_report_evidence_per_scope(self, store: Store):
        store.upsert_5min([self._string_row(300, "s1")])
        store.upsert_conversion(
            [
                (300, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1),
                (600, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1),
            ]
        )
        store.mark_conversion_censored(0, 500)
        counts = store.conversion_counts()
        assert counts["s1|mppt"] == {"rows": 2, "usable": 1}

    def test_pairs_outlive_the_telemetry_they_came_from(self, store: Store):
        """Compaction drops raw 5-minute rows; training data must survive."""
        now = 1_800_000_000
        old = now - 200 * 86400
        store.upsert_5min([self._string_row(old, "s1")])
        store.upsert_conversion([(old, "s1", "mppt", 600.0, 585.0, 1.0, "s1", 1)])
        store.mark_conversion_censored(old - 1, old + 1)
        store.compact(now, raw_days=90)
        assert len(store.conversion_rows()) == 1


class TestMigrationToPoaBeam:
    """v4 -> v5: the beam column switches meaning (horizontal -> POA share).

    Old values must not be read under the new meaning, so the upgrade nulls
    them; nulled rows take the beam_known=False path.  Nulling happens before
    the version stamp, so a crash cannot leave a v5-stamped db with
    horizontal values inside.
    """

    V4_TABLE = """
    CREATE TABLE shading_obs (
        ts_utc        INTEGER NOT NULL,
        string_id     TEXT    NOT NULL,
        azimuth_deg   REAL    NOT NULL,
        elevation_deg REAL    NOT NULL,
        ratio         REAL    NOT NULL,
        weight        REAL    NOT NULL,
        physics_w     REAL,
        beam          REAL,
        PRIMARY KEY (ts_utc, string_id)
    );
    """

    def _v4_database(self, tmp_path):
        import sqlite3

        path = tmp_path / "v4.db"
        conn = sqlite3.connect(path)
        conn.executescript(self.V4_TABLE)
        conn.execute(
            "INSERT INTO shading_obs VALUES (?,?,?,?,?,?,?,?)",
            (1_700_000_000, "s1", 180.0, 30.0, 0.8, 1.0, 500.0, 0.82),
        )
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        conn.close()
        return path

    def test_v4_beam_values_are_nulled(self, tmp_path):
        store = Store(self._v4_database(tmp_path))
        store.connect()
        try:
            rows = store.shading_rows_by_string()["s1"]
            assert rows == [(1_700_000_000.0, 180.0, 30.0, 0.8, 1.0, 500.0, None)]
        finally:
            store.close()

    def test_new_poa_values_survive_the_next_connect(self, tmp_path):
        path = self._v4_database(tmp_path)
        store = Store(path)
        store.connect()
        store.add_shading_obs(
            [(1_700_000_300, "s1", 181.0, 31.0, 0.7, 1.0, 450.0, 0.9)]
        )
        store.close()
        store = Store(path)
        store.connect()
        try:
            rows = store.shading_rows_by_string()["s1"]
            assert (1_700_000_300.0, 181.0, 31.0, 0.7, 1.0, 450.0, 0.9) in rows
        finally:
            store.close()
