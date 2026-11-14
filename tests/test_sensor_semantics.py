"""What the score sensors publish, checked on the source.

sensor.py imports Home Assistant, so it is read as an AST like in
test_translations.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "pvstrings"


def _descriptions() -> dict[str, ast.Call]:
    tree = ast.parse((ROOT / "sensor.py").read_text())
    found: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PlantSensorDescription"):
            continue
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                found[kw.value.value] = node
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _string_constants(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_every_bias_sensor_reports_per_day():
    """The hourly mean bias looks like zero on a plant that runs 0.3 kWh/day off."""
    bias = {key: call for key, call in _descriptions().items() if key.startswith("bias_")}
    assert {"bias_7d", "bias_day_ahead_30d"} <= set(bias)
    for key, call in bias.items():
        fields = _string_constants(_keyword(call, "value_fn"))
        assert "daily_bias_kwh" in fields, key
        assert "bias" not in fields, key


def test_every_bias_sensor_carries_the_score_attributes():
    for key, call in _descriptions().items():
        if key.startswith("bias_"):
            assert _keyword(call, "attrs_fn") is not None, key
