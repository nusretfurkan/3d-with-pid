"""Authored recipe composition: examples/recipes through the shared compiler, STEP and scene path.

    .venv-cad/bin/python -m unittest tests.test_cad_recipes -v

The horizontal drum is composed only from existing parts. These tests check
its explicit ports and provenance, parameter overrides and constraints,
rejection of malformed recipes, and the same STEP reimport verification as
the skid. They do not establish general equipment generation.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import tempfile
import unittest

try:
    import OCP  # noqa: F401  (CadQuery's OCCT bindings, only in .venv-cad)
except ImportError:  # e.g. Blender's Python running the existing suite
    raise unittest.SkipTest("OCP/CadQuery unavailable; run with .venv-cad/bin/python")

from skid.geometry.authored import build_authored, load_and_resolve
from skid.geometry.recipes import ASSUMPTION_CATEGORIES, RecipeError, compile_recipe, evaluate, resolve_recipe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRUM = os.path.join(ROOT, "examples", "recipes", "horizontal_drum.recipe.json")


def drum():
    with open(DRUM, encoding="utf-8") as handle:
        return json.load(handle)


class DrumRecipeTest(unittest.TestCase):

    def test_ports_and_provenance_are_explicit(self):
        resolved = load_and_resolve(DRUM)
        self.assertEqual(set(resolved["ports"]), {"inlet", "outlet"})
        inlet, outlet = resolved["ports"]["inlet"], resolved["ports"]["outlet"]
        # Defaults: L = 1400, R = 300, Hc = 750, inlet 600 mm from the left tangent line.
        self.assertEqual(inlet["position_mm"], [-100.0, 0.0, 1200.0])
        self.assertEqual(inlet["direction"], [0.0, 0.0, 1.0])
        self.assertEqual(outlet["position_mm"], [700.0 + 150.0 + 130.0, 0.0, 750.0])
        self.assertEqual(outlet["direction"], [1.0, 0.0, 0.0])
        assumptions = resolved["assumptions"]
        for record in assumptions.values():
            self.assertIn(record["category"], ASSUMPTION_CATEGORIES)
        for name, parameter in resolved["parameters"].items():
            self.assertIn(parameter["assumption_id"], assumptions, name)
            self.assertEqual(parameter["source"], "recipe default")
        for port in resolved["ports"].values():
            self.assertIn(port["assumption_id"], assumptions)
            self.assertEqual(port["nominal_diameter_mm"], 50)
        for component in resolved["components"]:
            self.assertTrue(set(component["assumptions"]) <= set(assumptions))
            self.assertIn("rcp-appearance", component["assumptions"])
        self.assertEqual(len(resolved["constraints_checked"]), 5)
        self.assertTrue(all(c["value"] >= 0 for c in resolved["constraints_checked"]))

    def test_ports_lie_on_outward_flange_faces_of_their_components(self):
        compiled = compile_recipe(load_and_resolve(DRUM))
        for name, report in compiled["port_checks"].items():
            self.assertTrue(report["ok"], name)
            self.assertLessEqual(abs(report["beyond_port_plane_mm"]), 1e-4)
            self.assertAlmostEqual(report["lateral_extent_mm"][1], 75.0, places=4)

    def test_override_moves_ports_with_the_parameter(self):
        base = load_and_resolve(DRUM)
        longer = load_and_resolve(DRUM, {"shell_length": 2.0})
        self.assertEqual(longer["parameters"]["shell_length"], {"value": 2.0, "unit": "m",
                                                               "assumption_id": "asm-drum-dimensions",
                                                               "source": "override"})
        moved = {n: [b - a for a, b in zip(base["ports"][n]["position_mm"], longer["ports"][n]["position_mm"])]
                 for n in ("inlet", "outlet")}
        self.assertEqual(moved["outlet"], [300.0, 0.0, 0.0])
        self.assertEqual(moved["inlet"], [-300.0, 0.0, 0.0])
        self.assertTrue(all(r["ok"] for r in compile_recipe(longer)["port_checks"].values()))

    def test_constraints_reject_clashing_variants(self):
        with self.assertRaisesRegex(RecipeError, "clear the left saddle"):
            load_and_resolve(DRUM, {"inlet_offset": 0.35})
        with self.assertRaisesRegex(RecipeError, "150 mm below the shell"):
            load_and_resolve(DRUM, {"shell_diameter": 1.2, "centreline_height": 0.7})

    def test_malformed_recipes_are_rejected(self):
        def rejected(mutate, pattern, overrides=None):
            recipe = drum()
            mutate(recipe)
            with self.assertRaisesRegex(RecipeError, pattern):
                resolved = resolve_recipe(recipe, overrides)
                compile_recipe(resolved)

        rejected(lambda r: None, "outside", {"shell_length": 9.0})
        rejected(lambda r: None, "unknown parameters", {"wall_thickness": 0.01})
        rejected(lambda r: r["parameters"]["shell_length"].update(assumption_id="asm-missing"), "unknown assumption")
        rejected(lambda r: r["components"][0]["assumptions"].append("asm-missing"), "unknown assumption")
        rejected(lambda r: r["ports"]["inlet"].update(component="body"), "does not lie on an outward planar face")
        rejected(lambda r: r["ports"]["inlet"].update(component="nozzle/missing"), "unknown component")
        rejected(lambda r: r["ports"]["inlet"].update(nominal_diameter_mm=80), "only DN50")
        rejected(lambda r: r["ports"]["outlet"].update(position=["=xR + Hd + 100", 0, "=Hc"]),
                 "does not lie on an outward planar face")
        rejected(lambda r: r["components"][0]["parts"].append({"part": "teleporter"}), "unknown part")
        rejected(lambda r: r["derived"].append(["evil", "=__import__('os')"]), "unsupported syntax")
        rejected(lambda r: r["derived"].append(["bad", "=L / 0"]), "cannot be evaluated")
        rejected(lambda r: r["components"][1].update(key="body"), "duplicate component key")
        rejected(lambda r: r.update(extra=True), "unsupported")
        rejected(lambda r: r["assumptions"]["rcp-appearance"].update(category="marketing"), "category")
        rejected(lambda r: r["components"][0].update(material="unobtainium"), "unknown material")

    def test_expression_evaluator_is_arithmetic_only(self):
        self.assertEqual(evaluate("max(2, 3) * sqrt(16) - abs(-1)", {}), 11.0)
        self.assertAlmostEqual(evaluate("sin(30)", {}), 0.5)
        for text in ("a.b", "x[0]", "lambda: 1", "1 if 1 else 2", "open('f')", "2 ** 5000"):
            with self.assertRaises(RecipeError, msg=text):
                evaluate(text, {"a": 1.0, "x": 1.0})

    def test_build_authored_runs_the_skid_step_and_scene_path(self):
        out = tempfile.mkdtemp(prefix="test-cad-recipe-")
        self.addCleanup(shutil.rmtree, out, True)
        events = []
        result = build_authored(DRUM, out, {"shell_length": 1.6}, emit=events.append)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("complete", "complete"))
        identity = (result["cad"]["project_id"], result["cad"]["source_revision"])
        self.assertEqual(identity, ("recipe:horizontal-drum-demo", 0))
        with open(os.path.join(out, "cad.verification.json"), encoding="utf-8") as handle:
            verification = json.load(handle)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(verification["summary"]["ports_on_flange_faces"], 2)
        with open(os.path.join(out, "scene.json"), encoding="utf-8") as handle:
            scene = json.load(handle)
        ports = {p["name"]: p for p in scene["ports"]}
        self.assertEqual(ports["outlet"]["position_m"], [0.8 + 0.15 + 0.13, 0.0, 0.75])
        self.assertEqual(sorted(c["component_key"] for c in scene["components"]),
                         ["body", "fasteners", "nozzle/inlet", "nozzle/outlet", "support"])
        body = [c for c in scene["components"] if c["component_key"] == "body"][0]
        self.assertAlmostEqual(body["bounds_m"]["max"][0] - body["bounds_m"]["min"][0], 1.6 + 2 * 0.15, delta=1e-5)
        with open(os.path.join(out, "geometry.recipe.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["source"]["recipe"]["parameters"]["shell_length"]["source"], "override")
        self.assertEqual([e["kind"] for e in events if e["kind"] != "activity"], ["preview", "artifact", "artifact"])


if __name__ == "__main__":
    unittest.main()
