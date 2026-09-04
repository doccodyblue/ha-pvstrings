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

from . import (
    PvStringsConfigEntry,
    group_device_info,
    plant_device_info,
    string_device_info,
)
from .const import (
    CONF_OUTPUT_PATH,
    OUTPUT_PATH_DIRECT,
    OUTPUT_PATH_NONE,
    OUTPUT_PATH_STORAGE,
    SUBENTRY_GROUP,
    SUBENTRY_STRING,
)
from .coordinator import PvStringsCoordinator, PvStringsData
from .core.aggregate import SPLIT_HOURLY, split_source
from .core.forecast import DAY_AHEAD_ISSUE_HOUR_LOCAL, floor_hour
from .core.weather import OPEN_METEO_ATTRIBUTION, SOURCE_OPEN_METEO
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


_REMAINING_SEMANTICS = (
    "A subset of the day's forecast, not a second summand: forecast today = "
    "forecast elapsed + this. The hour that has already started is split on "
    "the five-minute series the forecast was built from, so half past a "
    "bright hour reports half of it, not all of it."
)
_STALE_SPLIT_NOTE = (
    "split_source is hourly_stale: no five-minute detail for the running "
    "hour, so it is counted whole and this value is too high until the next "
    "refresh."
)


def _remaining_attrs(today: float, remaining: float, source: str) -> dict[str, Any]:
    """The relationship spelled out, because two users read it as a sum."""
    out: dict[str, Any] = {
        "forecast_today_kwh": round(today, 3),
        # Named for what it is: the forecast for the hours that have passed,
        # not what the plant measured in them.
        "forecast_elapsed_kwh": round(today - remaining, 3),
        "split_source": source,
        "semantics": _REMAINING_SEMANTICS,
    }
    if source == SPLIT_HOURLY:
        out["note"] = _STALE_SPLIT_NOTE
    return out


_ATTRIBUTION_NOTE = (
    "Splits the day-ahead error into the two culprits it can have. "
    "chain = what the chain gets wrong when the irradiance is known -- the "
    "same physics, sky map and learned correction, re-run on the measured "
    "irradiance. source = how far the irradiance forecast alone moved the "
    "answer. Both are absolute errors on the same hours, so they do not add "
    "up to the end-to-end figure: an over- and an under-shoot cancel there "
    "and cannot cancel here. The chain figure flatters itself slightly, "
    "because the correction it contains was fitted on these very hours."
)
_ATTRIBUTION_REASONS = {
    "no_irradiance_sensor": (
        "No irradiance sensor configured, so there is nothing to check the "
        "chain against. Add a horizontal GHI or illuminance sensor in the "
        "options to get this split; everything else works without it."
    ),
    "collecting": (
        "Not enough hours with a measured irradiance yet. The split appears "
        "once about a day of them has accumulated."
    ),
}


def _attribution(data: PvStringsData, days: int) -> dict[str, Any]:
    return data.scores_day_ahead.get(days, {}).get("attribution") or {}


def _attribution_pct(data: PvStringsData, days: int, field: str) -> Any:
    value = _attribution(data, days).get(field)
    return None if value is None else round(value * 100, 2)


def _attribution_attrs(data: PvStringsData) -> dict[str, Any]:
    week, month = _attribution(data, 7), _attribution(data, 30)
    reason = week.get("reason")
    out: dict[str, Any] = {
        "wmape_source_7d": _attribution_pct(data, 7, "wmape_source"),
        "wmape_end_to_end_7d": _attribution_pct(data, 7, "wmape_end_to_end"),
        "wmape_chain_30d": _attribution_pct(data, 30, "wmape_chain"),
        "wmape_source_30d": _attribution_pct(data, 30, "wmape_source"),
        "wmape_end_to_end_30d": _attribution_pct(data, 30, "wmape_end_to_end"),
        "hours_split_7d": week.get("hours"),
        "hours_split_30d": month.get("hours"),
        # What the day-ahead score saw in total: the gap to hours_split is the
        # hours no measurement covered.
        "hours_scored_7d": week.get("hours_scored"),
        "reason": reason,
        "semantics": _ATTRIBUTION_NOTE,
    }
    if reason:
        out["note"] = _ATTRIBUTION_REASONS.get(reason, reason)
    return out


