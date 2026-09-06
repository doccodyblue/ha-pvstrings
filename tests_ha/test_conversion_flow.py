"""Conversion-layer pieces that need the HA install: parser, migration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.pvstrings import async_migrate_entry
from custom_components.pvstrings import config_flow as cf


class TestCurveParser:
    def test_roundtrip(self):
        points = cf._parse_curve("5:0.90, 20:0.955, 50:0.967, 100:0.962")
        assert points == [[5.0, 0.90], [20.0, 0.955], [50.0, 0.967], [100.0, 0.962]]
        assert cf._parse_curve(cf._curve_to_text(points)) == points

    def test_rejects_garbage(self):
        assert cf._parse_curve("") is None
        assert cf._parse_curve("5:0.9") is None  # one point is a level, not a curve
        assert cf._parse_curve("5:0.9, 5:0.95") is None  # load must increase
        assert cf._parse_curve("5:0.9, 20:1.2") is None  # efficiency > 1
        assert cf._parse_curve("5:0.9, 20:0.3") is None  # below the 0.5 floor
        assert cf._parse_curve("5;0.9, 20;0.95") is None  # wrong separator

    def test_json_roundtrip_shape(self):
        """HA persists entries as JSON: tuples come back as lists.

        The parser emits lists and the text serialiser accepts both, so a
        stored curve survives save -> restart -> reconfigure unchanged.
        """
        import json

        points = cf._parse_curve("10:0.93, 50:0.96")
        restored = json.loads(json.dumps(points))
        assert restored == points
        assert cf._curve_to_text(restored) == cf._curve_to_text(points)


class TestMigration:
    def _hass_with_recorder(self, calls: list) -> SimpleNamespace:
        def update(entry, **kwargs):
            calls.append(kwargs)

        return SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update)
        )

    def test_minor_bump_is_stamped_once(self):
        calls: list = []
        entry = SimpleNamespace(version=1, minor_version=1)
        assert asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == [{"minor_version": 3}]

    def test_current_minor_is_untouched(self):
        calls: list = []
        entry = SimpleNamespace(version=1, minor_version=3)
        assert asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == []

    def test_future_major_is_refused(self):
        calls: list = []
        entry = SimpleNamespace(version=2, minor_version=1)
        assert not asyncio.run(
            async_migrate_entry(self._hass_with_recorder(calls), entry)
        )
        assert calls == []


class TestPriceEntityValidation:
    """A tariff sensor whose price cannot be scaled is refused at setup.

    The one mistake here that nothing later can catch: EUR/kWh read as ct/kWh
    is a factor of a hundred, and the wrong figure looks entirely ordinary on
    a savings sensor for as long as nobody checks it against a bill.
    """

    def _hass(self, **units) -> SimpleNamespace:
        states = {
            entity_id: SimpleNamespace(
                attributes={} if unit is None else {"unit_of_measurement": unit}
            )
            for entity_id, unit in units.items()
        }
        return SimpleNamespace(states=SimpleNamespace(get=states.get))

    def test_a_readable_unit_passes(self):
        hass = self._hass(**{"sensor.price": "EUR/kWh"})
        assert cf.price_entity_errors(hass, {"import_price_entity": "sensor.price"}) == {}

    def test_cents_are_readable_too(self):
        hass = self._hass(**{"sensor.price": "ct/kWh"})
        assert cf.price_entity_errors(hass, {"import_price_entity": "sensor.price"}) == {}

    def test_a_missing_unit_blocks(self):
        hass = self._hass(**{"sensor.price": None})
        assert cf.price_entity_errors(
            hass, {"import_price_entity": "sensor.price"}
        ) == {"import_price_entity": "price_unit_missing"}

    def test_a_unit_that_is_not_a_price_per_energy_blocks(self):
        """A bare currency cannot be scaled, and neither can a bare kWh."""
        hass = self._hass(**{"sensor.price": "EUR"})
        assert cf.price_entity_errors(
            hass, {"import_price_entity": "sensor.price"}
        ) == {"import_price_entity": "price_unit_unreadable"}

    def test_an_unknown_entity_says_so_rather_than_blaming_the_unit(self):
        assert cf.price_entity_errors(
            self._hass(), {"export_price_entity": "sensor.gone"}
        ) == {"export_price_entity": "price_entity_unknown"}

    def test_both_sides_are_reported_separately(self):
        hass = self._hass(**{"sensor.a": None, "sensor.b": "EUR"})
        assert cf.price_entity_errors(
            hass,
            {"import_price_entity": "sensor.a", "export_price_entity": "sensor.b"},
        ) == {
            "import_price_entity": "price_unit_missing",
            "export_price_entity": "price_unit_unreadable",
        }

    def test_empty_fields_are_not_an_error(self):
        assert cf.price_entity_errors(self._hass(), {}) == {}
        assert cf.price_entity_errors(self._hass(), {"import_price_entity": ""}) == {}


class TestClearingAnOptionalEntity:
    """Emptying a field has to survive two traps at once.

    An _optional field is *absent* from the submission when blanked, not blank,
    so "drop what arrived empty" never fires.  And options are merged over the
    original entry data, so merely removing the key resurrects whatever was
    stored at setup.  Only an explicit empty value clears it for good.
    """

    class _Flow:
        _save = cf.PvStringsOptionsFlow._save

        def __init__(self, data: dict, options: dict) -> None:
            self.config_entry = SimpleNamespace(data=data, options=options)

        def async_create_entry(self, title: str, data: dict) -> dict:
            return data

    def _merged(self, entry_data: dict, options: dict) -> dict:
        """What build_plant_config sees: options over data, blanks as absent."""
        merged = {**entry_data, **options}
        return {k: v for k, v in merged.items() if v not in ("", None)}

    def test_a_value_from_the_original_setup_really_goes(self):
        flow = self._Flow({"import_price_entity": "sensor.old"}, {})
        options = flow._save({}, clearable=("import_price_entity",))
        assert "import_price_entity" not in self._merged(
            {"import_price_entity": "sensor.old"}, options
        )

    def test_a_value_in_the_options_goes_too(self):
        flow = self._Flow({}, {"import_price_entity": "sensor.old"})
        options = flow._save({}, clearable=("import_price_entity",))
        assert self._merged({}, options) == {}

    def test_a_field_the_step_does_not_own_survives(self):
        flow = self._Flow({}, {"battery_soc_entity": "sensor.soc"})
        options = flow._save({}, clearable=("import_price_entity",))
        assert options["battery_soc_entity"] == "sensor.soc"

    def test_a_submitted_value_is_kept(self):
        flow = self._Flow({}, {})
        options = flow._save(
            {"import_price_entity": "sensor.new"}, clearable=("import_price_entity",)
        )
        assert options["import_price_entity"] == "sensor.new"
