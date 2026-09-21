"""The physics chain.

These assertions are about behaviour a wrong implementation would break, not
about pvlib's own numbers: orientation ordering, temperature derating, the
component closure test, and the interval-midpoint rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pvlib
import pytest

from core.config import GeometrySegment
from core.learning import daypart
from core.physics import PhysicsEngine, clamp_to_daylight, to_index

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
    def test_offset_is_near_zero_at_local_apparent_noon(self, engine: PhysicsEngine):
        # 10 deg east -> solar noon a bit before 12:00 UTC, so 12:00 UTC sits
        # just after it.
        offset = engine.hours_from_solar_noon(SUMMER_NOON)
        assert 0.0 < offset < 1.0

    def test_offset_advances_within_the_day(self, engine: PhysicsEngine):
        # The old noon lookup was cached per day and returned the same value
        # all day; the offset must not.
        first = engine.hours_from_solar_noon(SUMMER_NOON)
        second = engine.hours_from_solar_noon(SUMMER_NOON + 1800)
        assert second == pytest.approx(first + 0.5, abs=1e-3)

    def test_sun_is_below_horizon_at_midnight(self, engine: PhysicsEngine):
        index = to_index([SUMMER_NOON - 12 * 3600])
        elevation = engine.solar_position(index)["apparent_elevation"].iloc[0]
        assert elevation < 0


#: 21 December, 12:00 UTC -- the short end of the year.
WINTER_NOON = SUMMER_NOON + 183 * 86400


class TestDaylightWindow:
    def test_summer_window_brackets_solar_noon(self, engine: PhysicsEngine):
        window = engine.daylight_window_for(SUMMER_NOON)
        assert window is not None
        sunrise, sunset = window
        assert sunrise < SUMMER_NOON < sunset
        # ~17 hours of daylight at 53.5 deg north in June.
        assert 15 * 3600 < sunset - sunrise < 19 * 3600

    def test_winter_days_are_short(self, engine: PhysicsEngine):
        window = engine.daylight_window_for(WINTER_NOON)
        assert window is not None
        sunrise, sunset = window
        assert 6 * 3600 < sunset - sunrise < 9 * 3600

    def test_window_is_cached_per_day(self, engine: PhysicsEngine):
        assert engine.daylight_window_for(SUMMER_NOON) == engine.daylight_window_for(
            SUMMER_NOON + 1800
        )

    def test_polar_night_and_day_have_no_window(self):
        svalbard = PhysicsEngine(78.0, 15.0)
        assert svalbard.daylight_window_for(WINTER_NOON) is None
        assert svalbard.daylight_window_for(SUMMER_NOON) is None


class TestClampToDaylight:
    WINDOW = (6 * 3600.0, 20 * 3600.0)  # sunrise 06:00, sunset 20:00

    def test_night_edges_are_cut(self):
        assert clamp_to_daylight(0, 86400, self.WINDOW) == (6 * 3600, 20 * 3600)

    def test_midday_now_caps_the_end(self):
        assert clamp_to_daylight(0, 12 * 3600, self.WINDOW) == (6 * 3600, 12 * 3600)

    def test_before_sunrise_the_window_is_empty(self):
        start, end = clamp_to_daylight(0, 5 * 3600, self.WINDOW)
        assert end <= start

    def test_no_window_means_no_clamp(self):
        assert clamp_to_daylight(0, 86400, None) == (0, 86400)


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


class TestBeamScope:
    """scope="beam": the shadow takes only the POA direct component."""

    def _run(self, engine: PhysicsEngine, geometry: GeometrySegment, ts=SUMMER_NOON, **kw):
        index = to_index([ts])
        ghi, dni, dhi = _clear_conditions(engine, index)
        return engine.run(index, geometry, ghi=ghi, dni=dni, dhi=dhi, **kw)

    def test_diffuse_only_light_is_untouched(self, engine: PhysicsEngine):
        # dni=0, ghi=dhi: an overcast moment.  A beam shadow costs nothing.
        index = to_index([SUMMER_NOON])
        _ghi, dni, dhi = _clear_conditions(engine, index)
        geometry = GeometrySegment(0, 180, 30, 1.0)
        clear = engine.run(
            index, geometry, ghi=dhi, dni=dni * 0.0, dhi=dhi,
            shading_scope="beam",
        )
        shaded = engine.run(
            index, geometry, ghi=dhi, dni=dni * 0.0, dhi=dhi,
            shading_factor=0.5, shading_scope="beam",
        )
        assert clear.dc_power_w.iloc[0] > 0
        assert shaded.dc_power_w.iloc[0] == pytest.approx(
            clear.dc_power_w.iloc[0], rel=1e-6
        )

    def test_total_darkness_still_leaves_the_diffuse_floor(self, engine: PhysicsEngine):
        geometry = GeometrySegment(0, 180, 30, 1.0)
        clear = self._run(engine, geometry, shading_scope="beam")
        beam_black = self._run(
            engine, geometry, shading_factor=0.0, shading_scope="beam"
        )
        total_black = self._run(engine, geometry, shading_factor=0.0)
        assert total_black.dc_power_w.iloc[0] == pytest.approx(0.0)
        assert 0.0 < beam_black.dc_power_w.iloc[0] < clear.dc_power_w.iloc[0]

    def test_applied_ratio_recovers_the_unshaded_power(self, engine: PhysicsEngine):
        geometry = GeometrySegment(0, 180, 30, 1.0)
        clear = self._run(engine, geometry, shading_scope="beam")
        shaded = self._run(
            engine, geometry, shading_factor=0.4, shading_scope="beam"
        )
        applied = shaded.shading_applied.iloc[0]
        assert 0.4 < applied < 1.0
        assert shaded.dc_power_w.iloc[0] == pytest.approx(
            clear.dc_power_w.iloc[0] * applied, rel=1e-6
        )

    def test_beam_share_is_bounded_and_dark_at_night(self, engine: PhysicsEngine):
        noon = self._run(engine, GeometrySegment(0, 180, 30, 1.0))
        assert 0.7 <= noon.beam_share.iloc[0] <= 1.0
        night = self._run(
            engine, GeometrySegment(0, 180, 30, 1.0), ts=SUMMER_NOON - 12 * 3600
        )
        assert night.beam_share.iloc[0] == pytest.approx(0.0)
        assert night.shading_applied.iloc[0] == pytest.approx(1.0)

    def test_an_east_panel_has_no_beam_to_lose_in_the_afternoon(self, engine: PhysicsEngine):
        # The v1.18 failure case: horizontal beam share was high while the
        # panel's own plane saw none.
        afternoon = SUMMER_NOON + 5 * 3600
        geometry = GeometrySegment(0, 90, 35, 1.0)
        clear = self._run(engine, geometry, ts=afternoon, shading_scope="beam")
        assert clear.beam_share.iloc[0] < 0.15
        shaded = self._run(
            engine, geometry, ts=afternoon, shading_factor=0.05,
            shading_scope="beam",
        )
        assert shaded.dc_power_w.iloc[0] == pytest.approx(
            clear.dc_power_w.iloc[0], rel=0.16
        )

    def test_total_scope_is_unchanged_default(self, engine: PhysicsEngine):
        geometry = GeometrySegment(0, 180, 30, 1.0)
        default = self._run(engine, geometry, shading_factor=0.3)
        explicit = self._run(
            engine, geometry, shading_factor=0.3, shading_scope="total"
        )
        assert default.dc_power_w.iloc[0] == pytest.approx(
            explicit.dc_power_w.iloc[0]
        )
        assert default.shading_applied.iloc[0] == pytest.approx(0.3)


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


class TestMissingComponentsAreNotPlausibleComponents:
    """Present and plausible are two different questions.

    ``components_plausible`` is a closure test, and a closure test on a missing
    value cannot fail -- so it answers "I found nothing wrong", which is not
    the same as "this is usable".  Taken at face value the missing components
    reached ``fillna(0.0)`` and became a hard zero: a plant standing in
    640 W/m2 modelled with no beam and no diffuse light, only ground
    reflection.  The physics came out around a hundredth of the truth, every
    measured-versus-physics ratio blew past the sanity bound, and the learning
    stopped -- on exactly the installations that had fitted an irradiance
    sensor, because that is the path that blanks the components.
    """

    GHI = 643.0

    def _engine(self) -> PhysicsEngine:
        return PhysicsEngine(
            latitude=53.7,
            longitude=10.0,
            elevation_m=20.0,
            albedo=0.2,
            transposition_model="perez-driesse",
            time_zone="Europe/Berlin",
        )

    def _midday(self):
        engine = self._engine()
        index = to_index([1_755_000_000 + step * 300 for step in range(6)])
        return engine, index, engine.solar_position(index)

    def _series(self, index, value):
        return pd.Series([value] * len(index), index=index)

    def test_nan_components_are_decomposed_not_zeroed(self):
        engine, index, position = self._midday()
        ghi = self._series(index, self.GHI)
        blank = self._series(index, float("nan"))
        dni, dhi, _ = engine.ensure_components(ghi, blank, blank, position, index)
        assert dni.iloc[0] > 100.0
        assert dhi.iloc[0] > 10.0

    def test_nan_matches_an_absent_series(self):
        """Blanking the components must behave exactly like never having them."""
        engine, index, position = self._midday()
        ghi = self._series(index, self.GHI)
        blank = self._series(index, float("nan"))
        by_nan = engine.ensure_components(ghi, blank, blank, position, index)
        by_none = engine.ensure_components(ghi, None, None, position, index)
        assert by_nan[0].round(6).equals(by_none[0].round(6))
        assert by_nan[1].round(6).equals(by_none[1].round(6))

    def test_nan_components_are_never_reported_plausible(self):
        engine, index, position = self._midday()
        ghi = self._series(index, self.GHI)
        blank = self._series(index, float("nan"))
        _dni, _dhi, plausible = engine.ensure_components(
            ghi, blank, blank, position, index
        )
        assert not plausible.any()

    def test_one_missing_interval_does_not_discard_the_others(self):
        engine, index, position = self._midday()
        ghi = self._series(index, self.GHI)
        dni = self._series(index, 515.0)
        dhi = self._series(index, 245.0)
        dni.iloc[2] = float("nan")
        out_dni, _out_dhi, plausible = engine.ensure_components(
            ghi, dni, dhi, position, index
        )
        assert out_dni.iloc[0] == pytest.approx(515.0)
        assert out_dni.iloc[2] > 100.0  # derived, not zero
        assert not plausible.iloc[2]
        assert plausible.iloc[0]

    def test_good_components_still_pass_through(self):
        engine, index, position = self._midday()
        ghi = self._series(index, self.GHI)
        zenith = np.radians(position["apparent_zenith"])
        dhi = self._series(index, 245.0)
        dni = (ghi - dhi) / np.cos(zenith).clip(lower=0.01)
        out_dni, out_dhi, plausible = engine.ensure_components(
            ghi, dni, dhi, position, index
        )
        assert plausible.all()
        assert out_dni.iloc[0] == pytest.approx(dni.iloc[0])
        assert out_dhi.iloc[0] == pytest.approx(245.0)

    def test_a_real_array_produces_real_power_from_a_measured_ghi(self):
        """The end-to-end symptom: 1.4 kWh became 0.014 kWh."""
        engine, index, _position = self._midday()
        ghi = self._series(index, self.GHI)
        blank = self._series(index, float("nan"))
        segment = GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=1.8)
        result = engine.run(index, segment, ghi=ghi, dni=blank, dhi=blank)
        # A 1.8 kWp south-facing array under 643 W/m2 makes hundreds of watts,
        # not the ~14 W that ground reflection alone would give.
        assert result.dc_power_w.iloc[0] > 400.0


class TestHoursFromSolarNoon:
    """The daypart offset, and the two ways of computing it that were wrong.

    Both failures were invisible in Europe and load-bearing everywhere else, so
    these cases name their sites: a regression here is a regression for someone
    whose plant nobody in this repo can look at.
    """

    #: Equator on the antimeridian, half an hour after solar noon.  The SPA's
    #: transit dates jump either side of UTC midnight here, so resolving a noon
    #: by calendar day -- whether the timestamp's own day or the nearest of
    #: three -- lands almost a day out and buckets this as morning.  The sun
    #: stands at 79 degrees.
    ANTIMERIDIAN_TS = 1788827400.0  # 2026-09-02 00:30 UTC

    @pytest.mark.parametrize("longitude", [180.0, -180.0])
    def test_antimeridian_noon_is_midday(self, longitude: float):
        engine = PhysicsEngine(latitude=0.0, longitude=longitude)
        offset = engine.hours_from_solar_noon(self.ANTIMERIDIAN_TS)
        assert offset == pytest.approx(0.5, abs=0.05)
        assert daypart(offset) == "midday"

    def test_both_antimeridian_signs_agree(self):
        east = PhysicsEngine(latitude=0.0, longitude=180.0)
        west = PhysicsEngine(latitude=0.0, longitude=-180.0)
        assert east.hours_from_solar_noon(
            self.ANTIMERIDIAN_TS
        ) == pytest.approx(west.hours_from_solar_noon(self.ANTIMERIDIAN_TS), abs=1e-9)

    @pytest.mark.parametrize(
        "name,latitude,longitude,zone",
        [
            # East of the date line's reach: the local morning carries
            # yesterday's UTC date.  These plants had no morning bucket at all.
            ("sydney", -33.87, 151.21, "Australia/Sydney"),
            ("auckland", -36.85, 174.76, "Pacific/Auckland"),
            ("fiji", -18.14, 178.44, "Pacific/Fiji"),
            ("tokyo", 35.68, 139.69, "Asia/Tokyo"),
            # West: the local evening carries tomorrow's, and was learned as
            # morning -- the worse case, because the bucket looked populated.
            ("los_angeles", 34.05, -118.24, "America/Los_Angeles"),
            ("anchorage", 61.22, -149.90, "America/Anchorage"),
            # Never affected, and must stay that way.
            ("berlin", 53.60, 9.90, "Europe/Berlin"),
            ("new_york", 40.71, -74.01, "America/New_York"),
        ],
    )
    def test_dayparts_run_in_order_over_local_daylight(
        self, name: str, latitude: float, longitude: float, zone: str
    ):
        """Morning, then midday, then afternoon, by the local clock.

        Daylight hours only, and away from the solstices: under the polar day
        daylight spans solar midnight, where afternoon correctly wraps back to
        morning.  That seam is tested separately.
        """
        tz = ZoneInfo(zone)
        engine = PhysicsEngine(latitude=latitude, longitude=longitude)
        seen: dict[str, list[int]] = {}
        for hour in range(6, 19):
            ts = (
                datetime(2026, 9, 21, hour, tzinfo=tz).timestamp() + 1800
            )
            seen.setdefault(daypart(engine.hours_from_solar_noon(ts)), []).append(hour)

        assert set(seen) == {"morning", "midday", "afternoon"}, f"{name}: {seen}"
        assert max(seen["morning"]) < min(seen["midday"]), f"{name}: {seen}"
        assert max(seen["midday"]) < min(seen["afternoon"]), f"{name}: {seen}"

    def test_offset_stays_within_half_a_day_across_longitudes(self):
        """The invariant the calendar-day lookup violated by up to 22 hours."""
        for longitude in range(-180, 181, 15):
            engine = PhysicsEngine(latitude=0.0, longitude=float(longitude))
            for hour in range(0, 24, 3):
                ts = datetime(
                    2026, 3, 11, hour, tzinfo=timezone.utc
                ).timestamp()
                assert -12.0 <= engine.hours_from_solar_noon(ts) < 12.0

    def test_batch_matches_one_at_a_time(self, engine: PhysicsEngine):
        stamps = [SUMMER_NOON + n * 1800 for n in range(12)]
        batch = engine.hours_from_solar_noon_many(stamps)
        # Length first: zip() over a truncated result silently checks only the
        # rows that survived, and a batch that returns one row for twelve
        # timestamps would pass every other assertion here.
        assert len(batch) == len(stamps)
        for ts, value in zip(stamps, batch):
            assert float(value) == pytest.approx(
                engine.hours_from_solar_noon(ts), abs=1e-9
            )

    def test_batch_preserves_order_and_duplicates(self, engine: PhysicsEngine):
        stamps = [
            SUMMER_NOON + 3600,
            SUMMER_NOON - 7200,
            SUMMER_NOON + 3600,
            SUMMER_NOON,
        ]
        batch = engine.hours_from_solar_noon_many(stamps)
        assert len(batch) == len(stamps)
        assert float(batch[0]) == pytest.approx(float(batch[2]), abs=1e-9)
        assert float(batch[1]) < float(batch[3]) < float(batch[0])

    @pytest.mark.parametrize(
        "when,label",
        [
            (datetime(2026, 11, 3, 12, tzinfo=timezone.utc), "equation of time at +16 min"),
            (datetime(2026, 2, 11, 12, tzinfo=timezone.utc), "equation of time at -14 min"),
            (datetime(2026, 6, 14, 12, tzinfo=timezone.utc), "equation of time near zero"),
        ],
    )
    def test_offset_agrees_with_the_spa_transit(self, when: datetime, label: str):
        """Cross-check against the SPA, at the two turning points of the year.

        Berlin, far from UTC midnight, is where resolving a transit by calendar
        day happens to be right -- so the SPA is a valid second opinion here and
        the two must agree to within a minute.  Dropping the equation-of-time
        term still passes every other test in this class while moving the
        bucket edges by up to a quarter of an hour; this is the test that
        notices.
        """
        engine = PhysicsEngine(latitude=53.60, longitude=9.90)
        ts = when.timestamp()
        day = pd.DatetimeIndex([pd.Timestamp(when.date())]).tz_localize("UTC")
        transit = pvlib.solarposition.sun_rise_set_transit_spa(
            day, engine.latitude, engine.longitude
        )["transit"].iloc[0]
        from_spa = (ts - transit.timestamp()) / 3600.0
        assert engine.hours_from_solar_noon(ts) == pytest.approx(
            from_spa, abs=60 / 3600
        ), label
