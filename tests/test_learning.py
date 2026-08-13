"""The learning rules -- especially the ones that must NOT fire."""

from __future__ import annotations

import math

import pytest

from core.learning import (
    MAX_N_EFF,
    SHRINK_K,
    STRING_DAYPART_MIN_N,
    Effect,
    GhiBiasModel,
    LogRatioModel,
    Observation,
    daypart,
    horizon_bucket,
    plant_key,
    weather_class,
)


class TestBuckets:
    def test_daypart_is_relative_to_solar_noon(self):
        noon = 1_700_000_000.0
        assert daypart(noon - 4 * 3600, noon) == "morning"
        assert daypart(noon, noon) == "midday"
        assert daypart(noon + 90 * 60, noon) == "midday"
        assert daypart(noon + 4 * 3600, noon) == "afternoon"

    def test_horizon_buckets(self):
        assert horizon_bucket(1) == "0-6h"
        assert horizon_bucket(12) == "6-24h"
        assert horizon_bucket(30) == "24-48h"
        assert horizon_bucket(60) == "48h+"

    def test_weather_class_prefers_clearsky_index(self):
        assert weather_class(clearsky_index=0.9, clouds_pct=90) == "clear"
        assert weather_class(clearsky_index=0.5) == "partly_cloudy"
        assert weather_class(clearsky_index=0.2) == "overcast"

    def test_rain_wins(self):
        assert weather_class(clearsky_index=0.9, rain_mm=1.0) == "rain"

    def test_cloud_cover_fallback(self):
        assert weather_class(clouds_pct=10) == "clear"
        assert weather_class(clouds_pct=90) == "overcast"
        assert weather_class() == "partly_cloudy"


class TestEffect:
    def test_thin_bucket_is_shrunk_towards_neutral(self):
        effect = Effect(value=0.5, n_eff=1.0)
        assert effect.shrunk == pytest.approx(0.5 * 1.0 / (1.0 + SHRINK_K))

    def test_empty_bucket_is_exactly_neutral(self):
        assert Effect().shrunk == 0.0

    def test_shrinkage_and_ema_share_one_history(self):
        """The failure mode this replaces: a fixed-alpha EMA next to an
        unbounded weight sum counts two different histories, and the shrinkage
        quietly stops doing anything."""
        effect = Effect()
        for _ in range(200):
            effect.update(0.2, 1.0)
        # n_eff saturates at 1/ALPHA instead of growing without bound.
        assert effect.n_eff < 30.0
        assert effect.shrunk == pytest.approx(0.2 * effect.n_eff / (effect.n_eff + SHRINK_K), abs=0.01)

    def test_effects_are_clamped(self):
        effect = Effect()
        for _ in range(500):
            effect.update(5.0, 1.0)
        assert effect.shrunk <= 0.7

    def test_the_saturation_ceiling_is_what_the_constant_says(self):
        """``n_eff`` converges on ``w / ALPHA`` -- about 22 at full weight."""
        effect = Effect()
        for _ in range(2000):
            effect.update(0.2, 1.0)
        assert effect.n_eff == pytest.approx(MAX_N_EFF, rel=0.01)

    def test_no_threshold_may_sit_above_the_ceiling(self):
        """A gate above the ceiling disables its layer permanently.

        STRING_DAYPART_MIN_N was 25 against a ceiling of 22.14: the per-string
        x daypart effect accumulated evidence for ever, was filtered back out
        of the summary by the same threshold, and never once reached the
        forecast.
        """
        assert STRING_DAYPART_MIN_N < MAX_N_EFF


