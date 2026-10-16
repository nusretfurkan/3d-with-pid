"""Composed equipment through the real CAD path: build, STEP reimport, edit, snapshot replay.

    .venv-cad/bin/python -m unittest tests.test_cad_equipment -v

The example horizontal drum and the inline box housing are inserted into the
pump skid (``tests.test_equipment`` scenario helpers) and built by
``skid.geometry.build_geometry`` with OCCT. Checks cover STEP reimport, port
faces, entity-namespaced definition provenance, a dimension edit that moves
one port, snapshot replay and tampering, unresolved placement and a
core-valid definition that OCCT cannot build. They do not establish
engineering adequacy of the illustrative geometry.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest

try:
    import OCP  # noqa: F401  (CadQuery's OCCT bindings, only in .venv-cad)
except ImportError:  # e.g. Blender's Python or a bare interpreter
    raise unittest.SkipTest("OCP/CadQuery unavailable; run with .venv-cad/bin/python")

from examples.make_fixture import make_document
from skid.core.commands import apply_command
from skid.equipment import MATERIAL_NAMES, PART_SIGNATURES, resolve_definition
from skid.geometry import SnapshotError, build_geometry, rebuild_from_recipe
from skid.geometry.equipment import RECIPE_ASSUMPTIONS
from skid.geometry.materials import MATERIALS
from skid.geometry.parts import PARTS
from skid.geometry.recipes import resolve_recipe
from skid.geometry.snapshot import with_digest
from skid.templates import world_port_position
from tests.test_equipment import (
    DRUM,
    DRUM_ASSUMPTION,
    HOUSING,
    assumption,
    command,
    defaults,
    drum_batch,
    drum_entity,
    housing_batch,
    inline_payload,
    load_definition,
)

_DIRS = []


def tearDownModule():
    for path in _DIRS:
        shutil.rmtree(path, ignore_errors=True)


def new_dir():
    path = tempfile.mkdtemp(prefix="test-cad-equipment-")
    _DIRS.append(path)
    return path


def load(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return json.load(handle)


def composed_document():
    document = make_document()
    document, _ = apply_command(document, drum_batch(document))
    document, _ = apply_command(document, housing_batch(document))
    return document


def set_drum(document, name, value):
    payload = {"entity_id": DRUM, "name": name, "quantity": {"value": value, "unit": "m",
                                                              "assumption_id": DRUM_ASSUMPTION}}
    document, _ = apply_command(document, command(document, "set_parameter", payload, f"cmd-test-{name}"))
    return document


def build(project):
    out = new_dir()
    return out, build_geometry(project, out)


class VocabularyTest(unittest.TestCase):

    def test_pure_vocabulary_mirrors_the_cad_parts_and_palette(self):
        self.assertEqual(set(PART_SIGNATURES), set(PARTS))
        for name, spec in PARTS.items():
            required, optional = PART_SIGNATURES[name]
            self.assertEqual(tuple(required), spec.required, name)
            self.assertEqual(set(optional), set(spec.optional), name)
        self.assertEqual(MATERIAL_NAMES, set(MATERIALS))

    def test_cad_and_core_resolve_definitions_identically(self):
        entity = drum_entity(shell_length=1.9, inlet_offset=0.7)
        core = resolve_definition(entity)
        values = defaults(load_definition())
        values.update(shell_length=1.9, inlet_offset=0.7)
        cad = resolve_recipe(load_definition()["geometry"], values)
        for key in ("components", "ports", "derived_mm", "constraints_checked"):
            self.assertEqual(core[key], cad[key], key)


class ComposedBuildTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.document = composed_document()
        cls.project = cls.document["current"]
        cls.out, cls.result = build(cls.project)
        cls.scene = load(cls.out, "scene.json")
        cls.verification = load(cls.out, "cad.verification.json")
        cls.snapshot = load(cls.out, "geometry.recipe.json")

    def test_build_is_complete_and_step_reimport_verifies_composed_ports(self):
        self.assertEqual((self.result["display"]["status"], self.result["cad"]["status"]), ("complete", "complete"))
        self.assertTrue(self.verification["ok"], self.verification["problems"])
        summary = self.verification["summary"]
        self.assertEqual(summary["ports_on_flange_faces"], summary["ports_checked"])
        reports = {r["port_id"]: r for r in self.verification["ports"]}
        for port_id in ("port-drum-inlet", "port-drum-outlet", "port-housing-inlet", "port-housing-outlet"):
            self.assertTrue(reports[port_id]["ok"], port_id)
            self.assertEqual(reports[port_id]["position_m"], list(world_port_position(self.project, port_id)))
        self.assertEqual(reports["port-drum-inlet"]["part"], "ent-drum#nozzle/inlet")
        self.assertEqual(self.result["cad"]["representations"]["port-drum-outlet"], ["ent-drum#nozzle/outlet"])
        components = {c["id"]: c for c in self.scene["components"]}
        for key in ("body", "support", "nozzle/inlet", "nozzle/outlet", "fasteners"):
            self.assertEqual(components[f"{DRUM}#{key}"]["kind"], "composed_equipment")
        for key in ("body", "cover", "support", "nozzle/inlet", "nozzle/outlet", "fasteners"):
            self.assertIn(f"{HOUSING}#{key}", components)
        # STEP dimensions: the drum shell (with heads) spans shell_length + 2 x R/2 along world -y.
        parts = {p["name"]: p for p in self.verification["parts"]}
        low, high = parts[f"{DRUM}#body"]["bounds_mm"]
        self.assertAlmostEqual(high[1] - low[1], 1400.0 + 2 * 150.0, delta=1e-3)
        self.assertAlmostEqual(high[2] - low[2], 600.0 + 2 * 1.5, delta=1e-3)  # shell plus seam bands at R + 1.5
        self.assertTrue(all(p["valid"] for p in self.verification["parts"]))

    def test_definition_provenance_is_namespaced_per_entity(self):
        records = {a["id"]: a for a in self.scene["assumptions"]}
        drum_local, housing_local = records[f"{DRUM}::rcp-appearance"], records[f"{HOUSING}::rcp-appearance"]
        self.assertEqual((drum_local["scope"], housing_local["scope"]), ("definition", "definition"))
        self.assertIn("horizontal-drum", drum_local["source"])
        self.assertIn("inline-box-housing", housing_local["source"])
        self.assertNotEqual(drum_local["applies_to"], housing_local["applies_to"])
        # Global recipe and core project assumptions stay global and untouched.
        self.assertEqual(records["rcp-appearance"]["scope"], "recipe")
        self.assertEqual(records["rcp-appearance"]["description"], RECIPE_ASSUMPTIONS["rcp-appearance"]["description"])
        self.assertEqual(records[DRUM_ASSUMPTION]["scope"], "project")
        self.assertEqual(records[DRUM_ASSUMPTION]["description"],
                         self.project["model"]["assumptions"][DRUM_ASSUMPTION]["description"])
        for component in self.scene["components"]:
            if component["semantic_id"] == DRUM:
                cited = set(component["assumptions"])
                self.assertIn(DRUM_ASSUMPTION, cited)
                self.assertIn(self.project["physical"]["placements"][DRUM]["assumption_id"], cited)
                self.assertTrue(any(a.startswith(f"{DRUM}::") for a in cited))
                self.assertFalse(cited & set(load_definition()["geometry"]["assumptions"]), cited)
        self.assertEqual(self.snapshot["assumptions"][f"{DRUM}::asm-drum-ports"]["scope"], "definition")

    def test_snapshot_retains_definition_and_current_parameters(self):
        unit = [u for u in self.snapshot["units_built"] if u["id"] == DRUM][0]
        self.assertEqual(unit["kind"], "composed_equipment")
        self.assertEqual(unit["generator"], "skid.equipment.definition/horizontal-drum@1")
        self.assertEqual(unit["definition"]["document"], load_definition())
        self.assertEqual(unit["definition"]["parameters_m"], defaults(load_definition()))
        self.assertEqual(len(unit["definition"]["sha256"]), 64)
        self.assertEqual({p["component_key"] for p in unit["ports"]}, {"nozzle/inlet", "nozzle/outlet"})

    def test_unchanged_snapshot_replays_and_edits_are_rejected(self):
        path = os.path.join(self.out, "geometry.recipe.json")
        rebuilt_dir = new_dir()
        rebuilt = rebuild_from_recipe(path, rebuilt_dir)
        self.assertEqual(rebuilt["cad"]["status"], "complete")
        self.assertEqual(load(rebuilt_dir, "scene.json")["components"], self.scene["components"])

        def rejected(edit, pattern):
            document = copy.deepcopy(self.snapshot)
            edit([u for u in document["units_built"] if u["id"] == DRUM][0])
            target = os.path.join(new_dir(), "geometry.recipe.json")
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(with_digest(document), handle)
            out = new_dir()
            with self.assertRaisesRegex(SnapshotError, pattern):
                rebuild_from_recipe(target, out)
            self.assertEqual(os.listdir(out), [])

        rejected(lambda u: u["definition"]["parameters_m"].update(shell_length=2.0), "parameters_mm differ")
        rejected(lambda u: u["components"][0]["parts"][0].update(origin=[0, 0, 900]), "differs from the recorded")
        rejected(lambda u: u["definition"]["document"].update(name="Edited drum"), "does not match its recorded sha256")
        rejected(lambda u: u["definition"].pop("sha256"), "definition must be")
        rejected(lambda u: u.pop("definition"), "missing \\['definition'\\]")

    def test_offline_renderers_still_cover_the_original_fixture(self):
        out, result = build(make_document()["current"])
        self.assertEqual(result["cad"]["status"], "complete")
        self.assertFalse([a for a in load(out, "scene.json")["assumptions"] if "::" in a["id"]])


class ComposedEditBuildTest(unittest.TestCase):

    def test_dimension_edit_moves_cad_and_invalidates_only_the_moved_route(self):
        document = set_drum(composed_document(), "inlet_offset", 0.7)
        project = document["current"]
        out, result = build(project)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("pending", "pending"))
        pending = [d["entity_ids"][0] for d in result["cad"]["diagnostics"] if d["code"] == "route_pending"]
        self.assertEqual(pending, ["conn-discharge-3"])
        verification = load(out, "cad.verification.json")
        self.assertTrue(verification["ok"], verification["problems"])
        reports = {r["port_id"]: r for r in verification["ports"]}
        self.assertEqual(reports["port-drum-inlet"]["position_m"], [2.7, -1.6, 1.2])
        self.assertTrue(reports["port-drum-inlet"]["ok"])
        self.assertEqual(result["cad"]["representations"]["conn-discharge-3"], [])
        self.assertTrue(result["cad"]["representations"]["conn-drum-out"])
        scene = load(out, "scene.json")
        ghost = [c for c in scene["components"] if c["id"] == "conn-discharge-3#pending/path"]
        self.assertTrue(ghost and ghost[0]["display_only"])
        inlet = [c for c in scene["components"] if c["id"] == f"{DRUM}#nozzle/inlet"][0]
        # Display mesh of the same shape, within the 0.5 mm tessellation deflection.
        self.assertAlmostEqual((inlet["bounds_m"]["min"][1] + inlet["bounds_m"]["max"][1]) / 2, -1.6, delta=5e-4)

    def test_shell_length_edit_reaches_step_dimensions(self):
        document = set_drum(composed_document(), "shell_length", 1.8)
        out, result = build(document["current"])
        self.assertEqual(result["cad"]["status"], "pending")  # both incident routes moved
        verification = load(out, "cad.verification.json")
        self.assertTrue(verification["ok"], verification["problems"])
        parts = {p["name"]: p for p in verification["parts"]}
        low, high = parts[f"{DRUM}#body"]["bounds_mm"]
        self.assertAlmostEqual(high[1] - low[1], 1800.0 + 2 * 150.0, delta=1e-3)
        unit = [u for u in load(out, "geometry.recipe.json")["units_built"] if u["id"] == DRUM][0]
        self.assertEqual(unit["definition"]["parameters_m"]["shell_length"], 1.8)
        self.assertEqual(unit["parameters_mm"]["shell_length"], 1800.0)

    def test_unresolved_placement_is_pending_without_composed_geometry(self):
        document = make_document()
        payload = inline_payload(document, "conn-discharge-3", drum_entity(), "drum", "L-106", (700, 220))
        document, _ = apply_command(document, command(document, "insert_equipment", payload, "cmd-test-u", assumptions={
            DRUM_ASSUMPTION: assumption("Illustrative drum for the unresolved-placement test.")}))
        out, result = build(document["current"])
        self.assertEqual(result["display"]["status"], "pending")
        omitted = [d for d in result["display"]["diagnostics"] if d["code"] == "geometry_omitted"]
        self.assertEqual([d["entity_ids"] for d in omitted], [[DRUM]])
        self.assertFalse([c for c in load(out, "scene.json")["components"] if c["semantic_id"] == DRUM])
        self.assertEqual(result["display"]["representations"][DRUM], [])

    def test_core_valid_definition_that_occt_cannot_build_fails_explicitly(self):
        entity = drum_entity()
        nozzle = [c for c in entity["definition"]["geometry"]["components"] if c["key"] == "nozzle/outlet"][0]
        nozzle["parts"][0]["neck_length"] = 10  # shorter than the 18 mm flange: a part-level failure
        document = make_document()
        payload = inline_payload(document, "conn-discharge-3", entity, "drum", "L-106", (700, 220),
                                 placement={"position_m": [2.7, -1.6, 0.0], "rotation_deg": [0.0, 0.0, -90.0],
                                            "status": "placed", "manual": True, "assumption_id": DRUM_ASSUMPTION})
        document, _ = apply_command(document, command(document, "insert_equipment", payload, "cmd-test-f", assumptions={
            DRUM_ASSUMPTION: assumption("Illustrative drum for the failed-build test.")}))
        _, result = build(document["current"])
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("failed", "failed"))
        failed = [d for d in result["display"]["diagnostics"] if d["code"] == "geometry_build_failed"]
        self.assertEqual([d["entity_ids"] for d in failed], [[DRUM]])
        self.assertIn("neck_length", failed[0]["message"])


if __name__ == "__main__":
    unittest.main()
