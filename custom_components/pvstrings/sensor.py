"""Sensor platform.

Plant-level sensors live on a service device; every string gets its own device
under it, so a five-string plant reads as five devices rather than twenty-five
loose entities.

Hourly detail is exposed as a ``forecast`` attribute -- a list of
``{datetime, potential_kwh}`` -- which is what ApexCharts and friends consume.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import PvStringsConfigEntry, plant_device_info, string_device_info
from .const import SUBENTRY_STRING
from .coordinator import PvStringsCoordinator, PvStringsData
from .core.forecast import DAY_AHEAD_ISSUE_HOUR_LOCAL, floor_hour
from .core import units

#: Fallback when Home Assistant has no currency configured.
DEFAULT_CURRENCY = "EUR"


def _iso(ts_utc: int) -> str:
    return datetime.fromtimestamp(ts_utc, tz=timezone.utc).isoformat()


def _forecast_attribute(
    hourly: list[tuple[int, float]],
    unshaded: list[tuple[int, float]] | None = None,
    chain: dict[int, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Hourly forecast, and what it would have been without the sky map.

    Both curves in one list so a chart can draw them against each other: the
    gap between them is the shadow the model has learned about, and the gap
    that remains to the measurement is the part it has not.
    """
    bare = dict(unshaded or [])
    steps = chain or {}
    return [
        {
            "datetime": _iso(ts),
            "potential_kwh": round(value, 4),
            "unshaded_kwh": round(bare.get(ts, value), 4),
            **(steps.get(ts) or {}),
        }
        for ts, value in hourly
    ]


# --------------------------------------------------------------------------- #
# descriptions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class PlantSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PvStringsData, PvStringsCoordinator], Any]
    attrs_fn: Callable[[PvStringsData, PvStringsCoordinator], dict[str, Any]] | None = (
        None
    )


@dataclass(frozen=True, kw_only=True)
class StringSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PvStringsData, str], Any]
    attrs_fn: Callable[[PvStringsData, str], dict[str, Any]] | None = None


def _now_ts() -> int:
    return int(dt_util.utcnow().timestamp())


def _score(
    data: PvStringsData,
    days: int,
    field: str,
    censored: bool,
    day_ahead: bool = False,
) -> Any:
    """Read one metric out of either score family.

    Nowcast and day-ahead scores have the same shape, so they share a reader
    rather than drifting apart in two copies.
    """
    source = data.scores_day_ahead if day_ahead else data.scores
    block = source.get(days, {})
    bucket = block.get("all_hours" if censored else "uncensored", {})
    value = bucket.get(field)
    if value is None:
        return None
    return round(value * 100, 2) if field == "wmape" else round(value, 4)


_GRANULARITY_NOTE = (
    "wmape and daily_bias_kwh are about whole days; bias, mae_kwh and nmae "
    "are means over single hours."
)

_COMPARABILITY_NOTE = (
    "Only the uncensored figure is comparable with other forecast "
    "services; it excludes hours in which an inverter limit was binding."
)


def _score_attrs(
    data: PvStringsData, days: int, day_ahead: bool = False
) -> dict[str, Any]:
    source = data.scores_day_ahead if day_ahead else data.scores
    block = source.get(days, {})
    attrs = {
        "hours_scored": block.get("hours_scored"),
        "hours_uncensored": block.get("hours_uncensored"),
        "days_scored": block.get("days_scored"),
        "uncensored": block.get("uncensored"),
        "all_hours": block.get("all_hours"),
        "note": f"{_COMPARABILITY_NOTE} {_GRANULARITY_NOTE}",
    }
    if day_ahead:
        attrs["issue_hour_local"] = block.get("issue_hour_local")
        attrs["note"] = (
            "Scored against the forecast as it stood at "
            f"{block.get('issue_hour_local')}:00 local time the evening before, "
            "not against the last run before each hour. "
            f"{_COMPARABILITY_NOTE} {_GRANULARITY_NOTE}"
        )
    return attrs


