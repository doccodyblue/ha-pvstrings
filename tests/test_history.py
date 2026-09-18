"""The look back: a past day in full, accuracy by week, and the baseline behind it.

Everything here republishes numbers the scores already use, so most tests hold
the new figures against the score itself rather than against hand-computed
expectations -- the thing that must not happen is the two drifting apart.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import history
from core.forecast import CURSOR_LEARN, HOUR, ForecastEngine, _ScoreTally
from core.store import Store

from test_forecast_engine import DAY_START, clear_sky_forecast

TZ = ZoneInfo("Europe/Berlin")
#: 2025-06-23, the Monday after DAY_START (a Saturday).
MONDAY = date(2025, 6, 23)
MONDAY_TS = DAY_START + 2 * 86400
ISSUE_DAYS = 35


@pytest.fixture
def engine(seeded_store: Store, plant) -> ForecastEngine:
    engine = ForecastEngine(plant, seeded_store)
    engine.load_models()
    return engine


def midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


def measured_day(
    engine: ForecastEngine,
    store: Store,
    day: date,
    evening_factor: float,
    baseline_factor: float | None = None,
    value_kind: str = "measured",
    curtailed: float = 0.0,
) -> list:
    """A complete day: clear-sky actuals, a perfect nowcast, an evening run off by a factor."""
    start = midnight(day)
    clear_sky_forecast(engine, store, start - 7 * HOUR, start, 24)
    rows = engine.forecast(start, hours=24, start_ts=start, apply_learning=False)
    cutoff = engine.day_ahead_cutoff(start)
    store.upsert_hourly(
        [
            (row.ts_utc, row.string_id, row.potential_kwh, 1.0, curtailed, None, None,
             None, value_kind, "exact" if row.potential_kwh > 0 else "night")
            for row in rows
        ]
    )
    store.log_forecast(
        [(row.ts_utc - HOUR, row.ts_utc, row.string_id, row.potential_kwh, "c")
         for row in rows]
    )
    store.log_forecast(
        [
            (cutoff, row.ts_utc, row.string_id, row.potential_kwh * evening_factor, "c",
             None if baseline_factor is None else row.potential_kwh * baseline_factor,
             row.potential_kwh)
            for row in rows
        ]
    )
    return rows


#: Over- and undershooting days, so a daily absolute error differs from the
#: absolute value of the weekly sum -- the distinction the week must keep.
FACTORS = (0.5, 1.4, 0.8, 1.2, 0.6, 1.1, 0.9)


class TestBaselineRun:
    def test_it_leaves_the_live_nowcast_state_alone(self, engine: ForecastEngine,
                                                    seeded_store: Store):
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 72)
        sentinel = object()
        engine.last_nowcast = sentinel
        engine.last_nowcast_reason = "measured"

        engine.baseline_forecast(DAY_START + 10 * HOUR, hours=72, start_ts=DAY_START)

        assert engine.last_nowcast is sentinel
        assert engine.last_nowcast_reason == "measured"

    def test_it_ignores_every_learned_layer(self, engine: ForecastEngine,
                                            seeded_store: Store, monkeypatch):
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 72)
        now = DAY_START + 10 * HOUR
        bare = engine.baseline_forecast(now, hours=72, start_ts=DAY_START)
        monkeypatch.setattr(engine.model, "factor", lambda *_: 2.0)

        learned = engine.forecast(now, hours=72, start_ts=DAY_START)
        again = engine.baseline_forecast(now, hours=72, start_ts=DAY_START)

        assert again == bare
        daylight = [row for row in learned if row.physics_kwh > 0]
        assert daylight and all(
            row.potential_kwh == pytest.approx(2.0 * bare[(row.ts_utc, row.string_id)])
            for row in daylight
        )

    def test_it_is_logged_next_to_the_forecast(self, engine: ForecastEngine,
                                               seeded_store: Store):
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 72)
        now = DAY_START + 10 * HOUR + 600
        baseline = engine.baseline_forecast(now, hours=72, start_ts=DAY_START)
        rows = engine.forecast(now, hours=72, start_ts=DAY_START)
        engine.log_forecast(now, rows, baseline)

        logged = seeded_store._query(
            "SELECT * FROM forecast_log WHERE ts_utc = ? AND string_id = 's1'",
            (DAY_START + 13 * HOUR,),
        )[0]
        assert logged["baseline_kwh"] == pytest.approx(
            baseline[(DAY_START + 13 * HOUR, "s1")], abs=1e-5
        )
        assert logged["unshaded_kwh"] is not None

    def test_a_replay_reads_the_weather_as_it_stood(self, engine: ForecastEngine,
                                                    seeded_store: Store):
        early, late = DAY_START - 10 * HOUR, DAY_START - 2 * HOUR
        clear_sky_forecast(engine, seeded_store, early, DAY_START, 72, scale=0.5)
        clear_sky_forecast(engine, seeded_store, late, DAY_START, 72, scale=1.0)
        now = DAY_START + 12 * HOUR

        replay = engine.baseline_forecast(
            now, hours=72, start_ts=DAY_START, weather_issued_before=early
        )
        live = engine.baseline_forecast(now, hours=72, start_ts=DAY_START)
        key = (DAY_START + 12 * HOUR, "s1")
        # Half the irradiance; not exactly half the energy -- cooler cells.
        assert 0.45 < replay[key] / live[key] < 0.65


class TestDay:
    def test_every_hour_with_a_forecast_or_a_measurement(self, engine: ForecastEngine,
                                                         seeded_store: Store):
        day = date(2025, 6, 21)
        start = midnight(day)
        noon = start + 12 * HOUR
        # s1: forecast only at 11, measurement only at 13, both at 12.
        seeded_store.log_forecast([(noon - 2 * HOUR, noon - HOUR, "s1", 0.4, "c", 0.5, 0.45)])
        seeded_store.log_forecast([(noon - HOUR, noon, "s1", 0.6, "c", 0.7, 0.65)])
        seeded_store.upsert_hourly(
            [
                (noon, "s1", 0.55, 1.0, 0.0, None, None, None, "measured", "exact"),
                (noon + HOUR, "s1", 0.3, 0.9, 0.25, None, None, None, "measured", "exact"),
                (noon, "s3", 0.2, 1.0, 0.0, None, None, None, "reconstructed", "exact"),
            ]
        )
        payload = history.day_payload(engine, day)

        s1 = {h["start"][11:16]: h for h in payload["strings"]["s1"]["hours"]}
        assert set(s1) == {"11:00", "12:00", "13:00"}
        assert s1["11:00"]["actual_kwh"] is None and s1["11:00"]["forecast_kwh"] == 0.4
        assert s1["12:00"]["baseline_kwh"] == 0.7 and s1["12:00"]["actual_kwh"] == 0.55
        assert s1["13:00"]["forecast_kwh"] is None
        assert s1["13:00"]["censored"] is True  # curtailed
        assert s1["12:00"]["censored"] is False
        assert s1["12:00"]["start"].endswith("+02:00")

        plant = {h["start"][11:16]: h for h in payload["plant"]["hours"]}
        # s1 measured and s3 reconstructed: the sum of both, censored by s3.
        assert plant["12:00"]["actual_kwh"] == pytest.approx(0.75)
        assert plant["12:00"]["forecast_kwh"] == pytest.approx(0.6)
        assert plant["12:00"]["censored"] is True
        assert plant["11:00"]["actual_kwh"] is None
        assert payload["strings"]["s2"]["hours"] == []

    def test_censored_is_the_scores_own_rule(self, engine: ForecastEngine,
                                             seeded_store: Store):
        day = date(2025, 6, 23)
        measured_day(engine, seeded_store, day, 0.5, value_kind="reconstructed")
        measured_day(engine, seeded_store, day + timedelta(days=1), 0.5, curtailed=0.2)
        measured_day(engine, seeded_store, day + timedelta(days=2), 0.5)
        for offset in range(3):
            current = day + timedelta(days=offset)
            payload = history.day_payload(engine, current)
            tally = _ScoreTally()
            start = midnight(current)
            engine._tally(
                seeded_store.forecast_vs_actual_before(
                    start, start + 86400, engine.day_ahead_cutoff(start)
                ),
                tally,
            )
            flagged = sum(
                1
                for hours in payload["strings"].values()
                for h in hours["hours"]
                if h["actual_kwh"] is not None and not h["censored"]
                and h["value_kind"] is not None
                and seeded_store._query(
                    "SELECT quality FROM string_hourly WHERE ts_utc = ?",
                    (int(datetime.fromisoformat(h["start"]).timestamp()),),
                )[0]["quality"] != "night"
            )
            assert flagged == len(tally.uncensored)

    def test_the_day_ahead_figure_names_the_run_it_came_from(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        day = date(2025, 6, 21)
        start = midnight(day)
        cutoff = engine.day_ahead_cutoff(start)
        hour = start + 9 * HOUR
        # HA was down at six: the five o'clock run is what the reader saw.
        seeded_store.log_forecast([(cutoff - HOUR, hour, "s1", 0.3, "c")])
        seeded_store.log_forecast([(hour - HOUR, hour, "s1", 0.5, "c")])

        entry = history.day_payload(engine, day)["strings"]["s1"]["hours"][0]
        assert entry["day_ahead_kwh"] == 0.3
        assert entry["day_ahead_issued_at"] == datetime.fromtimestamp(
            cutoff - HOUR, tz=TZ
        ).isoformat()
        assert entry["forecast_kwh"] == 0.5
        assert entry["baseline_kwh"] is None  # logged before the column existed

    @pytest.mark.parametrize(
        ("day", "hours", "slots"),
        [(date(2025, 10, 26), 25, 300), (date(2025, 3, 30), 23, 276)],
    )
    def test_dst_days_are_as_long_as_they_are(self, engine: ForecastEngine,
                                              seeded_store: Store, day, hours, slots):
        start, end = midnight(day), midnight(day + timedelta(days=1))
        assert (end - start) // HOUR == hours
        seeded_store.log_forecast(
            [(ts - HOUR, ts, "s1", 0.1, "c") for ts in range(start, end, HOUR)]
        )
        seeded_store.upsert_5min(
            [(start + 7200, "s1", 25.0, 300.0, 1.0, 1, None, None, "measured")]
        )
        payload = history.day_payload(engine, day)
        assert len(payload["plant"]["hours"]) == hours
        assert len({h["start"] for h in payload["plant"]["hours"]}) == hours
        power = payload["plant"]["intervals"]["power_w"]
        assert len(power) == slots
        assert power[24] == pytest.approx(300.0)

    def test_a_gap_in_the_five_minutes_is_null_not_zero(self, engine: ForecastEngine,
                                                        seeded_store: Store):
        day = date(2025, 6, 21)
        start = midnight(day)
        seeded_store.upsert_5min(
            [
                (start + 12 * HOUR, "s1", 50.0, 600.0, 1.0, 1, None, None, "measured"),
                (start + 12 * HOUR, "s2", 25.0, 300.0, 1.0, 1, None, None, "measured"),
                (start + 12 * HOUR + 300, "s2", 0.0, 0.0, 1.0, 1, None, None, "measured"),
            ]
        )
        payload = history.day_payload(engine, day)
        slot = 12 * 12
        s1 = payload["strings"]["s1"]["intervals"]["power_w"]
        assert s1[slot] == pytest.approx(600.0)
        assert s1[slot + 1] is None
        plant = payload["plant"]["intervals"]["power_w"]
        assert plant[slot] == pytest.approx(900.0)
        assert plant[slot + 1] == 0.0  # measured zero stays zero
        assert plant[slot + 2] is None
        # A string without a single raw row: compacted away, not a dark day.
        assert payload["strings"]["s3"]["intervals"]["power_w"] == []

    def test_it_is_plain_json_and_small(self, engine: ForecastEngine,
                                        seeded_store: Store):
        day = date(2025, 6, 23)
        measured_day(engine, seeded_store, day, 0.5, baseline_factor=0.9)
        start = midnight(day)
        seeded_store.upsert_5min(
            [(start + i * 300, sid, 10.0, 120.0, 1.0, 1, None, None, "measured")
             for i in range(288) for sid in ("s1", "s2", "s3")]
        )
        payload = history.day_payload(engine, day)
        encoded = json.dumps(payload)
        assert len(encoded) < 40_000
        assert payload["earliest_date"] == "2025-06-23"
        assert payload["intervals_since"] == "2025-06-23"
        assert payload["day_ahead_since"] == "2025-06-23"


class TestWeek:
    def _week(self, engine, store, days=7, baseline=None, skip=()):
        for offset in range(days):
            if offset in skip:
                continue
            measured_day(engine, store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=baseline)

    def test_the_week_reproduces_the_seven_day_score(self, engine: ForecastEngine,
                                                     seeded_store: Store):
        """Only on a Monday are the calendar week and the last seven days the same window."""
        self._week(engine, seeded_store)
        now = MONDAY_TS + 7 * 86400 + 1800
        week = history.week_payload(engine, MONDAY, now, None)
        score = engine.score_day_ahead(7, now)

        day_ahead = week["day_ahead"]
        assert week["baseline"] is None
        assert week["days_scored"] == score["days_scored"] == 7
        assert day_ahead["abs_error_kwh"] / day_ahead["actual_kwh"] == pytest.approx(
            score["uncensored"]["wmape"], abs=1e-4
        )
        assert week["hours_uncensored"] == score["hours_uncensored"]
        assert week["hourly_profile"] == score["hourly_profile"]
        # Daily, not weekly: the over- and undershoots must not cancel.
        assert day_ahead["abs_error_kwh"] > abs(
            day_ahead["forecast_kwh"] - day_ahead["actual_kwh"]
        ) + 1.0

    def test_a_thin_week_is_still_summed(self, engine: ForecastEngine,
                                         seeded_store: Store):
        self._week(engine, seeded_store, skip=(1, 3, 5))
        now = MONDAY_TS + 7 * 86400 + 1800
        week = history.week_payload(engine, MONDAY, now, None)
        score = engine.score_day_ahead(7, now)
        assert week["days_scored"] == 4
        assert week["day_ahead"]["abs_error_kwh"] / week["day_ahead"][
            "actual_kwh"
        ] == pytest.approx(score["uncensored"]["wmape"], abs=1e-4)

    def test_the_running_week_counts_only_complete_days(self, engine: ForecastEngine,
                                                        seeded_store: Store):
        self._week(engine, seeded_store, days=4)
        now = MONDAY_TS + 3 * 86400 + 12 * HOUR  # Thursday noon
        week = history.week_payload(engine, MONDAY, now, None)
        assert week["days_scored"] == 3

    def test_both_blocks_cover_the_same_hours(self, engine: ForecastEngine,
                                              seeded_store: Store):
        # Days 0-2 before the update (no baseline), 3-6 after.
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=None if offset < 3 else 1.3)
        now = MONDAY_TS + 7 * 86400 + 1800
        week = history.week_payload(engine, MONDAY, now, None)

        assert week["baseline"] is not None
        assert week["day_ahead"]["hours"] == week["baseline"]["hours"]
        assert week["day_ahead"]["actual_kwh"] == week["baseline"]["actual_kwh"]
        # Four days of seven, and the full score still describes all seven.
        assert week["days_scored"] == 7
        assert week["day_ahead"]["hours"] < week["hours_uncensored"]
        expected = sum(
            abs(FACTORS[offset] - 1.0) for offset in range(3, 7)
        )
        per_day_actual = week["baseline"]["actual_kwh"] / 4
        assert week["day_ahead"]["abs_error_kwh"] == pytest.approx(
            expected * per_day_actual, rel=1e-3
        )
        assert week["baseline"]["abs_error_kwh"] == pytest.approx(
            0.3 * 4 * per_day_actual, rel=1e-3
        )


class TestWhereTheBaselineCameFrom:
    """A week may only carry a causal claim if it can say where it came from.

    The published figure and the baseline are always over the same hours (see
    above), but a *reconstructed* baseline was computed with today's code
    against a forecast published by the code of back then.  Summing that into
    "learning avoided N kWh" charges every later fix to the learning, so the
    week has to be honest about its own provenance -- per week, because one
    week can hold both kinds.
    """

    NOW = MONDAY_TS + 7 * 86400 + 2 * HOUR

    def test_a_week_logged_live_says_live(self, engine: ForecastEngine,
                                          seeded_store: Store):
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=1.3)
        week = history.week_payload(engine, MONDAY, self.NOW, None, replay=True)
        assert week["baseline_basis"] == history.BASIS_LIVE

    def test_a_week_that_had_to_be_rebuilt_says_so(self, engine: ForecastEngine,
                                                   seeded_store: Store):
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=None)
        week = history.week_payload(engine, MONDAY, self.NOW, None, replay=True)
        assert week["baseline_basis"] == history.BASIS_REPLAYED

    def test_a_week_with_both_kinds_says_mixed(self, engine: ForecastEngine,
                                               seeded_store: Store):
        """The case the old batch-wide flag could not express.

        Three days from before the baseline existed, four from after: one
        week, two comparisons.  Stored as "live" this would have walked into
        the running total as if it had all been measured against a baseline
        of its own time.
        """
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset],
                         baseline_factor=None if offset < 3 else 1.3)
        week = history.week_payload(engine, MONDAY, self.NOW, None, replay=True)
        assert week["baseline_basis"] == history.BASIS_MIXED

    def test_a_week_without_any_baseline_claims_nothing(self, engine: ForecastEngine,
                                                        seeded_store: Store):
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=None)
        week = history.week_payload(engine, MONDAY, self.NOW, None)
        assert week["baseline"] is None
        assert week["baseline_basis"] is None

    def test_a_week_stored_before_this_existed_reads_as_unknown(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """Version-1 rows stay readable and stay out of any total.

        The payload holds sums only, so nothing in it can prove where its
        baseline came from -- and a row is never rescored.  Silently reading
        it as "live" is the one outcome that must not happen.
        """
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=1.3)
        payload = history.week_payload(engine, MONDAY, self.NOW, None, replay=True)
        payload.pop("baseline_basis")
        start, end = history.week_bounds(MONDAY, TZ)
        assert seeded_store.insert_week(
            MONDAY.isoformat(), start, end, self.NOW, False, 1,
            json.dumps(payload),
        )

        weeks = history.weeks_payload(engine, self.NOW)["weeks"]
        stored = next(w for w in weeks if w["week_start"] == MONDAY.isoformat())
        assert stored["baseline"] is not None
        assert stored["baseline_basis"] == history.BASIS_UNKNOWN


class TestTheErrorTwoWays:
    """Daily net and per hour, because they answer different questions."""

    NOW = MONDAY_TS + 7 * 86400 + 2 * HOUR

    @staticmethod
    def _self_cancelling_day(engine: ForecastEngine, store: Store, day: date) -> None:
        """A day promised too high in the morning and too low after noon."""
        start = midnight(day)
        clear_sky_forecast(engine, store, start - 7 * HOUR, start, 24)
        rows = engine.forecast(start, hours=24, start_ts=start, apply_learning=False)
        cutoff = engine.day_ahead_cutoff(start)
        store.upsert_hourly(
            [
                (row.ts_utc, row.string_id, row.potential_kwh, 1.0, 0.0, None, None,
                 None, "measured", "exact" if row.potential_kwh > 0 else "night")
                for row in rows
            ]
        )
        def factor(ts: int) -> float:
            return 1.4 if datetime.fromtimestamp(ts, tz=TZ).hour < 12 else 0.6
        store.log_forecast(
            [(cutoff, row.ts_utc, row.string_id,
              row.potential_kwh * factor(row.ts_utc), "c",
              row.potential_kwh, row.potential_kwh)
             for row in rows]
        )

    def test_the_hourly_error_does_not_let_a_day_cancel_itself(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The reason the comparison uses the hourly figure.

        A run that promises too much in the morning and too little in the
        afternoon has a daily net near zero and is still wrong twice -- and a
        forecast without a multiplicative correction cancels that way more
        readily than one with it, which would hand the baseline a win it did
        not earn.
        """
        for offset in range(7):
            self._self_cancelling_day(engine, seeded_store,
                                      MONDAY + timedelta(days=offset))
        week = history.week_payload(engine, MONDAY, self.NOW, None)
        published, base = week["day_ahead"], week["baseline"]
        # The published run is wrong twice a day; per day it nearly cancels.
        assert published["abs_error_hourly_kwh"] > 3 * published["abs_error_kwh"]
        # The baseline here is exact, so both of its figures are zero and the
        # daily-net comparison would call the published run almost as good.
        assert base["abs_error_hourly_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert published["abs_error_kwh"] < 0.35 * published["abs_error_hourly_kwh"]

    def test_each_block_names_the_days_and_hours_it_rests_on(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """No figure may be divided by the week's size instead of its own."""
        for offset in range(7):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset],
                         baseline_factor=None if offset < 3 else 1.3)
        week = history.week_payload(engine, MONDAY, self.NOW, None)
        for block in (week["day_ahead"], week["baseline"]):
            assert block["days"] == 4
            assert block["hours"] < week["hours_uncensored"]
        assert week["days_scored"] == 7