class TestLogRatioModel:
    def _obs(self, **kwargs) -> Observation:
        base = dict(
            string_id="s1",
            weather="clear",
            part="midday",
            measured_kwh=0.9,
            physics_kwh=1.0,
            weight=1.0,
        )
        base.update(kwargs)
        return Observation(**base)

    def test_neutral_before_any_data(self):
        assert LogRatioModel().factor("s1", "clear", "midday") == pytest.approx(1.0)

    def test_learns_towards_the_observed_ratio(self):
        model = LogRatioModel()
        for _ in range(100):
            model.observe(self._obs())
        factor = model.factor("s1", "clear", "midday")
        assert 0.85 < factor < 1.0

    def test_the_string_daypart_layer_actually_switches_on(self):
        """One string weak in the morning only -- the third layer's whole job.

        The plant bucket is keyed on (weather, daypart), so a morning deficit
        shared by every string lands there. Only a deficit specific to *one*
        string in *one* daypart reaches the interaction, and until the gate was
        lowered below the saturation ceiling it never reached the forecast at
        all.
        """
        model = LogRatioModel()
        for _ in range(200):
            model.observe(self._obs(string_id="s1", part="morning", measured_kwh=0.7))
            model.observe(self._obs(string_id="s2", part="morning", measured_kwh=1.0))
            model.observe(self._obs(string_id="s1", part="afternoon", measured_kwh=1.0))
            model.observe(self._obs(string_id="s2", part="afternoon", measured_kwh=1.0))

        buckets = model.summary()["string_daypart"]
        assert "s1|morning" in buckets, "the interaction never became visible"
        assert model.factor("s1", "clear", "morning") < model.factor(
            "s1", "clear", "afternoon"
        )

    def test_plant_effect_is_shared_between_strings(self):
        """A forecast error is plant-wide; one string's evidence must help the
        others rather than being re-learned from scratch."""
        model = LogRatioModel()
        for _ in range(60):
            model.observe(self._obs(string_id="s1"))
            model.observe(self._obs(string_id="s2"))
        untouched = model.factor("s3", "clear", "midday")
        assert untouched < 0.99

    def test_string_offset_captures_what_the_plant_level_did_not(self):
        model = LogRatioModel()
        for _ in range(80):
            model.observe(self._obs(string_id="s1", measured_kwh=1.0))
            model.observe(self._obs(string_id="s2", measured_kwh=0.6))
        assert model.factor("s2", "clear", "midday") < model.factor(
            "s1", "clear", "midday"
        )

    def test_absurd_ratios_are_rejected(self):
        model = LogRatioModel()
        assert model.observe(self._obs(measured_kwh=50.0)) is False
        assert model.observe(self._obs(measured_kwh=0.001)) is False

    def test_zero_physics_is_skipped(self):
        assert LogRatioModel().observe(self._obs(physics_kwh=0.0)) is False

    def test_zero_weight_is_skipped(self):
        assert LogRatioModel().observe(self._obs(weight=0.0)) is False


class TestCensoredUpdates:
    """The hinge rule: a curtailed hour may only ever push the model up."""

    def _obs(self, **kwargs) -> Observation:
        base = dict(
            string_id="s1",
            weather="clear",
            part="midday",
            measured_kwh=1.0,
            physics_kwh=1.0,
            weight=1.0,
        )
        base.update(kwargs)
        return Observation(**base)

    def test_consistent_censored_hour_teaches_nothing(self):
        model = LogRatioModel()
        used = model.observe(
            self._obs(measured_kwh=0.8, physics_kwh=1.5, value_kind="lower_bound")
        )
        assert used is False
        assert model.factor("s1", "clear", "midday") == pytest.approx(1.0)

    def test_censored_hour_above_physics_pushes_up(self):
        model = LogRatioModel()
        used = model.observe(
            self._obs(measured_kwh=1.4, physics_kwh=1.0, value_kind="lower_bound")
        )
        assert used is True
        assert model.factor("s1", "clear", "midday") > 1.0

    def test_a_summer_of_curtailment_does_not_bias_the_model_down(self):
        """Without the hinge this is the systematic downward drift that makes
        every sunny midday look worse than it is."""
        model = LogRatioModel()
        for _ in range(200):
            model.observe(
                self._obs(measured_kwh=0.8, physics_kwh=2.0, value_kind="lower_bound")
            )
        assert model.factor("s1", "clear", "midday") == pytest.approx(1.0)

    def test_reconstructed_moves_less_than_measured(self):
        strong, weak = LogRatioModel(), LogRatioModel()
        for _ in range(30):
            strong.observe(self._obs(measured_kwh=0.7))
            weak.observe(self._obs(measured_kwh=0.7, value_kind="reconstructed"))
        assert weak.factor("s1", "clear", "midday") > strong.factor(
            "s1", "clear", "midday"
        )


