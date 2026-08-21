"""Orchestration: forecast, learning cycle and scoring.

This is where the pieces meet.  The order matters:

1. Physics turns irradiance into a per-string potential.  No training involved.
2. The GHI bias model corrects the *irradiance source* per (local hour, forecast
   horizon) -- a +1 h and a +48 h forecast do not share a bias.
3. The log-ratio model corrects whatever the physics still gets wrong, split
   into a plant-wide part and a per-string part.

The two learned layers are deliberately fed from different comparisons so they
cannot both chase the same signal:

* GHI bias learns from *forecast at horizon h* vs. *what the irradiance turned
  out to be* (a measured pyranometer if the user has one, otherwise the same
  source's newest short-horizon run for that target hour).  It never touches PV
  data, so it works from day one on a site with no history.
* The log-ratio model learns from *measured PV* vs. *physics driven by the best
  available irradiance*, i.e. at horizon ~0.  It therefore sees model error, not
  forecast error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import curtailment as curt
from .aggregate import hourly_from_5min, interval_mid
from .config import INTERVAL_SECONDS, GeometrySegment, PlantConfig
from .learning import (
    SCOPE_PLANT,
    SCOPE_STRING,
    SCOPE_STRING_DAYPART,
    GhiBiasModel,
    bias_weight,
    LogRatioModel,
    Observation,
    daypart,
    weather_class,
)
from .physics import PhysicsEngine, to_index
from .plausibility import (
    Plane,
    exceeds_ceiling,
    judgement_floor,
    plant_ceiling_w,
)
from .shading import METHOD_DIFFERENTIAL, ShadingModel
from .quality import (
    QUALITY_NIGHT,
    VALUE_LOWER_BOUND,
    VALUE_MEASURED,
    assess,
)
from .store import Store

_LOGGER = logging.getLogger(__name__)

HOUR = 3600
INTERVALS_PER_HOUR = HOUR // INTERVAL_SECONDS

#: Cursor names in ``learning_cursor``.
CURSOR_HOURLY = "hourly_materialised"
CURSOR_LEARN = "model_learned"
CURSOR_BIAS = "ghi_bias_learned"

#: A forecast issued at most this far ahead of the target hour counts as the
#: "nowcast" -- our best guess at what the irradiance actually was.
NOWCAST_MAX_HORIZON_H = 2

#: Local hour whose forecast counts as "what we said the day before".  Day-ahead
#: quality is scored against the run that stood at this time on the previous
#: day, not against a rolling lead: a rolling one draws each hour of a day from
#: a different model run, and corresponds to no moment at which anybody ever
#: looked at the forecast.  Eighteen hundred is after the day's production is
#: settled and before the evening's decisions.
DAY_AHEAD_ISSUE_HOUR_LOCAL = 18

#: Day-ahead accuracy is withheld until this many complete days are in it.  One
#: day of history yields a confident-looking percentage that describes the
#: weather of a single day, which is worse than admitting we do not know yet.
MIN_SCORED_DAYS = 3

#: Shading observations are only collected above this elevation; below it the
#: ratio is dominated by the model's own low-sun uncertainty.
SHADING_MIN_ELEVATION_DEG = 8.0

#: How much the pile of shading observations must grow before the sky map is
#: worth rebuilding.  Proportional on purpose: a two-day-old map gains half
#: its size in a morning and should follow immediately, while a mature one is
#: barely moved by the same morning and can wait for the daily refit.
SHADING_REFIT_GROWTH = 1.10

#: ...but never for a handful of rows, or a quiet winter afternoon would
#: rebuild the map every hour on a plant that has almost nothing in it.
SHADING_REFIT_MIN_NEW = 24

#: An hour needs this much of its irradiance grid present before the ceiling
#: may judge it.  The ceiling is a mean over the intervals the sensor reported,
#: while the energy it is compared against covers the whole hour -- so a sensor
#: that drops out over the bright half of an hour produces a ceiling built from
#: the dim half and convicts itself.
GHI_HOUR_MIN_COVERAGE = 0.8

METHOD_PHYSICS = "physics"
METHOD_CORRECTED = "physics+learned"


@dataclass(frozen=True, slots=True)
class HourForecast:
    """One forecast hour of one string."""

    ts_utc: int
    string_id: str
    potential_kwh: float
    physics_kwh: float
    #: What the physics would have said with the sky map switched off.  Kept
    #: so a dashboard can show how much of the gap to reality the map already
    #: explains -- the difference between the two curves is the shadow.
    unshaded_kwh: float
    weather: str
    part: str
    method: str
    correction: float
    #: The two corrections that happen before the log-ratio one, kept so a
    #: dashboard can show the chain instead of only its result: what the
    #: irradiance source was trusted at, and what the sky map took away.
    bias_factor: float = 1.0
    shading_factor: float = 1.0

    def as_log_row(self, issued_at_utc: int) -> tuple[Any, ...]:
        return (
            issued_at_utc,
            self.ts_utc,
            self.string_id,
            round(self.potential_kwh, 5),
            self.method,
        )


@dataclass(slots=True)
class _ScoreTally:
    """Paired hours behind one score, accumulated across any number of queries.

    A rolling-lead score fills this from a single query; a day-ahead score fills
    it one local day at a time, because each day has its own issue cut-off.
    Both then hand the identical structure to the same metrics function, so the
    two figures stay comparable.
    """

    uncensored: list[tuple[float, float]] = field(default_factory=list)
    every: list[tuple[float, float]] = field(default_factory=list)
    daily_uncensored: dict[str, list[float]] = field(default_factory=dict)
    daily_all: dict[str, list[float]] = field(default_factory=dict)


@dataclass(slots=True)
class LearnStats:
    hours_materialised: int = 0
    observations_used: int = 0
    observations_skipped: int = 0
    bias_observations: int = 0
    shading_observations: int = 0
    ghi_hours_rejected: int = 0
    censored_hours: int = 0
    reconstructed_intervals: int = 0
    #: Why observations were skipped.  A bare count says an hour was not used
    #: and leaves you guessing which of four quite different reasons applied --
    #: which is exactly how a plant can sit at zero learned observations for
    #: days with nothing to point at.
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.observations_skipped += 1
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours_materialised": self.hours_materialised,
            "observations_used": self.observations_used,
            "observations_skipped": self.observations_skipped,
            "bias_observations": self.bias_observations,
            "shading_observations": self.shading_observations,
            "ghi_hours_rejected": self.ghi_hours_rejected,
            "censored_hours": self.censored_hours,
            "reconstructed_intervals": self.reconstructed_intervals,
            "skipped_because": dict(sorted(self.skipped.items())),
        }


def floor_hour(ts_utc: float) -> int:
    return int(ts_utc // HOUR * HOUR)


class ForecastEngine:
    """Stateful orchestrator.  One instance per config entry."""

    def __init__(
        self,
        plant: PlantConfig,
        store: Store,
        physics: PhysicsEngine | None = None,
    ) -> None:
        self.plant = plant
        self.store = store
        self.physics = physics or PhysicsEngine(
            latitude=plant.latitude,
            longitude=plant.longitude,
            elevation_m=plant.elevation_m,
            albedo=plant.albedo,
            transposition_model=plant.transposition_model,
            time_zone=plant.time_zone,
        )
        self.model = LogRatioModel()
        self.ghi_bias = GhiBiasModel()
        self.shading = ShadingModel()
        self._tz = ZoneInfo(plant.time_zone)
        self._monthly_weights: list[float] | None = None
        #: Memoised result of the irradiance plausibility check.  One learn
        #: cycle asks for the measured GHI three times over the same window;
        #: the check is not free and must not be counted three times either.
        self._implausible_key: tuple[int, int] | None = None
        self._implausible_hours: frozenset[int] = frozenset()
        self._shading_fitted_day: int | None = None
        self._shading_fitted_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # model state
    # ------------------------------------------------------------------ #

    def load_models(self) -> None:
        self.model = LogRatioModel.from_rows(
            plant=self.store.load_effects(SCOPE_PLANT),
            string=self.store.load_effects(SCOPE_STRING),
            string_daypart=self.store.load_effects(SCOPE_STRING_DAYPART),
        )
        self.ghi_bias = GhiBiasModel.from_rows(
            self.store.load_ghi_bias(self.plant.forecast_source)
        )
        self.fit_shading(force=True)

    def fit_shading(self, now_ts: float | None = None, force: bool = False) -> None:
        """Rebuild the sky maps from the raw observations.

        Refitted rather than stored: the observations are the durable thing,
        and a map derived from them can be re-cut at a different resolution
        later without a migration.  A few tens of thousands of rows group in
        milliseconds, which is nothing next to the pvlib pass it feeds.

        Refitted when the evidence has actually moved, not on a schedule.

        The cost of a refit scales with the size of the table, and in steady
        state that is one row per five-minute interval per string across the
        retention window -- hundreds of thousands of rows.  Doing it every
        daylight hour would be a visible load on a small host.  But a fixed
        daily cadence is wrong at the other end of the plant's life: a map two
        days old grows by half its size in a morning, and holding that back
        until tomorrow means a whole day of sun corrects nothing.

        Tying the trigger to *proportional* growth handles both without a
        special case.  A young map refits several times a day because a
        morning is a large fraction of what it knows; a mature one falls back
        to the daily floor because the same morning barely moves the total.
        """
        day = None if now_ts is None else int(now_ts // 86400)
        counts = self.store.shading_observations_by_string()
        fresh_day = day is None or day != self._shading_fitted_day
        # Per string, because the maps are per string.  A plant-wide total
        # would let one long-established string hold back a new or repaired
        # one: sixty fresh rows against a sibling's three thousand is not ten
        # percent of anything, and that string's map would sit idle for a day
        # while the sun crossed the sky it needs to see.
        grown = any(
            count >= self._shading_fitted_counts.get(string_id, 0)
            * SHADING_REFIT_GROWTH
            and count - self._shading_fitted_counts.get(string_id, 0)
            >= SHADING_REFIT_MIN_NEW
            for string_id, count in counts.items()
        )
        if not (force or fresh_day or grown):
            return
        self.shading = ShadingModel.fit(
            self.store.shading_rows_by_string(), now_ts=now_ts
        )
        self._shading_fitted_day = day
        self._shading_fitted_counts = dict(counts)

    def save_models(self, now_ts: int) -> None:
        for scope in (SCOPE_PLANT, SCOPE_STRING, SCOPE_STRING_DAYPART):
            self.store.save_effects(scope, self.model.to_rows(scope), now_ts)
        self.store.save_ghi_bias(
            self.plant.forecast_source, self.ghi_bias.to_rows(), now_ts
        )

    # ------------------------------------------------------------------ #
    # geometry
    # ------------------------------------------------------------------ #

    def geometry_at(self, string_id: str, ts_utc: int) -> GeometrySegment | None:
        return self.store.geometry_at(string_id, ts_utc)

    def _geometry_segments(
        self, string_id: str, hours: Sequence[int]
    ) -> list[tuple[GeometrySegment, list[int]]]:
        """Group the requested hours by the geometry in force.

        Almost always a single group -- but when an adjustable mount was moved
        mid-window, the two halves must be computed against different tilts
        rather than smeared into one wrong average.
        """
        grouped: list[tuple[GeometrySegment, list[int]]] = []
        for hour in hours:
            segment = self.geometry_at(string_id, hour)
            if segment is None:
                continue
            if grouped and grouped[-1][0] == segment:
                grouped[-1][1].append(hour)
            else:
                grouped.append((segment, [hour]))
        return grouped

    # ------------------------------------------------------------------ #
    # irradiance on the five-minute grid
    # ------------------------------------------------------------------ #

    def _midpoint_index(self, start_ts: int, end_ts: int) -> pd.DatetimeIndex:
        """Interval midpoints, which is where solar position must be evaluated."""
        stamps = [
            interval_mid(ts)
            for ts in range(int(start_ts), int(end_ts), INTERVAL_SECONDS)
        ]
        return to_index(stamps)

    def _hourly_frame(self, rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
        records = [dict(row) for row in rows]
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame.from_records(records)
        frame = frame.drop_duplicates(subset="ts_utc", keep="last")
        frame = frame.set_index("ts_utc").sort_index()
        return frame

    def _downscale(
        self,
        index: pd.DatetimeIndex,
        hourly: pd.DataFrame,
        apply_bias: bool = True,
        issued_at_utc: int | None = None,
    ) -> pd.DataFrame:
        """Spread hourly irradiance onto the five-minute grid.

        Holding GHI constant across an hour is badly wrong near sunrise and
        sunset, where the clear-sky curve moves by a factor of several within
        the hour.  Instead we hold the **clear-sky index** constant and let the
        clear-sky model supply the shape.  Temperature and wind are simply
        interpolated -- they do not have that problem.
        """
        epochs = np.array([int(value.timestamp()) for value in index])
        hour_keys = (epochs // HOUR) * HOUR

        solar_position = self.physics.solar_position(index)
        clearsky = self.physics.clearsky(index, solar_position=solar_position)

        cs_frame = pd.DataFrame(
            {
                "hour": hour_keys,
                "cs_ghi": clearsky["ghi"].to_numpy(),
                "cs_dni": clearsky["dni"].to_numpy(),
                "cs_dhi": clearsky["dhi"].to_numpy(),
            },
            index=index,
        )
        hour_means = cs_frame.groupby("hour")[["cs_ghi", "cs_dni", "cs_dhi"]].mean()

        out = pd.DataFrame(index=index)
        out["hour"] = hour_keys

        def hourly_series(column: str) -> np.ndarray:
            if column not in hourly.columns:
                return np.full(len(index), np.nan)
            return hourly[column].reindex(hour_keys).to_numpy(dtype=float)

        fc_ghi = hourly_series("ghi_wm2")
        fc_dni = hourly_series("dni_wm2")
        fc_dhi = hourly_series("dhi_wm2")

        if apply_bias and issued_at_utc is not None:
            factors = np.array(
                [
                    self._bias_factor(int(hour), issued_at_utc)
                    for hour in hour_keys
                ]
            )
            fc_ghi = fc_ghi * factors
            out["bias_factor"] = factors
            fc_dni = fc_dni * factors
            fc_dhi = fc_dhi * factors

        # An hour the source never delivered is unknown, not dark.  Turning
        # NaN into 0 W/m2 makes a short-horizon weather entity produce a
        # confident 0.00 kWh for the day after tomorrow, sitting next to a
        # correct today and indistinguishable from a genuinely dark forecast.
        if "bias_factor" not in out:
            out["bias_factor"] = np.ones(len(index))
        out["covered"] = np.isfinite(fc_ghi)

        for name, forecast, cs_column in (
            ("ghi", fc_ghi, "cs_ghi"),
            ("dni", fc_dni, "cs_dni"),
            ("dhi", fc_dhi, "cs_dhi"),
        ):
            hour_mean = hour_means[cs_column].reindex(hour_keys).to_numpy(dtype=float)
            instant = cs_frame[cs_column].to_numpy(dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(hour_mean > 1.0, forecast / hour_mean, 0.0)
            values = np.where(np.isfinite(ratio), ratio * instant, np.nan)
            # Below the clear-sky floor there is nothing to shape; fall back to
            # the flat hourly value rather than producing a NaN hole.
            values = np.where(hour_mean > 1.0, values, np.nan_to_num(forecast))
            out[name] = np.clip(np.nan_to_num(values), 0.0, None)

        for name, column, default in (
            ("temp_c", "temp_c", 20.0),
            ("wind_ms", "wind_ms", 1.0),
            ("clouds_pct", "clouds_pct", np.nan),
            ("rain_mm", "rain_mm", 0.0),
        ):
            series = pd.Series(hourly_series(column), index=index)
            series = series.interpolate(limit_direction="both")
            out[name] = series.fillna(default)

        out["cs_ghi"] = cs_frame["cs_ghi"]
        out["elevation"] = solar_position["apparent_elevation"].to_numpy()
        return out

    def _bias_factor(self, hour_ts: int, issued_at_utc: int) -> float:
        hour_local = datetime.fromtimestamp(hour_ts, tz=self._tz).hour
        horizon_h = max(0.0, (hour_ts - issued_at_utc) / HOUR)
        return self.ghi_bias.factor(hour_local, horizon_h)

    # ------------------------------------------------------------------ #
    # forecast
    # ------------------------------------------------------------------ #

    def forecast(
        self,
        now_ts: int,
        hours: int = 48,
        apply_learning: bool = True,
        start_ts: int | None = None,
    ) -> list[HourForecast]:
        """Per-string hourly potential over the horizon."""
        start = floor_hour(start_ts if start_ts is not None else now_ts)
        end = start + hours * HOUR
        rows = self.store.latest_forecast(start, end, self.plant.forecast_source)
        hourly = self._hourly_frame(rows)
        if hourly.empty:
            _LOGGER.debug("pvstrings: no weather forecast rows for %s..%s", start, end)
            return []

        index = self._midpoint_index(start, end)
        conditions = self._downscale(
            index, hourly, apply_bias=apply_learning, issued_at_utc=now_ts
        )
        return self._evaluate(
            index, conditions, apply_learning=apply_learning, is_forecast=True
        )

    def _evaluate(
        self,
        index: pd.DatetimeIndex,
        conditions: pd.DataFrame,
        apply_learning: bool,
        is_forecast: bool,
    ) -> list[HourForecast]:
        """Run physics for every string and fold to hourly energies."""
        hour_keys = conditions["hour"].to_numpy()
        covered = conditions.groupby("hour")["covered"].any()
        unique_hours = sorted(
            int(hour) for hour in {int(h) for h in hour_keys} if covered.get(hour, True)
        )
        classes = self._classify_hours(conditions)
        # Weighted by irradiance: the bias of a dark interval is real but says
        # nothing about the hour's energy, and a plain mean lets dawn dominate.
        weights = conditions["ghi"].to_numpy()
        bias_frame = pd.DataFrame(
            {
                "hour": conditions["hour"].to_numpy(),
                "w": weights,
                "wb": weights * conditions["bias_factor"].to_numpy(),
            }
        ).groupby("hour").sum()
        bias_by_hour = {
            int(hour): (row["wb"] / row["w"] if row["w"] > 0 else 1.0)
            for hour, row in bias_frame.iterrows()
        }
        # Shading is a learned correction like any other, so it answers to both
        # gates: ``apply_learning`` for callers that want the bare physics --
        # the accuracy baseline, and every test that pins the chain itself --
        # and the user's own "apply learned correction" switch.  Honouring only
        # the first left a plant with learning turned off still being
        # multiplied down by a map it had been told to ignore.
        shading_position = (
            self.physics.solar_position(index)
            if apply_learning and self.plant.learning_enabled
            else None
        )

        results: list[HourForecast] = []
        for string in self.plant.strings:
            grouped = self._geometry_segments(string.string_id, unique_hours)
            if not grouped:
                _LOGGER.debug(
                    "pvstrings: no geometry for string %s, skipping",
                    string.string_id,
                )
                continue

            per_hour: dict[int, float] = {}
            per_hour_unshaded: dict[int, float] = {}
            for segment, hours_in_segment in grouped:
                mask = np.isin(hour_keys, hours_in_segment)
                if not mask.any():
                    continue
                sub_index = index[mask]
                sub = conditions.loc[mask]
                shading_factor: pd.Series | float = 1.0
                shading_scope = "total"
                if shading_position is not None:
                    sub_position = shading_position.loc[mask]
                    shading_factor = pd.Series(
                        self.shading.factors(
                            string.string_id,
                            sub_position["azimuth"].to_numpy(),
                            sub_position["apparent_elevation"].to_numpy(),
                            [value.timestamp() for value in sub_index],
                        ),
                        index=sub_index,
                    )
                    # Differential cells hold the clear-day loss; physics
                    # applies it to the POA beam component only.  Absolute
                    # envelopes already average the weather in.
                    if self.shading.method_of(string.string_id) == METHOD_DIFFERENTIAL:
                        shading_scope = "beam"
                result = self.physics.run(
                    sub_index,
                    segment,
                    ghi=pd.Series(sub["ghi"].to_numpy(), index=sub_index),
                    dni=pd.Series(sub["dni"].to_numpy(), index=sub_index),
                    dhi=pd.Series(sub["dhi"].to_numpy(), index=sub_index),
                    temp_air=pd.Series(sub["temp_c"].to_numpy(), index=sub_index),
                    wind_speed=pd.Series(sub["wind_ms"].to_numpy(), index=sub_index),
                    system_efficiency=self.plant.efficiency_of(string.string_id),
                    mount_type=string.mount_type,
                    shading_factor=shading_factor,
                    shading_scope=shading_scope,
                )
                power = result.dc_power_w.to_numpy()
                # The chain is exactly linear in the *applied* ratio -- it
                # scales the effective irradiance, and the cell temperature is
                # taken from the unscaled plane irradiance -- so dividing it
                # back out recovers the unshaded power without a second pass.
                # This has to happen *before* the ceiling: dividing an already
                # capped value would report a 430 W tracker under half shade as
                # though it could have made 860 W, when without the shadow it
                # would simply have sat on its ceiling.
                if isinstance(shading_factor, pd.Series):
                    divisor = result.shading_applied.to_numpy()
                    bare = np.divide(
                        power, divisor, out=power.copy(), where=divisor > 0.0
                    )
                    # The physics itself clips at nameplate, and that clip is
                    # the one place the chain stops being linear in the shading
                    # factor: on a bright, cold interval the shaded value is
                    # already sitting on the ceiling, so dividing it back out
                    # invents power the module could never make.  Every ceiling
                    # the shaded curve met, the bare curve meets too.
                    bare = np.minimum(bare, segment.kwp * 1000.0)
                else:
                    bare = power
                if string.max_power_w:
                    # The published forecast must respect the tracker ceiling:
                    # promising 500 W through a channel that tops out at 430 W
                    # is energy nobody can ever collect, and it would inflate
                    # every accuracy figure.  The *uncensored* physics used for
                    # the binding test stays uncapped -- see _interval_power.
                    power = np.minimum(power, string.max_power_w)
                    bare = np.minimum(bare, string.max_power_w)
                energy = power * INTERVAL_SECONDS / HOUR / 1000.0
                unshaded = bare * INTERVAL_SECONDS / HOUR / 1000.0
                for hour, value, bare in zip(
                    sub["hour"].to_numpy(), energy, unshaded
                ):
                    per_hour[int(hour)] = per_hour.get(int(hour), 0.0) + float(value)
                    per_hour_unshaded[int(hour)] = per_hour_unshaded.get(
                        int(hour), 0.0
                    ) + float(bare)

            for hour in unique_hours:
                physics_kwh = per_hour.get(hour, 0.0)
                weather, part = classes[hour]
                correction = 1.0
                method = METHOD_PHYSICS
                if apply_learning and self.plant.learning_enabled and physics_kwh > 0:
                    correction = self.model.factor(string.string_id, weather, part)
                    method = METHOD_CORRECTED
                results.append(
                    HourForecast(
                        ts_utc=hour,
                        string_id=string.string_id,
                        potential_kwh=physics_kwh * correction,
                        physics_kwh=physics_kwh,
                        unshaded_kwh=per_hour_unshaded.get(hour, physics_kwh)
                        * correction,
                        bias_factor=float(bias_by_hour.get(hour, 1.0)),
                        shading_factor=(
                            physics_kwh / per_hour_unshaded[hour]
                            if per_hour_unshaded.get(hour)
                            else 1.0
                        ),
                        weather=weather,
                        part=part,
                        method=method,
                        correction=correction,
                    )
                )
        return results

    def _classify_hours(self, conditions: pd.DataFrame) -> dict[int, tuple[str, str]]:
        """Weather class and daypart per hour."""
        frame = conditions.groupby("hour").agg(
            ghi=("ghi", "mean"),
            cs_ghi=("cs_ghi", "mean"),
            rain=("rain_mm", "max"),
            clouds=("clouds_pct", "mean"),
        )
        out: dict[int, tuple[str, str]] = {}
        for hour, row in frame.iterrows():
            hour_ts = int(hour)
            kc: float | None = None
            if row["cs_ghi"] and row["cs_ghi"] > 20.0:
                kc = float(row["ghi"]) / float(row["cs_ghi"])
            clouds = None if pd.isna(row["clouds"]) else float(row["clouds"])
            rain = None if pd.isna(row["rain"]) else float(row["rain"])
            solar_noon = self.physics.solar_noon_for(hour_ts + HOUR / 2)
            out[hour_ts] = (
                weather_class(clearsky_index=kc, clouds_pct=clouds, rain_mm=rain),
                daypart(hour_ts + HOUR / 2, solar_noon),
            )
        return out

    def log_forecast(self, issued_at_utc: int, rows: Sequence[HourForecast]) -> int:
        """Record the prediction so it can be scored later.

        The issue time is quantised to the hour and hours that have already
        started are dropped.  Two reasons:

        * The coordinator recomputes every fifteen minutes.  Logging each run
          under its own issue time would write four full 48-hour horizons per
          hour and turn this table into the largest thing on disk.  Quantising
          makes the later runs overwrite the earlier ones through the primary
          key.
        * With the issue quantised, a run computed at 14:10 would otherwise be
          stamped 14:00 and then look like a forecast *for* the 14:00 hour that
          predates it.  Only logging hours that have not begun makes hindsight
          structurally impossible rather than merely discouraged.
        """
        issued_hour = floor_hour(issued_at_utc)
        return self.store.log_forecast(
            [row.as_log_row(issued_hour) for row in rows if row.ts_utc > issued_hour]
        )

    # ------------------------------------------------------------------ #
    # curtailment evaluation on the five-minute grid
    # ------------------------------------------------------------------ #

    def evaluate_curtailment(self, start_ts: int, end_ts: int) -> int:
        """Decide, per five-minute interval, whether output was actually held back.

        Two independent ceilings can bite, and both have to be tested:

        * the **group's** inverter limit, which applies to the sum over the
          group.  Testing one string against a limit that covers three of them
          never fires, and every clipped hour would be learned as if it were
          free.
        * the **string's own tracker ceiling**.  A 1600 W micro-inverter with
          four trackers caps each channel near 430 W regardless of what the
          module could deliver.  No limit entity ever reports that, so without
          it the model concludes those strings weaken in bright sun.

        The collector cannot do any of this: it knows the commanded limit but
        not the potential.  Only once physics has run can a commanded limit be
        told apart from an effective one.
        """
        index = self._midpoint_index(start_ts, end_ts)
        if len(index) == 0:
            return 0
        conditions = self._actual_conditions(index, start_ts, end_ts)
        if conditions is None:
            return 0

        potentials, _beams = self._interval_power(index, conditions)
        rows: dict[str, dict[int, Any]] = {}
        for string in self.plant.strings:
            rows[string.string_id] = {
                int(row["ts_utc"]): row
                for row in self.store.fivemin_range(
                    string.string_id, start_ts, end_ts
                )
            }

        group_flags = self._group_binding(rows, potentials)

        updates: list[tuple[int | None, int, str]] = []
        for string in self.plant.strings:
            series = potentials.get(string.string_id)
            if series is None:
                continue
            for ts, row in rows[string.string_id].items():
                physics_w = series.get(ts)
                own = curt.is_binding(
                    row["power_mean_w"], string.max_power_w, physics_w
                )
                shared = group_flags.get(string.curtailment_group_id, {}).get(ts)
                binding = curt.combine_binding(own, shared)
                if binding is None and row["limit_binding"] is None:
                    continue
                updates.append(
                    (None if binding is None else int(binding), ts, string.string_id)
                )
        self.store.update_curtailment_flags(updates)
        return len(updates)

    @staticmethod
    def _binding_span(rows: dict[str, dict[int, Any]]) -> tuple[int, int]:
        """The interval range covered by ``rows``, as a half-open span."""
        stamps = [ts for series in rows.values() for ts in series]
        if not stamps:
            return 0, 0
        return min(stamps), max(stamps) + INTERVAL_SECONDS

    def _group_binding(
        self,
        rows: dict[str, dict[int, Any]],
        potentials: dict[str, dict[int, float]],
    ) -> dict[str, dict[int, bool | None]]:
        """Per group and interval: was the group held back?

        Two quite different mechanisms, merged into one verdict: a commanded
        inverter limit, and a battery-coupled group whose battery has filled up.
        The second commands nothing and leaves no trace in the data -- only the
        state of charge betrays it.
        """
        out: dict[str, dict[int, bool | None]] = {}
        # The plant's battery.  One battery per site is every installation we
        # have seen; a site with one battery per inverter would need per-group
        # SOC collection -- worth revisiting before that stops being true.
        soc = (
            self.store.battery_soc_series(*self._binding_span(rows))
            if any(group.battery_coupled for group in self.plant.groups)
            else {}
        )
        for group in self.plant.groups:
            members = self.plant.strings_in_group(group.group_id)
            if not members:
                continue
            stamps: set[int] = set()
            for member in members:
                stamps |= set(rows.get(member.string_id, {}))
            flags: dict[int, bool | None] = {}
            for ts in stamps:
                measured = 0.0
                physics = 0.0
                limit: float | None = None
                complete = True
                for member in members:
                    row = rows.get(member.string_id, {}).get(ts)
                    power = row["power_mean_w"] if row is not None else None
                    potential = potentials.get(member.string_id, {}).get(ts)
                    if power is None or potential is None:
                        complete = False
                        break
                    measured += power
                    physics += potential
                    if row["limit_commanded_w"] is not None:
                        limit = row["limit_commanded_w"]
                if not complete:
                    # A partial sum would understate the group and hide a
                    # binding limit, so we decline to judge rather than guess.
                    flags[ts] = None
                    continue
                commanded = curt.group_binding(measured, limit, physics)
                battery = (
                    curt.full_battery_binding(
                        measured, physics, soc.get(ts), group.soc_limit_pct
                    )
                    if group.battery_coupled
                    else None
                )
                flags[ts] = curt.combine_binding(commanded, battery)
            out[group.group_id] = flags
        return out

    def _interval_power(
        self,
        index: pd.DatetimeIndex,
        conditions: pd.DataFrame,
        apply_shading: bool = True,
    ) -> tuple[dict[str, dict[int, float]], dict[str, dict[int, float]]]:
        """DC power and POA beam share per string per interval (start-keyed).

        Returns ``(power, beam_share)``.  ``apply_shading`` must be false
        wherever the power is the denominator of a shading observation --
        measuring the map against physics that already contains the map would
        freeze it.  The beam share is geometry-only and unaffected by the
        flag.
        """
        shading_position = (
            self.physics.solar_position(index)
            if apply_shading and self.plant.learning_enabled
            else None
        )
        epochs = [int(value.timestamp()) - INTERVAL_SECONDS // 2 for value in index]
        hour_keys = conditions["hour"].to_numpy()
        unique_hours = sorted({int(hour) for hour in hour_keys})

        out: dict[str, dict[int, float]] = {}
        beam_out: dict[str, dict[int, float]] = {}
        for string in self.plant.strings:
            grouped = self._geometry_segments(string.string_id, unique_hours)
            if not grouped:
                continue
            values: dict[int, float] = {}
            beams: dict[int, float] = {}
            for segment, hours_in_segment in grouped:
                mask = np.isin(hour_keys, hours_in_segment)
                if not mask.any():
                    continue
                sub_index = index[mask]
                sub = conditions.loc[mask]
                shading_factor: pd.Series | float = 1.0
                shading_scope = "total"
                if shading_position is not None:
                    sub_position = shading_position.loc[mask]
                    shading_factor = pd.Series(
                        self.shading.factors(
                            string.string_id,
                            sub_position["azimuth"].to_numpy(),
                            sub_position["apparent_elevation"].to_numpy(),
                            [value.timestamp() for value in sub_index],
                        ),
                        index=sub_index,
                    )
                    if (
                        self.shading.method_of(string.string_id)
                        == METHOD_DIFFERENTIAL
                    ):
                        shading_scope = "beam"
                result = self.physics.run(
                    sub_index,
                    segment,
                    ghi=pd.Series(sub["ghi"].to_numpy(), index=sub_index),
                    dni=pd.Series(sub["dni"].to_numpy(), index=sub_index),
                    dhi=pd.Series(sub["dhi"].to_numpy(), index=sub_index),
                    temp_air=pd.Series(sub["temp_c"].to_numpy(), index=sub_index),
                    wind_speed=pd.Series(sub["wind_ms"].to_numpy(), index=sub_index),
                    system_efficiency=self.plant.efficiency_of(string.string_id),
                    mount_type=string.mount_type,
                    shading_factor=shading_factor,
                    shading_scope=shading_scope,
                )
                sub_epochs = [
                    int(value.timestamp()) - INTERVAL_SECONDS // 2
                    for value in sub_index
                ]
                for ts, power, beam in zip(
                    sub_epochs,
                    result.dc_power_w.to_numpy(),
                    result.beam_share.to_numpy(),
                ):
                    values[int(ts)] = float(power)
                    beams[int(ts)] = float(beam)
            out[string.string_id] = values
            beam_out[string.string_id] = beams
        return out, beam_out

    def _actual_conditions(
        self, index: pd.DatetimeIndex, start_ts: int, end_ts: int
    ) -> pd.DataFrame | None:
        """Best available reconstruction of the irradiance that actually occurred.

        Preference order: a measured GHI (or illuminance) sensor, then the
        source's shortest-horizon run for that hour.  Never a long-horizon
        forecast -- that would fold forecast error into the model correction.
        """
        rows = self.store.latest_forecast(start_ts, end_ts, self.plant.forecast_source)
        hourly = self._hourly_frame(rows)
        if hourly.empty:
            return None

        conditions = self._downscale(index, hourly, apply_bias=False)
        measured = self._measured_ghi(start_ts, end_ts)
        if measured is not None and not measured.empty:
            epochs = np.array(
                [int(value.timestamp()) - INTERVAL_SECONDS // 2 for value in index]
            )
            aligned = measured.reindex(epochs)
            replace = aligned.notna().to_numpy()
            if replace.any():
                ghi = conditions["ghi"].to_numpy().copy()
                ghi[replace] = aligned.to_numpy()[replace]
                conditions["ghi"] = ghi
                # Components no longer match the measured GHI -- drop them and
                # let the physics layer decompose with Erbs instead of mixing a
                # measured global with a forecast split.
                conditions["dni"] = np.nan
                conditions["dhi"] = np.nan
        return conditions

    def _raw_measured_ghi(self, start_ts: int, end_ts: int) -> pd.Series | None:
        """Whatever the sensor reported, before it has been believed."""
        sources = self.plant.weather_sources
        if not (sources.ghi_entity or sources.illuminance_entity):
            return None
        rows = self.store.weather_actual_range(start_ts, end_ts)
        if not rows:
            return None
        values: dict[int, float] = {}
        for row in rows:
            if row["ghi_wm2"] is not None:
                values[int(row["ts_utc"])] = float(row["ghi_wm2"])
        if not values:
            return None
        return pd.Series(values).sort_index()

    def _measured_ghi(self, start_ts: int, end_ts: int) -> pd.Series | None:
        series = self._raw_measured_ghi(start_ts, end_ts)
        if series is None:
            return None

        rejected = self.implausible_ghi_hours(start_ts, end_ts, series)
        if not rejected:
            return series
        hours = (series.index.to_numpy() // HOUR) * HOUR
        keep = ~np.isin(hours, list(rejected))
        kept = series[keep]
        return kept if not kept.empty else None

    # ------------------------------------------------------------------ #
    # irradiance plausibility
    # ------------------------------------------------------------------ #

    def implausible_ghi_hours(
        self,
        start_ts: int,
        end_ts: int,
        series: pd.Series | None = None,
    ) -> frozenset[int]:
        """Hours whose measured irradiance the array itself contradicts.

        Dropping the hour rather than correcting it is the conservative move:
        we can prove the sensor is wrong, but not by how much, and the forecast
        source is a serviceable second-best.  Silence is the common case -- a
        healthy sensor never trips this.
        """
        key = (int(start_ts), int(end_ts))
        if self._implausible_key == key:
            return self._implausible_hours

        result = self._find_implausible_ghi_hours(start_ts, end_ts, series)
        self._implausible_key = key
        self._implausible_hours = result
        return result

    def _find_implausible_ghi_hours(
        self,
        start_ts: int,
        end_ts: int,
        series: pd.Series | None,
    ) -> frozenset[int]:
        if series is None:
            series = self._raw_measured_ghi(start_ts, end_ts)
        if series is None or series.empty:
            return frozenset()

        actual = self._plant_hourly_actual(start_ts, end_ts)
        if not actual:
            return frozenset()

        epochs = series.index.to_numpy()
        position = self.physics.solar_position(
            to_index(epochs + INTERVAL_SECONDS // 2)
        )
        elevation = position["apparent_elevation"].to_numpy()
        azimuth = position["azimuth"].to_numpy()
        ghi = series.to_numpy(dtype=float)
        hours = (epochs // HOUR) * HOUR

        expected_samples = HOUR // INTERVAL_SECONDS
        rejected: set[int] = set()
        for hour, per_string in actual.items():
            mask = hours == hour
            present = int(mask.sum())
            if present < expected_samples * GHI_HOUR_MIN_COVERAGE:
                # Too little of the hour was measured for its mean to stand
                # against a whole hour of energy.  Leaving the hour alone is
                # the safe outcome: an unjudged hour is still usable truth,
                # a wrongly rejected one is not.
                continue
            planes = self._planes_at(hour)
            if not planes:
                continue
            # Only strings that contributed a plane may contribute energy.  A
            # string with no geometry on record would otherwise be counted
            # against a ceiling that never made room for it, and accuse a
            # perfectly good sensor.
            measured_kwh = sum(
                kwh for string_id, kwh in per_string.items() if string_id in planes
            )
            ceiling = plant_ceiling_w(
                list(planes.values()), ghi[mask], elevation[mask], azimuth[mask]
            )
            # Both sides now describe the same, near-complete hour: the
            # coverage gate above guarantees the samples span it.
            if exceeds_ceiling(
                measured_kwh * 1000.0,
                float(np.mean(ceiling)),
                floor_w=judgement_floor(self._total_kwp()),
            ):
                rejected.add(int(hour))
        return frozenset(rejected)

    def _planes_at(self, hour: int) -> dict[str, Plane]:
        planes: dict[str, Plane] = {}
        for string in self.plant.strings:
            segment = self.geometry_at(string.string_id, hour)
            if segment is None:
                continue
            planes[string.string_id] = Plane(
                tilt_deg=segment.tilt_deg,
                azimuth_deg=segment.azimuth_deg,
                kwp=segment.kwp,
            )
        return planes

    def _plant_hourly_actual(
        self, start_ts: int, end_ts: int
    ) -> dict[int, dict[str, float]]:
        """Measured energy per hour per string, in kWh.

        Read from the five-minute rows rather than the hourly fold, and that is
        not an optimisation.  The fold is produced by the very learn cycle that
        wants this answer, one line after the check runs, so reading it here
        made the whole guard look at an empty table and quietly conclude that
        every sensor reading was fine.  The five-minute rows are written by the
        collector, independently of any cycle, so they are always already there.

        Only measured, well-covered intervals count.  A missing or censored
        string makes the sum too small, which can only ever *prevent* a
        rejection -- the safe direction for a test that overrules a sensor.
        """
        totals: dict[int, dict[str, float]] = {}
        for row in self.store.measured_5min_range(start_ts, end_ts):
            hour = int(row["ts_utc"]) // HOUR * HOUR
            bucket = totals.setdefault(hour, {})
            energy = (
                float(row["power_mean_w"]) * INTERVAL_SECONDS / HOUR / 1000.0
            )
            bucket[row["string_id"]] = bucket.get(row["string_id"], 0.0) + energy
        return totals

    # ------------------------------------------------------------------ #
    # hourly materialisation
    # ------------------------------------------------------------------ #

    def materialise_hourly(self, start_ts: int, end_ts: int) -> int:
        """Fold five-minute rows into ``string_hourly``.

        Hourly values are always derived, never measured separately, so the two
        can never drift apart.
        """
        written = 0
        payload: list[tuple[Any, ...]] = []
        for hour in range(floor_hour(start_ts), floor_hour(end_ts), HOUR):
            solar_noon = self.physics.solar_noon_for(hour + HOUR / 2)
            mid_index = to_index([hour + HOUR / 2])
            elevation = float(
                self.physics.solar_position(mid_index)["apparent_elevation"].iloc[0]
            )
            for string in self.plant.strings:
                rows = self.store.fivemin_range(
                    string.string_id, hour, hour + HOUR
                )
                if not rows:
                    continue
                folded = hourly_from_5min(
                    (
                        int(row["ts_utc"]),
                        row["energy_wh"],
                        float(row["coverage"]),
                        row["value_kind"],
                        row["limit_binding"],
                        row["limit_commanded_w"],
                    )
                    for row in rows
                )
                quality = assess(folded["coverage"], elevation).quality
                payload.append(
                    (
                        hour,
                        string.string_id,
                        folded["energy_kwh"],
                        folded["coverage"],
                        folded["curtailed_fraction"],
                        folded["limit_min_w"],
                        folded["limit_max_w"],
                        folded["limit_mean_w"],
                        folded["value_kind"],
                        quality,
                    )
                )
                written += 1
        self.store.upsert_hourly(payload)
        return written

    # ------------------------------------------------------------------ #
    # learning cycle
    # ------------------------------------------------------------------ #

    def learn(self, now_ts: int, max_hours: int = 48) -> LearnStats:
        """Process every hour that has closed since the last run.

        Bounded by ``max_hours`` so a long outage cannot turn the first update
        after a restart into a multi-minute blocking job.
        """
        stats = LearnStats()
        last_closed = floor_hour(now_ts) - HOUR
        # Default zero, not "one hour back": on a cold start there may already
        # be days of collected data, and the ``max_hours`` clamp below is what
        # keeps the catch-up bounded.
        cursor = self.store.get_cursor(CURSOR_LEARN, default=0)
        if cursor <= 0:
            # Cold start: look back a bounded window rather than crawling
            # whatever happens to be in the database.
            start = max(0, last_closed - max_hours * HOUR)
        else:
            # Warm start: continue exactly where the last run stopped.  Taking
            # max(cursor, now - max_hours) here would silently drop everything
            # older than the window after any downtime longer than it, and the
            # cursor would then jump past those hours for good.
            start = cursor
        if start > last_closed:
            return stats

        # Advance by at most one window per run; the next hourly cycle picks up
        # the rest, so a long outage catches up over a few cycles instead of
        # blocking one of them for minutes.
        end = min(last_closed + HOUR, start + max_hours * HOUR)

        self.evaluate_curtailment(start, end)
        stats.hours_materialised = self.materialise_hourly(start, end)
        # Must exist before compaction is allowed to drop the raw rows.
        self.store.materialise_plant_hourly(start, end)
        self._learn_ghi_bias(start, end, stats)

        if self.plant.learning_enabled:
            self._learn_effects(start, end, stats)

        if stats.shading_observations:
            self.fit_shading(now_ts)
        stats.ghi_hours_rejected = len(self.implausible_ghi_hours(start, end))
        self.store.set_cursor(CURSOR_LEARN, end)
        self.store.set_cursor(CURSOR_HOURLY, end)
        self.save_models(now_ts)
        return stats

    def _learn_effects(self, start_ts: int, end_ts: int, stats: LearnStats) -> None:
        index = self._midpoint_index(start_ts, end_ts)
        if len(index) == 0:
            return
        conditions = self._actual_conditions(index, start_ts, end_ts)
        if conditions is None:
            return

        classes = self._classify_hours(conditions)
        # Two passes over the same window, and they must stay two.  The shading
        # map is measured against physics that knows nothing about shading;
        # everything downstream is measured against physics that does.  Sharing
        # one pass between them would either blind the map to its own subject
        # or let the same shadow be subtracted twice.
        raw_interval, raw_beams = self._interval_power(
            index, conditions, apply_shading=False
        )
        per_interval, _shaded_beams = self._interval_power(index, conditions)
        hourly_physics = self._fold_hourly(per_interval)
        actual = {
            (row.ts_utc, row.string_id): row
            for row in self.store.hourly_range(start_ts, end_ts)
        }

        self._collect_shading(index, raw_interval, raw_beams, stats)

        for (hour, string_id), row in sorted(actual.items()):
            physics_kwh = hourly_physics.get(string_id, {}).get(hour)
            if physics_kwh is None:
                stats.skip("no_physics_row")
                continue
            if physics_kwh <= 0.0:
                # Legitimate at night, an anomaly in daylight -- and the two
                # were indistinguishable in the counter until now.
                stats.skip(
                    "night"
                    if row.quality == QUALITY_NIGHT
                    else "zero_physics_in_daylight"
                )
                continue
            if row.quality == QUALITY_NIGHT or row.energy_kwh is None:
                stats.skip("night" if row.quality == QUALITY_NIGHT else "no_energy")
                continue
            if row.value_kind == VALUE_LOWER_BOUND:
                stats.censored_hours += 1

            quality = assess(row.coverage, 90.0, row.value_kind)
            if not quality.usable_for_learning:
                stats.skip("low_coverage")
                self.store.add_exclusion(
                    hour, "low_coverage", string_id, f"coverage={row.coverage:.2f}"
                )
                continue

            weather, part = classes.get(hour, ("partly_cloudy", "midday"))
            observation = Observation(
                string_id=string_id,
                weather=weather,
                part=part,
                measured_kwh=row.energy_kwh,
                physics_kwh=physics_kwh,
                weight=quality.weight,
                value_kind=row.value_kind,
            )
            declined = self.model.decline_reason(observation)
            if declined is not None:
                stats.skip(declined)
                continue
            if self.model.observe(observation):
                stats.observations_used += 1
            else:  # pragma: no cover - the two agree by construction
                stats.skip("declined")

    @staticmethod
    def _fold_hourly(
        per_interval: dict[str, dict[int, float]],
    ) -> dict[str, dict[int, float]]:
        """Interval watts -> hourly kWh, per string."""
        out: dict[str, dict[int, float]] = {}
        for string_id, values in per_interval.items():
            folded: dict[int, float] = {}
            for ts, power in values.items():
                hour = floor_hour(ts)
                folded[hour] = folded.get(hour, 0.0) + power * INTERVAL_SECONDS / HOUR / 1000.0
            out[string_id] = folded
        return out

    def _collect_shading(
        self,
        index: pd.DatetimeIndex,
        potentials: dict[str, dict[int, float]],
        beams: dict[str, dict[int, float]],
        stats: LearnStats,
    ) -> None:
        """Store raw shading observations for the sky map to be fitted from.

        Deliberately not rasterised on write: a fixed grid built from thin data
        is a lossy commitment.  Raw azimuth/elevation pairs can be binned any
        way we like once there is a year of them.

        Each row carries the physics watts and the string's POA beam share
        (from the raw, unshaded run) for the joint fit's nuisance terms.
        Recorded at collect time because the weather rows are pruned on a far
        shorter leash than the observations.
        """
        solar_position = self.physics.solar_position(index)
        epochs = [
            int(value.timestamp()) - INTERVAL_SECONDS // 2 for value in index
        ]

        payload: list[tuple[Any, ...]] = []
        for string in self.plant.strings:
            series = potentials.get(string.string_id)
            if not series:
                continue
            beam_series = beams.get(string.string_id, {})
            rows = {
                int(row["ts_utc"]): row
                for row in self.store.fivemin_range(
                    string.string_id, epochs[0], epochs[-1] + INTERVAL_SECONDS
                )
            }
            for position, ts in enumerate(epochs):
                row = rows.get(ts)
                if row is None or row["power_mean_w"] is None:
                    continue
                if row["value_kind"] != VALUE_MEASURED or row["limit_binding"]:
                    continue
                if row["coverage"] < 0.8:
                    continue
                elevation = float(
                    solar_position["apparent_elevation"].iloc[position]
                )
                if elevation < SHADING_MIN_ELEVATION_DEG:
                    continue
                physics_w = series.get(ts)
                if not physics_w or physics_w <= 0:
                    continue
                ratio = float(row["power_mean_w"]) / physics_w
                # Five, not two: a string whose physics runs at two thirds
                # meets genuine cloud enhancement well above 2.0, and clipping
                # those moments would bias the joint fit's moment term low.
                # Truly broken sensors land orders of magnitude out, not here.
                if not 0.0 <= ratio <= 5.0:
                    continue
                beam = beam_series.get(ts)
                payload.append(
                    (
                        ts,
                        string.string_id,
                        float(solar_position["azimuth"].iloc[position]),
                        elevation,
                        ratio,
                        float(row["coverage"]),
                        float(physics_w),
                        beam if beam is not None and np.isfinite(beam) else None,
                    )
                )
        self.store.add_shading_obs(payload)
        stats.shading_observations = len(payload)

    def _learn_ghi_bias(
        self, start_ts: int, end_ts: int, stats: LearnStats
    ) -> None:
        """Compare each forecast issue against what the irradiance turned out to be.

        Truth is a measured GHI sensor when the user has one; otherwise the
        source's own shortest-horizon run for the same target hour.  The second
        is not perfect truth, but it isolates *horizon* error, which is what the
        bias buckets are supposed to correct.
        """
        rows = self.store.forecast_for_verification(
            start_ts, end_ts, self.plant.forecast_source, max_horizon_h=72
        )
        if not rows:
            return

        by_hour: dict[int, list[Any]] = {}
        for row in rows:
            by_hour.setdefault(int(row["ts_utc"]), []).append(row)

        measured = self._measured_ghi(start_ts, end_ts)
        measured_hourly: dict[int, float] = {}
        if measured is not None and not measured.empty:
            frame = measured.to_frame("ghi")
            frame["hour"] = (frame.index // HOUR) * HOUR
            measured_hourly = {
                int(hour): float(value)
                for hour, value in frame.groupby("hour")["ghi"].mean().items()
            }

        for hour, issues in by_hour.items():
            truth = measured_hourly.get(hour)
            if truth is None:
                nowcasts = [
                    issue
                    for issue in issues
                    if issue["horizon_h"] <= NOWCAST_MAX_HORIZON_H
                    and issue["ghi_wm2"] is not None
                ]
                if not nowcasts:
                    continue
                truth = float(max(nowcasts, key=lambda r: r["issued_at_utc"])["ghi_wm2"])
            if truth <= 5.0:
                continue

            hour_local = datetime.fromtimestamp(hour, tz=self._tz).hour
            for issue in issues:
                if issue["ghi_wm2"] is None:
                    continue
                horizon = float(issue["horizon_h"])
                if horizon < 0:
                    # Issued after the hour it describes: that is an analysis,
                    # not a forecast.  It makes a fine yardstick (above) but
                    # scoring it would flatter the short-horizon buckets with
                    # hindsight.
                    continue
                if horizon <= NOWCAST_MAX_HORIZON_H and truth == issue["ghi_wm2"]:
                    continue
                if self.ghi_bias.observe(
                    hour_local=hour_local,
                    horizon_h=horizon,
                    measured_ghi=truth,
                    forecast_ghi=float(issue["ghi_wm2"]),
                    weight=bias_weight(truth),
                ):
                    stats.bias_observations += 1

    # ------------------------------------------------------------------ #
    # scoring
    # ------------------------------------------------------------------ #

    def score(
        self, start_ts: int, end_ts: int, lead_time_h: float = 0.0
    ) -> dict[str, Any]:
        """WMAPE, nMAE and bias -- separately for uncensored and all hours.

        Only the uncensored figure describes model quality; the all-hours figure
        describes everyday usefulness.  Reporting a single number without saying
        which one it is, is how "78.6 % accuracy" ends up meaning nothing.
        """
        tally = _ScoreTally()
        self._tally(self.store.forecast_vs_actual(start_ts, end_ts, lead_time_h), tally)
        return {**self._scored(tally), "lead_time_h": lead_time_h}

    def score_day_ahead(self, days: int, now_ts: int) -> dict[str, Any]:
        """The same metrics, but against what we said the evening before.

        This is the figure the headline feature deserves: every published
        accuracy number until now compared an hour against the forecast issued
        minutes before it, which is a nowcast and flatters the model badly when
        the question being asked is "how much will tomorrow bring".

        Only *complete* local days count.  Half of today's production measured
        against a whole day of forecast would drag every window down for a
        reason that has nothing to do with forecast quality.
        """
        tally = _ScoreTally()
        for day_start, day_end, cutoff in self._day_ahead_windows(days, now_ts):
            self._tally(
                self.store.forecast_vs_actual_before(day_start, day_end, cutoff), tally
            )

        result = self._scored(tally)
        result["issue_hour_local"] = DAY_AHEAD_ISSUE_HOUR_LOCAL
        if result["days_scored"] < MIN_SCORED_DAYS:
            # Silent about the number, honest about the basis: the counts stay
            # as they are so the attributes can show how far off publishing is.
            blank = dict.fromkeys(
                ("wmape", "nmae", "bias", "mae_kwh", "daily_bias_kwh")
            )
            for bucket in ("uncensored", "all_hours"):
                result[bucket] = {**result[bucket], **blank}
        return result

    def day_ahead_cutoff(self, day_start_ts: int) -> int:
        """The instant a local day's forecast is judged against.

        The evening before, at :data:`DAY_AHEAD_ISSUE_HOUR_LOCAL` local time.
        Anything issued later knows more than the reader did and would flatter
        the score.
        """
        day = datetime.fromtimestamp(day_start_ts, tz=self._tz).date()
        prev = day - timedelta(days=1)
        return int(
            datetime(
                prev.year,
                prev.month,
                prev.day,
                DAY_AHEAD_ISSUE_HOUR_LOCAL,
                tzinfo=self._tz,
            ).timestamp()
        )

    def _day_ahead_windows(
        self, days: int, now_ts: int
    ) -> Iterable[tuple[int, int, int]]:
        """``(day_start, day_end, cutoff)`` per complete local day, oldest first.

        Built from local calendar dates rather than by subtracting 86400, so the
        two days a year that are not twenty-four hours long still line up with
        the days a reader sees on the dashboard.
        """
        today = datetime.fromtimestamp(now_ts, tz=self._tz).date()
        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            nxt = day + timedelta(days=1)
            start = int(
                datetime(day.year, day.month, day.day, tzinfo=self._tz).timestamp()
            )
            yield (
                start,
                int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=self._tz).timestamp()),
                self.day_ahead_cutoff(start),
            )

    def _tally(self, rows: Iterable[Any], tally: "_ScoreTally") -> None:
        """Fold paired hours into a running tally, in place."""
        for row in rows:
            actual = row["energy_kwh"]
            predicted = row["potential_kwh"]
            if actual is None or predicted is None:
                continue
            if row["quality"] in (QUALITY_NIGHT, "missing"):
                continue
            day = datetime.fromtimestamp(int(row["ts_utc"]), tz=self._tz).strftime(
                "%Y-%m-%d"
            )
            tally.every.append((predicted, actual))
            tally.daily_all.setdefault(day, [0.0, 0.0])
            tally.daily_all[day][0] += predicted
            tally.daily_all[day][1] += actual
            if row["value_kind"] == VALUE_MEASURED and not row["curtailed_fraction"]:
                tally.uncensored.append((predicted, actual))
                tally.daily_uncensored.setdefault(day, [0.0, 0.0])
                tally.daily_uncensored[day][0] += predicted
                tally.daily_uncensored[day][1] += actual

    def _scored(self, tally: "_ScoreTally") -> dict[str, Any]:
        nameplate = self._total_kwp()
        return {
            "uncensored": _metrics(
                tally.uncensored, tally.daily_uncensored, nameplate
            ),
            "all_hours": _metrics(tally.every, tally.daily_all, nameplate),
            "hours_scored": len(tally.every),
            "hours_uncensored": len(tally.uncensored),
            "days_scored": len(tally.daily_all),
        }

    def _total_kwp(self) -> float:
        total = 0.0
        now = int(datetime.now(tz=timezone.utc).timestamp())
        for string in self.plant.strings:
            segment = self.geometry_at(string.string_id, now)
            if segment:
                total += segment.kwp
        return total

    def monthly_weights(self) -> list[float]:
        """Clear-sky seasonality of the plant, cached.

        Weighted by the strings' own geometry, so the seasonal extrapolation is
        right for a steep winter-facing balcony as well as a flat roof.
        """
        if self._monthly_weights is not None:
            return self._monthly_weights

        now = int(datetime.now(tz=timezone.utc).timestamp())
        weighted = np.zeros(12)
        total_kwp = 0.0
        for string in self.plant.strings:
            segment = self.geometry_at(string.string_id, now)
            if segment is None:
                continue
            share = np.array(
                self.physics.monthly_clearsky_share(
                    segment.tilt_deg, segment.azimuth_deg
                )
            )
            weighted += share * segment.kwp
            total_kwp += segment.kwp
        if total_kwp <= 0:
            self._monthly_weights = [1.0 / 12.0] * 12
        else:
            weighted /= weighted.sum()
            self._monthly_weights = [float(value) for value in weighted]
        return self._monthly_weights


def _metrics(
    pairs: Sequence[tuple[float, float]],
    daily: Mapping[str, Sequence[float]],
    nameplate_kwp: float,
) -> dict[str, float | None]:
    """Two granularities in one dict, which the callers have to keep straight.

    ``wmape`` and ``daily_bias_kwh`` are about **days**; ``bias``, ``mae_kwh``
    and ``nmae`` are means over **hours**.  Mixing them up turns "0.4 kWh too
    high" from a daily statement into an hourly one and back, so anything that
    publishes these has to say which is which.
    """
    if not pairs:
        return {
            "wmape": None,
            "nmae": None,
            "bias": None,
            "mae_kwh": None,
            "daily_bias_kwh": None,
            "n": 0,
            "days": len(daily),
        }

    abs_error = sum(abs(p - a) for p, a in pairs)
    signed_error = sum(p - a for p, a in pairs)

    daily_actual = sum(values[1] for values in daily.values())
    daily_abs = sum(abs(values[0] - values[1]) for values in daily.values())
    daily_signed = sum(values[0] - values[1] for values in daily.values())
    wmape = daily_abs / daily_actual if daily_actual > 0 else None

    nmae = abs_error / len(pairs) / nameplate_kwp if nameplate_kwp > 0 else None

    return {
        "wmape": round(wmape, 4) if wmape is not None else None,
        "nmae": round(nmae, 5) if nmae is not None else None,
        "bias": round(signed_error / len(pairs), 5),
        "mae_kwh": round(abs_error / len(pairs), 5),
        # Per day, and signed: "typically half a kilowatt-hour too optimistic"
        # is a sentence somebody can act on, which the hourly mean above is not.
        "daily_bias_kwh": round(daily_signed / len(daily), 5) if daily else None,
        "n": len(pairs),
        "days": len(daily),
    }
