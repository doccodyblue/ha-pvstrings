"""Does the irradiance sensor tell the truth?

Cheap weather stations do not measure irradiance.  They measure illuminance
with a diode weighted to human vision and divide by a constant -- 126.7 on the
Ecowitt family and its clones, which is most of what people hang on a shed.
That constant is right for one reference case and wrong everywhere else,
because the share of the energy a photopic diode cannot see grows as the sun
drops and its light reddens.

On the reference plant, over 889 hours of 72 days against two reanalysis
products:

    sun  3-15 deg   0.597
    sun 15-25 deg   0.654
    sun 25-35 deg   0.726
    sun 35-45 deg   0.749
    sun 45-90 deg   0.775

So the station reads a quarter low at noon and closer to half low at dawn.
That matters beyond a number on a card: this one sensor feeds the nowcast, the
source-bias model and the shading map, and all three then learn a world that
is darker than the real one -- as a time-of-day effect, which is
indistinguishable from shade.

**Nothing in the integration applies what this module computes.**  It reports
a verdict and, where the evidence carries one, the curve that verdict would
justify.  Putting that curve in front of the existing models would correct the
error twice, because they have each absorbed part of it already; the way out
is a second branch that learns from scratch on corrected readings and is
scored against the first, never a factor bolted onto the front.

Two things this module deliberately cannot do.  It cannot tell a low sensor
from a high reference when both products agree -- they are models with shared
inputs, not instruments, so ``Band.systematic`` is a lower bound on their
error and not a safeguard.  And it says nothing about elevations it has not
seen: the curve fades to no correction beyond its evidence rather than holding
its outermost value, because holding flat is itself a claim.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

#: Elevation bands the ratio is reported in.  Bands rather than a fitted curve
#: because the shape is what has to be read off first: a flat profile means a
#: calibration offset, a rising one means the spectral effect, and a profile
#: that differs east to west means the thing is not level.  A curve fitted too
#: early hides all three behind one number.
ELEVATION_BANDS: tuple[tuple[float, float], ...] = (
    (3.0, 15.0),
    (15.0, 25.0),
    (25.0, 35.0),
    (35.0, 45.0),
    (45.0, 90.0),
)

#: The elevation window the east/west comparison runs in, and how finely it is
#: sliced inside that window.  Slices rather than one pool: the sides have to
#: be compared at matched sun heights or the spectral curve masquerades as a
#: tilt.
TILT_BAND: tuple[float, float] = (15.0, 45.0)
TILT_BUCKET_DEG = 5.0

#: Days each side must bring to a slice before it counts.
MIN_TILT_DAYS = 5

#: Bumped when the owner tells us the instrument changed -- moved, replaced,
#: cleaned, rewired.  Rows from different epochs are never pooled, because a
#: verdict averaged over two sensors describes neither.
CURSOR_IRRADIANCE_EPOCH = "irradiance_epoch"

#: When the current epoch began.  The counter alone does not separate two
#: instruments: the live banking reaches back two days and would stamp the old
#: sensor's last hours with the new epoch, handing a healthy replacement its
#: predecessor's verdict.
CURSOR_IRRADIANCE_EPOCH_SINCE = "irradiance_epoch_since"

#: Below this the sun is too low for the reference grid to mean anything --
#: it resolves neither terrain shadow nor the horizon, and both bite hardest
#: at dawn.  Hours below it are never banked, so they cost no storage either.
MIN_ELEVATION_DEG = 3.0

#: Five-minute samples an hour must carry before its mean stands for the hour.
#: An hour the collector only caught the bright end of averages to something
#: the sky never did, and the check would read that as a sensor reading high.
MIN_SAMPLES_PER_HOUR = 9

#: Below this the reference itself is unreliable -- a coarse grid resolves
#: neither terrain shadow nor the horizon, and both bite hardest at dawn.
MIN_REFERENCE_WM2 = 60.0

#: A band says nothing until it holds this much reference energy, in kWh/m2.
#: Counted as energy rather than as hours because one bright hour carries more
#: about a sensor than a dozen dim ones, and because it makes the threshold
#: mean the same thing in every band.
MIN_BAND_ENERGY_KWH = 1.0

#: The largest correction that will ever be applied.  A factor of 1.5 is a
#: sensor reading a third low; beyond that the more likely explanation is that
#: the reference is wrong -- a coarse grid at low sun, a horizon it cannot
#: see -- and multiplying by more than this on the strength of a model is a
#: bigger claim than the model supports.
MAX_APPLIED_FACTOR = 1.5

#: And not from too few days: hours of one overcast afternoon are one weather
#: event, not twelve independent looks at the sensor.
MIN_BAND_DAYS = 5

#: Share of a band's reference energy that must also carry a second,
#: independently produced reference before the band may bend the curve.  One
#: product cannot state its own error, and a band that only has one is a band
#: about which nothing systematic is known.
MIN_CROSS_SHARE = 0.5


@dataclass(slots=True)
class Band:
    """One elevation band's verdict, and how far it can be trusted."""

    low: float
    high: float
    hours: int = 0
    days: int = 0
    measured_kwh: float = 0.0
    reference_kwh: float = 0.0
    #: Reference energy times elevation, for the band's centre of evidence.
    elevation_kwh: float = 0.0
    #: Per day, [measured, reference] -- kept so the band can say how much its
    #: own days disagree with each other, which is the only honest measure of
    #: how much of its correction it has earned.
    daily: dict[int, list[float]] = field(default_factory=dict)
    #: Energy of the hours that carry a second reference, on both sides: the
    #: primary product's and the cross product's, so their disagreement can be
    #: read off without the hours that only one of them covers.
    cross_primary_kwh: float = 0.0
    cross_kwh: float = 0.0

    @property
    def ratio(self) -> float | None:
        if self.reference_kwh <= 0.0:
            return None
        return self.measured_kwh / self.reference_kwh

    @property
    def usable(self) -> bool:
        return (
            self.reference_kwh >= MIN_BAND_ENERGY_KWH
            and self.days >= MIN_BAND_DAYS
        )

    @property
    def centre(self) -> float | None:
        """Where in the band the evidence actually sits.

        Not the arithmetic middle: the 45-90 band is thirty degrees of sky
        that a German plant only ever reaches the bottom of, and pinning its
        knot at 67 degrees would stretch the curve across elevations no hour
        was ever recorded at.
        """
        if self.reference_kwh <= 0.0:
            return None
        return self.elevation_kwh / self.reference_kwh

    @property
    def standard_error(self) -> float | None:
        """Scatter of the daily log-ratios about their mean.

        Days rather than hours, because hours of one overcast afternoon share
        a weather event and would claim a precision the band has not got.
        """
        values = [
            math.log(m / r)
            for m, r in self.daily.values()
            if r > 0.0 and m > 0.0
        ]
        if len(values) < 3:
            return None
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        return math.sqrt(var / len(values))

    @property
    def cross_share(self) -> float:
        """How much of the band's evidence a second product also covers."""
        if self.reference_kwh <= 0.0:
            return 0.0
        return self.cross_primary_kwh / self.reference_kwh

    @property
    def systematic(self) -> float | None:
        """How far the two reference products disagree in this band, in log space.

        This is what more days cannot shrink.  The scatter between days says
        how precisely the band's ratio is known; it says nothing about whether
        the yardstick itself is straight, and a reanalysis product is a model,
        not an instrument.

        It is a *lower* bound and is documented as one: the two products share
        inputs -- SARAH-3 takes its water vapour and ozone from ERA5 -- so
        agreement between them is weaker evidence than independence would be.
        Where they disagree, though, at least one of them is wrong, and the
        band should keep less of its correction.
        """
        if self.cross_primary_kwh <= 0.0 or self.cross_kwh <= 0.0:
            return None
        return abs(math.log(self.cross_primary_kwh / self.cross_kwh))

    @property
    def trust(self) -> float:
        """What share of this band's correction is worth applying, 0 to 1.

        Signal against noise, so no constant has to be invented: a correction
        far larger than the uncertainty of the band that produced it is
        applied almost whole, one the size of that uncertainty is shrunk
        almost away.

        Two kinds of noise, added in quadrature.  ``se`` is how much the days
        disagree with each other and shrinks with more of them.  ``sys`` is how
        much the two reference products disagree and does not shrink at all --
        which is the point, because that is the part a longer soak cannot
        argue away.

        What this does **not** do is protect against an error the two products
        share.  If both read five percent high, ``sys`` is zero and a healthy
        sensor is handed a factor of 1.05.  For the shadow branch that is
        survivable -- a level error is the one thing the log-ratio layer
        resolves well -- but it is not a safeguard, and it must not be sold as
        one.
        """
        if not self.usable or self.ratio is None or self.ratio <= 0.0:
            return 0.0
        if self.cross_share < MIN_CROSS_SHARE:
            return 0.0
        effect = math.log(1.0 / self.ratio)
        se = self.standard_error
        sys = self.systematic
        if se is None or sys is None:
            return 0.0
        noise = se * se + sys * sys
        if noise <= 0.0:
            return 1.0
        return effect * effect / (effect * effect + noise)

    @property
    def factor(self) -> float:
        """The multiplier this band would apply to a reading, shrunk and capped."""
        if self.ratio is None or self.ratio <= 0.0:
            return 1.0
        raw = math.log(1.0 / self.ratio) * self.trust
        return min(math.exp(raw), MAX_APPLIED_FACTOR)

    def as_dict(self) -> dict[str, Any]:
        return {
            "elevation": f"{self.low:.0f}-{self.high:.0f}",
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "hours": self.hours,
            "days": self.days,
            "reference_kwh": round(self.reference_kwh, 2),
            "usable": self.usable,
            "centre_deg": None if self.centre is None else round(self.centre, 1),
            "cross_share": round(self.cross_share, 2),
            "reference_spread": (
                None if self.systematic is None else round(self.systematic, 4)
            ),
            "trust": round(self.trust, 3),
            "factor": round(self.factor, 3),
        }


