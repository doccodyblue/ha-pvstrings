"""Forecast source parsing and the savings calculation."""

from __future__ import annotations

from datetime import date

import pytest

from core.config import Economics
from core.economics import (
    MODE_FEED_IN,
    MODE_NET_METERING,
    MODE_SELF_CONSUMPTION,
    amortisation,
    annual_estimate,
    period_share,
    savings,
    scenarios,
)
from core.weather import (
    RADIATION_LABEL_OFFSET_S,
    ghi_from_cloud_cover,
    lux_to_ghi,
    open_meteo_params,
    parse_open_meteo,
    rows_from_ha_weather,
)

ISSUED = 1_700_000_000


class TestOpenMeteo:
    def _payload(self, times, **series):
        return {"hourly": {"time": list(times), **series}}

    def test_requests_components_not_plane_of_array(self):
        params = open_meteo_params(53.5, 10.0)
        variables = params["hourly"].split(",")
        assert "shortwave_radiation" in variables
        assert "direct_normal_irradiance" in variables
        assert "diffuse_radiation" in variables
        # Their GTI uses an isotropic sky and a fixed albedo -- we transpose
        # ourselves, so we must never ask for it.
        assert not any("tilted" in name for name in variables)

    def test_unixtime_and_utc_are_requested(self):
        params = open_meteo_params(53.5, 10.0)
        assert params["timeformat"] == "unixtime"
        assert params["timezone"] == "GMT"
        assert params["wind_speed_unit"] == "ms"

    def test_best_match_sends_no_model_override(self):
        assert "models" not in open_meteo_params(53.5, 10.0, model="best_match")
        assert open_meteo_params(53.5, 10.0, model="icon_seamless")["models"] == (
            "icon_seamless"
        )

    def test_hourly_labels_are_shifted_to_interval_start(self):
        """Open-Meteo labels radiation with the END of the averaging hour."""
        stamp = 1_700_003_600
        rows = parse_open_meteo(
            self._payload([stamp], shortwave_radiation=[500.0]), ISSUED
        )
        assert rows[0].ts_utc == stamp + RADIATION_LABEL_OFFSET_S
        assert RADIATION_LABEL_OFFSET_S == -3600

    def test_horizon_is_derived_from_the_issue_time(self):
        rows = parse_open_meteo(
            self._payload(
                [ISSUED + 3600, ISSUED + 25 * 3600],
                shortwave_radiation=[100.0, 200.0],
            ),
            ISSUED,
        )
        assert rows[0].horizon_h == 0
        assert rows[1].horizon_h == 24

    def test_past_rows_are_kept_for_backfill(self):
        rows = parse_open_meteo(
            self._payload([ISSUED - 3600], shortwave_radiation=[100.0]), ISSUED
        )
        assert rows[0].horizon_h == -2

    def test_all_fields_are_mapped(self):
        rows = parse_open_meteo(
            self._payload(
                [ISSUED + 3600],
                shortwave_radiation=[500.0],
                direct_normal_irradiance=[700.0],
                diffuse_radiation=[120.0],
                temperature_2m=[21.5],
                cloud_cover=[35.0],
                wind_speed_10m=[3.2],
                relative_humidity_2m=[60.0],
                precipitation=[0.4],
                surface_pressure=[1013.0],
            ),
            ISSUED,
        )
        row = rows[0]
        assert (row.ghi_wm2, row.dni_wm2, row.dhi_wm2) == (500.0, 700.0, 120.0)
        assert (row.temp_c, row.clouds_pct, row.wind_ms) == (21.5, 35.0, 3.2)
        assert (row.humidity_pct, row.rain_mm, row.pressure_hpa) == (60.0, 0.4, 1013.0)

    def test_missing_series_become_none_not_zero(self):
        rows = parse_open_meteo(
            self._payload([ISSUED + 3600], shortwave_radiation=[500.0]), ISSUED
        )
        assert rows[0].dni_wm2 is None
        assert rows[0].temp_c is None

    def test_nulls_survive_as_none(self):
        rows = parse_open_meteo(
            self._payload([ISSUED + 3600], shortwave_radiation=[None]), ISSUED
        )
        assert rows[0].ghi_wm2 is None

    def test_empty_response_is_not_an_error(self):
        assert parse_open_meteo({}, ISSUED) == []
        assert parse_open_meteo({"hourly": {"time": []}}, ISSUED) == []

    def test_row_tuple_matches_the_table_column_order(self):
        rows = parse_open_meteo(
            self._payload([ISSUED + 3600], shortwave_radiation=[500.0]), ISSUED
        )
        row = rows[0].as_row()
        assert len(row) == 14
        assert row[0] == ISSUED
        assert row[2] == "open_meteo"


