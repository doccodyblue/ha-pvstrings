"""Home Assistant side of the shading backfill.

Everything that needs the recorder or the network lives here; the arithmetic
lives in ``core.backfill`` where it can be tested without either.

The recorder keeps hourly long-term statistics for every sensor with a
measurement state class, and it keeps them forever -- they survive the purge
that removes the raw states after ten days.  For an inverter that has been
wired up for a year, that is a year of hourly mean power sitting in the
database, describing exactly the shadows we want to learn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .core import units
from .core.backfill import BACKFILL_WEIGHT, hourly_series, shading_rows_from_history
from .core.weather import OPEN_METEO_ARCHIVE_URL, open_meteo_archive_params

_LOGGER = logging.getLogger(__name__)

HOUR = 3600

#: Retention deletes shading observations past this age, so asking for more
#: history than that hands the user a map that evaporates at the next nightly
#: purge.  Kept in step with ``Store.compact``'s ``shading_days``.
MAX_BACKFILL_DAYS = 730

#: The reanalysis archive trails real time by a few days.  Asking for the
#: last of them returns nulls, which would be read as darkness.
ARCHIVE_LAG_DAYS = 6

#: One request per chunk.  Sixteen months of hourly data in a single call is
#: a large response and an impolite thing to ask of a free service.
CHUNK_DAYS = 120

ARCHIVE_TIMEOUT = 120


async def async_backfill_shading(
    hass: HomeAssistant,
    coordinator: Any,
    days: int,
) -> dict[str, Any]:
    """Reconstruct shading observations from recorder history.

    Returns a summary suitable for handing straight back to the caller as a
    service response.
    """
    plant = coordinator.plant
    entities = {
        string.power_entity: string.string_id
        for string in plant.strings
        if string.power_entity
    }
    if not entities:
        return {"error": "no power entities configured"}

    end = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(days=ARCHIVE_LAG_DAYS)
    start = end - timedelta(days=min(days, MAX_BACKFILL_DAYS))

    stats = await _async_statistics(hass, sorted(entities), start, end)
    if not stats:
        return {"error": "no long-term statistics for the configured entities"}

    power_by_string: dict[str, dict[int, float]] = {}
    scaled: dict[str, str] = {}
    for entity_id, rows in stats.items():
        string_id = entities.get(entity_id)
        if string_id is None:
            continue
        series = hourly_series(rows)
        if not series:
            continue
        # The same conversion the live collector applies.  Recorder statistics
        # are stored in the sensor's own unit, and an inverter that publishes
        # kilowatts would otherwise reconstruct ratios a thousand times too
        # small -- which no downstream guard would recognise as a unit
        # mismatch rather than a very deep shadow.
        factor = _power_factor(hass, entity_id)
        if factor != 1.0:
            series = {hour: value * factor for hour, value in series.items()}
            scaled[entity_id] = _unit_of(hass, entity_id) or "?"
        power_by_string[string_id] = series

    if not power_by_string:
        return {"error": "statistics contained no hourly means"}

    covered = sorted({hour for series in power_by_string.values() for hour in series})
    irradiance, temperature, wind = await _async_archive(
        hass, plant, covered[0], covered[-1]
    )
    if not irradiance:
        return {"error": "irradiance archive returned nothing"}

    result = await hass.async_add_executor_job(
        shading_rows_from_history,
        coordinator.engine.physics,
        power_by_string,
        irradiance,
        coordinator.engine.geometry_at,
        temperature,
        wind,
        plant.efficiency_of,
        _mount_lookup(plant),
    )

    if result.rows:
        await hass.async_add_executor_job(
            coordinator.store.add_shading_obs, result.rows
        )
        await hass.async_add_executor_job(coordinator.engine.fit_shading)
        await coordinator.async_request_refresh()

    summary = result.as_dict()
    if scaled:
        summary["unit_converted"] = scaled
    summary["from"] = datetime.fromtimestamp(covered[0], timezone.utc).date().isoformat()
    summary["to"] = datetime.fromtimestamp(covered[-1], timezone.utc).date().isoformat()
    summary["weight_each"] = BACKFILL_WEIGHT
    summary["map"] = coordinator.engine.shading.summary()
    _LOGGER.info("pvstrings: backfilled %s shading observations", len(result.rows))
    return summary


def _unit_of(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.attributes.get("unit_of_measurement") if state else None


def _power_factor(hass: HomeAssistant, entity_id: str) -> float:
    """How many watts one unit of this sensor is worth."""
    converted = units.convert(1.0, _unit_of(hass, entity_id), units.POWER)
    return 1.0 if converted is None else converted


def _mount_lookup(plant: Any):
    mounts = {string.string_id: string.mount_type for string in plant.strings}
    return lambda string_id: mounts.get(string_id, "open_rack")


async def _async_statistics(
    hass: HomeAssistant,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Hourly means from the recorder, fetched on the recorder's own thread."""
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:  # pragma: no cover - recorder is a default integration
        _LOGGER.warning("pvstrings: recorder unavailable, cannot backfill")
        return {}

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        set(statistic_ids),
        "hour",
        None,
        {"mean"},
    )


async def _async_archive(
    hass: HomeAssistant,
    plant: Any,
    first_hour: int,
    last_hour: int,
) -> tuple[
    dict[int, tuple[float | None, float | None, float | None]],
    dict[int, float],
    dict[int, float],
]:
    """Historical irradiance for the covered range, in chunks."""
    session = async_get_clientsession(hass)
    irradiance: dict[int, tuple[float | None, float | None, float | None]] = {}
    temperature: dict[int, float] = {}
    wind: dict[int, float] = {}

    cursor = datetime.fromtimestamp(first_hour, timezone.utc).date()
    final = datetime.fromtimestamp(last_hour, timezone.utc).date()
    while cursor <= final:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), final)
        params = open_meteo_archive_params(
            plant.latitude, plant.longitude, cursor.isoformat(), chunk_end.isoformat()
        )
        try:
            async with session.get(
                OPEN_METEO_ARCHIVE_URL, params=params, timeout=ARCHIVE_TIMEOUT
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except Exception as err:  # noqa: BLE001 - one bad chunk must not lose the rest
            _LOGGER.warning("pvstrings: archive chunk %s failed: %s", cursor, err)
            cursor = chunk_end + timedelta(days=1)
            continue

        _absorb(payload, irradiance, temperature, wind)
        cursor = chunk_end + timedelta(days=1)

    return irradiance, temperature, wind


def _absorb(
    payload: dict[str, Any],
    irradiance: dict[int, tuple[float | None, float | None, float | None]],
    temperature: dict[int, float],
    wind: dict[int, float],
) -> None:
    """Fold one archive response into the accumulating maps.

    The archive labels a radiation row with the *end* of the hour it averages,
    exactly as the forecast API does, so the same shift applies here.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    for index, stamp in enumerate(times):
        hour = int(stamp) - HOUR
        irradiance[hour] = (
            _at(hourly.get("shortwave_radiation"), index),
            _at(hourly.get("direct_normal_irradiance"), index),
            _at(hourly.get("diffuse_radiation"), index),
        )
        air = _at(hourly.get("temperature_2m"), index)
        if air is not None:
            temperature[hour] = air
        speed = _at(hourly.get("wind_speed_10m"), index)
        if speed is not None:
            wind[hour] = speed


def _at(values: list[Any] | None, index: int) -> float | None:
    if not values or index >= len(values):
        return None
    value = values[index]
    return None if value is None else float(value)
