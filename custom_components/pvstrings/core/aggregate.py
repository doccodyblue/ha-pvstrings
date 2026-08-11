"""Turning an event stream into five-minute aggregates.

Home Assistant hands us irregular, event-driven power samples plus a watchdog
snapshot every ``watchdog_seconds``.  This module integrates those into fixed
five-minute buckets and -- just as importantly -- reports honestly how much of
each bucket was actually covered by data.

An hour is not the primary unit here: an hour can be twenty minutes free and
forty minutes curtailed, and on hourly means that is no longer separable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import INTERVAL_SECONDS
from .quality import VALUE_MEASURED


def interval_start(ts_utc: float, seconds: int = INTERVAL_SECONDS) -> int:
    """Floor a timestamp onto the interval grid."""
    return int(math.floor(ts_utc / seconds) * seconds)


def closed_interval(callback_ts: float, seconds: int = INTERVAL_SECONDS) -> int:
    """Start of the interval that has just ended when a flush fires.

    The flush is scheduled a moment *after* a boundary, so ``callback_ts``
    already sits inside the interval that is only beginning.  The one to
    persist starts a full interval earlier.

    Getting this off by one interval is silent and total: every window is then
    written with about a second of data, coverage collapses to 1/300, and every
    hour is discarded as unusable -- while the collector's own counters keep
    reporting healthy sample rates.
    """
    return int(math.floor((callback_ts - 1) / seconds) * seconds) - seconds


def interval_mid(ts_utc: int, seconds: int = INTERVAL_SECONDS) -> float:
    """Midpoint of the interval that starts at ``ts_utc``.

    Solar position must be evaluated here, not at the interval start -- naive
    assignment produces substantial transposition errors.
    """
    return ts_utc + seconds / 2.0


@dataclass(frozen=True, slots=True)
class Sample:
    """One observation of a numeric entity."""

    ts_utc: float
    value: float | None  # ``None`` means unavailable / unknown


@dataclass(frozen=True, slots=True)
class IntervalAggregate:
    """The persisted five-minute record for one string."""

    ts_utc: int
    string_id: str
    energy_wh: float | None
    power_mean_w: float | None
    coverage: float
    sample_count: int
    limit_commanded_w: float | None = None
    limit_binding: int | None = None
    value_kind: str = VALUE_MEASURED
    power_max_w: float | None = None


def _max_gap(watchdog_seconds: int) -> float:
    """Longest gap between two samples that still counts as covered.

    Two missed watchdog ticks plus slack.  Beyond that we do not know what the
    inverter did in between and must not pretend otherwise.
    """
    return max(60.0, watchdog_seconds * 2.5)


def integrate(
    samples: Sequence[Sample],
    start_ts: int,
    end_ts: int,
    watchdog_seconds: int,
) -> tuple[float | None, float | None, float, int, float | None]:
    """Trapezoidally integrate a power series over ``[start_ts, end_ts)``.

    ``samples`` must be sorted and may include one sample before ``start_ts``
    (the carry-in state) and one after ``end_ts``.

    Returns ``(energy_wh, power_mean_w, coverage, sample_count, power_max_w)``.
    Coverage is the fraction of the window spanned by usable sample pairs.
    """
    window = float(end_ts - start_ts)
    if window <= 0:
        raise ValueError("end_ts must be after start_ts")

    gap_limit = _max_gap(watchdog_seconds)
    energy_ws = 0.0
    covered = 0.0
    peak: float | None = None
    counted = 0

    for previous, current in zip(samples, samples[1:]):
        t0, v0 = previous.ts_utc, previous.value
        t1, v1 = current.ts_utc, current.value
        if v0 is None or v1 is None:
            continue
        span = t1 - t0
        if span <= 0 or span > gap_limit:
            continue
        lo = max(t0, float(start_ts))
        hi = min(t1, float(end_ts))
        if hi <= lo:
            continue
        # Linear interpolation onto the part of the segment inside the window.
        a = v0 + (v1 - v0) * ((lo - t0) / span)
        b = v0 + (v1 - v0) * ((hi - t0) / span)
        energy_ws += (a + b) / 2.0 * (hi - lo)
        covered += hi - lo
        peak = max(a, b) if peak is None else max(peak, a, b)

    for sample in samples:
        if start_ts <= sample.ts_utc < end_ts and sample.value is not None:
            counted += 1
            peak = sample.value if peak is None else max(peak, sample.value)

    coverage = min(1.0, covered / window)
    if covered <= 0.0:
        return None, None, 0.0, counted, None

    energy_wh = energy_ws / 3600.0
    power_mean = energy_ws / covered
    return energy_wh, power_mean, coverage, counted, peak


def mean_of(samples: Sequence[Sample], start_ts: int, end_ts: int) -> float | None:
    """Simple time-unweighted mean of the valid samples inside the window."""
    values = [
        s.value
        for s in samples
        if s.value is not None and start_ts <= s.ts_utc < end_ts
    ]
    if not values:
        return None
    return sum(values) / len(values)


def last_of(samples: Sequence[Sample], start_ts: int, end_ts: int) -> float | None:
    """Last valid value at or before ``end_ts`` (carry-in aware)."""
    latest: float | None = None
    for sample in samples:
        if sample.ts_utc >= end_ts:
            break
        if sample.value is not None:
            latest = sample.value
    return latest


@dataclass(slots=True)
class SampleBuffer:
    """Bounded, sorted sample store for one entity.

    The collector keeps one of these per tracked entity.  Raw seconds are never
    persisted; the buffer only has to survive long enough to close the current
    interval, so it is trimmed aggressively.
    """

    watchdog_seconds: int = 30
    samples: list[Sample] = field(default_factory=list)

    def add(self, ts_utc: float, value: float | None) -> None:
        if self.samples and ts_utc < self.samples[-1].ts_utc:
            # Out-of-order arrival: insert rather than corrupt the ordering.
            index = len(self.samples)
            while index > 0 and self.samples[index - 1].ts_utc > ts_utc:
                index -= 1
            self.samples.insert(index, Sample(ts_utc, value))
            return
        self.samples.append(Sample(ts_utc, value))

    def window(self, start_ts: int, end_ts: int) -> list[Sample]:
        """Samples covering the window, including one carry-in and one carry-out."""
        out: list[Sample] = []
        carry_in: Sample | None = None
        for sample in self.samples:
            if sample.ts_utc < start_ts:
                carry_in = sample
                continue
            if sample.ts_utc > end_ts:
                out.append(sample)
                break
            out.append(sample)
        if carry_in is not None:
            out.insert(0, carry_in)
        return out

    def trim(self, before_ts: float) -> None:
        """Drop everything older than ``before_ts``, keeping one carry-in sample."""
        keep_from = 0
        for index, sample in enumerate(self.samples):
            if sample.ts_utc >= before_ts:
                keep_from = max(0, index - 1)
                break
        else:
            keep_from = max(0, len(self.samples) - 1)
        if keep_from:
            del self.samples[:keep_from]

    @property
    def last_value(self) -> float | None:
        for sample in reversed(self.samples):
            if sample.value is not None:
                return sample.value
        return None


def hourly_from_5min(
    rows: Iterable[tuple[int, float | None, float, str, int | None, float | None]],
) -> dict[str, float]:
    """Fold five-minute rows of one string and one hour into hourly figures.

    ``rows`` are ``(ts_utc, energy_wh, coverage, value_kind, limit_binding,
    limit_commanded_w)``.  Hourly values are always derived, never measured
    separately, so that the two can never drift apart.
    """
    energy_wh = 0.0
    coverage_sum = 0.0
    binding_intervals = 0
    known_binding = 0
    limits: list[float] = []
    kinds: set[str] = set()
    seen = 0

    for _ts, wh, coverage, value_kind, binding, limit in rows:
        seen += 1
        coverage_sum += coverage
        kinds.add(value_kind)
        if wh is not None:
            energy_wh += wh
        if binding is not None:
            known_binding += 1
            binding_intervals += int(bool(binding))
        if limit is not None:
            limits.append(limit)

    expected = 3600 // INTERVAL_SECONDS
    coverage = coverage_sum / expected if expected else 0.0
    curtailed_fraction = (
        binding_intervals / known_binding if known_binding else 0.0
    )

    # The weakest kind wins: one censored interval makes the hour censored.
    if VALUE_MEASURED in kinds and len(kinds) == 1:
        value_kind = VALUE_MEASURED
    elif "lower_bound" in kinds:
        value_kind = "lower_bound"
    elif "reconstructed" in kinds:
        value_kind = "reconstructed"
    else:
        value_kind = VALUE_MEASURED

    return {
        "energy_kwh": energy_wh / 1000.0,
        "coverage": min(1.0, coverage),
        "curtailed_fraction": curtailed_fraction,
        "limit_min_w": min(limits) if limits else None,
        "limit_max_w": max(limits) if limits else None,
        "limit_mean_w": sum(limits) / len(limits) if limits else None,
        "value_kind": value_kind,
        "intervals": seen,
    }
