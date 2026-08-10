"""Test fixtures.

The core package is imported directly, without Home Assistant.  That is the
whole point of keeping ``core/`` HA-free: the physics and the learning rules can
be exercised in milliseconds against a temporary SQLite file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "pvstrings"))

from core.config import (  # noqa: E402
    CurtailmentGroup,
    Economics,
    GeometrySegment,
    PlantConfig,
    StringConfig,
)
from core.store import Store  # noqa: E402

#: A north-German balcony plant: two battery-coupled strings behind one
#: inverter, one grid-tied string on another.  Close enough to a real small
#: installation to catch the interesting cases, at a neutral reference
#: location rather than anyone's address.
LAT, LON = 53.5, 10.0


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "test.db")
    store.connect()
    yield store
    store.close()


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig(
        name="Test plant",
        latitude=LAT,
        longitude=LON,
        elevation_m=5.0,
        time_zone="Europe/Berlin",
        strings=(
            StringConfig(
                string_id="s1",
                name="South 30",
                power_entity="sensor.s1_power",
                curtailment_group_id="battery",
            ),
            StringConfig(
                string_id="s2",
                name="South 60",
                power_entity="sensor.s2_power",
                curtailment_group_id="battery",
            ),
            StringConfig(
                string_id="s3",
                name="East 27",
                power_entity="sensor.s3_power",
            ),
        ),
        groups=(
            CurtailmentGroup(
                group_id="battery",
                name="Battery inverter",
                limit_entity="number.limit_relative",
                inverter_max_ac_w=1600.0,
                battery_coupled=True,
                soc_entity="sensor.soc",
            ),
        ),
        economics=Economics(
            mode="net_metering",
            price_per_kwh=0.32,
            feed_in_tariff=0.08,
            investment_eur=3500.0,
        ),
    )


@pytest.fixture
def seeded_store(store: Store, plant: PlantConfig) -> Store:
    store.add_geometry(
        "s1", GeometrySegment(0, azimuth_deg=180, tilt_deg=30, kwp=1.80)
    )
    store.add_geometry(
        "s2", GeometrySegment(0, azimuth_deg=180, tilt_deg=60, kwp=1.00)
    )
    store.add_geometry(
        "s3", GeometrySegment(0, azimuth_deg=110, tilt_deg=27, kwp=0.95)
    )
    return store
