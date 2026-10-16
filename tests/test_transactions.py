"""Atomic batch commands and generic two-port insert_equipment through apply_command.

    python3 -m unittest tests.test_transactions -v      # pure standard library

A batch runs ordinary operations on one working copy and commits only its
validated final result: one revision, one undo snapshot, redo cleared once,
nothing changed on any rejection. insert_equipment reuses the insert_valve
rewiring for composed two-port items; unresolved placement stays pending
until explicitly resolved, and later dimension edits invalidate only routes
whose endpoints actually move.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

from examples.make_fixture import make_document
from skid.core.commands import COMMAND_TYPES, MAX_BATCH_OPERATIONS, apply_command
from skid.core.errors import CommandError
from skid.core.storage import load_document, save_document
from skid.core.validation import validate_document, validate_project
from skid.templates import world_port_position
from tests.test_equipment import (
    DRUM,
    DRUM_ASSUMPTION,
    assumption,
    codes,
    command,
    drum_batch,
    drum_entity,
    errors,
    housing_batch,
    housing_entity,
    inline_payload,
    load_definition,
    manual_route,
    placed,
)

PUMP = "ent-pump"


def rename(tag, entity_id=PUMP):
    return {"type": "rename_entity", "payload": {"entity_id": entity_id, "tag": tag}}


def set_parameter(name, value, entity_id=PUMP, assumption_id="asm-pump-dimensions"):
    return {"type": "set_parameter", "payload": {"entity_id": entity_id, "name": name, "quantity": {
        "value": value, "unit": "m", "assumption_id": assumption_id}}}


def batch(document, operations, command_id="cmd-test-batch"):
    return command(document, "batch", {"operations": operations}, command_id)


class BatchTest(unittest.TestCase):

    def setUp(self):
        self.document = make_document()
        self.frozen = copy.deepcopy(self.document)

    def rejected(self, envelope, code, contains=None):
        frozen_command = copy.deepcopy(envelope)
        with self.assertRaises(CommandError) as caught:
            apply_command(self.document, envelope)
        self.assertEqual(self.document, self.frozen, "a rejected batch must leave the document unchanged")
        self.assertEqual(envelope, frozen_command, "the command must not be mutated")
        self.assertIn(code, codes(caught.exception.diagnostics), caught.exception.diagnostics)
        if contains:
            self.assertTrue(any(contains in d["message"] for d in caught.exception.diagnostics),
                            caught.exception.diagnostics)
        return caught.exception.diagnostics

    def test_contract_example_is_one_revision_and_one_undo_step(self):
        self.assertIn("batch", COMMAND_TYPES)
        self.assertIn("insert_equipment", COMMAND_TYPES)
        operations = [rename("P-201"), dict(set_parameter("motor_length", 0.42, assumption_id="asm-plan"), assumptions={
            "asm-plan": {"description": "User-requested illustrative motor length.",
                         "source": "reviewed engineering plan"}})]
        document, warnings = apply_command(self.document, batch(self.document, operations))
        self.assertEqual(self.document, self.frozen)
        current = document["current"]
        self.assertEqual(current["revision"], 1)
        self.assertEqual((len(document["undo"]), document["redo"]), (1, []))
        self.assertEqual(current["model"]["entities"][PUMP]["tag"], "P-201")
        self.assertEqual(current["model"]["entities"][PUMP]["parameters"]["motor_length"]["value"], 0.42)
        self.assertIn("asm-plan", current["model"]["assumptions"])
        self.assertEqual(errors(validate_document(document)), [])
        self.assertFalse(errors(warnings))
        undone, _ = apply_command(document, command(document, "undo", {}, "cmd-test-undo"))
        self.assertEqual({k: undone["current"][k] for k in ("model", "pid", "physical")},
                         {k: self.document["current"][k] for k in ("model", "pid", "physical")})
        self.assertEqual(undone["current"]["revision"], 2)
        # Dry run and commit share one path: the same input gives the same result.
        again, _ = apply_command(self.document, batch(self.document, operations))
        self.assertEqual(again, document)

    def test_new_batch_clears_redo_once(self):
        document, _ = apply_command(self.document, batch(self.document, [rename("P-201")]))
        document, _ = apply_command(document, command(document, "undo", {}, "cmd-test-undo"))
        self.assertEqual(len(document["redo"]), 1)
        document, _ = apply_command(document, batch(document, [rename("P-301"), rename("TK-201", "ent-tank")]))
        self.assertEqual((document["current"]["revision"], len(document["undo"]), document["redo"]), (3, 1, []))

    def test_rejected_operation_names_its_index_and_leaves_everything_unchanged(self):
        operations = [rename("P-201"), set_parameter("impeller_diameter", 0.2), rename("P-301")]
        found = self.rejected(batch(self.document, operations), "unsupported_property", "operations[1] (set_parameter)")
        self.assertTrue(all(d["message"].startswith("operations[1]") for d in found))
        self.rejected(batch(self.document, [rename("P-201"), rename("TK-101")]), "duplicate_tag", "operations[1]")
        self.rejected(batch(self.document, [rename("P-201"), rename("X", "ent-missing")]), "unknown_reference",
                      "operations[1]")

    def test_limits_nesting_and_shape(self):
        self.rejected(batch(self.document, []), "unsupported_payload")
        self.rejected(batch(self.document, [rename(f"P-{i}") for i in range(MAX_BATCH_OPERATIONS + 1)]),
                      "unsupported_payload")
        for kind in ("batch", "undo", "redo"):
            self.rejected(batch(self.document, [{"type": kind, "payload": {}}]), "unsupported_command", kind)
        self.rejected(batch(self.document, [{"type": "delete_entity", "payload": {}}]), "unsupported_command")
        self.rejected(batch(self.document, [dict(rename("P-201"), note="x")]), "unsupported_payload")
        self.rejected(batch(self.document, [{"type": "rename_entity", "payload": []}]), "unsupported_payload")
        self.rejected(command(self.document, "batch", {"operations": [rename("P-201")], "dry_run": True}, "cmd-x"),
                      "unsupported_payload")
        envelope = batch(self.document, [rename("P-201")])
        envelope["assumptions"] = {"asm-x": assumption("x")}
        self.rejected(envelope, "unsupported_payload")
        stale = batch(self.document, [rename("P-201")])
        stale["expected_revision"] = 5
        self.rejected(stale, "stale_revision")
        self.assertEqual(MAX_BATCH_OPERATIONS, 12)
        document, _ = apply_command(self.document, batch(self.document, [rename(f"P-{i}") for i in range(12)]))
        self.assertEqual(document["current"]["model"]["entities"][PUMP]["tag"], "P-11")

    def test_unchanged_batch_is_one_no_op(self):
        for operations in ([rename("P-101")], [rename("P-201"), rename("P-101")]):
            with self.subTest(operations=len(operations)):
                document, warnings = apply_command(self.document, batch(self.document, operations))
                self.assertEqual(document, self.document)
                self.assertEqual([d["code"] for d in warnings if d["code"] == "no_op"], ["no_op"])

    def test_only_the_final_result_is_validated(self):
        tank = "ent-tank"
        height = self.document["current"]["model"]["entities"][tank]["parameters"]["height"]["value"]
        raise_outlet = set_parameter("outlet_height", height + 0.2, tank, "asm-tank-dimensions")
        raise_shell = set_parameter("height", height + 0.5, tank, "asm-tank-dimensions")
        with self.assertRaises(CommandError):  # alone, the outlet would lie above the shell
            apply_command(self.document, command(self.document, raise_outlet["type"], raise_outlet["payload"], "c"))
        document, _ = apply_command(self.document, batch(self.document, [raise_outlet, raise_shell]))
        self.assertEqual(document["current"]["revision"], 1)
        self.rejected(batch(self.document, [raise_shell, raise_outlet, set_parameter(
            "height", height, tank, "asm-tank-dimensions")]), "out_of_bounds")

    def test_operation_assumptions(self):
        add = {"asm-new": assumption("Illustrative new pump length.")}
        uses = dict(set_parameter("body_length", 0.6, assumption_id="asm-new"), assumptions=add)
        document, _ = apply_command(self.document, batch(self.document, [rename("P-201"), uses]))
        self.assertIn("asm-new", document["current"]["model"]["assumptions"])
        # Added by operation 0, referenced by operation 1.
        document, _ = apply_command(self.document, batch(self.document, [
            dict(rename("P-201"), assumptions=add), set_parameter("body_length", 0.6, assumption_id="asm-new")]))
        self.assertIn("asm-new", document["current"]["model"]["assumptions"])
        self.rejected(batch(self.document, [dict(rename("P-201"), assumptions=add)]), "unreferenced_assumption",
                      "operations 0")
        # Overwritten later in the batch: no longer referenced by the final result.
        self.rejected(batch(self.document, [uses, set_parameter("body_length", 0.6)]), "unreferenced_assumption")
        self.rejected(batch(self.document, [uses, dict(rename("P-201"), assumptions=add)]), "assumption_conflict",
                      "operations[1]")


STATE_WARNINGS = ("route_invalidated", "diagram_edge_invalidated", "placement_unresolved")


def state_warnings(warnings):
    return sorted((d["code"], d["entity_ids"][0]) for d in warnings if d["code"] in STATE_WARNINGS)


def provisional(document, operation):
    """The project after one operation alone: used only to compute explicit test coordinates."""
    result, _ = apply_command(document, command(document, operation["type"], operation["payload"], "cmd-probe"))
    return result["current"]


def route_op(connection_id, points):
    return {"type": "set_route", "payload": {"connection_id": connection_id, "points_m": points, "mode": "manual",
                                             "assumption_id": "asm-manual-route"}}


class BatchCommittedWarningsTest(unittest.TestCase):
    """Returned batch warnings describe the committed result, not intermediate states resolved in the same batch."""

    def setUp(self):
        self.document = make_document()

    def pump_move(self):
        return {"type": "move_equipment", "payload": {"entity_id": PUMP, "placement": {
            "position_m": [1.5, 0.2, 0.0], "rotation_deg": [0.0, 0.0, 0.0], "status": "placed", "manual": True,
            "assumption_id": "asm-skid-layout"}}}

    def pump_reroutes(self):
        project = provisional(self.document, self.pump_move())
        inlet, outlet = (list(world_port_position(project, p)) for p in ("port-pump-inlet", "port-pump-outlet"))
        valve_out = list(world_port_position(project, "port-suction-valve-outlet"))
        check_in = list(world_port_position(project, "port-check-valve-inlet"))
        return [
            route_op("conn-suction-2", [valve_out, [1.0, valve_out[1], valve_out[2]], [1.0, inlet[1], inlet[2]], inlet]),
            route_op("conn-discharge-1", [outlet, [outlet[0], outlet[1], check_in[2]],
                                          [outlet[0], check_in[1], check_in[2]], check_in]),
        ]

    def test_single_move_keeps_its_invalidation_warnings(self):
        move = self.pump_move()
        _, warnings = apply_command(self.document, command(self.document, move["type"], move["payload"], "cmd-m"))
        self.assertEqual(state_warnings(warnings), [("route_invalidated", "conn-discharge-1"),
                                                    ("route_invalidated", "conn-suction-2")])

    def test_move_and_reroute_in_one_batch_reports_no_resolved_invalidation(self):
        document, warnings = apply_command(self.document, batch(self.document, [self.pump_move()]
                                                                 + self.pump_reroutes()))
        self.assertEqual(validate_project(document["current"]), [])
        self.assertEqual(warnings, [])

    def test_partial_resolution_keeps_only_the_remaining_pending_route(self):
        document, warnings = apply_command(self.document, batch(self.document, [self.pump_move()]
                                                                 + self.pump_reroutes()[:1]))
        self.assertEqual(state_warnings(warnings), [("route_invalidated", "conn-discharge-1")])
        route = document["current"]["physical"]["routes"]["conn-discharge-1"]
        (effect,) = [d for d in warnings if d["code"] == "route_invalidated"]
        self.assertTrue(effect["message"].startswith("operations[0] (move_equipment):"))
        self.assertIn(f"is {route['status']}: {route['reason']}", effect["message"])
        self.assertIn("route_not_current", codes(warnings))  # the final validation still reports it

    def test_reinvalidated_route_reports_its_last_cause_once(self):
        second = copy.deepcopy(self.pump_move())
        second["payload"]["placement"]["position_m"] = [1.5, 0.3, 0.0]
        document, warnings = apply_command(self.document, batch(self.document, [self.pump_move()]
                                                                 + self.pump_reroutes() + [second]))
        self.assertEqual(state_warnings(warnings), [("route_invalidated", "conn-discharge-1"),
                                                    ("route_invalidated", "conn-suction-2")])
        self.assertTrue(all(d["message"].startswith("operations[3]") for d in warnings
                            if d["code"] == "route_invalidated"))
        self.assertEqual(document["current"]["physical"]["routes"]["conn-suction-2"]["status"], "needs_reroute")

    def test_symbol_move_and_manual_diagram_repair_in_one_batch(self):
        move = {"type": "move_symbol", "payload": {"symbol_id": "sym-check-valve", "x": 480.0, "y": 200.0,
                                                   "rotation_deg": 0.0}}
        _, single = apply_command(self.document, command(self.document, move["type"], move["payload"], "cmd-s"))
        self.assertEqual(state_warnings(single), [("diagram_edge_invalidated", "edge-discharge-1")])
        repair = {"type": "set_diagram_route", "payload": {"edge_id": "edge-discharge-1",
                                                           "points": [[360.0, 272.0], [360.0, 200.0], [464.0, 200.0]]}}
        document, warnings = apply_command(self.document, batch(self.document, [move, repair]))
        self.assertEqual(warnings, [])
        self.assertEqual(validate_project(document["current"]), [])
        edge = document["current"]["pid"]["edges"]["edge-discharge-1"]
        self.assertEqual((edge["status"], edge["manual"]), ("current", True))

    def test_composed_placement_and_reroute_batch_reports_the_committed_state(self):
        # Insert V-101 without placement (warnings expected), then resolve everything in one batch.
        payload = inline_payload(self.document, "conn-discharge-3", drum_entity(), "drum", "L-106", (700, 220))
        pending, inserted = apply_command(self.document, command(self.document, "insert_equipment", payload, "cmd-i",
                                                                 assumptions={DRUM_ASSUMPTION: assumption("x")}))
        # Single-command semantics are unchanged (the handler effect and final validation both report placement).
        self.assertEqual(set(state_warnings(inserted)), {("placement_unresolved", DRUM),
                                                         ("route_invalidated", "conn-discharge-3"),
                                                         ("route_invalidated", "conn-drum-out")})
        operations = [
            {"type": "move_equipment", "payload": {"entity_id": "ent-outlet-boundary", "placement": {
                "position_m": [2.7, -3.0, 0.75], "rotation_deg": [0.0, 0.0, -90.0], "status": "placed",
                "manual": True, "assumption_id": "asm-skid-layout"}}},
            {"type": "place_equipment", "payload": {"entity_id": DRUM, "placement": placed((2.7, -1.6, 0.0))}},
            route_op("conn-discharge-3", [[2.7, -0.53, 0.7], [2.7, -0.68, 0.7], [2.7, -0.68, 1.45],
                                          [2.7, -1.5, 1.45], [2.7, -1.5, 1.2]]),
            route_op("conn-drum-out", [[2.7, -2.58, 0.75], [2.7, -2.9, 0.75]]),
            {"type": "set_diagram_route", "payload": {"edge_id": "edge-drum-out",
                                                      "points": [[760.0, 220.0], [702.0, 220.0]]}},
        ]
        document, warnings = apply_command(pending, batch(pending, operations))
        self.assertEqual(validate_project(document["current"]), [])
        self.assertEqual(warnings, [])
        self.assertEqual((document["current"]["revision"], len(document["undo"])), (2, 2))
        # Without the downstream route the drum is placed but that route stays honestly pending.
        document, warnings = apply_command(pending, batch(pending, operations[:3] + operations[4:]))
        self.assertEqual(state_warnings(warnings), [("route_invalidated", "conn-drum-out")])
        self.assertEqual(document["current"]["physical"]["routes"]["conn-drum-out"]["status"], "needs_reroute")


def drum_parameter(name, value, assumption_id=DRUM_ASSUMPTION):
    return {"type": "set_parameter", "payload": {"entity_id": DRUM, "name": name, "quantity": {
        "value": value, "unit": "m", "assumption_id": assumption_id}}}


class BatchRouteStateTest(unittest.TestCase):
    """Route state committed by a batch follows placement records and final frames, not intermediate states."""

    @classmethod
    def setUpClass(cls):
        cls.base = make_document()
        cls.drum, _ = apply_command(cls.base, drum_batch(cls.base))  # drum placed, both routes current

    def routes(self, document, *ids):
        return {cid: (document["current"]["physical"]["routes"][cid]["status"],
                      document["current"]["physical"]["routes"][cid].get("reason")) for cid in ids}

    def pump_at(self, y):
        return {"type": "move_equipment", "payload": {"entity_id": PUMP, "placement": dict(
            self.base["current"]["physical"]["placements"][PUMP], position_m=[1.5, y, 0.0], manual=True)}}

    def test_transient_constraint_violation_is_rerouting_not_missing_placement(self):
        # shell_length 0.8 alone violates "inlet at least 120 mm from the right tangent line"; inlet_offset 0.4
        # restores validity. Either order must commit the same, true route state.
        shorter, inlet = drum_parameter("shell_length", 0.8), drum_parameter("inlet_offset", 0.4)
        with self.assertRaises(CommandError) as caught:
            apply_command(self.drum, command(self.drum, shorter["type"], shorter["payload"], "cmd-single"))
        self.assertIn("definition_resolution_failed", codes(caught.exception.diagnostics))
        results = {}
        for label, operations in (("violating first", [shorter, inlet]), ("valid order", [inlet, shorter])):
            document, warnings = apply_command(self.drum, batch(self.drum, operations))
            self.assertEqual(errors(validate_document(document)), [], label)
            self.assertEqual(document["current"]["physical"]["placements"][DRUM]["status"], "placed")
            states = self.routes(document, "conn-discharge-3", "conn-drum-out")
            self.assertEqual({status for status, _ in states.values()}, {"needs_reroute"}, label)
            for _, reason in states.values():
                self.assertTrue(reason.endswith("supply validated route points"), (label, reason))
            self.assertFalse([d for d in warnings if "placement unresolved" in d["message"]], label)
            self.assertFalse([d for d in warnings if d["code"] == "route_invalidated" and DRUM in d["entity_ids"]])
            results[label] = {cid: status for cid, (status, _) in states.items()}
        self.assertEqual(results["violating first"], results["valid order"])

    def test_genuinely_unplaced_endpoint_is_still_unresolved(self):
        payload = inline_payload(self.base, "conn-discharge-3", drum_entity(), "drum", "L-106", (700, 220))
        document, _ = apply_command(self.base, batch(self.base, [{"type": "insert_equipment", "payload": payload,
                                                                  "assumptions": {DRUM_ASSUMPTION: assumption("x")}}]))
        for status, reason in self.routes(document, "conn-discharge-3", "conn-drum-out").values():
            self.assertEqual(status, "unresolved")
            self.assertIn(f"placement unresolved for {DRUM}", reason)

    def test_changed_and_restored_dimension_is_one_no_op(self):
        document, warnings = apply_command(self.drum, batch(self.drum, [drum_parameter("shell_length", 1.8),
                                                                         drum_parameter("shell_length", 1.4)]))
        self.assertEqual(document, self.drum)
        self.assertEqual([d["code"] for d in warnings if d["code"] in STATE_WARNINGS + ("no_op",)], ["no_op"])

    def test_restored_value_with_new_provenance_keeps_routes_current(self):
        reviewed = dict(drum_parameter("shell_length", 1.8, "asm-reviewed"),
                        assumptions={"asm-reviewed": assumption("Reviewed drum length (restored).")})
        document, warnings = apply_command(self.drum, batch(self.drum, [
            reviewed, drum_parameter("shell_length", 1.4, "asm-reviewed")]))
        self.assertEqual(document["current"]["revision"], self.drum["current"]["revision"] + 1)
        self.assertEqual(document["current"]["physical"], self.drum["current"]["physical"])
        self.assertEqual(validate_project(document["current"]), [])
        self.assertEqual(warnings, [])

    def test_move_away_and_back_is_one_no_op_in_both_views(self):
        original = copy.deepcopy(self.base["current"]["physical"]["placements"][PUMP])
        back = {"type": "move_equipment", "payload": {"entity_id": PUMP, "placement": original}}
        document, warnings = apply_command(self.base, batch(self.base, [self.pump_at(0.2), back]))
        self.assertEqual((document, codes(warnings) & set(STATE_WARNINGS)), (self.base, set()))
        symbol = self.base["current"]["pid"]["symbols"]["sym-check-valve"]
        moves = [{"type": "move_symbol", "payload": {"symbol_id": "sym-check-valve", "x": x, "y": y,
                                                     "rotation_deg": 0.0}}
                 for x, y in ((480.0, 200.0), (symbol["x"], symbol["y"]))]
        document, warnings = apply_command(self.base, batch(self.base, moves))
        self.assertEqual(document, self.base)  # the manual edge edge-discharge-1 fits its anchors again
        self.assertIn("no_op", codes(warnings))

    def test_pre_existing_pending_paths_are_not_promoted(self):
        original = copy.deepcopy(self.base["current"]["physical"]["placements"][PUMP])
        moved, _ = apply_command(self.base, command(self.base, "move_equipment", self.pump_at(0.2)["payload"], "c1"))
        back, _ = apply_command(moved, command(moved, "move_equipment", {"entity_id": PUMP, "placement": original},
                                               "c2"))
        # Single commands keep their semantics: the routes stay flagged even though their points fit again.
        pending = self.routes(back, "conn-suction-2", "conn-discharge-1")
        self.assertEqual({status for status, _ in pending.values()}, {"needs_reroute"})
        renamed, _ = apply_command(back, batch(back, [rename("P-201")]))
        self.assertEqual(self.routes(renamed, "conn-suction-2", "conn-discharge-1"), pending)

    def test_deliberate_route_edit_is_kept_while_untouched_routes_reconcile(self):
        away = self.pump_at(0.2)
        project = provisional(self.base, away)
        inlet = list(world_port_position(project, "port-pump-inlet"))
        valve_out = list(world_port_position(project, "port-suction-valve-outlet"))
        edited = route_op("conn-suction-2", [valve_out, [1.0, valve_out[1], valve_out[2]],
                                             [1.0, inlet[1], inlet[2]], inlet])
        back = {"type": "move_equipment", "payload": {"entity_id": PUMP, "placement": copy.deepcopy(
            self.base["current"]["physical"]["placements"][PUMP])}}
        document, warnings = apply_command(self.base, batch(self.base, [away, edited, back]))
        routes = document["current"]["physical"]["routes"]
        self.assertEqual(routes["conn-suction-2"]["status"], "needs_reroute")
        self.assertEqual(routes["conn-suction-2"]["points_m"], edited["payload"]["points_m"])
        self.assertEqual(routes["conn-discharge-1"], self.base["current"]["physical"]["routes"]["conn-discharge-1"])
        self.assertEqual(state_warnings(warnings), [("route_invalidated", "conn-suction-2")])


class InsertEquipmentTest(unittest.TestCase):

    def setUp(self):
        self.document = make_document()

    def insert(self, document=None, envelope_extra=None, **payload_options):
        document = document or self.document
        options = dict(connection_id="conn-discharge-3", entity=drum_entity(), prefix="drum", line_tag="L-106",
                       symbol_xy=(700, 220))
        options.update(payload_options)
        payload = inline_payload(document, **options)
        extra = {"assumptions": {DRUM_ASSUMPTION: assumption("Illustrative drum for the test.")}}
        extra.update(envelope_extra or {})
        return apply_command(document, command(document, "insert_equipment", payload, "cmd-test-insert", **extra))

    def rejected(self, code, **options):
        with self.assertRaises(CommandError) as caught:
            self.insert(**options)
        self.assertIn(code, codes(caught.exception.diagnostics), caught.exception.diagnostics)
        self.assertEqual(self.document, make_document())

    def test_unresolved_insertion_then_explicit_resolution_in_an_isolated_document(self):
        document, warnings = self.insert()
        project = document["current"]
        model, physical, pid = project["model"], project["physical"], project["pid"]
        self.assertEqual(errors(validate_document(document)), [])
        self.assertEqual(model["connections"]["conn-discharge-3"]["to_port_id"], "port-drum-inlet")
        self.assertEqual(model["connections"]["conn-drum-out"]["from_port_id"], "port-drum-outlet")
        self.assertEqual(model["connections"]["conn-drum-out"]["to_port_id"], "port-boundary-inlet")
        self.assertEqual(pid["edges"]["edge-discharge-3"]["to_symbol_id"], "sym-drum")
        self.assertEqual(physical["placements"][DRUM]["status"], "unresolved")
        self.assertIsNone(world_port_position(project, "port-drum-inlet"))
        for cid in ("conn-discharge-3", "conn-drum-out"):
            self.assertEqual(physical["routes"][cid]["status"], "unresolved")
        self.assertIn("placement_unresolved", codes(warnings))
        self.assertEqual(model["entities"][DRUM]["definition"], load_definition())
        # Explicit resolution: move OUT-101 clear, place the drum, route both halves and the diagram edge.
        operations = [
            {"type": "move_equipment", "payload": {"entity_id": "ent-outlet-boundary", "placement": {
                "position_m": [2.7, -3.0, 0.75], "rotation_deg": [0.0, 0.0, -90.0], "status": "placed",
                "manual": True, "assumption_id": "asm-skid-layout"}}},
            {"type": "place_equipment", "payload": {"entity_id": DRUM, "placement": placed((2.7, -1.6, 0.0))}},
            {"type": "set_route", "payload": {"connection_id": "conn-discharge-3", "mode": "manual",
                                              "assumption_id": "asm-manual-route", "points_m": [
                                                  [2.7, -0.53, 0.7], [2.7, -0.68, 0.7], [2.7, -0.68, 1.45],
                                                  [2.7, -1.5, 1.45], [2.7, -1.5, 1.2]]}},
            {"type": "set_route", "payload": {"connection_id": "conn-drum-out", "mode": "manual",
                                              "assumption_id": "asm-manual-route",
                                              "points_m": [[2.7, -2.58, 0.75], [2.7, -2.9, 0.75]]}},
            {"type": "set_diagram_route", "payload": {"edge_id": "edge-drum-out",
                                                      "points": [[760.0, 220.0], [702.0, 220.0]]}},
        ]
        resolved, warnings = apply_command(document, command(document, "batch", {"operations": operations}, "cmd-r"))
        current = resolved["current"]
        self.assertEqual(current["revision"], 2)
        self.assertEqual(world_port_position(current, "port-drum-inlet"), (2.7, -1.5, 1.2))
        for cid in ("conn-discharge-3", "conn-drum-out"):
            self.assertEqual(current["physical"]["routes"][cid]["status"], "current")
        self.assertEqual(current["pid"]["edges"]["edge-drum-out"]["status"], "current")
        self.assertFalse([d for d in warnings if d["code"] in ("placement_unresolved", "route_not_current")])

    def test_batch_insertion_with_placement_and_routes(self):
        document, _ = apply_command(self.document, drum_batch(self.document))
        current = document["current"]
        self.assertEqual((current["revision"], len(document["undo"])), (1, 1))
        for cid in ("conn-discharge-3", "conn-drum-out"):
            self.assertEqual(current["physical"]["routes"][cid]["status"], "current", cid)
            self.assertEqual(current["physical"]["routes"][cid]["mode"], "manual")
        self.assertEqual(current["pid"]["edges"]["edge-discharge-3"]["points"], [[616.0, 220.0], [640.0, 220.0]])
        self.assertEqual(current["pid"]["edges"]["edge-drum-out"]["status"], "current")
        housing, _ = apply_command(document, housing_batch(document))
        self.assertEqual(errors(validate_document(housing)), [])
        self.assertEqual(housing["current"]["model"]["connections"]["conn-drum-out"]["to_port_id"],
                         "port-housing-inlet")
        # Manual routes and occurrences of the earlier insertion survive the later one unchanged.
        self.assertEqual(housing["current"]["physical"]["routes"]["conn-discharge-3"],
                         current["physical"]["routes"]["conn-discharge-3"])
        self.assertEqual(housing["current"]["pid"]["symbols"]["sym-drum"], current["pid"]["symbols"]["sym-drum"])

    def test_rejections(self):
        tank = dict(make_document()["current"]["model"]["entities"]["ent-tank"], id="ent-new-tank", tag="TK-9")
        self.rejected("unsupported_property", entity=tank)
        valve = dict(make_document()["current"]["model"]["entities"]["ent-check-valve"], id="ent-v", tag="NRV-9")
        self.rejected("unsupported_property", entity=valve)
        self.rejected("missing_definition", entity={k: v for k, v in drum_entity().items() if k != "definition"})
        self.rejected("missing_template", entity=dict(drum_entity(), template_version=2))
        bad = drum_entity()
        bad["definition"]["symbol"]["shape"] = "spaceship"
        self.rejected("unsupported_symbol", entity=bad)
        three = drum_entity()
        three["definition"]["ports"]["drain"] = {"direction": "out", "required": False, "service": "liquid"}
        three["definition"]["symbol"]["anchors"]["drain"] = [0, 30]
        three["definition"]["geometry"]["ports"]["drain"] = dict(three["definition"]["geometry"]["ports"]["outlet"])
        self.rejected("unsupported_property", entity=three)
        self.rejected("definition_resolution_failed", entity=drum_entity(inlet_offset=0.35))
        self.rejected("invalid_value", entity=drum_entity(shell_length=9.0))
        self.rejected("unsupported_payload", entity=dict(drum_entity(), observes={"entity_id": "ent-tank",
                                                                                  "variable": "level"}))
        self.rejected("duplicate_tag", entity=dict(drum_entity(), tag="TK-101"))
        self.rejected("duplicate_id", entity=dict(drum_entity(), id="ent-tank"))

    def test_insert_valve_keeps_its_restrictions(self):
        payload = inline_payload(self.document, "conn-discharge-3", drum_entity(), "drum", "L-106", (700, 220))
        extra = {"assumptions": {DRUM_ASSUMPTION: assumption("x")}}
        with self.assertRaises(CommandError) as caught:
            apply_command(self.document, command(self.document, "insert_valve", payload, "cmd-x", **extra))
        self.assertIn("unsupported_payload", codes(caught.exception.diagnostics))  # no definition field on valves
        entity = {k: v for k, v in drum_entity().items() if k != "definition"}
        payload = inline_payload(self.document, "conn-discharge-3", entity, "drum", "L-106", (700, 220))
        with self.assertRaises(CommandError) as caught:
            apply_command(self.document, command(self.document, "insert_valve", payload, "cmd-x", **extra))
        self.assertIn("unsupported_property", codes(caught.exception.diagnostics))

    def test_definition_assumption_ids_are_not_project_references(self):
        # "asm-drum-ports" is declared inside the definition; adding a project assumption of the same ID
        # must still be referenced by a project record, not by the definition's local ID.
        document, _ = self.insert()
        added = {"asm-drum-ports": assumption("unrelated")}
        with self.assertRaises(CommandError) as caught:
            apply_command(document, command(document, "rename_entity", {"entity_id": DRUM, "tag": "V-201"}, "cmd-y",
                                            assumptions=added))
        self.assertIn("unreferenced_assumption", codes(caught.exception.diagnostics))


class ComposedEditTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.base = make_document()
        cls.document, _ = apply_command(cls.base, drum_batch(cls.base))

    def edit(self, name, value, document=None):
        document = document or self.document
        payload = {"entity_id": DRUM, "name": name, "quantity": {"value": value, "unit": "m",
                                                                  "assumption_id": DRUM_ASSUMPTION}}
        return apply_command(document, command(document, "set_parameter", payload, "cmd-test-edit"))

    def routes(self, document):
        return {cid: document["current"]["physical"]["routes"][cid]["status"]
                for cid in ("conn-discharge-3", "conn-drum-out")}

    def test_only_routes_whose_endpoints_move_are_invalidated(self):
        moved_inlet, warnings = self.edit("inlet_offset", 0.7)
        self.assertEqual(self.routes(moved_inlet), {"conn-discharge-3": "needs_reroute", "conn-drum-out": "current"})
        # inlet local x = -L/2 + inlet_offset = 0.0 m; the -90 deg turn maps local +x to world -y.
        self.assertEqual(world_port_position(moved_inlet["current"], "port-drum-inlet"), (2.7, -1.6, 1.2))
        self.assertEqual([d["entity_ids"][0] for d in warnings if d["code"] == "route_invalidated"],
                         ["conn-discharge-3"])
        # The invalidated route keeps its manual points.
        self.assertEqual(moved_inlet["current"]["physical"]["routes"]["conn-discharge-3"]["points_m"],
                         self.document["current"]["physical"]["routes"]["conn-discharge-3"]["points_m"])
        longer, _ = self.edit("shell_length", 1.8)
        self.assertEqual(self.routes(longer), {"conn-discharge-3": "needs_reroute", "conn-drum-out": "needs_reroute"})
        same, warnings = self.edit("shell_length", 1.4)
        self.assertEqual(same, self.document)
        self.assertIn("no_op", codes(warnings))

    def test_constraint_violations_are_rejected_without_change(self):
        frozen = copy.deepcopy(self.document)
        for name, value, code in (("inlet_offset", 0.35, "definition_resolution_failed"),
                                  ("shell_length", 2.7, "invalid_value")):
            with self.subTest(name=name):
                with self.assertRaises(CommandError) as caught:
                    self.edit(name, value)
                self.assertIn(code, codes(caught.exception.diagnostics))
        self.assertEqual(self.document, frozen)

    def test_undo_redo_save_reload_preserve_definitions_and_manual_work(self):
        edited, _ = self.edit("inlet_offset", 0.7)
        undone, _ = apply_command(edited, command(edited, "undo", {}, "cmd-u"))
        self.assertEqual({k: undone["current"][k] for k in ("model", "pid", "physical")},
                         {k: self.document["current"][k] for k in ("model", "pid", "physical")})
        redone, _ = apply_command(undone, command(undone, "redo", {}, "cmd-r"))
        self.assertEqual(redone["current"]["model"], edited["current"]["model"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "project.json")
            save_document(path, redone, None)
            loaded = load_document(path)
            self.assertEqual(loaded, json.loads(json.dumps(redone)))
            self.assertEqual(loaded["current"]["model"]["entities"][DRUM]["definition"], load_definition())
            self.assertEqual(errors(validate_document(loaded)), [])
            back, _ = apply_command(loaded, command(loaded, "undo", {}, "cmd-u2"))
            self.assertEqual(back["current"]["physical"]["routes"]["conn-discharge-3"]["mode"], "manual")
            # Two undo steps return to the original fixture: the batch insertion was a single step.
            first, _ = apply_command(back, command(back, "undo", {}, "cmd-u3"))
            self.assertEqual({k: first["current"][k] for k in ("model", "pid", "physical")},
                             {k: self.base["current"][k] for k in ("model", "pid", "physical")})

    def test_housing_is_a_second_composition_without_a_new_kind(self):
        document, _ = apply_command(self.document, housing_batch(self.document))
        entities = document["current"]["model"]["entities"]
        self.assertEqual({entities[DRUM]["kind"], entities["ent-housing"]["kind"]}, {"composed_equipment"})
        self.assertNotEqual(entities[DRUM]["definition"]["id"], entities["ent-housing"]["definition"]["id"])
        self.assertEqual(world_port_position(document["current"], "port-housing-inlet"), (2.7, -3.0, 0.75))
        self.assertEqual(housing_entity()["definition"]["symbol"]["shape"], "box_equipment")


if __name__ == "__main__":
    unittest.main()
