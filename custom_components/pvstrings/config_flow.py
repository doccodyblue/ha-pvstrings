"""Config, options and subentry flows.

Plant setup is one screen.  Strings and curtailment groups are subentries, so
adding a string is additive -- it never rewrites the plant configuration and it
gives every string its own device.

The one genuinely unusual bit is what happens when a mounting angle changes:
that is not an edit, it is a new validity period.  Overwriting the value would
silently re-evaluate months of history against a geometry that was not
installed at the time.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .const import (
    AGGREGATE_HELPER_PLATFORMS,
    CONF_AC_POWER_ENTITY,
    CONF_ALBEDO,
    CONF_AZIMUTH,
    CONF_BATTERY_COUPLED,
    CONF_CHARGE_EFFICIENCY,
    CONF_CUSTOM_CURVE,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_FORECAST_CLIPPING,
    CONF_INVERTER_MODEL,
    CONF_MPPT_EFFICIENCY,
    CONF_OUTPUT_PATH,
    INVERTER_MODEL_CUSTOM,
    INVERTER_MODEL_NONE,
    INVERTER_MODELS,
    OUTPUT_PATH_DIRECT,
    OUTPUT_PATH_NONE,
    OUTPUT_PATHS,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_COMMISSIONING,
    CONF_ECONOMICS_MODE,
    CONF_ELEVATION,
    CONF_ENERGY_ENTITY,
    CONF_FEED_IN,
    CONF_FIXED_LIMIT,
    CONF_FORECAST_MODEL,
    CONF_FORECAST_SOURCE,
    CONF_GEOMETRY_MODE,
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
    CONF_CHARGER_STATE,
    CONF_MAX_POWER,
    CONF_MOUNT_TYPE,
    CONF_NAME,
    CONF_NOTE,
    CONF_POWER_ENTITY,
    CONF_PRESSURE_ENTITY,
    CONF_PRICE,
    CONF_RAIN_ENTITY,
    CONF_RETENTION_DAYS,
    CONF_SOC_LIMIT,
    CONF_STRING_EFFICIENCY,
    CONF_SYSTEM_EFFICIENCY,
    CONF_TEMP_COEFF,
    CONF_TEMPERATURE_ENTITY,
    CONF_TILT,
    CONF_TRANSPOSITION,
    CONF_VALID_FROM,
    CONF_WATCHDOG,
    CONF_WEATHER_ENTITY,
    CONF_WIND_ENTITY,
    DOMAIN,
    GEOMETRY_MODE_CORRECTION,
    GEOMETRY_MODE_DATE,
    GEOMETRY_MODE_NOW,
    NO_GROUP,
    SUBENTRY_GROUP,
    SUBENTRY_STRING,
)
from .core.config import (
    DEFAULT_ALBEDO,
    DEFAULT_SYSTEM_EFFICIENCY,
    DEFAULT_TEMP_COEFF,
    DEFAULT_WATCHDOG_SECONDS,
    ECONOMICS_MODES,
    MOUNT_TYPES,
    TRANSPOSITION_MODELS,
    selector_step,
)
from .core.weather import OPEN_METEO_MODELS, SOURCE_HA_WEATHER, SOURCE_OPEN_METEO

_LOGGER = logging.getLogger(__name__)

# Home Assistant refuses ``async_update_reload_and_abort`` on an entry that has
# update listeners -- and this integration registers one so the options flow
# takes effect.  Subentry edits therefore use ``async_update_and_abort`` and let
# that listener do the reloading.  Creating a subentry is different: no listener
# fires for it, which is why ``_created`` schedules the reload itself.


def _select(options: tuple[str, ...] | list[str], key: str) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=option, label=option) for option in options
            ],
            translation_key=key,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _number(
    minimum: float,
    maximum: float,
    step: float = 1.0,
    unit: str | None = None,
    mode: NumberSelectorMode = NumberSelectorMode.BOX,
) -> NumberSelector:
    config = NumberSelectorConfig(
        min=minimum, max=maximum, step=selector_step(step), mode=mode
    )
    if unit is not None:
        # The selector schema types this as a plain string; passing ``None``
        # for "no unit" fails validation instead of being ignored.
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _optional(key: str, current: Any = None) -> vol.Marker:
    """An optional field that stays *absent* when the user leaves it blank.

    Entity and date selectors reject an empty string, and ``default=""`` makes
    voluptuous supply exactly that on every submit -- the form then fails
    validation before the user has done anything wrong.  A suggested value
    pre-fills the field without becoming a fallback.
    """
    if current in (None, ""):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": current})


def _prefilled(key: str, current: Any = None) -> vol.Marker:
    """Required counterpart of :func:`_optional`."""
    if current in (None, ""):
        return vol.Required(key)
    return vol.Required(key, description={"suggested_value": current})


def _power_entity_selector() -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain=["sensor"], device_class=["power"])
    )


# --------------------------------------------------------------------------- #
# helper-entity warning
# --------------------------------------------------------------------------- #


def _helper_platform(hass: Any, entity_id: str) -> str | None:
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return None
    return entry.platform if entry.platform in AGGREGATE_HELPER_PLATFORMS else None


def _sources_of_helper(hass: Any, entity_id: str) -> set[str]:
    """Best-effort lookup of which entities an aggregate helper reads."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.config_entry_id is None:
        return set()
    helper_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if helper_entry is None:
        return set()
    config = {**helper_entry.data, **helper_entry.options}
    sources: set[str] = set()
    for key in ("entity_ids", "entity_id", "source", "source_entity_id", "members"):
        value = config.get(key)
        if isinstance(value, str):
            sources.add(value)
        elif isinstance(value, (list, tuple)):
            sources.update(str(item) for item in value)
    return sources


