"""Telling "it is working" apart from "it has not crashed".

Both installations that ran this integration spent days in a state where every
sensor published, nothing raised, and nothing whatsoever was being learned.
The log was clean throughout. These rules exist so that state announces
itself -- and, just as importantly, so that a restart or a passing cloud does
not.
"""

from __future__ import annotations

from core.health import (
    BARREN_CYCLES,
    DAYLIGHT_ELEVATION_DEG,
    DEAD_COVERAGE_UPDATES,
    Health,
    learn_summary,
)

FULL = {"s1": 1.0, "s2": 0.98}
DEAD = {"s1": 0.0, "s2": 0.0}
NOON = 45.0
NIGHT = -8.0


class TestCaptureSilence:
    def test_a_healthy_plant_says_nothing(self):
        health = Health()
        for _ in range(20):
            assert health.observe_coverage(FULL, NOON) is None

    def test_darkness_is_not_a_fault(self):
        """Half the year is night, and an inverter asleep is not a defect."""
        health = Health()
        for _ in range(50):
            assert health.observe_coverage(DEAD, NIGHT) is None

    def test_a_restart_is_ridden_out(self):
        health = Health()
        for _ in range(DEAD_COVERAGE_UPDATES - 1):
            assert health.observe_coverage(DEAD, NOON) is None

    def test_sustained_silence_is_reported(self):
        health = Health()
        problems = [
            health.observe_coverage(DEAD, NOON) for _ in range(DEAD_COVERAGE_UPDATES)
        ]
        assert problems[-1] == "no_capture"
        assert problems[:-1] == [None] * (DEAD_COVERAGE_UPDATES - 1)

    def test_it_is_reported_once_not_every_update(self):
        health = Health()
        for _ in range(DEAD_COVERAGE_UPDATES):
            health.observe_coverage(DEAD, NOON)
        assert all(health.observe_coverage(DEAD, NOON) is None for _ in range(30))

    def test_recovery_rearms_it(self):
        health = Health()
        for _ in range(DEAD_COVERAGE_UPDATES):
            health.observe_coverage(DEAD, NOON)
        assert health.observe_coverage(FULL, NOON) is None
        problems = [
            health.observe_coverage(DEAD, NOON) for _ in range(DEAD_COVERAGE_UPDATES)
        ]
        assert problems[-1] == "no_capture"

    def test_a_single_good_update_resets_the_run(self):
        health = Health()
        for _ in range(DEAD_COVERAGE_UPDATES - 1):
            health.observe_coverage(DEAD, NOON)
        health.observe_coverage(FULL, NOON)
        for _ in range(DEAD_COVERAGE_UPDATES - 1):
            assert health.observe_coverage(DEAD, NOON) is None

    def test_a_plant_with_no_strings_yet_is_not_broken(self):
        assert Health().observe_coverage({}, NOON) is None

    def test_low_sun_is_left_alone(self):
        health = Health()
        for _ in range(20):
            assert health.observe_coverage(DEAD, DAYLIGHT_ELEVATION_DEG - 1) is None

    def test_partial_coverage_is_not_silence(self):
        health = Health()
        for _ in range(20):
            assert health.observe_coverage({"s1": 0.4}, NOON) is None


