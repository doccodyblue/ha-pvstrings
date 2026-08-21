"""DC -> AC / battery-charge conversion (upgrade.md, tranche 1).

Pure functions, strictly downstream of the DC forecast: nothing here may
feed back into learning, scoring or censoring.  The DC series stays the
model's truth; this layer only answers "what arrives behind the
inverter" (direct) or "what lands in the battery" (storage).

Load is input-referenced (dc / rated_ac) although datasheet curves are
usually output-referenced -- a deliberate approximation, absorbed later
by the learning stage together with unit-to-unit spread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

CURVE_DATASHEET = "datasheet"
CURVE_CUSTOM = "custom"
CURVE_NEUTRAL = "neutral"
#: The storage path has no curve at all -- its stages are flat configured
#: factors.  Reporting "neutral" there claimed the output equalled the input
#: while the factors were in fact applied.
CURVE_FIXED = "fixed_factors"

#: (load_pct_of_rated, efficiency) support points, sorted by load.
Curve = Sequence[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """Converted hourly series plus what happened to it."""

    hourly_kwh: dict[int, float]
    clipped_kwh: float
    curve_source: str
    stages: tuple[str, ...]
    #: Flat multiplier, for paths whose stages are constants rather than a
    #: load-dependent curve.  ``None`` where efficiency varies per hour.
    factor: float | None = None


def interpolate(curve: Curve, load_fraction: float) -> float:
    """Linear between support points, clamped at both ends."""
    load = load_fraction * 100.0
    if load <= curve[0][0]:
        return curve[0][1]
    if load >= curve[-1][0]:
        return curve[-1][1]
    for (lo_load, lo_eff), (hi_load, hi_eff) in zip(curve, curve[1:]):
        if lo_load <= load <= hi_load:
            span = hi_load - lo_load
            if span <= 0:
                return lo_eff
            return lo_eff + (hi_eff - lo_eff) * (load - lo_load) / span
    return curve[-1][1]


def convert_direct(
    hourly_kwh: Mapping[int, float],
    rated_ac_w: float | None,
    curve: Curve | None,
    curve_source: str,
    clipping: bool,
) -> ConversionResult:
    """DC hourly means -> AC behind the inverter.

    Without a rated AC power neither the load fraction nor the clip level
    is computable -> identity pass-through (the flow prevents this
    configuration; the core stays permissive so old entries always load).
    Hourly kWh equals mean kW, so the watt ceiling divides by 1000 and
    compares directly; sub-hour clipping inside a bright hour is
    understated -- callers surface that as a note, not as precision.
    """
    if not rated_ac_w or rated_ac_w <= 0:
        return ConversionResult(
            hourly_kwh=dict(hourly_kwh),
            clipped_kwh=0.0,
            curve_source=CURVE_NEUTRAL,
            stages=(),
        )
    # Curve and clipping are independent stages: "no curve, but clip at
    # rated" is a legitimate configuration and the cap needs no curve.
    out: dict[int, float] = {}
    clipped = 0.0
    cap_kwh = rated_ac_w / 1000.0
    for hour, dc_kwh in hourly_kwh.items():
        if dc_kwh <= 0.0:
            out[hour] = 0.0
            continue
        ac_kwh = dc_kwh
        if curve is not None:
            ac_kwh *= interpolate(curve, dc_kwh * 1000.0 / rated_ac_w)
        if clipping and ac_kwh > cap_kwh:
            clipped += ac_kwh - cap_kwh
            ac_kwh = cap_kwh
        out[hour] = ac_kwh
    stages = (("inverter_efficiency",) if curve is not None else ()) + (
        ("clipping",) if clipping else ()
    )
    return ConversionResult(
        hourly_kwh=out,
        clipped_kwh=round(clipped, 3),
        curve_source=curve_source if curve is not None else CURVE_NEUTRAL,
        stages=stages,
    )


def convert_storage(
    hourly_kwh: Mapping[int, float],
    mppt_efficiency: float | None,
    charge_efficiency: float,
) -> ConversionResult:
    """DC hourly means -> energy landing in the battery.

    Ends at the battery terminal by design: discharge is a control
    decision, not a forecast (upgrade.md 3.4).
    """
    factor = (mppt_efficiency or 1.0) * charge_efficiency
    stages = (("mppt_efficiency",) if mppt_efficiency else ()) + (
        "charge_efficiency",
    )
    return ConversionResult(
        hourly_kwh={
            hour: max(0.0, value) * factor for hour, value in hourly_kwh.items()
        },
        clipped_kwh=0.0,
        curve_source=CURVE_FIXED,
        stages=stages,
        factor=round(factor, 4),
    )


def convert_group(
    hourly_kwh: Mapping[int, float],
    output_path: str,
    rated_ac_w: float | None,
    inverter_model: str | None,
    custom_curve: Curve | None,
    forecast_clipping: bool,
    mppt_efficiency: float | None,
    charge_efficiency: float,
    curves: Mapping[str, Curve],
) -> ConversionResult | None:
    """Dispatch one group's DC series through its configured path.

    ``None`` for path "none": no conversion, no new entities -- the
    pre-conversion behaviour.
    """
    if output_path == "direct":
        if inverter_model == "custom" and custom_curve:
            return convert_direct(
                hourly_kwh, rated_ac_w, custom_curve, CURVE_CUSTOM,
                forecast_clipping,
            )
        curve = curves.get(inverter_model) if inverter_model else None
        return convert_direct(
            hourly_kwh,
            rated_ac_w,
            curve,
            CURVE_DATASHEET if curve else CURVE_NEUTRAL,
            forecast_clipping,
        )
    if output_path == "storage":
        return convert_storage(hourly_kwh, mppt_efficiency, charge_efficiency)
    return None
