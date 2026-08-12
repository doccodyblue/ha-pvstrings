"""Turning raw shading observations into a correction.

The collector records, for every five-minute interval a string was measured
cleanly, the triple (sun azimuth, sun elevation, actual / physics).  This module
folds those into a map over the sky and hands back a factor the physics chain
can multiply onto the effective irradiance.

Three choices carry the whole design:

**A grid over the sun's position, not over the clock.**  A chimney shadow sits
at a fixed place in the sky.  The clock time it arrives at drifts by an hour
twice a year and by weeks across the seasons; the azimuth and elevation do not.
A map indexed by sun position is learned once and stays correct.

**An upper envelope per cell, not a mean.**  A cell accumulates observations
from many different days.  Shading recurs on every one of them; curtailment, a
passing cloud the irradiance source did not see, and a dirty morning do not.
The mean of that mixture is dragged below the truth by whatever else went wrong
that day, and it keeps sinking as more bad days arrive.  A high quantile
recovers the behaviour the string shows when nothing else is in the way, which
is exactly what "how much of the sky can this panel see" means.

**Foliage, but only where the data asks for it.**  The sun visits every cell
exactly twice a year, once while the days lengthen and once while they
shorten.  For a wall those two visits are identical.  For a deciduous tree they
are not: on the way up the branches are bare, on the way down they are in full
leaf, and a map indexed on sun position alone averages the two into a shadow
that is wrong in both halves of the year.  So each cell is split in two
whenever -- and only whenever -- its own observations say the halves disagree.
A site shaded by buildings keeps the pooled cell and its full weight of
evidence; a site shaded by a tree gets the seasonal split it needs, without
anybody having to describe their garden to a config flow.

**The best cell defines unshaded.**  Shading only ever subtracts, so the map is
normalised against the sun positions where the string does best rather than
against its own average.  That keeps the map to a pure shape -- how the string
varies across the sky -- and leaves its overall level to the per-string effect
that the log-ratio model already learns.  Without that split the two layers
would both chase the same error and cancel each other in a slow oscillation.

Cells nobody has observed yet return exactly 1.0.  In August the sun never
visits the winter cells, and inventing a value for them from an adjacent
summer one would be a guess dressed up as a measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import numpy as np

#: Grid resolution.  Ten degrees of azimuth is about forty minutes of a summer
#: afternoon -- fine enough to place a roof edge, coarse enough that hourly
#: backfilled observations (which smear over fifteen degrees) still land
#: mostly in the right cell.
AZIMUTH_BIN_DEG = 10.0
ELEVATION_BIN_DEG = 5.0

#: Below this a cell has no opinion of its own and falls back to its
#: neighbours.  Four observations of the same sun position necessarily come
#: from four different days, so even this is not a single day's accident.
MIN_OBSERVATIONS = 4.0

#: Which quantile of a cell's ratios counts as "nothing else in the way".
UPPER_QUANTILE = 0.80

#: Shrinkage towards no-correction.  A cell needs roughly this many
#: observations before it is trusted at half its face value.
SHRINK_K = 6.0

#: The reference cell is this quantile of all cell values rather than the
#: outright maximum, so that one lucky cell cannot declare every other part of
#: the sky shaded.
REFERENCE_QUANTILE = 0.90

#: Only cells with at least this much evidence may define "unshaded".
#: Shrinkage drags a thin cell towards no-correction, so a reference taken
#: over shrunk values lets the emptiest corners of the sky set the standard --
#: and then a perfectly clear string whose physics runs a little optimistic
#: comes out shaded everywhere, swallowing level that belongs to the
#: per-string effect.
REFERENCE_MIN_OBSERVATIONS = 12.0

#: A correction is never allowed past these.  Total darkness is a broken
#: sensor, not a shadow, and the map may never amplify.
MIN_FACTOR = 0.05
MAX_FACTOR = 1.0

#: Cells within this many bins may stand in for an unobserved one.
NEIGHBOUR_RADIUS = 1

#: How far apart the two halves of the year must be, in log space, before a
#: cell is allowed to split.  0.18 is about twenty percent -- comfortably more
#: than the scatter between two samples of the same sky, comfortably less than
#: the difference a leaf canopy makes.
SEASON_SPLIT_THRESHOLD = 0.18

#: Each half needs observations from at least this many *different days*
#: before a split is considered.  Counting observations instead would compare
#: quantities that are not alike: eight five-minute intervals are one
#: afternoon, while eight backfilled hours are eight separate days.  A split
#: decided by a single day on each side is a split decided by the weather.
SEASON_MIN_DAYS = 10

#: Observations lose half their say over this span.  Trees grow, sheds go up,
#: and a panel that was moved reports against new geometry from that day on --
#: the map has to be able to forget.  Long enough that last winter still
#: informs this one, since nothing else can.
RECENCY_HALFLIFE_DAYS = 730.0

#: Day of year at the two solstices, close enough for a seasonal split.
_JUNE_SOLSTICE_DOY = 172
_DECEMBER_SOLSTICE_DOY = 355

ASCENDING = 0
DESCENDING = 1


def azimuth_bin(azimuth_deg: float) -> int:
    return int(math.floor((azimuth_deg % 360.0) / AZIMUTH_BIN_DEG))


def elevation_bin(elevation_deg: float) -> int:
    return int(math.floor(elevation_deg / ELEVATION_BIN_DEG))


def season_half(ts_utc: float) -> int:
    """Which side of the year an observation sits on.

    Declination rises from the December solstice to the June one everywhere on
    Earth, so this needs no hemisphere and no latitude -- it is a property of
    the date alone.
    """
    day = datetime.fromtimestamp(ts_utc, timezone.utc).timetuple().tm_yday
    ascending = day >= _DECEMBER_SOLSTICE_DOY or day < _JUNE_SOLSTICE_DOY
    return ASCENDING if ascending else DESCENDING


def recency_weight(ts_utc: float, now_ts: float) -> float:
    """Exponential decay, floored so old evidence still beats none at all."""
    age_days = max(0.0, (now_ts - ts_utc) / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


@dataclass(frozen=True, slots=True)
class Sample:
    """One observation, before it has been folded into a cell."""

    value: float  # log(actual / physics)
    weight: float
    half: int
    ts_utc: float

    def aged(self, reference_ts: float) -> "Sample":
        return Sample(
            value=self.value,
            weight=self.weight * recency_weight(self.ts_utc, reference_ts),
            half=self.half,
            ts_utc=self.ts_utc,
        )


@dataclass(frozen=True, slots=True)
class Cell:
    """One patch of sky, as seen by one string."""

    value: float  # log-space, before normalisation
    n: float

    @property
    def shrunk(self) -> float:
        return self.value * self.n / (self.n + SHRINK_K)


@dataclass(slots=True)
class ShadingMap:
    """One string's view of the sky."""

    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    #: Only the cells whose two halves of the year genuinely disagree.
    seasonal: dict[tuple[int, int, int], Cell] = field(default_factory=dict)
    reference: float = 0.0

    # -- fitting --------------------------------------------------------- #

    @classmethod
    def fit(
        cls,
        observations: Iterable[tuple[float, float, float, float, float]],
        now_ts: float | None = None,
    ) -> "ShadingMap":
        """Build a map from ``(ts, azimuth, elevation, ratio, weight)`` rows."""
        samples: dict[tuple[int, int], list[Sample]] = {}
        newest = 0.0
        for ts_utc, azimuth, elevation, ratio, weight in observations:
            if ratio <= 0.0 or weight <= 0.0 or elevation < 0.0:
                continue
            newest = max(newest, ts_utc)
            key = (azimuth_bin(azimuth), elevation_bin(elevation))
            samples.setdefault(key, []).append(
                Sample(
                    value=math.log(ratio),
                    weight=weight,
                    half=season_half(ts_utc),
                    ts_utc=ts_utc,
                )
            )
        if not samples:
            return cls()

        # Age is measured against the newest observation rather than the wall
        # clock, so refitting an old database does not silently erase it.
        reference_ts = now_ts if now_ts is not None else newest

        cells: dict[tuple[int, int], Cell] = {}
        seasonal: dict[tuple[int, int, int], Cell] = {}
        # Aged in place, one cell at a time.  Building a second full copy of
        # every observation first doubles peak memory for no benefit, and in
        # steady state "every observation" is a six-figure number.
        for key, raw in samples.items():
            rows = [sample.aged(reference_ts) for sample in raw]
            pooled = _cell_from(rows)
            if pooled is None:
                continue
            cells[key] = pooled
            split = _seasonal_split(rows)
            if split is not None:
                seasonal[(key[0], key[1], ASCENDING)] = split[ASCENDING]
                seasonal[(key[0], key[1], DESCENDING)] = split[DESCENDING]

        if not cells:
            return cls()
        return cls(
            cells=cells,
            seasonal=seasonal,
            reference=_reference_level(cells),
        )

    # -- lookup ---------------------------------------------------------- #

    def factor(
        self,
        azimuth_deg: float,
        elevation_deg: float,
        ts_utc: float | None = None,
    ) -> float:
        if not self.cells or elevation_deg < 0.0:
            return 1.0
        key = (azimuth_bin(azimuth_deg), elevation_bin(elevation_deg))
        cell = None
        if ts_utc is not None and self.seasonal:
            cell = self.seasonal.get((key[0], key[1], season_half(ts_utc)))
        if cell is None:
            cell = self.cells.get(key)
        if cell is None:
            cell = self._from_neighbours(key)
        if cell is None:
            return 1.0
        return _clamp(math.exp(cell.shrunk - self.reference))

    def factors(
        self,
        azimuth_deg: Sequence[float],
        elevation_deg: Sequence[float],
        ts_utc: Sequence[float] | None = None,
    ) -> np.ndarray:
        stamps = ts_utc if ts_utc is not None else [None] * len(azimuth_deg)
        return np.array(
            [
                self.factor(float(azimuth), float(elevation), stamp)
                for azimuth, elevation, stamp in zip(
                    azimuth_deg, elevation_deg, stamps
                )
            ],
            dtype=float,
        )

    def _from_neighbours(self, key: tuple[int, int]) -> Cell | None:
        """Borrow from adjacent sky when a cell has nothing of its own.

        Only immediate neighbours, and only ever one hop: a shadow edge is
        sharp, and reaching further would smear it across sky the string can
        see perfectly well.
        """
        azimuth, elevation = key
        found: list[Cell] = []
        for d_azimuth in range(-NEIGHBOUR_RADIUS, NEIGHBOUR_RADIUS + 1):
            for d_elevation in range(-NEIGHBOUR_RADIUS, NEIGHBOUR_RADIUS + 1):
                if d_azimuth == 0 and d_elevation == 0:
                    continue
                neighbour = self.cells.get(
                    ((azimuth + d_azimuth) % _AZIMUTH_BINS, elevation + d_elevation)
                )
                if neighbour is not None:
                    found.append(neighbour)
        if not found:
            return None
        total = sum(cell.n for cell in found)
        value = sum(cell.value * cell.n for cell in found) / total
        return Cell(value=value, n=total / len(found))

    # -- reporting ------------------------------------------------------- #

    @property
    def observed_cells(self) -> int:
        return len(self.cells)

    def summary(self, limit: int = 12) -> dict[str, object]:
        """The most-shaded corners of the sky, for diagnostics."""
        # Sorted by loss, worst first -- the same order the name promises.
        ranked = sorted(
            (
                (
                    f"{key[0] * AZIMUTH_BIN_DEG:.0f}-{(key[0] + 1) * AZIMUTH_BIN_DEG:.0f}"
                    f"|{key[1] * ELEVATION_BIN_DEG:.0f}-"
                    f"{(key[1] + 1) * ELEVATION_BIN_DEG:.0f}",
                    round(
                        (1.0 - _clamp(math.exp(cell.shrunk - self.reference))) * 100,
                        1,
                    ),
                    round(cell.n, 1),
                )
                for key, cell in self.cells.items()
            ),
            key=lambda row: -row[1],
        )
        return {
            "cells": len(self.cells),
            "most_shaded": [
                {"sector": sector, "shading_pct": loss, "n": n}
                for sector, loss, n in ranked[:limit]
            ],
        }