class TestBarrenLearning:
    @staticmethod
    def _cycle(folded=5, used=0, **reasons):
        skipped = sum(reasons.values())
        return {
            "hours_materialised": folded,
            "observations_used": used,
            "observations_skipped": skipped,
            "skipped_because": dict(reasons),
        }

    def test_learning_normally_says_nothing(self):
        health = Health()
        for _ in range(20):
            assert health.observe_learn(self._cycle(used=5)) is None

    def test_a_night_of_folded_darkness_is_silent(self):
        """The cycle runs hourly around the clock.

        Eight night hours fold rows and learn nothing from every one of them,
        entirely correctly. Judging on folded rows alone would raise the alarm
        before breakfast, every single day.
        """
        health = Health()
        for _ in range(12):
            assert health.observe_learn(self._cycle(night=5)) is None

    def test_learning_switched_off_is_not_a_fault(self):
        health = Health()
        for _ in range(20):
            assert (
                health.observe_learn(
                    self._cycle(ratio_out_of_range=5), learning_enabled=False
                )
                is None
            )

    def test_switching_learning_off_clears_a_standing_warning(self):
        health = Health()
        for _ in range(BARREN_CYCLES):
            health.observe_learn(self._cycle(ratio_out_of_range=5))
        assert "not_learning" in health.active
        health.observe_learn(self._cycle(ratio_out_of_range=5), learning_enabled=False)
        assert "not_learning" not in health.active

    def test_folding_rows_and_judging_none_of_them_is_barren(self):
        """The stall this warning exists for.

        When the weather source has gone away there is no irradiance to run
        the physics against, so the cycle folds its rows and gives up before
        judging any of them: rows materialised, nothing used, no skip reasons.
        Read as "a quiet night" that would go unreported forever.
        """
        health = Health()
        blank = {
            "hours_materialised": 5,
            "observations_used": 0,
            "observations_skipped": 0,
            "skipped_because": {},
        }
        problems = [health.observe_learn(blank) for _ in range(BARREN_CYCLES)]
        assert problems[-1] == "not_learning"

    def test_a_night_run_does_not_break_a_daylight_streak(self):
        """Night must neither raise the alarm nor reset the count."""
        health = Health()
        for _ in range(BARREN_CYCLES - 1):
            health.observe_learn(self._cycle(ratio_out_of_range=5))
        assert health.observe_learn(self._cycle(night=5)) is None
        assert health.observe_learn(self._cycle(ratio_out_of_range=5)) == "not_learning"

    def test_an_idle_cycle_is_not_a_barren_one(self):
        """Running twice inside the same hour folds nothing and learns nothing.

        That is the ordinary case, not a fault, and treating it as one would
        raise the alarm on every restart.
        """
        health = Health()
        for _ in range(50):
            assert health.observe_learn(self._cycle(folded=0)) is None

    def test_a_run_of_barren_cycles_is_reported(self):
        health = Health()
        problems = [
            health.observe_learn(self._cycle(ratio_out_of_range=5))
            for _ in range(BARREN_CYCLES)
        ]
        assert problems[-1] == "not_learning"
        assert problems[:-1] == [None] * (BARREN_CYCLES - 1)

    def test_one_learned_observation_clears_it(self):
        health = Health()
        for _ in range(BARREN_CYCLES - 1):
            health.observe_learn(self._cycle(ratio_out_of_range=5))
        assert health.observe_learn(self._cycle(used=1, ratio_out_of_range=4)) is None
        for _ in range(BARREN_CYCLES - 1):
            assert health.observe_learn(self._cycle(ratio_out_of_range=5)) is None

    def test_reported_once(self):
        health = Health()
        for _ in range(BARREN_CYCLES):
            health.observe_learn(self._cycle(ratio_out_of_range=5))
        assert all(
            health.observe_learn(self._cycle(ratio_out_of_range=5)) is None
            for _ in range(30)
        )

    def test_the_two_problems_are_independent(self):
        health = Health()
        for _ in range(DEAD_COVERAGE_UPDATES):
            health.observe_coverage(DEAD, NOON)
        for _ in range(BARREN_CYCLES):
            problem = health.observe_learn(self._cycle(ratio_out_of_range=5))
        assert problem == "not_learning"
        assert health.active == frozenset({"no_capture", "not_learning"})


class TestSummary:
    def test_an_idle_cycle_says_so(self):
        assert learn_summary({"hours_materialised": 0}) == "nothing new to fold"

    def test_a_working_cycle_reads_plainly(self):
        line = learn_summary(
            {
                "hours_materialised": 5,
                "observations_used": 5,
                "observations_skipped": 0,
                "shading_observations": 60,
                "bias_observations": 104,
            }
        )
        assert "5 hourly rows folded" in line
        assert "5 observations learned" in line
        assert "60 shading" in line

    def test_skips_carry_their_reasons(self):
        line = learn_summary(
            {
                "hours_materialised": 5,
                "observations_used": 1,
                "observations_skipped": 4,
                "skipped_because": {"low_coverage": 1, "ratio_out_of_range": 3},
            }
        )
        assert "4 skipped" in line
        assert "ratio_out_of_range 3" in line
        assert "low_coverage 1" in line

    def test_zero_counters_are_left_out(self):
        line = learn_summary({"hours_materialised": 2, "observations_used": 2})
        assert "censored" not in line
        assert "rejected" not in line

    def test_missing_keys_do_not_crash(self):
        assert learn_summary({}) == "nothing new to fold"
