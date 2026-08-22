"""Nowcast: the pure core, and the guarantees the engine must not break."""

from __future__ import annotations

import numpy as np
import pytest

from core import persistence


class TestSkyState:
    def test_reads_the_clearness_index_off_the_window(self):
        measured = np.array([400.0, 420.0, 410.0])
        clearsky = np.array([800.0, 840.0, 820.0])
        forecast = np.array([400.0, 420.0, 410.0])

        state, _ = persistence.sky_state(measured, forecast, clearsky, 1000.0)

        assert state is not None
        assert state.kt == pytest.approx(0.5, abs=0.01)
        assert state.intervals == 3

    def test_median_survives_a_single_cloud_gap(self):
        """One dark interval in a bright window must not define the sky."""
        measured = np.array([600.0, 610.0, 40.0, 605.0, 600.0])
        clearsky = np.full(5, 800.0)
        forecast = np.full(5, 600.0)

        state, _ = persistence.sky_state(measured, forecast, clearsky, 1000.0)

        assert state is not None
        assert state.kt == pytest.approx(0.75, abs=0.02)

    def test_darkness_yields_nothing(self):
        measured = np.array([2.0, 1.0, 0.0])
        clearsky = np.array([10.0, 8.0, 4.0])

        assert persistence.sky_state(measured, clearsky, clearsky)[0] is None

    def test_too_few_intervals_yields_nothing(self):
        measured = np.array([400.0, np.nan, np.nan])
        clearsky = np.full(3, 800.0)

        assert persistence.sky_state(measured, measured, clearsky)[0] is None

    def test_a_thin_window_is_not_reported_as_darkness(self):
        """Two answers that must not be confused.

        The collector's buffers are empty for a few minutes after a restart,
        so a bright afternoon can genuinely have no rows yet.  Calling that
        "too dark" at 190 W/m2 sends whoever reads the diagnostics hunting a
        broken sensor.
        """
        clearsky = np.full(4, 800.0)
        no_rows = np.full(4, np.nan)
        night = np.zeros(4)

        assert persistence.sky_state(no_rows, no_rows, clearsky)[1] == (
            persistence.REASON_THIN
        )
        assert persistence.sky_state(night, night, np.full(4, 10.0))[1] == (
            persistence.REASON_TOO_DARK
        )

    def test_cloud_enhancement_is_capped(self):
        measured = np.full(4, 1000.0)
        clearsky = np.full(4, 800.0)

        state, _ = persistence.sky_state(measured, measured, clearsky, 1000.0)

        assert state is not None
        assert state.kt == persistence.KT_MAX

    def test_a_calm_sky_carries_further_than_a_broken_one(self):
        clearsky = np.full(6, 800.0)
        measured = np.full(6, 600.0)
        calm, _ = persistence.sky_state(measured, measured, clearsky, 1000.0)
        broken, _ = persistence.sky_state(
            measured,
            np.array([200.0, 900.0, 250.0, 880.0, 210.0, 870.0]),
            clearsky,
            1000.0,
        )

        assert calm is not None and broken is not None
        assert calm.halflife_s > broken.halflife_s
        assert calm.weight(3600.0) > broken.weight(3600.0)


