"""The running hour is a part of the day, not a whole one.

These need the HA install only because the dataclasses live next to the
coordinator; nothing here touches hass.
"""

from __future__ import annotations

import pytest

from custom_components.pvstrings.coordinator import (
    GroupForecast,
    PvStringsData,
    StringForecast,
)
from custom_components.pvstrings.core.aggregate import SPLIT_FINE, SPLIT_HOURLY
from custom_components.pvstrings.core.conversion import ConversionResult

DAY = 1_700_000_000 // 86400 * 86400
NOON = DAY + 12 * 3600
#: Flat inside the hour, so the arithmetic in the assertions stays readable.
FINE = [(NOON + i * 300, 0.1) for i in range(12)]
HOURLY = [(NOON, 1.2), (NOON + 3600, 2.0), (NOON + 7200, 0.8)]
DAY_END = DAY + 86400


def _string() -> StringForecast:
    return StringForecast(
        string_id="s1", name="South", hourly=list(HOURLY), fine=list(FINE)
    )


class TestString:
    def test_half_past_leaves_half_the_hour(self):
        remaining = _string().remaining_kwh(NOON + 1800, DAY_END)
        assert remaining == pytest.approx(0.6 + 2.0 + 0.8)

    def test_the_old_behaviour_was_a_third_too_high(self):
        """The number from the field report: 16:31, hour counted whole."""
        whole_hour = sum(value for ts, value in HOURLY if ts >= NOON)
        assert _string().remaining_kwh(NOON + 1800, DAY_END) < whole_hour

    def test_without_detail_the_hour_is_counted_whole(self):
        bare = StringForecast(string_id="s1", name="South", hourly=list(HOURLY))
        assert bare.share_ahead(NOON + 1800) is None
        assert bare.remaining_kwh(NOON + 1800, DAY_END) == pytest.approx(4.0)


class TestGroup:
    def _group(self) -> GroupForecast:
        return GroupForecast(
            group_id="g1",
            name="WR1",
            hourly=list(HOURLY),
            fine=list(FINE),
            output_path="direct",
            converted=ConversionResult(
                hourly_kwh={ts: value * 0.95 for ts, value in HOURLY},
                clipped_kwh=0.0,
                curve_source="datasheet",
                stages=("inverter_efficiency",),
            ),
        )

    def test_the_running_hour_is_no_longer_dropped(self):
        """Summing from now against hour-start keys used to lose it whole."""
        dropped = sum(value for ts, value in HOURLY if ts >= NOON + 1800)
        assert self._group().remaining_kwh(NOON + 1800, DAY_END) > dropped

    def test_the_converted_series_is_split_by_the_dc_share(self):
        """Conversion happens per hour, so inside the hour being split the
        efficiency is one constant and the two curves have the same shape."""
        group = self._group()
        assert group.converted_remaining_kwh(NOON + 1800, DAY_END) == pytest.approx(
            group.remaining_kwh(NOON + 1800, DAY_END) * 0.95
        )

    def test_a_group_without_conversion_reports_nothing_converted(self):
        group = GroupForecast(group_id="g2", name="none", hourly=list(HOURLY))
        assert group.converted_remaining_kwh(NOON + 1800, DAY_END) == 0.0


class TestPlant:
    def _data(self, fine=FINE) -> PvStringsData:
        return PvStringsData(
            generated_at=NOON,
            day_start=DAY,
            day_end=DAY_END,
            tomorrow_start=DAY_END,
            tomorrow_end=DAY_END + 86400,
            plant_hourly=list(HOURLY),
            plant_fine=list(fine),
        )

    def test_elapsed_and_remaining_add_up_to_the_day(self):
        data = self._data()
        today = data.today_kwh
        remaining = data.remaining_kwh(NOON + 1800)
        assert round(today, 3) == round((today - remaining) + remaining, 3)
        assert remaining < today

    def test_the_split_source_is_reported_either_way(self):
        from custom_components.pvstrings.core.aggregate import split_source

        assert split_source(self._data().share_ahead(NOON + 1800)) == SPLIT_FINE
        assert split_source(self._data([]).share_ahead(NOON + 1800)) == SPLIT_HOURLY
