"""Does the irradiance sensor tell the truth?

Cheap weather stations do not measure irradiance.  They measure illuminance
with a diode weighted to human vision and divide by a constant -- 126.7 on the
Ecowitt family and its clones, which is most of what people hang on a shed.
That constant is right for one reference case and wrong everywhere else,
because the share of the energy a photopic diode cannot see grows as the sun
drops and its light reddens.

On the reference plant, over 865 hours against two independent references that
agree with each other to one percent:

    sun  5-15 deg   0.57
    sun 15-25 deg   0.63
    sun 25-35 deg   0.71
    sun 35-45 deg   0.74
    sun 45-60 deg   0.79

So the station reads a quarter low at noon and closer to half low at dawn.
That matters beyond a number on a card: this one sensor feeds the nowcast, the
source-bias model and the shading map, and all three then learn a world that
is darker than the real one -- as a time-of-day effect, which is
indistinguishable from shade.

**This module only looks.**  Nothing here multiplies the measured irradiance,
and nothing downstream reads its result.  Applying a correction means
migrating every model that has already absorbed part of this error, which is a
separate piece of work; a factor in front of them would correct it twice.
"""

from __future__ import annotations

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

#: And not from too few days: hours of one overcast afternoon are one weather
#: event, not twelve independent looks at the sensor.
MIN_BAND_DAYS = 5


@dataclass(slots=True)
class Band:
    """One elevation band's verdict."""

    low: float
    high: float
    hours: int = 0
    days: int = 0
    measured_kwh: float = 0.0
    reference_kwh: float = 0.0

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "elevation": f"{self.low:.0f}-{self.high:.0f}",
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "hours": self.hours,
            "days": self.days,
            "reference_kwh": round(self.reference_kwh, 2),
            "usable": self.usable,
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
