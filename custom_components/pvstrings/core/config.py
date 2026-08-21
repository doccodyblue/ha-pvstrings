"""Configuration objects.

These mirror what the config flow collects, but carry no Home Assistant types so
the physics and learning code can be exercised without a running instance.

Everything that can change over the lifetime of a plant *and* changes how past
data must be interpreted lives in the database (``string_geometry``), not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

#: Fallback temperature coefficient of Pmax in 1/K (-0.4 %/K).
DEFAULT_TEMP_COEFF = -0.004

#: Ground reflectance used for the transposition model.
DEFAULT_ALBEDO = 0.20

#: DC-to-AC path efficiency (wiring, mismatch, soiling, inverter) when the user
#: gives us nothing better.
DEFAULT_SYSTEM_EFFICIENCY = 0.90

#: Watchdog snapshot interval in seconds.  Measured against an OpenDTU channel:
#: event-driven updates arrive every 10-30 s, so 30 s still yields ten support
#: points per five-minute window during an outage.
DEFAULT_WATCHDOG_SECONDS = 30

#: Length of the primary aggregation interval in seconds.
INTERVAL_SECONDS = 300

ECONOMICS_MODES = ("net_metering", "self_consumption", "feed_in")

TRANSPOSITION_MODELS = ("perez-driesse", "haydavies", "isotropic")

#: Mapping from the friendly mounting option to a pvlib SAPM temperature
#: parameter set.  It lives here rather than in ``physics`` so the config flow
#: can offer the choice without importing pvlib, numpy and pandas -- a UI form
#: has no business dragging in the whole scientific stack, and if those
#: requirements are still installing the flow would fail outright.
MOUNT_TYPES: dict[str, str] = {
    "open_rack": "open_rack_glass_glass",
    "close_mount": "close_mount_glass_glass",
    "insulated_back": "insulated_back_glass_polymer",
    "open_rack_polymer": "open_rack_glass_polymer",
}


#: Home Assistant's ``NumberSelector`` validates its own config and rejects any
#: step below one thousandth.  A violation raises ``voluptuous.Invalid`` inside
#: the HTTP view, which surfaces as a bare "400: Bad Request" with **no log
#: entry at all** -- so it is worth making structurally impossible rather than
#: relying on remembering it.
MIN_SELECTOR_STEP = 0.001


def selector_step(step: float) -> float | str:
    """Clamp a form step to something the number selector will accept.

    Coordinates and temperature coefficients genuinely need finer resolution
    than a thousandth, and "any" is how the selector expresses exactly that.
    """
    return step if step >= MIN_SELECTOR_STEP else "any"


class ConfigError(ValueError):
    """Raised when a configuration cannot be used."""


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GeometrySegment:
    """One validity period of a string's mounting geometry.

    Adjustable mounts are the rule on balcony and small installations, and a
    wrong tilt is *not* a constant error -- it travels with the sun.  A segment
    is therefore never edited in place; a change appends a new segment and old
    data keeps being evaluated against the geometry that was actually installed.
    """

    valid_from_ts_utc: int
    azimuth_deg: float
    tilt_deg: float
    kwp: float
    temp_coeff: float = DEFAULT_TEMP_COEFF
    note: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.azimuth_deg <= 360.0:
            raise ConfigError(f"azimuth out of range: {self.azimuth_deg}")
        if not 0.0 <= self.tilt_deg <= 90.0:
            raise ConfigError(f"tilt out of range: {self.tilt_deg}")
        if self.kwp <= 0.0:
            raise ConfigError(f"kwp must be positive: {self.kwp}")
        if not -0.01 <= self.temp_coeff <= 0.0:
            raise ConfigError(f"implausible temp_coeff: {self.temp_coeff}")
        if self.valid_from_ts_utc < 0:
            raise ConfigError("valid_from_ts_utc must not be negative")

    def as_row(self, string_id: str) -> tuple[Any, ...]:
        return (
            string_id,
            int(self.valid_from_ts_utc),
            float(self.azimuth_deg),
            float(self.tilt_deg),
            float(self.kwp),
            float(self.temp_coeff),
            self.note,
        )


# --------------------------------------------------------------------------- #
# curtailment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CurtailmentGroup:
    """A set of strings that can only be curtailed together.

    Curtailment does not necessarily hit every string at once -- typically each
    inverter forms its own group, and battery-coupled inverters behave very
    differently from grid-tied ones.  A plain ``battery_coupled`` flag on the
    plant is not enough.
    """

    group_id: str
    name: str
    #: Relative limit in percent of the inverter's hardware maximum
    #: (OpenDTU style).
    limit_entity: str | None = None
    #: Absolute limit in watts.  Takes precedence when both are present.
    limit_abs_entity: str | None = None
    #: The inverter's *technical* AC maximum from the datasheet -- not a legal
    #: feed-in cap.  A relative limit of 100 % means "hardware maximum", so
    #: this is the denominator that turns percent into watts and nothing else.
    inverter_max_ac_w: float | None = None
    #: Statically configured absolute limit in watts, e.g. the 800 W a
    #: balcony plant's inverter is permanently set to.  Nothing reports a
    #: persistent limit as an entity, so it has to live in the configuration.
    #: Applies on top of whatever the limit entities command: the lower wins.
    fixed_limit_w: float | None = None
    battery_coupled: bool = False
    soc_limit_pct: float = 100.0
    #: Conversion layer (upgrade.md).  "none" = no conversion, no new
    #: entities -- the pre-conversion behaviour, and the default for every
    #: existing installation.  Loading stays permissive: hard rules (nameplate
    #: required for curves/clipping, no MPPT stage on direct) live in the
    #: config flow so an old entry can never fail to load.
    output_path: str = "none"
    inverter_model: str | None = None
    #: ((load_pct, efficiency), ...) support points for a user-supplied
    #: curve.  Persisted as list-of-lists (HA stores entries as JSON);
    #: build_plant_config normalises back to tuples.
    custom_curve: tuple[tuple[float, float], ...] | None = None
    forecast_clipping: bool = False
    #: External charge controllers only (Victron etc.).  Micro-inverters have
    #: MPPT losses inside their datasheet curve already -- a separate stage
    #: there would subtract the same loss twice.
    mppt_efficiency: float | None = None
    charge_efficiency: float = 0.96
    discharge_efficiency: float = 0.96
    #: Measured AC output, config-only in tranche 1 (future learning target).
    #: Deliberately NOT tracked or persisted yet.
    ac_power_entity: str | None = None

    def __post_init__(self) -> None:
        if not self.group_id:
            raise ConfigError("curtailment group needs an id")
        if self.fixed_limit_w is not None and self.fixed_limit_w <= 0:
            raise ConfigError(
                f"group {self.group_id}: fixed_limit_w must be positive"
            )
        if self.limit_entity and not self.limit_abs_entity:
            if not self.inverter_max_ac_w or self.inverter_max_ac_w <= 0:
                raise ConfigError(
                    f"group {self.group_id}: a relative limit entity requires "
                    "inverter_max_ac_w to be convertible into watts"
                )

    @property
    def has_limit(self) -> bool:
        return bool(self.limit_entity or self.limit_abs_entity or self.fixed_limit_w)

    @property
    def has_live_limit(self) -> bool:
        """Is a limit *entity* configured, as opposed to only the static cap?"""
        return bool(self.limit_entity or self.limit_abs_entity)

    def limit_watts(self, raw: float | None, absolute: bool) -> float | None:
        """Convert a raw limit reading into watts."""
        if raw is None:
            return None
        if absolute:
            return float(raw)
        if not self.inverter_max_ac_w:
            return None
        return float(raw) / 100.0 * float(self.inverter_max_ac_w)

    def effective_limit(self, entity_limit_w: float | None) -> float | None:
        """Combine the live commanded limit with the static one.

        Both constraints hold at once -- a persistent 800 W cap does not go
        away because the DTU commands 100 % -- so the lower of the two is the
        limit the plant actually runs under.

        A configured limit entity that yields no reading is different from no
        entity at all: the live limit may have been *below* the static cap,
        and recording the cap would let the binding test clear measurements it
        cannot actually vouch for.  So an unreadable live limit means no
        verdict, same as before the static cap existed.
        """
        if entity_limit_w is None and self.has_live_limit:
            return None
        if self.fixed_limit_w is None:
            return entity_limit_w
        if entity_limit_w is None:
            return float(self.fixed_limit_w)
        return min(float(entity_limit_w), float(self.fixed_limit_w))


# --------------------------------------------------------------------------- #
# strings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StringConfig:
    """A single PV string, i.e. one measured DC input."""

    string_id: str
    name: str
    power_entity: str
    curtailment_group_id: str | None = None
    #: Optional cumulative energy entity, used only as a cross-check.
    energy_entity: str | None = None
    #: Per-string DC-to-AC efficiency override.
    system_efficiency: float | None = None
    #: Hard ceiling of this MPP tracker in watts, if the inverter has one.
    #:
    #: Micro-inverters commonly cap each tracker well below what the module
    #: could deliver -- a 1600 W AC unit with four trackers lands near 430 W per
    #: channel.  That ceiling behaves exactly like curtailment but is invisible
    #: to the limit entity, so without it the learning layer would conclude that
    #: these strings weaken in bright sun.
    max_power_w: float | None = None
    #: Optional entity reporting the charge controller's own state.
    #:
    #: A solar charger that has finished bulk charging holds the battery at a
    #: voltage instead of taking everything the sun offers -- so it is no
    #: longer measuring the sun, it is measuring its own decision.  Nothing
    #: commands a limit while this happens, and the state entity is the only
    #: place the controller says so.  Victron exposes it; other makes may not,
    #: which is why this stays optional and absent means "carry on as before".
    charger_state_entity: str | None = None
    #: Cell temperature model parameter set, see physics.py.
    mount_type: str = "insulated_back"

    def __post_init__(self) -> None:
        if not self.string_id:
            raise ConfigError("string needs an id")
        if not self.power_entity:
            raise ConfigError(f"string {self.string_id}: power_entity is required")
        if self.system_efficiency is not None and not 0.1 < self.system_efficiency <= 1.0:
            raise ConfigError(
                f"string {self.string_id}: implausible system_efficiency"
            )
        if self.max_power_w is not None and self.max_power_w <= 0:
            raise ConfigError(f"string {self.string_id}: max_power_w must be positive")


# --------------------------------------------------------------------------- #
# economics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Economics:
    """Tariff model for the savings calculation.

    ``net_metering`` is the Ferraris case: the meter runs backwards, so every
    exported kWh really does save an imported one.  It is deliberately a
    separate mode and not "feed_in_tariff happens to equal price_per_kwh",
    because it is temporary by nature -- the meter gets swapped eventually --
    and the scenario comparison needs to be able to say what that will cost.
    """

    mode: str = "self_consumption"
    price_per_kwh: float = 0.30
    feed_in_tariff: float = 0.08
    investment_eur: float = 0.0
    commissioning_date: date | None = None

    def __post_init__(self) -> None:
        if self.mode not in ECONOMICS_MODES:
            raise ConfigError(f"unknown economics mode: {self.mode}")
        if self.price_per_kwh < 0 or self.feed_in_tariff < 0:
            raise ConfigError("prices must not be negative")

    def with_mode(self, mode: str) -> "Economics":
        """Return a copy in a different mode, for scenario comparison."""
        return replace(self, mode=mode)


# --------------------------------------------------------------------------- #
# plant
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlantState:
    """Optional whole-plant entities feeding ``plant_state_5min``."""

    battery_soc_entity: str | None = None
    battery_power_entity: str | None = None
    grid_power_entity: str | None = None
    house_load_entity: str | None = None


@dataclass(frozen=True, slots=True)
class WeatherSources:
    """Optional local measurements used as ground truth for the bias model."""

    temperature_entity: str | None = None
    humidity_entity: str | None = None
    wind_speed_entity: str | None = None
    rain_entity: str | None = None
    pressure_entity: str | None = None
    ghi_entity: str | None = None
    illuminance_entity: str | None = None


@dataclass(frozen=True, slots=True)
class PlantConfig:
    """Everything one config entry knows."""

    name: str
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    time_zone: str = "UTC"
    albedo: float = DEFAULT_ALBEDO
    system_efficiency: float = DEFAULT_SYSTEM_EFFICIENCY
    transposition_model: str = "perez-driesse"
    watchdog_seconds: int = DEFAULT_WATCHDOG_SECONDS
    forecast_source: str = "open_meteo"
    forecast_model: str = "best_match"
    strings: tuple[StringConfig, ...] = ()
    groups: tuple[CurtailmentGroup, ...] = ()
    economics: Economics = field(default_factory=Economics)
    plant_state: PlantState = field(default_factory=PlantState)
    weather_sources: WeatherSources = field(default_factory=WeatherSources)
    learning_enabled: bool = True
    #: How long raw five-minute rows are kept.  Aggregates, geometry and
    #: model state are never discarded -- they are small and irreplaceable.
    retention_days: int = 90

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ConfigError(f"latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ConfigError(f"longitude out of range: {self.longitude}")
        if self.transposition_model not in TRANSPOSITION_MODELS:
            raise ConfigError(f"unknown transposition model: {self.transposition_model}")
        if not 5 <= self.watchdog_seconds <= 300:
            raise ConfigError("watchdog_seconds must be between 5 and 300")
        if not 0.0 <= self.albedo <= 1.0:
            raise ConfigError(f"albedo out of range: {self.albedo}")
        seen: set[str] = set()
        for string in self.strings:
            if string.string_id in seen:
                raise ConfigError(f"duplicate string id: {string.string_id}")
            seen.add(string.string_id)
            if (
                string.curtailment_group_id
                and string.curtailment_group_id not in {g.group_id for g in self.groups}
            ):
                raise ConfigError(
                    f"string {string.string_id} references unknown curtailment "
                    f"group {string.curtailment_group_id}"
                )

    # -- lookups ----------------------------------------------------------- #

    def string(self, string_id: str) -> StringConfig:
        for candidate in self.strings:
            if candidate.string_id == string_id:
                return candidate
        raise KeyError(string_id)

    def group(self, group_id: str) -> CurtailmentGroup:
        for candidate in self.groups:
            if candidate.group_id == group_id:
                return candidate
        raise KeyError(group_id)

    def group_of(self, string_id: str) -> CurtailmentGroup | None:
        group_id = self.string(string_id).curtailment_group_id
        return self.group(group_id) if group_id else None

    def strings_in_group(self, group_id: str) -> tuple[StringConfig, ...]:
        return tuple(s for s in self.strings if s.curtailment_group_id == group_id)

    def efficiency_of(self, string_id: str) -> float:
        override = self.string(string_id).system_efficiency
        return override if override is not None else self.system_efficiency

    @property
    def power_entities(self) -> tuple[str, ...]:
        return tuple(s.power_entity for s in self.strings)

    @property
    def tracked_entities(self) -> tuple[str, ...]:
        """Every entity the collector has to subscribe to."""
        out: list[str] = []
        for string in self.strings:
            out.append(string.power_entity)
            if string.energy_entity:
                out.append(string.energy_entity)
        for group in self.groups:
            out.extend(
                e
                for e in (group.limit_entity, group.limit_abs_entity)
                if e
            )
        state = self.plant_state
        out.extend(
            e
            for e in (
                state.battery_soc_entity,
                state.battery_power_entity,
                state.grid_power_entity,
                state.house_load_entity,
            )
            if e
        )
        weather = self.weather_sources
        out.extend(
            e
            for e in (
                weather.temperature_entity,
                weather.humidity_entity,
                weather.wind_speed_entity,
                weather.rain_entity,
                weather.pressure_entity,
                weather.ghi_entity,
                weather.illuminance_entity,
            )
            if e
        )
        return tuple(dict.fromkeys(out))


def total_kwp(segments: Iterable[GeometrySegment]) -> float:
    return round(sum(segment.kwp for segment in segments), 3)