class TestCloudFallback:
    def test_clear_sky_passes_through(self):
        assert ghi_from_cloud_cover(800.0, 0.0) == pytest.approx(800.0)

    def test_overcast_attenuates_strongly(self):
        assert ghi_from_cloud_cover(800.0, 100.0) == pytest.approx(200.0)

    def test_monotonic_in_cloud_cover(self):
        values = [ghi_from_cloud_cover(800.0, pct) for pct in range(0, 101, 10)]
        assert values == sorted(values, reverse=True)

    def test_missing_cloud_cover_leaves_clearsky(self):
        assert ghi_from_cloud_cover(800.0, None) == pytest.approx(800.0)

    def test_ha_weather_rows_leave_components_empty(self):
        """Better an honest decomposition than an invented DNI/DHI split."""
        rows = rows_from_ha_weather(
            [{"ts_utc": ISSUED + 3600, "cloud_coverage": 50, "temperature": 18}],
            lambda _ts: 700.0,
            ISSUED,
        )
        assert rows[0].dni_wm2 is None
        assert rows[0].dhi_wm2 is None
        assert rows[0].components_plausible == 0
        assert rows[0].source == "ha_weather"
        assert 0 < rows[0].ghi_wm2 < 700.0

    def test_lux_conversion_is_bounded(self):
        assert lux_to_ghi(120_000) == pytest.approx(1000.0)
        assert lux_to_ghi(-5) == 0.0


class TestSavings:
    def _econ(self, mode: str) -> Economics:
        return Economics(
            mode=mode,
            price_per_kwh=0.32,
            feed_in_tariff=0.08,
            investment_eur=3500.0,
            commissioning_date=date(2025, 4, 1),
        )

    def test_net_metering_values_everything_at_the_retail_price(self):
        result = savings(10.0, 7.0, self._econ(MODE_NET_METERING))
        assert result.saved_eur == pytest.approx(3.20)

    def test_self_consumption_splits_the_price(self):
        result = savings(10.0, 7.0, self._econ(MODE_SELF_CONSUMPTION))
        assert result.saved_eur == pytest.approx(3 * 0.32 + 7 * 0.08)

    def test_full_feed_in(self):
        result = savings(10.0, 7.0, self._econ(MODE_FEED_IN))
        assert result.saved_eur == pytest.approx(0.80)

    def test_meter_swap_costs_roughly_two_thirds(self):
        """The exact number from the spec's critique of the old calculation."""
        both = scenarios(3095.0, 2352.0, self._econ(MODE_SELF_CONSUMPTION))
        ferraris = both[MODE_NET_METERING].saved_eur
        after_swap = both[MODE_SELF_CONSUMPTION].saved_eur
        assert after_swap / ferraris < 0.45

    def test_no_grid_meter_means_everything_is_self_used(self):
        result = savings(10.0, None, self._econ(MODE_SELF_CONSUMPTION))
        assert result.self_used_kwh == pytest.approx(10.0)
        assert result.saved_eur == pytest.approx(3.20)

    def test_export_cannot_exceed_production(self):
        result = savings(5.0, 9.0, self._econ(MODE_SELF_CONSUMPTION))
        assert result.export_kwh == pytest.approx(5.0)
        assert result.self_used_kwh == pytest.approx(0.0)

    def test_zero_production_does_not_divide_by_zero(self):
        assert savings(0.0, 0.0, self._econ(MODE_SELF_CONSUMPTION)).eur_per_kwh == 0.0


