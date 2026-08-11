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
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import PvStringsConfigEntry, plant_device_info, string_device_info
from .const import SUBENTRY_STRING
from .coordinator import PvStringsCoordinator, PvStringsData
from .core.forecast import floor_hour

#: Fallback when Home Assistant has no currency configured.
DEFAULT_CURRENCY = "EUR"


def _iso(ts_utc: int) -> str:
    return datetime.fromtimestamp(ts_utc, tz=timezone.utc).isoformat()


def _forecast_attribute(hourly: list[tuple[int, float]]) -> list[dict[str, Any]]:
    return [
        {"datetime": _iso(ts), "potential_kwh": round(value, 4)}
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


def _score(data: PvStringsData, days: int, field: str, censored: bool) -> Any:
    block = data.scores.get(days, {})
    bucket = block.get("all_hours" if censored else "uncensored", {})
    value = bucket.get(field)
    if value is None:
        return None
    return round(value * 100, 2) if field == "wmape" else round(value, 4)


def _score_attrs(data: PvStringsData, days: int) -> dict[str, Any]:
    block = data.scores.get(days, {})
    return {
        "hours_scored": block.get("hours_scored"),
        "hours_uncensored": block.get("hours_uncensored"),
        "uncensored": block.get("uncensored"),
        "all_hours": block.get("all_hours"),
        "note": (
            "Only the uncensored figure is comparable with other forecast "
            "services; it excludes hours in which an inverter limit was binding."
        ),
    }


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
        value_fn=lambda data, _c: round(data.tomorrow_kwh, 3),
        attrs_fn=lambda data, _c: {
            "forecast": _forecast_attribute(
                [
                    (ts, value)
                    for ts, value in data.plant_hourly
                    if data.tomorrow_start <= ts < data.tomorrow_end
                ]
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
        value_fn=lambda data, _c: round(data.day_after_kwh, 3),
        attrs_fn=lambda data, _c: {
            "forecast": _forecast_attribute(
                [
                    (ts, value)
                    for ts, value in data.plant_hourly
                    if data.day_after_start <= ts < data.day_after_end
                ]
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
        value_fn=lambda data, _c: (
            round(
                (data.forecast_yesterday_kwh - data.produced_yesterday_kwh)
                / data.produced_yesterday_kwh
                * 100,
                2,
            )
            if data.produced_yesterday_kwh > 0
            else None
        ),
        attrs_fn=lambda data, _c: {
            "forecast_kwh": round(data.forecast_yesterday_kwh, 3),
            "actual_kwh": round(data.produced_yesterday_kwh, 3),
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
            "forecast": _forecast_attribute(data.strings[sid].hourly)
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
        PlantSensor(coordinator, entry, description) for description in PLANT_SENSORS
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
