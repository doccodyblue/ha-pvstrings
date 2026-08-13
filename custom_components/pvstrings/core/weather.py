"""Irradiance forecast sources.

Default source is Open-Meteo: free, no API key, global coverage, and it exposes
the **components** (GHI, DNI, DHI).  We deliberately do not use its
plane-of-array output -- that is computed with an isotropic sky model and a
fixed albedo of 0.20, which throws away most of what a proper transposition
would give us on a tilted string.

A Home Assistant ``weather`` entity can serve as a fallback for users who have
no outbound internet access, at noticeably lower quality: cloud cover has to be
converted to irradiance with an empirical relation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

_LOGGER = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

#: Reanalysis going back decades, used to reconstruct the irradiance a plant
#: actually stood in on days nobody was recording it.  Lower resolution than
#: the forecast models and roughly five days behind, so it is only ever asked
#: about the past.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

SOURCE_OPEN_METEO = "open_meteo"
SOURCE_HA_WEATHER = "ha_weather"

#: Open-Meteo models worth offering.  ``best_match`` lets the API pick the best
#: available regional model for the site, which is the right default everywhere.
OPEN_METEO_MODELS = (
    "best_match",
    "icon_seamless",
    "gfs_seamless",
    "ecmwf_ifs025",
    "meteofrance_seamless",
    "ukmo_seamless",
    "jma_seamless",
    "gem_seamless",
)

_HOURLY_VARIABLES = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "cloud_cover",
    "wind_speed_10m",
    "relative_humidity_2m",
    "precipitation",
    # The reanalysis archive shares this list.  It has no such thing as a
    # probability and answers with a column of nulls rather than an error --
    # which is the honest result: a record of what happened carries no
    # likelihood.  Verified against the live endpoint over the full backfill
    # span (2904 hours, every irradiance value present), because the obvious
    # worry is that the archive rejects the whole request and the backfill
    # then silently returns nothing.  It does not.  One list, so the forecast
    # and archive paths cannot drift into different columns.
    "precipitation_probability",
    "surface_pressure",
)

#: Open-Meteo labels hourly radiation with the **end** of the averaging period:
#: the value at 14:00 is the mean over 13:00-14:00.  Our ``ts_utc`` is always an
#: interval start, so the timestamp has to be moved back by one hour.  One
#: constant, one place to change if the API ever switches convention.
RADIATION_LABEL_OFFSET_S = -3600


@dataclass(frozen=True, slots=True)
class ForecastRow:
    """One forecast hour, ready for ``weather_forecast``."""

    issued_at_utc: int
    ts_utc: int
    source: str
    horizon_h: int
    ghi_wm2: float | None = None
    dni_wm2: float | None = None
    dhi_wm2: float | None = None
    temp_c: float | None = None
    clouds_pct: float | None = None
    wind_ms: float | None = None
    humidity_pct: float | None = None
    rain_mm: float | None = None
    #: Chance of rain in the hour, as the source states it.  Kept separate from
    #: ``rain_mm``: half a millimetre at ninety percent and five millimetres at
    #: ten are different days, and a control loop that has to decide how much
    #: battery to hold back overnight needs the likelihood, not the volume.
    rain_probability_pct: float | None = None
    pressure_hpa: float | None = None
    components_plausible: int | None = None

    def as_row(self) -> tuple[Any, ...]:
        return (
            self.issued_at_utc,
            self.ts_utc,
            self.source,
            self.horizon_h,
            self.ghi_wm2,
            self.dni_wm2,
            self.dhi_wm2,
            self.temp_c,
            self.clouds_pct,
            self.wind_ms,
            self.humidity_pct,
            self.rain_mm,
            self.rain_probability_pct,
            self.pressure_hpa,
            self.components_plausible,
        )


def open_meteo_params(
    latitude: float,
    longitude: float,
    forecast_days: int = 3,
    past_days: int = 1,
    model: str = "best_match",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "hourly": ",".join(_HOURLY_VARIABLES),
        "timeformat": "unixtime",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
        "forecast_days": forecast_days,
        "past_days": past_days,
    }
    if model and model != "best_match":
        params["models"] = model
    return params


def open_meteo_archive_params(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Historical irradiance for one date range, same variables as the forecast.

    Dates are plain ISO days in UTC; the archive has no notion of a forecast
    horizon, so every row it returns is by definition an analysis.
    """
    return {
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "hourly": ",".join(_HOURLY_VARIABLES),
        "timeformat": "unixtime",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
        "start_date": start_date,
        "end_date": end_date,
    }


