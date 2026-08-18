"""The guard that stops a mis-reading irradiance sensor from teaching the model.

The regression case at the bottom is real: a station on a free-standing pole
that agreed with the array all morning and then under-read every afternoon,
until the array was producing half again as much as the measured irradiance
physically allows.  Nothing in the integration noticed, because a measured GHI
is treated as truth in three separate places and each of them stayed
self-consistent.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from core.config import PlantConfig
from core.forecast import HOUR, ForecastEngine
from core.learning import BIAS_FULL_WEIGHT_WM2, bias_weight
from core.plausibility import (
    DEFAULT_MARGIN,
    MIN_JUDGED_W,
    Plane,
    cos_incidence,
    exceeds_ceiling,
    judgement_floor,
    plant_ceiling_w,
    poa_ceiling_wm2,
)
from core.store import Store

#: 2025-06-21, local midnight in Europe/Berlin.
DAY_START = 1_750_456_800


def arr(*values: float) -> np.ndarray:
    return np.array(values, dtype=float)


class TestIncidence:
    def test_sun_normal_to_the_plane_is_one(self):
        # A 30 deg plane facing south, sun due south at 60 deg elevation.
        assert cos_incidence(30, 180, arr(60.0), arr(180.0))[0] == pytest.approx(
            1.0, abs=1e-9
        )

    def test_sun_behind_the_plane_is_floored_at_zero(self):
        # Sun in the north-east, plane facing south-west and steep.
        assert cos_incidence(80, 225, arr(10.0), arr(45.0))[0] == 0.0

    def test_flat_plane_follows_the_sine_of_elevation(self):
        for elevation in (5.0, 25.0, 70.0):
            assert cos_incidence(0, 180, arr(elevation), arr(120.0))[
                0
            ] == pytest.approx(np.sin(np.radians(elevation)))


class TestCeilingPicksTheRightExtreme:
    """The ceiling must take the best case over every beam/diffuse split.

    Assuming all of the measured GHI arrives as beam looks like the generous
    choice and is not: at grazing incidence a tilted plane collects almost no
    beam, and an all-diffuse sky would serve it far better.  Getting this wrong
    understates the ceiling and turns healthy hours into false rejections.
    """

    LOW_SUN = (arr(95.0), arr(12.0), arr(274.0))  # W/m2, elevation, azimuth

    def test_grazing_incidence_is_governed_by_the_diffuse_case(self):
        plane = Plane(tilt_deg=30, azimuth_deg=180, kwp=1.0)
        ceiling = poa_ceiling_wm2(plane, *self.LOW_SUN)[0]
        all_beam = (
            95.0
            * cos_incidence(30, 180, self.LOW_SUN[1], self.LOW_SUN[2])[0]
            / np.sin(np.radians(12.0))
        )
        assert ceiling > all_beam
        assert ceiling > 95.0 * 0.9  # roughly the isotropic sky view factor

    def test_plane_facing_the_low_sun_is_governed_by_the_beam_case(self):
        # A steep plane pointed straight at a low sun collects far more than
        # the horizontal does.
        plane = Plane(tilt_deg=78, azimuth_deg=274, kwp=1.0)
        ceiling = poa_ceiling_wm2(plane, *self.LOW_SUN)[0]
        assert ceiling > 95.0 * 3

    def test_ceiling_never_falls_below_the_horizontal_reading(self):
        for tilt in range(0, 91, 15):
            plane = Plane(tilt_deg=tilt, azimuth_deg=180, kwp=1.0)
            for elevation, azimuth in ((8.0, 100.0), (35.0, 180.0), (60.0, 200.0)):
                ceiling = poa_ceiling_wm2(
                    plane, arr(400.0), arr(elevation), arr(azimuth)
                )[0]
                assert ceiling >= 400.0 * 0.45, (tilt, elevation)

    def test_nothing_exceeds_the_physical_cap(self):
        plane = Plane(tilt_deg=90, azimuth_deg=180, kwp=1.0)
        ceiling = poa_ceiling_wm2(plane, arr(900.0), arr(0.2), arr(180.0))[0]
        assert ceiling <= 1400.0

    def test_darkness_yields_no_ceiling(self):
        plane = Plane(tilt_deg=30, azimuth_deg=180, kwp=2.0)
        assert plant_ceiling_w([plane], arr(0.0), arr(20.0), arr(150.0))[0] == 0.0

    def test_planes_add_up(self):
        one = Plane(tilt_deg=30, azimuth_deg=180, kwp=1.0)
        two = Plane(tilt_deg=30, azimuth_deg=180, kwp=3.0)
        single = plant_ceiling_w([two], arr(500.0), arr(40.0), arr(180.0))[0]
        split = plant_ceiling_w([one, one, one], arr(500.0), arr(40.0), arr(180.0))[0]
        assert split == pytest.approx(single)

    def test_a_zero_kwp_plane_contributes_nothing(self):
        dead = Plane(tilt_deg=30, azimuth_deg=180, kwp=0.0)
        assert plant_ceiling_w([dead], arr(500.0), arr(40.0), arr(180.0))[0] == 0.0


class TestTheTestIsOneSided:
    """Falling short is the normal condition and must never be flagged.

    Shading, soiling, snow and curtailment all push the array below the
    ceiling, and every one of them is something the model is meant to learn
    from.  Only the impossible direction says anything about the sensor.
    """

    def test_an_array_under_the_ceiling_is_fine(self):
        for actual in (0.0, 1.0, 500.0, 999.0):
            assert not exceeds_ceiling(actual, 1000.0)

    def test_heavy_shading_is_not_a_sensor_fault(self):
        assert not exceeds_ceiling(120.0, 2400.0)

    def test_the_margin_is_respected(self):
        assert not exceeds_ceiling(1000.0 * DEFAULT_MARGIN, 1000.0)
        assert exceeds_ceiling(1000.0 * DEFAULT_MARGIN + 1.0, 1000.0)

    def test_production_in_reported_darkness_is_always_rejected(self):
        assert exceeds_ceiling(300.0, 0.0)

    def test_darkness_on_both_sides_is_not_a_fault(self):
        assert not exceeds_ceiling(0.0, 0.0)


class TestBiasWeight:
    def test_a_dawn_hour_counts_for_little(self):
        assert bias_weight(20.0) < 0.05

    def test_a_midday_hour_counts_fully(self):
        assert bias_weight(BIAS_FULL_WEIGHT_WM2) == 1.0

    def test_weight_saturates_rather_than_growing_without_bound(self):
        assert bias_weight(1200.0) == 1.0

    def test_monotone(self):
        values = [bias_weight(g) for g in range(0, 1000, 50)]
        assert values == sorted(values)

    def test_darkness_carries_no_weight(self):
        assert bias_weight(0.0) == 0.0


class TestEngineDropsImplausibleHours:
    """The guard has to sit where all three consumers of the measured GHI meet.

    Filtering only the bias learner would leave the physics driver and the
    shading denominator still feeding on the same bad hour.
    """

    def _seed(
        self,
        store: Store,
        hour: int,
        ghi_wm2: float,
        energy_kwh: float,
        string_id: str = "s1",
        coverage: float = 1.0,
        value_kind: str = "measured",
        limit_binding: int | None = 0,
    ) -> None:
        """Seed one hour of irradiance and five-minute production.

        Five-minute rows, not the hourly fold: the fold does not exist yet at
        the moment the guard runs inside a learn cycle, and a test that seeds
        it is testing a path production never takes.
        """
        store.upsert_weather_actual(
            [
                (hour + step, None, None, None, None, None, ghi_wm2, None)
                for step in range(0, HOUR, 300)
            ]
        )
        watts = energy_kwh * 1000.0
        store.upsert_5min(
            [
                (
                    hour + step,
                    string_id,
                    watts * 300 / HOUR,
                    watts,
                    coverage,
                    10,
                    None,
                    limit_binding,
                    value_kind,
                )
                for step in range(0, HOUR, 300)
            ]
        )

    def _engine(self, store: Store, plant: PlantConfig) -> ForecastEngine:
        with_sensor = replace(
            plant,
            weather_sources=replace(
                plant.weather_sources, ghi_entity="sensor.station_ghi"
            ),
        )
        engine = ForecastEngine(with_sensor, store)
        engine.load_models()
        return engine

    def test_a_healthy_hour_survives(self, seeded_store: Store, plant: PlantConfig):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        # 700 W/m2 at midday against a modest 1.2 kWh from one 1.8 kWp string.
        self._seed(seeded_store, noon, 700.0, 1.2)
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset()
        measured = engine._measured_ghi(noon, noon + HOUR)
        assert measured is not None and not measured.empty

    def test_an_impossible_hour_is_dropped(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        # 40 W/m2 cannot make 3 kWh out of this plant, whatever the sky did.
        self._seed(seeded_store, noon, 40.0, 3.0)
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset({noon})
        assert engine._measured_ghi(noon, noon + HOUR) is None

    def test_only_the_offending_hour_is_dropped(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        good = DAY_START + 11 * HOUR
        bad = DAY_START + 12 * HOUR
        self._seed(seeded_store, good, 700.0, 1.2)
        self._seed(seeded_store, bad, 40.0, 3.0)
        rejected = engine.implausible_ghi_hours(good, bad + HOUR)
        assert rejected == frozenset({bad})
        measured = engine._measured_ghi(good, bad + HOUR)
        assert measured is not None
        assert set((measured.index.to_numpy() // HOUR) * HOUR) == {good}

    def test_a_night_hour_with_no_production_is_not_flagged(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        night = DAY_START + 1 * HOUR
        self._seed(seeded_store, night, 0.0, 0.0)
        assert engine.implausible_ghi_hours(night, night + HOUR) == frozenset()

    def test_a_censored_hour_cannot_trigger_a_rejection(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """A curtailed hour under-reports, so it must never accuse the sensor."""
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        self._seed(
            seeded_store, noon, 40.0, 3.0, value_kind="lower_bound", limit_binding=1
        )
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset()

    def test_a_poorly_covered_hour_cannot_trigger_a_rejection(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        self._seed(seeded_store, noon, 40.0, 3.0, coverage=0.4)
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset()

    def test_a_string_without_geometry_cannot_accuse_the_sensor(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Energy may only be weighed against a ceiling that made room for it.

        A string with no geometry on record contributes kilowatt-hours but no
        plane.  Counting it against the remaining strings' ceiling would
        convict a healthy sensor of a fault that is really a gap in the
        configuration.
        """
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        self._seed(seeded_store, noon, 700.0, 1.2)
        # No geometry was ever seeded for this one.
        self._seed(seeded_store, noon, 700.0, 40.0, string_id="ghost")
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset()

    def test_the_verdict_is_memoised_per_window(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        self._seed(seeded_store, noon, 40.0, 3.0)
        first = engine.implausible_ghi_hours(noon, noon + HOUR)
        # A second call over the same window must not recompute -- one learn
        # cycle asks three times and would otherwise count three times over.
        engine._find_implausible_ghi_hours = lambda *a, **k: pytest.fail(
            "recomputed a memoised window"
        )
        assert engine.implausible_ghi_hours(noon, noon + HOUR) is first

    def test_no_measured_sensor_means_nothing_to_check(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        assert engine._measured_ghi(noon, noon + HOUR) is None


class TestTheAfternoonStation:
    """Regression: the real numbers from 2026-08-11 on the reference plant.

    A 4.2 kWp plant over five planes.  The station tracked the array until
    early afternoon and then drifted low; by 17:00 local the array was making
    1988 W while the measured 307 W/m2 allows at most about 1343 W even in the
    most favourable sky.  Those five hours carried 9.5 of the day's 21.0 kWh.
    """

    def _planes(self) -> list[Plane]:
        return [
            Plane(tilt_deg=30, azimuth_deg=180, kwp=1.80),
            Plane(tilt_deg=60, azimuth_deg=180, kwp=1.00),
            Plane(tilt_deg=4, azimuth_deg=180, kwp=0.45),
            Plane(tilt_deg=24, azimuth_deg=110, kwp=0.45),
            Plane(tilt_deg=24, azimuth_deg=110, kwp=0.50),
        ]

    #: hour, sun elevation, sun azimuth, measured GHI, measured plant power
    MORNING = [
        ("08", 22.0, 96.0, 251.0, 792.0),
        ("10", 39.0, 128.0, 365.0, 1348.0),
        ("12", 50.0, 168.0, 425.0, 1759.0),
    ]
    AFTERNOON = [
        ("15", 44.0, 237.0, 578.0, 3252.0),
        ("16", 37.0, 253.0, 460.0, 2741.0),
        ("17", 29.0, 267.0, 307.0, 1988.0),
        ("18", 20.0, 279.0, 180.0, 1111.0),
    ]

    def _ceiling(self, elevation: float, azimuth: float, ghi: float) -> float:
        return float(
            plant_ceiling_w(self._planes(), arr(ghi), arr(elevation), arr(azimuth))[0]
        )

    @pytest.mark.parametrize("label,elevation,azimuth,ghi,power", MORNING)
    def test_the_morning_agreed_and_must_not_be_flagged(
        self, label, elevation, azimuth, ghi, power
    ):
        assert not exceeds_ceiling(power, self._ceiling(elevation, azimuth, ghi)), label

    @pytest.mark.parametrize("label,elevation,azimuth,ghi,power", AFTERNOON)
    def test_the_afternoon_is_physically_impossible(
        self, label, elevation, azimuth, ghi, power
    ):
        assert exceeds_ceiling(power, self._ceiling(elevation, azimuth, ghi)), label

    def test_the_worst_hour_is_flagged_by_a_wide_margin(self):
        ceiling = self._ceiling(20.0, 279.0, 180.0)
        assert 1111.0 / ceiling > 1.4

    def test_a_corrected_sensor_would_pass(self):
        """Scale the afternoon readings back up and every hour becomes fine."""
        for label, elevation, azimuth, ghi, power in self.AFTERNOON:
            assert not exceeds_ceiling(
                power, self._ceiling(elevation, azimuth, ghi * 1.7)
            ), label


class TestOtherPeoplesInstallations:
    """The ceiling must not accuse a healthy sensor anywhere on Earth.

    A false rejection throws away real truth, so every bound here is set to
    the physical worst case rather than to a typical one.
    """

    def test_snow_under_a_steep_plane_does_not_trip_it(self):
        """Fresh snow reaches an albedo of 0.9.

        A 70 deg plane over snow collects a fifth of the horizontal irradiance
        again from the ground alone.  A ceiling built for grass would call
        every bright February hour impossible.
        """
        plane = Plane(tilt_deg=70, azimuth_deg=180, kwp=3.0)
        ghi, elevation, azimuth = arr(300.0), arr(18.0), arr(180.0)
        ceiling = plant_ceiling_w([plane], ghi, elevation, azimuth)[0]
        # Beam on the plane plus a snow-lit foreground, no losses.
        snow_lit = 300.0 * (
            np.cos(np.radians(90 - 18 - 70)) / np.sin(np.radians(18))
        ) + 300.0 * 0.9 * (1 - np.cos(np.radians(70))) / 2
        assert ceiling >= snow_lit * 0.99

    def test_the_gross_fault_is_still_caught(self):
        """Blunting the test for snow must not blunt it for a broken sensor."""
        planes = [Plane(tilt_deg=30, azimuth_deg=180, kwp=1.8)]
        ceiling = plant_ceiling_w(planes, arr(180.0), arr(20.0), arr(279.0))[0]
        # The real 2026-08-11 18:00 hour, scaled to this one plane.
        assert exceeds_ceiling(900.0, ceiling)
        assert not exceeds_ceiling(ceiling, ceiling)

    def test_a_southern_hemisphere_plane_is_handled(self):
        """Nothing in the ceiling assumes which way north is."""
        north_facing = Plane(tilt_deg=30, azimuth_deg=0, kwp=2.0)
        # Sun in the north at midday, as seen from Sydney in June.
        ceiling = plant_ceiling_w([north_facing], arr(500.0), arr(32.0), arr(0.0))[0]
        assert ceiling > 500.0 * 2.0 * 0.9

    def test_the_equator_at_the_zenith(self):
        flat = Plane(tilt_deg=0, azimuth_deg=180, kwp=1.0)
        ceiling = plant_ceiling_w([flat], arr(1000.0), arr(89.9), arr(180.0))[0]
        assert ceiling == pytest.approx(1000.0, rel=0.02)


class TestTheGuardActuallyRunsInALearnCycle:
    """The end-to-end test whose absence let the guard ship inert.

    Every other test in this file calls `implausible_ghi_hours` directly
    against a pre-seeded store.  That passed happily while the live path was
    dead: inside `learn()` the check ran before the hourly fold it was reading
    existed, memoised an empty answer, and every later call in the cycle got
    the memo.  Nothing anywhere reported a problem.
    """

    HOUR_OF_DAY = 12

    def _engine(self, store: Store, plant: PlantConfig) -> ForecastEngine:
        with_sensor = replace(
            plant,
            weather_sources=replace(
                plant.weather_sources, ghi_entity="sensor.station_ghi"
            ),
        )
        engine = ForecastEngine(with_sensor, store)
        engine.load_models()
        return engine

    def _seed_forecast(self, store: Store, hour: int, ghi: float) -> None:
        """A short-horizon irradiance forecast for the hour.

        Without one, `_actual_conditions` returns early and the cycle never
        reaches the plausibility check at all -- which is exactly how the first
        version of this test managed to pass against the broken code.
        """
        store.upsert_weather_forecast(
            [
                (
                    hour - HOUR,  # issued an hour ahead
                    hour,
                    "open_meteo",
                    1,
                    ghi,
                    ghi * 0.8,
                    ghi * 0.3,
                    20.0,
                    0.0,
                    2.0,
                    60.0,
                    0.0,      # rain_mm
                    None,     # rain_probability_pct
                    1013.0,
                    1,
                )
            ]
        )

    def _seed_hour(self, store: Store, hour: int, ghi: float, watts: float) -> None:
        self._seed_forecast(store, hour, ghi)
        store.upsert_weather_actual(
            [
                (hour + step, None, None, None, None, None, ghi, None)
                for step in range(0, HOUR, 300)
            ]
        )
        store.upsert_5min(
            [
                (hour + step, "s1", watts * 300 / HOUR, watts, 1.0, 10, None, 0, "measured")
                for step in range(0, HOUR, 300)
            ]
        )

    def _run(self, store: Store, plant: PlantConfig, ghi: float, watts: float):
        engine = self._engine(store, plant)
        hour = DAY_START + self.HOUR_OF_DAY * HOUR
        self._seed_hour(store, hour, ghi, watts)
        # "Now" one hour past the seeded hour, so it counts as closed.
        return engine, engine.learn(hour + 2 * HOUR), hour

    def test_an_impossible_hour_is_rejected_by_the_cycle_itself(
        self, seeded_store: Store, plant: PlantConfig
    ):
        _engine, stats, _hour = self._run(seeded_store, plant, ghi=40.0, watts=3000.0)
        assert stats.ghi_hours_rejected >= 1

    def test_a_healthy_hour_is_left_alone_by_the_cycle(
        self, seeded_store: Store, plant: PlantConfig
    ):
        _engine, stats, _hour = self._run(seeded_store, plant, ghi=700.0, watts=1200.0)
        assert stats.ghi_hours_rejected == 0

    def test_the_rejection_reaches_the_measured_irradiance(
        self, seeded_store: Store, plant: PlantConfig
    ):
        """Not just counted -- actually withheld from the three consumers."""
        engine, _stats, hour = self._run(
            seeded_store, plant, ghi=40.0, watts=3000.0
        )
        assert engine._measured_ghi(hour, hour + HOUR) is None


class TestTheCeilingLeavesTwilightAlone:
    """Cheap irradiance sensors round to zero before the array does.

    An illuminance-derived GHI commonly reads 0.0 W/m2 through the first and
    last hour of daylight while the panels are already making a few watt-hours.
    Without a floor on the production side, a zero ceiling against any output
    at all reads as a fault, and two hours of every clear day are discarded for
    the life of the installation.
    """

    def test_a_few_watts_against_a_dark_sensor_is_not_a_fault(self):
        floor = judgement_floor(total_kwp=4.2)
        assert not exceeds_ceiling(25.0, 0.0, floor_w=floor)

    def test_real_production_against_a_dark_sensor_still_is(self):
        floor = judgement_floor(total_kwp=4.2)
        assert exceeds_ceiling(2000.0, 0.0, floor_w=floor)

    def test_the_floor_scales_with_the_plant(self):
        assert judgement_floor(0.3) < judgement_floor(30.0)

    def test_a_tiny_plant_keeps_an_absolute_floor(self):
        assert judgement_floor(0.3) >= MIN_JUDGED_W


class TestTheHourMustBeMeasuredBeforeItIsJudged:
    """The ceiling is a mean over reported intervals; the energy is not.

    If the sensor drops out over the bright half of an hour, the ceiling
    describes the dim half while the energy describes the whole hour -- and a
    perfectly good sensor convicts itself.
    """

    def _engine(self, store: Store, plant: PlantConfig) -> ForecastEngine:
        with_sensor = replace(
            plant,
            weather_sources=replace(
                plant.weather_sources, ghi_entity="sensor.station_ghi"
            ),
        )
        engine = ForecastEngine(with_sensor, store)
        engine.load_models()
        return engine

    def _seed(self, store: Store, hour: int, ghi_steps: range, watts: float) -> None:
        store.upsert_weather_actual(
            [
                (hour + step, None, None, None, None, None, 40.0, None)
                for step in ghi_steps
            ]
        )
        store.upsert_5min(
            [
                (hour + step, "s1", watts * 300 / HOUR, watts, 1.0, 10, None, 0, "measured")
                for step in range(0, HOUR, 300)
            ]
        )

    def test_a_fully_measured_hour_is_judged(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        self._seed(seeded_store, noon, range(0, HOUR, 300), 3000.0)
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset({noon})

    def test_a_half_measured_hour_is_left_alone(
        self, seeded_store: Store, plant: PlantConfig
    ):
        engine = self._engine(seeded_store, plant)
        noon = DAY_START + 12 * HOUR
        # Only the first half of the hour has irradiance samples.
        self._seed(seeded_store, noon, range(0, HOUR // 2, 300), 3000.0)
        assert engine.implausible_ghi_hours(noon, noon + HOUR) == frozenset()
