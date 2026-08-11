"""Event-driven capture into five-minute aggregates.

Three sources feed the same buffers:

* ``state_changed`` events, which is where most samples come from
* a watchdog snapshot every ``watchdog_seconds``, so a silent entity still
  produces support points instead of a hole
* the interval boundary itself, which closes the window and persists

Raw seconds are never written to disk.  The buffers only have to survive long
enough to close the current interval.

Nothing in here may block the event loop: all database work goes through the
executor, and a failure to persist one interval must never stop the next one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .core.aggregate import (
    INTERVAL_SECONDS,
    closed_interval,
    SampleBuffer,
    integrate,
    interval_start,
    last_of,
    mean_of,
)
from .core.config import PlantConfig
from .core.quality import VALUE_MEASURED
from .core import units
from .core.store import Store

_LOGGER = logging.getLogger(__name__)

#: Keep two intervals of history so a late-arriving event can still be folded
#: into the window it belongs to.
BUFFER_RETENTION_S = INTERVAL_SECONDS * 2


def _numeric(state: Any) -> float | None:
    """Parse a state object into a float, or ``None`` if it carries no value.

    ``unavailable`` is deliberately not zero.  An inverter dropping below its
    start-up voltage at dawn flickers between ``0.0`` and ``unavailable`` --
    that is information about the light level, and turning it into a hard zero
    at midday would teach the model a fault as if it were weather.
    """
    if state is None:
        return None
    value = getattr(state, "state", state)
    if value in (None, STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class CollectorStats:
    intervals_written: int = 0
    last_flush_ts: int | None = None
    last_flush_duration_ms: float | None = None
    watchdog_ticks: int = 0
    events_seen: int = 0
    write_errors: int = 0
    last_error: str | None = None
    coverage_last: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intervals_written": self.intervals_written,
            "last_flush_ts": self.last_flush_ts,
            "last_flush_duration_ms": self.last_flush_duration_ms,
            "watchdog_ticks": self.watchdog_ticks,
            "events_seen": self.events_seen,
            "write_errors": self.write_errors,
            "last_error": self.last_error,
            "coverage_last": dict(self.coverage_last),
        }


class Collector:
    """Owns the sample buffers and the write path."""

    def __init__(
        self,
        hass: HomeAssistant,
        plant: PlantConfig,
        store: Store,
    ) -> None:
        self.hass = hass
        self.plant = plant
        self.store = store
        self.stats = CollectorStats()
        self._buffers: dict[str, SampleBuffer] = {}
        self._unsubscribes: list[Callable[[], None]] = []
        self._cancel_flush: Callable[[], None] | None = None
        self._running = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def async_start(self) -> None:
        entities = self.plant.tracked_entities
        if not entities:
            _LOGGER.debug("pvstrings: nothing to collect yet")
            return

        for entity_id in entities:
            self._buffers[entity_id] = SampleBuffer(
                watchdog_seconds=self.plant.watchdog_seconds
            )

        # Seed from the current state so the first interval has a carry-in
        # value instead of starting blind.
        now = time.time()
        for entity_id in entities:
            self._buffers[entity_id].add(
                now, _numeric(self.hass.states.get(entity_id))
            )

        self._unsubscribes.append(
            async_track_state_change_event(
                self.hass, list(entities), self._handle_state_event
            )
        )
        self._unsubscribes.append(
            async_track_time_interval(
                self.hass,
                self._handle_watchdog,
                timedelta(seconds=self.plant.watchdog_seconds),
                name="pvstrings watchdog",
            )
        )
        self._running = True
        self._schedule_next_flush()
        _LOGGER.debug(
            "pvstrings: collector watching %d entities, watchdog %ds",
            len(entities),
            self.plant.watchdog_seconds,
        )

    async def async_stop(self) -> None:
        self._running = False
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None
        while self._unsubscribes:
            self._unsubscribes.pop()()
        self._buffers.clear()

    # ------------------------------------------------------------------ #
    # sampling
    # ------------------------------------------------------------------ #

    @callback
    def _handle_state_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        buffer = self._buffers.get(entity_id)
        if buffer is None:
            return
        new_state = event.data["new_state"]
        stamp = (
            new_state.last_updated.timestamp()
            if new_state is not None
            else event.time_fired.timestamp()
        )
        buffer.add(stamp, _numeric(new_state))
        self.stats.events_seen += 1

    @callback
    def _handle_watchdog(self, now: Any) -> None:
        """Snapshot every tracked entity.

        Redundant while events flow -- two identical samples integrate to the
        same energy -- and the only thing standing between us and an unusable
        interval when they stop.
        """
        stamp = dt_util.utcnow().timestamp()
        for entity_id, buffer in self._buffers.items():
            buffer.add(stamp, _numeric(self.hass.states.get(entity_id)))
        self.stats.watchdog_ticks += 1

    # ------------------------------------------------------------------ #
    # flushing
    # ------------------------------------------------------------------ #

    @callback
    def _schedule_next_flush(self) -> None:
        if not self._running:
            return
        next_boundary = interval_start(dt_util.utcnow().timestamp()) + INTERVAL_SECONDS
        # A second of slack so late-arriving events for the closing interval
        # have landed before we integrate it.
        target = dt_util.utc_from_timestamp(next_boundary + 1)
        self._cancel_flush = async_track_point_in_utc_time(
            self.hass, self._handle_flush, target
        )

    async def _handle_flush(self, now: datetime) -> None:
        """Persist the interval that just closed, plus any the loop ran past.

        The catch-up is bounded by what the buffers still hold; asking for
        older windows would only write empty ones.
        """
        self._cancel_flush = None
        self._schedule_next_flush()

        last_closed = closed_interval(now.timestamp())
        previous = self.stats.last_flush_ts
        first = last_closed if previous is None else previous + INTERVAL_SECONDS
        first = max(first, last_closed - BUFFER_RETENTION_S + INTERVAL_SECONDS)

        boundary = first
        while boundary <= last_closed:
            await self.async_flush(boundary)
            boundary += INTERVAL_SECONDS

    async def async_flush(self, window_start: int) -> None:
        """Close and persist the interval starting at ``window_start``."""
        started = time.monotonic()
        window_end = window_start + INTERVAL_SECONDS

        try:
            string_rows = self._build_string_rows(window_start, window_end)
            plant_row = self._build_plant_row(window_start, window_end)
            weather_row = self._build_weather_row(window_start, window_end)

            await self.hass.async_add_executor_job(
                self._write, string_rows, plant_row, weather_row
            )
        except Exception as err:  # noqa: BLE001 - a bad interval must not kill the loop
            self.stats.write_errors += 1
            self.stats.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception("pvstrings: failed to persist interval %s", window_start)
            return
        finally:
            for buffer in self._buffers.values():
                buffer.trim(window_end - BUFFER_RETENTION_S)

        self.stats.intervals_written += len(string_rows)
        self.stats.last_flush_ts = window_start
        self.stats.last_flush_duration_ms = round((time.monotonic() - started) * 1000, 2)

    def _write(
        self,
        string_rows: list[tuple[Any, ...]],
        plant_row: tuple[Any, ...] | None,
        weather_row: tuple[Any, ...] | None,
    ) -> None:
        self.store.upsert_5min(string_rows)
        if plant_row is not None:
            self.store.upsert_plant_state([plant_row])
        if weather_row is not None:
            self.store.upsert_weather_actual([weather_row])

    # ------------------------------------------------------------------ #
    # row construction
    # ------------------------------------------------------------------ #

    def _limit_for(self, group_id: str | None, start: int, end: int) -> float | None:
        """Commanded limit in watts, or ``None`` when the group has none.

        Whether it was actually *binding* is not decided here -- the collector
        does not know the potential.  That flag stays NULL until physics runs.
        """
        if not group_id:
            return None
        try:
            group = self.plant.group(group_id)
        except KeyError:
            return None

        if group.limit_abs_entity:
            buffer = self._buffers.get(group.limit_abs_entity)
            if buffer is not None:
                raw = mean_of(buffer.samples, start, end)
                if raw is None:
                    raw = last_of(buffer.samples, start, end)
                value = group.limit_watts(raw, absolute=True)
                if value is not None:
                    return value

        if group.limit_entity:
            buffer = self._buffers.get(group.limit_entity)
            if buffer is not None:
                raw = mean_of(buffer.samples, start, end)
                if raw is None:
                    raw = last_of(buffer.samples, start, end)
                return group.limit_watts(raw, absolute=False)
        return None

    def _build_string_rows(self, start: int, end: int) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for string in self.plant.strings:
            buffer = self._buffers.get(string.power_entity)
            if buffer is None:
                continue
            samples = buffer.window(start, end)
            energy_wh, power_mean, coverage, count, _peak = integrate(
                samples, start, end, self.plant.watchdog_seconds
            )
            limit = self._limit_for(string.curtailment_group_id, start, end)
            rows.append(
                (
                    start,
                    string.string_id,
                    energy_wh,
                    power_mean,
                    coverage,
                    count,
                    limit,
                    None,  # limit_binding: decided later, by physics
                    VALUE_MEASURED,
                )
            )
            self.stats.coverage_last[string.string_id] = round(coverage, 3)
        return rows

    def _mean_entity(
        self,
        entity_id: str | None,
        start: int,
        end: int,
        quantity: str | None = None,
    ) -> float | None:
        """Interval mean of an entity, normalised to the canonical unit.

        The unit is read from the entity itself at flush time.  A weather
        station reporting km/h or degrees Fahrenheit must not silently reach
        the physics chain in those units.
        """
        if not entity_id:
            return None
        buffer = self._buffers.get(entity_id)
        if buffer is None:
            return None
        value = mean_of(buffer.samples, start, end)
        if value is None or quantity is None:
            return value
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return units.convert(value, unit, quantity)

    def _build_plant_row(self, start: int, end: int) -> tuple[Any, ...] | None:
        state = self.plant.plant_state
        values = (
            self._mean_entity(state.battery_soc_entity, start, end),
            self._mean_entity(state.battery_power_entity, start, end),
            self._mean_entity(state.grid_power_entity, start, end),
            self._mean_entity(state.house_load_entity, start, end),
        )
        if all(value is None for value in values):
            return None
        return (start, *values)

    def _build_weather_row(self, start: int, end: int) -> tuple[Any, ...] | None:
        sources = self.plant.weather_sources
        lux = self._mean_entity(
            sources.illuminance_entity, start, end, units.ILLUMINANCE
        )
        ghi = self._mean_entity(sources.ghi_entity, start, end, units.IRRADIANCE)
        if ghi is None and lux is not None:
            from .core.weather import lux_to_ghi

            ghi = lux_to_ghi(lux)
        values = (
            self._mean_entity(
                sources.temperature_entity, start, end, units.TEMPERATURE
            ),
            self._mean_entity(sources.humidity_entity, start, end, units.RATIO),
            self._mean_entity(sources.wind_speed_entity, start, end, units.SPEED),
            self._mean_entity(
                sources.rain_entity, start, end, units.PRECIPITATION
            ),
            self._mean_entity(sources.pressure_entity, start, end, units.PRESSURE),
            ghi,
            lux,
        )
        if all(value is None for value in values):
            return None
        return (start, *values)
