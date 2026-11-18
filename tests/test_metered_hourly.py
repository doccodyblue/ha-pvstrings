"""The plant forecast published to the HA Energy dashboard."""

from __future__ import annotations

import pytest

from core.aggregate import GroupOutput, metered_hourly

H0, H1, H2 = 3600 * 100, 3600 * 101, 3600 * 102


def _as_dict(series):
    return dict(series)


class TestMeteredHourly:
    def test_ungrouped_strings_count_as_dc(self):
        out = metered_hourly(
            {"a": [(H0, 1.0), (H1, 2.0)], "b": [(H1, 0.5)]},
            {"a": None, "b": None},
            {},
        )
        assert _as_dict(out) == {H0: 1.0, H1: 2.5}

    def test_direct_group_counts_its_ac_not_the_members_dc(self):
        out = metered_hourly(
            {"a": [(H0, 1.0)], "b": [(H0, 1.0)]},
            {"a": "g", "b": "g"},
            {"g": GroupOutput("direct", ac_hourly={H0: 1.9})},
        )
        assert _as_dict(out) == {H0: 1.9}

    def test_storage_group_stops_at_the_charge_controller(self):
        out = metered_hourly(
            {"a": [(H0, 2.0)]},
            {"a": "g"},
            {"g": GroupOutput("storage", mppt_efficiency=0.97)},
        )
        assert _as_dict(out)[H0] == pytest.approx(1.94)

    def test_storage_without_mppt_efficiency_is_dc(self):
        out = metered_hourly(
            {"a": [(H0, 2.0)]}, {"a": "g"}, {"g": GroupOutput("storage")}
        )
        assert _as_dict(out) == {H0: 2.0}

    def test_path_none_and_unconverted_direct_fall_back_to_dc(self):
        out = metered_hourly(
            {"a": [(H0, 1.0)], "b": [(H0, 2.0)]},
            {"a": "none", "b": "direct"},
            {
                "none": GroupOutput("none"),
                "direct": GroupOutput("direct", ac_hourly=None),
            },
        )
        assert _as_dict(out) == {H0: 3.0}

    def test_unknown_group_id_falls_back_to_dc(self):
        out = metered_hourly({"a": [(H0, 1.0)]}, {"a": "gone"}, {})
        assert _as_dict(out) == {H0: 1.0}

    def test_mixed_plant_counts_every_string_once(self):
        """Andy's layout: storage group, direct group, one loose string."""
        out = metered_hourly(
            {
                "s1": [(H0, 1.0)],
                "s2": [(H0, 1.0)],
                "h1": [(H0, 0.5)],
                "h2": [(H0, 0.5)],
                "loose": [(H0, 0.25)],
            },
            {"s1": "sp", "s2": "sp", "h1": "net", "h2": "net", "loose": None},
            {
                "sp": GroupOutput("storage", mppt_efficiency=0.97),
                "net": GroupOutput("direct", ac_hourly={H0: 0.95}),
            },
        )
        assert _as_dict(out)[H0] == pytest.approx(2 * 0.97 + 0.95 + 0.25)

    def test_missing_hours_stay_missing(self):
        """A weather gap must not turn into a confident zero."""
        out = metered_hourly(
            {"a": [(H0, 1.0), (H2, 1.0)]}, {"a": None}, {}
        )
        assert [ts for ts, _ in out] == [H0, H2]

    def test_sorted_and_never_negative(self):
        out = metered_hourly(
            {"a": [(H1, -0.2), (H0, 1.0)]}, {"a": None}, {}
        )
        assert out == [(H0, 1.0), (H1, 0.0)]
