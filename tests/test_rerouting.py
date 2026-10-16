"""Guided rerouting helper (``skid.interactive.rerouting``): frames, rules, candidates and actual CAD pipes.

    .venv-cad/bin/python -m unittest tests.test_rerouting -v

Pure tests run anywhere. ``CadAgreementTests`` and ``CadBuildTests`` need
CadQuery/OCP (``.venv-cad``): they compare the helper's endpoint frames and
bend-room rule with ``skid.geometry`` and sweep the selected routes into
actual OCCT solids, including a full ``build_geometry`` of rerouted projects.
They do not establish clearance, support or fabrication adequacy.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from skid.clearance import route_report
from skid.core import apply_command, validate_project
from skid.interactive import rerouting
from skid.interactive.rerouting import RerouteError, path_report, plan_reroute, port_frame
from skid.templates import world_port_position

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "pump_skid.json"

try:
    import OCP  # noqa: F401
    HAVE_CAD = True
except ImportError:
    HAVE_CAD = False


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def move(document, entity_id, position, rotation=(0, 0, 0)):
    revision = document["current"]["revision"]
    placement = {"position_m": list(position), "rotation_deg": list(rotation), "status": "placed", "manual": True,
                 "assumption_id": "asm-skid-layout"}
    command = {"id": f"cmd-test-move-{revision}", "expected_revision": revision, "type": "move_equipment",
               "payload": {"entity_id": entity_id, "placement": placement}}
    return apply_command(document, command)[0]


def apply_plan(document, plan, token="t"):
    command = rerouting.reroute_command(document["current"], plan, f"cmd-test-reroute-{token}",
                                        f"asm-test-reroute-{token}", "test")
    return apply_command(document, command)[0]


def reroute_all(document):
    """Reroute every non-current route; return the new document and the plans."""
    plans = []
    for cid in sorted(document["current"]["physical"]["routes"]):
        if document["current"]["physical"]["routes"][cid]["status"] != "current":
            plan = plan_reroute(document["current"], cid)
            plans.append(plan)
            document = apply_plan(document, plan, cid)
    return document, plans


def frame(position, direction):
    return {"position_m": list(position), "direction": list(direction)}


def ends_match(test, plan, project):
    connection = project["model"]["connections"][plan["connection_id"]]
    test.assertEqual(plan["points_m"][0], list(world_port_position(project, connection["from_port_id"])))
    test.assertEqual(plan["points_m"][-1], list(world_port_position(project, connection["to_port_id"])))


# Pump inlet faces -x at (x - 0.325, y, 0.2) for a pump placed at (x, y, 0); the suction valve outlet faces +x at
# (0.88, 0, 0.2). These scenarios move the pump only.
COLLINEAR_SHORT = (0.88 + 0.07 + 0.325, 0.0, 0.0)  # 70 mm facing gap on one axis
OFFSET = (1.6, 0.1, 0.0)  # ordinary moves: 100 mm sideways
TIGHT_SIDEWAYS = (1.5, 0.04, 0.0)  # 40 mm sideways: no room for a bend on a direct dogleg
# Pump inlet behind the valve outlet, ports facing away. At (1.0, 0, 0) the HV-101 outlet port (0.88, 0, 0.2) lies
# inside the pump body (x 0.775-1.225, z 0-0.4), so every route from it enters P-101: OVERLAPPING must be refused.
# Offset 0.6 m sideways the pump clears the valve; the route lifts over HV-101 and returns behind the inlet.
OVERLAPPING = (1.0, 0.0, 0.0)
BEHIND = (1.0, 0.6, 0.0)
TIGHT_VERTICAL = (1.5, 0.0, 0.05)  # 50 mm vertical offset of facing ports: no orthogonal path fits


class FrameTests(unittest.TestCase):
    def test_fixture_frames_are_the_authoritative_positions_and_template_axes(self):
        project = fixture()["current"]
        expected = {
            "port-tank-outlet": [1.0, 0.0, 0.0], "port-pump-inlet": [-1.0, 0.0, 0.0],
            "port-pump-outlet": [0.0, 0.0, 1.0], "port-suction-valve-inlet": [-1.0, 0.0, 0.0],
            "port-check-valve-outlet": [1.0, 0.0, 0.0],
            # HV-102 and OUT-101 are turned -90 degrees about z: local -x becomes +y, local +x becomes -y.
            "port-discharge-valve-inlet": [0.0, 1.0, 0.0], "port-discharge-valve-outlet": [0.0, -1.0, 0.0],
            "port-boundary-inlet": [0.0, 1.0, 0.0],
        }
        for port_id, direction in expected.items():
            result = port_frame(project, port_id)
            self.assertEqual(result["direction"], direction, port_id)
            self.assertEqual(result["position_m"], list(world_port_position(project, port_id)), port_id)

    def test_frames_follow_the_current_placement_and_quarter_turns(self):
        document = move(fixture(), "ent-pump", (2.0, -0.5, 0.3), (0, 0, 90))
        project = document["current"]
        inlet = port_frame(project, "port-pump-inlet")
        self.assertEqual(inlet["direction"], [0.0, -1.0, 0.0])  # -x turned +90 about z
        self.assertEqual(inlet["position_m"], list(world_port_position(project, "port-pump-inlet")))
        self.assertAlmostEqual(inlet["position_m"][1], -0.5 - 0.325, places=9)
        document = move(fixture(), "ent-pump", (1.5, 0.0, 0.0), (90, 0, 0))
        self.assertEqual(port_frame(document["current"], "port-pump-outlet")["direction"], [0.0, -1.0, 0.0])

    def test_unsupported_rotation_and_unplaced_endpoints_are_refused(self):
        document = move(fixture(), "ent-pump", (1.5, 0.0, 0.0), (0, 0, 45))
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(document["current"], "conn-suction-2")
        self.assertEqual(caught.exception.code, "reroute_unsupported_rotation")
        project = copy.deepcopy(fixture()["current"])
        project["physical"]["placements"]["ent-pump"] = {
            "position_m": None, "rotation_deg": None, "status": "unresolved", "manual": False,
            "assumption_id": "asm-skid-layout", "reason": "test: not placed"}
        project["physical"]["routes"]["conn-suction-2"].update(status="unresolved", reason="test")
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-suction-2")
        self.assertEqual(caught.exception.code, "reroute_unplaced_endpoint")
        self.assertIn("ent-pump", caught.exception.ids)

    def test_non_axis_port_directions_are_refused(self):
        project = copy.deepcopy(fixture()["current"])
        original = rerouting._local_direction
        try:
            rerouting._local_direction = lambda entity, name: [math.sqrt(0.5), math.sqrt(0.5), 0.0]
            with self.assertRaises(RerouteError) as caught:
                port_frame(project, "port-pump-inlet")
        finally:
            rerouting._local_direction = original
        self.assertEqual(caught.exception.code, "reroute_unsupported_direction")


class RuleTests(unittest.TestCase):
    """``path_report`` encodes the pipe compiler's rules: tangents, reversal, bend room, spacing."""

    START = frame((0, 0, 0), (1, 0, 0))
    OD, DN = 0.0603, 50

    def report(self, points, end):
        return path_report(points, self.START, end, self.OD, self.DN)

    def test_good_right_angle_has_a_full_bend(self):
        result = self.report([[0, 0, 0], [0.5, 0, 0], [0.5, 0.5, 0]], frame((0.5, 0.5, 0), (0, -1, 0)))
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["bend_radii_mm"], [75.0])

    def test_endpoint_tangents_must_follow_the_port_axes(self):
        wrong_start = self.report([[0, 0, 0], [0, 0.5, 0], [0.5, 0.5, 0]], frame((0.5, 0.5, 0), (1, 0, 0)))
        self.assertTrue(any("first segment" in p for p in wrong_start["problems"]))
        wrong_end = self.report([[0, 0, 0], [0.5, 0, 0], [0.5, 0.5, 0]], frame((0.5, 0.5, 0), (1, 0, 0)))
        self.assertTrue(any("last segment" in p for p in wrong_end["problems"]))

    def test_reversal_short_segment_and_non_axis_segment(self):
        end = frame((0.3, 0, 0), (1, 0, 0))
        reversing = self.report([[0, 0, 0], [0.5, 0, 0], [0.3, 0, 0]], end)
        self.assertTrue(any("reverses" in p for p in reversing["problems"]), reversing)
        short = self.report([[0, 0, 0], [0.0005, 0, 0]], frame((0.0005, 0, 0), (-1, 0, 0)))
        self.assertTrue(any("shorter than" in p for p in short["problems"]), short)
        diagonal = self.report([[0, 0, 0], [0.5, 0.5, 0]], frame((0.5, 0.5, 0), (-1, 0, 0)))
        self.assertTrue(any("not parallel" in p for p in diagonal["problems"]), diagonal)

    def test_bend_room_threshold_matches_the_compiler_formula(self):
        # Corner radius = min(75, 0.49 * shorter run); it must exceed 1.01 * 30.15 mm.
        threshold = 0.0603 * 500 * 1.01 / 0.49 / 1000  # metres of shorter run
        for run, ok in ((threshold - 0.0005, False), (threshold + 0.0005, True), (0.1, True)):
            result = self.report([[0, 0, 0], [run, 0, 0], [run, 0.5, 0]], frame((run, 0.5, 0), (0, -1, 0)))
            self.assertEqual(not result["problems"], ok, (run, result["problems"]))
            if ok and run == 0.1:
                self.assertEqual(result["bend_radii_mm"], [49.0])
                self.assertEqual(result["reduced_bends"], 1)

    def test_non_adjacent_runs_must_keep_one_diameter_apart(self):
        points = [[0, 0, 0], [0.4, 0, 0], [0.4, 0.2, 0], [-0.2, 0.2, 0], [-0.2, 0.05, 0], [0.2, 0.05, 0]]
        result = self.report(points, frame((0.2, 0.05, 0), (-1, 0, 0)))
        self.assertTrue(any("closer than one pipe diameter" in p for p in result["problems"]), result)