PLANT_SENSORS: tuple[PlantSensorDescription, ...] = (
    PlantSensorDescription(
        key="forecast_today",
        translation_key="forecast_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, _c: round(data.today_kwh, 3),
        attrs_fn=lambda data, _c: {
            "forecast": _forecast_attribute(
                [
                    (ts, value)
                    for ts, value in data.plant_hourly
                    if data.day_start <= ts < data.day_end
                ]
            ,
            data.plant_unshaded,
            )
        },
    ),
    PlantSensorDescription(
        key="forecast_remaining",
        translation_key="forecast_remaining",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, _c: round(data.remaining_kwh(_now_ts()), 3),
    ),
    PlantSensorDescription(
        key="forecast_tomorrow",
        translation_key="forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, _c: (
            None if data.tomorrow_kwh is None else round(data.tomorrow_kwh, 3)
        ),
        attrs_fn=lambda data, _c: {
            "forecast": _forecast_attribute(
                [
                    (ts, value)
                    for ts, value in data.plant_hourly
                    if data.tomorrow_start <= ts < data.tomorrow_end
                ]
            ,
            data.plant_unshaded,
            )
        },
    ),
    PlantSensorDescription(
        key="forecast_day_after",
        translation_key="forecast_day_after",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, _c: (
            None if data.day_after_kwh is None else round(data.day_after_kwh, 3)
        ),
        attrs_fn=lambda data, _c: {
            "forecast": _forecast_attribute(
                [
                    (ts, value)
                    for ts, value in data.plant_hourly
                    if data.day_after_start <= ts < data.day_after_end
                ]
            ,
            data.plant_unshaded,
            )
        },
    ),
    PlantSensorDescription(
        key="forecast_next_hour",
        translation_key="forecast_next_hour",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda data, _c: round(data.next_hour_kwh(_now_ts()), 3),
    ),
    PlantSensorDescription(
        key="potential_now",
        translation_key="potential_now",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        # The hourly potential in kWh is numerically the mean power in kW over
        # that hour, so the plant sum plots directly against measured watts.
        value_fn=lambda data, _c: round(
            data.plant_between(floor_hour(_now_ts()), floor_hour(_now_ts()) + 3600)
            * 1000,
            1,
        ),
    ),
    PlantSensorDescription(
        key="peak_hour_today",
        translation_key="peak_hour_today",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data, _c: (
            dt_util.utc_from_timestamp(data.peak_hour()[0])
            if data.peak_hour()[0] is not None
            else None
        ),
        attrs_fn=lambda data, _c: {"potential_kwh": round(data.peak_hour()[1], 3)},
    ),
    PlantSensorDescription(
        key="produced_today",
        translation_key="produced_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda data, _c: round(data.produced_today_total, 3),
    ),
    PlantSensorDescription(
        key="deviation_yesterday",
        translation_key="deviation_yesterday",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        # Against the forecast as it stood the evening before, not against the
        # sum of that day's nowcasts.  Unknown rather than -100 % while no such
        # forecast exists yet: a fresh install has nothing to be measured
        # against, which is not the same as having predicted nothing.
        value_fn=lambda data, _c: (
            round(
                (data.forecast_yesterday_kwh - data.produced_yesterday_kwh)
                / data.produced_yesterday_kwh
                * 100,
                2,
            )
            if data.produced_yesterday_kwh > 0
            and data.forecast_yesterday_kwh is not None
            else None
        ),
        attrs_fn=lambda data, _c: {
            "forecast_kwh": (
                None
                if data.forecast_yesterday_kwh is None
                else round(data.forecast_yesterday_kwh, 3)
            ),
            "actual_kwh": round(data.produced_yesterday_kwh, 3),
            "issue_hour_local": DAY_AHEAD_ISSUE_HOUR_LOCAL,
        },
    ),
    PlantSensorDescription(
        key="wmape_7d",
        translation_key="wmape_7d",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: _score(data, 7, "wmape", censored=False),
        attrs_fn=lambda data, _c: _score_attrs(data, 7),
    ),
    PlantSensorDescription(
        key="wmape_30d",
        translation_key="wmape_30d",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: _score(data, 30, "wmape", censored=False),
        attrs_fn=lambda data, _c: _score_attrs(data, 30),
    ),
    PlantSensorDescription(
        key="bias_7d",
        translation_key="bias_7d",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: _score(data, 7, "bias", censored=False),
    ),
    PlantSensorDescription(
        key="wmape_day_ahead_7d",
        translation_key="wmape_day_ahead_7d",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        # Not diagnostic, unlike its nowcast siblings above.  "How much will
        # tomorrow bring" is what this integration exists to answer, and the
        # number that says how well it does so belongs next to the forecast
        # rather than folded away with the plumbing.
        value_fn=lambda data, _c: _score(
            data, 7, "wmape", censored=False, day_ahead=True
        ),
        attrs_fn=lambda data, _c: _score_attrs(data, 7, day_ahead=True),
    ),
    PlantSensorDescription(
        key="wmape_day_ahead_30d",
        translation_key="wmape_day_ahead_30d",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: _score(
            data, 30, "wmape", censored=False, day_ahead=True
        ),
        attrs_fn=lambda data, _c: _score_attrs(data, 30, day_ahead=True),
    ),
    PlantSensorDescription(
        key="bias_day_ahead_30d",
        translation_key="bias_day_ahead_30d",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Per *day*, not per hour: "typically half a kilowatt-hour too
        # optimistic" is a sentence somebody can act on.
        value_fn=lambda data, _c: _score(
            data, 30, "daily_bias_kwh", censored=False, day_ahead=True
        ),
        attrs_fn=lambda data, _c: _score_attrs(data, 30, day_ahead=True),
    ),
    PlantSensorDescription(
        key="savings_today",
        translation_key="savings_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
                suggested_display_precision=2,
        value_fn=lambda data, _c: data.savings.get("today", {}).get("eur"),
    ),
    PlantSensorDescription(
        key="savings_month",
        translation_key="savings_month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
                suggested_display_precision=2,
        value_fn=lambda data, _c: data.savings.get("month", {}).get("eur"),
    ),
    PlantSensorDescription(
        key="savings_total",
        translation_key="savings_total",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
                suggested_display_precision=2,
        value_fn=lambda data, _c: data.savings.get("total", {}).get("eur"),
        attrs_fn=lambda data, _c: {
            "week_eur": data.savings.get("week", {}).get("eur"),
            "year_eur": data.savings.get("year", {}).get("eur"),
            "kwh_total": data.savings.get("total", {}).get("kwh"),
            "eur_per_kwh": data.savings.get("total", {}).get("eur_per_kwh"),
            "annual_estimate_eur": data.savings.get("annual_estimate_eur"),
            "scenario_eur": data.scenarios,
            "note": (
                "Scenarios value the same measured production under every tariff "
                "model, so the cost of a meter swap is visible before it happens."
            ),
        },
    ),
    PlantSensorDescription(
        key="amortisation",
        translation_key="amortisation",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        value_fn=lambda data, _c: (
            data.amortisation.progress_pct if data.amortisation else None
        ),
        attrs_fn=lambda data, _c: (
            {
                "investment_eur": data.amortisation.investment_eur,
                "saved_total_eur": round(data.amortisation.saved_total_eur, 2),
                "annual_saving_eur": data.amortisation.annual_saving_eur,
                "months_remaining": data.amortisation.months_remaining,
                "target_date": (
                    data.amortisation.target_date.isoformat()
                    if data.amortisation.target_date
                    else None
                ),
                "note": (
                    "The annual figure is weighted by the site's own clear-sky "
                    "seasonality, not extrapolated linearly from days elapsed."
                ),
            }
            if data.amortisation
            else {}
        ),
    ),
    PlantSensorDescription(
        key="rain_probability_tomorrow",
        translation_key="rain_probability_tomorrow",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        icon="mdi:weather-rainy",
        # Deliberately not diagnostic: a battery controller deciding how much
        # charge to hold back overnight reads this, so it belongs with the
        # forecast rather than behind the diagnostics fold.  The one figure
        # that steers a decision is the state; the rest are attributes.
        value_fn=lambda data, _c: (
            data.outlook.get("tomorrow", {}).get("rain_probability_pct")
        ),
        attrs_fn=lambda data, _c: {
            "clouds_pct": data.outlook.get("tomorrow", {}).get("clouds_pct"),
            "rain_mm": data.outlook.get("tomorrow", {}).get("rain_mm"),
            "today_rain_probability_pct": (
                data.outlook.get("today", {}).get("rain_probability_pct")
            ),
            "today_clouds_pct": data.outlook.get("today", {}).get("clouds_pct"),
            "today_rain_mm": data.outlook.get("today", {}).get("rain_mm"),
            "note": (
                "Highest hourly chance of rain over the day, from the same "
                "forecast run that drives the yield prediction. Unknown rather "
                "than zero when the source does not offer it."
            ),
        },
    ),
    PlantSensorDescription(
        key="model_observations",
        translation_key="model_observations",
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda data, _c: data.model_summary.get("observations"),
        attrs_fn=lambda data, _c: {
            "log_ratio": data.model_summary.get("log_ratio"),
            "ghi_bias": data.model_summary.get("ghi_bias"),
            "last_learn_cycle": data.learn_stats,
        },
    ),
    PlantSensorDescription(
        key="ghi_forecast",
        translation_key="ghi_forecast",
        device_class=SensorDeviceClass.IRRADIANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="W/m²",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: data.irradiance.get("forecast_wm2"),
        attrs_fn=lambda data, _c: data.irradiance,
    ),
    PlantSensorDescription(
        key="strings_detail",
        translation_key="strings_detail",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _c: len(data.string_detail),
        attrs_fn=lambda data, _c: {
            "strings": data.string_detail,
            "note": (
                "Geometry is a validity history, not a value: a tilt changed "
                "weeks ago explains a pattern that otherwise looks like shading."
            ),
        },
    ),
    PlantSensorDescription(
        key="collector_health",
        translation_key="collector_health",
        native_unit_of_measurement="%",
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, coordinator: (
            round(
                sum(coordinator.collector.stats.coverage_last.values())
                / len(coordinator.collector.stats.coverage_last)
                * 100
            )
            if coordinator.collector.stats.coverage_last
            else None
        ),
        attrs_fn=lambda data, coordinator: {
            **coordinator.collector.stats.as_dict(),
            "weather_ok": data.weather_ok,
            "weather_error": data.weather_error,
        },
    ),
)


