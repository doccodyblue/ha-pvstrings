"""Forecast source parsing and the savings calculation."""

from __future__ import annotations

from datetime import date

import pytest

from core.config import Economics
from core.economics import (
    BASIS_CONFIGURED,
    BASIS_CURVE,
    BASIS_DC,
    BASIS_MEASURED,
    MODE_FEED_IN,
    MODE_NET_METERING,
    MODE_SELF_CONSUMPTION,
    REFUSED_IMPLAUSIBLE,
    REFUSED_TOO_FEW,
    DeliveryFactor,
    amortisation,
    annual_estimate,
    delivered,
    delivery_factor,
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

    def test_row_tuple_matches_the_table_column_order(self, store):
        """Checked against the real table, not against a number.

        The tuple is positional and the insert names its columns in the same
        order, so a field added in one place and not the other slides every
        later value one column across -- pressure into components_plausible --
        without raising anything a test would notice. Reading the arity off
        the schema means the guard cannot itself go stale.
        """
        columns = [
            row[1]
            for row in store._query("PRAGMA table_info(weather_forecast)", ())
        ]
        rows = parse_open_meteo(
            self._payload([ISSUED + 3600], shortwave_radiation=[500.0]), ISSUED
        )
        row = rows[0].as_row()
        assert len(row) == len(columns)
        assert columns[0] == "issued_at_utc" and row[0] == ISSUED
        assert columns[2] == "source" and row[2] == "open_meteo"

    def test_rain_probability_is_parsed(self):
        rows = parse_open_meteo(
            self._payload(
                [ISSUED + 3600],
                shortwave_radiation=[500.0],
                precipitation_probability=[80.0],
            ),
            ISSUED,
        )
        assert rows[0].rain_probability_pct == 80.0

    def test_a_source_without_it_leaves_it_absent(self):
        """None, not zero -- "not offered" and "certainly dry" are different."""
        rows = parse_open_meteo(
            self._payload([ISSUED + 3600], shortwave_radiation=[500.0]), ISSUED
        )
        assert rows[0].rain_probability_pct is None

    def test_it_is_requested_from_the_source(self):
        variables = open_meteo_params(53.5, 10.0)["hourly"].split(",")
        assert "precipitation_probability" in variables


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


class TestHaWeatherCarriesRainProbability:
    """The fallback source has the field too, and an install without internet
    is exactly the one that cannot fall back to anything else."""

    def _entry(self, **extra):
        base = {"ts_utc": ISSUED + 3600, "cloud_coverage": 40, "temperature": 18}
        base.update(extra)
        return [base]

    def test_it_is_mapped(self):
        rows = rows_from_ha_weather(
            self._entry(precipitation_probability=75), lambda _ts: 700.0, ISSUED
        )
        assert rows[0].rain_probability_pct == 75

    def test_an_entry_without_it_stays_absent(self):
        rows = rows_from_ha_weather(self._entry(), lambda _ts: 700.0, ISSUED)
        assert rows[0].rain_probability_pct is None

    def test_it_reaches_the_outlook(self, store):
        """End to end: a fallback install must get a usable sensor, not None."""
        rows = rows_from_ha_weather(
            self._entry(precipitation_probability=60), lambda _ts: 700.0, ISSUED
        )
        store.upsert_weather_forecast([row.as_row() for row in rows])
        out = store.weather_outlook(ISSUED, ISSUED + 7200, "ha_weather")
        assert out["rain_probability_pct"] == 60.0

class TestAmortisationDoesNotProjectNonsense:
    """Reported from the field: OverflowError on every coordinator refresh.

    A plant commissioned months before this integration was installed has its
    recorded savings scaled up over the whole period since commissioning, not
    over the period anything was measured. The annual figure comes out tiny,
    the projected amortisation runs to tens of thousands of months, and the
    date arithmetic overflows -- taking down the refresh, not just the sensor.
    """

    def test_an_absurd_projection_is_declined_rather_than_dated(self):
        result = amortisation(1800.0, 12.0, 0.5, date(2026, 8, 17))
        assert result.target_date is None
        assert result.months_remaining is None
        # The parts that are still knowable stay knowable.
        assert result.progress_pct == pytest.approx(12.0 / 1800.0 * 100, abs=0.01)
        assert result.annual_saving_eur == 0.5

    def test_a_rate_that_never_pays_off_does_not_raise(self):
        """The exact report: a hundredth of a euro a year used to overflow."""
        result = amortisation(1800.0, 12.0, 0.01, date(2026, 8, 17))
        assert result.target_date is None

    def test_a_plausible_projection_is_still_dated(self):
        result = amortisation(1800.0, 200.0, 200.0, date(2026, 8, 17))
        assert result.target_date is not None
        assert result.target_date.year == 2034

    def test_the_boundary_is_a_century(self):
        just_inside = amortisation(1200.0, 0.0, 12.0, date(2026, 8, 17))
        assert just_inside.months_remaining == pytest.approx(1200.0, abs=1)
        assert just_inside.target_date is not None
        just_outside = amortisation(1300.0, 0.0, 12.0, date(2026, 8, 17))
        assert just_outside.target_date is None


class TestDeliveryFactor:
    """Which rung of the evidence ladder a group ends up on, and why."""

    def test_measurement_beats_the_curve(self):
        result = delivery_factor(
            "direct", measured=(1000.0, 940.0, 500), curve_factor=0.96
        )
        assert result == DeliveryFactor(0.94, BASIS_MEASURED, 500, 0.94, None)

    def test_too_few_pairs_fall_back_to_the_curve(self):
        result = delivery_factor(
            "direct", measured=(1000.0, 940.0, 12), curve_factor=0.96
        )
        assert (result.factor, result.basis) == (0.96, BASIS_CURVE)

    def test_a_refused_measurement_stays_visible(self):
        """A group sitting on its curve with an AC sensor wired up is a
        question somebody will ask."""
        result = delivery_factor(
            "direct", measured=(1000.0, 940.0, 12), curve_factor=0.96
        )
        assert result.measured_ratio == 0.94
        assert result.samples == 12
        assert result.refused == REFUSED_TOO_FEW

    def test_an_impossible_measurement_is_refused_not_applied(self):
        """A mis-scaled AC sensor reading 1.4x would otherwise pay for energy
        nobody produced."""
        result = delivery_factor(
            "direct", measured=(1000.0, 1400.0, 900), curve_factor=0.96
        )
        assert (result.factor, result.basis) == (0.96, BASIS_CURVE)
        assert result.refused == REFUSED_IMPLAUSIBLE
        assert result.measured_ratio == 1.4

    def test_no_curve_and_no_evidence_stays_at_dc(self):
        assert delivery_factor("direct") == DeliveryFactor(1.0, BASIS_DC)

    def test_a_refused_measurement_without_a_curve_still_stays_at_dc(self):
        result = delivery_factor("direct", measured=(1000.0, 300.0, 900))
        assert (result.factor, result.basis) == (1.0, BASIS_DC)
        assert result.refused == REFUSED_IMPLAUSIBLE

    def test_storage_counts_charge_and_discharge(self):
        result = delivery_factor("storage", configured_factor=0.97 * 0.96 * 0.96)
        assert result.basis == BASIS_CONFIGURED
        assert result.factor == pytest.approx(0.894, abs=5e-4)

    def test_a_group_without_a_path_is_untouched(self):
        assert delivery_factor("none") == DeliveryFactor(1.0, BASIS_DC)

    def test_a_measured_storage_path_does_not_exist(self):
        """Charge efficiency is not a two-port and is never measured; passing
        pairs in must not promote the storage path to 'measured'."""
        result = delivery_factor(
            "storage", measured=(1000.0, 940.0, 900), configured_factor=0.9
        )
        assert result.basis == BASIS_CONFIGURED


class TestDelivered:
    FACTORS = {
        "s1": DeliveryFactor(0.95, BASIS_MEASURED, 800),
        "s2": DeliveryFactor(0.9, BASIS_CONFIGURED),
    }

    def test_each_string_is_converted_on_its_own_path(self):
        result = delivered({"s1": 10.0, "s2": 5.0}, self.FACTORS)
        assert result.kwh == pytest.approx(9.5 + 4.5)
        assert result.dc_kwh == pytest.approx(15.0)

    def test_the_basis_split_says_what_rests_on_a_measurement(self):
        result = delivered({"s1": 10.0, "s2": 5.0}, self.FACTORS)
        assert result.by_basis == {BASIS_MEASURED: 9.5, BASIS_CONFIGURED: 4.5}

    def test_an_unknown_string_counts_at_dc_rather_than_vanishing(self):
        """History outlives configuration: a string removed from the setup
        still has energy in the database, and dropping it would shrink the
        lifetime figure instead of merely leaving it uncorrected."""
        result = delivered({"s1": 10.0, "gone": 4.0}, self.FACTORS)
        assert result.kwh == pytest.approx(13.5)
        assert result.by_basis[BASIS_DC] == pytest.approx(4.0)

    def test_nothing_produced_is_not_a_division(self):
        assert delivered({}, self.FACTORS).factor == 1.0


class TestSavingsRunOnDeliveredEnergy:
    def _econ(self, mode=MODE_SELF_CONSUMPTION):
        return Economics(
            mode=mode,
            price_per_kwh=0.32,
            feed_in_tariff=0.08,
            investment_eur=3500.0,
            commissioning_date=date(2025, 4, 1),
        )

    def test_conversion_losses_are_not_paid_for(self):
        """The whole point of the delivery layer: 5 % of DC energy never
        reaches a socket and must not earn the retail price."""
        dc = savings(10.0, None, self._econ()).saved_eur
        ac = savings(delivered({"s1": 10.0}, {"s1": DeliveryFactor(0.95, BASIS_MEASURED)}).kwh,
                     None, self._econ()).saved_eur
        assert ac == pytest.approx(dc * 0.95)

    def test_both_sides_of_the_split_are_now_ac(self):
        """self_used = delivered - exported: the exported half comes off the
        grid meter, so the other half has to be on the same side of the
        inverter or the split is a unit mix."""
        result = savings(9.5, 7.0, self._econ())
        assert result.self_used_kwh == pytest.approx(2.5)
        assert result.saved_eur == pytest.approx(2.5 * 0.32 + 7 * 0.08)
