"""Data quality classification.

The point of this module is a single rule: a missing measurement is only a zero
if the sun is down.  Writing ``float(0)`` for an unavailable entity turns a
midday inverter dropout into a learned zero, and the learning layer has no way
to ever recover from that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Below this solar elevation an inverter that reports nothing is simply asleep.
NIGHT_ELEVATION_DEG: Final = 3.0

#: Coverage thresholds for the interval quality classes.
EXACT_COVERAGE: Final = 0.95
PARTIAL_COVERAGE: Final = 0.80

QUALITY_EXACT: Final = "exact"
QUALITY_PARTIAL: Final = "partial"
QUALITY_MISSING: Final = "missing"
QUALITY_NIGHT: Final = "night"

VALUE_MEASURED: Final = "measured"
VALUE_LOWER_BOUND: Final = "lower_bound"
VALUE_RECONSTRUCTED: Final = "reconstructed"


def classify_availability(missing: bool, sun_elevation_deg: float) -> str:
    """Distinguish "no data" from "night"."""
    if not missing:
        return "ok"
    return QUALITY_NIGHT if sun_elevation_deg < NIGHT_ELEVATION_DEG else QUALITY_MISSING


def classify(coverage: float, sun_elevation_deg: float) -> str:
    """Map interval coverage plus sun position onto a quality class.

    Darkness is decided before coverage, and that order is the whole point.
    An inverter that stays awake and reports a steady zero all night covers
    its hour perfectly; asking about coverage first labelled those hours
    ``exact`` -- which is to say daylight -- while a neighbouring string that
    simply went unavailable was correctly called night.  Everything reads that
    label afterwards: learning counted the dark zeros as an anomaly (physics
    at zero with the sun supposedly up), the health check took the anomaly for
    a stalled learner and said so once every night, and scoring averaged those
    errorless hours into ``nmae`` and ``mae_kwh``.
    """
    if sun_elevation_deg < NIGHT_ELEVATION_DEG:
        return QUALITY_NIGHT
    if coverage >= EXACT_COVERAGE:
        return QUALITY_EXACT
    if coverage >= PARTIAL_COVERAGE:
        return QUALITY_PARTIAL
    return QUALITY_MISSING


def learn_weight(quality: str, coverage: float) -> float:
    """How much an observation of this quality may move the model."""
    if quality == QUALITY_EXACT:
        return 1.0
    if quality == QUALITY_PARTIAL:
        return max(0.0, min(1.0, coverage))
    if quality == QUALITY_NIGHT:
        # A dark hour is a genuine zero and worth learning, but it carries no
        # information about the correction factors, so the forecast layer skips
        # it anyway.  Keeping the weight here makes the intent explicit.
        return 1.0
    return 0.0


def value_kind_weight(value_kind: str, base_weight: float) -> float:
    """Down-weight censored and reconstructed observations."""
    if value_kind == VALUE_LOWER_BOUND:
        return base_weight * 0.5
    if value_kind == VALUE_RECONSTRUCTED:
        return base_weight * 0.35
    return base_weight


@dataclass(frozen=True, slots=True)
class IntervalQuality:
    quality: str
    weight: float
    usable_for_learning: bool

    @property
    def is_night(self) -> bool:
        return self.quality == QUALITY_NIGHT


def assess(
    coverage: float,
    sun_elevation_deg: float,
    value_kind: str = VALUE_MEASURED,
) -> IntervalQuality:
    """Full assessment of one interval."""
    quality = classify(coverage, sun_elevation_deg)
    weight = value_kind_weight(value_kind, learn_weight(quality, coverage))
    usable = quality in (QUALITY_EXACT, QUALITY_PARTIAL) and weight > 0.0
    return IntervalQuality(quality=quality, weight=weight, usable_for_learning=usable)
