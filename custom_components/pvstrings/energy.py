"""Energy platform: the plant forecast for the HA Energy dashboard.

Selected under Energy settings -> solar production -> forecast.  evcc's
``homeassistant`` solar tariff reads the same ``energy/solar_forecast``
websocket call, so this also feeds evcc.  Keys are the start of each hour in
UTC (evcc treats them as slot start), values Wh.  The series is
``PvStringsData.energy_hourly``: the forecast where a production meter sits,
not DC.  No coordinator import -- this module is loaded by the energy
component and must stay light.
"""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    """Hourly Wh for one PVStrings entry, or None while it has no forecast."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        return None
    coordinator = getattr(entry, "runtime_data", None)
    data = getattr(coordinator, "data", None)
    hourly = getattr(data, "energy_hourly", None)
    if not hourly:
        return None
    return {
        "wh_hours": {
            datetime.fromtimestamp(ts, UTC).isoformat(): round(kwh * 1000.0, 1)
            for ts, kwh in hourly
        }
    }
