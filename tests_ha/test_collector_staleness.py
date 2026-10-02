"""A weather station that drops off the network keeps its last state in HA.

The collector's watchdog would go on writing that value as a fresh reading
(2 Oct 2026: 4.58 W/m2 from dusk until mid-morning).  Run against HA's real
``State`` so ``last_reported`` is the attribute HA actually maintains.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import State

from custom_components.pvstrings.collector import IRRADIANCE_STALE_S, Collector

NOW = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)


def probe(state: State | None):
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: state))
    return SimpleNamespace(hass=hass)


def reading(reported_ago_s: float, changed_ago_s: float | None = None) -> State:
    reported = NOW - timedelta(seconds=reported_ago_s)
    changed = NOW - timedelta(seconds=changed_ago_s or reported_ago_s)
    return State(
        "sensor.ghi",
        "4.58",
        last_changed=changed,
        last_updated=changed,
        last_reported=reported,
    )


def fresh(state: State | None) -> bool:
    return Collector._reported_recently(probe(state), "sensor.ghi", int(NOW.timestamp()))


def test_a_silent_station_is_not_recorded():
    assert not fresh(reading(IRRADIANCE_STALE_S + 60))


def test_an_unchanged_value_that_keeps_being_reported_is_live():
    """Unchanged but still being written: the station is talking."""
    assert fresh(reading(reported_ago_s=30, changed_ago_s=8 * 3600))


def test_unknown_entities_are_left_to_the_normal_path():
    assert fresh(None)
