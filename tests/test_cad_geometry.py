"""CAD/display geometry spike: skid.geometry.build_geometry against the real fixture and core commands.

Run with the CadQuery environment:

    .venv-cad/bin/python -m unittest tests.test_cad_geometry -v

Covers STEP reimport (own XCAF reader and CadQuery's independent importer),
scene schema and unit conversion, port frames, progressive previews and
events, supported parameter edits with core route invalidation, rotated and
translated placements, determinism, rebuild from the saved recipe,
cancellation and malformed input.
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

from skid.core import apply_command
from skid.core.validation import canonical_digest, validate_project
from skid.templates import world_port_position

from skid.geometry import ADAPTER_VERSION, build_geometry, rebuild_from_recipe
from skid.geometry import kernel, parts
from skid.geometry.build import LINEAR_DEFLECTION_MM
from skid.geometry.equipment import RECIPE_ASSUMPTIONS
from skid.geometry.step import header_facts, read_step

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "examples", "pump_skid.json")
SCENE_FIELDS = {"schema_version", "project_id", "source_revision", "input_digest", "units", "up_axis", "components",
                "ports", "assumptions", "bounds_m"}
COMPONENT_FIELDS = {"id", "semantic_id", "component_key", "tag", "positions", "normals", "indices", "material",
                    "bounds_m", "display_only"}
MANIFEST_FIELDS = {"adapter", "adapter_version", "project_id", "source_revision", "input_digest", "status",
                   "diagnostics", "representations", "files"}
PUMP_KEYS = ["baseplate", "bearing_frame", "body", "coupling", "coupling_guard", "fasteners", "motor", "motor_feet",
             "motor_terminal_box", "nozzle/inlet", "nozzle/outlet", "shaft", "support"]

_TEMP_DIRS = []
_CACHE = {}


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def fixture_document():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def new_dir():
    path = tempfile.mkdtemp(prefix="test-cad-")
    _TEMP_DIRS.append(path)
    return path


def command(document, kind, payload):
    command.counter = getattr(command, "counter", 0) + 1
    return apply_command(document, {"id": f"cmd-test-cad-{command.counter}",
                                    "expected_revision": document["current"]["revision"], "type": kind,
                                    "payload": payload})


def pump_quantity(value):
    return {"value": value, "unit": "m", "assumption_id": "asm-pump-dimensions"}


def build(project, emit=None, cancelled=None):
    out = new_dir()
    result = build_geometry(project, out, emit=emit, cancelled=cancelled)
    return out, result


def load(out, name):
    with open(os.path.join(out, name), encoding="utf-8") as handle:
        return json.load(handle)


def fixture_build():
    if "fixture" not in _CACHE:
        events = []
        project = fixture_document()["current"]
        before = copy.deepcopy(project)
        out, result = build(project, emit=events.append)
        _CACHE["fixture"] = (project, before, out, result, events)
    return _CACHE["fixture"]


def components_by_id(scene):
    return {c["id"]: c for c in scene["components"]}


def mesh_points(component):
    p = component["positions"]
    return [(p[i], p[i + 1], p[i + 2]) for i in range(0, len(p), 3)]


class FixtureBuildTest(unittest.TestCase):
    """One build of the unchanged fixture, inspected from several sides."""

    @classmethod
    def setUpClass(cls):
        cls.project, cls.before, cls.out, cls.result, cls.events = fixture_build()
        cls.scene = load(cls.out, "scene.json")
        cls.verification = load(cls.out, "cad.verification.json")

    def test_input_is_not_mutated(self):
        self.assertEqual(self.project, self.before)

    def test_manifests_share_snapshot_identity_and_cover_every_semantic_id(self):
        digest = canonical_digest(self.project)
        model = self.project["model"]
        expected_ids = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
        for name in ("display", "cad"):
            manifest = self.result[name]
            self.assertEqual(manifest, load(self.out, f"{name}.manifest.json"))
            self.assertLessEqual(MANIFEST_FIELDS, set(manifest))
            self.assertEqual((manifest["adapter"], manifest["adapter_version"]), (name, "0.2.0"))
            self.assertEqual((manifest["project_id"], manifest["source_revision"], manifest["input_digest"]),
                             ("project-pump-skid", 0, digest))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["diagnostics"], [])
            self.assertLessEqual(expected_ids, set(manifest["representations"]))
            for path in manifest["files"]:
                self.assertTrue(os.path.getsize(os.path.join(self.out, path)) > 0, path)
        self.assertEqual(self.result["cad"]["files"], ["model.step", "geometry.recipe.json", "cad.verification.json"])
        self.assertEqual(self.result["display"]["files"][0], "scene.json")
        for scene_like in (self.scene, load(self.out, "geometry.recipe.json"), self.verification["identity"]):
            self.assertEqual((scene_like["project_id"], scene_like["source_revision"], scene_like["input_digest"]),
                             ("project-pump-skid", 0, digest))
        with open(os.path.join(self.out, "model.step"), encoding="ascii") as handle:
            facts = header_facts(handle.read())
        self.assertIn(f"input_digest={digest};", facts["description"])
        self.assertTrue(facts["length_unit_millimetre"])
        self.assertEqual(self.verification["header"], facts)

    def test_scene_schema_units_and_mesh_integrity(self):
        scene = self.scene
        self.assertLessEqual(SCENE_FIELDS, set(scene))
        self.assertEqual((scene["schema_version"], scene["units"], scene["up_axis"]), (1, "m", "Z"))
        self.assertEqual(scene["status"], "complete")
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        for c in scene["components"]:
            self.assertLessEqual(COMPONENT_FIELDS, set(c))
            self.assertEqual(c["id"], f"{c['semantic_id']}#{c['component_key']}")
            self.assertNotIn(c["tag"], c["id"])
            n = len(c["positions"])
            self.assertTrue(n % 3 == 0 and n == len(c["normals"]) and n > 0)
            self.assertTrue(len(c["indices"]) % 3 == 0 and c["indices"])
            self.assertTrue(all(0 <= i < n // 3 for i in c["indices"]))
            for i in range(0, n, 3):
                length = math.sqrt(sum(v * v for v in c["normals"][i:i + 3]))
                self.assertAlmostEqual(length, 1.0, delta=3e-3)
            points = mesh_points(c)
            for axis in range(3):
                values = [p[axis] for p in points]
                self.assertEqual(c["bounds_m"]["min"][axis], min(values))
                self.assertEqual(c["bounds_m"]["max"][axis], max(values))
                lo[axis], hi[axis] = min(lo[axis], min(values)), max(hi[axis], max(values))
            self.assertRegex(c["material"]["color"], r"^#[0-9a-f]{6}$")
            self.assertTrue(0 <= c["material"]["metalness"] <= 1 and 0 <= c["material"]["roughness"] <= 1)
        self.assertEqual(scene["bounds_m"], {"min": lo, "max": hi})
        # World metres, Z up: the deck is z = 0, the tank is 1.2 m tall, the skid is a few metres long.
        self.assertAlmostEqual(lo[2], 0.0, places=6)
        self.assertAlmostEqual(hi[2], 1.2, delta=0.13)
        self.assertTrue(2.5 < hi[0] - lo[0] < 4.0)

    def test_triangle_budget(self):
        self.assertLess(self.scene["triangle_count"], 250_000)
        self.assertEqual(self.scene["triangle_count"],
                         sum(len(c["indices"]) // 3 for c in self.scene["components"]))

    def test_component_ids_and_pump_detail(self):
        keys = sorted(c["component_key"] for c in self.scene["components"] if c["semantic_id"] == "ent-pump")
        self.assertEqual(keys, PUMP_KEYS)
        by_id = components_by_id(self.scene)
        self.assertEqual(by_id["ent-pump#body"]["material"]["color"], "#1f4f8f")
        self.assertEqual(by_id["ent-pump#motor"]["material"]["color"], "#6b7378")
        self.assertEqual(by_id["ent-pump#coupling_guard"]["material"]["color"], "#f0b020")
        for cid in ("conn-suction-1#pipe", "conn-suction-1#flange/from", "conn-suction-1#bolting/to"):
            self.assertIn(cid, by_id)

    def test_ports_match_template_positions_and_world_directions(self):
        expected_dirs = {
            "port-tank-outlet": (1, 0, 0), "port-pump-inlet": (-1, 0, 0), "port-pump-outlet": (0, 0, 1),
            "port-suction-valve-inlet": (-1, 0, 0), "port-check-valve-outlet": (1, 0, 0),
            # The discharge valve and boundary are rotated -90 degrees about z.
            "port-discharge-valve-inlet": (0, 1, 0), "port-discharge-valve-outlet": (0, -1, 0),
            "port-boundary-inlet": (0, 1, 0),
        }
        ports = {p["id"]: p for p in self.scene["ports"]}
        self.assertEqual(set(ports), set(self.project["model"]["ports"]))
        for pid, port in ports.items():
            self.assertEqual(tuple(port["position_m"]), world_port_position(self.project, pid))
            self.assertAlmostEqual(math.sqrt(sum(c * c for c in port["direction"])), 1.0, places=12)
            if pid in expected_dirs:
                self.assertEqual(tuple(port["direction"]), expected_dirs[pid], pid)
        # Every current route starts and ends exactly at its ports.
        for cid, route in self.project["physical"]["routes"].items():
            connection = self.project["model"]["connections"][cid]
            self.assertEqual(tuple(route["points_m"][0]), tuple(ports[connection["from_port_id"]]["position_m"]))
            self.assertEqual(tuple(route["points_m"][-1]), tuple(ports[connection["to_port_id"]]["position_m"]))

    def test_mesh_flange_faces_lie_on_port_planes(self):
        by_id = components_by_id(self.scene)
        for port in self.scene["ports"]:
            component = by_id[port["component_id"]]
            p, d = port["position_m"], port["direction"]
            heights = [sum((v[i] - p[i]) * d[i] for i in range(3)) for v in mesh_points(component)]
            self.assertAlmostEqual(max(heights), 0.0, delta=1e-5, msg=port["id"])
            on_face = [v for v, h in zip(mesh_points(component), heights) if abs(h) < 1e-5]
            radial = max(math.dist(v, p) for v in on_face)
            self.assertAlmostEqual(radial, 0.075, delta=1e-4, msg=f"{port['id']} flange rim")

    def test_step_reimport_verification(self):
        v = self.verification
        self.assertTrue(v["ok"], v["problems"])
        cad_components = [c for c in self.scene["components"] if not c["display_only"]]
        s = v["summary"]
        self.assertEqual(s["parts"], len(cad_components))
        self.assertEqual(s["groups"], 13)
        self.assertEqual(s["length_unit"], "mm")
        self.assertEqual((s["ports_checked"], s["ports_on_flange_faces"]), (10, 10))
        self.assertEqual(s["envelope_checks"], s["envelope_checks_passed"])
        self.assertGreaterEqual(s["envelope_checks"], 10)
        self.assertLessEqual(s["max_bounds_delta_mm"], 1e-3)
        self.assertLessEqual(s["max_volume_relative_delta"], 1e-6)
        self.assertEqual(s["max_placement_delta_mm"], 0.0)
        self.assertLessEqual(s["max_mesh_reach_mm"], 2 * LINEAR_DEFLECTION_MM + 0.01)
        self.assertLessEqual(s["max_mesh_outside_mm"], 0.01)
        self.assertEqual(self.result["cad"]["solid_count"], s["solids"])

    def test_step_reread_independently_matches_placements_and_mesh_bounds(self):
        data = read_step(os.path.join(self.out, "model.step"))
        self.assertEqual(data["root"], "project-pump-skid")
        groups = {g["name"]: g for g in data["groups"]}
        placements = self.project["physical"]["placements"]
        self.assertEqual(set(groups), set(placements) | set(self.project["physical"]["routes"]))
        for eid, placement in placements.items():
            translation = [groups[eid]["location"][i][3] for i in range(3)]
            for got, want in zip(translation, placement["position_m"]):
                self.assertAlmostEqual(got, want * 1000.0, places=9)
        by_id = components_by_id(self.scene)
        for group in data["groups"]:
            for part in group["parts"]:
                self.assertEqual(part["name"], part["product"])
                (x0, y0, z0), (x1, y1, z1) = kernel.bounds(part["shape"])
                mesh = by_id[part["name"]]["bounds_m"]
                for got, want in zip((x0, y0, z0), mesh["min"]):
                    self.assertLessEqual(abs(got / 1000.0 - want), 1.1e-3, part["name"])
                for got, want in zip((x1, y1, z1), mesh["max"]):
                    self.assertLessEqual(abs(got / 1000.0 - want), 1.1e-3, part["name"])
        # The pump motor frame keeps the motor envelope: 0.35 m long, 0.22 m diameter.
        motor = by_id["ent-pump#motor"]["bounds_m"]
        self.assertAlmostEqual(motor["max"][0] - motor["min"][0], 0.35, delta=1e-5)
        self.assertAlmostEqual(motor["max"][1] - motor["min"][1], 0.22, delta=1e-3)
        self.assertAlmostEqual(motor["max"][2], 0.2 + 0.11, delta=1e-3)

    def test_cadquery_importer_reads_the_same_assembly(self):
        import cadquery as cq
        from cadquery.occ_impl.importers.assembly import importStep

        path = os.path.join(self.out, "model.step")
        shape = cq.importers.importStep(path)
        self.assertEqual(len(shape.solids().vals()), self.result["cad"]["solid_count"])
        bb = shape.val().BoundingBox()
        cad = [c for c in self.scene["components"] if not c["display_only"]]
        lo = [min(c["bounds_m"]["min"][i] for c in cad) * 1000 for i in range(3)]
        hi = [max(c["bounds_m"]["max"][i] for c in cad) * 1000 for i in range(3)]
        for got, want in zip((bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax), lo + hi):
            self.assertLessEqual(abs(got - want), 2.0)  # OCCT's default boxes are slightly enlarged
        assy = cq.Assembly()
        importStep(assy, path)
        pump = assy.objects["ent-pump"]
        self.assertEqual(pump.loc.toTuple()[0], (1500.0, 0.0, 0.0))
        self.assertEqual(sorted(c.name for c in pump.children), [f"ent-pump#{k}" for k in PUMP_KEYS])

    def test_display_only_components_are_marked_and_excluded_from_cad(self):
        display_only = [c["id"] for c in self.scene["components"] if c["display_only"]]
        self.assertEqual(display_only, ["ent-outlet-boundary#body"])
        self.assertEqual(self.result["cad"]["excluded_display_only"], display_only)
        cad_ids = {cid for ids in self.result["cad"]["representations"].values() for cid in ids}
        self.assertNotIn("ent-outlet-boundary#body", cad_ids)
        self.assertIn("ent-outlet-boundary#body", self.result["display"]["representations"]["ent-outlet-boundary"])
        self.assertEqual(self.result["cad"]["representations"]["port-pump-outlet"], ["ent-pump#nozzle/outlet"])

    def test_events_and_immutable_previews_follow_actual_work(self):
        events = self.events
        self.assertEqual(events[0]["kind"], "activity")
        previews = [e for e in events if e["kind"] == "preview"]
        self.assertEqual([e["path"] for e in previews], [f"preview/display-{i:04d}.json" for i in range(1, 14)])
        self.assertEqual([e["completed"] for e in previews], list(range(1, 14)))
        self.assertTrue(all(e["total"] == 13 and e["artifact"] == "display" for e in previews))
        artifacts = [(e["artifact"], e["path"]) for e in events if e["kind"] == "artifact"]
        self.assertEqual(artifacts, [("display", "scene.json"), ("cad", "model.step")])
        kinds = [e["kind"] for e in events]
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "preview"), kinds.index("artifact"))
        cad_activity = [e["message"] for e in events if e["artifact"] == "cad" and e["kind"] == "activity"]
        self.assertTrue(cad_activity[0].startswith("Exporting STEP"))
        self.assertTrue(cad_activity[1].startswith("Re-importing"))
        for event in events:
            self.assertLessEqual(set(event), {"kind", "artifact", "message", "path", "completed", "total"})
        counts, seen_routes = [], False
        for index, event in enumerate(previews, 1):
            snapshot = load(self.out, event["path"])
            self.assertLessEqual(SCENE_FIELDS, set(snapshot))
            self.assertEqual(snapshot["preview"]["sequence"], index)
            self.assertFalse(snapshot["preview"]["final"])
            self.assertEqual(snapshot["status"], "working")
            self.assertEqual(snapshot["input_digest"], self.scene["input_digest"])
            counts.append(len(snapshot["components"]))
            is_route = snapshot["preview"]["unit_id"].startswith("conn-")
            self.assertFalse(seen_routes and not is_route, "equipment must precede routes")
            seen_routes |= is_route
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], len(self.scene["components"]))
        last = load(self.out, previews[-1]["path"])
        self.assertEqual(last["components"], self.scene["components"])
        flow = [load(self.out, e["path"])["preview"]["unit_id"] for e in previews[:6]]
        self.assertEqual(flow, ["ent-tank", "ent-suction-valve", "ent-pump", "ent-check-valve",
                                "ent-discharge-valve", "ent-outlet-boundary"])

    def test_recipe_and_assumptions_are_saved_with_provenance(self):
        recipe = load(self.out, "geometry.recipe.json")
        self.assertEqual(recipe["length_unit"], "mm")
        self.assertEqual(recipe["world"]["up_axis"], "Z")
        units = {u["id"]: u for u in recipe["units_built"]}
        pump = units["ent-pump"]
        self.assertEqual(pump["generator"], "skid.geometry.equipment/centrifugal_pump@1")
        self.assertEqual(pump["parameters_mm"]["body_height"], 400.0)
        self.assertEqual([c["key"] for c in pump["components"]][:1], ["body"])
        self.assertTrue(all(call["part"] in parts.PARTS for c in pump["components"] for call in c["parts"]))
        assumptions = {a["id"]: a for a in self.scene["assumptions"]}
        project_assumptions = self.project["model"]["assumptions"]
        for c in self.scene["components"]:
            self.assertIn("rcp-appearance", c["assumptions"])
            self.assertTrue(any(a in RECIPE_ASSUMPTIONS and a != "rcp-appearance" for a in c["assumptions"]), c["id"])
            for aid in c["assumptions"]:
                self.assertIn(c["id"], assumptions[aid]["applies_to"])
                self.assertTrue(assumptions[aid]["description"])
                self.assertEqual(assumptions[aid]["scope"], "project" if aid in project_assumptions else "recipe")
        self.assertIn("asm-pump-dimensions", components_by_id(self.scene)["ent-pump#motor"]["assumptions"])
        self.assertIn("asm-skid-layout", components_by_id(self.scene)["ent-pump#motor"]["assumptions"])
        self.assertEqual(set(recipe["assumptions"]), set(assumptions))

    def assertSameStepStructure(self, out):
        """STEP bytes vary (OCCT counters, style order, time); the reimported structure must not."""
        mine, theirs = self.verification, load(out, "cad.verification.json")
        self.assertTrue(theirs["ok"], theirs["problems"])
        for key in ("groups", "parts", "ports", "envelopes"):
            self.assertEqual(theirs[key], mine[key], key)

    def test_rebuild_is_deterministic(self):
        project = fixture_document()["current"]
        out, result = build(project)
        for name in ("scene.json", "geometry.recipe.json"):
            with open(os.path.join(self.out, name), "rb") as a, open(os.path.join(out, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)
        self.assertSameStepStructure(out)
        self.assertEqual(result["cad"]["representations"], self.result["cad"]["representations"])
        self.assertEqual(result["display"]["representations"], self.result["display"]["representations"])

    def test_rebuild_from_saved_recipe_reproduces_scene_and_step(self):
        out = new_dir()
        result = rebuild_from_recipe(os.path.join(self.out, "geometry.recipe.json"), out)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("complete", "complete"))
        self.assertEqual(load(out, "scene.json")["components"], self.scene["components"])
        self.assertEqual(result["cad"]["representations"], self.result["cad"]["representations"])
        self.assertSameStepStructure(out)


class ParameterEditTest(unittest.TestCase):
    """Supported pump edits through the real core command layer."""

    @classmethod
    def setUpClass(cls):
        cls.base = fixture_build()
        cls.base_scene = load(cls.base[2], "scene.json")

    def pump_ports(self, scene):
        return {p["name"]: p for p in scene["ports"] if p["entity_id"] == "ent-pump"}

    def test_body_height_edit_moves_nozzles_and_routes_become_pending(self):
        document, warnings = command(fixture_document(), "set_parameter",
                                     {"entity_id": "ent-pump", "name": "body_height", "quantity": pump_quantity(0.5)})
        project = document["current"]
        routes = project["physical"]["routes"]
        self.assertEqual({cid for cid, r in routes.items() if r["status"] != "current"},
                         {"conn-suction-2", "conn-discharge-1"})
        self.assertTrue(any(w["code"] == "route_invalidated" for w in warnings))
        out, result = build(project)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("pending", "pending"))
        self.assertEqual(result["display"]["source_revision"], 1)
        self.assertEqual(result["cad"]["input_digest"], canonical_digest(project))
        codes = sorted((d["code"], tuple(d["entity_ids"])) for d in result["display"]["diagnostics"])
        self.assertEqual(codes, [("route_pending", ("conn-discharge-1",)), ("route_pending", ("conn-suction-2",))])
        scene = load(out, "scene.json")
        ports = self.pump_ports(scene)
        self.assertEqual(ports["inlet"]["position_m"], [1.175, 0.0, 0.25])
        self.assertEqual(ports["outlet"]["position_m"], [1.5, 0.0, 0.6])
        before = components_by_id(self.base_scene)
        after = components_by_id(scene)
        self.assertEqual(sorted(k for k in after if k.startswith("ent-pump#")),
                         sorted(k for k in before if k.startswith("ent-pump#")))
        self.assertAlmostEqual(after["ent-pump#nozzle/outlet"]["bounds_m"]["max"][2], 0.6, places=6)
        self.assertAlmostEqual(after["ent-pump#body"]["bounds_m"]["max"][2], 0.5, places=4)
        self.assertNotEqual(after["ent-pump#body"]["positions"], before["ent-pump#body"]["positions"])
        for cid in ("conn-suction-2", "conn-discharge-1"):
            ghosts = [c for c in scene["components"] if c["semantic_id"] == cid]
            self.assertEqual([(c["component_key"], c["display_only"], c["status"]) for c in ghosts],
                             [("pending/path", True, "pending")])
            self.assertEqual(result["cad"]["representations"][cid], [])
            self.assertNotIn(f"{cid}#pipe", after)
        self.assertIn("conn-suction-1#pipe", after)
        verification = load(out, "cad.verification.json")
        self.assertTrue(verification["ok"], verification["problems"])
        groups = {g["name"] for g in read_step(os.path.join(out, "model.step"))["groups"]}
        self.assertNotIn("conn-discharge-1", groups)

    def test_body_length_edit_moves_only_the_suction_nozzle(self):
        document, _ = command(fixture_document(), "set_parameter",
                              {"entity_id": "ent-pump", "name": "body_length", "quantity": pump_quantity(0.55)})
        routes = document["current"]["physical"]["routes"]
        self.assertEqual(routes["conn-suction-2"]["status"], "needs_reroute")
        self.assertEqual(routes["conn-discharge-1"]["status"], "current")
        out, result = build(document["current"])
        self.assertEqual(result["cad"]["status"], "pending")
        ports = self.pump_ports(load(out, "scene.json"))
        self.assertEqual(ports["inlet"]["position_m"], [1.125, 0.0, 0.2])
        self.assertEqual(ports["outlet"]["position_m"], [1.5, 0.0, 0.5])
        envelopes = {e["name"]: e for e in load(out, "cad.verification.json")["envelopes"] if e["group"] == "ent-pump"}
        self.assertEqual(envelopes["casing reaches the suction face and body top"]["actual_mm"]["xmin"], -275.0)

    def test_compatible_motor_length_edit_builds_complete_revised_outputs(self):
        document, _ = command(fixture_document(), "set_parameter",
                              {"entity_id": "ent-pump", "name": "motor_length", "quantity": pump_quantity(0.42)})
        project = document["current"]
        self.assertTrue(all(r["status"] == "current" for r in project["physical"]["routes"].values()))
        out, result = build(project)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("complete", "complete"))
        self.assertEqual((result["display"]["source_revision"], result["cad"]["source_revision"]), (1, 1))
        scene = load(out, "scene.json")
        motor = components_by_id(scene)["ent-pump#motor"]["bounds_m"]
        self.assertAlmostEqual(motor["max"][0] - motor["min"][0], 0.42, delta=1e-5)
        self.assertEqual([c["id"] for c in scene["components"]], [c["id"] for c in self.base_scene["components"]])
        self.assertEqual(self.pump_ports(scene), self.pump_ports(self.base_scene))
        verification = load(out, "cad.verification.json")
        self.assertTrue(verification["ok"])
        motor_check = [e for e in verification["envelopes"] if e["group"] == "ent-pump" and "motor" in e["name"]][0]
        self.assertEqual(motor_check["actual_mm"]["xmax"], 225.0 + 420.0)


class PlacementTest(unittest.TestCase):
    """Rotated and translated placements reach the scene, ports and STEP consistently."""

    def moved(self, position, rotation):
        placement = {"position_m": position, "rotation_deg": rotation, "status": "placed", "manual": True,
                     "assumption_id": "asm-skid-layout"}
        document, _ = command(fixture_document(), "move_equipment", {"entity_id": "ent-pump", "placement": placement})
        return document["current"]

    def check(self, project, rotation_z):
        out, result = build(project)
        self.assertEqual(result["cad"]["status"], "pending")
        scene = load(out, "scene.json")
        verification = load(out, "cad.verification.json")
        self.assertTrue(verification["ok"], verification["problems"])
        placement = project["physical"]["placements"]["ent-pump"]
        group = [g for g in read_step(os.path.join(out, "model.step"))["groups"] if g["name"] == "ent-pump"][0]
        expected = kernel.placement_matrix(placement["position_m"], placement["rotation_deg"], 1000.0)
        for i in range(3):
            for j in range(4):
                self.assertAlmostEqual(group["location"][i][j], expected[i][j], places=9)
        c, s = math.cos(math.radians(rotation_z)), math.sin(math.radians(rotation_z))
        ports = {p["name"]: p for p in scene["ports"] if p["entity_id"] == "ent-pump"}
        for name, local in (("inlet", (-1.0, 0.0, 0.0)), ("outlet", (0.0, 0.0, 1.0))):
            want = (c * local[0] - s * local[1], s * local[0] + c * local[1], local[2])
            for got, w in zip(ports[name]["direction"], want):
                self.assertAlmostEqual(got, w, places=12)
            self.assertEqual(tuple(ports[name]["position_m"]), world_port_position(project, f"port-pump-{name}"))
        flange_ports = [r for r in verification["ports"] if r["port_id"].startswith("port-pump")]
        self.assertTrue(len(flange_ports) == 2 and all(r["ok"] for r in flange_ports))
        return scene

    def test_quarter_turn_and_translation(self):
        base = components_by_id(load(fixture_build()[2], "scene.json"))
        scene = self.check(self.moved([1.6, 0.3, 0.05], [0.0, 0.0, 90.0]), 90.0)
        moved = components_by_id(scene)
        old, new = base["ent-pump#motor"]["bounds_m"], moved["ent-pump#motor"]["bounds_m"]
        # Local (x, y) -> world (-y, x) after the turn, then translation by (1.6, 0.3, 0.05) instead of (1.5, 0, 0).
        self.assertAlmostEqual(new["min"][1], old["min"][0] - 1.5 + 0.3, places=4)
        self.assertAlmostEqual(new["max"][1], old["max"][0] - 1.5 + 0.3, places=4)
        self.assertAlmostEqual(new["min"][0], -old["max"][1] + 1.6, places=4)
        self.assertAlmostEqual(new["max"][2], old["max"][2] + 0.05, places=4)

    def test_arbitrary_rotation(self):
        self.check(self.moved([1.6, 0.3, 0.0], [0.0, 0.0, 30.0]), 30.0)


class UnresolvedAndFailureTest(unittest.TestCase):

    def test_unresolved_placement_is_omitted_with_diagnostics(self):
        project = fixture_document()["current"]
        project["physical"]["placements"]["ent-discharge-valve"] = {
            "position_m": None, "rotation_deg": None, "status": "unresolved", "manual": False,
            "assumption_id": None, "reason": "test: placement withdrawn"}
        for cid in ("conn-discharge-2", "conn-discharge-3"):
            project["physical"]["routes"][cid].update(status="unresolved", reason="test: valve unplaced")
        self.assertFalse([d for d in validate_project(project) if d["severity"] == "error"])
        out, result = build(project)
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("pending", "pending"))
        self.assertEqual(result["display"]["representations"]["ent-discharge-valve"], [])
        self.assertEqual(result["cad"]["representations"]["port-discharge-valve-inlet"], [])
        codes = {(d["code"], d["entity_ids"][0]) for d in result["display"]["diagnostics"]}
        self.assertIn(("geometry_omitted", "ent-discharge-valve"), codes)
        self.assertIn(("route_pending", "conn-discharge-3"), codes)
        scene = load(out, "scene.json")
        self.assertNotIn("port-discharge-valve-inlet", {p["id"] for p in scene["ports"]})
        self.assertTrue(load(out, "cad.verification.json")["ok"])

    def test_malformed_inputs(self):
        out, result = build("not a project")
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("failed", "failed"))
        self.assertEqual(result["display"]["diagnostics"][0]["code"], "invalid_input")
        self.assertFalse(os.path.exists(os.path.join(out, "scene.json")))
        self.assertEqual(load(out, "cad.manifest.json")["status"], "failed")

        broken = fixture_document()["current"]
        broken["physical"]["routes"]["conn-suction-1"]["points_m"][0] = [0.4, 0.0, 0.2]
        out, result = build(broken)
        self.assertEqual(result["display"]["status"], "failed")
        self.assertIn("route_endpoint_mismatch", {d["code"] for d in result["display"]["diagnostics"]})
        self.assertEqual(sorted(os.listdir(out)), ["cad.manifest.json", "display.manifest.json"])

        nan = fixture_document()["current"]
        nan["model"]["entities"]["ent-tank"]["parameters"]["height"]["value"] = float("nan")
        _, result = build(nan)
        self.assertEqual(result["display"]["status"], "failed")

        with self.assertRaises(ValueError):
            build_geometry(fixture_document()["current"], os.path.join(new_dir(), "missing"))
        with self.assertRaises(TypeError):
            build_geometry(fixture_document()["current"], new_dir(), emit="not callable")

    def test_out_of_range_recipe_parameter_fails_explicitly(self):
        document, _ = command(fixture_document(), "set_parameter",
                              {"entity_id": "ent-pump", "name": "body_length", "quantity": pump_quantity(0.25)})
        out, result = build(document["current"])
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("failed", "failed"))
        errors = [d for d in result["display"]["diagnostics"] if d["severity"] == "error"]
        self.assertEqual([(d["code"], d["entity_ids"]) for d in errors], [("geometry_recipe_failed", ["ent-pump"])])
        self.assertIn("supported range", errors[0]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "scene.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "model.step")))

    def test_cancellation_between_units_and_before_cad(self):
        events = []

        def after_three_previews():
            return sum(e["kind"] == "preview" for e in events) >= 3

        out = new_dir()
        with self.assertRaises(InterruptedError):
            build_geometry(fixture_document()["current"], out, emit=events.append, cancelled=after_three_previews)
        self.assertEqual(sorted(os.listdir(os.path.join(out, "preview"))),
                         ["display-0001.json", "display-0002.json", "display-0003.json"])
        for name in ("scene.json", "model.step", "display.manifest.json", "cad.manifest.json"):
            self.assertFalse(os.path.exists(os.path.join(out, name)), name)
        self.assertEqual(load(out, "preview/display-0003.json")["preview"]["unit_id"], "ent-pump")

        events = []

        def once_cad_starts():
            return any(e["artifact"] == "cad" for e in events)

        out = new_dir()
        with self.assertRaises(InterruptedError):
            build_geometry(fixture_document()["current"], out, emit=events.append, cancelled=once_cad_starts)
        self.assertTrue(os.path.exists(os.path.join(out, "scene.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "cad.manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "cad.verification.json")))

    def test_reimport_mismatch_or_export_failure_fails_cad_only(self):
        import skid.geometry.build as geometry_build
        from unittest import mock

        real_read = geometry_build.read_step

        def lossy_read(path):
            data = real_read(path)
            pump = [g for g in data["groups"] if g["name"] == "ent-pump"][0]
            pump["parts"] = [p for p in pump["parts"] if p["name"] != "ent-pump#motor"]
            return data

        with mock.patch.object(geometry_build, "read_step", lossy_read):
            out, result = build(fixture_document()["current"])
        self.assertEqual((result["display"]["status"], result["cad"]["status"]), ("complete", "failed"))
        self.assertEqual(result["cad"]["representations"], {})
        self.assertIn("ent-pump#motor", result["cad"]["diagnostics"][-1]["message"])
        self.assertFalse(load(out, "cad.verification.json")["ok"])
        self.assertEqual(load(out, "cad.manifest.json")["status"], "failed")

        def broken_write(*args, **kwargs):
            raise geometry_build.StepError("injected export failure")

        events = []
        with mock.patch.object(geometry_build, "write_step", broken_write):
            out, result = build(fixture_document()["current"], emit=events.append)
        self.assertEqual(result["cad"]["status"], "failed")
        self.assertIn("injected export failure", result["cad"]["diagnostics"][-1]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "model.step")))
        self.assertNotIn("cad", [e["artifact"] for e in events if e["kind"] == "artifact"])

    def test_previews_are_never_overwritten(self):
        out = new_dir()
        os.makedirs(os.path.join(out, "preview"))
        with open(os.path.join(out, "preview", "display-0001.json"), "w") as handle:
            handle.write("{}")
        with self.assertRaises(FileExistsError):
            build_geometry(fixture_document()["current"], out)
        with open(os.path.join(out, "preview", "display-0001.json")) as handle:
            self.assertEqual(handle.read(), "{}")


class KernelAndPartsTest(unittest.TestCase):

    def test_rotation_matches_template_convention(self):
        from skid.templates import _rotate

        for rotation in ([0, 0, 90], [90, 0, 0], [0, -90, 0], [10, 20, 30], [-45, 135, 270]):
            matrix = kernel.rotation_matrix(rotation)
            for vector in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.3, -0.2, 0.9)):
                want = _rotate(rotation, vector)
                got = [sum(matrix[i][j] * vector[j] for j in range(3)) for i in range(3)]
                for g, w in zip(got, want):
                    self.assertAlmostEqual(g, w, places=12)

    def test_part_calls_are_validated(self):
        bad = [
            {"part": "teleporter"},
            {"part": "cylinder", "start": [0, 0, 0], "end": [0, 0, 1]},
            {"part": "cylinder", "start": [0, 0, 0], "end": [0, 0, 1], "radius": 1, "colour": "red"},
            {"part": "cylinder", "start": [0, 0, 0], "end": [0, 0, 0], "radius": 1},
            {"part": "box", "center": [0, 0, 0], "size": [1, float("nan"), 1]},
            {"part": "revolve", "origin": [0, 0, 0], "axis": [1, 0, 0], "profile": [[1, 0], [2, 0], [-1, 1]]},
            {"part": "flange", "face_center": [0, 0, 0], "normal": [1, 0, 0], "outer_radius": 50, "thickness": 10,
             "bore_radius": 20, "bolt_count": 4, "bolt_circle_radius": 45, "hole_radius": 9},
            {"part": "polar_array", "origin": [0, 0, 0], "axis": [0, 0, 1], "count": 3, "angles_deg": [0],
             "item": {"part": "sphere", "center": [1, 0, 0], "radius": 0.5}},
            "not a call",
        ]
        for call in bad:
            with self.assertRaises((parts.PartError, kernel.KernelError), msg=repr(call)):
                parts.build(call)

    def test_fuse_and_arrays(self):
        from skid.geometry.recipes import compile_component

        base = {"key": "demo", "material": "pump_paint", "display_only": False, "assumptions": ["rcp-appearance"]}
        boxes = [{"part": "box", "center": [0, 0, 0], "size": [10, 10, 10]},
                 {"part": "box", "center": [5, 0, 0], "size": [10, 10, 10]}]
        fused, count = compile_component(dict(base, parts=boxes, combine="fuse"))
        self.assertEqual(count, 1)
        self.assertTrue(kernel.is_valid(fused))
        self.assertAlmostEqual(kernel.volume(fused), 1500.0, places=6)
        grouped, count = compile_component(dict(base, parts=boxes))
        self.assertEqual(count, 2)
        self.assertAlmostEqual(kernel.volume(grouped), 2000.0, places=6)
        row = parts.build({"part": "linear_array", "count": 4, "step": [0, 20, 0],
                           "item": {"part": "cylinder", "start": [0, 0, 0], "end": [0, 0, 5], "radius": 2}})
        self.assertEqual(len(row), 4)
        self.assertAlmostEqual(kernel.bounds(kernel.compound(row))[1][1], 62.0, places=6)

    def test_pipe_bends_shrink_only_where_runs_are_short(self):
        _, radii = kernel.pipe([[0, 0, 0], [0, 0, 200], [500, 0, 200]], 30, 75)
        self.assertEqual(radii, [75.0])
        _, radii = kernel.pipe([[0, 0, 0], [0, 0, 120], [500, 0, 120]], 30, 75)
        self.assertAlmostEqual(radii[0], 0.49 * 120, places=6)
        with self.assertRaises(kernel.KernelError):
            kernel.pipe([[0, 0, 0], [0, 0, 100], [0, 0, 0]], 30, 75)
        with self.assertRaises(kernel.KernelError):
            kernel.pipe([[0, 0, 0], [0, 0, 40], [500, 0, 40]], 30, 75)


if __name__ == "__main__":
    unittest.main()