class TestWeight:
    def _state(self, halflife=persistence.HALFLIFE_CALM_S, trust=1.0):
        return persistence.SkyState(
            kt=0.6, spread=0.1, intervals=3, halflife_s=halflife, trust=trust
        )

    def test_the_past_is_never_touched(self):
        """The guarantee the accuracy scoring rests on.

        ``forecast()`` runs from the start of the local day, so the series
        carries negative horizons.  Left to the bare formula they would come
        out *above* one and amplify hours that have already been graded.
        """
        state = self._state()
        for horizon in (-7200.0, -300.0, -1.0, 0.0):
            assert state.weight(horizon) == 0.0

    def test_decays_by_half_over_the_halflife(self):
        # Both points inside the reach -- 7200 s is the cut itself.
        state = self._state(halflife=1800.0)

        assert state.weight(1800.0) == pytest.approx(0.5)
        assert state.weight(3600.0) == pytest.approx(0.25)

    def test_nothing_survives_the_reach(self):
        state = self._state()

        assert state.weight(persistence.REACH_SECONDS + 1.0) == 0.0

    def test_the_reach_itself_is_already_out(self):
        """Exactly at the cut, not one interval later.

        With a calm half-life the weight at 120 min is still about 0.3, so an
        exclusive bound would let a third of the measurement through at the
        very horizon the calibration said to stop at.
        """
        state = self._state()

        assert state.weight(float(persistence.REACH_SECONDS)) == 0.0

    def test_a_thin_bias_model_damps_the_whole_curve(self):
        confident = self._state(trust=1.0)
        unsure = self._state(trust=0.2)

        assert unsure.weight(600.0) < confident.weight(600.0)

    def test_trust_grows_with_evidence(self):
        assert persistence.bias_trust(0.0) == 0.0
        assert persistence.bias_trust(persistence.BIAS_EVIDENCE_K) == pytest.approx(0.5)
        assert persistence.bias_trust(1e6) > 0.99


class TestFrozenSensor:
    """The collector cannot tell a live sensor from a stuck one.

    Its watchdog stamps every sample with the moment it looked, not with the
    age of the state, so an entity that quietly stops updating keeps filling
    the window with fresh-looking rows holding a dead value.  Projecting that
    two hours forward is the worst case this feature has.
    """

    def test_a_repeated_value_is_refused(self):
        assert persistence.looks_frozen(np.full(4, 431.7))

    def test_a_moving_sensor_passes(self):
        assert not persistence.looks_frozen(
            np.array([431.7, 433.1, 429.8, 435.0])
        )

    def test_darkness_may_legitimately_rest_at_zero(self):
        assert not persistence.looks_frozen(np.zeros(4))

    def test_a_short_window_is_not_judged(self):
        assert not persistence.looks_frozen(np.full(2, 400.0))


class TestUnknownRegime:
    def test_without_forecast_rows_the_sky_counts_as_broken(self):
        """No evidence must buy the short carry, not the long one."""
        measured = np.array([600.0, 610.0, 605.0, 600.0])
        clearsky = np.full(4, 800.0)

        state, _ = persistence.sky_state(
            measured, np.full(4, np.nan), clearsky, 1000.0
        )

        assert state is not None
        assert state.spread is None
        assert state.halflife_s == persistence.HALFLIFE_BROKEN_S

    def test_halflife_for_none_is_the_short_one(self):
        assert persistence.halflife_for(None) == persistence.HALFLIFE_BROKEN_S


class TestBlend:
    def test_full_weight_is_the_measured_sky(self):
        forecast = np.array([200.0, 200.0])
        clearsky = np.array([800.0, 900.0])

        out = persistence.blend(forecast, clearsky, np.array([1.0, 1.0]), 0.75)

        assert out == pytest.approx([600.0, 675.0])

    def test_zero_weight_leaves_the_forecast_bit_identical(self):
        forecast = np.array([123.456, 789.012])
        clearsky = np.array([800.0, 900.0])

        out = persistence.blend(forecast, clearsky, np.zeros(2), 0.9)

        assert out.tolist() == forecast.tolist()

    def test_never_predicts_more_light_than_the_sky_can_deliver(self):
        forecast = np.array([900.0])
        clearsky = np.array([800.0])

        out = persistence.blend(forecast, clearsky, np.array([1.0]), 1.1)

        assert out[0] <= 800.0 * persistence.KT_MAX + 1e-9

    def test_half_weight_sits_between_the_two(self):
        forecast = np.array([200.0])
        clearsky = np.array([800.0])

        out = persistence.blend(forecast, clearsky, np.array([0.5]), 0.5)

        assert out[0] == pytest.approx(300.0)
