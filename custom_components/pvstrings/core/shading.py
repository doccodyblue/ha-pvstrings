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

**Shade is a difference between siblings, not an absolute.**  On a plant with
two or more strings the map is fitted jointly: every observation is read as
``level(string) + moment(timestamp) + shade(cell)`` in log space.  The moment
term is whatever all strings saw at once -- a cloud edge the irradiance sensor
missed, enhancement off a bright cumulus, physics running low across the site
-- and it cancels between siblings, because only the shade is specific to one
string's patch of sky.  This is what lets a shadow be measured on a string
whose every cell beats physics: against an absolute reference such a string
looks flawless, against its own siblings the missing morning is plain.  The
level and moment terms are estimated and thrown away; the map keeps only the
shape, same as it always did.  A single-string plant has no siblings to
difference against and keeps the absolute fit with its capped reference.

Within a cell the shade is the weighted *median* of those residuals, each one
first inverted to the clear-day loss it implies and weighted by the beam share
of its moment: shade only ever costs beam, so an overcast observation says
nothing about the obstacle and must not be allowed to vote it away.  The same
beam share scales the correction at forecast time -- a shadow learned from
clear mornings is not subtracted from an overcast one, and a loss observed at
half beam is not stored as if the day had been clear.

Cells nobody has observed yet return exactly 1.0.  In August the sun never
visits the winter cells, and inventing a value for them from an adjacent
summer one would be a guess dressed up as a measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

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
#: the sky shaded.  Weighted by evidence rather than filtered by it: filtering
#: was tried and removed, because it can only lower the estimate of "unshaded",
#: and a reference that lands inside a shadow normalises that shadow away.
REFERENCE_QUANTILE = 0.90

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

#: Rounds of the alternating joint fit.  Two strings sharing one obstacle need
#: a second pass -- in the first, each one's shadow still leaks into the moment
#: term through the other.  Three is one more than that needs, fixed rather
#: than convergence-tested so a refit is deterministic and cheap to reason
#: about.
FIT_ROUNDS = 3

#: Beam share assumed for observations recorded before the collector stored
#: one.  Half says "we do not know whether this moment carried beam", which
#: lets old rows keep contributing without letting them outvote rows that know.
LEGACY_BEAM_WEIGHT = 0.5

#: Co-observed five-minute epochs needed before the differential fit is
#: trusted at all.  Two strings whose histories never overlap have no moments
#: in common, and "differential" over disjoint data is just the absolute fit
#: wearing a better name -- worse, actually, because single-sighted weather
#: residuals would be read as shade.  Fifty five-minute epochs is about four
#: hours of shared daylight: enough to anchor the moment term, small enough
#: that a freshly added sibling starts differencing on its first afternoon.
MIN_JOINT_EPOCHS = 50

