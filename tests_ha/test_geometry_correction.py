"""Correcting a geometry whose history has more than one period.

S2 on the reference plant: configured 200/70, measured 22 Sep as 190/55 and
saved as a new period -- but 200/70 had never been true.  "Wrong from the
start" only replaced the newest period, and on a two-period history it tried
to move that period to the beginning, onto the one already there.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.config import GeometrySegment
from custom_components.pvstrings import config_flow as cf

SWITCH = 1_790_000_000
NOW = 1_795_000_000
OLD = [
    GeometrySegment(0, azimuth_deg=200, tilt_deg=70, kwp=1.0),
    GeometrySegment(SWITCH, azimuth_deg=190, tilt_deg=55, kwp=1.0),
]


class FakeStore:
    def __init__(self, history):
        self.history = list(history)
        self.calls = []

    def geometry_history(self, _sid):
        return list(self.history)

    def replace_latest_geometry(self, sid, segment):
        self.calls.append(("latest", segment))
        self.history[-1] = segment

    def replace_all_geometry(self, sid, segment):
        self.calls.append(("all", segment))
        self.history = [segment]

    def add_geometry(self, sid, segment):
        self.calls.append(("add", segment))


class FakeCoordinator:
    def __init__(self, store):
        self.store = store
        self.rebased = []

    async def async_rebase_periods(self, sid, periods, trusted=False):
        self.rebased.append((sid, periods, trusted))
        return {}


def _flow(history, pending, monkeypatch):
    store = FakeStore(history)
    coordinator = FakeCoordinator(store)

    async def run(func, *args):
        return func(*args)

    flow = cf.StringSubentryFlow()
    flow.hass = SimpleNamespace(async_add_executor_job=run)
    flow.handler = ("entry", "string")
    flow.flow_id = "flow"
    entry = SimpleNamespace(runtime_data=coordinator)
    flow._get_entry = lambda: entry
    flow._get_reconfigure_subentry = lambda: SimpleNamespace(subentry_id="s2")
    flow._pending = {"name": "S2", "temp_coeff": -0.004, **pending}
    flow.async_update_and_abort = lambda *a, **kw: {"type": "abort"}
    monkeypatch.setattr(cf.dt_util, "utcnow", lambda: SimpleNamespace(timestamp=lambda: NOW))
    return flow, store, coordinator


def _modes(result):
    for key, selector in result["data_schema"].schema.items():
        if str(key) == "geometry_mode":
            return [o if isinstance(o, str) else o["value"] for o in selector.config["options"]]
    raise AssertionError("no geometry_mode field")


def test_replacing_the_whole_history_is_offered_only_when_there_is_one(monkeypatch):
    flow, *_ = _flow(OLD[:1], {"azimuth": 190, "tilt": 55, "kwp": 1.0}, monkeypatch)
    assert "correction_all" not in _modes(asyncio.run(flow.async_step_geometry()))
    flow, *_ = _flow(OLD, {"azimuth": 190, "tilt": 55, "kwp": 1.0}, monkeypatch)
    assert "correction_all" in _modes(asyncio.run(flow.async_step_geometry()))


def test_always_so_replaces_every_period_and_carries_each_over(monkeypatch):
    flow, store, coordinator = _flow(OLD, {"azimuth": 190, "tilt": 55, "kwp": 1.0}, monkeypatch)
    asyncio.run(flow.async_step_geometry({"geometry_mode": "correction_all", "note": ""}))

    assert [(h.valid_from_ts_utc, h.azimuth_deg, h.tilt_deg) for h in store.history] == [(0, 190, 55)]
    (sid, periods, trusted), = coordinator.rebased
    # Only the period that differs is moved, from its own start to its end.
    assert sid == "s2" and trusted
    assert [(p.azimuth_deg, p.tilt_deg, since, until) for p, since, until in periods] == [
        (200, 70, 0, SWITCH)
    ]


def test_wrong_from_the_start_of_the_last_period_keeps_its_start(monkeypatch):
    flow, store, coordinator = _flow(OLD, {"azimuth": 192, "tilt": 55, "kwp": 1.0}, monkeypatch)
    asyncio.run(flow.async_step_geometry({"geometry_mode": "correction", "note": ""}))

    assert [(h.valid_from_ts_utc, h.azimuth_deg) for h in store.history] == [(0, 200), (SWITCH, 192)]
    (_sid, periods, _t), = coordinator.rebased
    assert [(p.azimuth_deg, since, until) for p, since, until in periods] == [(190, SWITCH, NOW)]


def test_an_unchanged_plane_can_still_be_declared_always_so(monkeypatch):
    """S2's case: the values are already right, only the history is not."""
    flow, store, coordinator = _flow(OLD, {"azimuth": 190, "tilt": 55, "kwp": 1.0}, monkeypatch)
    flow._geometry_values_changed = False
    form = asyncio.run(flow.async_step_geometry())
    assert _modes(form) == ["keep", "correction_all"]

    asyncio.run(flow.async_step_geometry({"geometry_mode": "correction_all", "note": ""}))
    assert [(h.valid_from_ts_utc, h.azimuth_deg) for h in store.history] == [(0, 190)]
    (_sid, periods, _t), = coordinator.rebased
    assert [(p.azimuth_deg, since, until) for p, since, until in periods] == [(200, 0, SWITCH)]


def test_keeping_the_periods_writes_nothing(monkeypatch):
    flow, store, coordinator = _flow(OLD, {"azimuth": 190, "tilt": 55, "kwp": 1.0}, monkeypatch)
    flow._geometry_values_changed = False
    asyncio.run(flow.async_step_geometry({"geometry_mode": "keep", "note": ""}))
    assert store.calls == [] and coordinator.rebased == []
