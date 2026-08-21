"""Reconstructing shading observations from history nobody kept for us.

A fresh install knows nothing about where the shadows fall, and learning it
from live five-minute data takes a full turn of the seasons: the sun does not
visit the winter part of the sky until winter.  Meanwhile Home Assistant has
usually been recording the very same inverters for months, as hourly
long-term statistics that survive every purge.

This module turns that history into shading observations.  For each past hour
it takes the string's mean power, the irradiance the site actually stood in --
from a reanalysis archive, not from the user's own sensor, which may not have
existed yet and may not be trustworthy anyway -- and runs the same physics the
live path runs.  The ratio of the two is exactly what the collector would have
written at the time.

Two honest limitations, both of which the fitter downstream is built to absorb:

*Hourly resolution smears the sun.*  In an hour the sun moves about fifteen
degrees of azimuth, so a backfilled observation is placed at the midpoint of an
arc rather than at a point.  Shadow edges therefore come out softer than the
live collector will eventually draw them.

*Reanalysis is not a measurement.*  On any given hour it may be well off.  It
is unbiased across many days at the same sun position, though, and the fitter
takes an upper envelope over exactly that population, so the errors that matter
are the ones that would correlate with sun position -- and those are small.

Backfilled rows are marked by their weight so they never outvote a real
five-minute observation of the same patch of sky.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .config import GeometrySegment
from .physics import PhysicsEngine, to_index

_LOGGER = logging.getLogger(__name__)

HOUR = 3600

#: A backfilled hour is worth this much next to a clean five-minute interval.
#: It is a real observation of a real day, but placed with an hour's worth of
#: azimuth smear and driven by reanalysis rather than by measurement.
BACKFILL_WEIGHT = 0.35

#: Below this the physics is too small for a ratio to mean anything -- dividing
#: a handful of watts by a handful of watts amplifies noise without limit.
#: Scaled by nameplate as well, because "a handful" means something different
#: to a 300 Wp balcony panel and to a 30 kWp roof.
MIN_PHYSICS_W = 25.0
MIN_PHYSICS_FRACTION = 0.02

#: Sun elevations below this are excluded for the same reason the live
#: collector excludes them.
MIN_ELEVATION_DEG = 8.0

#: A backfilled observation is stamped at the middle of its hour, plus one
#: second.  The bare midpoint is 1800 s past the hour, which is a multiple of
#: 300 and therefore a perfectly valid five-minute interval start -- and
#: ``shading_obs`` is keyed on (ts_utc, string_id) with an upsert, so every
#: backfilled row would silently overwrite a real measurement of that slot.
#: One second moves it off the grid and costs nothing: the sun does not
#: measurably move in a second.
MIDPOINT_OFFSET_S = HOUR // 2 + 1

#: Ratios outside this are not shading, they are a configuration error or a
#: unit mismatch, and letting them into the fitter would poison a whole cell.
#: The lower bound is emphatically not zero: an inverter reporting kilowatts
#: against physics in watts lands near 0.001, which the fitter would happily
#: accept and turn into a sky that is shut for good.  No real shadow leaves a
#: string at two percent of its potential across a whole hour.
MIN_RATIO = 0.02
MAX_RATIO = 2.5


@dataclass(frozen=True, slots=True)
class BackfillResult:
    rows: list[tuple[Any, ...]]
    hours_considered: int
    hours_used: int
    strings: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours_considered": self.hours_considered,
            "hours_used": self.hours_used,
            "observations": len(self.rows),
            "per_string": self.strings,
        }


def shading_rows_from_history(
    physics: PhysicsEngine,
    power_by_string: Mapping[str, Mapping[int, float]],
    irradiance: Mapping[int, tuple[float | None, float | None, float | None]],
    geometry_at: Any,
    temperature: Mapping[int, float] | None = None,
    wind: Mapping[int, float] | None = None,
    efficiency_of: Any = None,
    mount_of: Any = None,
) -> BackfillResult:
    """Rebuild shading observations from hourly history.

    ``power_by_string`` maps a string id to {hour start -> mean watts}.
    ``irradiance`` maps an hour start to (ghi, dni, dhi).  ``geometry_at`` is
    called as ``geometry_at(string_id, hour)`` so that a mount which was moved
    mid-history is honoured rather than smeared.
    """
    hours = sorted(irradiance)
    if not hours:
        return BackfillResult([], 0, 0, {})

    rows: list[tuple[Any, ...]] = []
    per_string: dict[str, int] = {}
    used: set[int] = set()

    for string_id, series in sorted(power_by_string.items()):
        common = [hour for hour in hours if hour in series]
        if not common:
            continue
        produced = _rows_for_string(
            physics=physics,
            string_id=string_id,
            hours=common,
            power=series,
            irradiance=irradiance,
            geometry_at=geometry_at,
            temperature=temperature or {},
            wind=wind or {},
            efficiency=(efficiency_of(string_id) if efficiency_of else 0.96),
            mount=(mount_of(string_id) if mount_of else "open_rack"),
        )
        if produced:
            rows.extend(produced)
            per_string[string_id] = len(produced)
            used.update(row[0] for row in produced)

    return BackfillResult(
        rows=rows,
        hours_considered=len(hours),
        hours_used=len(used),
        strings=per_string,
    )


def _rows_for_string(
    physics: PhysicsEngine,
    string_id: str,
    hours: Sequence[int],
    power: Mapping[int, float],
    irradiance: Mapping[int, tuple[float | None, float | None, float | None]],
    geometry_at: Any,
    temperature: Mapping[int, float],
    wind: Mapping[int, float],
    efficiency: float,
    mount: str,
) -> list[tuple[Any, ...]]:
    grouped = _group_by_geometry(string_id, hours, geometry_at)
    rows: list[tuple[Any, ...]] = []
    rejected = 0

    for segment, segment_hours in grouped:
        floor_w = max(MIN_PHYSICS_W, MIN_PHYSICS_FRACTION * segment.kwp * 1000.0)
        # Evaluate at the middle of the hour: the mean power over an hour
        # belongs to the mean sun position over that hour, not to its start.
        midpoints = [hour + MIDPOINT_OFFSET_S for hour in segment_hours]
        index = to_index(midpoints)
        ghi = pd.Series(
            [_component(irradiance, hour, 0) for hour in segment_hours], index=index
        )
        dni = pd.Series(
            [_component(irradiance, hour, 1) for hour in segment_hours], index=index
        )
        dhi = pd.Series(
            [_component(irradiance, hour, 2) for hour in segment_hours], index=index
        )
        result = physics.run(
            index,
            segment,
            ghi=ghi,
            dni=dni,
            dhi=dhi,
            temp_air=pd.Series(
                [temperature.get(hour, 15.0) for hour in segment_hours], index=index
            ),
            wind_speed=pd.Series(
                [wind.get(hour, 1.5) for hour in segment_hours], index=index
            ),
            system_efficiency=efficiency,
            mount_type=mount,
        )
        position = physics.solar_position(index)
        physics_w = result.dc_power_w.to_numpy()
        elevation = position["apparent_elevation"].to_numpy()
        azimuth = position["azimuth"].to_numpy()
        beam_values = result.beam_share.to_numpy()

        for offset, hour in enumerate(segment_hours):
            if elevation[offset] < MIN_ELEVATION_DEG:
                continue
            expected = float(physics_w[offset])
            if not math.isfinite(expected) or expected < floor_w:
                continue
            actual = float(power[hour])
            if actual < 0.0:
                continue
            ratio = actual / expected
            if not MIN_RATIO <= ratio <= MAX_RATIO:
                rejected += 1
                continue
            # POA beam share from the same physics run, same measure as the
            # live collector.
            beam = float(beam_values[offset])
            beam = beam if math.isfinite(beam) else None
            rows.append(
                (
                    int(hour + MIDPOINT_OFFSET_S),
                    string_id,
                    float(azimuth[offset]),
                    float(elevation[offset]),
                    ratio,
                    BACKFILL_WEIGHT,
                    expected,
                    beam,
                )
            )
    if rejected and rejected > len(rows):
        _LOGGER.warning(
            "pvstrings: %s discarded %s of %s backfilled hours as out-of-range "
            "ratios -- check the power sensor's unit",
            string_id,
            rejected,
            rejected + len(rows),
        )
    return rows


def _group_by_geometry(
    string_id: str, hours: Sequence[int], geometry_at: Any
) -> list[tuple[GeometrySegment, list[int]]]:
    grouped: list[tuple[GeometrySegment, list[int]]] = []
    for hour in hours:
        segment = geometry_at(string_id, hour)
        if segment is None:
            continue
        if grouped and grouped[-1][0] == segment:
            grouped[-1][1].append(hour)
        else:
            grouped.append((segment, [hour]))
    return grouped


def _component(
    irradiance: Mapping[int, tuple[float | None, float | None, float | None]],
    hour: int,
    position: int,
) -> float:
    value = irradiance.get(hour, (None, None, None))[position]
    return float("nan") if value is None else float(value)


#: Any epoch value past this is milliseconds, not seconds -- as seconds it
#: would be the year 5138.
_MILLISECOND_THRESHOLD = 1e11


def hourly_series(statistics: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """Home Assistant statistic rows -> {hour start (UTC seconds) -> mean}.

    Rows without a mean are dropped rather than zero-filled: an hour the
    recorder has no mean for is an hour we know nothing about, and calling it
    zero would invent a shadow that was never there.

    The timestamp arrives in one of three shapes depending on which door you
    came through -- the recorder's own API hands out epoch *seconds*, the
    WebSocket API converts to *milliseconds* for the frontend, and older
    releases returned a ``datetime``.  Guessing wrong is silent: every hour
    lands in a bucket that matches no irradiance row and the backfill simply
    produces nothing.
    """
    out: dict[int, float] = {}
    for row in statistics:
        mean = row.get("mean")
        if mean is None:
            continue
        start = row.get("start")
        if start is None:
            continue
        if hasattr(start, "timestamp"):
            seconds = float(start.timestamp())
        else:
            seconds = float(start)
            if seconds > _MILLISECOND_THRESHOLD:
                seconds /= 1000.0
        out[int(seconds // HOUR * HOUR)] = float(mean)
    return out