class TestClosingWeeks:
    NOW = MONDAY_TS + 7 * 86400 + 2 * HOUR

    def _week(self, engine, store, monday=MONDAY, baseline=1.3):
        for offset in range(7):
            measured_day(engine, store, monday + timedelta(days=offset),
                         FACTORS[offset], baseline_factor=baseline)

    def test_nothing_closes_before_the_learning_cursor_passes(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._week(engine, seeded_store)
        seeded_store.set_cursor(CURSOR_LEARN, self.NOW - 3 * HOUR)  # Sunday 23:00
        assert history.close_weeks(engine, self.NOW, ISSUE_DAYS) == 0
        seeded_store.set_cursor(CURSOR_LEARN, self.NOW - HOUR)
        assert history.close_weeks(engine, self.NOW, ISSUE_DAYS) == 1

    def test_closing_is_idempotent(self, engine: ForecastEngine, seeded_store: Store):
        self._week(engine, seeded_store)
        seeded_store.set_cursor(CURSOR_LEARN, self.NOW)
        history.close_weeks(engine, self.NOW, ISSUE_DAYS)
        before = [tuple(row) for row in seeded_store.weeks()]
        assert history.close_weeks(engine, self.NOW + HOUR, ISSUE_DAYS) == 0
        assert [tuple(row) for row in seeded_store.weeks()] == before

    def test_the_first_close_is_a_backfill_and_later_ones_are_not(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._week(engine, seeded_store)
        seeded_store.set_cursor(CURSOR_LEARN, self.NOW)
        history.close_weeks(engine, self.NOW, ISSUE_DAYS)

        next_monday = MONDAY + timedelta(days=7)
        self._week(engine, seeded_store, monday=next_monday)
        later = self.NOW + 7 * 86400
        seeded_store.set_cursor(CURSOR_LEARN, later)
        history.close_weeks(engine, later, ISSUE_DAYS)

        first, second = seeded_store.weeks()
        assert first["backfilled"] == 1
        assert json.loads(first["payload"])["maturity"] is None
        assert second["backfilled"] == 0
        maturity = json.loads(second["payload"])["maturity"]
        assert set(maturity) == {"weather_n_eff", "sky_cells"}

    def test_a_week_whose_issues_are_gone_is_not_invented(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        self._week(engine, seeded_store)
        much_later = MONDAY_TS + 40 * 86400
        seeded_store.set_cursor(CURSOR_LEARN, much_later)
        assert history.close_weeks(engine, much_later, ISSUE_DAYS) == 0

    def test_an_empty_week_is_not_written(self, engine: ForecastEngine,
                                          seeded_store: Store):
        seeded_store.set_cursor(CURSOR_LEARN, self.NOW)
        assert history.close_weeks(engine, self.NOW, ISSUE_DAYS) == 0
        assert seeded_store.weeks() == []


class TestReplay:
    """A baseline the log never carried, rebuilt from the evening's weather."""

    def _day_with_evening_weather(self, engine, store, day: date, baseline=None):
        start = midnight(day)
        cutoff = engine.day_ahead_cutoff(start)
        run_day_start = midnight(day - timedelta(days=1))
        # The evening issue covers the live window of that run.
        clear_sky_forecast(engine, store, cutoff, run_day_start, 72, scale=0.8)
        live = engine.baseline_forecast(
            cutoff, hours=72, start_ts=run_day_start, weather_issued_before=cutoff + HOUR - 1
        )
        rows = engine.forecast(start, hours=24, start_ts=start, apply_learning=False)
        store.upsert_hourly(
            [(r.ts_utc, r.string_id, r.potential_kwh, 1.0, 0.0, None, None, None,
              "measured", "exact" if r.potential_kwh > 0 else "night") for r in rows]
        )
        store.log_forecast(
            [(cutoff, r.ts_utc, r.string_id, r.potential_kwh * 0.7, "c",
              None if baseline is None else live[(r.ts_utc, r.string_id)], None)
             for r in rows]
        )
        # A later issue with different weather must not leak into the replay.
        clear_sky_forecast(engine, store, cutoff + 2 * HOUR, run_day_start, 72, scale=1.2)
        return live

    def test_replay_equals_what_the_live_log_would_have_held(
        self, engine: ForecastEngine, seeded_store: Store, tmp_path, plant
    ):
        now = MONDAY_TS + 7 * 86400 + 2 * HOUR
        for offset in range(7):
            self._day_with_evening_weather(
                engine, seeded_store, MONDAY + timedelta(days=offset)
            )
        seeded_store.set_cursor(CURSOR_LEARN, now)
        replayed = history.week_payload(engine, MONDAY, now, None, replay=True)

        other = Store(tmp_path / "logged.db")
        other.connect()
        try:
            for sid, row in (("s1", (180, 30, 1.80)), ("s2", (180, 60, 1.00)),
                             ("s3", (110, 27, 0.95))):
                from core.config import GeometrySegment

                other.add_geometry(sid, GeometrySegment(0, *row))
            logged_engine = ForecastEngine(plant, other)
            logged_engine.load_models()
            for offset in range(7):
                self._day_with_evening_weather(
                    logged_engine, other, MONDAY + timedelta(days=offset), baseline=True
                )
            logged = history.week_payload(logged_engine, MONDAY, now, None)
        finally:
            other.close()

        assert replayed["baseline"] is not None
        assert replayed["baseline"] == logged["baseline"]
        assert replayed["day_ahead"] == logged["day_ahead"]

    def test_without_replay_there_is_no_baseline(self, engine: ForecastEngine,
                                                 seeded_store: Store):
        now = MONDAY_TS + 7 * 86400 + 2 * HOUR
        for offset in range(7):
            self._day_with_evening_weather(
                engine, seeded_store, MONDAY + timedelta(days=offset)
            )
        assert history.week_payload(engine, MONDAY, now, None)["baseline"] is None

    def test_compaction_does_not_change_a_backfill(self, engine: ForecastEngine,
                                                   seeded_store: Store):
        now = MONDAY_TS + 7 * 86400 + 2 * HOUR
        for offset in range(7):
            self._day_with_evening_weather(
                engine, seeded_store, MONDAY + timedelta(days=offset)
            )
        before = history.week_payload(engine, MONDAY, now, None, replay=True)
        seeded_store.compact(now, issue_days=ISSUE_DAYS)
        after = history.week_payload(engine, MONDAY, now, None, replay=True)
        assert after == before


class TestTheReplayHasNoHindsight:
    def test_it_does_not_read_a_weather_run_the_original_never_had(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        """The issue is quantised to its hour; the cut-off must not be.

        A reconstruction that may read weather from anywhere inside the issue
        hour sees up to an hour further than the run it stands in for. Only
        reconstructed weeks would carry that advantage, which is the bias
        that makes them incomparable with live ones.
        """
        day = MONDAY
        start = midnight(day)
        clear_sky_forecast(engine, seeded_store, start - 7 * HOUR, start, 24)
        rows = engine.forecast(start, hours=24, start_ts=start, apply_learning=False)
        cutoff = engine.day_ahead_cutoff(start)
        seeded_store.upsert_hourly(
            [
                (row.ts_utc, row.string_id, row.potential_kwh, 1.0, 0.0, None, None,
                 None, "measured", "exact" if row.potential_kwh > 0 else "night")
                for row in rows
            ]
        )
        seeded_store.log_forecast(
            [(cutoff, row.ts_utc, row.string_id, row.potential_kwh * 0.7, "c",
              None, None) for row in rows]
        )
        # A brighter run, issued inside the same hour as the logged issue.
        clear_sky_forecast(engine, seeded_store, cutoff + 1800,
                           midnight(day - timedelta(days=1)), 72, scale=1.6)

        replayed = [dict(r) for r in seeded_store.forecast_vs_actual_before(
            start, start + 86400, cutoff)]
        filled = history._replay_baseline(engine, replayed)

        assert filled
        # The 1.6x run would push the baseline above the measurement it is
        # derived from; the 1.0x weather of the issue itself does not.
        for row in replayed:
            if row["baseline_kwh"] is not None and row["energy_kwh"]:
                assert row["baseline_kwh"] < row["energy_kwh"] * 1.3


class TestWeeksResponse:
    def test_stored_weeks_then_the_running_one(self, engine: ForecastEngine,
                                               seeded_store: Store):
        for offset in range(10):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset % 7], baseline_factor=1.2)
        closed = MONDAY_TS + 7 * 86400 + 2 * HOUR
        seeded_store.set_cursor(CURSOR_LEARN, closed)
        history.close_weeks(engine, closed, ISSUE_DAYS)

        now = MONDAY_TS + 10 * 86400 + 12 * HOUR  # Thursday of the next week
        response = history.weeks_payload(engine, now)
        json.dumps(response)

        weeks = response["weeks"]
        assert [w["week_start"] for w in weeks] == ["2025-06-23", "2025-06-30"]
        assert weeks[0]["complete"] is True and weeks[0]["backfilled"] is True
        assert weeks[1]["complete"] is False and weeks[1]["days_scored"] == 3
        assert weeks[1]["maturity"] is not None
        for week in weeks:
            assert {"week_start", "complete", "backfilled", "days_scored", "day_ahead",
                    "baseline", "hourly_profile", "maturity"} <= set(week)
            assert {"forecast_kwh", "actual_kwh", "abs_error_kwh"} <= set(week["day_ahead"])

    def test_the_week_before_shows_while_it_waits_to_close(
        self, engine: ForecastEngine, seeded_store: Store
    ):
        for offset in range(14):
            measured_day(engine, seeded_store, MONDAY + timedelta(days=offset),
                         FACTORS[offset % 7])
        first_close = MONDAY_TS + 7 * 86400 + 2 * HOUR
        seeded_store.set_cursor(CURSOR_LEARN, first_close)
        history.close_weeks(engine, first_close, ISSUE_DAYS)

        # Monday 00:20 of the week after: the cursor has not passed Sunday yet.
        now = MONDAY_TS + 14 * 86400 + 1200
        weeks = history.weeks_payload(engine, now)["weeks"]
        assert [w["week_start"] for w in weeks] == [
            "2025-06-23", "2025-06-30", "2025-07-07"
        ]
        assert weeks[1]["complete"] is False and weeks[1]["days_scored"] == 7
        assert weeks[2]["days_scored"] == 0
