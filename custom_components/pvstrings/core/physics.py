"""Physical model of one string, built on pvlib.

The chain is deterministic and needs no training at all -- it produces
``potential_kwh`` on day one.  Everything the learning layer does afterwards is
a correction of what is left over.

Two details that are easy to get wrong and expensive to get wrong:

* Solar position is evaluated at the **midpoint** of each interval.  Assigning
  it to the interval start produces a systematic transposition error that grows
  towards sunrise and sunset.
* Geometry is resolved **per timestamp** from the validity history, never taken
  from the current configuration.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import pandas as pd
import pvlib
from pvlib.location import Location
from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS

from .config import MOUNT_TYPES, GeometrySegment

_LOGGER = logging.getLogger(__name__)

#: Relative tolerance for the GHI ~ DHI + DNI*cos(z) closure test.
COMPONENT_TOLERANCE = 0.15

#: Below this elevation the closure test is meaningless (huge air mass, tiny
#: signal) and the components are accepted as given.
COMPONENT_MIN_ELEVATION_DEG = 5.0


@dataclass(frozen=True, slots=True)
class StringResult:
    """Per-interval physical output of one string."""

    index: pd.DatetimeIndex
    dc_power_w: pd.Series
    poa_global: pd.Series
    cell_temp_c: pd.Series
    aoi_deg: pd.Series

    def energy_kwh(self, interval_seconds: int) -> float:
        return float(self.dc_power_w.sum()) * interval_seconds / 3600.0 / 1000.0


def to_index(timestamps: Sequence[float]) -> pd.DatetimeIndex:
    """Unix epoch seconds -> tz-aware UTC index."""
    return pd.DatetimeIndex(
        pd.to_datetime(list(timestamps), unit="s", utc=True), name="ts_utc"
    )


def to_epoch(index: pd.DatetimeIndex) -> list[int]:
    return [int(value.timestamp()) for value in index]


class PhysicsEngine:
    """Site-level physics.  One instance per config entry."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        elevation_m: float = 0.0,
        albedo: float = 0.20,
        transposition_model: str = "perez-driesse",
        time_zone: str = "UTC",
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.albedo = albedo
        self.transposition_model = transposition_model
        self.time_zone = time_zone
        self.location = Location(
            latitude, longitude, altitude=elevation_m, tz="UTC", name="pvstrings"
        )
        self._turbidity_lookup_ok: bool | None = None

    # -- solar geometry ---------------------------------------------------- #

    def solar_position(self, index: pd.DatetimeIndex) -> pd.DataFrame:
        return self.location.get_solarposition(index)

    def _fallback_turbidity(self, index: pd.DatetimeIndex) -> pd.Series:
        """Climatological Linke turbidity without the lookup table.

        Turbidity peaks in summer (more water vapour and aerosol) and is lower
        at high latitudes.  This is coarse next to the gridded climatology, but
        the clear-sky model is only the *shape* carrier here -- the actual
        irradiance comes from the forecast, and the GHI bias model absorbs a
        systematic offset in this term anyway.
        """
        day_of_year = index.dayofyear.to_numpy()
        # Northern summer peaks around day 200; flip for the southern hemisphere.
        peak = 200.0 if self.latitude >= 0 else 200.0 - 182.5
        base = 3.5 - 0.01 * abs(self.latitude)
        values = base + 0.8 * np.cos(2 * np.pi * (day_of_year - peak) / 365.0)
        return pd.Series(values, index=index, name="turbidity")

    def linke_turbidity(self, index: pd.DatetimeIndex) -> pd.Series:
        """Gridded Linke turbidity, falling back to a climatology.

        The lookup reads a 2160x4320x12 HDF5 grid through ``h5py``.  That is a
        heavy native dependency for a container image to satisfy, and if it is
        missing or the data file cannot be read there is no reason to take the
        whole integration down with it.
        """
        if self._turbidity_lookup_ok is not False:
            try:
                turbidity = pvlib.clearsky.lookup_linke_turbidity(
                    index, self.latitude, self.longitude
                )
                self._turbidity_lookup_ok = True
                return turbidity
            except Exception as err:  # noqa: BLE001 - any failure means fall back
                if self._turbidity_lookup_ok is None:
                    _LOGGER.warning(
                        "pvstrings: Linke turbidity lookup unavailable (%s); "
                        "using a latitude and season climatology instead. "
                        "Clear-sky shape stays usable, accuracy is slightly lower",
                        err,
                    )
                self._turbidity_lookup_ok = False
        return self._fallback_turbidity(index)

    def clearsky(
        self, index: pd.DatetimeIndex, solar_position: pd.DataFrame | None = None
    ) -> pd.DataFrame:
        """Ineichen clear sky with climatological Linke turbidity."""
        return self.location.get_clearsky(
            index,
            model="ineichen",
            solar_position=solar_position,
            linke_turbidity=self.linke_turbidity(index),
        )

    @functools.lru_cache(maxsize=512)
    def solar_noon_ts(self, day_ordinal: int) -> float:
        """Epoch seconds of solar transit for a given proleptic day ordinal.

        Cached: the daypart classification asks for this on every interval.
        """
        day = datetime.fromordinal(day_ordinal).replace(tzinfo=timezone.utc)
        index = pd.DatetimeIndex([pd.Timestamp(day)])
        transit = pvlib.solarposition.sun_rise_set_transit_spa(
            index, self.latitude, self.longitude
        )["transit"]
        value = transit.iloc[0]
        if pd.isna(value):
            # Polar day or night: fall back to local apparent noon.
            return day.timestamp() + 12 * 3600 - self.longitude / 15.0 * 3600
        return float(value.timestamp())

    def solar_noon_for(self, ts_utc: float) -> float:
        day = datetime.fromtimestamp(ts_utc, tz=timezone.utc).date()
        return self.solar_noon_ts(day.toordinal())

    # -- irradiance components --------------------------------------------- #

    def components_plausible(
        self,
        ghi: pd.Series,
        dni: pd.Series,
        dhi: pd.Series,
        solar_position: pd.DataFrame,
    ) -> pd.Series:
        """Closure test ``GHI ~ DHI + DNI*cos(zenith)``.

        Several free forecast sources ship components that do not close.  Using
        them anyway silently corrupts the transposition, so we test and fall
        back to a decomposition model when they fail.
        """
        cos_zenith = np.cos(np.radians(solar_position["apparent_zenith"])).clip(lower=0)
        reconstructed = dhi + dni * cos_zenith
        denominator = ghi.where(ghi > 20.0)
        relative = (reconstructed - ghi).abs() / denominator
        low_sun = solar_position["apparent_elevation"] < COMPONENT_MIN_ELEVATION_DEG
        plausible = (relative <= COMPONENT_TOLERANCE) | relative.isna() | low_sun
        return plausible.fillna(True)

    def decompose(
        self, ghi: pd.Series, solar_position: pd.DataFrame, index: pd.DatetimeIndex
    ) -> tuple[pd.Series, pd.Series]:
        """Derive DNI and DHI from GHI with the Erbs model."""
        result = pvlib.irradiance.erbs(
            ghi.clip(lower=0), solar_position["apparent_zenith"], index
        )
        return result["dni"].fillna(0.0), result["dhi"].fillna(0.0)

    def ensure_components(
        self,
        ghi: pd.Series,
        dni: pd.Series | None,
        dhi: pd.Series | None,
        solar_position: pd.DataFrame,
        index: pd.DatetimeIndex,
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Return usable ``(dni, dhi, plausible)`` for the whole series."""
        if dni is None or dhi is None:
            derived_dni, derived_dhi = self.decompose(ghi, solar_position, index)
            return derived_dni, derived_dhi, pd.Series(False, index=index)

        # Present and plausible are two different questions, and conflating
        # them is expensive.  ``components_plausible`` is a closure test, and a
        # closure test on a missing value cannot fail -- so it answers "true",
        # meaning "I found nothing wrong", which is not the same as "this is
        # usable".  Taken at face value the missing components then survive to
        # ``fillna(0.0)`` and become a hard zero: a plant standing in 640 W/m2
        # is modelled with no beam and no diffuse light, leaving only the
        # ground reflection.  The physics comes out around a hundredth of the
        # truth, every measured-versus-physics ratio explodes past the sanity
        # bound, and the learning quietly stops -- on precisely the
        # installations that took the trouble to fit an irradiance sensor,
        # because that is the path that blanks the components in the first
        # place.
        present = dni.notna() & dhi.notna()
        plausible = (
            self.components_plausible(ghi, dni, dhi, solar_position) & present
        )
        if plausible.all():
            return dni.fillna(0.0), dhi.fillna(0.0), plausible

        derived_dni, derived_dhi = self.decompose(ghi, solar_position, index)
        return (
            dni.where(plausible, derived_dni).fillna(0.0),
            dhi.where(plausible, derived_dhi).fillna(0.0),
            plausible,
        )

    # -- transposition and conversion -------------------------------------- #

    def _transposition_model(self, components_ok: bool) -> str:
        """Fall back to Hay-Davies when the components are not trustworthy.

        Perez-Driesse leans hard on the circumsolar and horizon-brightening
        terms, which are exactly what a bad DNI/DHI split gets wrong.
        """
        if self.transposition_model == "perez-driesse" and not components_ok:
            return "haydavies"
        return self.transposition_model

    def poa(
        self,
        geometry: GeometrySegment,
        solar_position: pd.DataFrame,
        ghi: pd.Series,
        dni: pd.Series,
        dhi: pd.Series,
        dni_extra: pd.Series | None = None,
        airmass: pd.Series | None = None,
        components_ok: bool = True,
    ) -> pd.DataFrame:
        model = self._transposition_model(components_ok)
        kwargs: dict[str, object] = {}
        if model in ("haydavies", "perez", "perez-driesse", "reindl", "klucher"):
            kwargs["dni_extra"] = dni_extra
        if model in ("perez", "perez-driesse"):
            kwargs["airmass"] = airmass
        return pvlib.irradiance.get_total_irradiance(
            surface_tilt=geometry.tilt_deg,
            surface_azimuth=geometry.azimuth_deg,
            solar_zenith=solar_position["apparent_zenith"],
            solar_azimuth=solar_position["azimuth"],
            dni=dni,
            ghi=ghi,
            dhi=dhi,
            albedo=self.albedo,
            model=model,
            **kwargs,
        )

    def run(
        self,
        index: pd.DatetimeIndex,
        geometry: GeometrySegment,
        ghi: pd.Series,
        dni: pd.Series | None = None,
        dhi: pd.Series | None = None,
        temp_air: pd.Series | float = 20.0,
        wind_speed: pd.Series | float = 1.0,
        system_efficiency: float = 0.90,
        mount_type: str = "insulated_back",
        shading_factor: pd.Series | float = 1.0,
    ) -> StringResult:
        """Full chain for one string over one time index.

        ``index`` must already be the interval **midpoints**.
        """
        solar_position = self.solar_position(index)
        dni_ok, dhi_ok, plausible = self.ensure_components(
            ghi, dni, dhi, solar_position, index
        )
        components_ok = bool(plausible.all())

        dni_extra = pvlib.irradiance.get_extra_radiation(index)
        airmass = self.location.get_airmass(
            solar_position=solar_position
        )["airmass_relative"]

        poa = self.poa(
            geometry,
            solar_position,
            ghi.fillna(0.0),
            dni_ok,
            dhi_ok,
            dni_extra=dni_extra,
            airmass=airmass,
            components_ok=components_ok,
        )

        aoi = pvlib.irradiance.aoi(
            geometry.tilt_deg,
            geometry.azimuth_deg,
            solar_position["apparent_zenith"],
            solar_position["azimuth"],
        )
        # Reflection losses matter most at large incidence angles -- precisely
        # the regime where a wrong tilt shows up, so it must not be swallowed.
        iam = pvlib.iam.ashrae(aoi).fillna(0.0)
        effective = (poa["poa_direct"] * iam + poa["poa_diffuse"]).clip(lower=0.0)
        effective = effective * shading_factor

        params = TEMPERATURE_MODEL_PARAMETERS["sapm"][
            MOUNT_TYPES.get(mount_type, MOUNT_TYPES["insulated_back"])
        ]
        cell_temp = pvlib.temperature.sapm_cell(
            poa_global=poa["poa_global"].clip(lower=0.0),
            temp_air=temp_air,
            wind_speed=wind_speed,
            **params,
        )

        dc = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective=effective,
            temp_cell=cell_temp,
            pdc0=geometry.kwp * 1000.0,
            gamma_pdc=geometry.temp_coeff,
        ).clip(lower=0.0)

        dc = dc * system_efficiency
        # Never promise more than the modules can physically deliver.
        dc = dc.clip(upper=geometry.kwp * 1000.0)

        return StringResult(
            index=index,
            dc_power_w=dc.fillna(0.0),
            poa_global=poa["poa_global"].fillna(0.0),
            cell_temp_c=cell_temp,
            aoi_deg=aoi,
        )

    # -- helpers used by the forecast orchestrator ------------------------- #

    def clearsky_index(
        self, index: pd.DatetimeIndex, ghi: pd.Series, floor_wm2: float = 5.0
    ) -> pd.Series:
        """Measured or forecast GHI over clear-sky GHI.

        Returns ``NaN`` below ``floor_wm2`` of clear-sky irradiance -- at night
        the ratio is genuinely undefined, and zero would read as "overcast" to
        every consumer of this series.  Callers must handle the gap explicitly.
        """
        clear = self.clearsky(index)["ghi"]
        usable = clear.where(clear > floor_wm2)
        return (ghi / usable).clip(lower=0.0, upper=1.3)

    def monthly_clearsky_share(
        self, tilt_deg: float, azimuth_deg: float, year: int = 2021
    ) -> list[float]:
        """Share of the annual clear-sky plane-of-array yield per month.

        Used to extrapolate a partial year of savings without the classic
        "measured since April, times 365" error, which in mid latitudes
        overstates the year badly because April to August carries most of it.
        Deriving the weights from the site's own geometry keeps this correct for
        any latitude instead of baking in numbers for one country.
        """
        index = pd.date_range(
            f"{year}-01-01", f"{year}-12-31 23:00", freq="h", tz="UTC"
        )
        solar_position = self.solar_position(index)
        clear = self.clearsky(index, solar_position=solar_position)
        dni_extra = pvlib.irradiance.get_extra_radiation(index)
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=tilt_deg,
            surface_azimuth=azimuth_deg,
            solar_zenith=solar_position["apparent_zenith"],
            solar_azimuth=solar_position["azimuth"],
            dni=clear["dni"],
            ghi=clear["ghi"],
            dhi=clear["dhi"],
            albedo=self.albedo,
            model="haydavies",
            dni_extra=dni_extra,
        )["poa_global"].clip(lower=0.0)
        monthly = poa.groupby(poa.index.month).sum()
        total = float(monthly.sum())
        if total <= 0:
            return [1.0 / 12.0] * 12
        return [float(monthly.get(month, 0.0)) / total for month in range(1, 13)]
