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
    }[quantity]
