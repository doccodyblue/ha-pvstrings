"""The per-string hour profile is published live but kept out of the recorder."""

from __future__ import annotations

from custom_components.pvstrings.coordinator import PvStringsData
from custom_components.pvstrings.sensor import PLANT_SENSORS, PlantSensor

PROFILE = {"s1": [{"hour": 9, "forecast_kwh": 1.0, "actual_kwh": 0.5, "days": 3}]}


def _description():
    return next(d for d in PLANT_SENSORS if d.key == "wmape_day_ahead_30d")


def test_the_30_day_sensor_carries_it():
    data = PvStringsData(0, 0, 86400, 86400, 2 * 86400)
    data.scores_day_ahead[30] = {"hourly_profile_by_string": PROFILE}
    attrs = _description().attrs_fn(data, None)
    assert attrs["hourly_profile_by_string"] == PROFILE


def test_the_recorder_does_not_store_it():
    # 5 strings x ~14 hours next to the existing history would push the state
    # toward the recorder's 16 KB limit, past which it drops every attribute.
    assert "hourly_profile_by_string" in PlantSensor._unrecorded_attributes