STRING_SENSORS: tuple[StringSensorDescription, ...] = (
    StringSensorDescription(
        key="sky_map",
        translation_key="string_sky_map",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:grid",
        # Its own sensor precisely because it is static between refits: the
        # recorder deduplicates attribute blobs by hash, so a map kept away
        # from the moving sun position is stored once instead of every update.
        value_fn=lambda data, sid: len(data.sky_map.get(sid) or []),
        attrs_fn=lambda data, sid: {"cells": data.sky_map.get(sid) or []},
    ),
    StringSensorDescription(
        key="shading_now",
        translation_key="string_shading_now",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        icon="mdi:tree-outline",
        # Not diagnostic: on a plant with a tree or a gable this is the single
        # number that explains the day, and it belongs next to the forecast
        # rather than hidden in the diagnostics fold.
        #
        # Reported as the loss, not as the surviving fraction.  A sensor called
        # "shading" that reads 100 % when nothing is in the way is read exactly
        # backwards by everyone who sees it, including the people who asked for
        # it -- so 0 % is a clear view and 100 % is a panel in full shadow.
        value_fn=lambda data, sid: (
            None
            if (data.shading.get("strings", {}).get(sid, {}).get("factor")) is None
            else round((1.0 - data.shading["strings"][sid]["factor"]) * 100, 1)
        ),
        attrs_fn=lambda data, sid: {
            **{
                key: value
                for key, value in data.shading.get("strings", {})
                .get(sid, {})
                .items()
                if key != "factor"
            },
            "sun_azimuth": data.shading.get("sun_azimuth"),
            "sun_elevation": data.shading.get("sun_elevation"),
            "note": (
                "Loss at the sun's current position: 0 % is a clear view, "
                "100 % is full shadow. Learned from measurements, not from a "
                "description of the garden -- sky the sun has not visited yet "
                "is never corrected, so it reads 0 %."
            ),
        },
    ),
    StringSensorDescription(
        key="forecast_today",
        translation_key="string_forecast_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, sid: round(
            data.strings[sid].sum_between(data.day_start, data.day_end), 3
        ),
        attrs_fn=lambda data, sid: {
            "forecast": _forecast_attribute(
                data.strings[sid].hourly,
                data.strings[sid].unshaded,
                data.strings[sid].chain,
            )
        },
    ),
    StringSensorDescription(
        key="forecast_remaining",
        translation_key="string_forecast_remaining",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, sid: round(
            data.strings[sid].sum_between(floor_hour(_now_ts()), data.day_end), 3
        ),
    ),
    StringSensorDescription(
        key="forecast_tomorrow",
        translation_key="string_forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, sid: round(
            data.strings[sid].sum_between(data.tomorrow_start, data.tomorrow_end), 3
        ),
    ),
    StringSensorDescription(
        key="potential_now",
        translation_key="string_potential_now",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        # The hourly potential in kWh is numerically the mean power in kW over
        # that hour, so this is the expected average, not an instantaneous peak.
        value_fn=lambda data, sid: round(
            data.strings[sid].value_at(floor_hour(_now_ts())) * 1000, 1
        ),
    ),
    StringSensorDescription(
        key="produced_today",
        translation_key="string_produced_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda data, sid: round(data.produced_today.get(sid, 0.0), 3),
    ),
)


