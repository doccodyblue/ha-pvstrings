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
)

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
        assert verdict.reading() == "reads consistently off, flat across the sky"

    def test_the_spectral_curve_is_recognised(self):
        """The reference plant's own shape, rounded: 0.59 at dawn, 0.78 high."""
        verdict = assess(
            pairs([(10, 0.59), (20, 0.67), (30, 0.73), (40, 0.75), (50, 0.78)])
        )
        assert verdict.slope > 0.08
        assert verdict.reading() == "reads low, and lower as the sun drops"

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
