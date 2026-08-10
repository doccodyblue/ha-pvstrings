"""The physics chain.

These assertions are about behaviour a wrong implementation would break, not
about pvlib's own numbers: orientation ordering, temperature derating, the
component closure test, and the interval-midpoint rule.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.config import GeometrySegment
from core.physics import PhysicsEngine, to_index

LAT, LON = 53.5, 10.0

#: 21 June, 12:00 UTC -- high sun, unambiguous south preference.
SUMMER_NOON = 1_750_507_200


@pytest.fixture
def engine() -> PhysicsEngine:
    return PhysicsEngine(LAT, LON, elevation_m=5.0, time_zone="Europe/Berlin")


def _clear_conditions(engine: PhysicsEngine, index: pd.DatetimeIndex):
    """Use the clear-sky model itself as the irradiance input."""
    solar_position = engine.solar_position(index)
    clear = engine.clearsky(index, solar_position=solar_position)
    return clear["ghi"], clear["dni"], clear["dhi"]


class TestSolarGeometry:
    def test_solar_noon_is_near_local_apparent_noon(self, engine: PhysicsEngine):
        noon = engine.solar_noon_for(SUMMER_NOON)
        # 10 deg east -> solar noon a bit before 12:00 UTC.
        assert SUMMER_NOON - 3600 < noon < SUMMER_NOON

    def test_solar_noon_is_cached_per_day(self, engine: PhysicsEngine):
        first = engine.solar_noon_for(SUMMER_NOON)
        second = engine.solar_noon_for(SUMMER_NOON + 1800)
        assert first == second

    def test_sun_is_below_horizon_at_midnight(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON - 12 * 3600])
        elevation = engine.solar_position(index)["apparent_elevation"].iloc[0]
        assert elevation < 0


class TestComponentPlausibility:
    def test_closing_components_pass(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON])
        ghi, dni, dhi = _clear_conditions(engine, index)
        solar_position = engine.solar_position(index)
        assert engine.components_plausible(ghi, dni, dhi, solar_position).all()

    def test_broken_components_are_detected(self, engine: PhysicsEngine):
        """Several free sources ship a GHI/DNI/DHI triple that does not close.
        Using it anyway silently corrupts the transposition."""
        index = to_index([SUMMER_NOON])
        ghi, dni, dhi = _clear_conditions(engine, index)
        assert not engine.components_plausible(
            ghi, dni * 0.2, dhi * 0.2, engine.solar_position(index)
        ).all()

    def test_broken_components_are_replaced_by_a_decomposition(
        self, engine: PhysicsEngine
    ):
        index = to_index([SUMMER_NOON])
        ghi, dni, dhi = _clear_conditions(engine, index)
        fixed_dni, fixed_dhi, plausible = engine.ensure_components(
            ghi, dni * 0.2, dhi * 0.2, engine.solar_position(index), index
        )
        assert not plausible.all()
        assert fixed_dni.iloc[0] > (dni * 0.2).iloc[0]

    def test_missing_components_are_derived_from_ghi(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON])
        ghi, _dni, _dhi = _clear_conditions(engine, index)
        dni, dhi, plausible = engine.ensure_components(
            ghi, None, None, engine.solar_position(index), index
        )
        assert dni.iloc[0] > 0
        assert dhi.iloc[0] > 0
        assert not plausible.any()

    def test_low_sun_is_exempt_from_the_closure_test(self, engine: PhysicsEngine):
        """Huge air mass, tiny signal -- the ratio is meaningless there."""
        index = to_index([SUMMER_NOON - 8 * 3600])
        ghi, dni, dhi = _clear_conditions(engine, index)
        solar_position = engine.solar_position(index)
        if solar_position["apparent_elevation"].iloc[0] < 5.0:
            assert engine.components_plausible(
                ghi, dni * 0.1, dhi * 0.1, solar_position
            ).all()


class TestChain:
    def _run(self, engine: PhysicsEngine, geometry: GeometrySegment, ts=SUMMER_NOON, **kw):
        index = to_index([ts])
        ghi, dni, dhi = _clear_conditions(engine, index)
        return engine.run(index, geometry, ghi=ghi, dni=dni, dhi=dhi, **kw)

    def test_south_beats_north_at_noon(self, engine: PhysicsEngine):
        south = self._run(engine, GeometrySegment(0, 180, 30, 1.0))
        north = self._run(engine, GeometrySegment(0, 0, 30, 1.0))
        assert south.dc_power_w.iloc[0] > north.dc_power_w.iloc[0]

    def test_east_leads_in_the_morning(self, engine: PhysicsEngine):
        morning = SUMMER_NOON - 5 * 3600
        east = self._run(engine, GeometrySegment(0, 90, 30, 1.0), ts=morning)
        west = self._run(engine, GeometrySegment(0, 270, 30, 1.0), ts=morning)
        assert east.dc_power_w.iloc[0] > west.dc_power_w.iloc[0]

    def test_output_never_exceeds_nameplate(self, engine: PhysicsEngine):
        result = self._run(engine, GeometrySegment(0, 180, 35, 1.0))
        assert result.dc_power_w.iloc[0] <= 1000.0

    def test_night_yields_nothing(self, engine: PhysicsEngine):
        result = self._run(engine, GeometrySegment(0, 180, 30, 1.0), ts=SUMMER_NOON - 12 * 3600)
        assert result.dc_power_w.iloc[0] == pytest.approx(0.0)

    def test_heat_reduces_output(self, engine: PhysicsEngine):
        cool = self._run(engine, GeometrySegment(0, 180, 30, 1.0), temp_air=5.0)
        hot = self._run(engine, GeometrySegment(0, 180, 30, 1.0), temp_air=35.0)
        assert hot.dc_power_w.iloc[0] < cool.dc_power_w.iloc[0]

    def test_wind_cools_the_cells(self, engine: PhysicsEngine):
        still = self._run(
            engine, GeometrySegment(0, 180, 30, 1.0), temp_air=30.0, wind_speed=0.5,
            mount_type="open_rack",
        )
        breezy = self._run(
            engine, GeometrySegment(0, 180, 30, 1.0), temp_air=30.0, wind_speed=8.0,
            mount_type="open_rack",
        )
        assert breezy.cell_temp_c.iloc[0] < still.cell_temp_c.iloc[0]
        assert breezy.dc_power_w.iloc[0] > still.dc_power_w.iloc[0]

    def test_system_efficiency_scales_linearly(self, engine: PhysicsEngine):
        full = self._run(engine, GeometrySegment(0, 180, 30, 1.0), system_efficiency=1.0)
        derated = self._run(
            engine, GeometrySegment(0, 180, 30, 1.0), system_efficiency=0.5
        )
        assert derated.dc_power_w.iloc[0] == pytest.approx(
            full.dc_power_w.iloc[0] * 0.5, rel=1e-6
        )

    def test_nameplate_scales_output(self, engine: PhysicsEngine):
        small = self._run(engine, GeometrySegment(0, 180, 30, 1.0))
        large = self._run(engine, GeometrySegment(0, 180, 30, 2.0))
        assert large.dc_power_w.iloc[0] > small.dc_power_w.iloc[0] * 1.9

    def test_shading_factor_attenuates(self, engine: PhysicsEngine):
        clear = self._run(engine, GeometrySegment(0, 180, 30, 1.0))
        shaded = self._run(
            engine, GeometrySegment(0, 180, 30, 1.0), shading_factor=0.3
        )
        assert shaded.dc_power_w.iloc[0] < clear.dc_power_w.iloc[0]


class TestTiltError:
    """The scenario from the spec: 60 deg vs 70 deg on a south-facing string.

    The point is not the exact percentage but that the error is *not constant*
    -- it is large in summer and small in winter, which is exactly what makes a
    fixed wrong value look like a weather or shading effect to the learner.
    """

    def _daily_kwh(self, engine: PhysicsEngine, tilt: float, day_start: int) -> float:
        stamps = [day_start + step * 1800 + 900 for step in range(48)]
        index = to_index(stamps)
        ghi, dni, dhi = _clear_conditions(engine, index)
        result = engine.run(
            index, GeometrySegment(0, 180, tilt, 1.0), ghi=ghi, dni=dni, dhi=dhi
        )
        return float(result.dc_power_w.sum()) * 1800 / 3600 / 1000

    def test_error_is_seasonal_not_constant(self, engine: PhysicsEngine):
        summer_day = SUMMER_NOON - 12 * 3600
        winter_day = summer_day + 183 * 86400

        summer_gap = abs(
            self._daily_kwh(engine, 60, summer_day)
            - self._daily_kwh(engine, 70, summer_day)
        ) / self._daily_kwh(engine, 60, summer_day)
        winter_gap = abs(
            self._daily_kwh(engine, 60, winter_day)
            - self._daily_kwh(engine, 70, winter_day)
        ) / self._daily_kwh(engine, 60, winter_day)

        assert summer_gap > winter_gap * 2


class TestIntervalMidpoint:
    def test_start_of_interval_differs_from_midpoint_near_sunrise(
        self, engine: PhysicsEngine
    ):
        """Why solar position must be evaluated at the interval midpoint."""
        sunrise_ish = SUMMER_NOON - 7 * 3600
        geometry = GeometrySegment(0, 90, 30, 1.0)
        ghi_start, dni_start, dhi_start = _clear_conditions(
            engine, to_index([sunrise_ish])
        )
        at_start = engine.run(
            to_index([sunrise_ish]), geometry, ghi=ghi_start, dni=dni_start, dhi=dhi_start
        ).dc_power_w.iloc[0]

        mid = sunrise_ish + 150
        ghi_mid, dni_mid, dhi_mid = _clear_conditions(engine, to_index([mid]))
        at_mid = engine.run(
            to_index([mid]), geometry, ghi=ghi_mid, dni=dni_mid, dhi=dhi_mid
        ).dc_power_w.iloc[0]

        assert at_mid != pytest.approx(at_start, rel=1e-3)


class TestSeasonality:
    def test_monthly_shares_sum_to_one(self, engine: PhysicsEngine):
        weights = engine.monthly_clearsky_share(30, 180)
        assert sum(weights) == pytest.approx(1.0)
        assert len(weights) == 12

    def test_summer_dominates_at_northern_latitudes(self, engine: PhysicsEngine):
        weights = engine.monthly_clearsky_share(30, 180)
        summer = sum(weights[3:8])  # April..August
        assert summer > 0.5

    def test_a_steep_panel_flattens_the_season(self, engine: PhysicsEngine):
        flat = engine.monthly_clearsky_share(10, 180)
        steep = engine.monthly_clearsky_share(70, 180)
        assert sum(steep[3:8]) < sum(flat[3:8])


def test_clearsky_index_is_one_under_clear_sky(engine: PhysicsEngine):
    index = to_index([SUMMER_NOON])
    ghi, _dni, _dhi = _clear_conditions(engine, index)
    assert engine.clearsky_index(index, ghi).iloc[0] == pytest.approx(1.0, abs=1e-6)


def test_clearsky_index_is_undefined_at_night(engine: PhysicsEngine):
    """Not zero -- zero would read as "overcast" to every consumer."""
    index = to_index([SUMMER_NOON - 12 * 3600])
    ghi = pd.Series([0.0], index=index)
    assert np.isnan(engine.clearsky_index(index, ghi).iloc[0])


class TestTurbidityFallback:
    """The gridded Linke turbidity needs h5py and a 2160x4320x12 HDF5 grid.

    That is a heavy native dependency for a Home Assistant container to
    satisfy.  If it is unavailable the clear-sky model must degrade, not take
    the whole integration down.
    """

    def test_lookup_is_used_when_available(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON])
        assert engine.linke_turbidity(index).iloc[0] > 0
        assert engine._turbidity_lookup_ok is True

    def test_fallback_keeps_clearsky_usable(self, engine: PhysicsEngine, monkeypatch):
        import pvlib

        monkeypatch.setattr(
            pvlib.clearsky,
            "lookup_linke_turbidity",
            lambda *a, **kw: (_ for _ in ()).throw(ImportError("no h5py")),
        )
        index = to_index([SUMMER_NOON])
        clear = engine.clearsky(index)

        assert engine._turbidity_lookup_ok is False
        assert 700 < clear["ghi"].iloc[0] < 1100

    def test_fallback_stays_within_a_plausible_band(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON + day * 86400 for day in range(0, 365, 15)])
        values = engine._fallback_turbidity(index)
        assert values.min() > 1.5
        assert values.max() < 6.0

    def test_fallback_peaks_in_summer(self, engine: PhysicsEngine):
        summer = engine._fallback_turbidity(to_index([SUMMER_NOON])).iloc[0]
        winter = engine._fallback_turbidity(
            to_index([SUMMER_NOON + 183 * 86400])
        ).iloc[0]
        assert summer > winter

    def test_fallback_is_close_enough_to_the_lookup(self, engine: PhysicsEngine):
        """Not equal -- but the same order, so the forecast stays sane."""
        index = to_index([SUMMER_NOON])
        looked_up = float(engine.linke_turbidity(index).iloc[0])
        approximated = float(engine._fallback_turbidity(index).iloc[0])
        assert abs(looked_up - approximated) < 1.5

    def test_warning_is_logged_only_once(self, engine: PhysicsEngine, monkeypatch, caplog):
        import pvlib

        monkeypatch.setattr(
            pvlib.clearsky,
            "lookup_linke_turbidity",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("missing file")),
        )
        index = to_index([SUMMER_NOON])
        with caplog.at_level("WARNING"):
            engine.clearsky(index)
            engine.clearsky(index)
        assert sum("turbidity" in r.message for r in caplog.records) == 1