_DELIVERED_SEMANTICS = (
    "Valued on delivered energy, not on DC production: each group's measured "
    "DC energy is multiplied by its conversion factor -- measured where an AC "
    "sensor supplies the evidence, otherwise the inverter curve, and for a "
    "battery path the configured charge and discharge efficiencies, which is "
    "an estimate. Strings with no output path stay at DC and are counted as "
    "such under delivery.by_basis_kwh."
)


PLANT_SENSORS: tuple[PlantSensorDescription, ...] = (
    PlantSensorDescription(
        key="forecast_today",
        translation_key="forecast_today",
        device_class=SensorDeviceClass.ENERGY,
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
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, _c: round(data.remaining_kwh(_now_ts()), 3),
        attrs_fn=lambda data, _c: _remaining_attrs(
            data.today_kwh,
            data.remaining_kwh(_now_ts()),
            split_source(data.share_ahead(_now_ts())),
        ),
    ),
    PlantSensorDescription(
        key="forecast_tomorrow",
        translation_key="forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
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
            # The day-by-day history lives on the day-ahead accuracy sensor,
            # not here: it is built from scored pairs and would contradict
            # the two numbers above, which sum every logged hour and the
            # whole measured day.
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
        attrs_fn=lambda data, _c: {
            **_score_attrs(data, 30, day_ahead=True),
            # The days behind this number: what was announced the evening
            # before and what came, plant and per string (keyed by string_id;
            # strings_detail maps names to ids).  Published here because these
            # are exactly the pairs this score is computed from -- a card
            # drawing them cannot disagree with the sensor it sits next to.
            # The last row may be today, with a null actual: it is kept out of
            # the score, but it is the number a reader looks for in the
            # morning.
            "history": data.scores_day_ahead.get(30, {}).get("history"),
        },
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
        key="wmape_chain_7d",
        translation_key="wmape_chain_7d",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        # The published error mixes two culprits: the irradiance the forecast
        # was handed, and what the chain made of it.  This is the second half
        # alone -- and the only one a code change can move.
        value_fn=lambda data, _c: _attribution_pct(data, 7, "wmape_chain"),
        attrs_fn=lambda data, _c: _attribution_attrs(data),
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
            "dc_kwh_total": data.savings.get("total", {}).get("dc_kwh"),
            "eur_per_kwh": data.savings.get("total", {}).get("eur_per_kwh"),
            "annual_estimate_eur": data.savings.get("annual_estimate_eur"),
            "delivery": data.savings.get("delivery"),
            "scenario_eur": data.scenarios,
            "semantics": _DELIVERED_SEMANTICS,
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
                    "seasonality, not extrapolated linearly from days elapsed, "
                    "and rests on delivered energy -- see the savings sensor "
                    "for what each group's conversion factor is based on."
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
        attrs_fn=lambda data, sid: {
            "cells": data.sky_map.get(sid) or [],
            # What this string delivers relative to physics where nothing is
            # in the way.  Only the differential fit can know it; on a
            # single-string plant it is None and the map is an absolute
            # envelope with a capped reference, as before.
            "level": data.sky_level.get(sid),
            "fit_method": data.sky_method.get(sid),
        },
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
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data, sid: round(
            data.strings[sid].remaining_kwh(_now_ts(), data.day_end), 3
        ),
        attrs_fn=lambda data, sid: _remaining_attrs(
            data.strings[sid].sum_between(data.day_start, data.day_end),
            data.strings[sid].remaining_kwh(_now_ts(), data.day_end),
            split_source(data.strings[sid].share_ahead(_now_ts())),
        ),
    ),
    StringSensorDescription(
        key="forecast_tomorrow",
        translation_key="string_forecast_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
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

    plant_entities: list[SensorEntity] = [
        PlantSensor(coordinator, entry, description) for description in PLANT_SENSORS
    ]
    plant_entities.append(PlantPowerSensor(coordinator, entry))
    if any(
        subentry.subentry_type == SUBENTRY_GROUP
        and subentry.data.get(CONF_OUTPUT_PATH) == OUTPUT_PATH_DIRECT
        for subentry in entry.subentries.values()
    ):
        plant_entities += [
            PlantAcForecastSensor(coordinator, entry, "today"),
            PlantAcForecastSensor(coordinator, entry, "tomorrow"),
        ]
    async_add_entities(plant_entities)

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_STRING:
            async_add_entities(
                [
                    StringSensor(
                        coordinator, entry, subentry_id, subentry.title, description
                    )
                    for description in STRING_SENSORS
                ],
                config_subentry_id=subentry_id,
            )
        elif subentry.subentry_type == SUBENTRY_GROUP:
            # One per configured group. A plant with no groups -- the common
            # case -- gets nothing here and is unchanged.
            group_entities: list[SensorEntity] = [
                GroupForecastSensor(coordinator, entry, subentry_id, subentry.title)
            ]
            path = subentry.data.get(CONF_OUTPUT_PATH, OUTPUT_PATH_NONE)
            if path in (OUTPUT_PATH_DIRECT, OUTPUT_PATH_STORAGE):
                # Additive: the DC sensor above stays untouched for every
                # group; conversion output is its own entity with its own
                # unique_id per path.
                group_entities.append(
                    GroupConversionSensor(
                        coordinator, entry, subentry_id, subentry.title, path
                    )
                )
            async_add_entities(group_entities, config_subentry_id=subentry_id)


class PvStringsEntity(CoordinatorEntity[PvStringsCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PvStringsCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def attribution(self) -> str | None:
        """Credit the irradiance source.

        Open-Meteo serves its data under CC-BY 4.0, which obliges us to name
        it wherever the data surfaces.  Resolved per state rather than set
        once, because the source is a config option: on the weather-entity
        fallback no Open-Meteo data is involved and claiming otherwise would
        be a false credit.
        """
        if self.coordinator.plant.forecast_source == SOURCE_OPEN_METEO:
            return OPEN_METEO_ATTRIBUTION
        return None


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


class GroupForecastSensor(PvStringsEntity):
    """What is still to come behind one shared inverter, today.

    One entity per group rather than three: a controller asks "how much can
    still reach this inverter", and today's and tomorrow's totals along with
    the hourly shape ride along as attributes instead of multiplying the
    entity count by the number of groups.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_translation_key = "group_forecast_remaining"

    def __init__(
        self,
        coordinator: PvStringsCoordinator,
        entry: ConfigEntry,
        group_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._attr_unique_id = f"{entry.entry_id}_{group_id}_forecast_remaining"
        self._attr_device_info = group_device_info(entry, group_id, name)

    def _group(self) -> Any:
        data = self.coordinator.data
        return None if data is None else data.groups.get(self._group_id)

    @property
    def available(self) -> bool:
        return super().available and self._group() is not None

    @property
    def native_value(self) -> Any:
        group = self._group()
        if group is None:
            return None
        data = self.coordinator.data
        # Was summed from now_ts against hour-start keys, which dropped the
        # running hour whole -- the plant and string sensors had the opposite
        # error and counted it whole.
        return round(group.remaining_kwh(_now_ts(), data.day_end), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        group = self._group()
        if group is None:
            return None
        data = self.coordinator.data
        out = {
            "today_kwh": round(group.sum_between(data.day_start, data.day_end), 3),
            "tomorrow_kwh": round(
                group.sum_between(data.tomorrow_start, data.tomorrow_end), 3
            ),
            "strings": group.members,
            "forecast": _forecast_attribute(group.hourly),
            "note": (
                "Only the strings behind this inverter. Strings in no group are "
                "in no such total, so the groups need not add up to the plant."
            ),
        }
        out.update(
            _remaining_attrs(
                group.sum_between(data.day_start, data.day_end),
                group.remaining_kwh(_now_ts(), data.day_end),
                split_source(group.share_ahead(_now_ts())),
            )
        )
        return out


_AC_SEMANTICS = (
    "Hardware potential behind the inverter, capped at its rated AC power "
    "only. Deliberately NOT capped at commanded or legal feed-in limits -- "
    "a plant limited to 800 W by regulation but built bigger will see more "
    "here than it may feed in."
)
_CLIP_NOTE = (
    "Clipping is applied to hourly means; brief peaks inside a bright hour "
    "can clip at the inverter even when the hourly mean stays below rated."
)
_STORAGE_SEMANTICS = (
    "Energy expected to land in the battery (DC side, after MPPT and charge "
    "losses). Not comparable to and not summable with AC forecasts: when "
    "this energy leaves the battery again is a control decision, not a "
    "forecast."
)


class GroupConversionSensor(PvStringsEntity):
    """Converted forecast for one group: AC (direct) or battery charge."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: PvStringsCoordinator,
        entry: ConfigEntry,
        group_id: str,
        name: str,
        output_path: str,
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        self._direct = output_path == OUTPUT_PATH_DIRECT
        if self._direct:
            key = "forecast_ac"
            self._attr_translation_key = "group_forecast_ac"
        else:
            key = "forecast_battery_charge"
            self._attr_translation_key = "group_forecast_battery_charge"
        self._attr_unique_id = f"{entry.entry_id}_{group_id}_{key}"
        self._attr_device_info = group_device_info(entry, group_id, name)

    def _group(self) -> Any:
        data = self.coordinator.data
        return None if data is None else data.groups.get(self._group_id)

    @property
    def available(self) -> bool:
        group = self._group()
        return super().available and group is not None and group.converted is not None

    @property
    def native_value(self) -> Any:
        group = self._group()
        if group is None or group.converted is None:
            return None
        return round(
            group.converted_remaining_kwh(
                _now_ts(), self.coordinator.data.day_end
            ),
            3,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        group = self._group()
        if group is None or group.converted is None:
            return None
        data = self.coordinator.data
        out: dict[str, Any] = {
            "output_path": group.output_path,
            "today_kwh": round(
                group.converted_sum_between(data.day_start, data.day_end), 3
            ),
            "tomorrow_kwh": round(
                group.converted_sum_between(data.tomorrow_start, data.tomorrow_end),
                3,
            ),
            "strings": group.members,
            "forecast": _forecast_attribute(group.converted_pairs()),
            "curve_source": group.converted.curve_source,
            "stages": list(group.converted.stages),
            "semantics": _AC_SEMANTICS if self._direct else _STORAGE_SEMANTICS,
        }
        if group.converted.curve_prior is not None:
            out["curve_prior"] = group.converted.curve_prior
        if group.converted.learning is not None:
            out["conversion_learning"] = {
                "stage": "inverter_efficiency",
                **group.converted.learning,
            }
        if group.converted.factor is not None:
            # Flat-factor paths: the number that was actually applied, so a
            # card need not infer it from the ratio of two rounded totals.
            out["conversion_factor"] = group.converted.factor
        if self._direct:
            out["clipped_kwh"] = group.converted.clipped_kwh
            if "clipping" in group.converted.stages:
                out["note"] = _CLIP_NOTE
        return out


class PlantAcForecastSensor(PvStringsEntity):
    """AC forecast summed over the direct-path groups: the externally
    referenced figure (stable unique_id) a controller may consume."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: PvStringsCoordinator,
        entry: ConfigEntry,
        day: str,
    ) -> None:
        super().__init__(coordinator)
        self._day = day
        if day == "today":
            self._attr_translation_key = "forecast_ac_today"
        else:
            self._attr_translation_key = "forecast_ac_tomorrow"
        self._attr_unique_id = f"{entry.entry_id}_forecast_ac_{day}"
        self._attr_device_info = plant_device_info(entry)

    def _window(self) -> tuple[int, int] | None:
        data = self.coordinator.data
        if data is None or not data.has_direct_conversion:
            return None
        if self._day == "today":
            return data.day_start, data.day_end
        return data.tomorrow_start, data.tomorrow_end

    @property
    def available(self) -> bool:
        return super().available and self._window() is not None

    @property
    def native_value(self) -> Any:
        window = self._window()
        if window is None:
            return None
        start, end = window
        return round(
            sum(
                value
                for ts, value in self.coordinator.data.ac_hourly
                if start <= ts < end
            ),
            3,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        window = self._window()
        if window is None:
            return None
        data = self.coordinator.data
        start, end = window
        return {
            "forecast": _forecast_attribute(
                [(ts, value) for ts, value in data.ac_hourly if start <= ts < end]
            ),
            # A consumer treating this as "the whole plant" needs to see what
            # it cannot: strings outside every direct-path group -- both the
            # invisible ones and the ones forecast as battery charge instead.
            "partial": bool(data.unconverted_strings or data.storage_strings),
            "unconverted_strings": data.unconverted_strings,
            "storage_strings": data.storage_strings,
            "semantics": _AC_SEMANTICS,
        }


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