class TestGhiBias:
    def test_neutral_without_data(self):
        assert GhiBiasModel().factor(12, 3.0) == pytest.approx(1.0)

    def test_horizons_do_not_share_a_bucket(self):
        """A +1 h and a +48 h forecast do not have the same bias."""
        model = GhiBiasModel()
        for _ in range(60):
            model.observe(12, 3.0, measured_ghi=800.0, forecast_ghi=800.0)
            model.observe(12, 40.0, measured_ghi=800.0, forecast_ghi=600.0)
        assert model.factor(12, 3.0) == pytest.approx(1.0, abs=0.01)
        assert model.factor(12, 40.0) > 1.1

    def test_darkness_carries_no_information(self):
        model = GhiBiasModel()
        assert model.observe(4, 3.0, measured_ghi=1.0, forecast_ghi=2.0) is False

    def test_local_hours_are_separate(self):
        model = GhiBiasModel()
        for _ in range(60):
            model.observe(8, 10.0, measured_ghi=300.0, forecast_ghi=400.0)
        assert model.factor(8, 10.0) < 1.0
        assert model.factor(14, 10.0) == pytest.approx(1.0)


def test_roundtrip_through_rows():
    model = LogRatioModel()
    for _ in range(20):
        model.observe(
            Observation("s1", "overcast", "morning", 0.6, 1.0, 1.0)
        )
    restored = LogRatioModel.from_rows(
        plant=model.to_rows("plant"),
        string=model.to_rows("string"),
        string_daypart=model.to_rows("string_daypart"),
    )
    assert restored.factor("s1", "overcast", "morning") == pytest.approx(
        model.factor("s1", "overcast", "morning")
    )


def test_plant_key_shape():
    assert plant_key("overcast", "midday") == "overcast|midday"
    assert math.isclose(math.exp(0.0), 1.0)


class TestDeclineReasons:
    """"Not used" covers five different situations.

    A caller that only sees a boolean can report a shrug, and on a plant where
    four strings in five are dropped every hour the difference between them is
    the entire diagnosis.
    """

    def _obs(self, **kwargs):
        base = dict(
            string_id="s1",
            weather="clear",
            part="midday",
            measured_kwh=0.8,
            physics_kwh=1.0,
            weight=1.0,
        )
        base.update(kwargs)
        return Observation(**base)

    def test_a_good_observation_has_no_reason(self):
        assert LogRatioModel().decline_reason(self._obs()) is None

    def test_zero_weight(self):
        assert LogRatioModel().decline_reason(self._obs(weight=0.0)) == "no_weight"

    def test_zero_physics(self):
        assert (
            LogRatioModel().decline_reason(self._obs(physics_kwh=0.0)) == "no_physics"
        )

    def test_zero_production(self):
        assert (
            LogRatioModel().decline_reason(self._obs(measured_kwh=0.0))
            == "no_production"
        )

    def test_an_absurd_ratio(self):
        assert (
            LogRatioModel().decline_reason(self._obs(measured_kwh=50.0))
            == "ratio_out_of_range"
        )

    def test_a_censored_hour_the_physics_already_covers(self):
        assert (
            LogRatioModel().decline_reason(
                self._obs(measured_kwh=0.5, physics_kwh=1.0, value_kind="lower_bound")
            )
            == "censored_and_consistent"
        )

    def test_a_censored_hour_that_beats_the_physics_is_learned(self):
        assert (
            LogRatioModel().decline_reason(
                self._obs(measured_kwh=1.5, physics_kwh=1.0, value_kind="lower_bound")
            )
            is None
        )

    def test_the_reason_and_the_verdict_never_disagree(self):
        cases = [
            self._obs(),
            self._obs(weight=0.0),
            self._obs(physics_kwh=0.0),
            self._obs(measured_kwh=0.0),
            self._obs(measured_kwh=50.0),
            self._obs(measured_kwh=0.5, value_kind="lower_bound"),
            self._obs(measured_kwh=1.5, value_kind="lower_bound"),
        ]
        for obs in cases:
            model = LogRatioModel()
            assert (model.decline_reason(obs) is None) == model.observe(obs)
