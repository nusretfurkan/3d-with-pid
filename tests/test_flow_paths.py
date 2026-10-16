"""Declared flow paths: definition validation, circuit-isolated traversal and the exchanger drawing (docs/EMPTY_START.md).

Projects are built through the real command layer from tests/empty_start_support.py; drafting runs the real
pipeline. Negative controls strip the declared paths to show the legacy reading the paths replace.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from skid.core import apply_command, new_document, validate_project
from skid.drafting import build_drafting
from skid.drafting.description import DescriptionBuilder
from skid.drafting.graph import FlowGraph
from skid.drafting.narrative import process_narrative
from skid.drafting.symbols import FAMILIES, composed_spec
from skid.equipment import LIMITS, SYMBOL_SHAPES, entity_template, validate_definition
from tests import empty_start_support as fx

ROOT = Path(__file__).resolve().parents[1]
SVG = "{http://www.w3.org/2000/svg}"


def codes(diagnostics):
    return {d["code"] for d in diagnostics}


def exchanger_project(flow_paths="declared", place=True, lines=None):
    """The two-circuit exchanger scenario; ``flow_paths`` None strips the declaration (legacy reading)."""
    equipment, all_lines = fx.exchanger_operations(place=place)
    definition = equipment[0]["payload"]["entity"]["definition"]
    if flow_paths is None:
        del definition["flow_paths"]
    elif flow_paths != "declared":
        definition["flow_paths"] = flow_paths
    document = new_document("proj-hx-test")
    document, _ = apply_command(document, fx.batch(document, "cmd-hx-1", equipment))
    chosen = all_lines if lines is None else [all_lines[i] for i in lines]
    if chosen:
        chosen[0]["assumptions"] = all_lines[0]["assumptions"]
        document, _ = apply_command(document, fx.batch(document, "cmd-hx-2", chosen))
    return document


def narrate(project):
    graph = FlowGraph(project)
    builder = DescriptionBuilder(project, graph, {"items": []}, [])
    return graph, builder, process_narrative(builder)


def draft_exchanger_project():
    """Acceptance stage 3 shape: the joined TK-101 > HV-101 > HV-102 > P-101 > OUT-101 flow, then an exchanger added
    as a draft with all four ports open. Its ID sorts before the tank's, so ID order alone would read it first."""
    document = new_document("proj-hx-draft")
    for number, operations in enumerate((fx.stage1_operations(), fx.stage2_operations(), fx.stage3_operations()), 1):
        document, _ = apply_command(document, fx.batch(document, f"cmd-draft-{number}", operations))
    hx = fx.add_exchanger("ent-hx-101", "HX-101", (700.0, 120.0), "asm-hx-101", "asm-dn50-liquid")
    hx["assumptions"] = {"asm-hx-101": fx.assumption("Illustrative exchanger from the example definition defaults.")}
    document, _ = apply_command(document, fx.batch(document, "cmd-draft-4", [hx]))
    return document


def series_project(direct):
    """One stream through both sides of E-101: TK-201 > P-201 > tube side, an explicit external line (through
    HV-202, or ``direct`` from the tube outlet to the shell inlet), shell side > OUT-201. The rest stays open."""
    equipment, lines = fx.exchanger_operations()
    document = new_document("proj-hx-series")
    document, _ = apply_command(document, fx.batch(document, "cmd-series-1", equipment))
    tube_out, shell_in = fx.port_id("ent-e-101", "tube_outlet"), fx.port_id("ent-e-101", "shell_inlet")
    if direct:
        between = [fx.connect("conn-l-210", "L-210", tube_out, shell_in, "sym-e-101", "sym-e-101", "asm-dn50-pipe")]
    else:
        between = [fx.connect("conn-l-210", "L-210", tube_out, fx.port_id("ent-hv-202", "inlet"), "sym-e-101",
                              "sym-hv-202", "asm-dn50-pipe"),
                   fx.connect("conn-l-211", "L-211", fx.port_id("ent-hv-202", "outlet"), shell_in, "sym-hv-202",
                              "sym-e-101", "asm-dn50-pipe")]
    last = fx.connect("conn-l-212", "L-212", fx.port_id("ent-e-101", "shell_outlet"), fx.port_id("ent-out-201", "inlet"),
                      "sym-e-101", "sym-out-201", "asm-dn50-pipe")
    document, _ = apply_command(document, fx.batch(document, "cmd-series-2", lines[:2] + between + [last]))
    return document


