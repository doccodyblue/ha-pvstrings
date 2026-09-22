"""The irradiance sensor's own report card.

Cheap stations measure lux and divide by a constant, so they read low, and
lower as the sun drops and its light reddens. These tests pin the three shapes
the diagnosis has to tell apart -- a flat offset, a spectral curve, and a
sensor that is not level -- because reporting the wrong one sends the owner up
a ladder for nothing, or leaves a real fault unfound.
"""

from __future__ import annotations

import pytest

from core.irradiance_check import (
    MIN_BAND_DAYS,
    MIN_REFERENCE_WM2,
    assess,
    rows_to_bank,
)
from core.physics import to_index

DAY = 86400
NOON = 1_750_000_000 // DAY * DAY + 12 * 3600


def pairs(
    ratio_by_elevation, days: int = 20, azimuth_of=lambda elevation: 170.0
):
    """Synthetic hours: one per elevation per day, at a stated ratio."""
    out = []
    for day in range(days):
        for index, (elevation, ratio) in enumerate(ratio_by_elevation):
            reference = 400.0
            out.append(
                {
                    "ts_utc": NOON + day * DAY + index * 3600,
                    "measured_wm2": reference * ratio,
                    "reference_wm2": reference,
                    "reference_src": "satellite_radiation_seamless",
                    "elevation_deg": elevation,
                    "azimuth_deg": azimuth_of(elevation),
                }
            )
    return out


class TestTheThreeShapes:
    def test_a_sensor_that_agrees_says_so(self):
        verdict = assess(pairs([(10, 1.0), (20, 1.0), (30, 1.0), (40, 1.0), (50, 1.0)]))
        assert verdict.ratio == pytest.approx(1.0)
        assert verdict.reading() == "agrees with the reference"

    def test_a_flat_offset_is_called_flat(self):
        """Every band equally low: a divisor or a calibration, not the sky."""
        verdict = assess(pairs([(10, 0.8), (20, 0.8), (30, 0.8), (40, 0.8), (50, 0.8)]))
        assert verdict.ratio == pytest.approx(0.8)
        assert verdict.slope == pytest.approx(0.0, abs=1e-9)
        assert (
            verdict.reading()
            == "reads low by about the same amount at every sun height"
        )

    def test_the_spectral_curve_is_recognised(self):
        """The reference plant's own shape, rounded: 0.59 at dawn, 0.78 high."""
        verdict = assess(
            pairs([(10, 0.59), (20, 0.67), (30, 0.73), (40, 0.75), (50, 0.78)])
        )
        assert verdict.slope > 0.08
        assert verdict.reading() == "reads low, and more so as the sun drops"

    def test_a_tilted_sensor_outranks_the_curve(self):
        """East-west disagreement is the finding worth acting on.

        It is also the only one with a remedy that is not "buy a better
        sensor", so it must not be buried under a spectral verdict -- the
        numbers can carry both shapes at once.
        """
        def azimuth(elevation):
            # Morning readings high, afternoon low, at matched elevations.
            return 120.0 if elevation in (20, 40) else 240.0

        verdict = assess(
            pairs(
                [(20, 0.95), (40, 0.95), (21, 0.70), (41, 0.70)],
                azimuth_of=azimuth,
            )
        )
        assert verdict.tilt_hint is not None
        assert abs(verdict.tilt_hint) >= 0.08
        assert "level" in verdict.reading()


class TestItDoesNotInvertTheDirection:
    """The sign of the error is read off the bands, never off the slope.

    A sensor reading high with a rising ratio has the same slope as one
    reading low with a rising ratio; calling both "reads low" tells the owner
    to look for dirt on a sensor that is over-reporting.
    """

    def test_a_high_reading_sensor_with_a_curve_is_not_called_low(self):
        verdict = assess(
            pairs([(10, 1.08), (20, 1.14), (30, 1.20), (40, 1.24), (50, 1.28)])
        )
        assert verdict.slope > 0.08
        assert verdict.reading() == "reads high, and more so as the sun drops"
        assert "reads low" not in verdict.reading()

    def test_a_high_flat_sensor_is_not_called_low(self):
        verdict = assess(pairs([(10, 1.2), (20, 1.2), (30, 1.2), (40, 1.2), (50, 1.2)]))
        assert (
            verdict.reading()
            == "reads high by about the same amount at every sun height"
        )