class PlanTests(unittest.TestCase):
    def plan(self, position, cid):
        document = move(fixture(), "ent-pump", position)
        project = document["current"]
        self.assertNotEqual(project["physical"]["routes"][cid]["status"], "current")
        plan = plan_reroute(project, cid)
        ends_match(self, plan, project)
        rerouted = apply_plan(document, plan)
        route = rerouted["current"]["physical"]["routes"][cid]
        self.assertEqual((route["status"], route["mode"]), ("current", "guided"))
        self.assertEqual(route["points_m"], plan["points_m"])
        self.assertIn(route["assumption_id"], rerouted["current"]["model"]["assumptions"])
        return project, plan, rerouted

    def test_collinear_short_gap_is_one_straight_run(self):
        _, plan, _ = self.plan(COLLINEAR_SHORT, "conn-suction-2")
        self.assertEqual(len(plan["points_m"]), 2)
        self.assertAlmostEqual(plan["length_m"], 0.07, places=9)

    def test_ordinary_offsets_use_few_bends(self):
        _, plan, _ = self.plan(OFFSET, "conn-suction-2")
        self.assertEqual(plan["bends"], 2)
        _, plan, _ = self.plan(OFFSET, "conn-discharge-1")  # pump outlet +z to check-valve inlet -x
        self.assertEqual(plan["points_m"][1], [1.6, 0.1, 0.7])  # the riser stops level with the valve inlet
        self.assertEqual(plan["bends"], 2)

    def test_detours_make_bend_room(self):
        _, plan, _ = self.plan(TIGHT_SIDEWAYS, "conn-discharge-1")
        self.assertEqual(plan["bends"], 4)
        self.assertEqual(plan["bend_radii_mm"], [75.0] * 4)
        project, plan, _ = self.plan(BEHIND, "conn-suction-2")  # ports face away: over HV-101 and back
        self.assertEqual(plan["bends"], 5)
        self.assertEqual(plan["bend_radii_mm"], [75.0] * 5)
        self.assertTrue(route_report(project, "conn-suction-2", plan["points_m"])["clear"])

    def test_equipment_bodies_block_candidates(self):
        project = move(fixture(), "ent-pump", OVERLAPPING)["current"]
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-suction-2")
        self.assertEqual(caught.exception.code, "reroute_impossible")
        self.assertIn("blocked by P-101", caught.exception.message)

    def test_impossible_geometry_is_refused_explicitly(self):
        project = move(fixture(), "ent-pump", TIGHT_VERTICAL)["current"]
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-suction-2")
        self.assertEqual(caught.exception.code, "reroute_impossible")
        self.assertIn("candidates", caught.exception.message)

    def test_current_unknown_and_unsupported_size(self):
        project = fixture()["current"]
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-suction-1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("route_current", 409))
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-nope")
        self.assertEqual(caught.exception.code, "reroute_unknown_connection")
        project = copy.deepcopy(move(fixture(), "ent-pump", OFFSET)["current"])
        project["model"]["connections"]["conn-suction-2"]["nominal_diameter_mm"] = 80
        with self.assertRaises(RerouteError) as caught:
            plan_reroute(project, "conn-suction-2")
        self.assertEqual(caught.exception.code, "reroute_unsupported_size")

    def test_deterministic_and_bounded(self):
        project = move(fixture(), "ent-pump", BEHIND)["current"]
        first, second = plan_reroute(project, "conn-suction-2"), plan_reroute(copy.deepcopy(project), "conn-suction-2")
        self.assertEqual(first, second)
        self.assertLess(first["candidates_checked"], 2000)

    def test_unrelated_and_manual_routes_and_pid_are_preserved(self):
        document = move(fixture(), "ent-pump", OFFSET)
        before = document["current"]
        after, plans = reroute_all(document)
        self.assertEqual({p["connection_id"] for p in plans}, {"conn-suction-2", "conn-discharge-1"})
        self.assertEqual(after["current"]["pid"], before["pid"])
        self.assertEqual(after["current"]["model"]["entities"], before["model"]["entities"])
        for cid in ("conn-suction-1", "conn-discharge-2", "conn-discharge-3"):
            self.assertEqual(after["current"]["physical"]["routes"][cid], before["physical"]["routes"][cid])
        self.assertEqual([d for d in validate_project(after["current"]) if d["severity"] != "info"], [])


