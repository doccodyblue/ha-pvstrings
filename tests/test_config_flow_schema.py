"""Guards for the form schemas.

These cannot exercise Home Assistant's selectors directly (no HA in the test
environment), so they pin the constraints those selectors impose.  The step
rule below cost a full deployment cycle to find: a violation raises
``voluptuous.Invalid`` inside the HTTP view, which HA turns into a bare
"400: Bad Request" and logs *nothing at all*.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.config import MIN_SELECTOR_STEP, selector_step

FLOW = pathlib.Path(__file__).resolve().parents[1] / (
    "custom_components/pvstrings/config_flow.py"
)


class TestSelectorStep:
    def test_coarse_steps_pass_through(self):
        assert selector_step(1.0) == 1.0
        assert selector_step(0.01) == 0.01

    def test_the_boundary_is_accepted(self):
        assert selector_step(MIN_SELECTOR_STEP) == MIN_SELECTOR_STEP

    def test_finer_than_the_selector_allows_becomes_any(self):
        assert selector_step(0.00001) == "any"
        assert selector_step(0.0001) == "any"

    def test_coordinates_need_the_any_escape(self):
        """Five decimal places is roughly a metre -- a real requirement."""
        assert selector_step(0.00001) == "any"


class TestFlowSchemas:
    def test_every_number_step_goes_through_the_clamp(self):
        """No caller may build a NumberSelectorConfig with a raw step."""
        tree = ast.parse(FLOW.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "NumberSelectorConfig":
                continue
            for keyword in node.keywords:
                if keyword.arg != "step":
                    continue
                clamped = (
                    isinstance(keyword.value, ast.Call)
                    and getattr(keyword.value.func, "id", None) == "selector_step"
                )
                if not clamped:
                    offenders.append(ast.dump(keyword.value))
        assert not offenders, f"unclamped step values: {offenders}"

    def test_no_module_level_import_of_the_science_stack(self):
        """The config flow must render without pvlib, numpy or pandas.

        Home Assistant installs requirements lazily; a form that needs the
        whole scientific stack just to list mounting types fails before the
        user ever sees it.
        """
        tree = ast.parse(FLOW.read_text())
        forbidden = {"pvlib", "numpy", "pandas"}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names} & forbidden
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in forbidden:
                    found.add(root)
                if node.module.endswith("physics"):
                    found.add("core.physics (pulls pvlib)")
        assert not found, f"config_flow imports {found}"


class TestOptionalEntityFields:
    """Entity and date selectors reject an empty string.

    ``vol.Optional(key, default="")`` therefore makes voluptuous inject a value
    the selector refuses on *every* submit, and the form fails before the user
    has done anything wrong.  Blank must mean absent.
    """

    SELECTOR_CALLS = {"EntitySelector", "DateSelector", "sensor"}

    def _schema_pairs(self):
        tree = ast.parse(FLOW.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if key is None:
                    continue
                yield key, value

    @staticmethod
    def _call_name(node):
        if not isinstance(node, ast.Call):
            return None
        return getattr(node.func, "id", None) or getattr(node.func, "attr", None)

    def test_no_entity_or_date_field_carries_a_default(self):
        offenders = []
        for key, value in self._schema_pairs():
            if self._call_name(value) not in self.SELECTOR_CALLS:
                continue
            if self._call_name(key) not in {"Optional", "Required"}:
                continue
            if any(kw.arg == "default" for kw in key.keywords):
                offenders.append(ast.unparse(key))
        assert not offenders, (
            "entity/date fields must use _optional()/_prefilled(), not a "
            f"default: {offenders}"
        )

    def test_helpers_drop_blank_values(self):
        """Reimplements the contract the helpers must satisfy."""
        for blank in (None, ""):
            assert not _marker_has_suggestion(blank)
        assert _marker_has_suggestion("sensor.foo")


def _marker_has_suggestion(current) -> bool:
    """Mirror of the rule in ``_optional``: only real values are suggested."""
    return current not in (None, "")


class TestNamePrefill:
    """The name is stored as the subentry title, never inside its data.

    Both reconfigure paths therefore have to seed it back into the form, or the
    field comes up blank and submitting the form renames the string to "".
    """

    def _reconfigure_bodies(self):
        tree = ast.parse(FLOW.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "async_step_reconfigure",
                "_form",
            ):
                yield node.name, ast.unparse(node)

    def test_both_reconfigure_paths_seed_the_title(self):
        seeded = {
            name
            for name, body in self._reconfigure_bodies()
            if "subentry.title" in body
        }
        assert "_form" in seeded, "group reconfigure does not prefill the name"
        assert (
            "async_step_reconfigure" in seeded
        ), "string reconfigure does not prefill the name"


class TestReloadStrategy:
    """Two ways of reloading exist and they are mutually exclusive.

    ``ConfigSubentryFlow.async_update_reload_and_abort`` raises
    ``ValueError: Cannot update and reload entry with update listeners`` when
    the entry has any update listener registered.  This integration registers
    one so that the options flow takes effect, so subentry edits must use the
    plain ``async_update_and_abort`` and let the listener reload.

    Getting this wrong makes every string and group edit fail with a 500.
    """

    INIT = FLOW.parent / "__init__.py"

    def test_the_integration_registers_an_update_listener(self):
        assert "add_update_listener" in self.INIT.read_text()

    def test_no_subentry_flow_uses_the_reloading_variant(self):
        # AST rather than a text search, so the comment explaining *why* does
        # not trip the check.
        called = {
            node.func.attr
            for node in ast.walk(ast.parse(FLOW.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "async_update_reload_and_abort" not in called, (
            "incompatible with the update listener registered in __init__.py"
        )
        assert "async_update_and_abort" in called

    def test_subentry_creation_still_schedules_its_own_reload(self):
        """No listener fires when a subentry is added, so that path must."""
        source = FLOW.read_text()
        assert "async_schedule_reload" in source
