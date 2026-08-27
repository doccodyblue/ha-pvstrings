"""Quality classification and the commanded-vs-binding distinction."""

from __future__ import annotations

import pytest

from core import curtailment as curt
from core.config import ConfigError, CurtailmentGroup
from core.quality import (
    QUALITY_EXACT,
    QUALITY_MISSING,
    QUALITY_NIGHT,
    QUALITY_PARTIAL,
    VALUE_LOWER_BOUND,
    VALUE_RECONSTRUCTED,
    assess,
    classify,
)


class TestQuality:
    def test_full_coverage_is_exact(self):
        assert classify(1.0, 40.0) == QUALITY_EXACT

    def test_partial_coverage_above_threshold(self):
        assert classify(0.85, 40.0) == QUALITY_PARTIAL

    def test_missing_data_with_sun_up_is_missing(self):
        assert classify(0.1, 40.0) == QUALITY_MISSING

    def test_missing_data_with_sun_down_is_night(self):
        """Dawn flicker between 0.0 and unavailable is information, not a gap."""
        assert classify(0.1, 1.0) == QUALITY_NIGHT

    def test_full_coverage_with_sun_down_is_still_night(self):
        """An inverter that stays awake covers the dark hours perfectly.

        Two strings on the same plant behaved differently overnight: the ones
        that went unavailable were called night, the ones that kept reporting
        a steady zero were called ``exact``.  Same darkness, opposite labels,
        and the second one travelled downstream as a daylight hour -- read as
        an anomaly by learning, as a stalled learner by the health check, and
        as an errorless hour by the score.
        """
        assert classify(1.0, 1.0) == QUALITY_NIGHT
        assert assess(1.0, 1.0).quality == QUALITY_NIGHT

    def test_missing_hours_carry_no_learning_weight(self):
        assert assess(0.1, 40.0).usable_for_learning is False

    def test_partial_hours_are_weighted_by_coverage(self):
        result = assess(0.85, 40.0)
        assert result.weight == pytest.approx(0.85)
        assert result.usable_for_learning is True

    def test_censored_observations_are_down_weighted(self):
        assert assess(1.0, 40.0, VALUE_LOWER_BOUND).weight == pytest.approx(0.5)
        assert assess(1.0, 40.0, VALUE_RECONSTRUCTED).weight == pytest.approx(0.35)


class TestFixedLimit:
    """The statically configured cap, e.g. a balcony plant's legal 800 W.

    Nothing reports a persistent inverter limit as an entity, so it lives in
    the group configuration and applies on top of whatever the limit entities
    command -- both constraints hold at once, the lower one wins.
    """

    def _group(self, **kwargs) -> CurtailmentGroup:
        return CurtailmentGroup(group_id="g", name="Group", **kwargs)

    def test_no_fixed_limit_passes_the_entity_limit_through(self):
        assert self._group().effective_limit(2400.0) == 2400.0
        assert self._group().effective_limit(None) is None

    def test_fixed_limit_alone_is_the_limit(self):
        assert self._group(fixed_limit_w=800.0).effective_limit(None) == 800.0

    def test_the_lower_of_both_wins(self):
        """DTU commands 100 % of 2400 W hardware, but 800 W is set persistently."""
        group = self._group(fixed_limit_w=800.0)
        assert group.effective_limit(2400.0) == 800.0
        assert group.effective_limit(600.0) == 600.0

    def test_a_fixed_limit_counts_as_having_a_limit(self):
        assert self._group(fixed_limit_w=800.0).has_limit is True
        assert self._group().has_limit is False

    def test_an_unreadable_live_limit_declines_to_judge(self):
        """A configured limit entity with no reading is not "no limit".

        The live limit may have been below the static cap, so recording the
        cap would let the binding test clear measurements it cannot vouch for.
        """
        group = self._group(
            limit_abs_entity="number.limit_absolute", fixed_limit_w=800.0
        )
        assert group.effective_limit(None) is None
        assert group.effective_limit(600.0) == 600.0
        assert group.effective_limit(2400.0) == 800.0

    def test_a_non_positive_fixed_limit_is_rejected(self):
        with pytest.raises(ConfigError):
            self._group(fixed_limit_w=0.0)
        with pytest.raises(ConfigError):
            self._group(fixed_limit_w=-800.0)


class TestBinding:
    def test_limit_far_above_production_is_not_curtailment(self):
        """1796 W limit, 600 W available -- the measurement is exact."""
        assert curt.is_binding(600.0, 1796.0, 640.0) is False

    def test_running_into_the_limit_is_curtailment(self):
        assert curt.is_binding(1760.0, 1796.0, 2400.0) is True

    def test_at_the_limit_but_physics_agrees_is_not_curtailment(self):
        """Hitting the limit exactly when that is all the sun offers."""
        assert curt.is_binding(1760.0, 1796.0, 1800.0) is False

    def test_unknown_without_limit(self):
        assert curt.is_binding(600.0, None, 640.0) is None

    def test_unknown_without_physics(self):
        """Distinct from False: the collector must not pre-empt physics."""
        assert curt.is_binding(600.0, 1796.0, None) is None

    def test_zero_limit_censors_everything(self):
        assert curt.is_binding(5.0, 0.0, 800.0) is True

    def test_value_kind_follows_binding(self):
        assert curt.value_kind_for(True) == VALUE_LOWER_BOUND
        assert curt.value_kind_for(False) == "measured"
        assert curt.value_kind_for(None) == "measured"