@unittest.skipUnless(HAVE_CAD, "CadQuery/OCP unavailable; run with .venv-cad/bin/python")
class CadAgreementTests(unittest.TestCase):
    """The helper's frames and bend rule agree with the actual geometry compiler."""

    def test_frames_equal_the_cad_port_frames(self):
        from skid.geometry.build import plan_units
        from tests.test_cad_equipment import composed_document
        projects = [fixture()["current"], move(fixture(), "ent-pump", (2.0, -0.5, 0.3), (0, 0, 90))["current"],
                    move(fixture(), "ent-pump", (1.5, 0.0, 0.0), (90, 0, 0))["current"],
                    composed_document()["current"]]
        checked = 0
        for project in projects:
            units, _, failures = plan_units(copy.deepcopy(project))
            self.assertEqual(failures, [])
            for unit in units:
                for cad in unit["ports"]:
                    mine = port_frame(project, cad["id"])
                    self.assertEqual(mine["position_m"], cad["position_m"], cad["id"])
                    self.assertTrue(all(abs(a - b) < 1e-12 for a, b in zip(mine["direction"], cad["direction"])),
                                    (cad["id"], mine["direction"], cad["direction"]))
                    checked += 1
        self.assertGreater(checked, 30)

    def test_constants_mirror_the_compiler(self):
        from skid.geometry import routes
        from skid.geometry.equipment import FLANGE_DN50
        self.assertEqual(rerouting.BEND_RADIUS_FACTOR, routes.BEND_RADIUS_FACTOR)
        self.assertEqual(rerouting.JOINT_DEPTH_MM, FLANGE_DN50["thickness"] + 30.0)

    def test_bend_room_rule_agrees_with_the_kernel(self):
        from skid.geometry import kernel
        start = frame((0, 0, 0), (1, 0, 0))
        threshold = 0.0603 * 500 * 1.01 / 0.49 / 1000
        cases = [
            [[0, 0, 0], [threshold - 0.0005, 0, 0], [threshold - 0.0005, 0.5, 0]],
            [[0, 0, 0], [threshold + 0.0005, 0, 0], [threshold + 0.0005, 0.5, 0]],
            [[0, 0, 0], [0.1, 0, 0], [0.1, 0.3, 0], [0.4, 0.3, 0]],
            [[0, 0, 0], [0.5, 0, 0], [0.3, 0, 0]],
        ]
        for points in cases:
            end_dir = [b - a for a, b in zip(points[-2], points[-1])]
            norm = math.sqrt(sum(c * c for c in end_dir))
            end = frame(points[-1], [-c / norm for c in end_dir])
            ours = path_report(points, start, end, 0.0603, 50)
            try:
                solid, radii = kernel.pipe([[c * 1000 for c in p] for p in points], 30.15, 75.0)
                built = kernel.is_valid(solid)
            except kernel.KernelError:
                built, radii = False, None
            self.assertEqual(not ours["problems"], built, (points, ours["problems"]))
            if built:
                self.assertEqual([round(r, 6) for r in radii if r is not None], ours["bend_radii_mm"])