@dataclass(slots=True)
class ShadingModel:
    """Every string's map, keyed by string id."""

    maps: dict[str, ShadingMap] = field(default_factory=dict)

    @classmethod
    def fit(
        cls,
        rows_by_string: Mapping[
            str, Iterable[tuple[float, float, float, float, float]]
        ],
        now_ts: float | None = None,
    ) -> "ShadingModel":
        return cls(
            maps={
                string_id: ShadingMap.fit(rows, now_ts)
                for string_id, rows in rows_by_string.items()
            }
        )

    def factor(
        self,
        string_id: str,
        azimuth_deg: float,
        elevation_deg: float,
        ts_utc: float | None = None,
    ) -> float:
        found = self.maps.get(string_id)
        return found.factor(azimuth_deg, elevation_deg, ts_utc) if found else 1.0

    def factors(
        self,
        string_id: str,
        azimuth_deg: Sequence[float],
        elevation_deg: Sequence[float],
        ts_utc: Sequence[float] | None = None,
    ) -> np.ndarray:
        found = self.maps.get(string_id)
        if found is None:
            return np.ones(len(azimuth_deg), dtype=float)
        return found.factors(azimuth_deg, elevation_deg, ts_utc)

    def summary(self) -> dict[str, object]:
        return {
            string_id: found.summary()
            for string_id, found in sorted(self.maps.items())
            if found.observed_cells
        }


