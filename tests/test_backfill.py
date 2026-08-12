"""Reconstructing shading observations from hourly history.

The failure this module is most exposed to is silence.  Every input is a
mapping keyed by an epoch hour, and if two of those keys disagree about their
unit or their offset the join simply produces nothing -- no exception, no
warning, an empty result that looks exactly like "this plant has no history".
That happened once already, against a real installation, and the timestamp
tests below exist because of it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from core.backfill import (
    BACKFILL_WEIGHT,
    MIDPOINT_OFFSET_S,
    hourly_series,
    shading_rows_from_history,
)
from core.config import GeometrySegment
from core.physics import PhysicsEngine, to_index

#: 2025-06-21 in Europe/Berlin, a day with plenty of sun to work with.
DAY = 1_750_464_000  # 2025-06-21 00:00 UTC
HOUR = 3600


@pytest.fixture
def physics() -> PhysicsEngine:
    return PhysicsEngine(
        latitude=53.5,
        longitude=10.0,
        elevation_m=5.0,
        albedo=0.2,
        transposition_model="perez-driesse",
        time_zone="Europe/Berlin",
    )


def south_segment(kwp: float = 2.0) -> GeometrySegment:
    return GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=kwp)


def bright_day(strength: float = 1.0) -> dict[int, tuple[float, float, float]]:
    """A plausible clear midsummer day, hour by hour."""
    shape = {
        4: 40, 5: 120, 6: 240, 7: 380, 8: 520, 9: 640, 10: 730,
        11: 790, 12: 800, 13: 770, 14: 700, 15: 590, 16: 450,
        17: 300, 18: 160, 19: 60,
    }
    return {
        DAY + hour * HOUR: (
            ghi * strength,
            ghi * strength * 0.9,
            ghi * strength * 0.25,
        )
        for hour, ghi in shape.items()
    }


class TestHourlySeries:
    """Whichever door the recorder statistics came through, the hour must land.

    The internal recorder API hands out epoch seconds, the WebSocket API
    multiplies by a thousand for the frontend, and older releases returned a
    datetime.  Reading milliseconds as seconds puts every observation in the
    year 58000, where it matches no irradiance row at all.
    """

    def test_epoch_seconds(self):
        series = hourly_series([{"start": float(DAY), "mean": 500.0}])
        assert series == {DAY: 500.0}

    def test_epoch_milliseconds(self):
        series = hourly_series([{"start": float(DAY) * 1000.0, "mean": 500.0}])
        assert series == {DAY: 500.0}

    def test_datetime(self):
        stamp = datetime.fromtimestamp(DAY, timezone.utc)
        assert hourly_series([{"start": stamp, "mean": 500.0}]) == {DAY: 500.0}

    def test_all_three_agree(self):
        stamp = datetime.fromtimestamp(DAY, timezone.utc)
        forms = [
            {"start": float(DAY), "mean": 1.0},
            {"start": float(DAY) * 1000.0, "mean": 1.0},
            {"start": stamp, "mean": 1.0},
        ]
        assert {tuple(hourly_series([row]))[0] for row in forms} == {DAY}

    def test_a_row_without_a_mean_is_dropped_not_zeroed(self):
        """An hour the recorder cannot summarise is unknown, not dark."""
        assert hourly_series([{"start": float(DAY), "mean": None}]) == {}

    def test_a_row_without_a_start_is_dropped(self):
        assert hourly_series([{"mean": 5.0}]) == {}

    def test_a_ragged_start_is_floored_to_the_hour(self):
        series = hourly_series([{"start": float(DAY + 1799), "mean": 7.0}])
        assert series == {DAY: 7.0}

    def test_empty_input(self):
        assert hourly_series([]) == {}


class TestReconstruction:
    def _run(self, physics, power, irradiance=None, kwp=2.0, geometry=None):
        return shading_rows_from_history(
            physics=physics,
            power_by_string={"s1": power},
            irradiance=irradiance if irradiance is not None else bright_day(),
            geometry_at=geometry or (lambda _string, _hour: south_segment(kwp)),
        )

    def _unshaded(self, physics, kwp=2.0) -> dict[int, float]:
        """Power exactly matching the physics, so every ratio is one.

        Computed directly rather than inferred from a probe run: a probe fed a
        token 1 W now produces ratios below the unit-mismatch floor and gets
        thrown out, which would leave every test in this class silently
        running on no data at all.
        """
        hours = sorted(bright_day())
        index = to_index([hour + MIDPOINT_OFFSET_S for hour in hours])
        irradiance = bright_day()
        result = physics.run(
            index,
            south_segment(kwp),
            ghi=pd.Series([irradiance[hour][0] for hour in hours], index=index),
            dni=pd.Series([irradiance[hour][1] for hour in hours], index=index),
            dhi=pd.Series([irradiance[hour][2] for hour in hours], index=index),
            temp_air=15.0,
            wind_speed=1.5,
            system_efficiency=0.96,
            mount_type="open_rack",
        )
        return dict(zip(hours, result.dc_power_w.to_numpy()))

    def test_a_matching_string_yields_ratios_of_one(self, physics):
        result = self._run(physics, self._unshaded(physics))
        assert result.rows
        for row in result.rows:
            assert row[4] == pytest.approx(1.0, rel=1e-6)

    def test_half_power_yields_ratios_of_a_half(self, physics):
        halved = {hour: value / 2 for hour, value in self._unshaded(physics).items()}
        result = self._run(physics, halved)
        for row in result.rows:
            assert row[4] == pytest.approx(0.5, rel=1e-6)

    def test_rows_carry_the_backfill_weight(self, physics):
        result = self._run(physics, self._unshaded(physics))
        assert {row[5] for row in result.rows} == {BACKFILL_WEIGHT}

    def test_rows_are_stamped_at_the_middle_of_the_hour(self, physics):
        """The mean over an hour belongs to the mean sun position in it."""
        result = self._run(physics, self._unshaded(physics))
        for row in result.rows:
            assert (row[0] - MIDPOINT_OFFSET_S) % HOUR == 0

    def test_no_row_can_land_on_a_five_minute_boundary(self, physics):
        """``shading_obs`` upserts on (ts_utc, string_id).

        The bare hour midpoint is a multiple of 300 and therefore a valid live
        interval start, so a backfill run would overwrite one real measurement
        in twelve -- destroying the best evidence in the map, silently, and
        only on the first run.
        """
        result = self._run(physics, self._unshaded(physics))
        assert result.rows
        for row in result.rows:
            assert row[0] % 300 != 0

    def test_the_sun_is_where_it_should_be_at_local_noon(self, physics):
        result = self._run(physics, self._unshaded(physics))
        noon = [row for row in result.rows if row[0] == DAY + 11 * HOUR + MIDPOINT_OFFSET_S]
        assert noon, "the 11:00 UTC hour should have produced an observation"
        azimuth, elevation = noon[0][2], noon[0][3]
        assert 170 < azimuth < 200  # close to due south
        assert 50 < elevation < 62  # midsummer at 53.5 N

    def test_low_sun_is_excluded(self, physics):
        result = self._run(physics, self._unshaded(physics))
        assert all(row[3] >= 8.0 for row in result.rows)

    def test_hours_with_negligible_physics_are_excluded(self, physics):
        dim = {hour: (2.0, 1.0, 1.0) for hour in bright_day()}
        result = self._run(physics, {hour: 1.0 for hour in dim}, irradiance=dim)
        assert result.rows == []

    def test_an_impossible_ratio_is_dropped(self, physics):
        wild = {hour: value * 100 for hour, value in self._unshaded(physics).items()}
        assert self._run(physics, wild).rows == []

    def test_a_kilowatt_sensor_is_rejected_not_absorbed(self, physics):
        """An inverter reporting kW against physics in W lands near 0.001.

        Accepting that would not look like an error anywhere: the service
        reports thousands of observations and the map quietly shuts every sky
        cell the live collector has not yet reached.
        """
        kilowatts = {
            hour: value / 1000.0 for hour, value in self._unshaded(physics).items()
        }
        assert self._run(physics, kilowatts).rows == []

    def test_zero_production_is_kept_out_as_a_ratio_of_zero(self, physics):
        """A string that made nothing is not a shading observation of zero.

        Total darkness under a bright sky is an outage or a disconnected
        sensor, and a cell full of zeros would clamp that patch of sky shut
        for good.
        """
        result = self._run(physics, {hour: 0.0 for hour in bright_day()})
        assert result.rows == []

    def test_negative_power_is_dropped(self, physics):
        result = self._run(physics, {hour: -50.0 for hour in bright_day()})
        assert result.rows == []

    def test_hours_without_power_are_skipped(self, physics):
        power = self._unshaded(physics)
        trimmed = {hour: value for hour, value in power.items() if hour < DAY + 12 * HOUR}
        result = self._run(physics, trimmed)
        assert result.rows
        assert all(row[0] < DAY + 12 * HOUR for row in result.rows)

    def test_no_geometry_means_no_rows(self, physics):
        result = self._run(
            physics, self._unshaded(physics), geometry=lambda _s, _h: None
        )
        assert result.rows == []

    def test_a_mount_moved_mid_history_is_honoured(self, physics):
        """Two tilts in one window must not be smeared into one wrong average."""
        switch = DAY + 12 * HOUR

        def geometry(_string_id, hour):
            return (
                GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=2.0)
                if hour < switch
                else GeometrySegment(switch, azimuth_deg=180, tilt_deg=70, kwp=2.0)
            )

        flat = self._run(physics, self._unshaded(physics))
        moved = self._run(physics, self._unshaded(physics), geometry=geometry)
        before = {row[0]: row[4] for row in moved.rows if row[0] < switch}
        after = {row[0]: row[4] for row in moved.rows if row[0] > switch}
        baseline = {row[0]: row[4] for row in flat.rows}
        assert before and after
        # Hours before the move are untouched...
        assert all(before[ts] == pytest.approx(baseline[ts]) for ts in before)
        # ...and every hour after it is computed against the new tilt.  Not
        # all in the same direction: a steep plane collects far less at
        # midsummer noon but more once the sun is nearly on the horizon, which
        # is exactly why the two halves may not be averaged together.
        assert all(abs(after[ts] / baseline[ts] - 1.0) > 0.1 for ts in after)
        assert max(after.values()) > 1.2  # the midday hours lose most

    def test_counts_are_reported(self, physics):
        result = self._run(physics, self._unshaded(physics))
        summary = result.as_dict()
        assert summary["observations"] == len(result.rows)
        assert summary["per_string"]["s1"] == len(result.rows)
        assert 0 < summary["hours_used"] <= summary["hours_considered"]

    def test_no_irradiance_is_not_a_crash(self, physics):
        result = shading_rows_from_history(
            physics=physics,
            power_by_string={"s1": {DAY: 100.0}},
            irradiance={},
            geometry_at=lambda _s, _h: south_segment(),
        )
        assert result.rows == []
        assert result.hours_considered == 0

    def test_no_strings_is_not_a_crash(self, physics):
        result = shading_rows_from_history(
            physics=physics,
            power_by_string={},
            irradiance=bright_day(),
            geometry_at=lambda _s, _h: south_segment(),
        )
        assert result.rows == []

    def test_strings_are_kept_apart(self, physics):
        power = self._unshaded(physics)
        result = shading_rows_from_history(
            physics=physics,
            power_by_string={
                "bright": power,
                "dim": {hour: value * 0.4 for hour, value in power.items()},
            },
            irradiance=bright_day(),
            geometry_at=lambda _s, _h: south_segment(),
        )
        bright = [row[4] for row in result.rows if row[1] == "bright"]
        dim = [row[4] for row in result.rows if row[1] == "dim"]
        assert bright and dim
        assert min(bright) > max(dim)
