"""CAD of an empty project and of the shell-and-tube example (needs CadQuery/OCP: run with .venv-cad/bin/python).

The empty project exports a genuine STEP holding only its named root product (no fake solids) and must reimport as
exactly that; the exchanger's four DN50 flange faces are checked on reimport at the definition defaults and after a
shell-length change, against the core's own port positions.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    from skid.geometry import build_geometry, rebuild_from_recipe
    from skid.geometry.build import verify_step
    from skid.geometry.step import StepError, header_facts, read_step
except ImportError as exc:  # pragma: no cover - only without the CAD environment
    raise unittest.SkipTest(f"CadQuery/OCP not available: {exc}")

from skid.core import apply_command, new_document
from skid.templates import world_port_position
from tests import empty_start_support as fx

PORTS = ("port-e-101-tube-inlet", "port-e-101-tube-outlet", "port-e-101-shell-inlet", "port-e-101-shell-outlet")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class EmptyProjectCadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = cls.tmp.name
        cls.project = new_document("proj-empty-cad", "Empty CAD")["current"]
        cls.result = build_geometry(cls.project, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_empty_views_are_complete_and_hold_nothing(self):
        display, cad = self.result["display"], self.result["cad"]
        self.assertEqual((display["status"], cad["status"]), ("complete", "complete"), cad["diagnostics"])
        self.assertEqual(display["representations"], {})
        self.assertEqual((cad["group_count"], cad["part_count"], cad["solid_count"]), (0, 0, 0))
        scene = read(os.path.join(self.out, "scene.json"))
        self.assertEqual((scene["components"], scene["ports"]), ([], []))
        summary = read(os.path.join(self.out, "cad.verification.json"))["summary"]
        self.assertTrue(summary["empty_root_product"])
        self.assertEqual((summary["groups"], summary["parts"], summary["solids"], summary["ports_checked"]),
                         (0, 0, 0, 0))

    def test_empty_step_is_valid_and_reimports_as_one_empty_root_product(self):
        path = os.path.join(self.out, "model.step")
        with open(path, encoding="ascii") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("ISO-10303-21;"))
        self.assertIn("PRODUCT('proj-empty-cad'", text)
        self.assertNotIn("MANIFOLD_SOLID_BREP", text)
        self.assertNotIn("NEXT_ASSEMBLY_USAGE_OCCURRENCE", text)
        self.assertEqual(text.count("CARTESIAN_POINT"), 1, "only the root placement origin; no geometry")
        facts = header_facts(text)
        self.assertTrue(facts["length_unit_millimetre"])
        self.assertIn("source_project=proj-empty-cad", facts["description"])
        data = read_step(path)
        self.assertEqual((data["root"], data["groups"], data["empty"]), ("proj-empty-cad", [], True))

    def test_empty_snapshot_replays(self):
        with tempfile.TemporaryDirectory() as again:
            result = rebuild_from_recipe(os.path.join(self.out, "geometry.recipe.json"), again)
            self.assertEqual(result["cad"]["status"], "complete", result["cad"]["diagnostics"])
            self.assertEqual(read_step(os.path.join(again, "model.step"))["groups"], [])

    def test_a_non_empty_root_is_never_accepted_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "box.step")
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
            self.assertEqual(writer.Write(path), IFSelect_ReturnStatus.IFSelect_RetDone)
            with self.assertRaises(StepError):
                read_step(path)


class ExchangerCadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = new_document("proj-hx-cad")
        equipment, _ = fx.exchanger_operations()
        del equipment[0]["assumptions"]["asm-items-hx"]  # only the exchanger is added here
        cls.document, _ = apply_command(document, fx.batch(document, "cmd-hx", equipment[:1]))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.default_dir = os.path.join(cls.tmp.name, "default")
        cls.longer_dir = os.path.join(cls.tmp.name, "longer")
        os.makedirs(cls.default_dir)
        os.makedirs(cls.longer_dir)
        cls.default = build_geometry(cls.document["current"], cls.default_dir)
        command = {"id": "cmd-longer", "expected_revision": 1, "type": "set_parameter",
                   "payload": {"entity_id": "ent-e-101", "name": "shell_length",
                               "quantity": {"value": 2.2, "unit": "m", "assumption_id": "asm-hx-longer"}},
                   "assumptions": {"asm-hx-longer": fx.assumption("Illustrative 2.2 m shell length for the "
                                                                  "parameter-change check.")}}
        cls.longer_document, _ = apply_command(cls.document, command)
        cls.longer = build_geometry(cls.longer_document["current"], cls.longer_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_four_faces(self, project, result, directory):
        cad = result["cad"]
        # Unconnected required ports keep the outputs pending, but the STEP itself must verify.
        self.assertEqual(cad["status"], "pending", cad["diagnostics"])
        self.assertIn("required_port_unconnected", {d["code"] for d in cad["diagnostics"]})
        verification = read(os.path.join(directory, "cad.verification.json"))
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual((verification["summary"]["ports_checked"], verification["summary"]["ports_on_flange_faces"]),
                         (4, 4))
        reports = {r["port_id"]: r for r in verification["ports"]}
        self.assertEqual(set(reports), set(PORTS))
        for port_id in PORTS:
            expected = world_port_position(project, port_id)
            for a, b in zip(reports[port_id]["position_m"], expected):
                self.assertAlmostEqual(a, b, places=9)
            self.assertTrue(reports[port_id]["ok"])
        return {p["name"]: p for p in verification["parts"]}

    def test_four_flange_faces_at_defaults_and_after_a_parameter_change(self):
        before = self.check_four_faces(self.document["current"], self.default, self.default_dir)
        after = self.check_four_faces(self.longer_document["current"], self.longer, self.longer_dir)
        # The shell part runs between the two shell flanges' outer faces: shell_length minus two 15 mm half-tubesheets.
        length = lambda part: part["bounds_mm"][1][0] - part["bounds_mm"][0][0]  # noqa: E731
        self.assertAlmostEqual(length(before["ent-e-101#shell"]), 1570.0, delta=1e-3)
        self.assertAlmostEqual(length(after["ent-e-101#shell"]), 2170.0, delta=1e-3)
        moved = world_port_position(self.longer_document["current"], "port-e-101-tube-inlet")
        self.assertAlmostEqual(moved[0], 2.6 - 1.1 - 0.15, places=9)

    def test_exchanger_parts_cover_shell_channel_tubesheets_bonnet_and_nozzles(self):
        parts = {p["name"] for p in read(os.path.join(self.default_dir, "cad.verification.json"))["parts"]}
        for key in ("shell", "channel", "tubesheets", "rear_bonnet", "joints", "support", "anchors",
                    "nozzle/tube_inlet", "nozzle/tube_outlet", "nozzle/shell_inlet", "nozzle/shell_outlet"):
            self.assertIn(f"ent-e-101#{key}", parts)

    def test_an_exported_assembly_is_not_accepted_as_an_empty_project(self):
        empty_scene = type("Scene", (), {"units": []})()
        identity = {"project_id": "proj-hx-cad", "source_revision": 1, "input_digest": None}
        header = {"description": "not the header"}
        report = verify_step(os.path.join(self.default_dir, "model.step"), identity, empty_scene, [], header)
        self.assertFalse(report["ok"])
        self.assertIn("the source has no groups, but the STEP root is not an empty product", report["problems"])


if __name__ == "__main__":
    unittest.main()