@dataclass(slots=True)
class Verdict:
    """What the pairs say about the sensor, and how sure that is."""

    ratio: float | None = None
    hours: int = 0
    days: int = 0
    reference_kwh: float = 0.0
    bands: list[Band] = field(default_factory=list)
    east: float | None = None
    west: float | None = None
    sources: tuple[str, ...] = ()

    @property
    def slope(self) -> float | None:
        """How much the ratio changes between the lowest and highest usable band.

        The one number that separates the two explanations: a calibration
        offset is flat, the spectral effect rises with the sun.  ``None``
        while fewer than two bands carry enough evidence to compare.
        """
        usable = [b for b in self.bands if b.usable and b.ratio is not None]
        if len(usable) < 2:
            return None
        return usable[-1].ratio - usable[0].ratio

    @property
    def tilt_hint(self) -> float | None:
        """East-west difference at matched sun elevations.

        A sensor that is not level reads high on one side of noon and low on
        the other, and no calibration error does that.  It is a hint and not a
        verdict: a north-south tilt hides from this test entirely, and so does
        an obstruction that happens to be symmetric about south.
        """
        if self.east is None or self.west is None:
            return None
        return self.east - self.west

    def as_dict(self) -> dict[str, Any]:
        return {
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "hours": self.hours,
            "days": self.days,
            "reference_kwh": round(self.reference_kwh, 1),
            "by_elevation": [band.as_dict() for band in self.bands],
            "east_west": {
                "east": None if self.east is None else round(self.east, 3),
                "west": None if self.west is None else round(self.west, 3),
                "difference": (
                    None if self.tilt_hint is None else round(self.tilt_hint, 3)
                ),
            },
            "slope": None if self.slope is None else round(self.slope, 3),
            "reference_sources": list(self.sources),
            "reading": self.reading(),
            "note": (
                "Measured irradiance against an independent reference, per sun"
                " elevation. Diagnosis only -- the forecast does not use this."
                " A ratio near 1.0 means the sensor agrees with the reference."
            ),
        }

    def reading(self) -> str:
        """The shape in one sentence, or why there is not one yet.

        Order matters and is not the order of severity.  The overall ratio is
        checked *last*, because a band at 0.80 and one at 1.20 average to 1.00
        and would otherwise be reported as agreement -- the one verdict that
        stops anybody looking further.
        """
        usable = [b for b in self.bands if b.usable and b.ratio is not None]
        if self.ratio is None or not usable:
            return "not enough evidence yet"
        if self.tilt_hint is not None and abs(self.tilt_hint) >= 0.08:
            return "differs east to west -- check that the sensor is level"

        lowest = min(b.ratio for b in usable)
        highest = max(b.ratio for b in usable)
        # Which way it is wrong and what shape that error has are two
        # questions.  Reading the shape off the slope and then asserting the
        # direction from it reported a sensor reading 1.05 to 1.25 as "reads
        # low", which is the opposite of the truth.
        if highest < 0.95:
            direction = "reads low"
        elif lowest > 1.05:
            direction = "reads high"
        else:
            direction = "crosses the reference"

        slope = self.slope
        if slope is None:
            return "one elevation band only -- no shape yet"
        if slope >= 0.08:
            return f"{direction}, and more so as the sun drops"
        if slope <= -0.08:
            return f"{direction}, and more so as the sun rises -- unusual"

        # Flat, so one number describes it -- but only if the bands really do
        # agree with each other, not merely at their ends.
        if highest - lowest >= 0.08:
            return "uneven across the sky, with no clear trend"
        if max(abs(1.0 - b.ratio) for b in usable) < 0.05:
            return "agrees with the reference"
        if direction == "crosses the reference":
            return "close to the reference, but not evenly so"
        return f"{direction} by about the same amount at every sun height"