class TestAverageAgreementIsNotAgreement:
    def test_bands_that_cancel_out_are_not_called_agreement(self):
        """0.80 low and 1.20 high average to 1.00.

        The overall ratio alone would report agreement -- the one verdict that
        stops anybody looking further -- while the sensor is 20 percent wrong
        at both ends of the sky.
        """
        verdict = assess(pairs([(10, 0.8), (20, 0.9), (30, 1.0), (40, 1.1), (50, 1.2)]))
        assert verdict.ratio == pytest.approx(1.0)
        # Exact, because "reads low, and more so as the sun drops" would also
        # mention the sun dropping while naming the wrong direction: this
        # sensor is low at dawn and high at noon, and neither word alone fits.
        assert verdict.reading() == "crosses the reference, and more so as the sun drops"

    def test_a_bumpy_sensor_with_no_trend_says_it_has_none(self):
        """Ends that match, a middle that does not: no slope, no agreement."""
        verdict = assess(pairs([(10, 1.0), (20, 0.88), (30, 1.02), (40, 0.9), (50, 1.0)]))
        assert verdict.slope == pytest.approx(0.0, abs=1e-9)
        assert verdict.reading() == "uneven across the sky, with no clear trend"


class TestTheTiltTestDoesNotInventTilt:
    """East against west only where the sun stood equally high on both sides.

    The remedy this verdict implies is a ladder and a spirit level, so a false
    positive costs the owner an afternoon. Both traps below produced one.
    """

    @staticmethod
    def hour(ts, elevation, azimuth, ratio):
        return {
            "ts_utc": ts,
            "measured_wm2": 400.0 * ratio,
            "reference_wm2": 400.0,
            "reference_src": "era5",
            "elevation_deg": elevation,
            "azimuth_deg": azimuth,
        }

    def test_a_low_morning_is_not_compared_with_a_high_afternoon(self):
        """Otherwise the spectral curve itself reports as a tilt.

        Mornings recorded at 16 degrees and afternoons at 44 differ by the
        curve alone; pooling the whole 15-45 window turns that into a level
        problem the sensor does not have.
        """
        rows = []
        for day in range(20):
            rows.append(self.hour(NOON + day * DAY, 16.0, 100.0, 0.62))
            rows.append(self.hour(NOON + day * DAY + 3600, 44.0, 260.0, 0.80))
        verdict = assess(rows)
        assert verdict.tilt_hint is None
        assert "level" not in verdict.reading()

    @pytest.mark.parametrize("thin_azimuth", [100.0, 260.0])
    def test_one_thin_side_does_not_outvote_the_other(self, thin_azimuth):
        """Two days against four weeks is not a comparison, either way round.

        Parametrised because the two sides are separate code paths, and a
        check on one of them alone still lets the other raise a tilt from a
        stray afternoon.
        """
        fat_azimuth = 260.0 if thin_azimuth < 180.0 else 100.0
        rows = []
        for day in range(20):
            rows.append(self.hour(NOON + day * DAY, 21.0, fat_azimuth, 0.95))
        for day in range(2):
            rows.append(self.hour(NOON + day * DAY + 3600, 20.0, thin_azimuth, 0.60))
        verdict = assess(rows)
        assert verdict.tilt_hint is None
        assert "level" not in verdict.reading()

    def test_the_dawn_band_is_left_out_of_the_comparison(self):
        """Below 15 degrees the reference cannot resolve terrain or horizon.

        An east-west difference down there says more about the hill next door
        than about the sensor's bubble level, so it must not reach the
        verdict -- even when both sides bring weeks of evidence.
        """
        rows = []
        for day in range(20):
            rows.append(self.hour(NOON + day * DAY, 6.0, 100.0, 0.95))
            rows.append(self.hour(NOON + day * DAY + 3600, 6.0, 260.0, 0.55))
        verdict = assess(rows)
        assert verdict.tilt_hint is None
        assert "level" not in verdict.reading()


class TestTheSunOverhead:
    def test_ninety_degrees_lands_in_the_top_band(self):
        """The tropics reach it. Falling out of every band counts it nowhere."""
        verdict = assess(pairs([(90.0, 0.7)]))
        assert verdict.bands[-1].hours == 20
        assert verdict.hours == 20


