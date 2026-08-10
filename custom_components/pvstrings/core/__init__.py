"""Home-Assistant-free core of the pvstrings integration.

Nothing in this package may import ``homeassistant``.  It is plain Python so it
can be unit tested, benchmarked and run offline against a copy of the database.
"""

from __future__ import annotations

__all__ = [
    "aggregate",
    "config",
    "curtailment",
    "economics",
    "forecast",
    "learning",
    "physics",
    "quality",
    "store",
    "weather",
]