class DefinitionFlowPathTests(unittest.TestCase):
    def setUp(self):
        self.definition = fx.hx_definition()

    def problems(self, flow_paths):
        definition = copy.deepcopy(self.definition)
        definition["flow_paths"] = flow_paths
        return validate_definition(definition)

    def test_example_definition_validates_and_exposes_its_paths(self):
        self.assertEqual(validate_definition(self.definition), [])
        entity = fx.add_exchanger("ent-e-101", "E-101", (0, 0), "a", "b")["payload"]["entity"]
        template = entity_template(entity)
        self.assertEqual(template["flow_paths"], [["tube_inlet", "tube_outlet"], ["shell_inlet", "shell_outlet"]])
        self.assertEqual(set(template["ports"]), {"tube_inlet", "tube_outlet", "shell_inlet", "shell_outlet"})
        self.assertEqual(template["definition"]["symbol"], "heat_exchanger")
        self.assertEqual(LIMITS["flow_paths"], 4)
        self.assertIn("heat_exchanger", SYMBOL_SHAPES)

    def test_definitions_without_paths_are_unchanged(self):
        for name in ("horizontal_drum.definition.json", "inline_box_housing.definition.json"):
            with open(ROOT / "examples" / "recipes" / name, encoding="utf-8") as handle:
                definition = json.load(handle)
            with self.subTest(name):
                self.assertEqual(validate_definition(definition), [])
                entity = {"id": "ent-x", "kind": "composed_equipment", "template_version": 1, "definition": definition,
                          "parameters": {}}
                self.assertNotIn("flow_paths", entity_template(entity))

    def test_invalid_declarations_are_rejected(self):
        cases = {
            "not a list": ({"tube_inlet": "tube_outlet"}, "invalid_type"),
            "empty": ([], "missing_field"),
            "too many": ([["tube_inlet", "tube_outlet"]] * 5, "limit_exceeded"),
            "not a pair": ([["tube_inlet"]], "invalid_flow_path"),
            "not names": ([["tube_inlet", 2]], "invalid_flow_path"),
            "one port twice": ([["tube_inlet", "tube_inlet"]], "invalid_flow_path"),
            "unknown port": ([["tube_inlet", "vent"]], "invalid_flow_path"),
            "reversed path": ([["tube_outlet", "tube_inlet"]], "invalid_flow_path"),
            "outlet is an inlet": ([["tube_inlet", "shell_inlet"]], "invalid_flow_path"),
            "shared port": ([["tube_inlet", "tube_outlet"], ["shell_inlet", "tube_outlet"]], "invalid_flow_path"),
        }
        for label, (paths, code) in cases.items():
            with self.subTest(label):
                found = self.problems(paths)
                self.assertIn(code, codes(found), found)
                self.assertTrue(all(d["severity"] == "error" for d in found))

    def test_both_direction_ports_may_open_or_close_a_path(self):
        definition = copy.deepcopy(self.definition)
        definition["ports"]["shell_inlet"]["direction"] = "both"
        definition["ports"]["shell_outlet"]["direction"] = "both"
        definition["flow_paths"] = [["tube_inlet", "tube_outlet"], ["shell_inlet", "shell_outlet"]]
        self.assertEqual(validate_definition(definition), [])