#: How the model was fitted; decides whether the correction scales with
#: forecast clearness at apply time.  An absolute map's cell is a mixed-weather
#: envelope that already averages overcast in, so scaling it again would
#: subtract the shadow twice from a grey day.
METHOD_ABSOLUTE = "absolute"
METHOD_DIFFERENTIAL = "differential"

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
    #: The raw measured-over-physics envelope, kept purely for display.  In the
    #: differential fit ``value`` is a residual with level and moment removed,
    #: and a residual alone cannot answer "what did this string actually do
    #: here" -- which is the question the diagnosed grid exists for.
    raw: float | None = None

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
    #: Whether this map's cells are clear-day shades from the joint fit
    #: (scaled by beam at apply time) or absolute mixed-weather envelopes
    #: (applied as-is).  Per map, not per model: one plant can hold both --
    #: a string with no epochs in common with its siblings keeps an absolute
    #: map inside an otherwise differential model.
    differential: bool = False

    # -- fitting --------------------------------------------------------- #

    @classmethod
    def fit(
        cls,
        observations: Iterable[tuple[float, float, float, float, float]],
        now_ts: float | None = None,
    ) -> "ShadingMap":
        """Build a map from ``(ts, azimuth, elevation, ratio, weight)`` rows.

        Rows may carry the newer trailing columns (physics watts, clearness);
        this absolute fit ignores them -- its envelope semantics predate them
        and must stay exactly reproducible for single-string plants.
        """
        samples: dict[tuple[int, int], list[Sample]] = {}
        newest = 0.0
        for row in observations:
            ts_utc, azimuth, elevation, ratio, weight = row[:5]
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

    def grid(self) -> list[dict[str, object]]:
        """Every observed cell as ``(azimuth, elevation, loss)``.

        The map's shape is the interesting thing about it and a ranked list of
        the worst six sectors hides that completely -- you cannot see a gable
        edge or the outline of a tree in a top-six.  Handing out the raw cells
        lets a card draw the sky instead of tabulating it.

        ``season`` is ``None`` for the pooled value and names the half of the
        year for the split ones, so a card can pick the same cell the forecast
        picks: the seasonal entry when one exists for today, the pooled one
        otherwise.
        """
        def entry(key: tuple[int, int], cell: Cell, season: str | None) -> dict:
            return {
                "az": key[0] * AZIMUTH_BIN_DEG,
                "el": key[1] * ELEVATION_BIN_DEG,
                "loss": round(
                    (1.0 - _clamp(math.exp(cell.shrunk - self.reference))) * 100, 1
                ),
                # What the string actually did here, before any normalisation:
                # measured over physics, upper envelope.  Without it a flat map
                # is unreadable -- there is no way to tell "nothing is in the
                # way" from "everything is equally in the way", and those want
                # opposite fixes.
                "ratio": round(
                    math.exp(cell.raw if cell.raw is not None else cell.value), 3
                ),
                "n": round(cell.n, 1),
                "season": season,
            }

        out = [entry(key, cell, None) for key, cell in sorted(self.cells.items())]
        # Seasonally split cells are what the forecast actually looks up once
        # it knows the date, so a map without them would show the pooled value
        # while the forecast quietly used a different one -- on precisely the
        # cells where the two disagree, which is the only reason they exist.
        for (azimuth, elevation, half), cell in sorted(self.seasonal.items()):
            out.append(
                entry(
                    (azimuth, elevation),
                    cell,
                    "ascending" if half == ASCENDING else "descending",
                )
            )
        return out

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
    #: How the maps were fitted; the differential ones scale with clearness at
    #: apply time, the absolute one must not (see the module docstring).
    method: str = METHOD_ABSOLUTE
    #: ``exp(level)`` per string from the joint fit: what the string delivers,
    #: relative to physics, where nothing is in the way.  Diagnostic only --
    #: the forecast never multiplies it in, the log-ratio layer learns the
    #: level from its own comparison -- but without it a map full of zeros
    #: cannot be told apart from a fit that silently swallowed the shadow.
    levels: dict[str, float] = field(default_factory=dict)

    @classmethod
    def fit(
        cls,
        rows_by_string: Mapping[str, Iterable[tuple[Any, ...]]],
        now_ts: float | None = None,
    ) -> "ShadingModel":
        materialised = {
            string_id: list(rows) for string_id, rows in rows_by_string.items()
        }
        joint = _joint_fit(materialised, now_ts)
        if joint is not None:
            maps, levels = joint
            return cls(maps=maps, method=METHOD_DIFFERENTIAL, levels=levels)
        return cls(
            maps={
                string_id: ShadingMap.fit(rows, now_ts)
                for string_id, rows in materialised.items()
            },
            method=METHOD_ABSOLUTE,
        )

    def factor(
        self,
        string_id: str,
        azimuth_deg: float,
        elevation_deg: float,
        ts_utc: float | None = None,
        beam: float | None = None,
    ) -> float:
        found = self.maps.get(string_id)
        geometric = (
            found.factor(azimuth_deg, elevation_deg, ts_utc) if found else 1.0
        )
        # Per map, not per model: a plant can hold an absolute map for a
        # string the joint fit could not cross-check, and an absolute map's
        # mixed-weather envelope must never be scaled by beam on top.
        if beam is None or found is None or not found.differential:
            return geometric
        share = min(max(beam, 0.0), 1.0) if math.isfinite(beam) else 1.0
        return 1.0 - share * (1.0 - geometric)

    def factors(
        self,
        string_id: str,
        azimuth_deg: Sequence[float],
        elevation_deg: Sequence[float],
        ts_utc: Sequence[float] | None = None,
        beam: Sequence[float] | None = None,
    ) -> np.ndarray:
        found = self.maps.get(string_id)
        if found is None:
            geometric = np.ones(len(azimuth_deg), dtype=float)
        else:
            geometric = found.factors(azimuth_deg, elevation_deg, ts_utc)
        if beam is None or found is None or not found.differential:
            return geometric
        share = np.asarray(beam, dtype=float)
        # An unknown beam share applies the full correction: that is exactly
        # what the absolute map always did, so ignorance degrades to old
        # behaviour rather than to optimism.
        share = np.where(np.isfinite(share), share, 1.0)
        share = np.clip(share, 0.0, 1.0)
        return 1.0 - share * (1.0 - geometric)

    def method_of(self, string_id: str) -> str:
        """How this one string's map was fitted.

        Not the same thing as the model's method: inside a differential model
        a string the joint fit could not cross-check keeps an absolute map,
        and a card reading the plant-level label would mislabel exactly that
        string.
        """
        found = self.maps.get(string_id)
        if found is None or not found.differential:
            return METHOD_ABSOLUTE
        return METHOD_DIFFERENTIAL

    def summary(self) -> dict[str, object]:
        return {
            string_id: found.summary()
            for string_id, found in sorted(self.maps.items())
            if found.observed_cells
        }

    def grid(self, string_id: str) -> list[dict[str, object]]:
        found = self.maps.get(string_id)
        return found.grid() if found else []

    def level(self, string_id: str) -> float | None:
        """The string's clear-view level relative to physics, or ``None``.

        Only the differential fit can know this; the absolute fit's capped
        reference deliberately refuses to (see ``_reference_level``).
        """
        found = self.levels.get(string_id)
        return None if found is None else round(found, 3)


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

    Taken over the cells' raw values, not their shrunk ones: a thin cell's
    shrunk value says "we do not know", which is not the same as "the sky is
    clear here".

    Weighted by evidence rather than filtered by it.  Excluding thin cells can
    only ever *lower* this estimate, and lowering it is the ruinous direction:
    it was excluding them, and on a string whose well-sampled cells all happen
    to sit in shadow -- a roof shaded every morning, sampled every morning --
    the reference dropped into the shadow itself.  Every cell was then measured
    against the shadow, came out at or above it, clamped to 1.0, and the map
    reported a flawless sky over a roof that was dark until one in the
    afternoon.  Weighting keeps a lucky thin cell from setting the standard
    alone without ever discarding a bright one.

    The bright end still has to carry weight to be heard: at the 0.90 quantile
    it needs rather more than a tenth of the total evidence.  A string whose
    clear sky is one thin cell against a thoroughly sampled shadow will still
    read as unshaded -- weighting makes that unlikely rather than impossible,
    and only more observations can settle it.

    Cells with no weight are dropped rather than passed on: a zero-weight
    population makes the quantile return its *lowest* value, which is the
    darkest cell -- exactly the wrong answer for "nothing in the way".
    """
    weighted = [(cell.value, cell.n) for cell in cells.values() if cell.n > 0.0]
    if not weighted:
        return 0.0
    # Never above parity.  A cell that matches physics exactly is not shaded,
    # whatever the rest of the sky manages -- and a string that beats physics
    # everywhere is not shaded either, it is a string whose physics runs low.
    # That is a level, and the level belongs to the per-string log-ratio model;
    # letting it into the map here reports it as shadow on every cell at once.
    # Measured: a reference of 1.78 put 10 to 36 % of phantom loss on a nearly
    # flat panel that can see the whole sky.
    #
    # The cap has a cost, and it is the one case this design cannot see: where
    # a string's physics genuinely runs low everywhere -- true clear ratio A
    # above one -- any shadow that fails to push a cell below parity stays
    # invisible, hiding up to 1 - 1/A of loss.  At A = 1.35 that is 26
    # percentage points.  Distinguishing it needs the string's level separated
    # out before the map is fitted, which is a different design; until then,
    # under-reporting one string's shadow beats inventing shade on every
    # string at once.
    return min(_weighted_quantile(weighted, REFERENCE_QUANTILE), 0.0)


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


@dataclass(slots=True)
class _JointObs:
    """One observation, prepared for the joint fit."""

    ts: float
    key: tuple[int, int]
    half: int
    log_r: float
    #: Coverage times recency: how much this row is allowed to say at all.
    w: float
    #: Beam share of the moment's irradiance, clamped to [0, 1]: how much of
    #: what it says is about beam, which is the only light a shadow can take.
    beam: float
    #: Whether the beam share was recorded or is the agnostic default.  Rows
    #: that do not know their beam are down-weighted by it but never
    #: *inverted* by it: dividing a true clear-day residual by an assumed
    #: half-beam doubles the shadow, and for the first weeks after an upgrade
    #: every row in the store is such a row.
    beam_known: bool
    physics_w: float | None


def _blended_shade_log(shade_log: float, beam: float) -> float:
    """What a clear-day shade looks like at partial beam, in log space.

    The blend the forecast applies, used in reverse gear by the fit: before an
    observation's shade can be taken out of the moment term, it has to be
    scaled down to what the obstacle could actually have cost in that light.
    """
    return math.log(max(1.0 - beam * (1.0 - math.exp(shade_log)), MIN_FACTOR))


def _clear_day_shade_log(blended_log: float, beam: float) -> float:
    """Invert the blend: the clear-day shade a partly-clear residual implies.

    An observation at half beam that came in 12 % low is reporting a quarter
    lost on a clear day, and storing the 12 % as if it were the clear-day
    figure would then be scaled by beam *again* at apply time -- the shadow
    would be discounted twice.  Clamped into ``[MIN_FACTOR, 1]``: at low beam
    the inversion amplifies noise, and the sample's beam weight is what keeps
    those from mattering.
    """
    transmission = 1.0 - (1.0 - math.exp(blended_log)) / max(beam, 1e-6)
    return math.log(min(max(transmission, MIN_FACTOR), 1.0))


def _joint_fit(
    rows_by_string: Mapping[str, Sequence[tuple[Any, ...]]],
    now_ts: float | None,
) -> tuple[dict[str, ShadingMap], dict[str, float]] | None:
    """Fit every string's map against its siblings rather than the absolute.

    Alternates three estimates for ``FIT_ROUNDS`` rounds:

    * the *moment* ``tau(t)`` -- what every string saw at once in a five-minute
      interval, weighted by each string's physics watts so a sliver of dawn on
      an east panel cannot define the moment for the whole plant;
    * each string's *level* -- its mean log-ratio once moment and shade are
      taken out;
    * each string's *shade per cell* -- the weighted median of what remains,
      each residual inverted through the beam blend to the clear-day loss it
      implies, re-zeroed per string at the ``REFERENCE_QUANTILE`` of its own
      cells so that level and shade cannot trade places, and clamped to never
      brighten.  Only epochs a sibling also saw take part: a lone
      observation's moment is unidentifiable, and treating it as neutral
      would read single-sighted weather as shade.

    Level and moment are nuisance terms: estimated so the shade is clean, then
    dropped (the level survives only as a diagnostic).  Returns ``None`` when
    fewer than two strings have usable rows -- differencing needs a sibling --
    and the caller falls back to the absolute per-string fit.
    """
    newest = 0.0
    parsed: dict[str, list[tuple[Any, ...]]] = {}
    for string_id, rows in rows_by_string.items():
        keep: list[tuple[Any, ...]] = []
        for row in rows:
            ts_utc, azimuth, elevation, ratio, weight = row[:5]
            if ratio <= 0.0 or weight <= 0.0 or elevation < 0.0:
                continue
            newest = max(newest, ts_utc)
            keep.append(row)
        if keep:
            parsed[string_id] = keep
    if len(parsed) < 2:
        return None

    reference_ts = now_ts if now_ts is not None else newest
    observations: dict[str, list[_JointObs]] = {}
    for string_id, keep in parsed.items():
        rows_out: list[_JointObs] = []
        for row in keep:
            ts_utc, azimuth, elevation, ratio, weight = row[:5]
            physics_w = float(row[5]) if len(row) > 5 and row[5] is not None else None
            beam_raw = float(row[6]) if len(row) > 6 and row[6] is not None else None
            aged = weight * recency_weight(ts_utc, reference_ts)
            if aged <= 0.0:
                continue
            beam_known = beam_raw is not None and math.isfinite(beam_raw)
            beam = (
                min(max(beam_raw, 0.0), 1.0) if beam_known else LEGACY_BEAM_WEIGHT
            )
            rows_out.append(
                _JointObs(
                    ts=float(ts_utc),
                    key=(azimuth_bin(azimuth), elevation_bin(elevation)),
                    half=season_half(ts_utc),
                    log_r=math.log(ratio),
                    w=aged,
                    beam=beam,
                    beam_known=beam_known,
                    physics_w=physics_w,
                )
            )
        if rows_out:
            observations[string_id] = rows_out
    if len(observations) < 2:
        return None

    by_epoch: dict[float, list[tuple[str, _JointObs]]] = {}
    for string_id, rows_out in observations.items():
        for obs in rows_out:
            by_epoch.setdefault(obs.ts, []).append((string_id, obs))
    shared_by_string: dict[str, int] = {string_id: 0 for string_id in observations}
    for members in by_epoch.values():
        present = {string_id for string_id, _obs in members}
        if len(present) < 2:
            continue
        for string_id in present:
            shared_by_string[string_id] += 1
    joint_ids = {
        string_id
        for string_id, count in shared_by_string.items()
        if count >= MIN_JOINT_EPOCHS
    }
    if len(joint_ids) < 2:
        return None
    # Strings below the overlap gate leave the joint fit entirely -- they get
    # their absolute map further down.  Keeping their rows in the epochs would
    # let a string that cannot be differenced still tilt everyone's moments.
    solo_ids = set(observations) - joint_ids
    if solo_ids:
        observations = {
            string_id: rows_out
            for string_id, rows_out in observations.items()
            if string_id in joint_ids
        }
        by_epoch = {}
        for string_id, rows_out in observations.items():
            for obs in rows_out:
                by_epoch.setdefault(obs.ts, []).append((string_id, obs))

    level: dict[str, float] = {
        string_id: sum(obs.w * obs.log_r for obs in rows_out)
        / sum(obs.w for obs in rows_out)
        for string_id, rows_out in observations.items()
    }
    shade: dict[str, dict[tuple[int, int], float]] = {
        string_id: {} for string_id in observations
    }
    shade_n: dict[str, dict[tuple[int, int], float]] = {
        string_id: {} for string_id in observations
    }

    tau: dict[float, float] = {}
    for _round in range(FIT_ROUNDS):
        tau = {}
        tau_num_total = 0.0
        tau_den_total = 0.0
        for ts_utc, members in by_epoch.items():
            if len({string_id for string_id, _obs in members}) < 2:
                continue
            # Watts and unitless weights must not be averaged together, and all
            # rows of one epoch come from one collect run, so the fallback is
            # per epoch rather than per row.
            physics_known = all(
                obs.physics_w is not None for _string_id, obs in members
            )
            num = 0.0
            den = 0.0
            for string_id, obs in members:
                w = obs.w * (obs.physics_w if physics_known else 1.0)
                if w <= 0.0:
                    continue
                # The shade is blended down to the moment's beam before it is
                # taken out, mirroring how it is applied at forecast time: in
                # an overcast moment the obstacle took nothing, and removing
                # the full clear-day loss would push the moment term up by
                # exactly the shadow it never cast.
                num += w * (
                    obs.log_r
                    - level[string_id]
                    - _blended_shade_log(
                        shade[string_id].get(obs.key, 0.0), obs.beam
                    )
                )
                den += w
            if den > 0.0:
                tau[ts_utc] = num / den
                tau_num_total += num
                tau_den_total += den
        if tau and tau_den_total > 0.0:
            # Moment and level are jointly free by a constant; pinning the
            # moment's weighted mean to zero keeps the levels meaning what
            # their name says instead of drifting round by round.
            centre = tau_num_total / tau_den_total
            tau = {ts_utc: value - centre for ts_utc, value in tau.items()}

        for string_id, rows_out in observations.items():
            num = 0.0
            den = 0.0
            for obs in rows_out:
                # Only epochs with a sibling: a lone observation cannot say
                # what was moment and what was string, and pretending its
                # moment was neutral folds single-sighted weather into the
                # level (and below, into the cells).
                if obs.ts not in tau:
                    continue
                num += obs.w * (
                    obs.log_r
                    - tau[obs.ts]
                    - _blended_shade_log(
                        shade[string_id].get(obs.key, 0.0), obs.beam
                    )
                )
                den += obs.w
            if den > 0.0:
                level[string_id] = num / den

        for string_id, rows_out in observations.items():
            per_cell: dict[tuple[int, int], list[tuple[float, float]]] = {}
            for obs in rows_out:
                if obs.ts not in tau:
                    continue
                w_cell = obs.w * obs.beam
                if w_cell <= 0.0:
                    continue
                residual = obs.log_r - level[string_id] - tau[obs.ts]
                per_cell.setdefault(obs.key, []).append(
                    (
                        _clear_day_shade_log(
                            residual, obs.beam if obs.beam_known else 1.0
                        ),
                        w_cell,
                    )
                )
            fitted: dict[tuple[int, int], tuple[float, float]] = {}
            for key, samples in per_cell.items():
                total = sum(weight for _value, weight in samples)
                if total < MIN_OBSERVATIONS:
                    continue
                fitted[key] = (_weighted_quantile(samples, 0.5), total)
            if not fitted:
                shade[string_id] = {}
                shade_n[string_id] = {}
                continue
            reference = _weighted_quantile(
                [(value, total) for value, total in fitted.values()],
                REFERENCE_QUANTILE,
            )
            level[string_id] += reference
            shade[string_id] = {
                key: min(value - reference, 0.0)
                for key, (value, _total) in fitted.items()
            }
            shade_n[string_id] = {
                key: total for key, (_value, total) in fitted.items()
            }

    maps: dict[str, ShadingMap] = {}
    for string_id, rows_out in observations.items():
        residual_samples: dict[tuple[int, int], list[Sample]] = {}
        raw_samples: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
        for obs in rows_out:
            raw_samples.setdefault(obs.key, []).append(
                (obs.log_r, obs.w, obs.half)
            )
            if obs.ts not in tau:
                continue
            w_cell = obs.w * obs.beam
            if w_cell <= 0.0:
                continue
            residual = obs.log_r - level[string_id] - tau[obs.ts]
            residual_samples.setdefault(obs.key, []).append(
                Sample(
                    value=_clear_day_shade_log(
                        residual, obs.beam if obs.beam_known else 1.0
                    ),
                    weight=w_cell,
                    half=obs.half,
                    ts_utc=obs.ts,
                )
            )

        def raw_envelope(
            key: tuple[int, int], half: int | None = None
        ) -> float | None:
            found = [
                (value, weight)
                for value, weight, sample_half in raw_samples.get(key, [])
                if half is None or sample_half == half
            ]
            return _weighted_quantile(found, UPPER_QUANTILE) if found else None

        cells: dict[tuple[int, int], Cell] = {}
        seasonal: dict[tuple[int, int, int], Cell] = {}
        for key, value in shade[string_id].items():
            cells[key] = Cell(
                value=value,
                n=shade_n[string_id][key],
                raw=raw_envelope(key),
            )
            split = _seasonal_split(
                residual_samples.get(key, []), cell_fn=_residual_cell_from
            )
            if split is not None:
                # The raw envelope goes on the split cells too, per half: a
                # grid mixing measured ratios on pooled cells with residual
                # shades on seasonal ones would be unreadable exactly where
                # the halves disagree, which is the only place splits exist.
                for half in (ASCENDING, DESCENDING):
                    seasonal[(key[0], key[1], half)] = Cell(
                        value=split[half].value,
                        n=split[half].n,
                        raw=raw_envelope(key, half),
                    )
        maps[string_id] = ShadingMap(
            cells=cells, seasonal=seasonal, reference=0.0, differential=True
        )

    # The strings below the overlap gate still deserve the map they would
    # have had on their own: an absolute envelope with the capped reference.
    # An empty differential map would quietly read as "no shade" on exactly
    # the string whose history nobody could cross-check.
    for string_id in solo_ids:
        maps[string_id] = ShadingMap.fit(parsed[string_id], now_ts)

    levels = {
        string_id: math.exp(value) for string_id, value in level.items()
    }
    return maps, levels


def _residual_cell_from(rows: Sequence[Sample]) -> Cell | None:
    """A cell from level-and-moment-free residuals: the differential path.

    A weighted *median*, not the envelope: after the moment term has cancelled
    the cloud-edge spikes between siblings, the residuals of one cell scatter
    around the shadow itself, and the middle of them is the honest estimate.
    The envelope was only ever needed to climb over that scatter -- kept here
    it would climb over the shadow instead, which is the exact failure this
    fit exists to end.  Clamped to "never brighter than the reference": a
    residual above zero is level estimation noise, not negative shade.
    """
    total = sum(row.weight for row in rows)
    if total < MIN_OBSERVATIONS:
        return None
    value = _weighted_quantile(
        [(row.value, row.weight) for row in rows], 0.5
    )
    return Cell(value=min(value, 0.0), n=total)


def _seasonal_split(
    rows: Sequence[Sample],
    cell_fn: Callable[[Sequence[Sample]], Cell | None] = _cell_from,
) -> dict[int, Cell] | None:
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

    cells = {half: cell_fn(side) for half, side in halves.items()}
    if any(cell is None for cell in cells.values()):
        return None
    if abs(cells[ASCENDING].value - cells[DESCENDING].value) < SEASON_SPLIT_THRESHOLD:
        return None
    return cells
