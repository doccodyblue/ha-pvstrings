"""No zone file is read while the integration sets up.

``PvStringsCoordinator.__init__`` runs on the event loop, and building a
``ZoneInfo`` can read the zone file from disk.  The integration hands the core
Home Assistant's already loaded zone; the core must then never resolve one
itself.  Raised in the HACS review of #10563.
"""

from __future__ import annotations

import ast
import zoneinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import core.physics
from core.forecast import ForecastEngine
from core.physics import PhysicsEngine

COORDINATOR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "pvstrings"
    / "coordinator.py"
)

LAT, LON = 53.5, 10.0
#: 21 June 2025, 12:00 UTC.
SUMMER_NOON = 1_750_507_200
#: Built at import, before any test patches ``ZoneInfo`` away.
_BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def no_zone_lookup(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("ZoneInfo built after setup was handed a zone")

    monkeypatch.setattr(core.physics, "ZoneInfo", refuse)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", refuse)


class TestZoneHandedIn:
    def test_physics_and_engine_use_the_given_zone(self, plant, store, no_zone_lookup):
        tz = _BERLIN
        physics = PhysicsEngine(LAT, LON, time_zone="Europe/Berlin", tz=tz)
        engine = ForecastEngine(plant, store, physics)

        assert physics.tz is tz
        assert engine._tz is tz
        window = physics.daylight_window_for(SUMMER_NOON)
        assert window is not None and window[0] < SUMMER_NOON < window[1]

    def test_zone_name_still_resolves_without_one(self):
        physics = PhysicsEngine(LAT, LON, time_zone="Europe/Berlin")
        assert physics.tz == ZoneInfo("Europe/Berlin")


class TestCoordinatorPassesHomeAssistantsZone:
    def test_physics_gets_default_time_zone(self):
        tree = ast.parse(COORDINATOR.read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PhysicsEngine"
        ]
        assert calls, "coordinator no longer builds a PhysicsEngine"
        for call in calls:
            tz = {kw.arg: kw.value for kw in call.keywords}.get("tz")
            assert tz is not None, "PhysicsEngine built without tz on the event loop"
            assert ast.unparse(tz) == "dt_util.get_default_time_zone()"
