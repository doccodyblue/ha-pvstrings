"""Savings and amortisation, computed on measured data only.

This runs on actuals, not on the forecast, so it stays valid even while the
learning layer is still cold.

Two mistakes this module exists to avoid:

1. **Valuing export at the retail price by default.**  That is only true while
   the meter physically runs backwards.  Once it is swapped, exported energy
   earns the feed-in tariff or, under a zero-export limit, nothing at all --
   because it is then never generated.  Keeping ``net_metering`` a separate,
   explicitly temporary mode makes the scenario comparison answer "what will the
   meter swap cost me?" *before* it happens.
2. **Extrapolating a partial year linearly.**  "Savings so far divided by days,
   times 365" measured from spring runs straight over the yield peak.  The
   annual estimate here is weighted by the site's own clear-sky seasonality.
3. **Valuing DC energy at an AC price.**  The strings are measured on their DC
   side; what displaces a purchase is what comes out of the inverter, or back
   out of the battery.  Paying the retail price for the conversion losses
   makes every saving five to ten percent too high and the amortisation date
   correspondingly early.  ``delivered`` closes that gap per group, and names
   what each group's factor rests on -- a measurement, a curve, or a
   configured number.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping

from .config import Economics

MODE_NET_METERING = "net_metering"
MODE_SELF_CONSUMPTION = "self_consumption"
MODE_FEED_IN = "feed_in"

#: Where a group's delivery factor came from.  Named rather than merely
#: applied: a plant that measures one inverter and assumes the other must be
#: able to say which half is which, or the total quietly claims a precision
#: only one of them has.
BASIS_MEASURED = "measured"
BASIS_CURVE = "curve"
BASIS_CONFIGURED = "configured"
#: No path configured, or nothing to convert with -- the energy is counted on
#: its DC side, exactly as before, and says so.
BASIS_DC = "dc"

#: Below this a "measured efficiency" is a broken sensor, above it a physical
#: impossibility.  Evidence outside the band is refused rather than allowed to
#: deflate or inflate the lifetime figure.
MIN_FACTOR = 0.5
MAX_FACTOR = 1.0

#: Measured pairs before the ratio is trusted over the datasheet curve.  Rows
#: are five minutes apart and only daylight produces them, so this is roughly
#: two clear days -- enough to cover the load range the plant actually runs
#: at, and short enough that a new installation is not stuck on its datasheet
#: for a month.
MIN_EVIDENCE_ROWS = 200


#: Why measured evidence was not used, when there was some.
REFUSED_TOO_FEW = "too_few_pairs"
REFUSED_IMPLAUSIBLE = "outside_plausible_band"


@dataclass(frozen=True, slots=True)
class DeliveryFactor:
    """What one group's DC energy has to be multiplied by, and on what basis."""

    factor: float
    basis: str
    #: Measured pairs behind the evidence, for display.  Zero where there was
    #: none to count.
    samples: int = 0
    #: What the measurement said, even when it was not used.  A group sitting
    #: on its curve while an AC sensor is wired up is a question somebody will
    #: ask, and "0.71 from 43 pairs, refused" answers it where a bare basis
    #: label cannot.
    measured_ratio: float | None = None
    refused: str | None = None


@dataclass(frozen=True, slots=True)
class Delivery:
    """Measured DC energy, and what of it actually left the plant."""

    kwh: float
    dc_kwh: float
    #: Delivered kWh per basis.  A reader can see at a glance how much of the
    #: total rests on a measurement and how much on an assumption.
    by_basis: dict[str, float]

    @property
    def factor(self) -> float:
        return self.kwh / self.dc_kwh if self.dc_kwh > 0 else 1.0


def _plausible(factor: float) -> bool:
    return MIN_FACTOR <= factor <= MAX_FACTOR