def assess(
    pairs: Iterable[Mapping[str, Any]],
    bands: Sequence[tuple[float, float]] = ELEVATION_BANDS,
) -> Verdict:
    """Fold complete pairs into a verdict.

    Each pair carries one closed hour: what the sensor averaged, what the
    reference says for the same hour, and where the sun stood.
    """
    built = [Band(low, high) for low, high in bands]
    days_seen: list[set[int]] = [set() for _ in built]
    all_days: set[int] = set()
    hours = measured = reference = 0.0
    tilt_east: dict[int, list] = {}
    tilt_west: dict[int, list] = {}
    sources: set[str] = set()

    for pair in pairs:
        ref = pair.get("reference_wm2")
        meas = pair.get("measured_wm2")
        if ref is None or meas is None or ref < MIN_REFERENCE_WM2:
            continue
        elevation = pair.get("elevation_deg")
        if elevation is None:
            continue
        day = int(pair["ts_utc"]) // 86400
        hours += 1
        measured += float(meas) / 1000.0
        reference += float(ref) / 1000.0
        all_days.add(day)
        if pair.get("reference_src"):
            sources.add(str(pair["reference_src"]))

        last = len(built) - 1
        for index, band in enumerate(built):
            # The top band closes at its upper edge: 90 degrees is a real
            # elevation between the tropics and would otherwise fall out of
            # every band and be counted nowhere.
            inside = (
                band.low <= elevation <= band.high
                if index == last
                else band.low <= elevation < band.high
            )
            if inside:
                band.hours += 1
                band.measured_kwh += float(meas) / 1000.0
                band.reference_kwh += float(ref) / 1000.0
                band.elevation_kwh += float(elevation) * float(ref) / 1000.0
                cross = pair.get("cross_wm2")
                if cross is not None and float(cross) > 0.0:
                    band.cross_primary_kwh += float(ref) / 1000.0
                    band.cross_kwh += float(cross) / 1000.0
                entry = band.daily.setdefault(day, [0.0, 0.0])
                entry[0] += float(meas)
                entry[1] += float(ref)
                days_seen[index].add(day)
                break

        azimuth = pair.get("azimuth_deg")
        if azimuth is not None and TILT_BAND[0] <= elevation < TILT_BAND[1]:
            # Bucketed by elevation, so the two sides are compared at matched
            # sun heights.  Pooling a whole 15-45 degree window would let a
            # low morning face a high afternoon and report the spectral curve
            # as a tilt -- which sends the owner up a ladder for nothing.
            slot = int((elevation - TILT_BAND[0]) // TILT_BUCKET_DEG)
            side = tilt_east if azimuth < 180.0 else tilt_west
            entry = side.setdefault(slot, [0.0, 0.0, set()])
            entry[0] += float(meas)
            entry[1] += float(ref)
            entry[2].add(day)

    for band, days in zip(built, days_seen):
        band.days = len(days)

    east, west = _matched_sides(tilt_east, tilt_west)

    return Verdict(
        ratio=(measured / reference) if reference > 0 else None,
        hours=int(hours),
        days=len(all_days),
        reference_kwh=reference,
        bands=built,
        east=east,
        west=west,
        sources=tuple(sorted(sources)),
    )


def _matched_sides(
    east: dict[int, list], west: dict[int, list]
) -> tuple[float | None, float | None]:
    """East and west ratios over the elevation slots both sides actually share.

    A slot counts only when each side brings its own days: one stray afternoon
    hour against a week of mornings is not a comparison, and before this it
    was enough to raise a tilt warning.
    """
    shared = [
        slot
        for slot in set(east) & set(west)
        if len(east[slot][2]) >= MIN_TILT_DAYS
        and len(west[slot][2]) >= MIN_TILT_DAYS
        and east[slot][1] > 0
        and west[slot][1] > 0
    ]
    if not shared:
        return None, None
    em = sum(east[s][0] for s in shared)
    er = sum(east[s][1] for s in shared)
    wm = sum(west[s][0] for s in shared)
    wr = sum(west[s][1] for s in shared)
    return em / er, wm / wr


def rows_to_bank(
    physics: Any,
    measured: Mapping[int, float],
    epoch: int,
    source: str,
) -> Iterator[tuple[Any, ...]]:
    """Measured hours with the sun's position at each hour's midpoint.

    Shared by the live cycle and the backfill service so both bank the same
    geometry: an hour banked live and the same hour re-derived from the
    recorder must land on the same elevation, or the bands would disagree
    with themselves.
    """
    from .physics import to_index

    hours = sorted(measured)
    if not hours:
        return
    index = to_index([hour + 1800 for hour in hours])
    position = physics.solar_position(index)
    elevations = position["apparent_elevation"].to_numpy()
    azimuths = position["azimuth"].to_numpy()
    for hour, elevation, azimuth in zip(hours, elevations, azimuths):
        if elevation < MIN_ELEVATION_DEG:
            continue
        yield (
            int(hour),
            int(epoch),
            float(measured[hour]),
            source,
            float(elevation),
            float(azimuth),
            None,
        )


#: How far beyond its outermost knot a curve is allowed to reach before it has
#: faded back to no correction at all.  One band's width: far enough that the
#: sun crossing the edge of the evidence does not step, short enough that a
#: curve learned in autumn says nothing about a June noon.
RAMP_DEG = 10.0


@dataclass(slots=True)
class Calibration:
    """What to multiply a reading by, as a function of the sun's elevation.

    Piecewise linear between the bands' centres of evidence, and fading to no
    correction over ``RAMP_DEG`` beyond the outermost of them.  Not a fitted
    curve: a fit would put a number on elevations no hour was ever recorded
    at, and the one thing this must not do is invent a correction for a part
    of the sky it has never seen.

    Fading rather than holding flat, because holding flat *is* a claim.  A
    curve whose highest knot sits at 35 degrees would otherwise apply that
    knot's correction at 60 degrees in June -- a season and a sun height it
    has never seen.  It still interpolates across gaps *between* knots, which
    is a model too, but a bounded one: both ends of the gap are evidence.
    """

    knots: tuple[tuple[float, float], ...] = ()
    capped: bool = False

    @property
    def active(self) -> bool:
        """Whether applying this would change anything.

        Two knots at least, because one knot is a flat offset that the source
        bias already models better -- and at least one of them has to differ
        from unity by more than a rounding error.
        """
        return len(self.knots) >= 2 and any(
            abs(f - 1.0) > 0.005 for _, f in self.knots
        )

    @property
    def revision(self) -> str:
        """A short, stable name for exactly this curve.

        Everything a reading passed through has to be attributable to the
        curve that was in force: the plausibility cache keys on it, the
        shadow branch freezes it, and an observation learned under one curve
        must never be pooled with one learned under another.
        """
        if not self.active:
            return "unity"
        raw = ";".join(f"{x:.2f}:{y:.4f}" for x, y in self.knots)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @property
    def evidence_range(self) -> tuple[float, float] | None:
        """The elevations the curve was actually learned at."""
        if not self.active:
            return None
        return self.knots[0][0], self.knots[-1][0]

    def factor(self, elevation: float) -> float:
        if not self.active:
            return 1.0
        low, high = self.knots[0], self.knots[-1]
        if elevation < low[0]:
            return _fade(low[1], low[0] - elevation)
        if elevation > high[0]:
            return _fade(high[1], elevation - high[0])
        for (x0, y0), (x1, y1) in zip(self.knots, self.knots[1:]):
            if x0 <= elevation <= x1:
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (elevation - x0) / (x1 - x0)
        return 1.0

    def as_dict(self) -> dict[str, Any]:
        span = self.evidence_range
        return {
            "active": self.active,
            "revision": self.revision,
            "capped": self.capped,
            "evidence_from_deg": None if span is None else round(span[0], 1),
            "evidence_to_deg": None if span is None else round(span[1], 1),
            "knots": [
                {"elevation": round(x, 1), "factor": round(y, 3)}
                for x, y in self.knots
            ],
        }


def _fade(factor: float, distance: float) -> float:
    """Blend a knot's correction back to unity over ``RAMP_DEG``."""
    if distance >= RAMP_DEG:
        return 1.0
    share = 1.0 - distance / RAMP_DEG
    return 1.0 + (factor - 1.0) * share


def calibration(verdict: Verdict) -> Calibration:
    """Turn a verdict into the curve it justifies -- and usually into none.

    Every band that has earned a correction contributes a knot; the rest are
    left out rather than pinned at unity, because a knot at 1.0 next to one at
    1.3 would ramp the correction across a stretch of sky about which nothing
    is known.
    """
    knots: list[tuple[float, float]] = []
    capped = False
    for band in verdict.bands:
        if not band.usable or band.centre is None:
            continue
        if band.trust <= 0.0:
            continue
        raw = math.exp(math.log(1.0 / band.ratio) * band.trust)
        if raw > MAX_APPLIED_FACTOR:
            capped = True
        knots.append((band.centre, band.factor))
    return Calibration(tuple(knots), capped)
