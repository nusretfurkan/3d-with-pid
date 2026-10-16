"""Composed equipment definitions: pure validation, template dispatch, resolution and offline views.

    python3 -m unittest tests.test_equipment -v            # any Python 3.9+, no CAD or Blender
    .venv-cad/bin/python -m unittest tests.test_equipment -v

The helpers at the top (``drum_entity``, ``inline_payload``, ``drum_batch``,
``housing_batch``) build the composed-equipment scenarios that
``tests.test_transactions`` and ``tests.test_cad_equipment`` reuse: the
example horizontal drum inserted into L-105 with explicit placement and
manual routes, and the inline box housing inserted downstream of it. They
are illustrative layouts, not engineering choices.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

from examples.make_fixture import make_document
from skid.core.commands import apply_command
from skid.core.errors import CommandError
from skid.core.validation import validate_document, validate_project
from skid.equipment import (
    COMPOSED_KIND,
    LIMITS,
    DefinitionError,
    entity_template,
    is_composed,
    resolve_definition,
    resolve_recipe,
    validate_definition,
)
from skid.templates import (
    diagram_port_position,
    entity_primitives,
    get_template,
    local_port_position,
    world_port_position,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES = os.path.join(ROOT, "examples", "recipes")
DRUM_FILE = os.path.join(RECIPES, "horizontal_drum.definition.json")
HOUSING_FILE = os.path.join(RECIPES, "inline_box_housing.definition.json")
DRUM = "ent-drum"
HOUSING = "ent-housing"
DRUM_ASSUMPTION = "asm-drum-example"
HOUSING_ASSUMPTION = "asm-housing-example"


# --- Shared scenario helpers -------------------------------------------------------


def load_definition(path=DRUM_FILE):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def defaults(definition):
    return {name: record["value"] for name, record in definition["geometry"]["parameters"].items()}


def composed_entity(definition, entity_id, tag, assumption_id, **values):
    parameters = defaults(definition)
    parameters.update(values)
    return {"id": entity_id, "kind": COMPOSED_KIND, "tag": tag, "template_version": 1, "definition": definition,
            "parameters": {name: {"value": value, "unit": "m", "assumption_id": assumption_id}
                           for name, value in sorted(parameters.items())}}


def drum_entity(**values):
    return composed_entity(load_definition(DRUM_FILE), DRUM, "V-101", DRUM_ASSUMPTION, **values)


def housing_entity(**values):
    values.setdefault("nozzle_height", 0.75)
    return composed_entity(load_definition(HOUSING_FILE), HOUSING, "U-101", HOUSING_ASSUMPTION, **values)


def assumption(description):
    return {"description": description, "source": "composed-equipment test scenario"}


def inline_payload(document, connection_id, entity, prefix, line_tag, symbol_xy, edge_points=None, placement=None,
                   upstream_route=None, downstream_route=None):
    """An insert_equipment/insert_valve payload splitting ``connection_id`` with fresh, explicit IDs."""
    project = document["current"]
    connection = project["model"]["connections"][connection_id]
    (up_edge,) = [e for e in project["pid"]["edges"].values() if e["connection_id"] == connection_id]
    port_assumption = entity["parameters"][sorted(entity["parameters"])[0]]["assumption_id"]
    ports = [{"id": f"port-{prefix}-{name}", "entity_id": entity["id"], "name": name, "direction": direction,
              "required": True, "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": port_assumption}
             for name, direction in (("inlet", "in"), ("outlet", "out"))]
    downstream = {"id": f"conn-{prefix}-out", "tag": line_tag, "from_port_id": f"port-{prefix}-outlet",
                  "to_port_id": connection["to_port_id"], "nominal_diameter_mm": 50,
                  "outside_diameter_m": connection["outside_diameter_m"], "assumption_id": connection["assumption_id"]}
    edge = {"id": f"edge-{prefix}-out", "connection_id": downstream["id"], "from_symbol_id": f"sym-{prefix}",
            "to_symbol_id": up_edge["to_symbol_id"], "points": edge_points or [], "manual": False,
            "status": "current" if edge_points else "unresolved"}
    if not edge_points:
        edge["reason"] = "diagram path not supplied at insertion"
    payload = {"connection_id": connection_id, "entity": entity, "ports": ports, "downstream_connection": downstream,
               "symbol": {"id": f"sym-{prefix}", "entity_id": entity["id"], "x": float(symbol_xy[0]),
                          "y": float(symbol_xy[1]), "rotation_deg": 0.0, "manual": True},
               "downstream_edge": edge}
    for key, value in (("placement", placement), ("upstream_route", upstream_route),
                       ("downstream_route", downstream_route)):
        if value is not None:
            payload[key] = value
    return payload


def placed(position, rotation=(0.0, 0.0, -90.0), assumption_id=DRUM_ASSUMPTION):
    return {"position_m": list(position), "rotation_deg": list(rotation), "status": "placed", "manual": True,
            "assumption_id": assumption_id}


def manual_route(points, assumption_id="asm-manual-route"):
    return {"points_m": [list(p) for p in points], "mode": "manual", "status": "current",
            "assumption_id": assumption_id}


def command(document, kind, payload, command_id, **extra):
    return dict({"id": command_id, "expected_revision": document["current"]["revision"], "type": kind,
                 "payload": payload}, **extra)


def move_boundary(position, symbol_x):
    return [
        {"type": "move_symbol", "payload": {"symbol_id": "sym-outlet-boundary", "x": float(symbol_x), "y": 220.0,
                                            "rotation_deg": 0.0}},
        {"type": "move_equipment", "payload": {"entity_id": "ent-outlet-boundary", "placement": {
            "position_m": list(position), "rotation_deg": [0.0, 0.0, -90.0], "status": "placed", "manual": True,
            "assumption_id": "asm-skid-layout"}}},
    ]


def drum_batch(document, **values):
    """One batch: move OUT-101 clear, then insert V-101 into L-105 with placement and manual routes.

    Drum at (2.7, -1.6, 0) turned -90 deg about z: inlet (2.7, -1.5, 1.2) facing +z, outlet
    (2.7, -2.58, 0.75) facing -y; OUT-101 moved so its inlet is at (2.7, -2.9, 0.75).
    """
    entity = drum_entity(**values)
    payload = inline_payload(
        document, "conn-discharge-3", entity, "drum", "L-106", (700, 220), edge_points=[[760.0, 220.0], [802.0, 220.0]],
        placement=placed((2.7, -1.6, 0.0)),
        upstream_route=manual_route([(2.7, -0.53, 0.7), (2.7, -0.68, 0.7), (2.7, -0.68, 1.45), (2.7, -1.5, 1.45),
                                     (2.7, -1.5, 1.2)]),
        downstream_route=manual_route([(2.7, -2.58, 0.75), (2.7, -2.9, 0.75)]))
    operations = move_boundary((2.7, -3.0, 0.75), 820) + [{
        "type": "insert_equipment", "payload": payload,
        "assumptions": {DRUM_ASSUMPTION: assumption("Illustrative horizontal drum dimensions, ports and placement "
                                                    "for the composed-equipment example; no duty implied.")}}]
    return command(document, "batch", {"operations": operations}, "cmd-test-drum-batch")


def housing_batch(document):
    """Insert U-101 between V-101 and OUT-101 (moved further along -y) with manual routes."""
    entity = housing_entity()
    payload = inline_payload(
        document, "conn-drum-out", entity, "housing", "L-107", (840, 220), edge_points=[[880.0, 220.0], [902.0, 220.0]],
        placement=placed((2.7, -3.4, 0.0), assumption_id=HOUSING_ASSUMPTION),
        upstream_route=manual_route([(2.7, -2.58, 0.75), (2.7, -3.0, 0.75)]),
        downstream_route=manual_route([(2.7, -3.8, 0.75), (2.7, -4.2, 0.75)]))
    operations = move_boundary((2.7, -4.3, 0.75), 920) + [{
        "type": "insert_equipment", "payload": payload,
        "assumptions": {HOUSING_ASSUMPTION: assumption("Illustrative inline housing dimensions, ports and placement "
                                                       "for the second composition example; no duty implied.")}}]
    return command(document, "batch", {"operations": operations}, "cmd-test-housing-batch")


def drum_document(**values):
    document, _ = apply_command(make_document(), drum_batch(make_document(), **values))
    return document


def codes(diagnostics):
    return {d["code"] for d in diagnostics}


def errors(diagnostics):
    return [d for d in diagnostics if d["severity"] == "error"]


# --- Definition validation ---------------------------------------------------------


class DefinitionValidationTest(unittest.TestCase):

    def rejects(self, mutate, code, definition=None):
        definition = copy.deepcopy(definition if definition is not None else load_definition())
        mutate(definition)
        found = validate_definition(definition)
        self.assertIn(code, codes(found), found)
        self.assertTrue(all(d["severity"] == "error" for d in found))
        return found

    def test_examples_are_valid_and_results_are_fresh_copies(self):
        for path in (DRUM_FILE, HOUSING_FILE):
            with self.subTest(path=os.path.basename(path)):
                self.assertEqual(validate_definition(load_definition(path)), [])
        bad = load_definition()
        bad["symbol"]["shape"] = "spaceship"
        first = validate_definition(bad)
        first[0]["message"] = "mutated by caller"
        self.assertNotEqual(validate_definition(bad)[0]["message"], "mutated by caller")

    def test_unknown_missing_and_mistyped_fields(self):
        self.rejects(lambda d: d.update(svg="<svg/>"), "unknown_field")
        self.rejects(lambda d: d["symbol"].update(svg="<path d='M0 0'/>"), "unknown_field")
        self.rejects(lambda d: d["geometry"].update(script="import os"), "unknown_field")
        self.rejects(lambda d: d["geometry"]["components"][0].update(note="x"), "unknown_field")
        self.rejects(lambda d: d["geometry"]["components"][0]["parts"][0].update(file="/etc/passwd"), "unknown_field")
        self.rejects(lambda d: d.pop("unknowns"), "missing_field")
        self.rejects(lambda d: d.update(geometry="examples/recipes/horizontal_drum.recipe.json"), "invalid_type")
        self.rejects(lambda d: d.update(schema="skid.equipment.definition/2"), "unsupported_schema")
        self.rejects(lambda d: d.update(id="Horizontal Drum"), "invalid_value")
        for garbage in (None, [], "definition", 3, {"schema": None}):
            with self.subTest(garbage=garbage):
                self.assertTrue(errors(validate_definition(garbage)))

    def test_numbers_must_be_finite_and_not_booleans(self):
        part = lambda d: d["geometry"]["components"][2]["parts"][0]  # noqa: E731 - inlet flanged_nozzle
        self.rejects(lambda d: part(d).update(neck_length=True), "invalid_number")
        self.rejects(lambda d: d["geometry"]["parameters"]["shell_length"].update(value=False), "invalid_number")
        self.rejects(lambda d: part(d).update(neck_length=float("inf")), "invalid_definition")
        self.rejects(lambda d: part(d).update(neck_length=float("nan")), "invalid_definition")
        self.rejects(lambda d: d["symbol"]["anchors"].update(inlet=[True, 0]), "invalid_number")
        self.rejects(lambda d: part(d)["flange"].update(bolt_count="=4"), "invalid_value")
        self.rejects(lambda d: part(d).update(neck_length="190"), "invalid_value")
        self.rejects(lambda d: part(d).update(neck_length=None), "invalid_value")

    def test_ports_components_and_symbols_must_match(self):
        self.rejects(lambda d: d["ports"].pop("outlet"), "port_mismatch")
        self.rejects(lambda d: d["symbol"]["anchors"].update(drain=[0, 30]), "port_mismatch")
        self.rejects(lambda d: d["geometry"]["ports"]["inlet"].update(component="nozzle/missing"), "port_mismatch")
        self.rejects(lambda d: d["geometry"]["components"][1].update(key="body"), "duplicate_component")
        self.rejects(lambda d: d["geometry"]["components"][1].update(key="body#2"), "invalid_value")
        self.rejects(lambda d: d["symbol"].update(shape="reactor_with_agitator"), "unsupported_symbol")
        self.rejects(lambda d: d["geometry"]["ports"]["inlet"].update(nominal_diameter_mm=80),
                     "unsupported_nominal_diameter")
        self.rejects(lambda d: d["ports"]["inlet"].update(service="gas"), "invalid_service")
        self.rejects(lambda d: d["ports"]["inlet"].update(direction="sideways"), "invalid_value")

        def display_only_port(d):
            d["geometry"]["components"][2]["display_only"] = True

        self.rejects(display_only_port, "port_mismatch")
        self.rejects(lambda d: d["geometry"]["components"][0].update(material="unobtainium"), "unknown_material")
        self.rejects(lambda d: d["geometry"]["components"][0]["parts"].append({"part": "teleporter"}), "unknown_part")

    def test_expressions_are_declared_bounded_arithmetic(self):
        derived = lambda d, text: d["geometry"]["derived"].append(["extra", text])  # noqa: E731
        self.rejects(lambda d: derived(d, "=wall_thickness * 2"), "undeclared_name")
        self.rejects(lambda d: d["geometry"]["ports"]["outlet"]["position"].__setitem__(0, "=xR + flange_gap"),
                     "undeclared_name")
        for text in ("=__import__('os').system('true')", "=L.real", "=x_in[0]", "=(lambda: 1)()", "=1 if L else 2",
                     "=open('f')", "=L < 2", "='a'", "=2 ** 9", "=L ** R", "=2 ** 5000", "=sqrt(1, 2)",
                     "=min(*[1, 2])", "=" + "+".join(["L"] * 200), "=" + "-" * 30 + "L",
                     "=" + "abs(" * 30 + "L" + ")" * 30):
            with self.subTest(text=text[:40]):
                self.rejects(lambda d: derived(d, text), "unsafe_expression")
        self.rejects(lambda d: derived(d, "=" + "1+" * 300 + "1"), "unsafe_expression")
        self.rejects(lambda d: d["geometry"]["constraints"].append({"at_least_zero": "=L - 5000", "message": "x"}),
                     "definition_resolution_failed")
        self.rejects(lambda d: derived(d, "=L / (R - R)"), "definition_resolution_failed")

    def test_assumptions_are_declared_and_local_ids_are_plain(self):
        self.rejects(lambda d: d["geometry"]["components"][0]["assumptions"].append("asm-elsewhere"),
                     "undeclared_assumption")
        self.rejects(lambda d: d["geometry"]["parameters"]["shell_length"].update(assumption_id="asm-x"),
                     "undeclared_assumption")

        def colon_id(d):
            d["geometry"]["assumptions"]["ent-a::b"] = {"category": "appearance", "description": "x"}

        self.rejects(colon_id, "invalid_value")
        self.rejects(lambda d: d["geometry"]["assumptions"]["rcp-appearance"].update(category="marketing"),
                     "invalid_value")

    def test_work_is_bounded(self):
        def nest(d, depth):
            item = {"part": "box", "center": [0, 0, 6], "size": [10, 10, 10]}
            for _ in range(depth):
                item = {"part": "instances", "offsets": [[0, 0, 0]], "item": item}
            d["geometry"]["components"][0]["parts"].append(item)

        nest_ok = load_definition()
        nest(nest_ok, 3)
        self.assertEqual(validate_definition(nest_ok), [])
        self.rejects(lambda d: nest(d, 4), "limit_exceeded")

        def expand(d):
            item = {"part": "linear_array", "count": 512, "step": [1, 0, 0],
                    "item": {"part": "bolt", "seat": [0, 0, 12], "axis": [0, 0, 1], "across_flats": 16, "height": 9,
                             "stud_radius": 5, "stud_length": 14}}
            d["geometry"]["components"][0]["parts"].append(item)

        self.rejects(expand, "limit_exceeded")
        self.rejects(lambda d: d["geometry"]["components"][0]["parts"][1].update(offsets=[[0, 0, 0]] * 257),
                     "limit_exceeded")
        self.rejects(lambda d: d.update(name="N" * 121), "limit_exceeded")
        self.rejects(lambda d: d["unknowns"].extend(["unknown"] * 16), "limit_exceeded")

        def huge(d):
            profile = [[i % 7 + 1, i] for i in range(256)]
            d["geometry"]["components"][0]["parts"] += [
                {"part": "revolve", "origin": [0, 0, 0], "axis": [1, 0, 0], "profile": profile}] * 40

        found = self.rejects(huge, "limit_exceeded")
        self.assertIn(str(LIMITS["definition_json_bytes"]), " ".join(d["message"] for d in found))

        deep = load_definition()
        cursor = deep["geometry"]
        for _ in range(5000):
            cursor["x"] = {}
            cursor = cursor["x"]
        self.assertTrue(errors(validate_definition(deep)))

    def test_default_resolution_must_satisfy_constraints(self):
        self.rejects(lambda d: d["geometry"]["parameters"]["inlet_offset"].update(value=0.35, minimum=0.1),
                     "definition_resolution_failed")
        self.rejects(lambda d: d["geometry"]["parameters"]["shell_length"].update(value=3.0), "out_of_bounds")


# --- Templates and resolution ------------------------------------------------------


class TemplateDispatchTest(unittest.TestCase):

    def test_built_ins_delegate_unchanged(self):
        project = make_document()["current"]
        for entity in project["model"]["entities"].values():
            with self.subTest(kind=entity["kind"]):
                self.assertFalse(is_composed(entity))
                self.assertEqual(entity_template(entity), get_template(entity["kind"], entity["template_version"]))
        with self.assertRaises(KeyError):
            get_template(COMPOSED_KIND, 1)
        with self.assertRaises(KeyError):
            entity_template({"kind": "reactor", "template_version": 1})

    def test_composed_template_record(self):
        template = entity_template(drum_entity())
        self.assertEqual(set(template), {"kind", "version", "parameters", "ports", "diagram_anchors", "definition"})
        self.assertEqual((template["kind"], template["version"]), (COMPOSED_KIND, 1))
        self.assertEqual(template["parameters"]["shell_length"], {"unit": "m", "min_inclusive": 0.8,
                                                                  "max_inclusive": 2.6})
        self.assertEqual(template["ports"], {
            "inlet": {"direction": "in", "required": True, "nominal_diameter_mm": 50, "service": "liquid"},
            "outlet": {"direction": "out", "required": True, "nominal_diameter_mm": 50, "service": "liquid"}})
        self.assertEqual(template["diagram_anchors"], {"inlet": [-60.0, 0.0], "outlet": [60.0, 0.0]})
        self.assertEqual(template["definition"]["id"], "horizontal-drum")
        self.assertEqual(template["definition"]["symbol"], "horizontal_vessel")
        template["parameters"].clear()
        self.assertTrue(entity_template(drum_entity())["parameters"])

    def test_definition_faults_raise_definition_error(self):
        tank = make_document()["current"]["model"]["entities"]["ent-tank"]
        cases = [
            dict(tank, definition=load_definition()),
            dict(drum_entity(), template_version=2),
            {k: v for k, v in drum_entity().items() if k != "definition"},
            dict(drum_entity(), definition=dict(load_definition(), symbol={"shape": "x", "anchors": {}})),
        ]
        for entity in cases:
            with self.subTest(entity=entity.get("kind")):
                with self.assertRaises(DefinitionError):
                    entity_template(entity)
        self.assertTrue(issubclass(DefinitionError, ValueError))

    def test_resolution_uses_current_values_and_checks_constraints(self):
        base = resolve_definition(drum_entity())
        longer = resolve_definition(drum_entity(shell_length=2.0))
        self.assertEqual(base["ports"]["outlet"]["position_mm"], [980.0, 0.0, 750.0])
        self.assertEqual(longer["ports"]["outlet"]["position_mm"], [1280.0, 0.0, 750.0])
        self.assertEqual(longer["ports"]["inlet"]["position_mm"], [-400.0, 0.0, 1200.0])
        self.assertEqual(longer["parameters"]["shell_length"],
                         {"value": 2.0, "unit": "m", "source": "entity parameter",
                          "provenance": {"assumption_id": DRUM_ASSUMPTION},
                          "recipe_default_assumption_id": "asm-drum-dimensions"})
        self.assertEqual((longer["definition_id"], longer["entity_id"]), ("horizontal-drum", DRUM))
        # The one shared resolver: identical components and ports to the standalone path at the same values.
        values = defaults(load_definition())
        values["shell_length"] = 2.0
        standalone = resolve_recipe(load_definition()["geometry"], values)
        for key in ("components", "ports", "derived_mm", "constraints_checked"):
            self.assertEqual(longer[key], standalone[key], key)
        with self.assertRaisesRegex(DefinitionError, "clear the left saddle"):
            resolve_definition(drum_entity(inlet_offset=0.35))
        with self.assertRaisesRegex(DefinitionError, "outside"):
            resolve_definition(drum_entity(shell_length=9.0))
        extra = drum_entity()
        extra["parameters"]["wall"] = {"value": 0.01, "unit": "m", "assumption_id": DRUM_ASSUMPTION}
        missing = drum_entity()
        missing["parameters"].pop("shell_length")
        mm = drum_entity()
        mm["parameters"]["shell_length"]["unit"] = "mm"
        for entity in (extra, missing, mm):
            with self.assertRaises(DefinitionError):
                resolve_definition(entity)

    def test_positions_anchors_and_primitives_dispatch(self):
        project = drum_document()["current"]
        self.assertEqual(local_port_position(project, "port-drum-inlet"), (-0.1, 0.0, 1.2))
        self.assertEqual(world_port_position(project, "port-drum-inlet"), (2.7, -1.5, 1.2))
        self.assertEqual(world_port_position(project, "port-drum-outlet"), (2.7, -2.58, 0.75))
        self.assertEqual(diagram_port_position(project, "port-drum-inlet", "sym-drum"), (640.0, 220.0))
        self.assertEqual(diagram_port_position(project, "port-drum-outlet", "sym-drum"), (760.0, 220.0))
        with self.assertRaises(DefinitionError):
            entity_primitives(project["model"]["entities"][DRUM])
        self.assertTrue(entity_primitives(project["model"]["entities"]["ent-tank"]))

    def test_pure_layer_never_imports_cad_or_blender(self):
        code = ("import json, sys; import skid.core, skid.equipment, skid.templates; "
                "from tests.test_equipment import drum_document; drum_document(); "
                "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
                "('OCP', 'cadquery', 'bpy', 'skid'))))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse([m for m in loaded if m.split(".")[0] in ("OCP", "cadquery", "bpy")], loaded)
        self.assertFalse([m for m in loaded if m.startswith(("skid.geometry", "skid.adapters", "skid.drafting"))])


# --- Project validation ------------------------------------------------------------


class ProjectValidationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.document = drum_document()

    def variant(self, mutate):
        document = copy.deepcopy(self.document)
        mutate(document["current"]["model"]["entities"])
        return document

    def assertRejects(self, mutate, code):
        found = errors(validate_document(self.variant(mutate)))
        self.assertIn(code, codes(found), found)
        return found

    def test_inserted_drum_validates_with_history(self):
        self.assertEqual(errors(validate_document(self.document)), [])
        self.assertEqual(len(self.document["undo"]), 1)
        self.assertNotIn(DRUM, self.document["undo"][0]["model"]["entities"])

    def test_entity_level_definition_rules(self):
        self.assertRejects(lambda e: e["ent-tank"].update(definition=load_definition()), "invalid_definition")
        self.assertRejects(lambda e: e[DRUM].pop("definition"), "missing_definition")
        self.assertRejects(lambda e: e[DRUM].update(template_version=2), "missing_template")
        found = self.assertRejects(lambda e: e[DRUM]["definition"]["symbol"].update(shape="spaceship"),
                                   "unsupported_symbol")
        self.assertIn(DRUM, found[0]["entity_ids"])

    def test_current_values_are_bounded_and_constrained(self):
        self.assertRejects(lambda e: e[DRUM]["parameters"]["shell_length"].update(value=9.0), "out_of_bounds")
        self.assertRejects(lambda e: e[DRUM]["parameters"]["inlet_offset"].update(value=0.35),
                           "definition_resolution_failed")
        self.assertRejects(lambda e: e[DRUM]["parameters"].update(wall={"value": 0.01, "unit": "m",
                                                                         "assumption_id": DRUM_ASSUMPTION}),
                           "unknown_parameter")
        self.assertRejects(lambda e: e[DRUM]["parameters"]["shell_length"].pop("assumption_id"), "missing_provenance")

    def test_ports_follow_the_definition(self):
        def wrong_dn(document):
            document["current"]["model"]["ports"]["port-drum-inlet"]["nominal_diameter_mm"] = 80

        document = copy.deepcopy(self.document)
        wrong_dn(document)
        self.assertIn("unsupported_nominal_diameter", codes(errors(validate_document(document))))
        document = copy.deepcopy(self.document)
        document["current"]["model"]["ports"]["port-drum-inlet"]["direction"] = "out"
        self.assertIn("wrong_port_direction", codes(errors(validate_document(document))))


# --- Offline renderers -------------------------------------------------------------


class OfflineRendererTest(unittest.TestCase):

    def test_pid_adapter_marks_composed_equipment_as_placeholder_not_failure(self):
        from skid.adapters.pid import render_pid

        _, original = render_pid(make_document()["current"])
        self.assertEqual(original["status"], "complete")
        svg, manifest = render_pid(drum_document()["current"])
        self.assertEqual(manifest["status"], "pending")
        missing = [d for d in manifest["diagnostics"] if d["code"] == "pid_symbol_missing"]
        self.assertEqual([d["entity_ids"][0] for d in missing], [DRUM])
        self.assertIn("horizontal_vessel", missing[0]["message"])
        self.assertIn('data-semantic-id="ent-drum"', svg)
        self.assertEqual(manifest["representations"]["port-drum-inlet"], ["sym-drum#port-drum-inlet"])

    def test_blender_plan_omits_composed_equipment_explicitly(self):
        from skid.adapters.blender_support.plan import plan_scene

        self.assertEqual(plan_scene(make_document()["current"])["status"], "complete")
        plan = plan_scene(drum_document()["current"])
        self.assertEqual(plan["status"], "pending")
        flagged = [d for d in plan["diagnostics"] if d["code"] == "geometry_unsupported"]
        self.assertEqual([d["entity_ids"] for d in flagged], [[DRUM]])
        self.assertEqual(plan["representations"][DRUM], [])
        self.assertFalse([o for o in plan["objects"] if o.get("properties", {}).get("entity_id") == DRUM])


if __name__ == "__main__":
    unittest.main()
