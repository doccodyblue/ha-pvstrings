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
from typing import Any, Iterable, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .core import units
from .core.backfill import (
    BACKFILL_WEIGHT,
    hourly_means_from_statistics,
    hourly_series,
    shading_rows_from_history,
)
from .core.irradiance_check import CURSOR_IRRADIANCE_EPOCH, assess, rows_to_bank
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


#: Two independent references, tried in order.  The satellite product resolves
#: about 5 km against the reanalysis grid's 25, which matters for a point
#: sensor -- but it only covers the Meteosat disc, so the reanalysis has to
#: stay as the fallback for everyone else.  Whichever answered is recorded per
#: row: a verdict is worth what its reference is worth, and the two must be
#: distinguishable afterwards.
IRRADIANCE_REFERENCES: tuple[tuple[str, str, str], ...] = (
    (
        "satellite_radiation_seamless",
        "https://satellite-api.open-meteo.com/v1/archive",
        "satellite_radiation_seamless",
    ),
    ("era5", "https://archive-api.open-meteo.com/v1/archive", "era5"),
)


def _statistics_unit(hass: HomeAssistant, entity_id: str) -> str | None:
    """The unit the statistics are stored in, if the entity still exists."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return state.attributes.get("unit_of_measurement")


async def _async_fill_reference(
    hass: HomeAssistant, coordinator: Any, first: int, last: int
) -> int:
    """Fetch a reference for every banked hour that still lacks one."""
    epoch = coordinator.store.get_cursor(CURSOR_IRRADIANCE_EPOCH, default=0)
    pending = await hass.async_add_executor_job(
        coordinator.store.irradiance_hours_awaiting_reference,
        epoch,
        last + HOUR,
        20000,
    )
    if not pending:
        return 0

    plant = coordinator.plant
    session = async_get_clientsession(hass)
    wanted = set(pending)
    filled = 0
    now = int(datetime.now(timezone.utc).timestamp())

    for name, url, model in IRRADIANCE_REFERENCES:
        if not wanted:
            break
        params = {
            "latitude": plant.latitude,
            "longitude": plant.longitude,
            "start_date": datetime.fromtimestamp(first, timezone.utc)
            .date()
            .isoformat(),
            # The archive labels an hour by its end, so the measurement of
            # 23:00-24:00 needs the stamp at 00:00 the next day.  Asking only
            # to ``last`` leaves the final hour of every request unfilled --
            # which in Sydney is the middle of the afternoon.
            "end_date": datetime.fromtimestamp(last + HOUR, timezone.utc)
            .date()
            .isoformat(),
            "hourly": "shortwave_radiation",
            "timezone": "UTC",
            "timeformat": "unixtime",
            "models": model,
        }
        try:
            async with session.get(
                url, params=params, timeout=ARCHIVE_TIMEOUT
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except Exception:  # noqa: BLE001 - a missing reference is not an error
            _LOGGER.debug("pvstrings: reference %s unavailable", name)
            continue

        hourly = payload.get("hourly") or {}
        stamps = hourly.get("time") or []
        values = hourly.get("shortwave_radiation") or []
        # The archive labels an hour with its end, as the forecast API does.
        updates = [
            (float(value), name, now, int(stamp) - HOUR, int(epoch))
            for stamp, value in zip(stamps, values)
            if value is not None and (int(stamp) - HOUR) in wanted
        ]
        if not updates:
            continue
        written = await hass.async_add_executor_job(
            coordinator.store.fill_irradiance_reference, updates
        )
        filled += written
        wanted -= {row[3] for row in updates}

    return filled


async def async_backfill_irradiance_check(
    hass: HomeAssistant,
    coordinator: Any,
    days: int,
) -> dict[str, Any]:
    """Pair past irradiance readings with an independent reference.

    Deliberately a service and never automatic.  Home Assistant keeps hourly
    statistics long after the raw states are purged, so this can reach back
    months -- but only the owner knows whether the sensor spent those months
    in the same place, clean, and pointing the same way.  A station that was
    moved, replaced, or had a branch grow over it inside the window would
    teach a verdict from a world that no longer exists, and nothing in the
    data would give that away.
    """
    plant = coordinator.plant
    entity = plant.weather_sources.ghi_entity
    if not entity:
        return {"error": "no irradiance sensor configured"}

    end = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(days=ARCHIVE_LAG_DAYS)
    start = end - timedelta(days=min(days, MAX_BACKFILL_DAYS))

    stats = await _async_statistics(hass, [entity], start, end)
    rows = stats.get(entity) or []
    if not rows:
        return {
            "error": "no long-term statistics for the irradiance sensor",
            "entity": entity,
        }

    measured = hourly_means_from_statistics(rows, _statistics_unit(hass, entity))
    if not measured:
        return {"error": "statistics carried no hourly means", "entity": entity}

    first, last = min(measured), max(measured)
    banked = await hass.async_add_executor_job(
        coordinator.store.bank_irradiance_hours,
        list(
            rows_to_bank(
                coordinator.engine.physics,
                measured,
                coordinator.store.get_cursor(CURSOR_IRRADIANCE_EPOCH, default=0),
                "statistics",
            )
        ),
    )
    filled = await _async_fill_reference(hass, coordinator, first, last)

    epoch = coordinator.store.get_cursor(CURSOR_IRRADIANCE_EPOCH, default=0)
    verdict = await hass.async_add_executor_job(
        coordinator.store.irradiance_pairs, epoch
    )
    coordinator.invalidate_irradiance_verdict()

    return {
        "entity": entity,
        "from": datetime.fromtimestamp(first, timezone.utc).date().isoformat(),
        "to": datetime.fromtimestamp(last, timezone.utc).date().isoformat(),
        "hours_measured": len(measured),
        "hours_banked": banked,
        "hours_referenced": filled,
        # The answer, in the same call.  Without it the owner runs a service
        # that reports three counts and has to go looking for the result on a
        # sensor that only refreshes on the hour.
        "verdict": assess(verdict).as_dict(),
        "note": (
            "Only run this if the sensor stayed in the same place, clean and"
            " level, for the whole period. Anything else teaches a verdict"
            " from a sensor that no longer exists."
        ),
    }
