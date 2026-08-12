"""The pvstrings integration.

Per-string PV forecasting: physics first, learned correction of the residual.

A config entry is one plant.  Subentries are strings and curtailment groups, so
adding a string never rewrites the plant's configuration and every string gets
its own device.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_STRING_ID,
    CONF_ALBEDO,
    CONF_AZIMUTH,
    CONF_BATTERY_COUPLED,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_COMMISSIONING,
    CONF_ECONOMICS_MODE,
    CONF_ELEVATION,
    CONF_ENERGY_ENTITY,
    CONF_EXPORT_LIMIT,
    CONF_FEED_IN,
    CONF_FORECAST_MODEL,
    CONF_FORECAST_SOURCE,
    CONF_GHI_ENTITY,
    CONF_GRID_POWER,
    CONF_GROUP_ID,
    CONF_HOUSE_LOAD,
    CONF_HUMIDITY_ENTITY,
    CONF_ILLUMINANCE_ENTITY,
    CONF_INVERTER_MAX_AC,
    CONF_INVESTMENT,
    CONF_KWP,
    CONF_LATITUDE,
    CONF_LEARNING_ENABLED,
    CONF_LIMIT_ABS_ENTITY,
    CONF_LIMIT_ENTITY,
    CONF_LONGITUDE,
    CONF_MAX_POWER,
    CONF_MOUNT_TYPE,
    CONF_NOTE,
    CONF_POWER_ENTITY,
    CONF_PRESSURE_ENTITY,
    CONF_PRICE,
    CONF_RAIN_ENTITY,
    CONF_RETENTION_DAYS,
    CONF_SOC_ENTITY,
    CONF_SOC_LIMIT,
    CONF_STRING_EFFICIENCY,
    CONF_SYSTEM_EFFICIENCY,
    CONF_TEMP_COEFF,
    CONF_TEMPERATURE_ENTITY,
    CONF_TILT,
    CONF_TRANSPOSITION,
    CONF_WATCHDOG,
    CONF_WEATHER_ENTITY,
    CONF_WIND_ENTITY,
    DEFAULT_DB_DIR,
    DOMAIN,
    NO_GROUP,
    SERVICE_ADD_GEOMETRY,
    SERVICE_BACKFILL,
    SERVICE_PURGE,
    SERVICE_RECALCULATE,
    SERVICE_RESET_LEARNING,
    SUBENTRY_GROUP,
    SUBENTRY_STRING,
)
from .coordinator import PvStringsCoordinator
from .core.config import (
    DEFAULT_ALBEDO,
    DEFAULT_SYSTEM_EFFICIENCY,
    DEFAULT_TEMP_COEFF,
    DEFAULT_WATCHDOG_SECONDS,
    ConfigError,
    CurtailmentGroup,
    Economics,
    GeometrySegment,
    PlantConfig,
    PlantState,
    StringConfig,
    WeatherSources,
)
from .core.store import Store

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type PvStringsConfigEntry = ConfigEntry[PvStringsCoordinator]

ADD_GEOMETRY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_STRING_ID): cv.string,
        vol.Required(CONF_AZIMUTH): vol.All(vol.Coerce(float), vol.Range(0, 360)),
        vol.Required(CONF_TILT): vol.All(vol.Coerce(float), vol.Range(0, 90)),
        vol.Required(CONF_KWP): vol.All(vol.Coerce(float), vol.Range(min=0.01)),
        vol.Optional(CONF_TEMP_COEFF): vol.All(
            vol.Coerce(float), vol.Range(-0.01, 0.0)
        ),
        vol.Optional("valid_from"): cv.date,
        vol.Optional(CONF_NOTE): cv.string,
    }
)

ENTRY_SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})
BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        # Bounded by how long shading observations survive the nightly
        # purge: offering four years would report a fine-looking result and
        # then lose half of it overnight.
        vol.Optional("days", default=540): vol.All(
            vol.Coerce(int), vol.Range(min=7, max=730)
        ),
    }
)


# --------------------------------------------------------------------------- #
# configuration assembly
# --------------------------------------------------------------------------- #


def _merged(entry: ConfigEntry) -> dict[str, Any]:
    """Options win over data -- that is what the options flow edits."""
    return {**entry.data, **entry.options}


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parsed = dt_util.parse_date(str(value))
    return parsed


def build_plant_config(hass: HomeAssistant, entry: ConfigEntry) -> PlantConfig:
    """Turn a config entry plus its subentries into a core ``PlantConfig``."""
    config = _merged(entry)

    groups: list[CurtailmentGroup] = []
    strings: list[StringConfig] = []

    for subentry_id, subentry in entry.subentries.items():
        data = subentry.data
        if subentry.subentry_type == SUBENTRY_GROUP:
            groups.append(
                CurtailmentGroup(
                    group_id=subentry_id,
                    name=subentry.title,
                    limit_entity=data.get(CONF_LIMIT_ENTITY) or None,
                    limit_abs_entity=data.get(CONF_LIMIT_ABS_ENTITY) or None,
                    inverter_max_ac_w=data.get(CONF_INVERTER_MAX_AC) or None,
                    battery_coupled=bool(data.get(CONF_BATTERY_COUPLED, False)),
                    soc_entity=data.get(CONF_SOC_ENTITY) or None,
                    soc_limit_pct=float(data.get(CONF_SOC_LIMIT, 100.0)),
                    battery_power_entity=data.get(CONF_BATTERY_POWER) or None,
                    export_limit_w=data.get(CONF_EXPORT_LIMIT),
                )
            )

    known_groups = {group.group_id for group in groups}

    for subentry_id, subentry in entry.subentries.items():
        data = subentry.data
        if subentry.subentry_type != SUBENTRY_STRING:
            continue
        group_id = data.get(CONF_GROUP_ID)
        if group_id in (NO_GROUP, "", None) or group_id not in known_groups:
            group_id = None
        strings.append(
            StringConfig(
                string_id=subentry_id,
                name=subentry.title,
                power_entity=data[CONF_POWER_ENTITY],
                curtailment_group_id=group_id,
                energy_entity=data.get(CONF_ENERGY_ENTITY) or None,
                system_efficiency=data.get(CONF_STRING_EFFICIENCY),
                mount_type=data.get(CONF_MOUNT_TYPE, "insulated_back"),
                max_power_w=data.get(CONF_MAX_POWER) or None,
            )
        )

    economics = Economics(
        mode=config.get(CONF_ECONOMICS_MODE, "self_consumption"),
        price_per_kwh=float(config.get(CONF_PRICE, 0.30)),
        feed_in_tariff=float(config.get(CONF_FEED_IN, 0.08)),
        investment_eur=float(config.get(CONF_INVESTMENT, 0.0)),
        commissioning_date=_parse_date(config.get(CONF_COMMISSIONING)),
        battery_efficiency=float(config.get(CONF_BATTERY_EFFICIENCY, 0.90)),
    )

    return PlantConfig(
        name=entry.title,
        latitude=float(config.get(CONF_LATITUDE, hass.config.latitude)),
        longitude=float(config.get(CONF_LONGITUDE, hass.config.longitude)),
        elevation_m=float(config.get(CONF_ELEVATION, hass.config.elevation or 0)),
        time_zone=hass.config.time_zone or "UTC",
        albedo=float(config.get(CONF_ALBEDO, DEFAULT_ALBEDO)),
        system_efficiency=float(
            config.get(CONF_SYSTEM_EFFICIENCY, DEFAULT_SYSTEM_EFFICIENCY)
        ),
        transposition_model=config.get(CONF_TRANSPOSITION, "perez-driesse"),
        watchdog_seconds=int(config.get(CONF_WATCHDOG, DEFAULT_WATCHDOG_SECONDS)),
        forecast_source=config.get(CONF_FORECAST_SOURCE, "open_meteo"),
        forecast_model=config.get(CONF_FORECAST_MODEL, "best_match"),
        strings=tuple(strings),
        groups=tuple(groups),
        economics=economics,
        plant_state=PlantState(
            battery_soc_entity=config.get(CONF_BATTERY_SOC) or None,
            battery_power_entity=config.get(CONF_BATTERY_POWER) or None,
            grid_power_entity=config.get(CONF_GRID_POWER) or None,
            house_load_entity=config.get(CONF_HOUSE_LOAD) or None,
        ),
        weather_sources=WeatherSources(
            temperature_entity=config.get(CONF_TEMPERATURE_ENTITY) or None,
            humidity_entity=config.get(CONF_HUMIDITY_ENTITY) or None,
            wind_speed_entity=config.get(CONF_WIND_ENTITY) or None,
            rain_entity=config.get(CONF_RAIN_ENTITY) or None,
            pressure_entity=config.get(CONF_PRESSURE_ENTITY) or None,
            ghi_entity=config.get(CONF_GHI_ENTITY) or None,
            illuminance_entity=config.get(CONF_ILLUMINANCE_ENTITY) or None,
        ),
        learning_enabled=bool(config.get(CONF_LEARNING_ENABLED, True)),
        retention_days=int(config.get(CONF_RETENTION_DAYS, 90)),
    )


def seed_geometry(store: Store, entry: ConfigEntry) -> None:
    """Write an initial geometry segment for strings that have no history yet.

    Only ever seeds.  Changing a mounting angle later goes through the subentry
    reconfigure flow, which appends a new validity period instead of rewriting
    the past.
    """
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_STRING:
            continue
        if store.geometry_history(subentry_id):
            continue
        data = subentry.data
        store.add_geometry(
            subentry_id,
            GeometrySegment(
                valid_from_ts_utc=0,
                azimuth_deg=float(data[CONF_AZIMUTH]),
                tilt_deg=float(data[CONF_TILT]),
                kwp=float(data[CONF_KWP]),
                temp_coeff=float(data.get(CONF_TEMP_COEFF, DEFAULT_TEMP_COEFF)),
                note="initial configuration",
            ),
        )


def string_device_info(entry: ConfigEntry, string_id: str, name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{string_id}")},
        name=name,
        manufacturer="pvstrings",
        model="PV string",
        via_device=(DOMAIN, entry.entry_id),
    )


def plant_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="pvstrings",
        model="PV plant",
        entry_type=DeviceEntryType.SERVICE,
    )


# --------------------------------------------------------------------------- #
# setup / teardown
# --------------------------------------------------------------------------- #


async def async_setup_entry(hass: HomeAssistant, entry: PvStringsConfigEntry) -> bool:
    try:
        plant = build_plant_config(hass, entry)
    except (ConfigError, KeyError) as err:
        _LOGGER.error("pvstrings: invalid configuration: %s", err)
        return False

    db_path = Path(hass.config.path(DEFAULT_DB_DIR)) / f"{entry.entry_id}.db"
    store = Store(db_path)

    def _open() -> None:
        store.connect()
        seed_geometry(store, entry)

    try:
        await hass.async_add_executor_job(_open)
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"cannot open {db_path}: {err}") from err

    coordinator = PvStringsCoordinator(hass, entry, plant, store)
    await coordinator.async_prepare(
        weather_entity=_merged(entry).get(CONF_WEATHER_ENTITY)
    )

    # First refresh must not hard-fail the setup: the physics forecast works
    # without any history, but a cold start with no weather data yet should
    # still leave the entities in place.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvStringsConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        await hass.async_add_executor_job(coordinator.store.close)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: Any
) -> bool:
    """Allow removing a string device once its subentry is gone."""
    return not any(
        identifier[1] == f"{entry.entry_id}_{subentry_id}"
        for identifier in device_entry.identifiers
        for subentry_id in entry.subentries
    )


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #


def _coordinator_for(hass: HomeAssistant, entry_id: str) -> PvStringsCoordinator:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        raise ServiceValidationError(f"unknown pvstrings config entry: {entry_id}")
    return entry.runtime_data


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RECALCULATE):
        return

    async def _recalculate(call: ServiceCall) -> None:
        await _coordinator_for(
            hass, call.data[ATTR_CONFIG_ENTRY_ID]
        ).async_recalculate()

    async def _reset_learning(call: ServiceCall) -> None:
        await _coordinator_for(
            hass, call.data[ATTR_CONFIG_ENTRY_ID]
        ).async_reset_learning()

    async def _purge(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call.data[ATTR_CONFIG_ENTRY_ID])
        await hass.async_add_executor_job(
            coordinator.store.compact,
            int(dt_util.utcnow().timestamp()),
            coordinator.plant.retention_days,
        )
        await hass.async_add_executor_job(coordinator.store.vacuum)

    async def _backfill(call: ServiceCall) -> dict[str, Any]:
        from .backfill import async_backfill_shading

        coordinator = _coordinator_for(hass, call.data[ATTR_CONFIG_ENTRY_ID])
        return await async_backfill_shading(hass, coordinator, call.data["days"])

    async def _add_geometry(call: ServiceCall) -> None:
        coordinator = _coordinator_for(hass, call.data[ATTR_CONFIG_ENTRY_ID])
        string_id = call.data[ATTR_STRING_ID]
        if string_id not in {s.string_id for s in coordinator.plant.strings}:
            raise ServiceValidationError(f"unknown string: {string_id}")

        valid_from = call.data.get("valid_from")
        if valid_from is None:
            valid_from_ts = int(dt_util.utcnow().timestamp())
        else:
            valid_from_ts = int(
                dt_util.start_of_local_day(valid_from).timestamp()
            )

        segment = GeometrySegment(
            valid_from_ts_utc=valid_from_ts,
            azimuth_deg=call.data[CONF_AZIMUTH],
            tilt_deg=call.data[CONF_TILT],
            kwp=call.data[CONF_KWP],
            temp_coeff=call.data.get(CONF_TEMP_COEFF, DEFAULT_TEMP_COEFF),
            note=call.data.get(CONF_NOTE),
        )
        await hass.async_add_executor_job(
            coordinator.store.add_geometry, string_id, segment
        )
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_RECALCULATE, _recalculate, schema=ENTRY_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_LEARNING, _reset_learning, schema=ENTRY_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PURGE, _purge, schema=ENTRY_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_GEOMETRY, _add_geometry, schema=ADD_GEOMETRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        _backfill,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