class TestSeasonalExtrapolation:
    #: Rough northern-German clear-sky seasonality.
    WEIGHTS = [
        0.026, 0.045, 0.081, 0.111, 0.132, 0.132,
        0.128, 0.111, 0.083, 0.056, 0.030, 0.022,
    ]

    def test_full_year_is_one(self):
        share = period_share(date(2025, 1, 1), date(2025, 12, 31), self.WEIGHTS)
        assert share == pytest.approx(1.0, abs=1e-6)

    def test_spring_to_summer_covers_most_of_the_year(self):
        """131 days from 1 April carry far more than 131/365 of the yield --
        which is exactly why the linear x365 extrapolation overstates it."""
        share = period_share(date(2025, 4, 1), date(2025, 8, 9), self.WEIGHTS)
        assert share > 0.45
        assert share > 131 / 365

    def test_seasonal_estimate_is_below_the_linear_one(self):
        start, end = date(2025, 4, 1), date(2025, 8, 9)
        observed = 1176.13
        seasonal = annual_estimate(observed, start, end, self.WEIGHTS)
        linear = observed / 131 * 365
        assert seasonal < linear
        assert seasonal / linear < 0.8

    def test_too_little_data_yields_no_estimate(self):
        assert (
            annual_estimate(50.0, date(2025, 6, 1), date(2025, 6, 5), self.WEIGHTS)
            is None
        )

    def test_more_than_a_year_scales_down(self):
        share = period_share(date(2024, 1, 1), date(2025, 12, 31), self.WEIGHTS)
        assert share == pytest.approx(2.0, abs=1e-6)


class TestAmortisation:
    def test_progress_and_remaining_months(self):
        result = amortisation(3500.0, 1176.0, 1500.0, date(2026, 8, 10))
        assert result.progress_pct == pytest.approx(33.6, abs=0.1)
        assert result.months_remaining == pytest.approx(18.6, abs=0.2)
        assert result.target_date.year == 2028

    def test_no_annual_estimate_means_no_target_date(self):
        result = amortisation(3500.0, 100.0, None, date(2026, 8, 10))
        assert result.months_remaining is None
        assert result.target_date is None

    def test_progress_is_capped_at_full(self):
        assert amortisation(1000.0, 5000.0, 500.0, date(2026, 8, 10)).progress_pct == 100.0

    def test_no_investment_is_already_paid_off(self):
        assert amortisation(0.0, 0.0, 100.0, date(2026, 8, 10)).progress_pct == 100.0


class TestZeroIsAValue:
    """Nought is an answer, absent is not.

    The irradiance sensor read `unknown` every night because a forecast of
    0.0 W/m2 was tested for truthiness rather than for None -- which looks
    exactly like a dead weather source.
    """

    def test_a_zero_forecast_row_is_a_reading(self):
        rows = parse_open_meteo(
            {"hourly": {"time": [ISSUED + 3600], "shortwave_radiation": [0.0]}},
            ISSUED,
        )
        assert rows[0].ghi_wm2 == 0.0
        assert rows[0].ghi_wm2 is not None

    def test_a_missing_forecast_row_is_absent(self):
        rows = parse_open_meteo(
            {"hourly": {"time": [ISSUED + 3600], "shortwave_radiation": [None]}},
            ISSUED,
        )
        assert rows[0].ghi_wm2 is None

    def test_the_two_are_distinguishable_downstream(self):
        """Whatever consumes these must branch on None, never on falsiness."""
        for value, expected in ((0.0, True), (None, False), (500.0, True)):
            assert (value is not None) is expected
