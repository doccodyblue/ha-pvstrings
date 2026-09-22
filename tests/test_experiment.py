"""The measurement the calibration trial will be decided on.

Every test here exists because an earlier version of the criterion would have
passed it while meaning nothing. The three ways to get a confident, useless
answer are pinned first, before anything that checks the arithmetic.
"""

from __future__ import annotations

import pytest

from core.experiment import (
    MIN_CLEAR_DAYS,
    MIN_TRIAL_DAYS,
    clear_hours,
    clearness,
    compare,
    decides,
    profile,
    worst_hour,
)

DAY = 86400
#: A Monday midnight UTC, so hour-of-day arithmetic is readable.
START = 1_750_464_000


def hour_row(day, hour, actual, live, shadow, clear=0.9, censored=0):
    return {
        "ts_utc": START + day * DAY + hour * 3600,
        "string_id": "s1",
        "actual_kwh": actual,
        "live_kwh": live,
        "shadow_kwh": shadow,
        "live_da_kwh": live,
        "shadow_da_kwh": shadow,
        "clearness": clear,
        "censored": censored,
    }


def trial(shape_live, shape_shadow, days=30, clear=0.9):
    """One archive: the same clear days, two branches, a stated hourly shape.

    Thirty days by default, because only the clearest third of them is
    selected and an hour of the day needs ``MIN_CLEAR_DAYS`` behind it before
    it says anything.
    """
    rows = []
    for day in range(days):
        for hour, (live_ratio, shadow_ratio) in shape_live.items():
            actual = 1.0
            rows.append(
                hour_row(
                    day,
                    hour,
                    actual,
                    actual * live_ratio,
                    actual * shape_shadow[hour],
                    clear=clear,
                )
            )
    return rows


class TestFlatIsNotRight:
    """The failing that broke the first criterion.

    It asked whether the hourly profile flattened. A branch that is 38 % low
    at every hour of the day has a perfectly flat profile -- and would have
    been declared the winner over a branch whose average was right.
    """

    def test_a_uniformly_wrong_branch_does_not_win(self):
        live = {8: (1.46, 0), 12: (1.0, 0), 16: (0.70, 0)}
        rows = trial(live, {8: 0.62, 12: 0.62, 16: 0.62}, days=45)
        result = compare(rows)

        # Flat, and therefore a spread of zero -- the old criterion's win.
        assert len(set(result["shadow_profile"].values())) == 1
        # Its worst hour is even *smaller* than the live branch's, so a rule
        # built on shape alone would still hand it the win.
        assert result["shadow_worst_hour"] < result["live_worst_hour"]
        # The level is what catches it: wrong all day, every day.
        assert result["shadow_level"] == pytest.approx(0.62, abs=0.01)
        verdict = decides(result)
        assert not verdict["decided"]
        assert any("level" in reason for reason in verdict["blocking"])

    def test_a_branch_that_really_is_closer_wins(self):
        live = {8: (1.46, 0), 12: (1.0, 0), 16: (0.70, 0)}
        # Forty-five days, so the clearest third clears the trial's bar.
        rows = trial(live, {8: 1.08, 12: 1.0, 16: 0.94}, days=45)
        result = compare(rows)
        assert result["live_worst_hour"] == pytest.approx(0.46, abs=0.01)
        assert result["shadow_worst_hour"] == pytest.approx(0.08, abs=0.01)
        assert result["improvement"] > 0.5
        assert decides(result)["decided"]


    def test_better_is_not_enough_on_its_own(self):
        """A little better is what a season's drift looks like.

        The live branch's own profile flattens on its own as the sun gets
        lower, so a bar of "beat it" would be met by a branch that changed
        nothing. Half the error has to go.
        """
        live = {8: (1.46, 0), 12: (1.0, 0), 16: (0.70, 0)}
        rows = trial(live, {8: 1.30, 12: 1.0, 16: 0.80}, days=45)
        result = compare(rows)
        assert result["shadow_worst_hour"] < result["live_worst_hour"]
        assert 0 < result["improvement"] < 0.5
        verdict = decides(result)
        assert not verdict["decided"]
        assert any("worst hour" in reason for reason in verdict["blocking"])