class TestChargerState:
    """Some controllers say outright that they have stopped chasing the sun."""

    def test_voltage_holding_states_are_limiting(self):
        for state in ("Absorption", "Float", "Equalize", "External Control"):
            assert curt.charger_is_limiting(state) is True, state

    def test_bulk_is_the_controller_taking_everything(self):
        assert curt.charger_is_limiting("Bulk") is False

    def test_case_and_padding_do_not_matter(self):
        assert curt.charger_is_limiting("  float  ") is True
        assert curt.charger_is_limiting("BULK") is False

    def test_absent_or_unusable_states_give_no_verdict(self):
        """Most inverters expose nothing here, and nothing may change for them."""
        for state in (None, "", "unknown", "unavailable"):
            assert curt.charger_is_limiting(state) is None, repr(state)

    def test_an_unfamiliar_word_is_not_read_as_permission(self):
        """Other makes word this differently; "not limiting" must be earned."""
        assert curt.charger_is_limiting("Whatever-Mode") is None

    def test_off_and_fault_are_not_limiting(self):
        for state in ("Off", "Fault"):
            assert curt.charger_is_limiting(state) is False, state


class TestFullBattery:
    """A battery-coupled group throttles without commanding anything.

    Nothing in the data says so: the battery stops accepting charge, the
    inverter backs off, the strings follow. Left undetected it is learned as
    genuine underperformance every sunny afternoon -- at the same sun
    positions, which is what the sky map reads as a permanent obstruction.
    """

    def test_full_battery_holding_the_strings_back_is_curtailment(self):
        """Physics offers 1500 W, the capped path passes 790 W."""
        assert curt.full_battery_binding(790.0, 1500.0, 100.0, 100.0) is True

    def test_a_charging_battery_never_censors(self):
        """The best hours of the day must stay learnable."""
        assert curt.full_battery_binding(1500.0, 1500.0, 62.0, 100.0) is False

    def test_a_full_battery_on_a_dim_afternoon_is_not_curtailment(self):
        """Full, but physics agrees with what came in -- nothing was held back."""
        assert curt.full_battery_binding(400.0, 420.0, 100.0, 100.0) is False

    def test_the_threshold_is_configurable(self):
        """A BMS that calls 98 % full should be believed at 98 %."""
        assert curt.full_battery_binding(790.0, 1500.0, 98.0, 98.0) is True
        assert curt.full_battery_binding(790.0, 1500.0, 97.0, 98.0) is False

    def test_unknown_state_of_charge_censors_nothing(self):
        """Distinct from False -- guessing here would censor on no evidence."""
        assert curt.full_battery_binding(790.0, 1500.0, None, 100.0) is None

    def test_it_combines_with_a_commanded_limit(self):
        """Either mechanism alone is enough to make the reading a lower bound."""
        assert curt.combine_binding(False, True) is True
        assert curt.combine_binding(None, True) is True
        assert curt.combine_binding(None, None) is None


class TestPeerReconstruction:
    def _peer(self, **kwargs):
        base = dict(
            string_id="s1", measured_w=800.0, physics_w=1000.0, binding=False
        )
        base.update(kwargs)
        return curt.PeerSample(**base)

    def test_reconstructs_from_a_free_peer(self):
        result = curt.reconstruct_from_peers(
            target_physics_w=900.0,
            target_nameplate_w=1000.0,
            peers=[self._peer()],
            sun_elevation_deg=35.0,
        )
        assert result is not None
        assert result.value_w == pytest.approx(720.0)
        assert result.value_kind == VALUE_RECONSTRUCTED
        assert result.weight <= 0.5

    def test_low_sun_blocks_reconstruction(self):
        assert (
            curt.reconstruct_from_peers(900.0, 1000.0, [self._peer()], 5.0) is None
        )

    def test_curtailed_peer_is_no_reference(self):
        assert (
            curt.reconstruct_from_peers(
                900.0, 1000.0, [self._peer(binding=True)], 35.0
            )
            is None
        )

    def test_shaded_peer_is_no_reference(self):
        assert (
            curt.reconstruct_from_peers(
                900.0, 1000.0, [self._peer(shaded=True)], 35.0
            )
            is None
        )

    def test_disagreeing_peers_produce_nothing(self):
        peers = [
            self._peer(string_id="a", measured_w=900.0),
            self._peer(string_id="b", measured_w=200.0),
        ]
        assert curt.reconstruct_from_peers(900.0, 1000.0, peers, 35.0) is None

    def test_barely_loaded_target_is_skipped(self):
        assert curt.reconstruct_from_peers(50.0, 1000.0, [self._peer()], 35.0) is None

    def test_everything_curtailed_yields_no_point_value(self):
        """Summer midday, battery full, load covered: a limit of the method."""
        peers = [self._peer(string_id="a", binding=True), self._peer(string_id="b", binding=True)]
        assert curt.reconstruct_from_peers(900.0, 1000.0, peers, 45.0) is None
        assert curt.group_fully_curtailed([True, True]) is True
        assert curt.group_fully_curtailed([True, None]) is True
        assert curt.group_fully_curtailed([True, False]) is False
        assert curt.group_fully_curtailed([None, None]) is False

    def test_curtailed_fraction_ignores_unknowns(self):
        assert curt.curtailed_fraction([True, False, None, True]) == pytest.approx(
            2 / 3
        )