def check_power_entity(
    hass: Any, entry: ConfigEntry | None, entity_id: str, own_subentry_id: str | None
) -> str | None:
    """Return a warning key when a chosen power entity is a bad idea.

    Aggregate helpers are the trap here.  Pointing two strings at a min/max or
    template helper double-counts one physical channel and drops another; in
    the plant total it barely shows, and per string -- which is the entire point
    of this integration -- it is useless.
    """
    platform = _helper_platform(hass, entity_id)
    if platform is None:
        return None

    if entry is not None:
        already_used: set[str] = set()
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_STRING:
                continue
            if subentry_id == own_subentry_id:
                continue
            configured = subentry.data.get(CONF_POWER_ENTITY)
            if configured:
                already_used.add(configured)
        if _sources_of_helper(hass, entity_id) & already_used:
            return "helper_overlaps"
    return "helper_entity"


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #


def plant_schema(
    hass: Any,
    defaults: dict[str, Any] | None = None,
    include_name: bool = True,
) -> vol.Schema:
    values = defaults or {}
    name_field: dict[Any, Any] = (
        {
            vol.Required(
                CONF_NAME, default=values.get(CONF_NAME, "PV Strings")
            ): TextSelector()
        }
        if include_name
        else {}
    )
    return vol.Schema(
        {
            **name_field,
            vol.Required(
                CONF_LATITUDE,
                default=values.get(CONF_LATITUDE, hass.config.latitude),
            ): _number(-90, 90, 0.00001, "°"),
            vol.Required(
                CONF_LONGITUDE,
                default=values.get(CONF_LONGITUDE, hass.config.longitude),
            ): _number(-180, 180, 0.00001, "°"),
            vol.Required(
                CONF_ELEVATION,
                default=values.get(CONF_ELEVATION, hass.config.elevation or 0),
            ): _number(-500, 5000, 1, "m"),
            vol.Required(
                CONF_FORECAST_SOURCE,
                default=values.get(CONF_FORECAST_SOURCE, SOURCE_OPEN_METEO),
            ): _select((SOURCE_OPEN_METEO, SOURCE_HA_WEATHER), "forecast_source"),
            vol.Optional(
                CONF_FORECAST_MODEL,
                default=values.get(CONF_FORECAST_MODEL, "best_match"),
            ): _select(OPEN_METEO_MODELS, "forecast_model"),
            _optional(CONF_WEATHER_ENTITY, values.get(CONF_WEATHER_ENTITY)): EntitySelector(EntitySelectorConfig(domain="weather")),
        }
    )


