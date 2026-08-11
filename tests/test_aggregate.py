"""Five-minute aggregation: the part that decides what "missing" means."""

from __future__ import annotations

import pytest

from core.aggregate import (
    closed_interval,
    Sample,
    SampleBuffer,
    hourly_from_5min,
    integrate,
    interval_mid,
    interval_start,
)


def test_interval_grid():
    assert interval_start(1_700_000_123) == 1_700_000_100
    assert interval_start(1_700_000_100) == 1_700_000_100
    assert interval_mid(1_700_000_100) == 1_700_000_250.0


def test_constant_power_integrates_exactly():
    start = 1_700_000_100
    samples = [Sample(start + offset, 600.0) for offset in range(0, 301, 30)]
    energy, mean, coverage, count, peak = integrate(samples, start, start + 300, 30)
    assert energy == pytest.approx(50.0)  # 600 W over 5 min
    assert mean == pytest.approx(600.0)
    assert coverage == pytest.approx(1.0)
    assert count == 10
    assert peak == pytest.approx(600.0)


def test_ramp_uses_trapezoid_not_last_value():
    start = 1_700_000_100
    samples = [Sample(start, 0.0), Sample(start + 300, 600.0)]
    energy, mean, coverage, _count, _peak = integrate(samples, start, start + 300, 300)
    assert energy == pytest.approx(25.0)
    assert mean == pytest.approx(300.0)
    assert coverage == pytest.approx(1.0)


def test_unavailable_breaks_coverage_instead_of_counting_as_zero():
    """An unavailable inverter is not a zero -- it is an unknown.

    Treating it as zero is what turns a midday dropout into a learned null.
    """
    start = 1_700_000_100
    samples = [
        Sample(start, 600.0),
        Sample(start + 150, None),
        Sample(start + 300, 600.0),
    ]
    energy, _mean, coverage, _count, _peak = integrate(samples, start, start + 300, 30)
    assert energy is None
    assert coverage == 0.0


def test_partial_window_reports_partial_coverage():
    start = 1_700_000_100
    samples = [Sample(start + offset, 600.0) for offset in range(0, 151, 30)]
    energy, mean, coverage, _count, _peak = integrate(samples, start, start + 300, 30)
    assert coverage == pytest.approx(0.5)
    assert energy == pytest.approx(25.0)
    assert mean == pytest.approx(600.0)


def test_long_gap_is_not_bridged():
    """Beyond two watchdog periods we do not know what happened in between."""
    start = 1_700_000_100
    samples = [Sample(start, 600.0), Sample(start + 300, 600.0)]
    _energy, _mean, coverage, _count, _peak = integrate(
        samples, start, start + 300, 30
    )
    assert coverage == 0.0


def test_carry_in_sample_before_window_counts():
    start = 1_700_000_100
    samples = [
        Sample(start - 20, 600.0),
        *[Sample(start + offset, 600.0) for offset in range(0, 301, 30)],
    ]
    _energy, _mean, coverage, _count, _peak = integrate(samples, start, start + 300, 30)
    assert coverage == pytest.approx(1.0)


def test_buffer_orders_late_arrivals():
    buffer = SampleBuffer(watchdog_seconds=30)
    buffer.add(100.0, 1.0)
    buffer.add(300.0, 3.0)
    buffer.add(200.0, 2.0)
    assert [s.ts_utc for s in buffer.samples] == [100.0, 200.0, 300.0]


def test_buffer_trim_keeps_one_carry_in():
    buffer = SampleBuffer(watchdog_seconds=30)
    for ts in range(0, 1000, 100):
        buffer.add(float(ts), 1.0)
    buffer.trim(500.0)
    assert buffer.samples[0].ts_utc == 400.0


def test_hourly_fold_weakest_kind_wins():
    rows = [
        (0, 100.0, 1.0, "measured", 0, None),
        (300, 100.0, 1.0, "lower_bound", 1, 1500.0),
        *[(ts, 100.0, 1.0, "measured", 0, None) for ts in range(600, 3600, 300)],
    ]
    folded = hourly_from_5min(rows)
    assert folded["energy_kwh"] == pytest.approx(1.2)
    assert folded["value_kind"] == "lower_bound"
    assert folded["curtailed_fraction"] == pytest.approx(1 / 12)
    assert folded["limit_mean_w"] == pytest.approx(1500.0)


def test_hourly_fold_partial_hour_reports_partial_coverage():
    rows = [(ts, 50.0, 1.0, "measured", None, None) for ts in range(0, 1800, 300)]
    folded = hourly_from_5min(rows)
    assert folded["coverage"] == pytest.approx(0.5)
    assert folded["intervals"] == 6


class TestClosedInterval:
    """Which window a flush callback is responsible for.

    Off by one interval here is silent and total: every window gets written
    with about a second of data, coverage collapses to 1/300, and every hour is
    then discarded as unusable -- while the collector's counters keep reporting
    healthy sample rates.
    """

    BOUNDARY = 1_700_000_100  # on the five-minute grid

    def test_the_interval_that_just_ended_is_returned(self):
        # The flush is scheduled one second past the boundary.
        assert closed_interval(self.BOUNDARY + 1) == self.BOUNDARY - 300

    def test_not_the_one_that_is_starting(self):
        assert closed_interval(self.BOUNDARY + 1) != self.BOUNDARY

    def test_a_slightly_late_callback_still_closes_the_same_window(self):
        for delay in (1, 5, 30, 120, 299):
            assert closed_interval(self.BOUNDARY + delay) == self.BOUNDARY - 300, delay

    def test_a_callback_a_full_interval_late_moves_on(self):
        assert closed_interval(self.BOUNDARY + 301) == self.BOUNDARY

    def test_exactly_on_the_boundary_closes_the_previous_window(self):
        assert closed_interval(self.BOUNDARY) == self.BOUNDARY - 600

    def test_result_is_always_on_the_grid(self):
        for offset in range(0, 900, 37):
            assert closed_interval(self.BOUNDARY + offset) % 300 == 0


class TestFlushMarkerAhead:
    """A clock stepped backwards must not silence the collector.

    Hardware without an RTC boots with a wrong time and NTP corrects it later.
    If the recorded flush position is then ahead of the scheduled boundary, a
    catch-up loop that only walks forwards writes nothing at all -- with no
    error, no exception and healthy-looking sample counters.
    """

    BOUNDARY = 1_700_000_100

    def _range(self, last_flush_ts, callback_ts, retention=600):
        last_closed = closed_interval(callback_ts)
        first = last_closed if last_flush_ts is None else last_flush_ts + 300
        first = max(first, last_closed - retention + 300)
        if first > last_closed:
            first = last_closed
        return [b for b in range(first, last_closed + 1, 300)]

    def test_normal_progress_writes_one_window(self):
        assert self._range(self.BOUNDARY - 600, self.BOUNDARY + 1) == [self.BOUNDARY - 300]

    def test_a_marker_from_the_future_still_writes(self):
        windows = self._range(self.BOUNDARY + 1800, self.BOUNDARY + 1)
        assert windows == [self.BOUNDARY - 300], "must not fall silent"

    def test_a_gap_is_caught_up_within_the_buffer(self):
        windows = self._range(self.BOUNDARY - 3600, self.BOUNDARY + 1)
        assert windows == [self.BOUNDARY - 600, self.BOUNDARY - 300]

    def test_first_run_writes_exactly_one_window(self):
        assert self._range(None, self.BOUNDARY + 1) == [self.BOUNDARY - 300]
