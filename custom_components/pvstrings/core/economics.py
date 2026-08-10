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
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Economics

MODE_NET_METERING = "net_metering"
MODE_SELF_CONSUMPTION = "self_consumption"
MODE_FEED_IN = "feed_in"


@dataclass(frozen=True, slots=True)
class SavingsResult:
    yield_kwh: float
    export_kwh: float
    self_used_kwh: float
    saved_eur: float
    mode: str

    @property
    def eur_per_kwh(self) -> float:
        return self.saved_eur / self.yield_kwh if self.yield_kwh > 0 else 0.0


def savings(
    yield_kwh: float, export_kwh: float | None, economics: Economics
) -> SavingsResult:
    """Monetary value of ``yield_kwh`` of production.

    ``export_kwh`` may be ``None`` when the plant has no grid meter configured;
    in that case everything is treated as self-consumed, which is the correct
    assumption for a small balcony plant behind the house load and is stated as
    such in the diagnostics.
    """
    exported = max(0.0, export_kwh or 0.0)
    exported = min(exported, yield_kwh)
    self_used = max(0.0, yield_kwh - exported)

    if economics.mode == MODE_NET_METERING:
        # The meter runs backwards: every exported kWh really does displace an
        # imported one.  True today, temporary by construction.
        saved = yield_kwh * economics.price_per_kwh
    elif economics.mode == MODE_FEED_IN:
        saved = yield_kwh * economics.feed_in_tariff
    else:
        saved = (
            self_used * economics.price_per_kwh + exported * economics.feed_in_tariff
        )

    return SavingsResult(
        yield_kwh=yield_kwh,
        export_kwh=exported,
        self_used_kwh=self_used,
        saved_eur=saved,
        mode=economics.mode,
    )


def scenarios(
    yield_kwh: float, export_kwh: float | None, economics: Economics
) -> dict[str, SavingsResult]:
    """The same production valued under every tariff model."""
    return {
        mode: savings(yield_kwh, export_kwh, economics.with_mode(mode))
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
        target = today + timedelta(days=int(months * 30.44))
    return Amortisation(
        investment_eur=investment_eur,
        saved_total_eur=saved_total_eur,
        annual_saving_eur=annual_saving_eur,
        progress_pct=round(progress, 2),
        months_remaining=round(months, 1) if months is not None else None,
        target_date=target,
    )
