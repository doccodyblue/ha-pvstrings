"""The conversion layer: DC in, AC or battery charge out, nothing upstream.

Pinned here: neutral configurations change nothing (upgrade.md 0.1), the
curve interpolates instead of snapping, clipping caps at rated AC, and the
storage chain ends at the battery terminal.
"""

from __future__ import annotations

import json

import pytest

from core.conversion import (
    CURVE_CUSTOM,
    CURVE_DATASHEET,
    CURVE_NEUTRAL,
    convert_direct,
    convert_group,
    convert_storage,
    interpolate,
)
from core.inverter_curves import load_curves

CURVE = ((5.0, 0.90), (10.0, 0.93), (50.0, 0.96), (100.0, 0.95))
HOURLY = {0: 0.0, 3600: 0.16, 7200: 0.80, 10800: 1.50}  # kWh == mean kW


class TestInterpolation:
    def test_hits_support_points_exactly(self):
        assert interpolate(CURVE, 0.10) == pytest.approx(0.93)
        assert interpolate(CURVE, 0.50) == pytest.approx(0.96)

    def test_linear_between_points(self):
        assert interpolate(CURVE, 0.30) == pytest.approx((0.93 + 0.96) / 2)

    def test_clamped_at_both_ends(self):
        assert interpolate(CURVE, 0.01) == pytest.approx(0.90)
        assert interpolate(CURVE, 1.40) == pytest.approx(0.95)


class TestDirect:
    def test_zero_and_low_load_hours(self):
        out = convert_direct(HOURLY, 1600.0, CURVE, CURVE_DATASHEET, False)
        assert out.hourly_kwh[0] == 0.0
        # 160 W on 1600 W rated = 10 % load.
        assert out.hourly_kwh[3600] == pytest.approx(0.16 * 0.93)

    def test_clipping_caps_at_rated_and_reports_the_loss(self):
        hourly = {0: 2.0}  # 2 kW mean on a 1.6 kW inverter
        clipped = convert_direct(hourly, 1600.0, CURVE, CURVE_DATASHEET, True)
        unclipped = convert_direct(hourly, 1600.0, CURVE, CURVE_DATASHEET, False)
        assert clipped.hourly_kwh[0] == pytest.approx(1.6)
        assert clipped.clipped_kwh == pytest.approx(
            unclipped.hourly_kwh[0] - 1.6, abs=0.002
        )
        assert "clipping" in clipped.stages

    def test_clipping_without_a_curve_still_caps(self):
        """"No curve, but clip at rated" is legitimate; the cap needs no curve."""
        out = convert_direct({0: 2.0}, 1600.0, None, CURVE_DATASHEET, True)
        assert out.hourly_kwh[0] == pytest.approx(1.6)
        assert out.clipped_kwh == pytest.approx(0.4)
        assert out.stages == ("clipping",)
        assert out.curve_source == CURVE_NEUTRAL

    def test_no_rated_power_means_identity(self):
        out = convert_direct(HOURLY, None, CURVE, CURVE_DATASHEET, True)
        assert out.hourly_kwh == HOURLY
        assert out.curve_source == CURVE_NEUTRAL
        assert out.stages == ()

    def test_no_curve_means_identity(self):
        out = convert_direct(HOURLY, 1600.0, None, CURVE_DATASHEET, False)
        assert out.hourly_kwh == HOURLY
        assert out.curve_source == CURVE_NEUTRAL


class TestStorage:
    def test_chain_is_mppt_times_charge(self):
        out = convert_storage({0: 1.0}, 0.97, 0.96)
        assert out.hourly_kwh[0] == pytest.approx(0.97 * 0.96)
        assert out.stages == ("mppt_efficiency", "charge_efficiency")

    def test_without_external_mppt_only_charge_applies(self):
        out = convert_storage({0: 1.0}, None, 0.96)
        assert out.hourly_kwh[0] == pytest.approx(0.96)
        assert out.stages == ("charge_efficiency",)


class TestDispatch:
    def test_path_none_converts_nothing(self):
        assert (
            convert_group(
                HOURLY, "none", 1600.0, "hoymiles_hms1600_4t", None, True,
                None, 0.96, {},
            )
            is None
        )

    def test_unknown_model_degrades_to_neutral(self):
        out = convert_group(
            HOURLY, "direct", 1600.0, "does_not_exist", None, False,
            None, 0.96, {},
        )
        assert out.curve_source == CURVE_NEUTRAL
        assert out.hourly_kwh == HOURLY

    def test_custom_curve_wins(self):
        out = convert_group(
            HOURLY, "direct", 1600.0, "custom", CURVE, False, None, 0.96, {},
        )
        assert out.curve_source == CURVE_CUSTOM


class TestCurveLoader:
    def test_the_shipped_models_all_load(self):
        # const.py standalone: importing the package would pull HA.
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "pvstrings_const",
            Path(__file__).parent.parent
            / "custom_components/pvstrings/const.py",
        )
        const = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(const)
        INVERTER_MODELS = const.INVERTER_MODELS

        curves = load_curves(INVERTER_MODELS)
        assert set(curves) == set(INVERTER_MODELS)
        for curve in curves.values():
            loads = [load for load, _eff in curve]
            assert loads == sorted(loads) and len(set(loads)) == len(loads)
            assert all(0.5 < eff <= 1.0 for _load, eff in curve)

    def test_a_broken_file_degrades_to_absent(self, tmp_path, monkeypatch):
        import core.inverter_curves as ic

        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "bad_points.json").write_text(
            json.dumps({"points": [[10, 1.4], [5, 0.9]]}), encoding="utf-8"
        )
        (tmp_path / "top_level_list.json").write_text("[]", encoding="utf-8")
        (tmp_path / "null_point.json").write_text(
            json.dumps({"points": [[5, 0.9], [None, 0.95]]}), encoding="utf-8"
        )
        monkeypatch.setattr(ic, "_MODELS_DIR", tmp_path)
        assert (
            ic.load_curves(
                ("broken", "bad_points", "top_level_list", "null_point", "missing")
            )
            == {}
        )