@unittest.skipUnless(HAVE_CAD, "CadQuery/OCP unavailable; run with .venv-cad/bin/python")
class CadBuildTests(unittest.TestCase):
    """Selected routes become valid OCCT solids through the real route recipe and a full geometry build."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="test-rerouting-cad-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def sweep(self, project, plan):
        from skid.geometry import kernel
        from skid.geometry.recipes import compile_component
        from skid.geometry.routes import route_recipe
        connection = project["model"]["connections"][plan["connection_id"]]
        route = {"points_m": plan["points_m"], "mode": "guided", "status": "current", "assumption_id": "asm-x"}
        recipe = route_recipe(connection, route, {"from": plan["from"], "to": plan["to"]})
        for component in recipe["components"]:
            shape, count = compile_component(component, component["key"])
            self.assertTrue(kernel.is_valid(shape), component["key"])
            self.assertGreaterEqual(count, 1)
        _, radii = kernel.pipe(recipe["components"][0]["parts"][0]["points"], connection["outside_diameter_m"] * 500,
                               75.0)
        self.assertEqual([round(r, 6) for r in radii if r is not None], plan["bend_radii_mm"])

    def test_each_scenario_route_sweeps_to_a_valid_solid(self):
        scenarios = [(COLLINEAR_SHORT, "conn-suction-2"), (OFFSET, "conn-suction-2"), (OFFSET, "conn-discharge-1"),
                     (TIGHT_SIDEWAYS, "conn-discharge-1"), (BEHIND, "conn-suction-2"),
                     ((2.0, -0.5, 0.3), "conn-suction-2"), ((2.0, -0.5, 0.3), "conn-discharge-1")]
        for position, cid in scenarios:
            with self.subTest(position=position, connection=cid):
                project = move(fixture(), "ent-pump", position)["current"]
                self.sweep(project, plan_reroute(project, cid))

    def test_full_geometry_build_of_rerouted_projects_is_complete(self):
        from skid.geometry import build_geometry
        from tests.test_cad_equipment import composed_document
        builtin, plans = reroute_all(move(fixture(), "ent-pump", (2.0, -0.5, 0.3), (0, 0, 90)))
        self.assertEqual(len(plans), 2)
        drum = move(composed_document(), "ent-drum", (3.0, -1.6, 0.0), (0, 0, -90))
        drum, drum_plans = reroute_all(drum)
        self.assertEqual({p["connection_id"] for p in drum_plans}, {"conn-discharge-3", "conn-drum-out"})
        for name, document in (("builtin", builtin), ("composed", drum)):
            with self.subTest(project=name):
                out = Path(self.out, name)
                out.mkdir()
                manifests = build_geometry(copy.deepcopy(document["current"]), str(out))
                for adapter in ("display", "cad"):
                    self.assertEqual(manifests[adapter]["status"], "complete",
                                     [d for d in manifests[adapter]["diagnostics"]])
                verification = json.loads((out / "cad.verification.json").read_text(encoding="utf-8"))
                self.assertTrue(verification, "verification evidence written")


if __name__ == "__main__":
    unittest.main()
