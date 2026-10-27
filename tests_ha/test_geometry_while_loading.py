"""A geometry edit submitted while the entry reloads must not half-save.

27 Oct 2026: two strings corrected back to back.  The second form was sent
while the first edit's reload was still running, so the subentry took the new
azimuth while the database kept the old plane -- and the forecast with it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.pvstrings import config_flow as cf


def _flow(runtime_data):
    flow = cf.StringSubentryFlow()
    flow.hass = SimpleNamespace()
    flow.handler = ("entry", "string")
    flow.flow_id = "flow"
    entry = SimpleNamespace(runtime_data=runtime_data)
    subentry = SimpleNamespace(subentry_id="s4")
    flow._get_entry = lambda: entry
    flow._get_reconfigure_subentry = lambda: subentry
    flow._pending = {
        "name": "S4", "azimuth": 119.0, "tilt": 24.0, "kwp": 0.45,
        "temp_coeff": -0.004,
    }
    saved = []
    flow.async_update_and_abort = lambda *a, **kw: saved.append(kw) or {"type": "abort"}
    return flow, saved


def test_nothing_is_saved_while_the_entry_is_not_loaded():
    flow, saved = _flow(runtime_data=None)
    result = asyncio.run(
        flow.async_step_geometry({"geometry_mode": "correction", "note": ""})
    )
    assert saved == []
    assert result["type"] == "form"
    assert result["errors"] == {"base": "not_loaded"}