def delivery_factor(
    output_path: str,
    measured: tuple[float, float, int] | None = None,
    curve_factor: float | None = None,
    configured_factor: float | None = None,
    min_samples: int = MIN_EVIDENCE_ROWS,
) -> DeliveryFactor:
    """Pick a group's factor from the best evidence it has.

    Measurement beats curve beats configuration, and anything implausible
    falls through to the next rung rather than being applied -- a factor of
    1.4 from a mis-scaled AC sensor would otherwise pay for energy nobody
    produced.  The last rung is always DC at 1.0: unconverted, and labelled
    so, because silently valuing DC at the retail price is the error this
    whole layer exists to remove.
    """
    if output_path == "storage":
        # "AC" is the wrong target here.  What displaces a purchase is what
        # comes back out of the battery, and charge and discharge efficiency
        # are both configured numbers: battery power is a net flow after the
        # house load, not a two-port, so neither side can be measured the way
        # an inverter's can.  This rung is an estimate by construction and
        # says so.
        if configured_factor is not None and _plausible(configured_factor):
            return DeliveryFactor(round(configured_factor, 4), BASIS_CONFIGURED)
        return DeliveryFactor(1.0, BASIS_DC)

    if output_path == "direct":
        ratio: float | None = None
        rows = 0
        refused: str | None = None
        if measured is not None:
            in_w, out_w, rows = measured
            if in_w > 0:
                ratio = round(out_w / in_w, 4)
                if rows < min_samples:
                    refused = REFUSED_TOO_FEW
                elif not _plausible(ratio):
                    refused = REFUSED_IMPLAUSIBLE
                else:
                    return DeliveryFactor(
                        ratio, BASIS_MEASURED, rows, measured_ratio=ratio
                    )
        if curve_factor is not None and _plausible(curve_factor):
            return DeliveryFactor(
                round(curve_factor, 4),
                BASIS_CURVE,
                rows,
                measured_ratio=ratio,
                refused=refused,
            )
        return DeliveryFactor(
            1.0, BASIS_DC, rows, measured_ratio=ratio, refused=refused
        )

    return DeliveryFactor(1.0, BASIS_DC)


def delivered(
    dc_by_string: Mapping[str, float],
    factors: Mapping[str, DeliveryFactor],
) -> Delivery:
    """Apply each string's factor to its measured DC energy.

    A string missing from ``factors`` -- no group, no path, or one removed
    from the configuration while its history stays in the database -- counts
    at DC.  That keeps the total complete; dropping it would silently shrink
    the lifetime figure instead of merely leaving it uncorrected.
    """
    delivered_kwh = 0.0
    dc_kwh = 0.0
    by_basis: dict[str, float] = {}
    for string_id, dc in dc_by_string.items():
        entry = factors.get(string_id) or DeliveryFactor(1.0, BASIS_DC)
        value = dc * entry.factor
        delivered_kwh += value
        dc_kwh += dc
        by_basis[entry.basis] = by_basis.get(entry.basis, 0.0) + value
    return Delivery(
        kwh=delivered_kwh,
        dc_kwh=dc_kwh,
        by_basis={basis: round(value, 3) for basis, value in by_basis.items()},
    )


@dataclass(frozen=True, slots=True)
class SavingsResult:
    delivered_kwh: float
    export_kwh: float
    self_used_kwh: float
    saved_eur: float
    mode: str

    @property
    def eur_per_kwh(self) -> float:
        return self.saved_eur / self.delivered_kwh if self.delivered_kwh > 0 else 0.0


def savings(
    delivered_kwh: float, export_kwh: float | None, economics: Economics
) -> SavingsResult:
    """Monetary value of ``delivered_kwh``.

    Delivered, not produced: the caller passes energy that has already been
    through ``delivered``, so both sides of the self-consumption split are on
    the same side of the inverter.  Feeding raw DC in here is the unit mix
    this module's third opening note is about -- ``self_used`` would then be a
    DC figure minus an AC one, invisible under ``net_metering`` where both
    carry the same price and wrong under ``self_consumption`` where they do
    not.

    ``export_kwh`` may be ``None`` when the plant has no grid meter
    configured; in that case everything is treated as self-consumed, which is
    the correct assumption for a small balcony plant behind the house load and
    is stated as such in the diagnostics.
    """
    exported = max(0.0, export_kwh or 0.0)
    exported = min(exported, delivered_kwh)
    self_used = max(0.0, delivered_kwh - exported)

    if economics.mode == MODE_NET_METERING:
        # The meter runs backwards: every exported kWh really does displace an
        # imported one.  True today, temporary by construction.
        saved = delivered_kwh * economics.price_per_kwh
    elif economics.mode == MODE_FEED_IN:
        saved = delivered_kwh * economics.feed_in_tariff
    else:
        saved = (
            self_used * economics.price_per_kwh + exported * economics.feed_in_tariff
        )

    return SavingsResult(
        delivered_kwh=delivered_kwh,
        export_kwh=exported,
        self_used_kwh=self_used,
        saved_eur=saved,
        mode=economics.mode,
    )


def scenarios(
    delivered_kwh: float, export_kwh: float | None, economics: Economics
) -> dict[str, SavingsResult]:
    """The same production valued under every tariff model."""
    return {
        mode: savings(delivered_kwh, export_kwh, economics.with_mode(mode))
        for mode in (MODE_NET_METERING, MODE_SELF_CONSUMPTION, MODE_FEED_IN)
    }


# --------------------------------------------------------------------------- #
# seasonal extrapolation
# --------------------------------------------------------------------------- #


def _normalised(monthly_weights: list[float]) -> list[float]:
    """Force the twelve monthly shares to sum to one.

    The clear-sky derivation already normalises, but an annual estimate that is
    quietly 5 % off because a caller handed over rounded weights is exactly the
    kind of silent error this module is meant to remove.
    """
    if len(monthly_weights) != 12:
        raise ValueError("monthly_weights must have twelve entries")
    total = sum(monthly_weights)
    if total <= 0:
        return [1.0 / 12.0] * 12
    return [weight / total for weight in monthly_weights]


def _daily_weights(year: int, monthly_weights: list[float]) -> dict[date, float]:
    weights: dict[date, float] = {}
    for month in range(1, 13):
        days = calendar.monthrange(year, month)[1]
        share = monthly_weights[month - 1] / days
        for day in range(1, days + 1):
            weights[date(year, month, day)] = share
    return weights


def period_share(
    start: date, end: date, monthly_weights: list[float]
) -> float:
    """Fraction of a typical year covered by ``[start, end]`` inclusive.

    Returns values above 1.0 for periods longer than a year, which the caller
    then simply divides by.
    """
    if end < start:
        return 0.0
    weights = _normalised(monthly_weights)
    total = 0.0
    cursor = start
    cache: dict[int, dict[date, float]] = {}
    while cursor <= end:
        year_weights = cache.get(cursor.year)
        if year_weights is None:
            year_weights = _daily_weights(cursor.year, weights)
            cache[cursor.year] = year_weights
        total += year_weights.get(cursor, 0.0)
        cursor += timedelta(days=1)
    return total


def annual_estimate(
    observed_value: float,
    start: date,
    end: date,
    monthly_weights: list[float],
    min_share: float = 0.05,
) -> float | None:
    """Scale an observation over ``[start, end]`` up to a full year.

    Returns ``None`` while too little of the year has been seen -- an annual
    figure from two sunny weeks in June is not an estimate, it is a decoration.
    """
    share = period_share(start, end, monthly_weights)
    if share < min_share:
        return None
    return observed_value / share


#: Beyond this the projection is not an estimate, it is a decoration.  A PV
#: plant lasts twenty-five to thirty years; a hundred is generous and still
#: far inside what a date can hold.
MAX_AMORTISATION_MONTHS = 100 * 12


@dataclass(frozen=True, slots=True)
class Amortisation:
    investment_eur: float
    saved_total_eur: float
    annual_saving_eur: float | None
    progress_pct: float
    months_remaining: float | None
    target_date: date | None


def amortisation(
    investment_eur: float,
    saved_total_eur: float,
    annual_saving_eur: float | None,
    today: date,
) -> Amortisation:
    progress = (
        min(100.0, saved_total_eur / investment_eur * 100.0)
        if investment_eur > 0
        else 100.0
    )
    remaining_eur = max(0.0, investment_eur - saved_total_eur)
    months: float | None = None
    target: date | None = None
    if annual_saving_eur and annual_saving_eur > 0:
        months = remaining_eur / (annual_saving_eur / 12.0)
        if months > MAX_AMORTISATION_MONTHS:
            # At this rate it does not amortise, and saying so is the honest
            # answer.  Projecting anyway produced dates in the fifty-sixth
            # century and, when the rate was small enough, an OverflowError
            # that took the whole coordinator down on every refresh.
            months = None
        else:
            target = today + timedelta(days=int(months * 30.44))
    return Amortisation(
        investment_eur=investment_eur,
        saved_total_eur=saved_total_eur,
        annual_saving_eur=annual_saving_eur,
        progress_pct=round(progress, 2),
        months_remaining=round(months, 1) if months is not None else None,
        target_date=target,
    )
