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


class TestWeather:
    def test_latest_issue_wins_per_target_hour(self, store: Store):
        hour = 1_700_000_000
        store.upsert_weather_forecast(
            [
                (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 9),
                (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 9),
            ]
        )
        rows = store.latest_forecast(hour, hour + 3600, "open_meteo")
        assert len(rows) == 1
        assert rows[0]["ghi_wm2"] == 550.0

    def test_all_issues_are_kept_for_bias_learning(self, store: Store):
        hour = 1_700_000_000
        store.upsert_weather_forecast(
            [
                (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 9),
                (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 9),
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
            (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 9),
            (hour - 7200, hour, "open_meteo", 2, 500.0, *[None] * 9),
            (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 9),
        ])
        store.compact(now_ts=200 * 86400, issue_days=14)
        rows = store.forecast_for_verification(hour, hour + 3600, "open_meteo")
        assert len(rows) == 1
        assert rows[0]["horizon_h"] == 1, "the run closest to the hour is the best estimate"

    def test_recent_issues_are_all_kept_for_bias_learning(self, store: Store):
        now = 200 * 86400
        hour = now - 3600
        store.upsert_weather_forecast([
            (hour - 86400, hour, "open_meteo", 24, 400.0, *[None] * 9),
            (hour - 3600, hour, "open_meteo", 1, 550.0, *[None] * 9),
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