class TestItRefusesToGuess:
    def test_nothing_is_claimed_without_evidence(self):
        verdict = assess(pairs([(30, 0.7)], days=2))
        assert verdict.reading() == "not enough evidence yet"
        assert all(not band.usable for band in verdict.bands)

    def test_hours_of_one_day_are_not_many_observations(self):
        """Twelve hours of one afternoon are one weather event.

        Evidence is counted in days as well as energy, so a single bright
        Sunday cannot produce a verdict about a sensor.
        """
        many_hours_one_day = []
        for hour in range(12):
            many_hours_one_day.append(
                {
                    "ts_utc": NOON + hour * 3600,
                    "measured_wm2": 280.0,
                    "reference_wm2": 400.0,
                    "reference_src": "era5",
                    "elevation_deg": 30.0,
                    "azimuth_deg": 180.0,
                }
            )
        verdict = assess(many_hours_one_day)
        assert verdict.days == 1
        assert not any(band.usable for band in verdict.bands)
        assert verdict.reading() == "not enough evidence yet"

    def test_dim_hours_are_filtered_by_the_reference(self):
        """Never by the measurement.

        Dropping hours where the *sensor* reads low would discard exactly the
        hours in which it reads lowest, and the curve would flatten itself
        out of existence.
        """
        dim = pairs([(30, 0.2)], days=MIN_BAND_DAYS + 5)
        for row in dim:
            row["reference_wm2"] = MIN_REFERENCE_WM2 - 1.0
            row["measured_wm2"] = 5.0
        assert assess(dim).hours == 0

        # The case that matters: plenty of reference energy, but the sensor
        # reports below the threshold. Filtering on the measurement would
        # throw exactly this hour away -- and this hour is the evidence.
        bright_reference = pairs([(30, 0.1)], days=MIN_BAND_DAYS + 5)
        for row in bright_reference:
            assert row["measured_wm2"] < MIN_REFERENCE_WM2
            assert row["reference_wm2"] > MIN_REFERENCE_WM2
        kept = assess(bright_reference)
        assert kept.hours > 0
        assert kept.ratio == pytest.approx(0.1)


class TestTheSums:
    def test_the_ratio_is_of_sums_not_a_mean_of_ratios(self):
        """A dim hour with a wild quotient must not outvote a bright one.

        Same mistake the source-bias estimator was rebuilt to stop making:
        what this measures is an energy error, and the number that describes
        it is the ratio of the totals.
        """
        rows = [
            {
                "ts_utc": NOON + day * DAY,
                "measured_wm2": 900.0,
                "reference_wm2": 1000.0,
                "reference_src": "era5",
                "elevation_deg": 40.0,
                "azimuth_deg": 180.0,
            }
            for day in range(MIN_BAND_DAYS + 5)
        ] + [
            {
                "ts_utc": NOON + day * DAY + 3600,
                "measured_wm2": 30.0,
                "reference_wm2": 100.0,
                "reference_src": "era5",
                "elevation_deg": 40.0,
                "azimuth_deg": 180.0,
            }
            for day in range(MIN_BAND_DAYS + 5)
        ]
        verdict = assess(rows)
        # Sums: 930 / 1100 = 0.845. Mean of quotients would be 0.60.
        assert verdict.ratio == pytest.approx(930 / 1100, abs=1e-6)


class TestWhatGetsBanked:
    """Live and backfilled hours have to describe the same sky.

    Both paths bank into one table keyed on the hour, and the store refuses
    duplicates -- so whichever arrives first decides the geometry for good.
    If the two disagreed, a plant would carry two elevation conventions in
    one set of bands and nothing would say so.
    """

    @staticmethod
    def engine():
        from core.physics import PhysicsEngine

        return PhysicsEngine(
            latitude=53.5,
            longitude=10.0,
            elevation_m=5.0,
            albedo=0.2,
            transposition_model="perez-driesse",
            time_zone="Europe/Berlin",
        )

    def test_both_sources_agree_on_the_geometry(self):
        physics = self.engine()
        measured = {NOON: 500.0}
        live = list(rows_to_bank(physics, measured, 0, "live"))
        stats = list(rows_to_bank(physics, measured, 0, "statistics"))
        assert len(live) == 1
        assert live[0][0] == stats[0][0]
        assert live[0][4:] == stats[0][4:]
        assert live[0][3] == "live"
        assert stats[0][3] == "statistics"

    def test_the_sun_is_placed_at_the_middle_of_the_hour(self):
        """Not at its start: a June hour moves the sun by several degrees."""
        physics = self.engine()
        morning = NOON - 6 * 3600
        (row,) = rows_to_bank(physics, {morning: 200.0}, 0, "live")
        index = to_index([morning + 1800])
        expected = float(physics.solar_position(index)["apparent_elevation"].iloc[0])
        assert row[4] == pytest.approx(expected)

    def test_night_hours_are_never_banked(self):
        """They cost storage and the reference grid cannot resolve them."""
        physics = self.engine()
        midnight = NOON - 12 * 3600
        assert list(rows_to_bank(physics, {midnight: 0.0}, 0, "live")) == []

    def test_the_epoch_travels_with_every_row(self):
        """Rows from before a sensor was moved must never pool with rows after."""
        physics = self.engine()
        (row,) = rows_to_bank(physics, {NOON: 500.0}, 7, "live")
        assert row[1] == 7