class CircuitIsolationTests(unittest.TestCase):
    def test_two_circuits_are_read_separately(self):
        document = exchanger_project()
        project = document["current"]
        graph, builder, paragraphs = narrate(project)
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "circuit", "scope"])
        tube, shell = paragraphs[0]["text"], paragraphs[1]["text"]
        self.assertIn("P-201 discharges from its top outlet through line L-202 to the tube inlet of E-101", tube)
        self.assertIn("Inside E-101 it follows the declared tube inlet to tube outlet flow path", tube)
        self.assertIn("Flow leaves E-101 from its tube outlet through line L-203 to scope boundary OUT-201", tube)
        self.assertNotIn("L-204", tube)
        self.assertNotIn("L-205", tube)
        self.assertIn("A separate flow circuit, sharing no line with the flow above, starts with", shell)
        self.assertIn("to the shell inlet of E-101. Inside E-101 it follows the declared shell inlet to shell outlet "
                      "flow path, separate from the tube inlet to tube outlet path described above", shell)
        self.assertIn("Flow leaves E-101 from its shell outlet through line L-205 to isolation valve HV-202", shell)
        whole = " ".join(p["text"] for p in paragraphs)
        for claim in ("joins the flow path described above", "closing a loop", "A branch leaves", "suction side",
                      "discharge side"):
            self.assertNotIn(claim, whole)
        flow = [(s["id"], s.get("flow_path")) for s in graph.steps if s["item_kind"] == "entity"]
        self.assertIn(("ent-e-101", ["tube_inlet", "tube_outlet"]), flow)
        self.assertIn(("ent-e-101", ["shell_inlet", "shell_outlet"]), flow)
        self.assertEqual(graph.nearest("ent-hv-202", "up", lambda e: project["model"]["entities"][e]["kind"]
                                       == "centrifugal_pump"), [])

    def test_negative_control_without_declared_paths_crosses_circuits(self):
        project = exchanger_project(flow_paths=None)["current"]
        _, builder, paragraphs = narrate(project)
        whole = " ".join(p["text"] for p in paragraphs)
        # The legacy entity-level reading these paths replace: the shell stream "joins" the tube flow and a
        # shell-side valve is placed on the pump's discharge side.
        self.assertIn("where it joins the flow path described above", whole)
        self.assertIn("on the discharge side of", builder._entity_function("ent-hv-202")["text"])

    def test_description_items_and_sequence_follow_declared_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_drafting(exchanger_project()["current"], directory)
            description = json.loads((Path(directory) / "description.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["status"] for k, v in result.items()},
                         {"pid": "pending", "legend": "pending", "description": "pending"}, "routes are pending")
        items = {item["tag"]: item for item in description["items"]}
        function = items["E-101"]["function"]["text"]
        self.assertIn("Declared tube inlet to tube outlet flow path (authored, not verified): receives flow from P-201 "
                      "through L-202; discharges to OUT-201 through L-203.", function)
        self.assertIn("Declared shell inlet to shell outlet flow path (authored, not verified): receives flow from "
                      "TK-202 through L-204; discharges to HV-202 through L-205.", function)
        self.assertNotIn("side of", items["HV-202"]["function"]["text"])
        records = [r for r in description["flow_sequence"] if r["semantic_id"] == "ent-e-101"]
        self.assertEqual([r["flow_path"] for r in records],
                         [["tube_inlet", "tube_outlet"], ["shell_inlet", "shell_outlet"]])
        self.assertEqual([r["position"] for r in records], ["inline", "inline"])
        self.assertTrue(all("flow_path" not in r for r in description["flow_sequence"]
                            if r["semantic_id"] != "ent-e-101"))

    def test_unconnected_circuit_ports_stay_explicit(self):
        document = exchanger_project(lines=[0, 1, 2])  # tube circuit only
        project = document["current"]
        open_items = [d["entity_ids"][0] for d in validate_project(project) if d["code"] == "required_port_unconnected"]
        self.assertEqual(sorted(open_items), ["port-e-101-shell-inlet", "port-e-101-shell-outlet",
                                              "port-hv-202-inlet", "port-hv-202-outlet", "port-out-202-inlet",
                                              "port-tk-202-outlet"])
        _, _, paragraphs = narrate(project)
        whole = " ".join(p["text"] for p in paragraphs)
        self.assertIn("Ports shell inlet and shell outlet of E-101 have no process connection in the model; open "
                      "required ports are marked unresolved, which keeps the outputs pending.", whole)
        self.assertNotIn("E-101 is not connected", whole)
        self.assertEqual(sorted(p for p, port in project["model"]["ports"].items() if "connection_status" in port),
                         sorted(open_items), "exactly the open required ports carry the explicit draft marker")

    def test_an_unpaired_port_is_not_fed_by_a_declared_path(self):
        # Multiport split: only the tube path is declared; the shell outlet is an unpaired port.
        document = exchanger_project(flow_paths=[["tube_inlet", "tube_outlet"]], lines=[0, 1, 2, 4, 5])
        _, _, paragraphs = narrate(document["current"])
        whole = " ".join(p["text"] for p in paragraphs)
        self.assertIn("Inside E-101 it follows the declared tube inlet to tube outlet flow path, which the definition "
                      "keeps separate from its other ports", whole)
        self.assertIn("Flow starts at its shell outlet, a port outside the declared flow paths of E-101.", whole)
        self.assertIn("Port shell inlet of E-101 has no process connection in the model; open required ports are "
                      "marked unresolved", whole)
        self.assertNotIn("A branch leaves E-101", whole)
        legacy = exchanger_project(flow_paths=None, lines=[0, 1, 2, 4, 5])
        legacy_text = " ".join(p["text"] for p in narrate(legacy["current"])[2])
        self.assertIn("A branch leaves E-101", legacy_text, "without paths every outgoing line is a branch")

    def test_required_unconnected_ports_of_built_ins_are_stated(self):
        document = new_document("proj-stage")
        document, _ = apply_command(document, fx.batch(document, "cmd-1", fx.stage1_operations()))
        _, _, paragraphs = narrate(document["current"])
        whole = " ".join(p["text"] for p in paragraphs)
        self.assertIn("Port outlet of HV-101 is required and explicitly unresolved (not connected yet), an open item "
                      "that keeps the outputs pending.", whole)


PURPOSE_BASIS = ("The model does not record the process purpose, fluid identity or duty; this statement is derived from "
                 "item kinds and connectivity only.")


class SummaryConnectivityTests(unittest.TestCase):
    """The purpose sentence and run order follow node-level connectivity (review findings F1 and F2)."""

    def test_a_single_joined_flow_keeps_the_legacy_purpose_record(self):
        document = new_document("proj-stage")
        for number, operations in enumerate((fx.stage1_operations(), fx.stage2_operations(),
                                             fx.stage3_operations()), 1):
            document, _ = apply_command(document, fx.batch(document, f"cmd-{number}", operations))
        _, builder, _ = narrate(document["current"])
        self.assertEqual(builder._purpose(), {
            "text": "The modeled scope carries liquid service from TK-101 to OUT-101 by way of centrifugal pump P-101. "
                    + PURPOSE_BASIS,
            "parts": [{"text": "The modeled scope carries liquid service from "}, {"ref": "ent-tk-101", "tag": "TK-101"},
                      {"text": " to "}, {"ref": "ent-out-101", "tag": "OUT-101"},
                      {"text": " by way of centrifugal pump "}, {"ref": "ent-p-101", "tag": "P-101"},
                      {"text": ". " + PURPOSE_BASIS}],
            "refs": ["ent-tk-101", "ent-out-101", "ent-p-101"]})

    def test_an_unconnected_draft_item_starts_and_ends_no_flow(self):
        _, builder, _ = narrate(draft_exchanger_project()["current"])
        purpose = builder._purpose()
        # Before the fix: "... from HX-101 and TK-101 to HX-101 and OUT-101 by way of centrifugal pump P-101."
        self.assertEqual(purpose["text"], "The modeled scope carries liquid service from TK-101 to OUT-101 by way of "
                                          "centrifugal pump P-101. HX-101 is not connected to any process line. "
                         + PURPOSE_BASIS)
        self.assertEqual(purpose["refs"], ["ent-tk-101", "ent-out-101", "ent-p-101", "ent-hx-101"])

    def test_declared_circuits_are_separate_clauses(self):
        _, builder, _ = narrate(exchanger_project()["current"])
        text = builder._purpose()["text"]
        # Before the fix: "... from TK-201 and TK-202 to OUT-201 and OUT-202 by way of centrifugal pump P-201."
        self.assertEqual(text, "The modeled scope carries separate connected flows that share no line: liquid service "
                               "from TK-201 to OUT-201 by way of centrifugal pump P-201; and liquid service from TK-202 "
                               "to OUT-202. " + PURPOSE_BASIS)
        clauses = text.split(":", 1)[1].split(";")
        self.assertFalse([c for c in clauses if "TK-202" in c and "P-201" in c], "the pump serves the tube circuit only")
        # Negative control: without the declared paths the exchanger is one node, so the circuits form one flow.
        merged = narrate(exchanger_project(flow_paths=None)["current"])[1]._purpose()["text"]
        self.assertTrue(merged.startswith("The modeled scope carries liquid service from TK-201 and TK-202 to OUT-202 "
                                          "and OUT-201 by way of centrifugal pump P-201. "), merged)

    def test_one_stream_through_both_sides_is_one_flow(self):
        for direct, unconnected in ((False, "OUT-202 and TK-202 are"), (True, "HV-202, OUT-202 and TK-202 are")):
            with self.subTest(direct=direct):
                graph, builder, paragraphs = narrate(series_project(direct)["current"])
                self.assertEqual(builder._purpose()["text"], "The modeled scope carries liquid service from TK-201 to "
                                 f"OUT-201 by way of centrifugal pump P-201. {unconnected} not connected to any "
                                 "process line. " + PURPOSE_BASIS)
                visits = [s for s in graph.steps if s["item_kind"] == "entity" and s["id"] == "ent-e-101"]
                self.assertEqual([s["flow_path"] for s in visits], [["tube_inlet", "tube_outlet"],
                                                                    ["shell_inlet", "shell_outlet"]])
                self.assertEqual({s["run"] for s in visits}, {1}, "one run, the first one, passes both sides")
                self.assertFalse(any(s["closes_loop"] for s in graph.steps))
                self.assertTrue(paragraphs[0]["text"].startswith("The design starts with a vertical cylindrical tank "
                                                                 "TK-201"))
                self.assertIn("separate from the tube inlet to tube outlet path described above", paragraphs[0]["text"])

    def test_no_joined_flow_states_the_unconnected_items(self):
        empty = narrate(new_document("proj-empty")["current"])[1]._purpose()["text"]
        self.assertEqual(empty, "The model contains no connected process flow path. " + PURPOSE_BASIS)
        document = new_document("proj-loose")
        document, _ = apply_command(document, fx.batch(document, "cmd-1", fx.stage1_operations()[:2]))  # no line
        loose = narrate(document["current"])[1]._purpose()["text"]
        self.assertEqual(loose, "The model contains no connected process flow path. HV-101 and TK-101 are not "
                                "connected to any process line. " + PURPOSE_BASIS)

    def test_connected_flow_is_read_before_unconnected_items(self):
        project = draft_exchanger_project()["current"]
        graph, _, paragraphs = narrate(project)
        # Before the fix the exchanger opened the narrative (role "flow") and the real flow was "a separate group".
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "separate", "scope"])
        self.assertTrue(paragraphs[0]["text"].startswith("The design starts with a vertical cylindrical tank TK-101"))
        self.assertTrue(paragraphs[1]["text"].startswith("HX-101, composed equipment from the definition"))
        self.assertTrue(paragraphs[1]["text"].endswith("is not connected to any process line."))
        self.assertEqual(paragraphs[1]["refs"], ["ent-hx-101"])
        self.assertEqual([s["id"] for s in graph.steps if s["item_kind"] == "entity"],
                         ["ent-tk-101", "ent-hv-101", "ent-hv-102", "ent-p-101", "ent-out-101", "ent-hx-101"])
        self.assertEqual(graph.steps[-1], {"item_kind": "entity", "id": "ent-hx-101", "run": 2, "closes_loop": False,
                                           "flow_path": ["tube_inlet", "tube_outlet"]})
        self.assertEqual(sorted(p for p, port in project["model"]["ports"].items() if "connection_status" in port),
                         sorted(fx.port_id("ent-hx-101", n) for n in ("tube_inlet", "tube_outlet", "shell_inlet",
                                                                     "shell_outlet")), "the draft ports stay open")
        with tempfile.TemporaryDirectory() as directory:
            result = build_drafting(project, directory)
            description = json.loads((Path(directory) / "description.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["status"] for k, v in result.items()},
                         {"pid": "pending", "legend": "pending", "description": "pending"})
        self.assertEqual(description["purpose"]["text"], narrate(project)[1]._purpose()["text"])
        self.assertEqual([r["semantic_id"] for r in description["flow_sequence"]][-1], "ent-hx-101")

    def test_branches_stay_with_their_flow_before_unconnected_items(self):
        document = exchanger_project(flow_paths=None)  # E-101 as one node: the shell inflow merges, a branch leaves
        loose = fx.add_builtin("ent-a-loose", "tank", "TK-900", {"diameter": 0.8, "height": 1.2, "outlet_height": 0.2},
                               (900.0, 600.0), "asm-items-hx", "asm-dn50-liquid")
        document, _ = apply_command(document, fx.batch(document, "cmd-loose", [loose]))
        graph, _, paragraphs = narrate(document["current"])
        roles = [p["role"] for p in paragraphs]
        self.assertIn("branch", roles)
        self.assertEqual(roles[-2:], ["separate", "scope"], "the loose tank after the flow and its branch")
        self.assertTrue(paragraphs[-2]["text"].startswith("A cylindrical tank TK-900, 0.8 m in diameter"),
                        paragraphs[-2]["text"])
        self.assertTrue(paragraphs[-2]["text"].endswith("is not connected to any process line."))
        self.assertEqual(graph.steps[-1]["id"], "ent-a-loose")

    def test_a_closed_loop_is_its_own_clause(self):
        # P-101 > HV-101 > HV-102 > P-101 has no start node. Its run keeps its legacy place after the unconnected
        # tank (tests/test_process_narrative.py pins that order); the purpose never calls the loop "no flow path".
        tank, valve, line = fx.stage1_operations()
        pump, suction, first, second = fx.stage2_operations()
        back = fx.connect("conn-l-109", "L-109", fx.port_id("ent-p-101", "outlet"), fx.port_id("ent-hv-101", "inlet"),
                          "sym-p-101", "sym-hv-101", "asm-dn50-pipe")
        first["assumptions"]["asm-dn50-pipe"] = line["assumptions"]["asm-dn50-pipe"]
        document = new_document("proj-loop")
        document, _ = apply_command(document, fx.batch(document, "cmd-1", [tank, valve, pump, suction, first, second,
                                                                           back]))
        graph, builder, paragraphs = narrate(document["current"])
        self.assertEqual([s["id"] for s in graph.steps if s["item_kind"] == "entity"],
                         ["ent-tk-101", "ent-hv-101", "ent-hv-102", "ent-p-101"])
        self.assertTrue(any(s["closes_loop"] for s in graph.steps))
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "loop", "scope"])
        self.assertTrue(paragraphs[0]["text"].endswith("TK-101, 1 m in diameter and 1.6 m high (assumed dimensions) is "
                                                       "not connected to any process line."), paragraphs[0]["text"])
        self.assertIn("closing a loop in the modeled flow", paragraphs[1]["text"])
        self.assertEqual(builder._purpose()["text"], "Lines joining HV-101, HV-102 and P-101 close a loop, so that "
                         "connected flow has no start or end item. TK-101 is not connected to any process line. "
                         + PURPOSE_BASIS)


