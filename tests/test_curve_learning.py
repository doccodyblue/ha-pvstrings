"""Stage B: the datasheet curve corrected by what the plant actually does.

The properties pinned here are the ones that decide whether learning
improves the forecast or quietly ruins it: the prior must survive thin
evidence, the cap must survive a broken sensor, and the excluded regimes
(clipping, standby, censored intervals) must stay excluded.
"""

from __future__ import annotations

import pytest

from core.curve_learning import (
    LOAD_BUCKETS,
    STANDBY_FLOOR_PCT,
    fit_curve,
    from_rows,
    to_rows,
)

PRIOR = ((2.0, 0.86), (10.0, 0.93), (50.0, 0.96), (100.0, 0.95))
RATED = 1600.0
NOW = 1_800_000_000


def pairs(load_pct: float, efficiency: float, count: int, ts: float = NOW):
    """``count`` five-minute pairs at one load, one per interval."""
    in_w = RATED * load_pct / 100.0
    return [
        (ts - index * 300, in_w, in_w * efficiency, 1.0) for index in range(count)
    ]


class TestThePriorHolds:
    def test_no_evidence_leaves_the_datasheet_untouched(self):
        curve = fit_curve([], PRIOR, RATED, NOW)
        assert not curve.any_learned
        assert curve.coverage == 0.0
        for load, bin_ in curve.bins.items():
            assert bin_.eta == pytest.approx(bin_.prior)

    def test_thin_evidence_barely_moves_a_point(self):
        """Ten samples against a threshold of fifty: a sixth of the way.

        Not zero, because a threshold is a cliff and cliffs put kinks into
        the interpolated curve; not much, because ten samples are not an
        efficiency measurement either.
        """
        curve = fit_curve(pairs(50.0, 0.90, 10), PRIOR, RATED, NOW, min_samples=50)
        b = curve.bins[50.0]
        assert b.learned is False
        assert b.n_eff == pytest.approx(10.0)
        assert b.measured == pytest.approx(0.90)
        # prior 0.96 + (0.90 - 0.96) * 10/60
        assert b.eta == pytest.approx(0.95, abs=0.002)

    def test_evidence_moves_a_point_further_the_more_of_it_there_is(self):
        etas = [
            fit_curve(pairs(50.0, 0.90, n), PRIOR, RATED, NOW, min_samples=50)
            .bins[50.0].eta
            for n in (10, 50, 200, 1000)
        ]
        assert etas == sorted(etas, reverse=True)
        assert etas[-1] == pytest.approx(0.91, abs=0.005)  # capped at 5 pp

    def test_the_curve_is_always_complete(self):
        """A half-learned curve is worse than the datasheet: the gaps are
        exactly where the interpolation runs."""
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        assert set(curve.bins) == set(LOAD_BUCKETS)
        assert len(curve.points()) == len(LOAD_BUCKETS)


class TestLearning:
    def test_enough_evidence_moves_the_point(self):
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        assert curve.bins[50.0].learned is True
        assert curve.bins[50.0].eta == pytest.approx(0.94, abs=0.005)
        assert curve.bins[50.0].prior == pytest.approx(0.96)

    def test_only_the_measured_point_moves(self):
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        assert curve.bins[10.0].learned is False
        assert curve.bins[10.0].eta == pytest.approx(curve.bins[10.0].prior)

    def test_readiness_is_measured_against_what_the_plant_can_reach(self):
        """A 1.4 kWp array on a 1600 W inverter never passes ~87 %.

        Counting its top support points as outstanding would leave the
        readiness figure below 100 % for ever, reading as "never
        finished" when the truth is "as complete as it can get".
        """
        curve = fit_curve(pairs(50.0, 0.94, 400), PRIOR, RATED, NOW)
        # Nothing above 50 % was ever seen, so nothing above it is counted.
        assert curve.reachable == (2.0, 5.0, 10.0, 20.0, 35.0, 50.0)
        assert curve.coverage == pytest.approx(1 / 6, abs=0.01)
        assert curve.max_load_pct == pytest.approx(50.0, abs=0.1)

    def test_the_unreachable_top_keeps_the_datasheet(self):
        curve = fit_curve(pairs(50.0, 0.90, 400), PRIOR, RATED, NOW)
        for load in (75.0, 100.0):
            assert curve.bins[load].eta == pytest.approx(curve.bins[load].prior)
            assert curve.bins[load].n_eff == 0.0

    def test_no_step_between_a_moved_point_and_its_neighbour(self):
        """The kink a hard threshold produced: one point on measurement,
        the next on the datasheet, and a jump between them that no
        inverter has."""
        curve = fit_curve(
            pairs(50.0, 0.90, 200) + pairs(35.0, 0.90, 8), PRIOR, RATED, NOW
        )
        # The thin neighbour leans the same way, so the gap between them
        # stays smaller than the correction itself.
        step = abs(curve.bins[50.0].eta - curve.bins[35.0].eta)
        correction = abs(curve.bins[50.0].eta - curve.bins[50.0].prior)
        assert step < correction

    def test_a_broken_sensor_cannot_rewrite_the_curve(self):
        """Half the output for a day is a fault, not an efficiency."""
        curve = fit_curve(
            pairs(50.0, 0.50, 500), PRIOR, RATED, NOW, max_deviation_pp=5.0
        )
        assert curve.bins[50.0].eta == pytest.approx(0.91, abs=0.001)

    def test_it_never_learns_above_unity(self):
        curve = fit_curve(pairs(50.0, 1.04, 500), PRIOR, RATED, NOW)
        assert curve.bins[50.0].eta <= 1.0

    def test_recent_evidence_outweighs_old(self):
        old = pairs(50.0, 0.90, 300, ts=NOW - 900 * 86400)
        new = pairs(50.0, 0.95, 300)
        curve = fit_curve(old + new, PRIOR, RATED, NOW)
        assert curve.bins[50.0].eta > 0.93


