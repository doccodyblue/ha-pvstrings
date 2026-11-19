"""The outlook run must leave the live forecast's state as it found it."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.pvstrings.coordinator import PvStringsCoordinator

DAY = 86400
D0 = 1_800_000_000 - 1_800_000_000 % DAY


class _Engine:
    def __init__(self, fail: bool):
        self.fail = fail
        self.last_nowcast = "live-state"
        self.last_nowcast_reason = "live-reason"
        self.calls = []

    def forecast(self, now_ts, hours, start_ts):
        self.calls.append((start_ts, hours))
        self.last_nowcast, self.last_nowcast_reason = "outlook", "outlook"
        if self.fail:
            raise RuntimeError("boom")
        return [
            SimpleNamespace(ts_utc=start_ts + 3600 * i, potential_kwh=0.5)
            for i in range(hours)
        ]


def _coordinator(fail: bool):
    return SimpleNamespace(
        engine=_Engine(fail),
        _outlook_failed=False,
        _local_midnight=lambda day_start, days: day_start + days * DAY,
    )


def test_success_restores_nowcast_and_reads_its_own_window():
    co = _coordinator(fail=False)
    out = PvStringsCoordinator._yield_outlook(co, D0 + 3600, D0)
    assert co.engine.calls == [(D0 + 3 * DAY, 72)]
    assert (co.engine.last_nowcast, co.engine.last_nowcast_reason) == (
        "live-state",
        "live-reason",
    )
    assert [kwh for _day, kwh in out] == [12.0, 12.0, 12.0]
    assert len(out) == 3 and all(len(day) == 10 for day, _ in out)


def test_failure_restores_nowcast_and_publishes_nothing():
    co = _coordinator(fail=True)
    out = PvStringsCoordinator._yield_outlook(co, D0 + 3600, D0)
    assert (co.engine.last_nowcast, co.engine.last_nowcast_reason) == (
        "live-state",
        "live-reason",
    )
    assert [kwh for _day, kwh in out] == [None, None, None]
    assert co._outlook_failed is True
