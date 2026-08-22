"""The coordinator: weather in, forecast out, learning in between.

Everything expensive -- pvlib, SQLite -- runs in the executor.  The hard rule
from the spec applies here above all: no side process may block the main path.
A failed weather fetch degrades the forecast to the last stored run; a failed
learning cycle leaves the model where it was.  Neither takes the entities down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FORECAST_HOURS,
    FORECAST_INTERVAL,
    OUTPUT_PATH_DIRECT,
    OUTPUT_PATH_NONE,
    OUTPUT_PATH_STORAGE,
    SCORE_WINDOWS,
    WEATHER_INTERVAL,
)
from .core import economics as econ
from .core import persistence
from .core.aggregate import merge_hourly
from .core.config import PlantConfig
from .core.conversion import ConversionResult, convert_group
from .core.learning import SCOPE_CONVERSION_CURVE
from .core.forecast import HOUR, ForecastEngine, LearnStats, floor_hour
from .core.health import Health, learn_summary
from .core.physics import PhysicsEngine, clamp_to_daylight, to_index
from .core.quality import NIGHT_ELEVATION_DEG
from .core.store import Store
from .core.weather import (
    OPEN_METEO_URL,
    SOURCE_HA_WEATHER,
    SOURCE_OPEN_METEO,
    open_meteo_params,
    parse_open_meteo,
    rows_from_ha_weather,
)
from .collector import Collector

_LOGGER = logging.getLogger(__name__)

WEATHER_TIMEOUT = 30


@dataclass(slots=True)
class StringForecast:
    string_id: str
    name: str
    hourly: list[tuple[int, float]] = field(default_factory=list)
    #: The same hours with the sky map switched off.  Plotted against
    #: ``hourly`` it shows how much of the gap to reality the map explains.
    unshaded: list[tuple[int, float]] = field(default_factory=list)
    #: Per hour, what each layer of the model did to the raw physics.
    chain: dict[int, dict[str, float]] = field(default_factory=dict)

    def sum_between(self, start_ts: int, end_ts: int) -> float:
        return sum(
            value for ts, value in self.hourly if start_ts <= ts < end_ts
        )

    def value_at(self, ts_utc: int) -> float:
        for ts, value in self.hourly:
            if ts == ts_utc:
                return value
        return 0.0


@dataclass(slots=True)
class GroupForecast:
    """The forecast for the strings behind one shared inverter.

    A controller deciding what to do with a surplus needs to know how much of
    what is still to come can reach a particular inverter -- because only that
    part can charge the battery behind it, or run into its ceiling. Summing the
    plant tells it nothing, and reconstructing the share from what has already
    been produced tells it about the wrong half of the day: a plant whose
    groups face different ways has a share that moves from morning to evening.
    """

    group_id: str
    name: str
    members: list[str] = field(default_factory=list)
    hourly: list[tuple[int, float]] = field(default_factory=list)
    #: Conversion layer output (AC or battery charge), None for path "none".
    #: ``hourly`` above stays the DC series -- the existing group sensor
    #: reads it and must not move.
    output_path: str = "none"
    converted: ConversionResult | None = None

    def sum_between(self, start_ts: int, end_ts: int) -> float:
        return sum(value for ts, value in self.hourly if start_ts <= ts < end_ts)

    def converted_pairs(self) -> list[tuple[int, float]]:
        if self.converted is None:
            return []
        return sorted(self.converted.hourly_kwh.items())

    def converted_sum_between(self, start_ts: int, end_ts: int) -> float:
        if self.converted is None:
            return 0.0
        return sum(
            value
            for ts, value in self.converted.hourly_kwh.items()
            if start_ts <= ts < end_ts
        )


@dataclass(slots=True)
class PvStringsData:
    """Everything the sensor platform needs, computed once per update."""

    generated_at: int
    day_start: int
    day_end: int
    tomorrow_start: int
    tomorrow_end: int
    day_after_start: int = 0
    day_after_end: int = 0
    strings: dict[str, StringForecast] = field(default_factory=dict)
    plant_hourly: list[tuple[int, float]] = field(default_factory=list)
    plant_unshaded: list[tuple[int, float]] = field(default_factory=list)
    produced_today: dict[str, float] = field(default_factory=dict)
    produced_yesterday_kwh: float = 0.0
    #: ``None`` while no forecast from the evening before yesterday exists --
    #: a fresh install has no such record, and reporting nought would read as
    #: "we predicted nothing" rather than "we were not there yet".
    forecast_yesterday_kwh: float | None = None
    #: Per curtailment group, keyed by group id.  Empty when the plant has no
    #: groups, which is the common case and must stay invisible.
    groups: dict[str, GroupForecast] = field(default_factory=dict)
    #: Plant-wide AC series: sum over the direct-path groups only.  Empty
    #: while no group has output_path "direct".  Storage groups are a
    #: different quantity (battery charge) and are deliberately not in here.
    ac_hourly: list[tuple[int, float]] = field(default_factory=list)
    has_direct_conversion: bool = False
    #: Strings the AC sum cannot see: ungrouped, or in a path-"none" group.
    #: A consumer treating the AC sum as "the whole plant" needs the names,
    #: not a count.
    unconverted_strings: list[str] = field(default_factory=list)
    #: Also outside the AC sum, but accounted for elsewhere: strings whose
    #: group forecasts battery charge instead.
    storage_strings: list[str] = field(default_factory=list)
    scores: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: The same windows scored against the forecast as it stood the evening
    #: before, which is the question the plant is actually bought to answer.
    scores_day_ahead: dict[int, dict[str, Any]] = field(default_factory=dict)
    savings: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, float] = field(default_factory=dict)
    amortisation: Any = None
    model_summary: dict[str, Any] = field(default_factory=dict)
    string_detail: dict[str, Any] = field(default_factory=dict)
    shading: dict[str, Any] = field(default_factory=dict)
    #: The fitted cells per string.  Separate from ``shading`` because this
    #: changes only on a refit, and the recorder can then reuse one stored
    #: attribute blob instead of writing the map out on every update.
    sky_map: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Per string, what it delivers relative to physics where nothing is in
    #: the way -- the joint fit's level term.  A flat map only means something
    #: next to it; ``None`` on a single-string plant, whose absolute fit
    #: cannot know its own level.
    sky_level: dict[str, float | None] = field(default_factory=dict)
    #: Per string: "differential" when fitted against siblings, "absolute"
    #: otherwise.  Per string because one plant can hold both -- a string
    #: with no epochs in common with its siblings keeps an absolute map.
    sky_method: dict[str, str] = field(default_factory=dict)
    #: What the sky is expected to do today and tomorrow, from the same
    #: forecast rows that drive the yield prediction.
    outlook: dict[str, dict[str, Any]] = field(default_factory=dict)
    irradiance: dict[str, Any] = field(default_factory=dict)
    learn_stats: dict[str, int] = field(default_factory=dict)
    weather_ok: bool = True
    weather_error: str | None = None

    # -- plant level aggregates ---------------------------------------- #

    def plant_between(self, start_ts: int, end_ts: int) -> float:
        return sum(value for ts, value in self.plant_hourly if start_ts <= ts < end_ts)

    def plant_between_or_none(self, start_ts: int, end_ts: int) -> float | None:
        """Sum, or ``None`` when the source covered none of the window.

        Zero and "not forecast" are different answers, and a day sensor that
        confidently reports 0.00 kWh because the weather entity only publishes
        24 hours is worse than one that admits it does not know.
        """
        hours = [v for ts, v in self.plant_hourly if start_ts <= ts < end_ts]
        return sum(hours) if hours else None

    @property
    def today_kwh(self) -> float:
        return self.plant_between(self.day_start, self.day_end)

    @property
    def tomorrow_kwh(self) -> float | None:
        return self.plant_between_or_none(self.tomorrow_start, self.tomorrow_end)

    @property
    def day_after_kwh(self) -> float | None:
        return self.plant_between_or_none(self.day_after_start, self.day_after_end)

    def remaining_kwh(self, now_ts: int) -> float:
        return self.plant_between(floor_hour(now_ts), self.day_end)

    def next_hour_kwh(self, now_ts: int) -> float:
        start = floor_hour(now_ts) + HOUR
        return self.plant_between(start, start + HOUR)

    def peak_hour(self) -> tuple[int | None, float]:
        today = [
            (ts, value)
            for ts, value in self.plant_hourly
            if self.day_start <= ts < self.day_end
        ]
        if not today:
            return None, 0.0
        return max(today, key=lambda item: item[1])

    @property
    def produced_today_total(self) -> float:
        return sum(self.produced_today.values())


class PvStringsCoordinator(DataUpdateCoordinator[PvStringsData]):
    """One per config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        plant: PlantConfig,
        store: Store,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {plant.name}",
            update_interval=FORECAST_INTERVAL,
            config_entry=entry,
        )
        self.plant = plant
        self.store = store
        #: Datasheet efficiency curves, loaded in the executor during setup.
        self.inverter_curves: dict[str, tuple[tuple[float, float], ...]] = {}
        self.physics = PhysicsEngine(
            latitude=plant.latitude,
            longitude=plant.longitude,
            elevation_m=plant.elevation_m,
            albedo=plant.albedo,
            transposition_model=plant.transposition_model,
            time_zone=plant.time_zone,
        )
        self.engine = ForecastEngine(plant, store, self.physics)
        self.collector = Collector(hass, plant, store)
        self._weather_entity: str | None = None
        self._last_weather_fetch: datetime | None = None
        self._last_learn_hour: int | None = None
        self._last_purge: date | None = None
        self._monthly_weights: list[float] | None = None
        self.last_learn_stats = LearnStats()
        self.health = Health()

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #

    async def async_prepare(self, weather_entity: str | None = None) -> None:
        self._weather_entity = weather_entity
        # Before loading: a group pointed at a different meter has pairs
        # that answer a question about the old one.
        dropped = await self.hass.async_add_executor_job(
            self.engine.invalidate_changed_conversion_sources,
            int(dt_util.utcnow().timestamp()),
        )
        for group_id in dropped:
            _LOGGER.info(
                "pvstrings: measured AC source changed for group %s, "
                "discarding its conversion pairs and learned curve",
                group_id,
            )
        await self.hass.async_add_executor_job(self.engine.load_models)
        await self.collector.async_start()

    async def async_shutdown(self) -> None:
        await self.collector.async_stop()
        await super().async_shutdown()

    # ------------------------------------------------------------------ #
    # weather
    # ------------------------------------------------------------------ #

    async def async_fetch_weather(self, force: bool = False) -> bool:
        """Pull a new irradiance forecast.  Returns whether anything was stored."""
        now = dt_util.utcnow()
        if (
            not force
            and self._last_weather_fetch is not None
            and now - self._last_weather_fetch < WEATHER_INTERVAL
        ):
            return False

        if self.plant.forecast_source == SOURCE_HA_WEATHER:
            rows = await self._fetch_ha_weather(now)
        else:
            rows = await self._fetch_open_meteo(now)

        if not rows:
            return False

        await self.hass.async_add_executor_job(
            self.store.upsert_weather_forecast, [row.as_row() for row in rows]
        )
        self._last_weather_fetch = now
        return True

    async def _fetch_open_meteo(self, now: datetime) -> list[Any]:
        session = async_get_clientsession(self.hass)
        params = open_meteo_params(
            self.plant.latitude,
            self.plant.longitude,
            forecast_days=max(2, FORECAST_HOURS // 24 + 1),
            past_days=1,
            model=self.plant.forecast_model,
        )
        async with session.get(
            OPEN_METEO_URL, params=params, timeout=WEATHER_TIMEOUT
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        return parse_open_meteo(payload, int(now.timestamp()), SOURCE_OPEN_METEO)

    async def _fetch_ha_weather(self, now: datetime) -> list[Any]:
        """Fallback path for installations without outbound internet.

        Cloud cover has to be converted into irradiance empirically, which is
        distinctly worse than real components -- the integration says so in its
        diagnostics rather than quietly pretending the two are equivalent.
        """
        if not self._weather_entity:
            return []
        response = await self.hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": self._weather_entity, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        entries = (response or {}).get(self._weather_entity, {}).get("forecast", [])
        if not entries:
            return []

        converted: list[dict[str, Any]] = []
        for entry in entries:
            stamp = dt_util.parse_datetime(entry.get("datetime", ""))
            if stamp is None:
                continue
            converted.append({**entry, "ts_utc": int(stamp.timestamp())})

        def build() -> list[Any]:
            from .core.physics import to_index

            stamps = [item["ts_utc"] + HOUR / 2 for item in converted]
            index = to_index(stamps)
            clear = self.physics.clearsky(index)["ghi"]
            lookup = {
                item["ts_utc"]: float(value)
                for item, value in zip(converted, clear.to_numpy())
            }
            return rows_from_ha_weather(
                converted, lambda ts: lookup.get(ts, 0.0), int(now.timestamp())
            )

        return await self.hass.async_add_executor_job(build)

    # ------------------------------------------------------------------ #
    # update cycle
    # ------------------------------------------------------------------ #

    async def _async_update_data(self) -> PvStringsData:
        weather_ok = True
        weather_error: str | None = None
        try:
            await self.async_fetch_weather()
        except Exception as err:  # noqa: BLE001 - stale weather beats no entities
            weather_ok = False
            weather_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("pvstrings: weather fetch failed: %s", weather_error)

        now = dt_util.utcnow()
        current_hour = floor_hour(now.timestamp())
        if self._last_learn_hour != current_hour:
            try:
                self.last_learn_stats = await self.hass.async_add_executor_job(
                    self.engine.learn, int(now.timestamp())
                )
                self._last_learn_hour = current_hour
                stats = self.last_learn_stats.as_dict()
                # Once an hour, in plain words.  Without it a quiet log means
                # only that nothing raised -- which is not the same as the
                # integration having done anything.
                _LOGGER.info("pvstrings: %s: %s", self.plant.name, learn_summary(stats))
                self._report_health(
                    self.health.observe_learn(stats, self.plant.learning_enabled),
                    stats,
                )
            except Exception:  # noqa: BLE001 - learning is a side process
                _LOGGER.exception("pvstrings: learning cycle failed")

        await self._async_maybe_purge(now)

        data = await self.hass.async_add_executor_job(self._build_data, now)
        data.weather_ok = weather_ok
        data.weather_error = weather_error
        data.learn_stats = self.last_learn_stats.as_dict()
        self._report_health(
            self.health.observe_coverage(
                self.collector.stats.coverage_last,
                float(data.shading.get("sun_elevation") or -90.0),
            ),
            data.learn_stats,
        )
        return data

    def _report_health(self, problem: str | None, stats: dict[str, Any]) -> None:
        """Say something the first time a problem sticks, then stay quiet.

        Both conditions are ones the integration used to pass over in silence:
        an installation can capture nothing at all, or capture perfectly and
        learn nothing from any of it, and in each case every sensor keeps
        publishing and nothing raises.
        """
        if problem == "no_capture":
            _LOGGER.warning(
                "pvstrings: %s is capturing nothing while the sun is up. "
                "The configured power entities are reporting no usable values "
                "-- check that they exist and are not unavailable. Nothing "
                "will be learned until this clears.",
                self.plant.name,
            )
        elif problem == "not_learning":
            _LOGGER.warning(
                "pvstrings: %s has folded daylight hours for several cycles "
                "without learning from any of them (%s). The forecast still "
                "works, but it is pure physics and will not improve.",
                self.plant.name,
                learn_summary(stats),
            )

    async def _async_maybe_purge(self, now: datetime) -> None:
        today = dt_util.as_local(now).date()
        if self._last_purge == today:
            return
        self._last_purge = today
        try:
            # Forecast issues have to outlive the widest score window, or
            # day-ahead accuracy starts silently dropping the hours whose
            # older issues have already been thinned away.
            deleted = await self.hass.async_add_executor_job(
                partial(
                    self.store.compact,
                    int(now.timestamp()),
                    self.plant.retention_days,
                    issue_days=max(SCORE_WINDOWS) + 5,
                )
            )
            if any(deleted.values()):
                _LOGGER.debug("pvstrings: compacted %s", deleted)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("pvstrings: compaction failed")

    # ------------------------------------------------------------------ #
    # data assembly (executor)
    # ------------------------------------------------------------------ #

    def _local_day_bounds(self, moment: datetime) -> tuple[int, int]:
        local = dt_util.as_local(moment)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    def _build_data(self, now: datetime) -> PvStringsData:
        now_ts = int(now.timestamp())
        day_start, day_end = self._local_day_bounds(now)
        tomorrow_start, tomorrow_end = day_end, day_end + 86400
        day_after_start, day_after_end = tomorrow_end, tomorrow_end + 86400

        rows = self.engine.forecast(now_ts, hours=FORECAST_HOURS, start_ts=day_start)
        self.engine.log_forecast(now_ts, rows)

        strings: dict[str, StringForecast] = {
            string.string_id: StringForecast(string.string_id, string.name)
            for string in self.plant.strings
        }
        plant_hourly: dict[int, float] = {}
        plant_unshaded: dict[int, float] = {}
        for row in rows:
            bucket = strings.get(row.string_id)
            if bucket is None:
                continue
            bucket.hourly.append((row.ts_utc, round(row.potential_kwh, 4)))
            bucket.unshaded.append((row.ts_utc, round(row.unshaded_kwh, 4)))
            # ``physics_kwh`` is the chain's starting point and the three
            # factors multiply out to ``potential_kwh`` exactly.  The
            # irradiance bias is *not* one of them: it was applied upstream to
            # the irradiance itself and is therefore already inside
            # ``physics_kwh``.  Reported as ``source_bias`` and named that way
            # so nothing downstream is tempted to multiply it in a second time.
            bucket.chain[row.ts_utc] = {
                "source_bias": round(row.bias_factor, 4),
                "shading": round(row.shading_factor, 4),
                "model": round(row.correction, 4),
                "physics_kwh": round(
                    row.unshaded_kwh / row.correction if row.correction else 0.0, 4
                ),
            }
            plant_hourly[row.ts_utc] = plant_hourly.get(row.ts_utc, 0.0) + row.potential_kwh
            plant_unshaded[row.ts_utc] = (
                plant_unshaded.get(row.ts_utc, 0.0) + row.unshaded_kwh
            )

        for bucket in strings.values():
            bucket.hourly.sort()
            bucket.unshaded.sort()

        # Aggregated only after every string has been computed, so that strings
        # inside one group may have entirely different geometry histories --
        # each was already evaluated against its own.  Strings belonging to no
        # group are in none of these sums, which is why the groups need not add
        # up to the plant and no synthetic "ungrouped" bucket is invented.
        groups: dict[str, GroupForecast] = {}
        ac_totals: dict[int, float] = {}
        has_direct = False
        for group in self.plant.groups:
            members = self.plant.strings_in_group(group.group_id)
            if not members:
                continue
            hourly = merge_hourly(
                strings[member.string_id].hourly
                for member in members
                if member.string_id in strings
            )
            # Downstream of everything learned: log_forecast, scoring and
            # censoring all ran on the DC series above.  Static config only
            # (executor context, no hass.states).
            converted = convert_group(
                dict(hourly),
                output_path=group.output_path,
                rated_ac_w=group.inverter_max_ac_w,
                inverter_model=group.inverter_model,
                custom_curve=group.custom_curve,
                forecast_clipping=group.forecast_clipping,
                mppt_efficiency=group.mppt_efficiency,
                charge_efficiency=group.charge_efficiency,
                curves=self.inverter_curves,
                learned=(
                    self.engine.curves.get(f"{group.group_id}|inverter")
                    # Both switches, like every other learned layer: the
                    # plant-wide one exists so a user can see bare physics.
                    if group.curve_learning and self.plant.learning_enabled
                    else None
                ),
            )
            if group.output_path == OUTPUT_PATH_DIRECT and converted is not None:
                has_direct = True
                for ts, value in converted.hourly_kwh.items():
                    ac_totals[ts] = ac_totals.get(ts, 0.0) + value
            groups[group.group_id] = GroupForecast(
                group_id=group.group_id,
                name=group.name,
                members=[member.name for member in members],
                hourly=hourly,
                output_path=group.output_path,
                converted=converted,
            )

        data = PvStringsData(
            generated_at=now_ts,
            day_start=day_start,
            day_end=day_end,
            tomorrow_start=tomorrow_start,
            tomorrow_end=tomorrow_end,
            day_after_start=day_after_start,
            day_after_end=day_after_end,
            strings=strings,
            groups=groups,
            ac_hourly=sorted(
                (ts, round(value, 4)) for ts, value in ac_totals.items()
            ),
            has_direct_conversion=has_direct,
            unconverted_strings=sorted(
                string.name
                for string in self.plant.strings
                if (found := self.plant.group_of(string.string_id)) is None
                or found.output_path == OUTPUT_PATH_NONE
            ),
            storage_strings=sorted(
                string.name
                for string in self.plant.strings
                if (found := self.plant.group_of(string.string_id)) is not None
                and found.output_path == OUTPUT_PATH_STORAGE
            ),
            plant_hourly=sorted(
                (ts, round(value, 4)) for ts, value in plant_hourly.items()
            ),
            plant_unshaded=sorted(
                (ts, round(value, 4)) for ts, value in plant_unshaded.items()
            ),
        )

        for string in self.plant.strings:
            data.produced_today[string.string_id] = self.store.energy_kwh_between(
                day_start, now_ts, string.string_id
            )

        yesterday_start = day_start - 86400
        data.produced_yesterday_kwh = self.store.energy_kwh_between(
            yesterday_start, day_start
        )
        data.forecast_yesterday_kwh = self._logged_forecast_sum(
            yesterday_start, day_start
        )

        for days in SCORE_WINDOWS:
            data.scores[days] = self.engine.score(
                now_ts - days * 86400, now_ts, lead_time_h=0.0
            )
            data.scores_day_ahead[days] = self.engine.score_day_ahead(days, now_ts)

        data.savings = self._savings(now, day_start, now_ts)
        data.amortisation = data.savings.pop("amortisation", None)
        data.scenarios = data.savings.pop("scenarios", {})
        data.string_detail = self._string_detail(day_start, now_ts)
        data.irradiance = self._irradiance_now(now_ts)
        data.outlook = {
            "today": self.store.weather_outlook(
                day_start, day_end, self.plant.forecast_source
            ),
            "tomorrow": self.store.weather_outlook(
                tomorrow_start, tomorrow_end, self.plant.forecast_source
            ),
        }
        data.shading = self._shading_now(now_ts)
        data.sky_map = {
            string.string_id: self.engine.shading.grid(string.string_id)
            for string in self.plant.strings
        }
        data.sky_level = {
            string.string_id: self.engine.shading.level(string.string_id)
            for string in self.plant.strings
        }
        data.sky_method = {
            string.string_id: self.engine.shading.method_of(string.string_id)
            for string in self.plant.strings
        }
        data.model_summary = {
            "log_ratio": self.engine.model.summary(),
            "ghi_bias": self.engine.ghi_bias.summary(),
            "shading": {
                "method": self.engine.shading.method,
                "levels": {
                    string_id: round(value, 3)
                    for string_id, value in sorted(
                        self.engine.shading.levels.items()
                    )
                },
                "strings": self.engine.shading.summary(),
            },
            "observations": self.engine.model.observations_seen,
            # Learned vs. datasheet side by side: an efficiency deviation is
            # otherwise not diagnosable after the fact.
            "conversion_curves": {
                key: curve.as_dict()
                for key, curve in sorted(self.engine.curves.items())
            },
        }
        return data

    def _shading_now(self, now_ts: int) -> dict[str, Any]:
        """What the sky map is doing to each string at this moment.

        The map's whole point is that it varies with the sun's position, so a
        static table of cells says very little on a dashboard.  One number per
        string, right now, is what tells you whether the tree is in the way --
        and it can be plotted against the day to draw the shadow's edge.
        """
        index = to_index([now_ts])
        position = self.engine.physics.solar_position(index)
        azimuth = float(position["azimuth"].iloc[0])
        elevation = float(position["apparent_elevation"].iloc[0])
        counts = self.store.shading_observations_by_string()

        # The map itself is deliberately *not* in here.  These fields change
        # on every update as the sun moves, and Home Assistant deduplicates
        # attribute blobs by hash -- a static grid sitting next to a moving
        # sun position would be written to the recorder again every few
        # minutes.  It lives on its own sensor, which changes only on a refit.
        out: dict[str, Any] = {
            "sun_azimuth": round(azimuth, 1),
            "sun_elevation": round(elevation, 1),
            "strings": {},
        }
        for string in self.plant.strings:
            found = self.engine.shading.maps.get(string.string_id)
            below_horizon = elevation < NIGHT_ELEVATION_DEG
            out["strings"][string.string_id] = {
                "name": string.name,
                # Carried per string rather than in one plant-wide table so a
                # card can show a name instead of a ULID.
                "most_shaded": (found.summary(limit=6)["most_shaded"] if found else []),
                "factor": (
                    None
                    if below_horizon
                    else round(
                        self.engine.shading.factor(
                            string.string_id, azimuth, elevation, now_ts
                        ),
                        3,
                    )
                ),
                "observations": counts.get(string.string_id, 0),
                "cells": found.observed_cells if found else 0,
                "seasonal_cells": (len(found.seasonal) // 2) if found else 0,
            }
        return out

    def _string_detail(self, day_start: int, now_ts: int) -> dict[str, Any]:
        """Per-string geometry history and today's data quality.

        The geometry history is the one thing a Lovelace card cannot reach on
        its own, and it is exactly what you need when a string starts drifting:
        a tilt that was changed three weeks ago explains a pattern that would
        otherwise look like shading.

        The quality stats cover daylight only.  Averaged over the whole day
        they would grade the source's night-time manners instead: a DTU-style
        source goes unavailable while its microinverter sleeps and started
        every day with a coverage handicap that a Victron, reporting 0 W all
        night, never had.
        """
        # Local noon's UTC date is the calendar day this daylight belongs to;
        # the UTC date of local midnight is yesterday's for any site east of
        # Greenwich.
        window = self.engine.physics.daylight_window_for(day_start + 43200)
        stats_start, stats_end = clamp_to_daylight(day_start, now_ts, window)
        out: dict[str, Any] = {}
        for string in self.plant.strings:
            history = self.store.geometry_history(string.string_id)
            current = self.store.geometry_at(string.string_id, now_ts)
            stats = self.store.interval_stats(string.string_id, stats_start, stats_end)
            group = None
            if string.curtailment_group_id:
                try:
                    group = self.plant.group(string.curtailment_group_id).name
                except KeyError:
                    group = None
            out[string.name] = {
                "string_id": string.string_id,
                "power_entity": string.power_entity,
                "group": group,
                "mount_type": string.mount_type,
                "azimuth": current.azimuth_deg if current else None,
                "tilt": current.tilt_deg if current else None,
                "kwp": current.kwp if current else None,
                "temp_coeff": current.temp_coeff if current else None,
                "geometry_periods": len(history),
                "geometry_history": [
                    {
                        "from": (
                            "Anbeginn"
                            if segment.valid_from_ts_utc == 0
                            else dt_util.as_local(
                                dt_util.utc_from_timestamp(segment.valid_from_ts_utc)
                            ).strftime("%Y-%m-%d %H:%M")
                        ),
                        "azimuth": segment.azimuth_deg,
                        "tilt": segment.tilt_deg,
                        "kwp": segment.kwp,
                        "note": segment.note,
                    }
                    for segment in history
                ],
                "today": stats,
                "produced_today_kwh": round(
                    self.store.energy_kwh_between(day_start, now_ts, string.string_id),
                    3,
                ),
            }
        return out

    def _irradiance_now(self, now_ts: int) -> dict[str, Any]:
        """Forecast, measured and clear-sky irradiance for the running hour.

        Irradiance is the dominant error source -- the physics behind it is
        deterministic -- so the gap between what the source promised and what
        actually arrived is the most informative single number the integration
        can show.  Without a sensor for the forecast side there is nothing to
        plot a pyranometer against.
        """
        hour = floor_hour(now_ts)
        rows = self.store.latest_forecast(hour, hour + HOUR, self.plant.forecast_source)
        # ``is not None``, not truthiness: at night the forecast is a perfectly
        # good 0.0 W/m2, and reading that as "no value" makes the sensor go
        # unknown every evening -- indistinguishable from a broken source.
        forecast = (
            float(rows[0]["ghi_wm2"])
            if rows and rows[0]["ghi_wm2"] is not None
            else None
        )

        measured = None
        actual = self.store.weather_actual_range(hour, hour + HOUR)
        values = [r["ghi_wm2"] for r in actual if r["ghi_wm2"] is not None]
        if values:
            measured = round(sum(values) / len(values), 1)

        from .core.physics import to_index

        index = to_index([hour + HOUR / 2])
        clearsky = round(float(self.physics.clearsky(index)["ghi"].iloc[0]), 1)

        sources = self.plant.weather_sources
        state = self.plant.plant_state
        has_sensor = bool(sources.ghi_entity or sources.illuminance_entity)
        return {
            "forecast_wm2": round(forecast, 1) if forecast is not None else None,
            "measured_wm2": measured,
            "clearsky_wm2": clearsky,
            "clearsky_index": (
                round(measured / clearsky, 3)
                if measured is not None and clearsky > 5
                else None
            ),
            "forecast_error_wm2": (
                round(forecast - measured, 1)
                if forecast is not None and measured is not None
                else None
            ),
            # Which yardstick the bias model is actually learning against.
            # Without a pyranometer it can only compare the source with its own
            # short-horizon run, which is a weaker claim and should say so.
            # Predates the nowcast feature and means something else entirely --
            # see ``nowcast_*`` below for that.
            "truth_source": "measured" if has_sensor and measured is not None else "nowcast",
            **self._nowcast_attributes(),
            "ghi_entity": sources.ghi_entity,
            "illuminance_entity": sources.illuminance_entity,
            # Reported rather than guessed: Home Assistant never exposes entry
            # options over the API, so a dashboard that hardcoded this list
            # would keep claiming a sensor is in use long after it was removed.
            "sources": {
                label: entity
                for label, entity in (
                    ("Globalstrahlung", sources.ghi_entity),
                    ("Beleuchtungsstärke", sources.illuminance_entity),
                    ("Außentemperatur", sources.temperature_entity),
                    ("Luftfeuchte", sources.humidity_entity),
                    ("Wind", sources.wind_speed_entity),
                    ("Luftdruck", sources.pressure_entity),
                    ("Niederschlag", sources.rain_entity),
                    ("Batterie-SOC", state.battery_soc_entity),
                    ("Batterieleistung", state.battery_power_entity),
                    ("Netzleistung", state.grid_power_entity),
                    ("Hausverbrauch", state.house_load_entity),
                )
                if entity
            },
        }

    def _nowcast_attributes(self) -> dict[str, Any]:
        """What the sensor contributed to the remaining forecast, or why nothing.

        A factor of 1.00 and "never ran" look identical from the outside, so the
        reason travels with it.  Kept on this one plant-level sensor and rounded
        on purpose: the forecast sensors now change every cycle rather than only
        when the source updates, and per-interval diagnostics in their large
        ``forecast`` attribute lists would multiply recorder writes.
        """
        state = self.engine.last_nowcast
        if state is None:
            return {
                "nowcast_active": False,
                "nowcast_reason": self.engine.last_nowcast_reason or None,
            }
        return {
            "nowcast_active": True,
            "nowcast_reason": None,
            # Measured clearness index the coming intervals are faded towards.
            "nowcast_kt": round(state.kt, 3),
            # Weight at the very next interval; decays to zero over the reach.
            "nowcast_weight_now": round(state.weight(300.0), 3),
            "nowcast_halflife_min": round(state.halflife_s / 60.0),
            # "calm" carries roughly twice as far as "broken"; an unknown
            # regime is treated as broken rather than guessed generously.
            "nowcast_sky": (
                "unknown"
                if state.spread is None
                else "calm"
                if state.spread <= persistence.SPREAD_SPLIT
                else "broken"
            ),
            "nowcast_spread": (
                None if state.spread is None else round(state.spread, 3)
            ),
            "nowcast_intervals": state.intervals,
            # Damping while the bias model is still thin: below 1.0 the
            # measurement is believed only in part.
            "nowcast_trust": round(state.trust, 3),
        }

    def _logged_forecast_sum(self, start_ts: int, end_ts: int) -> float | None:
        """What we said the day would bring, as of the evening before.

        Deliberately not the nowcast sum: "deviation yesterday" reads as a
        comparison against an announcement, and summing forecasts issued
        minutes before each hour is not an announcement -- it flattered the
        number without answering the question anybody was asking of it.
        """
        rows = self.store.forecast_vs_actual_before(
            start_ts, end_ts, self.engine.day_ahead_cutoff(start_ts)
        )
        predicted = [row["potential_kwh"] for row in rows if row["potential_kwh"]]
        return sum(predicted) if predicted else None

    def _savings(
        self, now: datetime, day_start: int, now_ts: int
    ) -> dict[str, Any]:
        economics = self.plant.economics
        local = dt_util.as_local(now)
        month_start = int(
            local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        week_start = day_start - local.weekday() * 86400
        year_start = int(
            local.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
        )
        commissioning = economics.commissioning_date or local.date()
        total_start = int(
            datetime(
                commissioning.year,
                commissioning.month,
                commissioning.day,
                tzinfo=local.tzinfo,
            ).timestamp()
        )

        out: dict[str, Any] = {}
        for label, start in (
            ("today", day_start),
            ("week", week_start),
            ("month", month_start),
            ("year", year_start),
            ("total", total_start),
        ):
            produced = self.store.energy_kwh_between(start, now_ts)
            _imported, exported = self.store.grid_energy_kwh(start, now_ts)
            has_grid = self.plant.plant_state.grid_power_entity is not None
            result = econ.savings(
                produced, exported if has_grid else None, economics
            )
            out[label] = {
                "kwh": round(produced, 3),
                "export_kwh": round(result.export_kwh, 3),
                "eur": round(result.saved_eur, 2),
                "eur_per_kwh": round(result.eur_per_kwh, 4),
            }

        produced_total = out["total"]["kwh"]
        _imported, exported_total = self.store.grid_energy_kwh(total_start, now_ts)
        has_grid = self.plant.plant_state.grid_power_entity is not None
        out["scenarios"] = {
            mode: round(result.saved_eur, 2)
            for mode, result in econ.scenarios(
                produced_total, exported_total if has_grid else None, economics
            ).items()
        }

        weights = self._weights()
        # Scaled over the period the data actually covers, not over the period
        # since commissioning.  A plant commissioned in May and given this
        # integration in August has four months nobody recorded; dividing one
        # week of savings by a third of a year understates the annual figure so
        # badly that the amortisation lands centuries out -- and, when the rate
        # gets small enough, overflows the date entirely.
        first_ts = self.store.first_hour_ts()
        measured_from = (
            commissioning
            if first_ts is None
            else max(
                commissioning,
                datetime.fromtimestamp(first_ts, tz=local.tzinfo).date(),
            )
        )
        annual = econ.annual_estimate(
            out["total"]["eur"],
            measured_from,
            local.date(),
            weights,
        )
        out["measured_from"] = measured_from.isoformat()
        out["annual_estimate_eur"] = round(annual, 2) if annual is not None else None
        out["amortisation"] = econ.amortisation(
            economics.investment_eur,
            out["total"]["eur"],
            annual,
            local.date(),
        )
        return out

    def _weights(self) -> list[float]:
        if self._monthly_weights is None:
            self._monthly_weights = self.engine.monthly_weights()
        return self._monthly_weights

    # ------------------------------------------------------------------ #
    # service entry points
    # ------------------------------------------------------------------ #

    async def async_recalculate(self) -> None:
        await self.async_fetch_weather(force=True)
        await self.async_request_refresh()

    async def async_reset_learning(self, string_id: str | None = None) -> None:
        """Discard learned corrections -- one string's, or everything.

        The shading map is a learned correction like any other, and it is the
        one the per-string effects are calibrated against.  Clearing the
        effects while leaving the map in place is worse than clearing neither:
        the forecast keeps being multiplied down without the offsetting level
        the model had learned.  It is also the only way back from a backfill
        built on a mis-scaled sensor.

        With ``string_id``: only that string's observations and factors go --
        the repair for one corrected geometry.  Plant-scope factors, ghi_bias
        and the siblings stay; whatever the bad string leaked into the plant
        scope relearns on its own.
        """
        if string_id:
            await self.hass.async_add_executor_job(
                self.store.clear_effects_for_string, string_id
            )
            await self.hass.async_add_executor_job(
                self.store.clear_shading_obs, string_id
            )
            await self.hass.async_add_executor_job(
                self.store.clear_conversion_obs, string_id
            )
            # The pairs are gone; the curve already fitted from them has to
            # go with them, or the next reload brings the poisoned version
            # straight back. Its own scope and its group's.
            group = self.plant.group_of(string_id)
            await self.hass.async_add_executor_job(
                self.store.clear_effects_with_prefix,
                SCOPE_CONVERSION_CURVE,
                [f"{string_id}|"] + ([f"{group.group_id}|"] if group else []),
            )
        else:
            await self.hass.async_add_executor_job(self.store.clear_effects, None)
            await self.hass.async_add_executor_job(self.store.clear_shading_obs)
            # The curves are refitted from these every hour, so leaving them
            # would rebuild what the reset just discarded.
            await self.hass.async_add_executor_job(self.store.clear_conversion_obs)
        await self.hass.async_add_executor_job(self.engine.load_models)
        await self.async_request_refresh()
