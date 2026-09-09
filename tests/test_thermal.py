"""Heat is reported, not hidden.

The physics has always run the cells hot; nothing said what that cost.  Each
forecast hour now carries its physics over the same physics with the cells held
at 25 degC, and the cell temperature that explains the gap.
"""

from __future__ import annotations

import pandas as pd
import pytest
from core.aggregate import thermal_loss_kwh
from core.config import GeometrySegment, PlantConfig
from core.forecast import ForecastEngine
from core.physics import PhysicsEngine, to_index
from core.store import Store
from test_forecast_engine import DAY_START, clear_sky_forecast
from test_measured_air import NOON, with_air, write_air

HOUR = 3600
#: A summer noon at the reference site, with real sun on a south array.
SUMMER_NOON_UTC = 1750507200


class TestPhysicsReference:
    def _run(
        self,
        temp_air: float,
        wind_speed: float = 1.0,
        kwp: float = 1.0,
        ghi_wm2: float = 800.0,
        system_efficiency: float = 0.9,
    ):
        engine = PhysicsEngine(53.5, 10.0)
        index = to_index([SUMMER_NOON_UTC])
        ghi = pd.Series([ghi_wm2], index=index)
        return engine.run(
            index,
            GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=kwp),
            ghi=ghi,
            temp_air=temp_air,
            wind_speed=wind_speed,
            system_efficiency=system_efficiency,
            mount_type="open_rack",
        )

    def test_a_hot_still_noon_runs_below_the_reference(self):
        result = self._run(temp_air=35.0, wind_speed=0.5)
        assert result.stc_power_w.iloc[0] > result.dc_power_w.iloc[0]
        assert result.cell_temp_c.iloc[0] > 25.0

    def test_a_cold_windy_noon_runs_above_it(self):
        result = self._run(temp_air=-10.0, wind_speed=8.0)
        assert result.dc_power_w.iloc[0] > result.stc_power_w.iloc[0]
        assert result.cell_temp_c.iloc[0] < 25.0

    def test_the_reference_respects_the_nameplate_too(self):
        """A cold bright interval clips both curves, not only the hot one."""
        result = self._run(
            temp_air=-10.0, wind_speed=8.0, kwp=0.1, ghi_wm2=1000.0,
            system_efficiency=1.0,
        )
        assert result.dc_power_w.iloc[0] == pytest.approx(100.0)
        assert result.stc_power_w.iloc[0] == pytest.approx(100.0)

    def test_night_has_no_reference_either(self):
        engine = PhysicsEngine(53.5, 10.0)
        index = to_index([SUMMER_NOON_UTC - 12 * HOUR])
        result = engine.run(
            index,
            GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=1.0),
            ghi=pd.Series([0.0], index=index),
        )
        assert result.dc_power_w.iloc[0] == 0.0
        assert result.stc_power_w.iloc[0] == 0.0


def _rows(engine: ForecastEngine):
    index = engine._midpoint_index(DAY_START, DAY_START + 24 * HOUR)
    conditions = engine._actual_conditions(index, DAY_START, DAY_START + 24 * HOUR)
    assert conditions is not None
    return [
        row
        for row in engine._evaluate(
            index, conditions, apply_learning=False, is_forecast=False
        )
        if row.string_id == "s1"
    ]


class TestHourlyChainCarriesHeat:
    def test_a_sunlit_hour_reports_its_factor_and_cell_temperature(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 24)

        rows = {row.ts_utc: row for row in _rows(engine)}
        noon = rows[NOON]
        # 20 degC air, 2 m/s: the cells run well above 25 degC at noon.
        assert 0.8 < noon.thermal_factor < 1.0
        assert noon.cell_temp_c is not None and noon.cell_temp_c > 25.0
        assert noon.air_temp_c == pytest.approx(20.0)
        assert noon.wind_ms == pytest.approx(2.0)
        # The factor is the physics over its own reference, nothing else.
        assert noon.physics_kwh / noon.thermal_factor > noon.physics_kwh

    def test_a_dark_hour_is_neutral_and_has_no_cell_temperature(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 24)

        rows = {row.ts_utc: row for row in _rows(engine)}
        midnight = rows[DAY_START]
        assert midnight.physics_kwh == 0.0
        assert midnight.thermal_factor == 1.0
        assert midnight.cell_temp_c is None
        # The air is still reported: it is a forecast fact, not a physics one.
        assert midnight.air_temp_c == pytest.approx(20.0)

    def test_station_heat_lowers_the_factor(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """The measured-air path from the previous change shows up here."""
        engine = ForecastEngine(with_air(plant), seeded_store)
        engine.load_models()
        clear_sky_forecast(engine, seeded_store, DAY_START - HOUR, DAY_START, 24)
        baseline = {row.ts_utc: row for row in _rows(engine)}[NOON]

        write_air(seeded_store, NOON, NOON + HOUR, temp_c=35.0, wind_ms=0.0)
        hot = {row.ts_utc: row for row in _rows(engine)}[NOON]

        assert hot.thermal_factor < baseline.thermal_factor
        assert hot.cell_temp_c > baseline.cell_temp_c
        assert hot.air_temp_c == pytest.approx(35.0)
        assert hot.wind_ms == pytest.approx(0.0)


class TestThermalLossSum:
    HOURLY = ((0, 1.0), (3600, 2.0), (7200, 1.0))

    def test_loss_and_gain_add_up_with_sign(self):
        chain = {0: {"thermal": 0.9}, 3600: {"thermal": 1.25}, 7200: {"thermal": 1.0}}
        total = thermal_loss_kwh(self.HOURLY, chain, 0, 10800)
        assert total == pytest.approx((1.0 / 0.9 - 1.0) + (2.0 / 1.25 - 2.0))

    def test_the_window_is_honoured(self):
        chain = {0: {"thermal": 0.5}, 3600: {"thermal": 0.5}}
        assert thermal_loss_kwh(self.HOURLY, chain, 3600, 7200) == pytest.approx(2.0)

    def test_hours_without_a_factor_are_neutral(self):
        """An older forecast row, or a dark hour, must not blow the sum up."""
        chain = {0: {"shading": 0.8}, 3600: {"thermal": None}}
        assert thermal_loss_kwh(self.HOURLY, chain, 0, 10800) == 0.0