# --------------------------------------------------------------------------- #
# platform setup
# --------------------------------------------------------------------------- #


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvStringsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    async_add_entities(
        [PlantSensor(coordinator, entry, description) for description in PLANT_SENSORS]
        + [PlantPowerSensor(coordinator, entry)]
    )

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_STRING:
            continue
        async_add_entities(
            [
                StringSensor(coordinator, entry, subentry_id, subentry.title, description)
                for description in STRING_SENSORS
            ],
            config_subentry_id=subentry_id,
        )


class PvStringsEntity(CoordinatorEntity[PvStringsCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PvStringsCoordinator) -> None:
        super().__init__(coordinator)


class PlantSensor(PvStringsEntity):
    entity_description: PlantSensorDescription

    def __init__(
        self,
        coordinator: PvStringsCoordinator,
        entry: ConfigEntry,
        description: PlantSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = plant_device_info(entry)
        if description.device_class is SensorDeviceClass.MONETARY:
            # Hardcoding EUR would mislabel every value for anyone outside the
            # euro zone; Home Assistant already knows the site currency.
            self._attr_native_unit_of_measurement = (
                coordinator.hass.config.currency or DEFAULT_CURRENCY
            )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, self.coordinator)


class StringSensor(PvStringsEntity):
    entity_description: StringSensorDescription

    def __init__(
        self,
        coordinator: PvStringsCoordinator,
        entry: ConfigEntry,
        string_id: str,
        name: str,
        description: StringSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._string_id = string_id
        self._attr_unique_id = f"{entry.entry_id}_{string_id}_{description.key}"
        self._attr_device_info = string_device_info(entry, string_id, name)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._string_id in self.coordinator.data.strings
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if data is None or self._string_id not in data.strings:
            return None
        return self.entity_description.value_fn(data, self._string_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if (
            data is None
            or self.entity_description.attrs_fn is None
            or self._string_id not in data.strings
        ):
            return None
        return self.entity_description.attrs_fn(data, self._string_id)


class PlantPowerSensor(PvStringsEntity):
    """Measured power summed over every configured string.

    Deliberately not a coordinator value: the coordinator refreshes every
    fifteen minutes, and a power reading that stale is useless for deciding
    whether to start the dishwasher.  This entity follows the string sensors
    directly instead.

    Summing the strings rather than reading a plant meter is what makes it
    comparable with the forecast, which is also a sum over exactly these
    strings -- a house meter would include things the model never modelled.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_translation_key = "power_now"

    def __init__(self, coordinator: PvStringsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_power_now"
        self._attr_device_info = plant_device_info(entry)
        self._entities = [s.power_entity for s in coordinator.plant.strings]
        self._value: float | None = None
        self._missing: list[str] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._entities, self._handle_change
                )
            )
        self._recompute()

    @callback
    def _handle_change(self, _event: Any) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _recompute(self) -> None:
        total = 0.0
        seen = False
        missing: list[str] = []
        for entity_id in self._entities:
            state = self.hass.states.get(entity_id)
            raw = None if state is None else state.state
            if raw in (None, "unknown", "unavailable", ""):
                missing.append(entity_id)
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                missing.append(entity_id)
                continue
            # A string reporting kW must not be added as if it were watts.
            total += units.convert(
                value, state.attributes.get("unit_of_measurement"), units.POWER
            )
            seen = True
        # A partial sum is wrong data, not a small reading.  Publishing it as
        # a valid measurement lets any Riemann-sum helper or Energy dashboard
        # integrate the dip into a permanently lower total, and the history
        # looks exactly like the plant genuinely dropping out.
        self._value = round(total, 1) if seen and not missing else None
        self._missing = missing

    @property
    def available(self) -> bool:
        return super().available and self._value is not None

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "strings_total": len(self._entities),
            "strings_reporting": len(self._entities) - len(self._missing),
            # A partial sum still has a value, but you should know it is partial
            # before comparing it with a forecast that covers every string.
            "not_reporting": self._missing,
        }
