"""Five-minute aggregation: the part that decides what "missing" means."""

from __future__ import annotations

import pytest

from core.aggregate import (
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
