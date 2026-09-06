"""What a window of energy met, in tariff terms.

A read model, and deliberately nothing more.  The store fills it from SQL, and
``economics`` turns it into money -- neither imports the other, and the shape
between them is written down once, here.

Why sums of products rather than a series of hours: the lifetime window reaches
back to commissioning, so a per-hour valuation in Python would pull years times
8760 rows on every refresh.  SQLite clamps and multiplies per hour and hands
back one row, so the cost stops depending on how long the plant has been
running -- or on how many distinct prices the tariff has, which is what ruled
out grouping by price instead.

Everything here is already clamped per hour.  That is the one thing the caller
cannot redo afterwards: ``sum(min(export_h, delivered_h))`` is not recoverable
from window totals, and it is the number a time-of-use split needs.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Where the price for a kilowatt-hour came from.  Named rather than merely
#: applied, in the same spirit as the delivery basis: a total that rests half
#: on a tariff sensor and half on a configured guess has to be able to say
#: which half is which.
PRICE_RECORDED = "recorded"
PRICE_CONFIGURED = "configured"


@dataclass(frozen=True, slots=True)
class PricedTotals:
    """One window of delivered energy, already clamped and multiplied.

    Every ``*_value`` is a sum of energy times the price recorded for that
    hour, and its ``*_priced_kwh`` sibling is the energy behind it.  The
    difference to the corresponding total is the energy no price covered, which
    is what the valuation falls back on the configured tariff for.  Keeping the
    two apart is what makes the fallback visible instead of assumed.
    """

    delivered_kwh: float = 0.0
    #: Grid export, capped per hour at what the strings delivered in that hour.
    export_kwh: float = 0.0
    self_used_kwh: float = 0.0
    #: Export beyond what the strings made in the same hour: a battery
    #: discharging, a second generator behind the meter, or a reversed meter
    #: sign.  Not credited to PV, and reported so the difference is not silent.
    dropped_export_kwh: float = 0.0
    hours: int = 0
    hours_priced: int = 0

    self_used_priced_kwh: float = 0.0
    self_used_value: float = 0.0
    export_priced_kwh: float = 0.0
    export_value: float = 0.0
    delivered_priced_in_kwh: float = 0.0
    delivered_value_in: float = 0.0
    delivered_priced_out_kwh: float = 0.0
    delivered_value_out: float = 0.0


def flat_totals(delivered_kwh: float, export_kwh: float | None) -> PricedTotals:
    """The same shape for a caller that has only window scalars.

    Used by the scalar ``savings()`` API, which predates any price series and
    still has to answer identically.  Nothing is priced, so the valuation falls
    back on the configured tariff for all of it -- and because a single window
    is also a single bucket, the clamp lands exactly where it always did.
    """
    exported = min(max(0.0, export_kwh or 0.0), delivered_kwh)
    return PricedTotals(
        delivered_kwh=delivered_kwh,
        export_kwh=exported,
        self_used_kwh=max(0.0, delivered_kwh - exported),
    )


def without_export(totals: PricedTotals) -> PricedTotals:
    """Everything behind the house load, for a plant with no grid meter.

    The correct assumption for a small plant, and stated in the diagnostics
    rather than inferred from a missing entity.
    """
    from dataclasses import replace

    return replace(
        totals,
        export_kwh=0.0,
        export_priced_kwh=0.0,
        export_value=0.0,
        dropped_export_kwh=0.0,
        self_used_kwh=totals.delivered_kwh,
        self_used_priced_kwh=totals.delivered_priced_in_kwh,
        self_used_value=totals.delivered_value_in,
    )