class ExchangerDrawingTests(unittest.TestCase):
    def test_family_glyph_has_interior_artwork_and_meets_every_anchor(self):
        anchors = fx.hx_definition()["symbol"]["anchors"]
        spec = composed_spec("heat_exchanger", anchors)
        self.assertEqual(spec.key, "symbol/heat_exchanger")
        self.assertEqual(spec.glyph.unmatched, ())
        self.assertEqual(spec.inner, "", "the tag is placed outside the tube bundle")
        roles = [attrs.get("data-role") for _, attrs in spec.draw(1.0)]
        for role in ("body", "tubesheet", "pass-partition", "tube-bundle"):
            self.assertIn(role, roles)
        self.assertEqual(set(FAMILIES), set(SYMBOL_SHAPES))

    def test_sheet_legend_and_index_use_the_exchanger_family_only(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            build_drafting(exchanger_project()["current"], directory)
            pid = ET.parse(out / "pid.svg").getroot()
            legend = ET.parse(out / "legend.svg").getroot()
            index = json.loads((out / "item-index.json").read_text(encoding="utf-8"))
        group = next(e for e in pid.iter(f"{SVG}g") if e.get("data-semantic-id") == "ent-e-101"
                     and e.get("data-occurrence-kind") == "symbol")
        self.assertEqual(group.get("data-symbol-shape"), "heat_exchanger")
        ports = {e.get("data-semantic-id") for e in group.iter(f"{SVG}g") if e.get("data-occurrence-kind")
                 == "port-anchor"}
        self.assertEqual(ports, {"port-e-101-tube-inlet", "port-e-101-tube-outlet", "port-e-101-shell-inlet",
                                 "port-e-101-shell-outlet"})
        entries = {e.get("data-legend-entry") for e in legend.iter() if e.get("data-legend-entry")}
        self.assertIn("symbol/heat_exchanger", entries)
        for other in set(FAMILIES) - {"heat_exchanger"}:
            self.assertNotIn(f"symbol/{other}", entries)
        item = next(i for i in index["items"] if i["semantic_id"] == "ent-e-101")
        self.assertIn("SHELL-AND-TUBE HEAT EXCHANGER", item["description"])

    def test_empty_project_drawings_are_genuinely_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result = build_drafting(new_document("proj-empty")["current"], directory)
            description = json.loads((out / "description.json").read_text(encoding="utf-8"))
            index = json.loads((out / "item-index.json").read_text(encoding="utf-8"))
            pid = ET.parse(out / "pid.svg").getroot()
            legend = ET.parse(out / "legend.svg").getroot()
        self.assertEqual({k: v["status"] for k, v in result.items()},
                         {"pid": "complete", "legend": "complete", "description": "complete"})
        self.assertEqual(description["items"], [])
        self.assertEqual(description["flow_sequence"], [])
        self.assertIn("no process items with ports", description["process_narrative"][0]["text"])
        self.assertEqual(index["items"], [])
        self.assertFalse([e for e in pid.iter() if e.get("data-occurrence-kind") in ("symbol", "edge")])
        self.assertFalse([e for e in legend.iter() if (e.get("data-legend-entry") or "").startswith("symbol/")])


if __name__ == "__main__":
    unittest.main()
