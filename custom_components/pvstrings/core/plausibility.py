"""Is the measured irradiance consistent with what the array actually did?

A user's irradiance sensor is one point on a pole; the array is a set of planes
spread over a roof.  When the two disagree the sensor is usually right -- but
not always.  A sensor that is slightly off level, soiled, spectrally limited or
clipped by a gable for part of the afternoon reads low for a few hours a day
and looks perfectly healthy for the rest of them.

That matters more than it sounds, because a measured GHI is used as *truth* in
three places at once: it drives the physics that actuals are compared against,
it is the yardstick for the forecast source's bias, and it forms the
denominator of every shading observation.  A sensor that under-reads all
afternoon produces no obviously wrong number anywhere.  It quietly teaches the
model that the array over-performs after lunch, and the error is then baked
into three tables that each look self-consistent.

The guard here is deliberately crude and one-sided.  For each plane it computes
the largest plane-of-array irradiance the measured GHI could possibly produce
-- taking the best case over every physically allowed split of that GHI into
beam and diffuse -- and rejects the hour only when the array beat even that.
No decomposition model can argue with the result, it needs no configuration,
and it stays silent unless something is genuinely wrong.

It is intentionally not symmetric.  An array falling *short* of the ceiling is
the normal condition -- that is shading, soiling, snow, curtailment and plain
losses, all of which are things the model is supposed to learn.  Only the
impossible direction is evidence about the sensor.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: How far over the ceiling an hour must go before we disbelieve the sensor.
#: The ceiling is already generous, so this is not a noise allowance -- it
#: covers an understated nameplate and a cold-cell efficiency bonus, both of
#: which lift real output above the naive STC scaling used below.
DEFAULT_MARGIN = 1.15

#: Ground reflection is bounded by the physical worst case, not by a typical
#: one.  Fresh snow reaches 0.9, and a steep plane over snow collects a fifth
#: of the horizontal irradiance again from the ground alone -- enough to put a
#: healthy winter hour over a ceiling built for grass.  A blunter test that is
#: never wrong beats a sharp one that discards good data every February.
MAX_ALBEDO = 0.90

#: Nothing on this planet delivers more than this to a plane, so a low-sun
#: division by ``sin(elevation)`` cannot run away.
MAX_POA_WM2 = 1400.0

#: Below this the geometry is too grazing for the ceiling to mean anything and
#: the energy at stake is negligible either way.
MIN_ELEVATION_DEG = 5.0

#: An hour must produce at least this share of nameplate before the ceiling is
#: allowed to have an opinion about it.  Cheap irradiance sensors -- and every
#: illuminance-derived one -- quantise to 0.0 W/m2 through the first and last
#: hour of daylight while the array is already making a few watt-hours.  With
#: no floor, a zero ceiling against any production at all reads as a fault,
#: and two hours of every single clear day get thrown away.
MIN_JUDGED_FRACTION = 0.02

#: ...and never less than this, so a very small plant is not judged on noise.
MIN_JUDGED_W = 30.0


def judgement_floor(total_kwp: float) -> float:
    """Production below which an hour is left alone."""
    return max(MIN_JUDGED_W, MIN_JUDGED_FRACTION * total_kwp * 1000.0)


@dataclass(frozen=True, slots=True)
class Plane:
    """One planar sub-array: what the ceiling needs and nothing more."""

    tilt_deg: float
    azimuth_deg: float
    kwp: float


def cos_incidence(
    tilt_deg: float,
    azimuth_deg: float,
    elevation_deg: np.ndarray,
    solar_azimuth_deg: np.ndarray,
) -> np.ndarray:
    """Cosine of the angle between the plane normal and the sun, floored at 0."""
    tilt = np.radians(tilt_deg)
    elevation = np.radians(elevation_deg)
    delta = np.radians(solar_azimuth_deg - azimuth_deg)
    cos_aoi = np.sin(elevation) * np.cos(tilt) + np.cos(elevation) * np.sin(
        tilt
    ) * np.cos(delta)
    return np.clip(cos_aoi, 0.0, None)


def poa_ceiling_wm2(
    plane: Plane,
    ghi_wm2: np.ndarray,
    elevation_deg: np.ndarray,
    solar_azimuth_deg: np.ndarray,
) -> np.ndarray:
    """Largest plane irradiance obtainable from ``ghi_wm2``, over every split.

    A measured global horizontal value constrains the pair (DNI, DHI) to the
    line ``DNI*sin(elevation) + DHI == GHI``.  The plane's response is linear in
    both, so the maximum sits at one of the two ends of that line: everything
    beam, or everything diffuse.  Which end wins depends on the geometry -- for
    a steep plane facing the low sun the beam end wins by a wide margin, for a
    near-flat plane at grazing incidence the diffuse end does.  Taking the
    larger of the two is therefore the true ceiling and not merely a guess at
    which case applies.
    """
    elevation = np.maximum(elevation_deg, MIN_ELEVATION_DEG)
    sin_elevation = np.sin(np.radians(elevation))
    tilt = np.radians(plane.tilt_deg)

    all_beam = ghi_wm2 * cos_incidence(
        plane.tilt_deg, plane.azimuth_deg, elevation_deg, solar_azimuth_deg
    ) / sin_elevation
    all_diffuse = ghi_wm2 * (1.0 + np.cos(tilt)) / 2.0
    ground = ghi_wm2 * MAX_ALBEDO * (1.0 - np.cos(tilt)) / 2.0

    return np.minimum(np.maximum(all_beam, all_diffuse) + ground, MAX_POA_WM2)


def plant_ceiling_w(
    planes: list[Plane],
    ghi_wm2: np.ndarray,
    elevation_deg: np.ndarray,
    solar_azimuth_deg: np.ndarray,
) -> np.ndarray:
    """Ceiling on total DC power, summed over the planes.

    Deliberately lossless: no inverter efficiency, no temperature derate, no
    incidence-angle reflection.  Every one of those only ever *reduces* real
    output, so leaving them out keeps the bound above anything achievable.
    """
    total = np.zeros_like(np.asarray(ghi_wm2, dtype=float))
    for plane in planes:
        if plane.kwp <= 0:
            continue
        poa = poa_ceiling_wm2(plane, ghi_wm2, elevation_deg, solar_azimuth_deg)
        total = total + poa / 1000.0 * plane.kwp * 1000.0
    return total


def exceeds_ceiling(
    actual_w: float,
    ceiling_w: float,
    margin: float = DEFAULT_MARGIN,
    floor_w: float = MIN_JUDGED_W,
) -> bool:
    """Did the array beat what the measured irradiance allows?

    ``floor_w`` keeps the twilight hours out of it.  A ceiling at or below zero
    against real production is the starkest form of disagreement there is --
    but at dawn it is far more likely to mean a sensor that rounds to zero
    than one that is broken, and convicting it would cost the same two hours
    every day for the life of the installation.
    """
    if actual_w <= floor_w:
        return False
    if ceiling_w <= 0.0:
        return True
    return actual_w > ceiling_w * margin
