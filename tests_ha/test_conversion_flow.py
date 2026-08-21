"""Conversion-layer pieces that need the HA install: parser, migration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.pvstrings import async_migrate_entry
from custom_components.pvstrings import config_flow as cf


class TestCurveParser:
    def test_roundtrip(self):
        points = cf._parse_curve("5:0.90, 20:0.955, 50:0.967, 100:0.962")
        assert points == [[5.0, 0.90], [20.0, 0.955], [50.0, 0.967], [100.0, 0.962]]
        assert cf._parse_curve(cf._curve_to_text(points)) == points

    def test_rejects_garbage(self):
        assert cf._parse_curve("") is None
        assert cf._parse_curve("5:0.9") is None  # one point is a level, not a curve
        assert cf._parse_curve("5:0.9, 5:0.95") is None  # load must increase
        assert cf._parse_curve("5:0.9, 20:1.2") is None  # efficiency > 1
        assert cf._parse_curve("5:0.9, 20:0.3") is None  # below the 0.5 floor
        assert cf._parse_curve("5;0.9, 20;0.95") is None  # wrong separator

    def test_json_roundtrip_shape(self):
        """HA persists entries as JSON: tuples come back as lists.

        The parser emits lists and the text serialiser accepts both, so a
        stored curve survives save -> restart -> reconfigure unchanged.
        """
        import json

        points = cf._parse_curve("10:0.93, 50:0.96")
        restored = json.loads(json.dumps(points))
        assert restored == points
        assert cf._curve_to_text(restored) == cf._curve_to_text(points)


class TestMigration:
    def _hass_with_recorder(self, calls: list) -> SimpleNamespace:
        def update(entry, **kwargs):
            calls.append(kwargs)

        return SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update)
        )

    def test_minor_bump_is_stamped_once(self):
        calls: list = []
        entry = SimpleNamespace(version=1, minor_version=1)
        assert asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == [{"minor_version": 2}]

    def test_current_minor_is_untouched(self):
        calls: list = []
        entry = SimpleNamespace(version=1, minor_version=2)
        assert asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == []

    def test_future_major_is_refused(self):
        calls: list = []
        entry = SimpleNamespace(version=2, minor_version=1)
        assert not asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == []
