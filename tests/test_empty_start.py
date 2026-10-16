"""Empty start: new_document, CLI ``new``, ``add_equipment`` / ``connect_ports``, atomic batches and history.

See docs/EMPTY_START.md. Fixtures (tests/empty_start_support.py) are synthetic and illustrative.
"""

import contextlib
import copy
import io
import json
import os
import re
import tempfile
import unittest

from skid import cli
from skid.core import (CommandError, DocumentError, apply_command, load_document, new_document, save_document,
                       validate_document, validate_project)
from skid.core.commands import COMMAND_TYPES
from tests import empty_start_support as fx


def codes(diagnostics):
    return {d["code"] for d in diagnostics}


def frozen(document):
    return json.dumps(document, sort_keys=True)


def stage1(document=None):
    document = document or new_document("proj-test", "Empty start test")
    return apply_command(document, fx.batch(document, "cmd-stage-1", fx.stage1_operations()))


def stage2():
    document, _ = stage1()
    return apply_command(document, fx.batch(document, "cmd-stage-2", fx.stage2_operations()))


def single(document, operation, command_id="cmd-single"):
    command = {"id": command_id, "expected_revision": document["current"]["revision"], "type": operation["type"],
               "payload": copy.deepcopy(operation["payload"])}
    if "assumptions" in operation:
        command["assumptions"] = copy.deepcopy(operation["assumptions"])
    return command


class NewDocumentTests(unittest.TestCase):
    def test_new_document_is_genuinely_empty(self):
        document = new_document("proj-a")
        self.assertEqual(document, {
            "schema_version": 1, "undo": [], "redo": [],
            "current": {"id": "proj-a", "revision": 0,
                        "model": {"entities": {}, "ports": {}, "connections": {}, "assumptions": {}},
                        "pid": {"symbols": {}, "edges": {}},
                        "physical": {"placements": {}, "routes": {}}}})
        self.assertEqual(validate_document(document), [])
        self.assertEqual(validate_project(document["current"]), [], "an empty model has no open items")
        self.assertIn("new_document", __import__("skid.core", fromlist=["__all__"]).__all__)

    def test_name_is_trimmed_and_checked_like_rename_project(self):
        self.assertEqual(new_document("proj-a", "  Blank study ")["current"]["name"], "Blank study")
        for name in ("", "   ", "a\nb", "‮evil", 5, "x" * 121):
            with self.subTest(name=name), self.assertRaises(DocumentError) as caught:
                new_document("proj-a", name)
            self.assertEqual(codes(caught.exception.diagnostics), {"invalid_project_name"})

    def test_invalid_project_ids_create_nothing(self):
        for project_id in ("", " proj", "proj/x", "-proj", "a" * 129, None, 7, "proj id"):
            with self.subTest(project_id=project_id), self.assertRaises(DocumentError) as caught:
                new_document(project_id)
            self.assertEqual(codes(caught.exception.diagnostics), {"invalid_project_id"})

    def test_documents_are_independent_and_round_trip(self):
        first, second = new_document("proj-a"), new_document("proj-a")
        first["current"]["model"]["entities"]["x"] = {}
        self.assertEqual(second["current"]["model"]["entities"], {})
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "blank.json")
            save_document(path, second, None)
            self.assertEqual(load_document(path), second)


class CliNewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_new_writes_an_empty_document_with_a_uuid_id(self):
        path = os.path.join(self.dir, "nested", "blank.json")
        status, out, _ = self.run_cli("new", path)
        self.assertEqual(status, cli.EXIT_OK, out)
        document = load_document(path)
        project = document["current"]
        self.assertRegex(project["id"], r"^project-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertNotIn("name", project)
        self.assertEqual(document, new_document(project["id"]))
        self.assertIn(project["id"], out)
        status, out, _ = self.run_cli("validate", path)
        self.assertEqual(status, cli.EXIT_OK, out)

    def test_new_with_name_and_id(self):
        path = os.path.join(self.dir, "named.json")
        status, _, _ = self.run_cli("new", path, "--name", "Exchanger study", "--project-id", "proj-hx-1")
        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(load_document(path), new_document("proj-hx-1", "Exchanger study"))

    def test_new_never_overwrites(self):
        path = os.path.join(self.dir, "existing.json")
        with open(path, "wb") as handle:
            handle.write(b"user bytes, not a document")
        status, _, err = self.run_cli("new", path)
        self.assertEqual(status, cli.EXIT_CONFLICT, err)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"user bytes, not a document")
        self.assertIn("nothing was written", err)
        link = os.path.join(self.dir, "dangling.json")
        os.symlink(os.path.join(self.dir, "missing-target.json"), link)
        status, _, _ = self.run_cli("new", link)
        self.assertEqual(status, cli.EXIT_CONFLICT)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "missing-target.json")))

    def test_invalid_arguments_write_nothing(self):
        path = os.path.join(self.dir, "bad.json")
        status, _, err = self.run_cli("new", path, "--project-id", "bad id")
        self.assertEqual(status, cli.EXIT_USAGE, err)
        status, _, err = self.run_cli("new", path, "--name", "  ")
        self.assertEqual(status, cli.EXIT_USAGE, err)
        self.assertFalse(os.path.lexists(path))


class StagedConstructionTests(unittest.TestCase):
    def test_command_types_include_the_foundation_commands(self):
        self.assertIn("add_equipment", COMMAND_TYPES)
        self.assertIn("connect_ports", COMMAND_TYPES)

    def test_tank_and_valve_then_pump_and_suction_valve(self):
        document, warnings = stage1()
        project = document["current"]
        self.assertEqual(project["revision"], 1)
        self.assertEqual(len(document["undo"]), 1)
        self.assertEqual(set(project["model"]["entities"]), {"ent-tk-101", "ent-hv-101"})
        self.assertEqual(set(project["model"]["ports"]),
                         {"port-tk-101-outlet", "port-hv-101-inlet", "port-hv-101-outlet"})
        self.assertEqual(project["model"]["connections"]["conn-l-101"]["from_port_id"], "port-tk-101-outlet")
        self.assertEqual(project["pid"]["edges"]["edge-l-101"]["points"], [[160.0, 320.0], [224.0, 320.0]])
        self.assertEqual(project["physical"]["routes"]["conn-l-101"]["status"], "current")
        # The valve outlet is not connected yet: an explicit warning, never silently complete.
        open_items = [d for d in warnings if d["code"] == "required_port_unconnected"]
        self.assertEqual([d["entity_ids"] for d in open_items], [["port-hv-101-outlet", "ent-hv-101"]])
        self.assertTrue(all(d["severity"] == "warning" for d in warnings))

        document, warnings = apply_command(document, fx.batch(document, "cmd-stage-2", fx.stage2_operations()))
        project = document["current"]
        self.assertEqual((project["revision"], len(document["undo"]), document["redo"]), (2, 2, []))
        self.assertEqual(set(project["model"]["connections"]), {"conn-l-101", "conn-l-102", "conn-l-103"})
        self.assertEqual(project["model"]["connections"]["conn-l-102"]["from_port_id"], "port-hv-101-outlet")
        self.assertEqual(project["model"]["connections"]["conn-l-103"]["to_port_id"], "port-p-101-inlet")
        remaining = [d for d in validate_project(project)]
        self.assertEqual([(d["code"], d["entity_ids"]) for d in remaining],
                         [("required_port_unconnected", ["port-p-101-outlet", "ent-p-101"])])
        self.assertEqual({e["tag"] for e in project["model"]["entities"].values()},
                         {"TK-101", "HV-101", "P-101", "HV-102"})
        for record in project["model"]["entities"].values():  # provenance on every parameter
            self.assertTrue(all("assumption_id" in q for q in record["parameters"].values()))

    def test_single_commands_and_indicator(self):
        document = new_document("proj-single")
        tank = fx.stage1_operations()[0]
        document, warnings = apply_command(document, single(document, tank))
        self.assertEqual(document["current"]["revision"], 1)
        self.assertIn("required_port_unconnected", codes(warnings))
        indicator = fx.add_builtin("ent-li-101", "indicator", "LI-101", {"diameter": 0.08}, (60.0, 200.0),
                                   "asm-tk-101", "asm-dn50-liquid")
        indicator["payload"]["entity"]["observes"] = {"entity_id": "ent-tk-101", "variable": "level"}
        indicator["payload"]["ports"] = []
        document, warnings = apply_command(document, single(document, indicator, "cmd-indicator"))
        placement = document["current"]["physical"]["placements"]["ent-li-101"]
        self.assertEqual((placement["status"], placement["position_m"], placement["assumption_id"]),
                         ("unresolved", None, None))
        self.assertIn("no physical placement supplied", placement["reason"])
        self.assertIn("placement_unresolved", codes(warnings))

    def test_undo_save_reopen_preserves_ids_and_history(self):
        document, _ = stage2()
        after_stage1, _ = stage1()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "project.json")
            save_document(path, document, None)
            reopened = load_document(path)
            self.assertEqual(reopened, document)
            undone, _ = apply_command(reopened, {"id": "cmd-undo", "expected_revision": 2, "type": "undo",
                                                 "payload": {}})
            for key in ("model", "pid", "physical"):
                self.assertEqual(undone["current"][key], after_stage1["current"][key])
            self.assertEqual((undone["current"]["revision"], len(undone["undo"]), len(undone["redo"])), (3, 1, 1))
            save_document(path, undone, 2)
            again = load_document(path)
            redone, _ = apply_command(again, {"id": "cmd-redo", "expected_revision": 3, "type": "redo", "payload": {}})
            for key in ("model", "pid", "physical"):
                self.assertEqual(redone["current"][key], document["current"][key])
            self.assertEqual(redone["current"]["name"], "Empty start test")