def _get(values: Sequence[Any] | None, index: int) -> float | None:
    if not values or index >= len(values):
        return None
    value = values[index]
    return None if value is None else float(value)


def parse_open_meteo(
    payload: Mapping[str, Any], issued_at_utc: int, source: str = SOURCE_OPEN_METEO
) -> list[ForecastRow]:
    """Turn an Open-Meteo response into forecast rows.

    Rows in the past are kept: after a restart or an outage they backfill the
    verification window that the bias model learns from.
    """
    hourly = payload.get("hourly") or {}
    times: Sequence[int] = hourly.get("time") or []
    if not times:
        return []

    rows: list[ForecastRow] = []
    for index, raw_time in enumerate(times):
        ts_utc = int(raw_time) + RADIATION_LABEL_OFFSET_S
        horizon_h = int(round((ts_utc - issued_at_utc) / 3600.0))
        rows.append(
            ForecastRow(
                issued_at_utc=issued_at_utc,
                ts_utc=ts_utc,
                source=source,
                horizon_h=horizon_h,
                ghi_wm2=_get(hourly.get("shortwave_radiation"), index),
                dni_wm2=_get(hourly.get("direct_normal_irradiance"), index),
                dhi_wm2=_get(hourly.get("diffuse_radiation"), index),
                temp_c=_get(hourly.get("temperature_2m"), index),
                clouds_pct=_get(hourly.get("cloud_cover"), index),
                wind_ms=_get(hourly.get("wind_speed_10m"), index),
                humidity_pct=_get(hourly.get("relative_humidity_2m"), index),
                rain_mm=_get(hourly.get("precipitation"), index),
                rain_probability_pct=_get(
                    hourly.get("precipitation_probability"), index
                ),
                pressure_hpa=_get(hourly.get("surface_pressure"), index),
            )
        )
    return rows


def ghi_from_cloud_cover(clearsky_ghi: float, clouds_pct: float | None) -> float:
    """Kasten-Czeplak cloud attenuation.

    Only used for the Home Assistant weather-entity fallback.  It is a coarse
    empirical fit -- good enough to keep the integration useful without internet
    access, not good enough to prefer over real irradiance components.
    """
    if clouds_pct is None:
        return clearsky_ghi
    fraction = max(0.0, min(1.0, clouds_pct / 100.0))
    return clearsky_ghi * (1.0 - 0.75 * fraction**3.4)


def rows_from_ha_weather(
    forecast: Iterable[Mapping[str, Any]],
    clearsky_ghi_at: "callable[[int], float]",
    issued_at_utc: int,
    source: str = SOURCE_HA_WEATHER,
) -> list[ForecastRow]:
    """Build forecast rows from a Home Assistant weather forecast list.

    ``forecast`` entries are the dicts returned by ``weather.get_forecasts``
    with ``datetime`` already converted to epoch seconds under the key
    ``ts_utc``.  Components are left empty on purpose so the physics layer
    decomposes GHI itself with Erbs, rather than us inventing a split here.
    """
    rows: list[ForecastRow] = []
    for entry in forecast:
        ts_utc = int(entry["ts_utc"])
        clouds = entry.get("cloud_coverage")
        ghi = ghi_from_cloud_cover(clearsky_ghi_at(ts_utc), clouds)
        rows.append(
            ForecastRow(
                issued_at_utc=issued_at_utc,
                ts_utc=ts_utc,
                source=source,
                horizon_h=int(round((ts_utc - issued_at_utc) / 3600.0)),
                ghi_wm2=ghi,
                dni_wm2=None,
                dhi_wm2=None,
                temp_c=entry.get("temperature"),
                clouds_pct=clouds,
                wind_ms=entry.get("wind_speed"),
                humidity_pct=entry.get("humidity"),
                rain_mm=entry.get("precipitation"),
                rain_probability_pct=entry.get("precipitation_probability"),
                pressure_hpa=entry.get("pressure"),
                components_plausible=0,
            )
        )
    return rows


def lux_to_ghi(lux: float) -> float:
    """Rough illuminance-to-irradiance conversion for cheap outdoor sensors.

    ~120 lm/W is the usual daylight luminous efficacy.  Only a sanity signal,
    never a substitute for a pyranometer.
    """
    return max(0.0, lux / 120.0)