class TestClearHoursAreChosenIndependently:
    """Selecting on production conditions on the answer.

    Picking the brightest quarter of each hour-of-day picks the hours the
    forecast was most likely to under-predict, whatever the model -- a
    forecast that hits the expected value exactly appears 67 % low there.
    """

    def test_the_selector_never_looks_at_a_forecast(self):
        """Same skies, wildly different forecasts, identical selection."""
        rows_a = trial({12: (1.0, 0)}, {12: 1.0})
        rows_b = trial({12: (0.2, 0)}, {12: 3.0})
        for a, b in zip(rows_a, rows_b):
            a["clearness"] = b["clearness"] = 0.3 + 0.05 * (a["ts_utc"] % 7)
        chosen_a = {r["ts_utc"] for r in clear_hours(rows_a)}
        chosen_b = {r["ts_utc"] for r in clear_hours(rows_b)}
        assert chosen_a == chosen_b

    def test_rank_survives_a_sensor_that_reads_low(self):
        """The scale error is the subject, so a fixed threshold would move.

        A plant whose sensor reads a quarter low would fail a cut at 0.7 on
        every hour it ever saw. Rank sees the same order either way.
        """
        rows = trial({12: (1.0, 0)}, {12: 1.0}, days=9)
        for index, row in enumerate(rows):
            row["clearness"] = 0.4 + index * 0.05
        honest = {r["ts_utc"] for r in clear_hours(rows)}
        for row in rows:
            row["clearness"] *= 0.75
        low = {r["ts_utc"] for r in clear_hours(rows)}
        assert honest == low

    def test_it_ranks_inside_the_hour_of_day(self):
        """A clear November eight is darker than an overcast September one.

        Ranked across the whole archive, the selection would be a list of
        summer noons and the profile would have one hour in it.
        """
        rows = []
        for day in range(9):
            rows.append(hour_row(day, 8, 0.3, 0.3, 0.3, clear=0.30 + day * 0.01))
            rows.append(hour_row(day, 12, 1.0, 1.0, 1.0, clear=0.80 + day * 0.01))
        chosen = clear_hours(rows)
        assert {r["ts_utc"] % DAY // 3600 for r in chosen} == {8, 12}

    def test_a_curtailed_hour_is_never_clear(self):
        """It is a bright hour whose output was held back, which is the one
        thing a forecast cannot be blamed for."""
        rows = trial({12: (1.0, 0)}, {12: 1.0}, days=9)
        for row in rows:
            row["censored"] = 1
        assert clear_hours(rows) == []


class TestItRefusesThinEvidence:
    def test_an_hour_of_the_day_needs_days_behind_it(self):
        rows = trial({12: (1.3, 0)}, {12: 1.0}, days=MIN_CLEAR_DAYS)
        # The top third of five days is two -- below the bar.
        assert profile(clear_hours(rows), "live_da_kwh") == {}

    def test_dim_hours_are_left_out(self):
        """Dawn divides a small number by a small number."""
        rows = []
        for day in range(30):
            rows.append(hour_row(day, 5, 0.01, 0.05, 0.05))
        assert profile(clear_hours(rows), "live_da_kwh") == {}

    def test_only_hours_both_branches_reached_are_compared(self):
        """Otherwise one is judged on an easier set than the other."""
        rows = trial({8: (1.4, 0), 12: (1.1, 0)}, {8: 1.0, 12: 1.0}, days=30)
        for row in rows:
            if row["ts_utc"] % DAY // 3600 == 8:
                row["shadow_da_kwh"] = None
        result = compare(rows)
        assert result["hours_of_day"] == [12]
        assert "8" not in result["live_profile"]

    def test_nothing_at_all_is_reported_as_nothing(self):
        result = compare([])
        assert result["live_worst_hour"] is None
        assert result["improvement"] is None
        assert not decides(result)["decided"]

    def test_a_short_trial_decides_nothing_however_good_it_looks(self):
        """The bar is days, and it is not negotiable after the fact.

        Set by arithmetic: a six-to-eight week trial is 42 to 56 days, whose
        clearest third is 14 to 19. A bar above that would turn into "run it
        longer until it passes".
        """
        rows = trial({12: (1.5, 0)}, {12: 1.0}, days=3 * (MIN_TRIAL_DAYS - 3))
        verdict = decides(compare(rows))
        assert not verdict["decided"]
        assert any("clear days" in reason for reason in verdict["blocking"])


class TestTheArithmetic:
    def test_the_ratio_is_of_sums(self):
        """A dim hour with a wild quotient must not outvote a bright one."""
        rows = []
        for day in range(30):
            rows.append(hour_row(day, 12, 1.0, 0.9, 0.9))
            rows.append(hour_row(day, 12, 0.1, 0.03, 0.03))
        # Both land in the same hour of the day, so they are summed:
        # 0.93 / 1.1 = 0.845, where a mean of quotients would give 0.60.
        shape = profile(rows, "live_da_kwh")
        assert shape[12] == pytest.approx(0.93 / 1.1, abs=1e-3)

    def test_the_worst_hour_is_a_distance_not_a_spread(self):
        assert worst_hour({8: 1.2, 12: 1.2}) == pytest.approx(0.2)
        assert worst_hour({8: 0.8, 12: 1.2}) == pytest.approx(0.2)
        assert worst_hour({}) is None

    def test_clearness_refuses_the_dark(self):
        assert clearness(300.0, 600.0) == pytest.approx(0.5)
        assert clearness(1.0, 5.0) is None
        assert clearness(None, 600.0) is None

    def test_the_local_offset_moves_the_profile(self):
        """The profile is read by a person in their own time zone."""
        rows = trial({0: (1.5, 0)}, {0: 1.0}, days=30)
        assert list(profile(clear_hours(rows), "live_da_kwh")) == [0]
        shifted = profile(clear_hours(rows, 2 * 3600), "live_da_kwh", 2 * 3600)
        assert list(shifted) == [2]
