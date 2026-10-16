"""Straight-section moves, equipment-body clearance and the guided L-105 repair.

    .venv-cad/bin/python -m unittest tests.test_route_edit -v

Pure and core tests run anywhere. ``SolidTests`` need CadQuery/OCP: they
compile the real CAD recipes of every placed item and route and measure
actual OCCT solid intersections, independently of the clearance boxes.
They do not establish support, stress or fabrication adequacy.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from skid import clearance, route_edit
from skid.core import apply_command, load_document, save_document, validate_project
from skid.core.errors import CommandError
from skid.interactive import rerouting

ROOT = Path(__file__).resolve().parents[1]
PARITY = ROOT / "tests" / "fixtures" / "pipe-segments.json"
R47 = ROOT / "tests" / "fixtures" / "pipe-segments-r47.json"
L105 = "conn-discharge-3"
RISER_MOVE = {"connection_id": L105, "segment_index": 1, "offset_m": [0, 0.4, 0], "assumption_id": "asm-test-move"}

try:
    import OCP  # noqa: F401
    HAVE_CAD = True
except ImportError:
    HAVE_CAD = False


def r47():
    return json.loads(R47.read_text(encoding="utf-8"))


def move_command(document, payload=None, assumptions=True, command_id="cmd-test-move"):
    payload = dict(RISER_MOVE if payload is None else payload)
    command = {"id": command_id, "expected_revision": document["current"]["revision"], "type": "move_route_segment",
               "payload": payload}
    if assumptions:
        command["assumptions"] = {payload["assumption_id"]: {"description": "Test section move.", "source": "test"}}
    return command


def codes(diagnostics):
    return [d["code"] for d in diagnostics]


class ParityFixtureTests(unittest.TestCase):
    """The committed browser-parity fixture is exactly what the module computes now."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(PARITY.read_text(encoding="utf-8"))

    def test_every_case_reproduces_exactly(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                args = (case["points_m"], case["start_direction"], case["end_direction"], case["segment_index"],
                        case["offset_m"], case["outside_diameter_m"], case["nominal_diameter_mm"], case["obstacles"])
                if "error" in case["expect"]:
                    with self.assertRaises(route_edit.RouteEditError) as caught:
                        route_edit.move_points(*args)
                    self.assertEqual(caught.exception.code, case["expect"]["error"])
                else:
                    self.assertEqual(route_edit.move_points(*args), case["expect"]["points_m"])
                if "sections" in case:
                    self.assertEqual(route_edit.section_records(case["points_m"], case["nominal_diameter_mm"])[1],
                                     case["sections"])

    def test_cases_cover_the_contract(self):
        names = {case["name"] for case in self.fixture["cases"]}
        errors = {case["expect"].get("error") for case in self.fixture["cases"]}
        self.assertTrue({"current47_l105_riser_clear", "interior_connector", "terminal_start", "terminal_end",
                         "straight_both_stubs", "collinear_and_duplicate_input", "interior_reversed_offset",
                         "adjacent_leg_cancelled_merges"} <= names)
        self.assertTrue({"route_edit_intrusion", "route_edit_bend_room", "route_edit_no_op",
                         "route_edit_invalid_offset", "route_edit_offset_parallel", "route_edit_stub_too_short",
                         "route_edit_port_tangent", "route_edit_self_clearance", "route_edit_unknown_section",
                         "route_edit_offset_too_large", "route_edit_reverses", "route_edit_offset_too_small"} <= errors)
        self.assertEqual(self.fixture["constants"]["terminal_stub_m"], route_edit.TERMINAL_STUB_M)
        self.assertEqual(self.fixture["constants"]["min_offset_m"], route_edit.MIN_OFFSET_M)

    def test_offsets_below_one_millimetre_refuse_and_exactly_one_millimetre_is_admissible(self):
        by_name = {case["name"]: case for case in self.fixture["cases"]}
        for name in ("r48_l105_riser_tiny_positive", "r48_l105_riser_tiny_negative", "r48_l105_riser_just_below_1mm",
                     "interior_stretch_tiny"):
            self.assertEqual(by_name[name]["expect"], {"error": "route_edit_offset_too_small"}, name)
        self.assertEqual(by_name["tiny_parallel_offset"]["expect"], {"error": "route_edit_offset_parallel"})
        self.assertEqual(by_name["tiny_two_axis_offset"]["expect"], {"error": "route_edit_invalid_offset"})
        self.assertEqual(by_name["zero_offset_no_op"]["expect"], {"error": "route_edit_no_op"})
        # Exactly 1 mm passes admissibility: it moves, or then meets a geometric limit.
        self.assertEqual(by_name["r48_l105_riser_exact_1mm"]["expect"], {"error": "route_edit_bend_room"})
        for name, x in (("interior_stretch_exact_1mm", 1.001), ("interior_stretch_exact_minus_1mm", 0.999)):
            self.assertEqual(by_name[name]["expect"]["points_m"], [[0.0, 0.0, 0.0], [x, 0.0, 0.0], [x, 1.0, 0.0],
                                                                    [2.0, 1.0, 0.0]], name)

    def test_moved_ports_and_tangents_stay_fixed(self):
        for case in self.fixture["cases"]:
            if "points_m" not in case["expect"]:
                continue
            with self.subTest(case=case["name"]):
                out = case["expect"]["points_m"]
                self.assertEqual(out[0], [float(c) for c in case["points_m"][0]])
                self.assertEqual(out[-1], [float(c) for c in case["points_m"][-1]])
                first = [b - a for a, b in zip(out[0], out[1])]
                last = [b - a for a, b in zip(out[-2], out[-1])]
                self.assertEqual([abs(c) > 0 and (c > 0) - (c < 0) for c in first],
                                 [d != 0 and int(d) for d in case["start_direction"]])
                self.assertEqual([abs(c) > 0 and (c > 0) - (c < 0) for c in last],
                                 [d != 0 and -int(d) for d in case["end_direction"]])


class SectionTests(unittest.TestCase):
    def test_current47_l105_sections_follow_the_bend_tangency_limits(self):
        project = r47()["current"]
        sections = route_edit.straight_sections(project, L105)
        self.assertEqual([s["axis"] for s in sections], [1, 2, 0, 2])
        self.assertEqual(sections[0]["start_m"], [2.7, -0.53, 0.7])  # terminal: starts at the port
        self.assertEqual(sections[0]["end_m"], [2.7, -1.8, 0.7])  # 75 mm bend at (2.7, -1.875, 0.7)
        self.assertEqual(sections[3]["end_m"], [1.35, -1.875, 1.1])
        self.assertAlmostEqual(sections[3]["length_m"], 0.16 - 0.075, places=12)
        self.assertEqual(sections[2]["move_axes"], [1, 2])

    def test_unrouted_and_unknown_connections_refuse(self):
        project = r47()["current"]
        with self.assertRaises(route_edit.RouteEditError) as caught:
            route_edit.straight_sections(project, "conn-nope")
        self.assertEqual(caught.exception.code, "unknown_reference")
        del project["physical"]["routes"][L105]
        with self.assertRaises(route_edit.RouteEditError) as caught:
            route_edit.straight_sections(project, L105)
        self.assertEqual(caught.exception.code, "route_edit_no_route")


class ClearanceTests(unittest.TestCase):
    def test_current47_only_l105_enters_a_body_and_loading_still_works(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.json")
            Path(path).write_text(R47.read_text(encoding="utf-8"), encoding="utf-8")
            document = load_document(path)  # an existing intrusion never makes a document unloadable
        warnings = [d for d in validate_project(document["current"]) if d["severity"] == "warning"]
        self.assertEqual(codes(warnings), ["route_body_intrusion"])
        self.assertEqual(warnings[0]["entity_ids"], [L105, "ent-column"])
        self.assertIn("C-101 body", warnings[0]["message"])
        for cid, route in document["current"]["physical"]["routes"].items():
            report = clearance.route_report(document["current"], cid, route["points_m"])
            self.assertEqual(report["clear"], cid != L105, cid)

    def test_unrelated_commands_still_apply_and_keep_reporting_the_intrusion(self):
        document = r47()
        command = {"id": "cmd-test-rename", "expected_revision": 47, "type": "rename_entity",
                   "payload": {"entity_id": "ent-pump", "tag": "P-122"}}
        new, warnings = apply_command(document, command)
        self.assertEqual(new["current"]["revision"], 48)
        self.assertIn("route_body_intrusion", codes(warnings))

    def test_a_body_owning_both_ports_still_blocks(self):
        """Root counterexample: one box body owns both ports; mapping a port to it must not exempt the vessel."""
        document = r47()
        project = document["current"]
        geometry = project["model"]["entities"]["ent-column"]["definition"]["geometry"]
        geometry["components"] = [{"key": "body", "material": "vessel_paint", "display_only": False,
                                   "assumptions": ["asm-drum-dimensions"],
                                   "parts": [{"part": "box", "center": [0, 0, 500], "size": [2000, 500, 1000]}]}]
        geometry["ports"]["inlet"].update(position=[-700, 0, 1000], direction=[0, 0, 1], component="body")
        geometry["ports"]["outlet"].update(position=[1000, 0, 500], direction=[1, 0, 0], component="body")
        routes = project["physical"]["routes"]
        routes[L105]["points_m"][-1] = [1.35, -1.875, 1.0]
        routes["conn-column-down"]["points_m"] = [[3.05, -1.875, 0.5], [3.21, -1.875, 0.5], [3.21, -1.875, 0.7],
                                                  [3.21, -3.09, 0.7], [2.7, -3.09, 0.7], [2.7, -3.25, 0.7]]
        self.assertFalse([d for d in validate_project(project) if d["severity"] == "error"])
        report = clearance.route_report(project, L105, routes[L105]["points_m"])
        self.assertFalse(report["clear"])
        self.assertEqual({(f["tag"], f["component"]) for f in report["intrusions"]}, {("C-101", "body")})
        # The face-mounted joints themselves are contact, not intrusion: the outside L-106 stays clear.
        self.assertTrue(clearance.route_report(project, "conn-column-down", routes["conn-column-down"]["points_m"])[
            "clear"])

    def test_unbounded_composed_parts_refuse_honestly(self):
        project = r47()["current"]
        body = project["model"]["entities"]["ent-column"]["definition"]["geometry"]["components"][0]
        body["parts"].append({"part": "loft", "sections": []})
        with self.assertRaises(clearance.ClearanceError) as caught:
            clearance.body_boxes(project)
        self.assertEqual(caught.exception.code, "clearance_unbounded_geometry")

    def test_route_envelope_includes_bends_and_end_flanges(self):
        points = [[0, 0, 0], [1, 0, 0], [1, 1, 0]]
        envelope = clearance.route_envelope(points, 0.0603, 50, [1, 0, 0], [0, -1, 0])
        parts = [(e["part"], e["index"]) for e in envelope]
        self.assertEqual(parts, [("leg", 0), ("leg", 1), ("bend", 1), ("flange", 0), ("flange", 2)])
        bend = envelope[2]["box"]
        self.assertAlmostEqual(bend[0][0], 1 - 0.075 - 0.03015, places=12)
        self.assertAlmostEqual(bend[1][1], 0.075 + 0.03015, places=12)
        flange = envelope[3]["box"]
        self.assertEqual(flange[0][0], 0)  # starts at the port face, outward only
        self.assertAlmostEqual(flange[1][0], 0.048, places=12)
        self.assertAlmostEqual(flange[1][1], 0.075, places=12)
        # A body touching the port face is contact; one 1 mm into the flange zone is an intrusion.
        touching = {"box": [[-0.2, -0.1, -0.1], [0.0, 0.1, 0.1]], "tag": "T", "component": "c", "entity_id": "e"}
        inside = {"box": [[0.04, 0.07, -0.01], [0.06, 0.2, 0.01]], "tag": "T", "component": "c", "entity_id": "e"}
        self.assertEqual(clearance.intrusions(envelope, [touching]), [])
        self.assertEqual([f["route_part"] for f in clearance.intrusions(envelope, [inside])], ["flange"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.document = r47()

    def test_riser_move_commits_one_manual_revision_and_repairs_l105(self):
        before = copy.deepcopy(self.document)
        new, warnings = apply_command(self.document, move_command(self.document))
        self.assertEqual(self.document, before)
        project = new["current"]
        self.assertEqual(project["revision"], 48)
        self.assertEqual((len(new["undo"]), new["redo"]), (1, []))
        route = project["physical"]["routes"][L105]
        self.assertEqual(route, {"points_m": [[2.7, -0.53, 0.7], [2.7, -1.475, 0.7], [2.7, -1.475, 1.26],
                                              [2.7, -1.875, 1.26], [1.35, -1.875, 1.26], [1.35, -1.875, 1.1]],
                                 "mode": "manual", "status": "current", "assumption_id": "asm-test-move"})
        self.assertNotIn("route_body_intrusion", codes(warnings))
        self.assertEqual([d for d in validate_project(project) if d["severity"] == "warning"], [])
        old = before["current"]
        self.assertEqual(project["pid"], old["pid"])
        self.assertEqual(project["physical"]["placements"], old["physical"]["placements"])
        for cid in old["physical"]["routes"]:
            if cid != L105:
                self.assertEqual(project["physical"]["routes"][cid], old["physical"]["routes"][cid])
        model = copy.deepcopy(project["model"])
        del model["assumptions"]["asm-test-move"]
        self.assertEqual(model, old["model"])

    def test_undo_redo_save_and_reopen(self):
        moved, _ = apply_command(self.document, move_command(self.document))
        undone, _ = apply_command(moved, {"id": "cmd-u", "expected_revision": 48, "type": "undo", "payload": {}})
        self.assertEqual(undone["current"]["physical"], self.document["current"]["physical"])
        self.assertEqual(undone["current"]["pid"], self.document["current"]["pid"])
        redone, _ = apply_command(undone, {"id": "cmd-r", "expected_revision": 49, "type": "redo", "payload": {}})
        self.assertEqual(redone["current"]["physical"], moved["current"]["physical"])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.json")
            save_document(path, redone, None)
            reopened = load_document(path)
        self.assertEqual(reopened, redone)
        self.assertEqual(reopened["current"]["physical"]["routes"][L105]["mode"], "manual")

    def assertRefused(self, document, command, code):
        before = copy.deepcopy(document)
        with self.assertRaises(CommandError) as caught:
            apply_command(document, command)
        self.assertEqual(document, before)  # nothing mutated, no history, no assumption
        self.assertEqual(caught.exception.diagnostics[0]["code"], code, caught.exception.diagnostics)
        return caught.exception.diagnostics

    def test_refusals_change_nothing(self):
        doc = self.document
        cases = [
            (dict(RISER_MOVE, offset_m=[0, 0, 0]), "route_edit_no_op"),
            (dict(RISER_MOVE, offset_m=[0, 0.4, 0.1]), "route_edit_invalid_offset"),
            (dict(RISER_MOVE, offset_m=[0, 0, 0.4]), "route_edit_offset_parallel"),
            (dict(RISER_MOVE, offset_m=[0, 11, 0]), "route_edit_offset_too_large"),
            (dict(RISER_MOVE, offset_m=[0, 1e-7, 0]), "route_edit_offset_too_small"),
            (dict(RISER_MOVE, offset_m=[0, -0.000999, 0]), "route_edit_offset_too_small"),
            (dict(RISER_MOVE, offset_m=[0, 0, 1e-7]), "route_edit_offset_parallel"),
            (dict(RISER_MOVE, offset_m=[0, 1e-7, 1e-7]), "route_edit_invalid_offset"),
            (dict(RISER_MOVE, segment_index=4), "route_edit_unknown_section"),
            (dict(RISER_MOVE, segment_index=2), "route_edit_intrusion"),  # top leg alone: leg 0 stays in C-101
            (dict(RISER_MOVE, segment_index=3), "route_edit_stub_too_short"),
            (dict(RISER_MOVE, segment_index=1, offset_m=[0, 0.02, 0]), "route_edit_bend_room"),
            (dict(RISER_MOVE, segment_index=1.0), "invalid_value"),
            (dict(RISER_MOVE, segment_index=True), "invalid_value"),
            (dict(RISER_MOVE, offset_m=[0, 0.4]), "invalid_value"),
            (dict(RISER_MOVE, connection_id="conn-nope"), "unknown_reference"),
            (dict(RISER_MOVE, points_m=[]), "unsupported_payload"),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload, code=code):
                diagnostics = self.assertRefused(doc, move_command(doc, payload), code)
                if code == "route_edit_intrusion":
                    self.assertIn("route_intrusion", codes(diagnostics))
                    self.assertTrue(any("C-101 body" in d["message"] for d in diagnostics))
        self.assertRefused(doc, dict(move_command(doc), expected_revision=46), "stale_revision")
        reused = dict(RISER_MOVE, assumption_id="asm-dn50-pipe")
        self.assertRefused(doc, move_command(doc, reused, assumptions=False), "assumption_not_fresh")

    def test_a_pending_route_cannot_be_section_moved(self):
        doc = copy.deepcopy(self.document)
        route = doc["current"]["physical"]["routes"][L105]
        route["status"], route["reason"] = "needs_reroute", "test"
        self.assertRefused(doc, move_command(doc), "route_edit_not_current")

    def test_set_route_refuses_new_intrusions(self):
        doc = self.document
        command = {"id": "cmd-test-set", "expected_revision": 47, "type": "set_route",
                   "payload": {"connection_id": L105, "mode": "manual", "assumption_id": "asm-dn50-pipe",
                               "points_m": doc["current"]["physical"]["routes"][L105]["points_m"]}}
        diagnostics = self.assertRefused(doc, command, "route_intrusion")
        self.assertEqual(diagnostics[0]["entity_ids"], [L105, "ent-column"])

    def test_batch_moves_need_their_own_assumption_and_are_rescreened_at_the_end(self):
        doc = self.document
        operation = {"type": "move_route_segment", "payload": dict(RISER_MOVE),
                     "assumptions": {"asm-test-move": {"description": "Test section move.", "source": "test"}}}
        batch = {"id": "cmd-b", "expected_revision": 47, "type": "batch", "payload": {"operations": [operation]}}
        new, _ = apply_command(doc, batch)
        self.assertEqual(new["current"]["physical"]["routes"][L105]["mode"], "manual")
        no_own = {"type": "move_route_segment", "payload": dict(RISER_MOVE)}
        add_elsewhere = {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-122"},
                         "assumptions": {"asm-test-move": {"description": "x", "source": "x"}}}
        self.assertRefused(doc, dict(batch, payload={"operations": [add_elsewhere, no_own]}), "assumption_not_fresh")
        # A later operation moves TK-101 onto the freshly moved riser: the batch's final result is refused.
        placement = copy.deepcopy(doc["current"]["physical"]["placements"]["ent-tank"])
        placement["position_m"] = [2.7, -1.2, 0.0]
        intrude = {"type": "move_equipment", "payload": {"entity_id": "ent-tank", "placement": placement}}
        diagnostics = self.assertRefused(doc, dict(batch, payload={"operations": [operation, intrude]}),
                                         "route_intrusion")
        self.assertIn("final batch result", diagnostics[0]["message"])
        self.assertIn("TK-101", diagnostics[0]["message"])


class InsertionClearanceTests(unittest.TestCase):
    """Explicit current split routes of insert_valve / insert_equipment are screened like set_route."""

    UPSTREAM = [[2.7, -0.53, 0.7], [2.7, -0.92, 0.7]]
    # The old L-105 path from the new valve outlet: through C-101.
    INTRUDING = [[2.7, -1.08, 0.7], [2.7, -1.875, 0.7], [2.7, -1.875, 1.26], [1.35, -1.875, 1.26],
                 [1.35, -1.875, 1.1]]
    CLEAR = [[2.7, -1.08, 0.7], [2.7, -1.24, 0.7], [2.7, -1.24, 1.26], [1.35, -1.24, 1.26], [1.35, -1.875, 1.26],
             [1.35, -1.875, 1.1]]

    def setUp(self):
        from tests.test_core import DOWNSTREAM, CommandTestCase
        self.document = r47()
        self.downstream = DOWNSTREAM
        self.helper = CommandTestCase("setUp")

    def valve_operation(self, downstream_points, routes=True):
        """HV-103 placed on L-105 at (2.7, -1.0, 0.7); with explicit current halves unless ``routes`` is false."""
        payload, extra = self.helper.valve_payload(self.document, placed=True, between=("HV-102", "C-101"),
                                                   position=[2.7, -1.0, 0.7])
        payload["downstream_connection"]["tag"] = "L-107"  # L-106 is already used in this fixture
        if routes:
            assumption = self.document["current"]["physical"]["routes"][L105]["assumption_id"]
            for key, points in (("upstream_route", self.UPSTREAM), ("downstream_route", downstream_points)):
                payload[key] = {"points_m": copy.deepcopy(points), "mode": "manual", "status": "current",
                                "assumption_id": assumption}
        return dict({"type": "insert_valve", "payload": payload}, **extra)

    def direct(self, operation):
        return dict(operation, id="cmd-test-insert", expected_revision=47)

    def batch(self, *operations):
        return {"id": "cmd-test-insert-batch", "expected_revision": 47, "type": "batch",
                "payload": {"operations": list(operations)}}

    def move(self, entity_id, position):
        placement = copy.deepcopy(self.document["current"]["physical"]["placements"][entity_id])
        placement.pop("reason", None)
        placement.update(position_m=position, status="placed", manual=True)
        return {"type": "move_equipment", "payload": {"entity_id": entity_id, "placement": placement}}

    def assertRefused(self, command, where):
        before = copy.deepcopy(self.document)
        with self.assertRaises(CommandError) as caught:
            apply_command(self.document, command)
        self.assertEqual(self.document, before)  # no revision, history, assumption or record written
        diagnostics = caught.exception.diagnostics
        self.assertEqual(codes(diagnostics)[0], "route_intrusion", diagnostics)
        self.assertIn(where, diagnostics[0]["message"])
        return diagnostics

    def test_intruding_current_valve_half_refuses_directly_and_in_a_batch(self):
        self.assertFalse(clearance.route_report(self.document["current"], L105, self.INTRUDING)["clear"])
        direct = self.assertRefused(self.direct(self.valve_operation(self.INTRUDING)), "valve ent-hv-103 split: ")
        self.assertEqual(direct[0]["entity_ids"], [self.downstream, "ent-column"])
        self.assertIn("C-101 body", direct[0]["message"])
        batched = self.assertRefused(self.batch(self.valve_operation(self.INTRUDING)), "operations[0] (insert_valve)")
        self.assertIn("C-101 body", batched[0]["message"])

    def test_clear_supplied_valve_halves_are_accepted_as_submitted(self):
        for command in (self.direct(self.valve_operation(self.CLEAR)), self.batch(self.valve_operation(self.CLEAR))):
            with self.subTest(type=command["type"]):
                new, warnings = apply_command(self.document, command)
                project = new["current"]
                self.assertEqual(project["revision"], 48)
                self.assertEqual(len(new["undo"]), len(self.document["undo"]) + 1)
                routes = project["physical"]["routes"]
                self.assertEqual(routes[L105]["points_m"], self.UPSTREAM)
                self.assertEqual(routes[self.downstream]["points_m"], self.CLEAR)
                self.assertEqual((routes[L105]["status"], routes[self.downstream]["status"]), ("current", "current"))
                self.assertEqual([d for d in warnings if d["severity"] == "warning"], [])
                self.assertNotIn("route_body_intrusion", codes(validate_project(project)))

    def test_a_later_batch_body_move_cannot_leave_a_new_split_intruding(self):
        # HV-101 is unrelated to both halves; moving it onto the new downstream leg must refuse the batch.
        diagnostics = self.assertRefused(self.batch(self.valve_operation(self.CLEAR),
                                                    self.move("ent-suction-valve", [2.0, -1.24, 1.26])),
                                         "final batch result of operations[0] (insert_valve): ")
        self.assertIn("HV-101", diagnostics[0]["message"])

    def test_a_later_endpoint_move_leaves_the_new_split_honestly_pending(self):
        new, warnings = apply_command(self.document, self.batch(self.valve_operation(self.CLEAR),
                                                                self.move("ent-column", [2.35, -1.875, 0])))
        route = new["current"]["physical"]["routes"][self.downstream]
        self.assertEqual(route["status"], "needs_reroute")
        self.assertEqual(route["points_m"], self.CLEAR)
        self.assertIn("route_invalidated", codes(warnings))

    def test_omitted_or_structurally_invalid_halves_stay_pending(self):
        new, warnings = apply_command(self.document, self.direct(self.valve_operation(None, routes=False)))
        routes = new["current"]["physical"]["routes"]
        # Both ends are placed, so the omitted halves await a reroute.
        self.assertEqual({routes[L105]["status"], routes[self.downstream]["status"]}, {"needs_reroute"})
        self.assertIn("inserted without this route", routes[self.downstream]["reason"])
        # Intruding and also wrong at the inlet: the existing pending path applies, not the body refusal.
        bent = copy.deepcopy(self.INTRUDING)
        bent[-1][2] += 0.05
        new, warnings = apply_command(self.document, self.direct(self.valve_operation(bent)))
        routes = new["current"]["physical"]["routes"]
        self.assertEqual({routes[L105]["status"], routes[self.downstream]["status"]}, {"unresolved"})
        self.assertIn("could not be validated", routes[self.downstream]["reason"])
        self.assertIn("route_invalidated", codes(warnings))

    def test_insert_equipment_halves_are_screened_directly_and_in_a_batch(self):
        from examples.make_fixture import make_document
        from tests import test_equipment as composed

        base = make_document()
        clear = composed.drum_batch(base)
        new, _ = apply_command(base, clear)  # the example's own supplied halves are clear
        self.assertEqual(new["current"]["physical"]["routes"]["conn-discharge-3"]["status"], "current")
        intruding = composed.manual_route([(2.7, -0.53, 0.7), (2.7, -1.2, 0.7), (2.7, -1.2, 1.45), (2.7, -1.5, 1.45),
                                           (2.7, -1.5, 1.2)])
        batch = composed.drum_batch(base)
        batch["payload"]["operations"][-1]["payload"]["upstream_route"] = intruding
        self.document = base
        diagnostics = self.assertRefused(batch, "operations[2] (insert_equipment): equipment ent-drum split: ")
        self.assertIn("V-101 body", diagnostics[0]["message"])
        # The same insertion as a single command after the boundary move.
        *moves, insert = batch["payload"]["operations"]
        self.document, _ = apply_command(base, dict(batch, payload={"operations": moves}))
        direct = dict(insert, id="cmd-test-drum", expected_revision=self.document["current"]["revision"])
        self.assertRefused(direct, "equipment ent-drum split: ")


class RerouteTests(unittest.TestCase):
    def test_guided_reroute_of_current47_l105_avoids_c101(self):
        project = copy.deepcopy(r47()["current"])
        project["physical"]["routes"][L105]["status"] = "needs_reroute"
        project["physical"]["routes"][L105]["reason"] = "test: computing the repair against a copy"
        plan = rerouting.plan_reroute(project, L105)
        self.assertEqual(plan["points_m"], [[2.7, -0.53, 0.7], [2.7, -0.69, 0.7], [2.7, -0.69, 1.26],
                                            [1.35, -0.69, 1.26], [1.35, -1.875, 1.26], [1.35, -1.875, 1.1]])
        self.assertEqual(plan["bend_radii_mm"], [75.0] * 4)
        self.assertGreater(plan["candidates_blocked_by_bodies"], 0)
        self.assertTrue(clearance.route_report(project, L105, plan["points_m"])["clear"])
        # The current three-bend route was the old choice: it is now among the blocked candidates.
        self.assertFalse(clearance.route_report(project, L105, r47()["current"]["physical"]["routes"][L105][
            "points_m"])["clear"])
        command = rerouting.reroute_command(r47()["current"], plan, "cmd-test-reroute", "asm-test-reroute", "test")
        self.assertIn("Screened clear of the placed equipment bodies", command["assumptions"]["asm-test-reroute"][
            "description"])
        new, warnings = apply_command(r47(), command)
        self.assertEqual(new["current"]["physical"]["routes"][L105]["mode"], "guided")
        self.assertEqual([d for d in warnings if d["severity"] == "warning"], [])

    def test_current_routes_are_still_refused(self):
        with self.assertRaises(rerouting.RerouteError) as caught:
            rerouting.plan_reroute(r47()["current"], L105)
        self.assertEqual(caught.exception.code, "route_current")


class ServiceTests(unittest.TestCase):
    def test_state_advertises_the_capability_and_commits_through_the_existing_transport(self):
        from tests.test_interactive_fakes import make_service, wait_terminal

        folder = tempfile.TemporaryDirectory(prefix="skid route edit ")
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "project.json"
        save_document(str(path), r47(), None)
        service = make_service(self, path)
        state = service.state()
        self.assertIs(state["capabilities"]["pipe_segment_move"], True)
        self.assertIn("route_body_intrusion", codes(state["diagnostics"]))
        service.command(move_command(load_document(str(path))))  # returns the new state
        state = wait_terminal(service)
        self.assertEqual((state["project"]["revision"], state["history"]["undo"]), (48, 1))
        self.assertEqual(state["last_command"]["type"], "move_route_segment")
        self.assertNotIn("route_body_intrusion", codes(state["diagnostics"]))
        self.assertEqual(load_document(str(path))["current"]["physical"]["routes"][L105]["mode"], "manual")


def solid_intersections(project, connection_id):
    """``{entity tag/component: mm3}`` of actual OCCT pipe-solid overlaps with every placed item's CAD solids."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    from skid.geometry import kernel
    from skid.geometry.build import plan_units
    from skid.geometry.recipes import compile_component

    units, _, failures = plan_units(copy.deepcopy(project))
    assert not failures, failures
    route = next(u for u in units if u["id"] == connection_id)
    pipe, _ = compile_component(next(c for c in route["components"] if c["key"] == "pipe"), "pipe")
    assert kernel.is_valid(pipe)
    found = {}
    for unit in units:
        if unit["type"] != "entity":
            continue
        for component in unit["components"]:
            if component.get("display_only"):
                continue
            shape, _ = compile_component(component, component["key"])
            common = BRepAlgoAPI_Common(pipe, kernel.located(shape, unit["matrix_mm"]))
            common.Build()
            assert common.IsDone()
            volume = abs(kernel.volume(common.Shape()))
            if volume > 1e-6:
                found[f"{unit['tag']} {component['key']}"] = round(volume, 3)
    return found


@unittest.skipUnless(HAVE_CAD, "CadQuery/OCP unavailable; run with .venv-cad/bin/python")
class SolidTests(unittest.TestCase):
    """Independent proof with actual OCCT solids, not the clearance boxes."""

    def test_original_l105_intersects_the_c101_vessel(self):
        found = solid_intersections(r47()["current"], L105)
        self.assertGreater(found.get("C-101 body", 0.0), 1e5, found)

    def test_both_repairs_have_no_solid_overlap_with_any_item(self):
        moved, _ = apply_command(r47(), move_command(r47()))
        project = copy.deepcopy(r47()["current"])
        project["physical"]["routes"][L105]["status"] = "needs_reroute"
        project["physical"]["routes"][L105]["reason"] = "test"
        command = rerouting.reroute_command(r47()["current"], rerouting.plan_reroute(project, L105), "cmd-x",
                                            "asm-x", "test")
        rerouted, _ = apply_command(r47(), command)
        for name, document in (("section move", moved), ("guided reroute", rerouted)):
            with self.subTest(repair=name):
                self.assertEqual(solid_intersections(document["current"], L105), {})

    def test_finned_frame_bound_encloses_the_fin_corners(self):
        import math

        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

        from skid.geometry import kernel
        from skid.geometry.parts import build

        def fins(axis, up, thickness, count):
            return {"part": "finned_frame", "origin": [0, 0, 0], "axis": axis, "length": 100, "core_radius": 50,
                    "fin_tip_radius": 100, "fin_thickness": thickness, "fin_count": count, "up": up,
                    "skip_center_deg": 0, "skip_half_width_deg": 0, "fin_inset": 5}

        theta = math.atan2(10, 100)
        tilted = [math.cos(theta), math.sin(theta), 0]
        oblique = [1 / math.sqrt(3)] * 3
        cases = {"z axis, tilted up": fins([0, 0, 1], tilted, 20, 4),
                 "oblique axis, thick fins": fins(oblique, [1 / math.sqrt(2), -1 / math.sqrt(2), 0], 30, 5)}
        for name, call in cases.items():
            with self.subTest(case=name):
                bound, actual = clearance._part_box(call), kernel.bounds(kernel.compound(build(call)))
                for i in range(3):
                    self.assertLessEqual(bound[0][i], actual[0][i] + 1e-6)
                    self.assertGreaterEqual(bound[1][i], actual[1][i] - 1e-6)
        # A vertical pipe that clips the tilted fin corners beyond the 100 mm tip radius.
        call = cases["z axis, tilted up"]
        pipe = kernel.cylinder([130.3, 0, -500], [130.3, 0, 500], 30.15)
        common = BRepAlgoAPI_Common(pipe, kernel.compound(build(call)))
        common.Build()
        self.assertGreater(abs(kernel.volume(common.Shape())), 40.0)
        envelope = clearance.route_envelope([[0.1303, 0, -0.5], [0.1303, 0, 0.5]], 0.0603, 50)
        body = {"entity_id": "e", "tag": "T", "component": "fins",
                "box": [[c / 1000 for c in corner] for corner in clearance._part_box(call)]}
        self.assertEqual([f["route_part"] for f in clearance.intrusions(envelope, [body])], ["leg"])


if __name__ == "__main__":
    unittest.main()
