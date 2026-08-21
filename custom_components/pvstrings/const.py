"""Constants and config-entry keys for the pvstrings integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "pvstrings"

#: Subentry types.  A string belongs to at most one curtailment group.
SUBENTRY_STRING: Final = "string"
SUBENTRY_GROUP: Final = "curtailment_group"

# -- config entry data ------------------------------------------------------ #

CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"
CONF_ELEVATION: Final = "elevation"
CONF_ALBEDO: Final = "albedo"
CONF_SYSTEM_EFFICIENCY: Final = "system_efficiency"
CONF_TRANSPOSITION: Final = "transposition_model"
CONF_WATCHDOG: Final = "watchdog_seconds"
CONF_FORECAST_SOURCE: Final = "forecast_source"
CONF_FORECAST_MODEL: Final = "forecast_model"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_LEARNING_ENABLED: Final = "learning_enabled"
CONF_RETENTION_DAYS: Final = "retention_days"

# -- economics -------------------------------------------------------------- #

CONF_ECONOMICS_MODE: Final = "economics_mode"
CONF_PRICE: Final = "price_per_kwh"
CONF_FEED_IN: Final = "feed_in_tariff"
CONF_INVESTMENT: Final = "investment_eur"
CONF_COMMISSIONING: Final = "commissioning_date"

# -- plant state entities --------------------------------------------------- #

CONF_BATTERY_SOC: Final = "battery_soc_entity"
CONF_BATTERY_POWER: Final = "battery_power_entity"
CONF_GRID_POWER: Final = "grid_power_entity"
CONF_HOUSE_LOAD: Final = "house_load_entity"

# -- local weather sensors -------------------------------------------------- #

CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_HUMIDITY_ENTITY: Final = "humidity_entity"
CONF_WIND_ENTITY: Final = "wind_speed_entity"
CONF_RAIN_ENTITY: Final = "rain_entity"
CONF_PRESSURE_ENTITY: Final = "pressure_entity"
CONF_GHI_ENTITY: Final = "ghi_entity"
CONF_ILLUMINANCE_ENTITY: Final = "illuminance_entity"

# -- string subentry -------------------------------------------------------- #

CONF_NAME: Final = "name"
CONF_POWER_ENTITY: Final = "power_entity"
CONF_ENERGY_ENTITY: Final = "energy_entity"
CONF_AZIMUTH: Final = "azimuth"
CONF_TILT: Final = "tilt"
CONF_KWP: Final = "kwp"
CONF_TEMP_COEFF: Final = "temp_coeff"
CONF_MOUNT_TYPE: Final = "mount_type"
CONF_GROUP_ID: Final = "curtailment_group_id"
CONF_STRING_EFFICIENCY: Final = "string_efficiency"
CONF_MAX_POWER: Final = "max_power_w"
CONF_CHARGER_STATE: Final = "charger_state_entity"

# -- geometry change flow --------------------------------------------------- #

CONF_GEOMETRY_MODE: Final = "geometry_mode"
CONF_VALID_FROM: Final = "valid_from"
CONF_NOTE: Final = "note"

GEOMETRY_MODE_NOW: Final = "now"
GEOMETRY_MODE_DATE: Final = "from_date"
GEOMETRY_MODE_CORRECTION: Final = "correction"

# -- curtailment group subentry --------------------------------------------- #

CONF_LIMIT_ENTITY: Final = "limit_entity"
CONF_LIMIT_ABS_ENTITY: Final = "limit_abs_entity"
CONF_INVERTER_MAX_AC: Final = "inverter_max_ac_w"
CONF_FIXED_LIMIT: Final = "fixed_limit_w"
CONF_BATTERY_COUPLED: Final = "battery_coupled"
CONF_SOC_LIMIT: Final = "soc_limit"

# -- conversion layer (upgrade.md) ------------------------------------------ #

CONF_OUTPUT_PATH: Final = "output_path"
CONF_INVERTER_MODEL: Final = "inverter_model"
CONF_CUSTOM_CURVE: Final = "custom_curve"
CONF_FORECAST_CLIPPING: Final = "forecast_clipping"
CONF_MPPT_EFFICIENCY: Final = "mppt_efficiency"
CONF_CHARGE_EFFICIENCY: Final = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"
CONF_AC_POWER_ENTITY: Final = "ac_power_entity"

OUTPUT_PATH_NONE: Final = "none"
OUTPUT_PATH_DIRECT: Final = "direct"
OUTPUT_PATH_STORAGE: Final = "storage"
OUTPUT_PATHS: Final = (OUTPUT_PATH_NONE, OUTPUT_PATH_DIRECT, OUTPUT_PATH_STORAGE)

#: Ids of shipped datasheet curves in inverter_models/.  A constant, not a
#: directory scan: forms must not do disk I/O, and a test keeps this list and
#: the files in sync.
INVERTER_MODELS: Final = ("hoymiles_hms1600_4t",)
INVERTER_MODEL_CUSTOM: Final = "custom"
#: Neutral pass-through: AC equals DC until a curve is chosen (upgrade.md §6).
INVERTER_MODEL_NONE: Final = "none"

NO_GROUP: Final = "__none__"

# -- timings ---------------------------------------------------------------- #

#: How often the coordinator recomputes the forecast from stored weather data.
FORECAST_INTERVAL: Final = timedelta(minutes=15)

#: How often we pull new weather data.  Open-Meteo updates hourly at best;
#: asking more often just burns their capacity for nothing.
WEATHER_INTERVAL: Final = timedelta(minutes=30)

#: Forecast horizon in hours.  Three local days, so a day-after-tomorrow
#: figure exists for side-by-side comparison with other forecast services.
FORECAST_HOURS: Final = 72

#: Rolling scoring windows.
SCORE_WINDOWS: Final = (7, 30)

DEFAULT_DB_DIR: Final = "pvstrings"

# -- services --------------------------------------------------------------- #

SERVICE_RECALCULATE: Final = "recalculate"
SERVICE_ADD_GEOMETRY: Final = "add_geometry"
SERVICE_RESET_LEARNING: Final = "reset_learning"
SERVICE_PURGE: Final = "purge"
SERVICE_BACKFILL: Final = "backfill_shading"

ATTR_STRING_ID: Final = "string_id"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

# -- helper platforms we warn about ----------------------------------------- #

#: Pointing a string at an aggregate helper silently double-counts one physical
#: channel and drops another.  It looks harmless in the plant total and is
#: useless per string -- which is the entire point of this integration.
AGGREGATE_HELPER_PLATFORMS: Final = frozenset(
    {"min_max", "group", "template", "derivative", "integration", "utility_meter"}
)
