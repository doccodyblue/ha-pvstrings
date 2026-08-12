"""Hierarchical log-ratio correction model.

What is learned here is **not** cloud attenuation -- that already sits in the
weather forecast and in pvlib.  It is the residual error of an already
cloud-aware physical forecast:

    log(actual / physics) = plant_effect[weather_class x daypart]
                          + string_offset[string_id]
                          ( + string_daypart[string, daypart] )

Weather forecast errors act plant-wide; mounting, nameplate and shading errors
act per string.  Splitting them that way shares information between strings
instead of estimating a dozen thin buckets independently.  There is deliberately
no string x weather-class interaction in v1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .quality import VALUE_LOWER_BOUND, VALUE_MEASURED, VALUE_RECONSTRUCTED

# --------------------------------------------------------------------------- #
# buckets
# --------------------------------------------------------------------------- #

WEATHER_CLASSES = ("clear", "partly_cloudy", "overcast", "rain")
DAYPARTS = ("morning", "midday", "afternoon")

SCOPE_PLANT = "plant"
SCOPE_STRING = "string"
SCOPE_STRING_DAYPART = "string_daypart"

#: Half-life of the rolling mean, counted in effective observations.
HALFLIFE = 15.0
ALPHA = 1.0 - 0.5 ** (1.0 / HALFLIFE)

#: Shrinkage constant.  ``k`` should eventually be estimated empirically as
#: Var(within bucket) / Var(between buckets); until there is a year of data,
#: ten observations to reach half strength is a defensible prior.
SHRINK_K = 10.0

#: A per-string x daypart effect only switches on once its bucket is populated.
STRING_DAYPART_MIN_N = 25.0

#: Hard clamp on any single correction, in the log domain.  exp(0.7) ~ 2.0.
MAX_LOG_EFFECT = 0.7

#: Ratios outside this band are not model error, they are broken data.
MIN_RATIO = 0.05
MAX_RATIO = 5.0

HORIZON_BUCKETS = ("0-6h", "6-24h", "24-48h", "48h+")


def horizon_bucket(horizon_h: float) -> str:
    if horizon_h < 6:
        return "0-6h"
    if horizon_h < 24:
        return "6-24h"
    if horizon_h < 48:
        return "24-48h"
    return "48h+"


def daypart(ts_utc: float, solar_noon_ts_utc: float) -> str:
    """Daypart relative to the sun, not to the clock.

    Clock-based dayparts drift by an hour twice a year and by up to half an
    hour across a time zone; solar noon does not.
    """
    delta_h = (ts_utc - solar_noon_ts_utc) / 3600.0
    if delta_h < -2.0:
        return "morning"
    if delta_h <= 2.0:
        return "midday"
    return "afternoon"


def weather_class(
    clearsky_index: float | None = None,
    clouds_pct: float | None = None,
    rain_mm: float | None = None,
) -> str:
    """Classify an interval.

    The clear-sky index (measured or forecast GHI over clear-sky GHI) is the
    better signal when it is available; cloud cover percentage is the fallback.
    """
    if rain_mm is not None and rain_mm >= 0.2:
        return "rain"
    if clearsky_index is not None:
        if clearsky_index >= 0.75:
            return "clear"
        if clearsky_index >= 0.40:
            return "partly_cloudy"
        return "overcast"
    if clouds_pct is not None:
        if clouds_pct < 25.0:
            return "clear"
        if clouds_pct < 75.0:
            return "partly_cloudy"
        return "overcast"
    return "partly_cloudy"


def plant_key(weather: str, part: str) -> str:
    return f"{weather}|{part}"


# --------------------------------------------------------------------------- #
# effect state
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Effect:
    """One bucket of the model: a value in log space plus its effective count.

    ``value`` is the raw rolling mean.  ``shrunk`` is what the forecast uses --
    pulled towards zero (neutral) while the bucket is still thin.  Both decay
    with the same history, which is the point: an EMA with a fixed alpha next to
    an unbounded weight sum counts two different histories and the shrinkage
    quietly stops doing anything.
    """

    value: float = 0.0
    n_eff: float = 0.0

    @property
    def shrunk(self) -> float:
        if self.n_eff <= 0.0:
            return 0.0
        factor = self.n_eff / (self.n_eff + SHRINK_K)
        return _clamp(self.value * factor, -MAX_LOG_EFFECT, MAX_LOG_EFFECT)

    def update(self, observation: float, weight: float) -> None:
        if weight <= 0.0:
            return
        alpha_eff = min(1.0, ALPHA * weight)
        self.n_eff = (1.0 - ALPHA) * self.n_eff + weight
        self.value = (1.0 - alpha_eff) * self.value + alpha_eff * observation

    def as_tuple(self) -> tuple[float, float]:
        return self.value, self.n_eff


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class Observation:
    """One hour of one string, ready to be learned from."""

    string_id: str
    weather: str
    part: str
    measured_kwh: float
    physics_kwh: float
    weight: float
    value_kind: str = VALUE_MEASURED

    @property
    def log_ratio(self) -> float:
        return math.log(self.measured_kwh / self.physics_kwh)


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


@dataclass
class LogRatioModel:
    plant: dict[str, Effect] = field(default_factory=dict)
    string: dict[str, Effect] = field(default_factory=dict)
    string_daypart: dict[str, Effect] = field(default_factory=dict)

    # -- persistence ------------------------------------------------------- #

    @classmethod
    def from_rows(
        cls,
        plant: Mapping[str, tuple[float, float]],
        string: Mapping[str, tuple[float, float]],
        string_daypart: Mapping[str, tuple[float, float]] | None = None,
    ) -> "LogRatioModel":
        def build(rows: Mapping[str, tuple[float, float]]) -> dict[str, Effect]:
            return {k: Effect(value=v, n_eff=n) for k, (v, n) in rows.items()}

        return cls(
            plant=build(plant),
            string=build(string),
            string_daypart=build(string_daypart or {}),
        )

    def to_rows(self, scope: str) -> dict[str, tuple[float, float]]:
        source = {
            SCOPE_PLANT: self.plant,
            SCOPE_STRING: self.string,
            SCOPE_STRING_DAYPART: self.string_daypart,
        }[scope]
        return {key: effect.as_tuple() for key, effect in source.items()}

    # -- prediction -------------------------------------------------------- #

    def log_correction(self, string_id: str, weather: str, part: str) -> float:
        total = 0.0
        plant = self.plant.get(plant_key(weather, part))
        if plant is not None:
            total += plant.shrunk
        offset = self.string.get(string_id)
        if offset is not None:
            total += offset.shrunk
        interaction = self.string_daypart.get(f"{string_id}|{part}")
        if interaction is not None and interaction.n_eff >= STRING_DAYPART_MIN_N:
            total += interaction.shrunk
        return _clamp(total, -MAX_LOG_EFFECT * 2, MAX_LOG_EFFECT * 2)

    def factor(self, string_id: str, weather: str, part: str) -> float:
        """Multiplicative correction applied to the physical potential."""
        return math.exp(self.log_correction(string_id, weather, part))

    def apply(
        self, physics_kwh: float, string_id: str, weather: str, part: str
    ) -> float:
        return physics_kwh * self.factor(string_id, weather, part)

    # -- learning ---------------------------------------------------------- #

    def decline_reason(self, obs: Observation) -> str | None:
        """Why this observation cannot be learned from, or ``None`` if it can.

        Split out from :meth:`observe` because "not used" covers five quite
        different situations, and a caller that only sees a boolean can report
        no more than a shrug.  On a plant where four strings in five are being
        dropped every hour, that difference is the whole diagnosis.
        """
        if obs.weight <= 0.0:
            return "no_weight"
        if obs.physics_kwh <= 0.0:
            return "no_physics"
        if obs.measured_kwh <= 0.0:
            return "no_production"
        ratio = obs.measured_kwh / obs.physics_kwh
        if not MIN_RATIO <= ratio <= MAX_RATIO:
            return "ratio_out_of_range"
        if (
            obs.value_kind == VALUE_LOWER_BOUND
            and obs.physics_kwh >= obs.measured_kwh
        ):
            # Physics already predicts at least what we saw through the limit
            # -- consistent, nothing to learn.
            return "censored_and_consistent"
        return None

    def observe(self, obs: Observation) -> bool:
        """Fold one observation into the model.

        Returns whether the observation was used.  Censored hours may only ever
        push the model *up*: an inverter sitting at its limit tells us the true
        potential was at least this high, never that it was this low.  A normal
        update there would build a systematic downward bias into every sunny
        midday.
        """
        if self.decline_reason(obs) is not None:
            return False
        ratio = obs.measured_kwh / obs.physics_kwh

        weight = obs.weight
        if obs.value_kind == VALUE_LOWER_BOUND:
            weight *= 0.5
        elif obs.value_kind == VALUE_RECONSTRUCTED:
            weight *= 0.35

        residual = math.log(ratio)

        # Attribute to the plant level first, then let the string level absorb
        # only what the plant level did not explain.  This is a single Gauss-
        # Seidel sweep of the hierarchical fit and keeps the levels from both
        # chasing the same signal.
        p_key = plant_key(obs.weather, obs.part)
        plant = self.plant.setdefault(p_key, Effect())
        plant.update(residual, weight)

        remainder = residual - plant.shrunk
        offset = self.string.setdefault(obs.string_id, Effect())
        offset.update(remainder, weight)

        sd_key = f"{obs.string_id}|{obs.part}"
        interaction = self.string_daypart.setdefault(sd_key, Effect())
        interaction.update(remainder - offset.shrunk, weight)
        return True

    def observe_many(self, observations: Iterable[Observation]) -> int:
        return sum(1 for obs in observations if self.observe(obs))

    # -- introspection ----------------------------------------------------- #

    def summary(self) -> dict[str, object]:
        return {
            "plant": {
                key: {"factor": round(math.exp(e.shrunk), 4), "n_eff": round(e.n_eff, 2)}
                for key, e in sorted(self.plant.items())
            },
            "string": {
                key: {"factor": round(math.exp(e.shrunk), 4), "n_eff": round(e.n_eff, 2)}
                for key, e in sorted(self.string.items())
            },
            "string_daypart": {
                key: {"factor": round(math.exp(e.shrunk), 4), "n_eff": round(e.n_eff, 2)}
                for key, e in sorted(self.string_daypart.items())
                if e.n_eff >= STRING_DAYPART_MIN_N
            },
        }

    @property
    def observations_seen(self) -> float:
        return round(sum(e.n_eff for e in self.plant.values()), 2)


# --------------------------------------------------------------------------- #
# GHI bias
# --------------------------------------------------------------------------- #


#: Irradiance at which a bias observation carries its full weight.  Above it
#: the weight saturates; there is no such thing as a more-than-complete hour.
BIAS_FULL_WEIGHT_WM2 = 600.0


def bias_weight(measured_ghi: float) -> float:
    """How much one hour should count towards the irradiance bias.

    The bias table exists to correct *energy*, and an hour's contribution to
    the day's energy error scales with its irradiance.  Counting a 20 W/m2
    dawn hour as heavily as a 600 W/m2 midday hour lets the least consequential
    and least reliable part of the day dominate a correction that is then
    applied to the whole of it -- which is precisely how a table ends up with
    more confidence in its 19:00 bucket than in its 13:00 one.
    """
    if measured_ghi <= 0.0:
        return 0.0
    return min(measured_ghi, BIAS_FULL_WEIGHT_WM2) / BIAS_FULL_WEIGHT_WM2


@dataclass
class GhiBiasModel:
    """Per (local hour, forecast horizon) correction of the irradiance source.

    A +1 h and a +48 h forecast do not share a bias, so they must not share a
    bucket.  This is expected to be the largest single lever in the project --
    the physics chain is deterministic, the irradiance input is not.
    """

    buckets: dict[tuple[int, str], Effect] = field(default_factory=dict)

    @classmethod
    def from_rows(
        cls, rows: Mapping[tuple[int, str], tuple[float, float]]
    ) -> "GhiBiasModel":
        return cls(
            buckets={k: Effect(value=v, n_eff=n) for k, (v, n) in rows.items()}
        )

    def to_rows(self) -> dict[tuple[int, str], tuple[float, float]]:
        return {key: effect.as_tuple() for key, effect in self.buckets.items()}

    def factor(self, hour_local: int, horizon_h: float) -> float:
        effect = self.buckets.get((hour_local, horizon_bucket(horizon_h)))
        return math.exp(effect.shrunk) if effect else 1.0

    def observe(
        self,
        hour_local: int,
        horizon_h: float,
        measured_ghi: float,
        forecast_ghi: float,
        weight: float = 1.0,
    ) -> bool:
        if forecast_ghi <= 5.0 or measured_ghi <= 0.0:
            # Near darkness: the ratio explodes and carries no information.
            return False
        ratio = measured_ghi / forecast_ghi
        if not MIN_RATIO <= ratio <= MAX_RATIO:
            return False
        key = (int(hour_local), horizon_bucket(horizon_h))
        self.buckets.setdefault(key, Effect()).update(math.log(ratio), weight)
        return True

    def summary(self) -> dict[str, object]:
        return {
            f"{hour:02d}|{bucket}": {
                "factor": round(math.exp(effect.shrunk), 4),
                "n_eff": round(effect.n_eff, 2),
            }
            for (hour, bucket), effect in sorted(self.buckets.items())
            if effect.n_eff > 0
        }