def economics_schema(
    defaults: dict[str, Any] | None = None, currency: str = "EUR"
) -> vol.Schema:
    values = defaults or {}
    per_kwh = f"{currency}/kWh"
    return vol.Schema(
        {
            vol.Required(
                CONF_ECONOMICS_MODE,
                default=values.get(CONF_ECONOMICS_MODE, "self_consumption"),
            ): _select(ECONOMICS_MODES, "economics_mode"),
            vol.Required(
                CONF_PRICE, default=values.get(CONF_PRICE, 0.30)
            ): _number(0, 5, 0.001, per_kwh),
            vol.Required(
                CONF_FEED_IN, default=values.get(CONF_FEED_IN, 0.08)
            ): _number(0, 5, 0.001, per_kwh),
            vol.Optional(
                CONF_INVESTMENT, default=values.get(CONF_INVESTMENT, 0)
            ): _number(0, 1_000_000, 1, currency),
            _optional(CONF_COMMISSIONING, values.get(CONF_COMMISSIONING)): DateSelector(),
        }
    )


def advanced_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ALBEDO, default=values.get(CONF_ALBEDO, DEFAULT_ALBEDO)
            ): _number(0, 1, 0.01),
            vol.Required(
                CONF_SYSTEM_EFFICIENCY,
                default=values.get(CONF_SYSTEM_EFFICIENCY, DEFAULT_SYSTEM_EFFICIENCY),
            ): _number(0.5, 1.0, 0.01),
            vol.Required(
                CONF_TRANSPOSITION,
                default=values.get(CONF_TRANSPOSITION, "perez-driesse"),
            ): _select(TRANSPOSITION_MODELS, "transposition_model"),
            vol.Required(
                CONF_WATCHDOG,
                default=values.get(CONF_WATCHDOG, DEFAULT_WATCHDOG_SECONDS),
            ): _number(5, 300, 5, "s"),
            vol.Required(
                CONF_LEARNING_ENABLED,
                default=values.get(CONF_LEARNING_ENABLED, True),
            ): BooleanSelector(),
            vol.Required(
                CONF_RETENTION_DAYS, default=values.get(CONF_RETENTION_DAYS, 90)
            ): _number(14, 3650, 1, "d"),
        }
    )


def entities_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    values = defaults or {}

    def sensor(device_class: str | None = None) -> EntitySelector:
        config = EntitySelectorConfig(domain=["sensor"])
        if device_class:
            config["device_class"] = [device_class]
        return EntitySelector(config)

    return vol.Schema(
        {
            _optional(CONF_GRID_POWER, values.get(CONF_GRID_POWER)): sensor("power"),
            _optional(CONF_HOUSE_LOAD, values.get(CONF_HOUSE_LOAD)): sensor("power"),
            _optional(CONF_BATTERY_SOC, values.get(CONF_BATTERY_SOC)): sensor("battery"),
            _optional(CONF_BATTERY_POWER, values.get(CONF_BATTERY_POWER)): sensor("power"),
            _optional(CONF_TEMPERATURE_ENTITY, values.get(CONF_TEMPERATURE_ENTITY)): sensor("temperature"),
            _optional(CONF_HUMIDITY_ENTITY, values.get(CONF_HUMIDITY_ENTITY)): sensor("humidity"),
            _optional(CONF_WIND_ENTITY, values.get(CONF_WIND_ENTITY)): sensor("wind_speed"),
            _optional(CONF_RAIN_ENTITY, values.get(CONF_RAIN_ENTITY)): sensor(),
            _optional(CONF_PRESSURE_ENTITY, values.get(CONF_PRESSURE_ENTITY)): sensor("atmospheric_pressure"),
            _optional(CONF_GHI_ENTITY, values.get(CONF_GHI_ENTITY)): sensor("irradiance"),
            _optional(CONF_ILLUMINANCE_ENTITY, values.get(CONF_ILLUMINANCE_ENTITY)): sensor("illuminance"),
        }
    )