def markers(document):
    return sorted(pid for pid, port in document["current"]["model"]["ports"].items() if "connection_status" in port)


def errors_of(diagnostics):
    return {d["code"] for d in diagnostics if d["severity"] == "error"}


def warnings_of(diagnostics):
    return {d["code"] for d in diagnostics if d["severity"] == "warning"}


class DraftMarkerTests(unittest.TestCase):
    """Explicit ``connection_status: "unresolved"`` open ports versus unmarked (legacy) broken topology."""

    def test_add_marks_open_required_ports_and_connect_clears_both_ends_once(self):
        document, _ = stage1()
        self.assertEqual(markers(document), ["port-hv-101-outlet"],
                         "the batch connected tank outlet and valve inlet; only the valve outlet is still open")
        self.assertEqual(document["current"]["model"]["ports"]["port-hv-101-outlet"]["connection_status"], "unresolved")
        document, _ = apply_command(document, fx.batch(document, "cmd-2", fx.stage2_operations()))
        self.assertEqual(markers(document), ["port-p-101-outlet"])
        document, _ = apply_command(document, fx.batch(document, "cmd-3", fx.stage3_operations()))
        self.assertEqual(markers(document), [])
        self.assertEqual(validate_project(document["current"]), [], "every required port is joined")

    def test_marker_is_derived_when_omitted_and_accepted_when_explicit(self):
        document = new_document("proj-marker")
        omitted = fx.stage1_operations()[0]
        explicit = copy.deepcopy(omitted)
        explicit["payload"]["ports"][0]["connection_status"] = "unresolved"
        for operation in (omitted, explicit):
            with self.subTest(explicit="connection_status" in operation["payload"]["ports"][0]):
                result, warnings = apply_command(document, single(document, operation))
                self.assertEqual(markers(result), ["port-tk-101-outlet"])
                self.assertEqual(warnings_of(warnings), {"required_port_unconnected"})

    def test_unmarked_legacy_break_is_an_error_but_an_explicit_draft_is_a_warning(self):
        document, _ = stage1()
        self.assertEqual(errors_of(validate_document(document)), set())
        self.assertIn("required_port_unconnected", warnings_of(validate_document(document)))
        broken = copy.deepcopy(document)
        del broken["current"]["model"]["ports"]["port-hv-101-outlet"]["connection_status"]
        self.assertIn("required_port_unconnected", errors_of(validate_document(broken)))
        with self.assertRaises(CommandError):  # the core refuses to work on a broken input document
            apply_command(broken, {"id": "cmd-x", "expected_revision": 1, "type": "rename_entity",
                                   "payload": {"entity_id": "ent-tk-101", "tag": "TK-201"}})

    def test_invalid_contradictory_and_optional_markers_are_refused(self):
        document, _ = stage1()
        ports = lambda d: d["current"]["model"]["ports"]  # noqa: E731
        for value in ("open", "", True, None, "UNRESOLVED"):
            with self.subTest(value=value):
                bad = copy.deepcopy(document)
                ports(bad)["port-hv-101-outlet"]["connection_status"] = value
                self.assertIn("invalid_connection_status", errors_of(validate_document(bad)))
        connected = copy.deepcopy(document)
        ports(connected)["port-tk-101-outlet"]["connection_status"] = "unresolved"
        self.assertIn("contradictory_connection_status", errors_of(validate_document(connected)))
        # An optional port (required: false in a definition) may not be marked.
        definition = fx.hx_definition()
        definition["ports"]["shell_outlet"]["required"] = False
        hx = fx.add_exchanger("ent-e-101", "E-101", (520.0, 300.0), "asm-tk-101", "asm-dn50-liquid")
        hx["payload"]["entity"]["definition"] = definition
        next(p for p in hx["payload"]["ports"] if p["name"] == "shell_outlet")["required"] = False
        added, _ = apply_command(document, single(document, hx))
        self.assertNotIn("connection_status", ports(added)["port-e-101-shell-outlet"])
        self.assertIn("connection_status", ports(added)["port-e-101-shell-inlet"])
        optional = copy.deepcopy(hx)
        next(p for p in optional["payload"]["ports"] if p["name"] == "shell_outlet")["connection_status"] = "unresolved"
        with self.assertRaises(CommandError) as caught:
            apply_command(document, single(document, optional))
        self.assertIn("invalid_connection_status", codes(caught.exception.diagnostics))
        marked = copy.deepcopy(added)
        ports(marked)["port-e-101-shell-outlet"]["connection_status"] = "unresolved"
        self.assertIn("invalid_connection_status", errors_of(validate_document(marked)))
        wrong = fx.stage1_operations()[0]
        wrong["payload"]["ports"][0]["connection_status"] = "open"
        fresh = new_document("proj-wrong")
        with self.assertRaises(CommandError) as caught:
            apply_command(fresh, single(fresh, wrong))
        self.assertIn("invalid_connection_status", codes(caught.exception.diagnostics))

    def test_inline_insertion_never_leaves_a_marker(self):
        document, _ = stage1()
        payload = {
            "connection_id": "conn-l-101",
            "entity": {"id": "ent-nrv-101", "kind": "check_valve", "tag": "NRV-101", "template_version": 1,
                       "parameters": {"length": {"value": 0.18, "unit": "m", "assumption_id": "asm-hv-dimensions"},
                                      "body_diameter": {"value": 0.11, "unit": "m",
                                                        "assumption_id": "asm-hv-dimensions"}}},
            "ports": fx.ports_for("ent-nrv-101", {"inlet": "in", "outlet": "out"}, "asm-dn50-liquid"),
            "downstream_connection": {"id": "conn-l-101b", "tag": "L-101B", "from_port_id": "port-nrv-101-outlet",
                                      "to_port_id": "port-hv-101-inlet", "nominal_diameter_mm": 50,
                                      "outside_diameter_m": 0.0603, "assumption_id": "asm-dn50-pipe"},
            "symbol": {"id": "sym-nrv-101", "entity_id": "ent-nrv-101", "x": 200.0, "y": 320.0, "rotation_deg": 0.0,
                       "manual": False},
            "downstream_edge": {"id": "edge-l-101b", "connection_id": "conn-l-101b", "from_symbol_id": "sym-nrv-101",
                                "to_symbol_id": "sym-hv-101", "points": [], "manual": False, "status": "unresolved",
                                "reason": "test insertion"},
        }
        command = {"id": "cmd-insert", "expected_revision": 1, "type": "insert_valve", "payload": payload}
        inserted, _ = apply_command(document, command)
        self.assertEqual(markers(inserted), ["port-hv-101-outlet"])
        marked = copy.deepcopy(command)
        marked["payload"]["ports"][0]["connection_status"] = "unresolved"
        with self.assertRaises(CommandError) as caught:
            apply_command(document, marked)
        self.assertIn("invalid_connection_status", codes(caught.exception.diagnostics))

    def test_undo_restores_and_save_reload_preserves_markers(self):
        document, _ = stage2()
        self.assertEqual(markers(document), ["port-p-101-outlet"])
        undone, _ = apply_command(document, {"id": "cmd-undo", "expected_revision": 2, "type": "undo", "payload": {}})
        self.assertEqual(markers(undone), ["port-hv-101-outlet"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "draft.json")
            save_document(path, undone, None)
            reopened = load_document(path)
        self.assertEqual(markers(reopened), ["port-hv-101-outlet"])
        self.assertEqual(reopened["redo"][0]["model"]["ports"]["port-p-101-outlet"]["connection_status"], "unresolved")
        redone, _ = apply_command(reopened, {"id": "cmd-redo", "expected_revision": 3, "type": "redo", "payload": {}})
        self.assertEqual(markers(redone), ["port-p-101-outlet"])

    def test_outputs_stay_pending_until_every_required_port_is_joined(self):
        from skid.drafting import build_drafting  # pure drafting; CAD is covered in tests/test_cad_empty_exchanger.py

        document, _ = stage2()
        with tempfile.TemporaryDirectory() as directory:
            pending = build_drafting(document["current"], directory)
            with open(os.path.join(directory, "item-index.json"), encoding="utf-8") as handle:
                self.assertIn("P-101", {item["tag"] for item in json.load(handle)["items"]})
        self.assertEqual({k: v["status"] for k, v in pending.items()},
                         {"pid": "pending", "legend": "pending", "description": "pending"})
        self.assertTrue(all("required_port_unconnected" in {d["code"] for d in m["diagnostics"]}
                            for m in pending.values()))
        document, _ = apply_command(document, fx.batch(document, "cmd-3", fx.stage3_operations()))
        with tempfile.TemporaryDirectory() as directory:
            complete = build_drafting(document["current"], directory)
        self.assertEqual({k: v["status"] for k, v in complete.items()},
                         {"pid": "complete", "legend": "complete", "description": "complete"})


class AtomicRejectionTests(unittest.TestCase):
    def setUp(self):
        self.base, _ = stage1()
        self.pump, self.suction = fx.stage2_operations()[:2]

    def assertRejected(self, document, command, code, where=None):
        before = frozen(document)
        command_before = frozen(command)
        with self.assertRaises(CommandError) as caught:
            apply_command(document, command)
        self.assertIn(code, codes(caught.exception.diagnostics), caught.exception.diagnostics)
        if where is not None:
            self.assertTrue(any(where in d["message"] for d in caught.exception.diagnostics),
                            caught.exception.diagnostics)
        self.assertEqual(frozen(document), before, "a rejected command must leave the document unchanged")
        self.assertEqual(frozen(command), command_before, "a rejected command must not be mutated")
        return caught.exception.diagnostics

    def add_variant(self, mutate, operation=None):
        operation = copy.deepcopy(operation or self.pump)
        mutate(operation["payload"])
        return single(self.base, operation)

    def test_add_equipment_rejections(self):
        def port(name):
            return lambda p: next(x for x in p["ports"] if x["name"] == name)

        cases = {
            "duplicate entity id": (lambda p: p["entity"].update(id="ent-tk-101"), "duplicate_id"),
            "port id reused": (lambda p: port("inlet")(p).update(id="port-tk-101-outlet"), "duplicate_id"),
            "symbol id equals entity id": (lambda p: p["symbol"].update(id="ent-p-101"), "duplicate_id"),
            "port id equals assumption id": (lambda p: port("inlet")(p).update(id="asm-tk-101"), "duplicate_id"),
            "duplicate tag": (lambda p: p["entity"].update(tag=" tk-101 "), "duplicate_tag"),
            "line tag reused": (lambda p: p["entity"].update(tag="L-101"), "duplicate_tag"),
            "unknown kind": (lambda p: p["entity"].update(kind="reactor"), "missing_template"),
            "unsupported version": (lambda p: p["entity"].update(template_version=2), "missing_template"),
            "missing parameter": (lambda p: p["entity"]["parameters"].pop("motor_length"), "unsupported_payload"),
            "extra parameter": (lambda p: p["entity"]["parameters"].update(
                speed={"value": 1.0, "unit": "m", "assumption_id": "asm-tk-101"}), "unsupported_payload"),
            "no provenance": (lambda p: p["entity"]["parameters"]["body_length"].pop("assumption_id"),
                              "missing_provenance"),
            "wrong unit": (lambda p: p["entity"]["parameters"]["body_length"].update(unit="mm"), "invalid_unit"),
            "nonpositive": (lambda p: p["entity"]["parameters"]["body_length"].update(value=0), "invalid_value"),
            "nonfinite-like string": (lambda p: p["entity"]["parameters"]["body_length"].update(value="1"),
                                      "invalid_value"),
            "wrong direction": (lambda p: port("inlet")(p).update(direction="out"), "wrong_port_direction"),
            "both direction": (lambda p: port("inlet")(p).update(direction="both"), "wrong_port_direction"),
            "DN80": (lambda p: port("inlet")(p).update(nominal_diameter_mm=80), "unsupported_nominal_diameter"),
            "service": (lambda p: port("inlet")(p).update(service="gas"), "port_template_mismatch"),
            "not required": (lambda p: port("inlet")(p).update(required=False), "port_template_mismatch"),
            "required as 1": (lambda p: port("inlet")(p).update(required=1), "port_template_mismatch"),
            "extra port field": (lambda p: port("inlet")(p).update(size="DN50"), "unsupported_payload"),
            "missing port": (lambda p: p.update(ports=[x for x in p["ports"] if x["name"] != "outlet"]),
                             "topology_mismatch"),
            "unknown port": (lambda p: port("inlet")(p).update(name="suction"), "unknown_port"),
            "duplicate port name": (lambda p: port("outlet")(p).update(name="inlet", direction="in"),
                                    "duplicate_port_name"),
            "foreign port owner": (lambda p: port("inlet")(p).update(entity_id="ent-tk-101"), "topology_mismatch"),
            "symbol owner": (lambda p: p["symbol"].update(entity_id="ent-tk-101"), "topology_mismatch"),
            "symbol coordinate": (lambda p: p["symbol"].update(x="300"), "invalid_value"),
            "observes on a pump": (lambda p: p["entity"].update(observes={"entity_id": "ent-tk-101",
                                                                         "variable": "level"}), "invalid_observes"),
            "definition on a built-in": (lambda p: p["entity"].update(definition=fx.hx_definition()),
                                         "invalid_definition"),
            "unknown port assumption": (lambda p: port("inlet")(p).update(assumption_id="asm-missing"),
                                        "unknown_assumption"),
            "unknown placement assumption": (lambda p: p["placement"].update(assumption_id="asm-missing"),
                                             "unknown_assumption"),
            "placement half-placed": (lambda p: p["placement"].update(position_m=None), "invalid_value"),
            "ports not a list": (lambda p: p.update(ports="inlet,outlet"), "unsupported_payload"),
            "entity not an object": (lambda p: p.update(entity=["ent"]), "unsupported_payload"),
            "unknown payload field": (lambda p: p.update(route=None), "unsupported_payload"),
        }
        for label, (mutate, code) in cases.items():
            with self.subTest(label):
                self.assertRejected(self.base, self.add_variant(mutate), code)

    def test_composed_entity_without_definition_and_unresolvable_values(self):
        hx = fx.add_exchanger("ent-e-101", "E-101", (520.0, 300.0), "asm-tk-101", "asm-dn50-liquid")
        missing = copy.deepcopy(hx)
        del missing["payload"]["entity"]["definition"]
        self.assertRejected(self.base, single(self.base, missing), "missing_definition")
        bad = copy.deepcopy(hx)
        bad["payload"]["entity"]["parameters"]["shell_length"]["value"] = 1.2  # violates the saddle constraint
        self.assertRejected(self.base, single(self.base, bad), "definition_resolution_failed")
        broken = copy.deepcopy(hx)
        broken["payload"]["entity"]["definition"]["flow_paths"] = [["tube_inlet", "shell_inlet"]]
        self.assertRejected(self.base, single(self.base, broken), "invalid_flow_path")

    def test_tank_outlet_above_shell_is_refused_by_final_validation(self):
        tank = fx.add_builtin("ent-tk-102", "tank", "TK-102", {"diameter": 1.0, "height": 1.0, "outlet_height": 1.2},
                              (120.0, 500.0), "asm-tk-101", "asm-dn50-liquid")
        self.assertRejected(self.base, single(self.base, tank), "out_of_bounds")

    def connected_base(self):
        document, _ = apply_command(self.base, fx.batch(self.base, "cmd-items", [self.pump, self.suction]))
        return document

    def connect_variant(self, document, mutate):
        operation = fx.connect("conn-l-102", "L-102", "port-hv-101-outlet", "port-hv-102-inlet", "sym-hv-101",
                               "sym-hv-102", "asm-dn50-pipe", [[1.08, 0.0, 0.2], [1.32, 0.0, 0.2]],
                               "asm-route-stage-1")
        mutate(operation["payload"])
        return single(document, operation)

    def test_connect_ports_rejections(self):
        document = self.connected_base()
        self.assertEqual(apply_command(document, self.connect_variant(document, lambda p: None))[0]["current"]
                         ["revision"], 3, "the unmutated connection is accepted")
        c = lambda p: p["connection"]  # noqa: E731
        cases = {
            "from an inlet": (lambda p: c(p).update(from_port_id="port-hv-102-inlet", to_port_id="port-p-101-inlet"),
                              "wrong_port_direction"),
            "to an outlet": (lambda p: c(p).update(to_port_id="port-p-101-outlet"), "wrong_port_direction"),
            "used port": (lambda p: c(p).update(from_port_id="port-tk-101-outlet"), "duplicate_port_use"),
            "used target": (lambda p: c(p).update(to_port_id="port-hv-101-inlet"), "duplicate_port_use"),
            "self": (lambda p: c(p).update(to_port_id="port-hv-101-outlet"), "self_connection"),
            "unknown port": (lambda p: c(p).update(to_port_id="port-nowhere"), "unknown_reference"),
            "DN80": (lambda p: c(p).update(nominal_diameter_mm=80), "dn_mismatch"),
            "DN not whole": (lambda p: c(p).update(nominal_diameter_mm=50.5), "invalid_nominal_diameter"),
            "outside diameter": (lambda p: c(p).update(outside_diameter_m=0), "invalid_value"),
            "duplicate id": (lambda p: c(p).update(id="conn-l-101"), "duplicate_id"),
            "edge id equals line id": (lambda p: p["edge"].update(id="conn-l-102"), "duplicate_id"),
            "duplicate tag": (lambda p: c(p).update(tag="hv-101"), "duplicate_tag"),
            "extra field": (lambda p: c(p).update(service="liquid"), "unsupported_payload"),
            "unknown assumption": (lambda p: c(p).update(assumption_id="asm-x"), "unknown_assumption"),
            "edge symbol of another entity": (lambda p: p["edge"].update(from_symbol_id="sym-tk-101"),
                                              "occurrence_mismatch"),
            "edge maps another line": (lambda p: p["edge"].update(connection_id="conn-l-101"), "topology_mismatch"),
            "edge points off the anchors": (lambda p: p["edge"].update(points=[[0, 0], [10, 0]]), "invalid_route"),
            "manual edge without points": (lambda p: p["edge"].update(manual=True), "invalid_route"),
            "pending edge without reason": (lambda p: p["edge"].update(status="unresolved"), "invalid_value"),
            "route off the port": (lambda p: p["route"].update(points_m=[[1.1, 0, 0.2], [1.32, 0, 0.2]]),
                                   "invalid_route"),
            "route not orthogonal": (lambda p: p["route"].update(
                points_m=[[1.08, 0, 0.2], [1.2, 0.1, 0.2], [1.32, 0, 0.2]]), "invalid_route"),
            "route mode": (lambda p: p["route"].update(mode="auto"), "invalid_value"),
            "current route with reason": (lambda p: p["route"].update(reason="x"), "unsupported_payload"),
        }
        for label, (mutate, code) in cases.items():
            with self.subTest(label):
                self.assertRejected(document, self.connect_variant(document, mutate), code)

    def test_route_to_an_unplaced_item_is_refused(self):
        pump = copy.deepcopy(self.pump)
        del pump["payload"]["placement"]
        document, _ = apply_command(self.base, fx.batch(self.base, "cmd-items", [pump, self.suction]))
        line = fx.connect("conn-l-103", "L-103", "port-hv-102-outlet", "port-p-101-inlet", "sym-hv-102", "sym-p-101",
                          "asm-dn50-pipe", [[1.48, 0.0, 0.2], [1.7, 0.0, 0.2]], "asm-route-stage-1")
        self.assertRejected(document, single(document, line), "invalid_route", "without a resolved world position")

    def test_failed_operation_rolls_back_the_whole_batch(self):
        operations = fx.stage2_operations()
        operations[3]["payload"]["connection"]["nominal_diameter_mm"] = 80
        self.assertRejected(self.base, fx.batch(self.base, "cmd-bad", operations), "dn_mismatch",
                            "operations[3] (connect_ports)")
        reuse = fx.stage2_operations()
        reuse[3]["payload"]["connection"]["from_port_id"] = "port-hv-101-outlet"  # already joined by operations[2]
        self.assertRejected(self.base, fx.batch(self.base, "cmd-reuse", reuse), "duplicate_port_use",
                            "operations[3] (connect_ports)")
        late = fx.stage2_operations()
        late[2]["payload"]["connection"]["to_port_id"] = "port-hv-103-inlet"  # created only by a later operation
        self.assertRejected(self.base, fx.batch(self.base, "cmd-late", late), "unknown_reference",
                            "operations[2] (connect_ports)")


class RouteScreeningTests(unittest.TestCase):
    def blocker(self, position):
        operation = fx.add_builtin("ent-blocker", "boundary", "X-901", {"size": 0.2}, (600.0, 500.0), "asm-tk-101",
                                   "asm-dn50-liquid", fx.placed(position, "asm-layout-stage-1"))
        return operation

    def test_connect_route_into_a_body_is_refused(self):
        operations = fx.stage1_operations()
        operations[2]["payload"]["route"]["points_m"] = [[0.6, 0.0, 0.2], [0.6, 0.0, 0.5], [0.0, 0.0, 0.5],
                                                         [0.0, 0.0, 1.9], [0.92, 0.0, 1.9], [0.92, 0.0, 0.2]]
        with self.assertRaises(CommandError) as caught:
            apply_command(new_document("proj-x"), fx.batch(new_document("proj-x"), "cmd-x", operations))
        self.assertIn("route_intrusion", codes(caught.exception.diagnostics))

    def test_final_batch_screen_catches_a_body_added_after_the_route(self):
        document = new_document("proj-screen")
        clear = fx.stage1_operations() + [self.blocker((0.5, 1.5, 0.2))]
        apply_command(document, fx.batch(document, "cmd-clear", clear))  # negative control: away from the route
        intruding = fx.stage1_operations() + [self.blocker((0.76, 0.0, 0.2))]
        with self.assertRaises(CommandError) as caught:
            apply_command(document, fx.batch(document, "cmd-intrude", intruding))
        found = [d for d in caught.exception.diagnostics if d["code"] == "route_intrusion"]
        self.assertTrue(found, caught.exception.diagnostics)
        self.assertIn("final batch result of operations[2] (connect_ports)", found[0]["message"])
        self.assertIn("X-901", found[0]["message"])

    def test_body_added_later_onto_an_existing_route_is_reported_not_hidden(self):
        document, _ = stage1()
        blocker = self.blocker((0.76, 0.0, 0.2))
        blocker["assumptions"] = {}
        document, warnings = apply_command(document, single(document, {"type": "add_equipment",
                                                                       "payload": blocker["payload"]}))
        intrusions = [d for d in warnings if d["code"] == "route_body_intrusion"]
        self.assertEqual(len(intrusions), 1)
        self.assertEqual(intrusions[0]["severity"], "warning")
        self.assertIn("conn-l-101", intrusions[0]["entity_ids"])

    def test_omitted_route_is_unresolved_until_a_route_is_set_in_the_same_batch(self):
        document, _ = stage1()
        pump, suction, first, second = fx.stage2_operations()
        del first["payload"]["route"]
        pending, warnings = apply_command(document, fx.batch(document, "cmd-p", [pump, suction, first, second]))
        route = pending["current"]["physical"]["routes"]["conn-l-102"]
        self.assertEqual((route["status"], route["points_m"], route["assumption_id"]),
                         ("unresolved", [], "asm-dn50-pipe"))
        self.assertIn("no physical route supplied", route["reason"])
        self.assertIn("route_invalidated", codes(warnings))
        repair = {"type": "set_route", "payload": {"connection_id": "conn-l-102", "mode": "guided",
                                                   "points_m": [[1.08, 0.0, 0.2], [1.32, 0.0, 0.2]],
                                                   "assumption_id": "asm-route-stage-2"}}
        done, warnings = apply_command(document, fx.batch(document, "cmd-q", [pump, suction, first, second, repair]))
        self.assertEqual(done["current"]["physical"]["routes"]["conn-l-102"]["status"], "current")
        self.assertNotIn("route_invalidated", codes(warnings), "a resolved intermediate warning is not reported")

    def test_guided_edge_path_uses_the_existing_deterministic_rule(self):
        document = new_document("proj-edge")
        operations = fx.stage1_operations()
        operations[1]["payload"]["symbol"]["y"] = 380.0  # not aligned with the tank outlet anchor
        document, _ = apply_command(document, fx.batch(document, "cmd-e", operations))
        self.assertEqual(document["current"]["pid"]["edges"]["edge-l-101"]["points"],
                         [[160.0, 320.0], [192.0, 320.0], [192.0, 380.0], [224.0, 380.0]])

    def test_dry_run_is_the_same_call_and_never_mutates(self):
        document = new_document("proj-dry")
        before = frozen(document)
        result, _ = apply_command(document, fx.batch(document, "cmd-dry", fx.stage1_operations()))
        self.assertEqual(frozen(document), before)
        self.assertEqual(result["current"]["revision"], 1)
        self.assertTrue(re.match(r"^\{", frozen(result)))


if __name__ == "__main__":
    unittest.main()