class TestExcludedRegimes:
    def test_the_standby_floor_is_not_a_conversion(self):
        below = STANDBY_FLOOR_PCT / 2
        curve = fit_curve(pairs(below, 0.40, 500), PRIOR, RATED, NOW)
        assert not curve.any_learned

    def test_clipped_intervals_are_not_curve_points(self):
        """At the ceiling the output stopped following the input; folding
        that in would bend the top of the curve for a reason that is not
        conversion."""
        clipped = [(NOW - i * 300, RATED * 1.2, RATED, 1.0) for i in range(500)]
        curve = fit_curve(clipped, PRIOR, RATED, NOW)
        assert not curve.any_learned

    def test_impossible_ratios_are_dropped(self):
        curve = fit_curve(pairs(50.0, 0.05, 500), PRIOR, RATED, NOW)
        assert not curve.any_learned

    def test_a_missing_reference_power_learns_nothing(self):
        assert fit_curve(pairs(50.0, 0.94, 500), PRIOR, 0.0, NOW).bins == {}


class TestPersistence:
    def test_only_measured_points_are_stored(self):
        """Storing the prior too would freeze today's datasheet into the
        database and survive a later correction of the datasheet itself.
        The raw measurement is stored, not the shrunk value, so changing
        the evidence constant or the cap takes effect on load."""
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        rows = to_rows({"g1|inverter": curve})
        assert sorted(rows) == ["g1|inverter|-1", "g1|inverter|50"]
        assert rows["g1|inverter|50"][0] == pytest.approx(0.94)
        assert curve.bins[50.0].eta != pytest.approx(0.94)

    def test_the_observed_ceiling_survives_a_restart(self):
        """Rebuilding it from which buckets hold samples is lossy: buckets
        take the nearest edge, so a plant peaking at 90 % stores under the
        100 % edge and would come back claiming it reaches full load."""
        curve = fit_curve(pairs(90.0, 0.95, 200), PRIOR, RATED, NOW)
        assert curve.max_load_pct == pytest.approx(90.0, abs=0.1)
        restored = from_rows(
            to_rows({"g1|inverter": curve}), {"g1|inverter": PRIOR}
        )["g1|inverter"]
        assert restored.max_load_pct == pytest.approx(90.0, abs=0.1)
        assert 100.0 not in restored.reachable

    def test_a_configured_scope_without_evidence_still_has_a_curve(self):
        """Otherwise a restart before the first pair looks exactly like
        learning being switched off."""
        curves = from_rows({}, {"g1|inverter": PRIOR})
        assert "g1|inverter" in curves
        assert curves["g1|inverter"].any_evidence is False
        assert curves["g1|inverter"].coverage == 0.0

    def test_round_trip_refills_the_prior(self):
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        restored = from_rows(
            to_rows({"g1|inverter": curve}), {"g1|inverter": PRIOR}
        )["g1|inverter"]
        assert restored.bins[50.0].eta == pytest.approx(curve.bins[50.0].eta)
        assert restored.bins[50.0].learned is True
        assert restored.bins[10.0].learned is False
        assert restored.bins[10.0].eta == pytest.approx(0.93)

    def test_a_stored_point_is_re_capped_against_the_current_prior(self):
        """Stored points carry no memory of the prior they were fitted on.

        Swap the inverter model afterwards and the old point would sit
        wherever it liked next to the new datasheet -- the cap has to hold
        across a restart, or it is not a guarantee.
        """
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        other_prior = ((2.0, 0.80), (50.0, 0.80), (100.0, 0.80))
        restored = from_rows(
            to_rows({"g1|inverter": curve}),
            {"g1|inverter": other_prior},
            {"g1|inverter": 5.0},
        )["g1|inverter"]
        assert restored.bins[50.0].eta == pytest.approx(0.85, abs=0.001)

    def test_a_scope_without_a_prior_is_dropped(self):
        """The prior comes from configuration; if the model was removed,
        the stored points describe a curve nobody can place any more."""
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        assert from_rows(to_rows({"g1|inverter": curve}), {}) == {}


class TestTheDashboardContract:
    def test_the_block_has_the_agreed_shape(self):
        curve = fit_curve(pairs(50.0, 0.94, 200), PRIOR, RATED, NOW)
        block = curve.as_dict()
        assert set(block) == {"coverage", "max_load", "bins"}
        entry = block["bins"]["0.50"]
        assert set(entry) == {
            "eta", "n_eff", "prior", "learned", "measured", "spread",
            "reachable",
        }
        assert entry["learned"] is True

    def test_a_point_can_be_watched_forming(self):
        """The raw measurement travels from the first sample on, so a
        card can show a point settling long before it carries the curve."""
        curve = fit_curve(pairs(50.0, 0.94, 5), PRIOR, RATED, NOW)
        entry = curve.as_dict()["bins"]["0.50"]
        assert entry["learned"] is False
        assert entry["measured"] == pytest.approx(0.94)
        assert entry["spread"] == pytest.approx(0.0, abs=1e-6)
        assert entry["n_eff"] == pytest.approx(5.0)

    def test_spread_reports_how_settled_a_point_is(self):
        noisy = pairs(50.0, 0.90, 100) + pairs(50.0, 0.98, 100)
        calm = pairs(50.0, 0.94, 200)
        assert (
            fit_curve(noisy, PRIOR, RATED, NOW).bins[50.0].spread
            > fit_curve(calm, PRIOR, RATED, NOW).bins[50.0].spread
        )