def string_schema(
    hass: Any, entry: ConfigEntry | None, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    values = defaults or {}
    group_options = [SelectOptionDict(value=NO_GROUP, label="No curtailment group")]
    if entry is not None:
        group_options.extend(
            SelectOptionDict(value=subentry_id, label=subentry.title)
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_GROUP
        )

    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=values.get(CONF_NAME, "")): TextSelector(),
            _prefilled(CONF_POWER_ENTITY, values.get(CONF_POWER_ENTITY)): _power_entity_selector(),
            vol.Required(
                CONF_AZIMUTH, default=values.get(CONF_AZIMUTH, 180)
            ): _number(0, 360, 1, "°"),
            vol.Required(CONF_TILT, default=values.get(CONF_TILT, 30)): _number(
                0, 90, 1, "°"
            ),
            vol.Required(CONF_KWP, default=values.get(CONF_KWP, 1.0)): _number(
                0.01, 100, 0.01, "kWp"
            ),
            vol.Optional(
                CONF_GROUP_ID, default=values.get(CONF_GROUP_ID, NO_GROUP)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=group_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(
                CONF_TEMP_COEFF, default=values.get(CONF_TEMP_COEFF, DEFAULT_TEMP_COEFF)
            ): _number(-0.01, 0.0, 0.0001, "1/K"),
            vol.Optional(
                CONF_MOUNT_TYPE, default=values.get(CONF_MOUNT_TYPE, "insulated_back")
            ): _select(tuple(MOUNT_TYPES), "mount_type"),
            _optional(CONF_ENERGY_ENTITY, values.get(CONF_ENERGY_ENTITY)): EntitySelector(
                EntitySelectorConfig(domain=["sensor"], device_class=["energy"])
            ),
            vol.Optional(
                CONF_STRING_EFFICIENCY, default=values.get(CONF_STRING_EFFICIENCY, 0)
            ): _number(0, 1.0, 0.01),
            vol.Optional(
                CONF_MAX_POWER, default=values.get(CONF_MAX_POWER, 0)
            ): _number(0, 20_000, 1, "W"),
            _optional(
                CONF_CHARGER_STATE, values.get(CONF_CHARGER_STATE)
            ): EntitySelector(EntitySelectorConfig(domain=["sensor"])),
        }
    )


def group_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=values.get(CONF_NAME, "")): TextSelector(),
            _optional(CONF_LIMIT_ENTITY, values.get(CONF_LIMIT_ENTITY)): EntitySelector(EntitySelectorConfig(domain=["number", "sensor", "input_number"])),
            vol.Optional(
                CONF_INVERTER_MAX_AC, default=values.get(CONF_INVERTER_MAX_AC, 0)
            ): _number(0, 100_000, 1, "W"),
            _optional(CONF_LIMIT_ABS_ENTITY, values.get(CONF_LIMIT_ABS_ENTITY)): EntitySelector(EntitySelectorConfig(domain=["number", "sensor", "input_number"])),
            vol.Optional(
                CONF_FIXED_LIMIT, default=values.get(CONF_FIXED_LIMIT, 0)
            ): _number(0, 100_000, 1, "W"),
            vol.Optional(
                CONF_BATTERY_COUPLED, default=values.get(CONF_BATTERY_COUPLED, False)
            ): BooleanSelector(),
            vol.Optional(
                CONF_SOC_LIMIT, default=values.get(CONF_SOC_LIMIT, 100)
            ): _number(0, 100, 1, "%"),
            vol.Required(
                CONF_OUTPUT_PATH,
                default=values.get(CONF_OUTPUT_PATH, OUTPUT_PATH_NONE),
            ): _select(list(OUTPUT_PATHS), "output_path"),
        }
    )


