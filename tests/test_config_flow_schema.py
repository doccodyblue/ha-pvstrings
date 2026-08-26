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

    def test_subentry_creation_does_not_schedule_a_second_reload(self):
        """``async_add_subentry`` already notifies the update listeners.

        Verified against Home Assistant 2026.8: it routes through
        ``_async_update_entry``, whose contract is that listeners fire when the
        entry changed.  Adding an explicit reload on top rebuilt the plant
        twice per added string -- and the explicit one ran *before* the
        subentry existed, so the first rebuild saw the old configuration.
        """
        source = FLOW.read_text()
        assert "async_schedule_reload" not in source


class TestConcernsReachTheUser:
    """A concern nobody can read is worse than no concern at all.

    These replaced a ``_LOGGER.warning`` that fired on every helper entity and
    was, by construction, invisible to the person still able to fix the setup.
    The rules themselves need Home Assistant to run, so what is pinned here is
    the wiring: every concern the code can raise has a checkbox label in every
    language, and both entry points into a subentry actually route through the
    confirmation step.
    """

    TRANSLATIONS = (
        FLOW.parent / "strings.json",
        FLOW.parent / "translations/en.json",
        FLOW.parent / "translations/de.json",
    )

    #: Which subentry type owns which concern, mirroring the two detectors.
    OWNERS = {
        "string": (
            "helper_entity",
            "power_entity_reused",
            "power_entity_missing",
            "power_entity_has_other_role",
            "charger_vocabulary",
        ),
        "curtailment_group": (
            "fixed_limit_with_battery",
            "storage_without_battery",
            "battery_without_soc",
            "limit_unit_relative",
            "limit_unit_absolute",
        ),
    }

    @staticmethod
    def _concern_constants() -> set[str]:
        """The CONCERN_* string literals the flow can actually raise."""
        tree = ast.parse(FLOW.read_text())
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.startswith("CONCERN_")
                    and isinstance(node.value, ast.Constant)
                ):
                    found.add(node.value.value)
        return found

    def test_every_concern_is_owned_by_exactly_one_subentry_type(self):
        declared = {c for group in self.OWNERS.values() for c in group}
        assert self._concern_constants() == declared, (
            "a new CONCERN_* constant needs a home in OWNERS and a label below"
        )

    @pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.name)
    def test_every_concern_has_a_checkbox_label(self, path):
        import json

        data = json.loads(path.read_text())
        for subentry_type, concerns in self.OWNERS.items():
            step = data["config_subentries"][subentry_type]["step"]
            assert "confirm" in step, f"{subentry_type} has no confirmation step"
            labels = step["confirm"]["data"]
            for concern in concerns:
                key = f"ack_{concern}"
                assert key in labels, f"{path.name}: {subentry_type} lacks {key}"
                assert labels[key].strip(), f"{path.name}: {key} is empty"

    @pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.name)
    def test_an_unticked_box_can_explain_itself(self, path):
        import json

        data = json.loads(path.read_text())
        for subentry_type in self.OWNERS:
            errors = data["config_subentries"][subentry_type].get("error", {})
            assert "must_acknowledge" in errors, (
                f"{path.name}: {subentry_type} cannot explain a blank checkbox"
            )

    def test_both_ways_into_a_string_run_the_concern_check(self):
        """Creating and editing must agree; the edit path is the older one."""
        tree = ast.parse(FLOW.read_text())
        flow = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "StringSubentryFlow"
        )
        for step in ("async_step_user", "async_step_reconfigure"):
            method = next(
                node
                for node in flow.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == step
            )
            called = {
                node.func.id
                for node in ast.walk(method)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            assert "string_concerns" in called, f"{step} skips the concern check"

    def test_the_group_form_runs_the_concern_check(self):
        source = FLOW.read_text()
        assert "group_concerns(self.hass, self._get_entry(), data)" in source


class TestFractionalCurve:
    """Percent-vs-fraction is a typo, so it gets an error, not a checkbox.

    Entered as fractions the curve parses cleanly and every real load lands
    past the last support point, where interpolation clamps to one constant
    efficiency -- the curve stops being a curve without ever complaining.
    """

    def test_the_parser_rejects_a_curve_that_never_leaves_the_first_percent(self):
        source = FLOW.read_text()
        assert "_FractionalCurve" in source
        assert "curve_fractional" in source

    @pytest.mark.parametrize(
        "path", TestConcernsReachTheUser.TRANSLATIONS, ids=lambda p: p.name
    )
    def test_the_message_says_what_to_write_instead(self, path):
        import json

        errors = json.loads(path.read_text())["config_subentries"][
            "curtailment_group"
        ]["error"]
        assert "curve_fractional" in errors
        # The point of a separate message is the worked example in it.
        assert "100:0.95" in errors["curve_fractional"]


class TestConstantsResolve:
    """Every constant the flow uses must actually be imported.

    The other tests here read the file as text or AST and never import it, so a
    name that exists nowhere sails straight through them -- and Home Assistant
    then raises ``NameError`` inside a form submit, which surfaces to the user
    as a bare "Unknown error occurred". Two missing imports were caught this
    way while the concern checks were being written.

    Only SHOUTING_CASE names are examined: this module never uses that spelling
    for a local, so membership needs no scope analysis to be exact.
    """

    @staticmethod
    def _available(tree: ast.Module) -> set[str]:
        names = set(dir(__builtins__)) | {"__name__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(
                    (alias.asname or alias.name).split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.Assign):
                names.update(
                    target.id
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        return names

    def test_no_constant_is_used_without_being_imported(self):
        tree = ast.parse(FLOW.read_text())
        available = self._available(tree)
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id.isupper()
            or (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == node.id.upper()
                and "_" in node.id
            )
        }
        missing = sorted(name for name in used if name not in available)
        assert not missing, f"used but never imported or defined: {missing}"
