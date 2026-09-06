"""Unit normalisation for entity readings.

The physics chain expects SI-ish canonical units: °C, m/s, hPa, W/m², mm.
Home Assistant entities are under no obligation to agree -- a common weather
station reports wind in km/h, US hardware reports °F and inHg, and feeding
those through unconverted is silent and expensive.  11.2 km/h read as 11.2 m/s
overstates convective cooling by a factor of four.

Conversion is driven by the entity's own ``unit_of_measurement``, so it works
for any station without asking the user to normalise anything first.
"""

from __future__ import annotations

from typing import Final

TEMPERATURE: Final = "temperature"
SPEED: Final = "speed"
PRESSURE: Final = "pressure"
IRRADIANCE: Final = "irradiance"
ILLUMINANCE: Final = "illuminance"
PRECIPITATION: Final = "precipitation"
RATIO: Final = "ratio"
POWER: Final = "power"
ENERGY_PRICE: Final = "energy_price"

#: Numerators that mean a hundredth of the major currency.  Deliberately not a
#: currency table: one would have to list every currency on earth and would
#: lock out the one it forgot.  Only the numerator's *scale* is decided here --
#: which currency it is stays the user's business, because nothing in this
#: integration converts between currencies.
_MINOR_CURRENCY: Final = frozenset(
    {"ct", "c", "cent", "cents", "¢", "p", "pence", "öre", "øre", "ore"}
)

#: Denominators, as a factor onto "per kWh".
_PER_ENERGY: Final[dict[str, float]] = {"kwh": 1.0, "mwh": 0.001, "wh": 1000.0}

#: Multiplicative factors onto the canonical unit, keyed by lowercased symbol.
_FACTORS: Final[dict[str, dict[str, float]]] = {
    SPEED: {
        "m/s": 1.0,
        "ms": 1.0,
        "km/h": 1 / 3.6,
        "kph": 1 / 3.6,
        "mph": 0.44704,
        "mi/h": 0.44704,
        "kn": 0.514444,
        "kt": 0.514444,
        "ft/s": 0.3048,
    },
    PRESSURE: {
        "hpa": 1.0,
        "mbar": 1.0,
        "bar": 1000.0,
        "pa": 0.01,
        "kpa": 10.0,
        "inhg": 33.8639,
        "mmhg": 1.33322,
        "psi": 68.9476,
    },
    IRRADIANCE: {"w/m²": 1.0, "w/m2": 1.0, "kw/m²": 1000.0, "kw/m2": 1000.0},
    ILLUMINANCE: {"lx": 1.0, "lux": 1.0, "klx": 1000.0},
    PRECIPITATION: {
        "mm": 1.0,
        "mm/h": 1.0,   # a rate over one hour is numerically the hourly total
        "cm": 10.0,
        "in": 25.4,
        "in/h": 25.4,
    },
    POWER: {"w": 1.0, "kw": 1000.0, "mw": 1_000_000.0, "va": 1.0, "kva": 1000.0},
    RATIO: {"%": 1.0},
}


def convert(value: float | None, unit: str | None, quantity: str) -> float | None:
    """Return ``value`` in the canonical unit for ``quantity``.

    An unknown unit is passed through unchanged rather than dropped: refusing a
    reading because its symbol is unfamiliar loses more than assuming the
    integration's own convention.  Known-but-different units are converted.
    """
    if value is None:
        return None
    if quantity == TEMPERATURE:
        return _temperature(value, unit)
    if quantity == ENERGY_PRICE:
        # The one quantity that does *not* pass an unreadable unit through.
        # Money is asymmetric: a price whose scale cannot be read is a factor
        # of a hundred or a thousand away from the truth and looks entirely
        # plausible on a savings sensor, while "unknown" merely falls back to
        # the configured flat price.  The config flow refuses such an entity
        # up front; this is what happens if the unit changes afterwards.
        factor = parse_energy_price_unit(unit)
        return None if factor is None else value * factor
    if unit is None:
        return value
    factor = _FACTORS.get(quantity, {}).get(unit.strip().lower())
    return value * factor if factor is not None else value


def _temperature(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    symbol = unit.strip().lower().replace("°", "").replace("deg", "").strip()
    if symbol in ("f", "fahrenheit"):
        return (value - 32.0) * 5.0 / 9.0
    if symbol in ("k", "kelvin"):
        return value - 273.15
    return value


def parse_energy_price_unit(unit: str | None) -> float | None:
    """Factor from ``unit`` onto major currency per kWh, or ``None``.

    ``None`` means the symbol is not a price per energy at all -- "EUR" with no
    denominator, a bare "kWh", an empty string.  Callers use that to refuse an
    entity rather than to guess, which is the whole point: ct/kWh read as
    EUR/kWh is a factor of a hundred, both numbers look ordinary on a savings
    sensor, and nobody re-checks a figure that was never obviously wrong.

    Shared on purpose between the config flow, which blocks on ``None``, and
    the collector, which converts with the factor.  Two implementations of
    this rule would eventually disagree about which units are acceptable, and
    the disagreement would be silent.
    """
    if not unit:
        return None
    numerator, _, denominator = unit.strip().lower().partition("/")
    per_energy = _PER_ENERGY.get(denominator.strip())
    if per_energy is None:
        return None
    return per_energy * (0.01 if numerator.strip() in _MINOR_CURRENCY else 1.0)


def canonical_unit(quantity: str) -> str:
    return {
        TEMPERATURE: "°C",
        SPEED: "m/s",
        PRESSURE: "hPa",
        IRRADIANCE: "W/m²",
        ILLUMINANCE: "lx",
        PRECIPITATION: "mm",
        POWER: "W",
        RATIO: "%",
        # The currency is the user's -- this integration never converts between
        # them -- so only the denominator can be named here.
        ENERGY_PRICE: "/kWh",
    }[quantity]