def conversion_schema(path: str, defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step two of the group flow, shaped by the chosen path.

    Two schemas instead of conditional fields -- HA forms are static, and a
    storage field on a direct plant is exactly the double-MPPT mistake the
    spec wants structurally prevented.
    """
    values = defaults or {}
    if path == OUTPUT_PATH_DIRECT:
        return vol.Schema(
            {
                vol.Required(
                    CONF_INVERTER_MODEL,
                    default=values.get(CONF_INVERTER_MODEL, INVERTER_MODEL_NONE),
                ): _select(
                    [INVERTER_MODEL_NONE]
                    + list(INVERTER_MODELS)
                    + [INVERTER_MODEL_CUSTOM],
                    "inverter_model",
                ),
                vol.Optional(
                    CONF_CUSTOM_CURVE,
                    default=_curve_to_text(values.get(CONF_CUSTOM_CURVE)),
                ): TextSelector(),
                vol.Optional(
                    CONF_FORECAST_CLIPPING,
                    default=values.get(CONF_FORECAST_CLIPPING, False),
                ): BooleanSelector(),
                _optional(
                    CONF_AC_POWER_ENTITY, values.get(CONF_AC_POWER_ENTITY)
                ): _power_entity_selector(),
            }
        )
    return vol.Schema(
        {
            vol.Optional(
                CONF_MPPT_EFFICIENCY,
                default=values.get(CONF_MPPT_EFFICIENCY, 0),
            ): _number(0, 1, 0.001),
            vol.Required(
                CONF_CHARGE_EFFICIENCY,
                default=values.get(CONF_CHARGE_EFFICIENCY, 0.96),
            ): _number(0.5, 1, 0.001),
            vol.Required(
                CONF_DISCHARGE_EFFICIENCY,
                default=values.get(CONF_DISCHARGE_EFFICIENCY, 0.96),
            ): _number(0.5, 1, 0.001),
        }
    )


#: Conversion keys per path; switching paths drops the other side's keys so
#: a storage-era mppt_efficiency can never leak into a direct plant.
_DIRECT_KEYS = (
    CONF_INVERTER_MODEL,
    CONF_CUSTOM_CURVE,
    CONF_FORECAST_CLIPPING,
    CONF_AC_POWER_ENTITY,
)
_STORAGE_KEYS = (
    CONF_MPPT_EFFICIENCY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
)


def _curve_to_text(curve: Any) -> str:
    if not curve:
        return ""
    return ", ".join(f"{point[0]:g}:{point[1]:g}" for point in curve)


def _parse_curve(text: str) -> list[list[float]] | None:
    """"5:0.90, 10:0.935, ..." -> [[load_pct, eff], ...] or None if invalid."""
    points: list[list[float]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        load_text, sep, eff_text = chunk.partition(":")
        if not sep:
            return None
        try:
            points.append([float(load_text), float(eff_text)])
        except ValueError:
            return None
    if len(points) < 2:
        return None
    last_load = -1.0
    for load, efficiency in points:
        if not (0.0 <= load <= 150.0 and 0.5 < efficiency <= 1.0):
            return None
        if load <= last_load:
            return None
        last_load = load
    return points


def _efficiency_out_of_range(data: dict[str, Any]) -> bool:
    """A per-string efficiency override below 0.5 is a percent-vs-fraction mixup.

    The selector cannot express "0 to inherit, otherwise at least 0.5", so this
    has to be a flow check.  Without it a value at or below 0.1 sails through
    the form and ``StringConfig`` rejects it during reload -- a hard error with
    no hint which field caused it.  The core guard stays at 0.1 on purpose:
    tightening it to 0.5 would refuse to *load* configs that were stored before
    this check existed, and a form error is recoverable where a setup error is
    not.
    """
    value = data.get(CONF_STRING_EFFICIENCY)
    return value is not None and float(value) < 0.5


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional values so ``.get()`` defaults stay meaningful."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value in ("", None):
            continue
        if key in (CONF_STRING_EFFICIENCY, CONF_MAX_POWER) and not value:
            continue
        if key in (CONF_INVERTER_MAX_AC, CONF_FIXED_LIMIT) and not value:
            continue
        # 0 means "no external MPPT stage", not a 0 % efficiency.
        if key == CONF_MPPT_EFFICIENCY and not value:
            continue
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
# main flow
# --------------------------------------------------------------------------- #


class PvStringsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Plant-level setup."""

    VERSION = 1
    #: Minor 2: conversion-layer group fields (additive, upgrade.md).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _currency(self) -> str:
        return self.hass.config.currency or "EUR"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input.get(CONF_FORECAST_SOURCE)
            if source == SOURCE_HA_WEATHER and not user_input.get(CONF_WEATHER_ENTITY):
                errors[CONF_WEATHER_ENTITY] = "weather_entity_required"
            if not errors:
                self._data = _clean(user_input)
                return await self.async_step_economics()

        return self.async_show_form(
            step_id="user", data_schema=plant_schema(self.hass), errors=errors
        )

    async def async_step_economics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return self.async_create_entry(
                title=self._data.get(CONF_NAME, "PV Strings"), data=self._data
            )
        return self.async_show_form(
            step_id="economics",
            data_schema=economics_schema(currency=self._currency()),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PvStringsOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            SUBENTRY_STRING: StringSubentryFlow,
            SUBENTRY_GROUP: GroupSubentryFlow,
        }


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #


class PvStringsOptionsFlow(OptionsFlow):
    """Prices, model knobs and the optional plant-level entities."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _currency(self) -> str:
        return self.hass.config.currency or "EUR"

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["economics", "entities", "advanced", "forecast"],
        )

    async def async_step_economics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self.async_show_form(
            step_id="economics",
            data_schema=economics_schema(self._current(), self._currency()),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input, allow_clear=True)
        return self.async_show_form(
            step_id="entities", data_schema=entities_schema(self._current())
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self.async_show_form(
            step_id="advanced", data_schema=advanced_schema(self._current())
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input, allow_clear=True)
        # No name field here: the plant name is the config entry title, which
        # options cannot change -- offering it would be a silent no-op.
        return self.async_show_form(
            step_id="forecast",
            data_schema=plant_schema(self.hass, self._current(), include_name=False),
        )

    def _save(
        self, user_input: dict[str, Any], allow_clear: bool = False
    ) -> ConfigFlowResult:
        options = {**self.config_entry.options}
        if allow_clear:
            # An emptied entity selector must actually clear the setting, so
            # here we take the input verbatim instead of dropping blanks.
            options.update(user_input)
            options = {key: value for key, value in options.items() if value != ""}
            for key in user_input:
                if user_input[key] == "":
                    options.pop(key, None)
        else:
            options.update(_clean(user_input))
        return self.async_create_entry(title="", data=options)


# --------------------------------------------------------------------------- #
# subentries
# --------------------------------------------------------------------------- #


class _ReloadingSubentryFlow(ConfigSubentryFlow):
    """Shared behaviour: adding a subentry must rebuild the plant."""

    def _created(self, title: str, data: dict[str, Any]) -> SubentryFlowResult:
        """Finish a subentry.

        No explicit reload here.  ``async_add_subentry`` goes through
        ``_async_update_entry``, which notifies the update listeners, and this
        integration registers one -- so the reload already happens, and after
        the subentry exists rather than before it.  Scheduling another one here
        rebuilt the plant twice per added string: two teardowns, two sets of
        entities going unavailable, and the collector losing its buffers twice.
        """
        return self.async_create_entry(title=title, data=data)


class GroupSubentryFlow(_ReloadingSubentryFlow):
    """A set of strings that can only be curtailed together."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._reconfigure = False
        self._existing: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._form(user_input, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._form(user_input, reconfigure=True)

    async def _form(
        self, user_input: dict[str, Any] | None, reconfigure: bool
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        self._reconfigure = reconfigure
        existing: dict[str, Any] = {}
        if reconfigure:
            subentry = self._get_reconfigure_subentry()
            # The name lives in the subentry *title*, not in its data, so it has
            # to be seeded back in or the edit form comes up blank and submitting
            # it would wipe the name.
            existing = {**subentry.data, CONF_NAME: subentry.title}
            self._existing = dict(subentry.data)

        if user_input is not None:
            data = _clean(user_input)
            if data.get(CONF_LIMIT_ENTITY) and not data.get(CONF_LIMIT_ABS_ENTITY):
                if not data.get(CONF_INVERTER_MAX_AC):
                    # A relative limit in percent cannot be turned into watts
                    # without the nameplate, and a limit we cannot express in
                    # watts is useless for the binding test.
                    errors[CONF_INVERTER_MAX_AC] = "nameplate_required"
            if not errors:
                self._pending = data
                path = data.get(CONF_OUTPUT_PATH, OUTPUT_PATH_NONE)
                if path != OUTPUT_PATH_NONE:
                    return await self.async_step_conversion()
                # Back to "none": stale conversion keys must not survive, or a
                # later path switch would resurrect them.
                for key in _DIRECT_KEYS + _STORAGE_KEYS:
                    self._pending.pop(key, None)
                return self._finish()
            existing = user_input

        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=group_schema(existing),
            errors=errors,
        )

    async def async_step_conversion(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        path = self._pending.get(CONF_OUTPUT_PATH, OUTPUT_PATH_NONE)
        direct = path == OUTPUT_PATH_DIRECT
        seed = {**self._existing}

        if user_input is not None:
            data = _clean(user_input)
            if direct:
                # The opposite path's keys go, structurally preventing the
                # double-MPPT mistake (spec 3.2.1) instead of validating it.
                for key in _STORAGE_KEYS:
                    self._pending.pop(key, None)
                model = data.get(CONF_INVERTER_MODEL, INVERTER_MODEL_NONE)
                curve_text = str(user_input.get(CONF_CUSTOM_CURVE) or "")
                data.pop(CONF_CUSTOM_CURVE, None)
                if model == INVERTER_MODEL_CUSTOM:
                    parsed = _parse_curve(curve_text)
                    if parsed is None:
                        errors[CONF_CUSTOM_CURVE] = "curve_invalid"
                    else:
                        data[CONF_CUSTOM_CURVE] = parsed
                if model != INVERTER_MODEL_NONE or data.get(
                    CONF_FORECAST_CLIPPING
                ):
                    # Load fraction and clip level both divide by the
                    # nameplate; without it neither is computable.
                    if not self._pending.get(CONF_INVERTER_MAX_AC):
                        errors[CONF_INVERTER_MODEL] = (
                            "nameplate_required_conversion"
                        )
                if model == INVERTER_MODEL_NONE:
                    data.pop(CONF_INVERTER_MODEL, None)
            else:
                for key in _DIRECT_KEYS:
                    self._pending.pop(key, None)
            if not errors:
                self._pending.update(data)
                return self._finish()
            seed = {**self._existing, **user_input}

        return self.async_show_form(
            step_id="conversion",
            data_schema=conversion_schema(path, seed),
            errors=errors,
        )

    def _finish(self) -> SubentryFlowResult:
        data = dict(self._pending)
        title = data.pop(CONF_NAME)
        if self._reconfigure:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=data,
            )
        return self._created(title, data)


class StringSubentryFlow(_ReloadingSubentryFlow):
    """One measured PV string."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    # -- creation ------------------------------------------------------- #

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()

        if user_input is not None:
            data = _clean(user_input)
            warning = check_power_entity(
                self.hass, entry, data[CONF_POWER_ENTITY], None
            )
            if warning == "helper_overlaps":
                errors[CONF_POWER_ENTITY] = warning
            if _efficiency_out_of_range(data):
                errors[CONF_STRING_EFFICIENCY] = "efficiency_out_of_range"
            if not errors:
                title = data.pop(CONF_NAME)
                if warning:
                    _LOGGER.warning(
                        "pvstrings: string %s points at helper entity %s -- prefer "
                        "the physical channel",
                        title,
                        data[CONF_POWER_ENTITY],
                    )
                return self._created(title, data)

        return self.async_show_form(
            step_id="user",
            data_schema=string_schema(self.hass, entry, user_input),
            errors=errors,
        )

    # -- reconfiguration ------------------------------------------------ #

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        # Same as for groups: the name is the subentry title, not part of data.
        current = {**subentry.data, CONF_NAME: subentry.title}

        if user_input is not None:
            data = _clean(user_input)
            warning = check_power_entity(
                self.hass, entry, data[CONF_POWER_ENTITY], subentry.subentry_id
            )
            if warning == "helper_overlaps":
                errors[CONF_POWER_ENTITY] = warning
            if _efficiency_out_of_range(data):
                errors[CONF_STRING_EFFICIENCY] = "efficiency_out_of_range"
            if not errors:
                self._pending = data
                if self._geometry_changed(current, data):
                    return await self.async_step_geometry()
                title = data.pop(CONF_NAME)
                return self.async_update_and_abort(
                    entry, subentry, title=title, data=data
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=string_schema(self.hass, entry, user_input or current),
            errors=errors,
            description_placeholders={"history": await self._history_text()},
        )

    @staticmethod
    def _geometry_changed(current: dict[str, Any], new: dict[str, Any]) -> bool:
        """Does this edit need a new validity period?

        The temperature coefficient belongs here even though it is not a
        mounting angle: it is stored *in* the geometry row, so changing it
        anywhere else would leave the form and the database disagreeing while
        the forecast quietly keeps using the old value.
        """
        return any(
            float(current.get(key, -999)) != float(new.get(key, -999))
            for key in (CONF_AZIMUTH, CONF_TILT, CONF_KWP, CONF_TEMP_COEFF)
        )

    async def async_step_geometry(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask *when* the new geometry started.

        Adjustable mounts are normal on small installations, and a wrong tilt is
        not a constant error -- it travels with the sun, which makes the
        learning layer book it as a weather or shading effect.  So a change is a
        new validity period, not an edit; the past keeps being evaluated against
        the geometry that was actually installed.
        """
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            from .core.config import GeometrySegment

            mode = user_input[CONF_GEOMETRY_MODE]
            note = user_input.get(CONF_NOTE)
            if mode == GEOMETRY_MODE_DATE and user_input.get(CONF_VALID_FROM):
                stamp = dt_util.start_of_local_day(
                    dt_util.parse_date(str(user_input[CONF_VALID_FROM]))
                )
                valid_from = int(stamp.timestamp())
            else:
                valid_from = int(dt_util.utcnow().timestamp())

            segment = GeometrySegment(
                valid_from_ts_utc=0 if mode == GEOMETRY_MODE_CORRECTION else valid_from,
                azimuth_deg=float(self._pending[CONF_AZIMUTH]),
                tilt_deg=float(self._pending[CONF_TILT]),
                kwp=float(self._pending[CONF_KWP]),
                temp_coeff=float(
                    self._pending.get(CONF_TEMP_COEFF, DEFAULT_TEMP_COEFF)
                ),
                note=note,
            )

            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is not None:
                store = coordinator.store
                if mode == GEOMETRY_MODE_CORRECTION:
                    await self.hass.async_add_executor_job(
                        store.replace_latest_geometry, subentry.subentry_id, segment
                    )
                else:
                    await self.hass.async_add_executor_job(
                        store.add_geometry, subentry.subentry_id, segment
                    )

            data = dict(self._pending)
            title = data.pop(CONF_NAME)
            return self.async_update_and_abort(entry, subentry, title=title, data=data)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GEOMETRY_MODE, default=GEOMETRY_MODE_NOW
                ): _select(
                    (GEOMETRY_MODE_NOW, GEOMETRY_MODE_DATE, GEOMETRY_MODE_CORRECTION),
                    "geometry_mode",
                ),
                vol.Optional(CONF_VALID_FROM): DateSelector(),
                vol.Optional(CONF_NOTE, default=""): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="geometry",
            data_schema=schema,
            description_placeholders={"history": await self._history_text()},
        )

    async def _history_text(self) -> str:
        """Render the recorded validity periods for the form description."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            return "-"
        history = await self.hass.async_add_executor_job(
            coordinator.store.geometry_history, subentry.subentry_id
        )
        if not history:
            return "-"
        lines = []
        for segment in history:
            when = (
                "from the start"
                if segment.valid_from_ts_utc == 0
                else dt_util.as_local(
                    datetime.fromtimestamp(segment.valid_from_ts_utc, tz=dt_util.UTC)
                ).strftime("%Y-%m-%d %H:%M")
            )
            note = f" ({segment.note})" if segment.note else ""
            lines.append(
                f"{when}: {segment.azimuth_deg:.0f}° / {segment.tilt_deg:.0f}° / "
                f"{segment.kwp:.2f} kWp{note}"
            )
        return "\n".join(lines)
