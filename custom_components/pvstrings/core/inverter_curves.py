"""Loader for shipped datasheet efficiency curves.

Blocking file I/O -- call from the executor.  Package-relative paths only:
the working directory under HA is not the component directory.  A broken
file degrades to "model unavailable" with a log line, never to a failed
setup -- the conversion then runs neutral, which is the documented
behaviour for a missing curve.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_MODELS_DIR = Path(__file__).resolve().parent.parent / "inverter_models"


def _valid(points: list) -> bool:
    if not isinstance(points, list) or len(points) < 2:
        return False
    last_load = -1.0
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            return False
        load, efficiency = point
        if not (0.0 <= float(load) <= 150.0 and 0.5 < float(efficiency) <= 1.0):
            return False
        if float(load) <= last_load:  # strictly increasing in load
            return False
        last_load = float(load)
    return True


def load_curves(
    model_ids: tuple[str, ...],
) -> dict[str, tuple[tuple[float, float], ...]]:
    """Load every listed model's curve; skip broken files with a log."""
    curves: dict[str, tuple[tuple[float, float], ...]] = {}
    for model_id in model_ids:
        path = _MODELS_DIR / f"{model_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            points = data.get("points") if isinstance(data, dict) else None
            if not _valid(points):
                raise ValueError("invalid curve points")
            curves[model_id] = tuple(
                (float(load), float(efficiency)) for load, efficiency in points
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as err:
            _LOGGER.warning(
                "pvstrings: inverter curve %s unusable (%s); "
                "conversion for this model runs neutral",
                path.name,
                err,
            )
    return curves
