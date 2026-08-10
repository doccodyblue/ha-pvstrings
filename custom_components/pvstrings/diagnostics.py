"""Diagnostics download.

Deliberately verbose: the geometry history, the model state and the collector
counters are exactly what is needed to tell "the forecast is wrong" apart from
"the data going in is wrong", and neither is guessable from the entity states.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PvStringsConfigEntry
from .const import SUBENTRY_GROUP, SUBENTRY_STRING

TO_REDACT = {"latitude", "longitude", "elevation"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PvStringsConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    plant = coordinator.plant

    def collect() -> dict[str, Any]:
        return {
            "store": coordinator.store.statistics(),
            "geometry": {
                string.string_id: [
                    {
                        "valid_from_ts_utc": segment.valid_from_ts_utc,
                        "azimuth_deg": segment.azimuth_deg,
                        "tilt_deg": segment.tilt_deg,
                        "kwp": segment.kwp,
                        "temp_coeff": segment.temp_coeff,
                        "note": segment.note,
                    }
                    for segment in coordinator.store.geometry_history(string.string_id)
                ]
                for string in plant.strings
            },
            "shading_observations": {
                string.string_id: coordinator.store.shading_count(string.string_id)
                for string in plant.strings
            },
        }

    stored = await hass.async_add_executor_job(collect)
    data = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subentries": {
            subentry_id: {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": dict(subentry.data),
            }
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type in (SUBENTRY_STRING, SUBENTRY_GROUP)
        },
        "plant": {
            "strings": [
                {
                    "id": string.string_id,
                    "name": string.name,
                    "power_entity": string.power_entity,
                    "group": string.curtailment_group_id,
                    "mount_type": string.mount_type,
                    "efficiency": plant.efficiency_of(string.string_id),
                }
                for string in plant.strings
            ],
            "groups": [
                {
                    "id": group.group_id,
                    "name": group.name,
                    "has_limit": group.has_limit,
                    "battery_coupled": group.battery_coupled,
                    "inverter_max_ac_w": group.inverter_max_ac_w,
                }
                for group in plant.groups
            ],
            "forecast_source": plant.forecast_source,
            "forecast_model": plant.forecast_model,
            "transposition_model": plant.transposition_model,
            "watchdog_seconds": plant.watchdog_seconds,
            "learning_enabled": plant.learning_enabled,
            "economics_mode": plant.economics.mode,
        },
        "collector": coordinator.collector.stats.as_dict(),
        "scores": data.scores if data else None,
        "model": data.model_summary if data else None,
        "last_learn_cycle": data.learn_stats if data else None,
        "weather": {
            "ok": data.weather_ok if data else None,
            "error": data.weather_error if data else None,
        },
        **stored,
    }