_AZIMUTH_BINS = int(360.0 / AZIMUTH_BIN_DEG)


def _clamp(factor: float) -> float:
    return min(max(factor, MIN_FACTOR), MAX_FACTOR)


def _weighted_quantile(samples: list[tuple[float, float]], quantile: float) -> float:
    """Weighted quantile of ``(value, weight)`` pairs.

    Plain ``numpy.quantile`` ignores the weights, and the weights here are
    interval coverage -- a half-covered interval genuinely knows half as much.
    """
    ordered = sorted(samples)
    total = sum(weight for _value, weight in ordered)
    target = total * quantile
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


def _reference_level(cells: Mapping[tuple[int, int], Cell]) -> float:
    """The log-ratio a string reaches where nothing is in the way.

    Taken over the cells' raw values, not their shrunk ones, and only over
    cells with enough evidence to have an opinion.  A thin cell's shrunk value
    says "we do not know", which is not the same as "the sky is clear here",
    and must never become the yardstick everything else is measured against.
    """
    confident = [
        cell for cell in cells.values() if cell.n >= REFERENCE_MIN_OBSERVATIONS
    ]
    population = confident or list(cells.values())
    values = sorted(cell.value for cell in population)
    if len(values) == 1:
        return values[0]
    position = REFERENCE_QUANTILE * (len(values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _cell_from(rows: Sequence[Sample]) -> Cell | None:
    total = sum(row.weight for row in rows)
    if total < MIN_OBSERVATIONS:
        return None
    return Cell(
        value=_weighted_quantile(
            [(row.value, row.weight) for row in rows], UPPER_QUANTILE
        ),
        n=total,
    )


def _seasonal_split(rows: Sequence[Sample]) -> dict[int, Cell] | None:
    """Two cells instead of one, but only if the year's halves disagree.

    Returning ``None`` is the common and desirable answer.  Splitting a cell
    halves the evidence behind each side, so it has to earn its keep: both
    halves need their own observations, and the gap between them has to be
    wider than the scatter of the sky itself.
    """
    halves = {
        ASCENDING: [row for row in rows if row.half == ASCENDING],
        DESCENDING: [row for row in rows if row.half == DESCENDING],
    }
    if any(
        len({int(row.ts_utc // 86400) for row in side}) < SEASON_MIN_DAYS
        for side in halves.values()
    ):
        return None

    cells = {half: _cell_from(side) for half, side in halves.items()}
    if any(cell is None for cell in cells.values()):
        return None
    if abs(cells[ASCENDING].value - cells[DESCENDING].value) < SEASON_SPLIT_THRESHOLD:
        return None
    return cells
