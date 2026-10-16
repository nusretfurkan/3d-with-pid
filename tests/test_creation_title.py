"""Drawing subtitle, which is also the description title: entity-level wording only where it is exact.

``SOURCES TO SINKS VIA PUMPS`` stays exactly as before for one connected flow with every process item on a line
and no item declaring separate flow paths. An unconnected draft item, separate flows or a multi-path item read
the generic ``PROCESS SCHEMATIC`` instead of an entity-level list that joins items no line joins.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from skid.core import apply_command, new_document
from skid.drafting import build_drafting
from skid.drafting.graph import FlowGraph
from skid.drafting.sheet import PidSheet
from tests import empty_start_support as fx
from tests.test_drafting_support import fixture_document, insert_hv103, remove_check_valve

ROOT = Path(__file__).resolve().parents[1]
# The root's actual empty-start creation (sealed build, revision 3): the pump flow, then HX-101 added as a draft
# with all four ports open. A byte-for-byte copy of that build's snapshot (docs/FIXTURE_PROVENANCE.md).
ROOT_R3 = ROOT / "tests" / "fixtures" / "creation-r3.json"
ROOT_R3_SHA256 = "96b5730778bd4d16019d116d4e74e937a100a6b189298c483682553ab8dc5228"
GENERIC = "PROCESS SCHEMATIC"
SAMPLE = "TK-101 TO OUT-101 VIA P-101"


def legacy_subtitle(project, graph):
    """The wording before this correction, verbatim: entity-level sources, sinks and pumps of the whole walk."""
    def join(ids):
        tags = [project["model"]["entities"][e]["tag"] for e in ids]
        return tags[0] if len(tags) == 1 else ", ".join(tags[:-1]) + " AND " + tags[-1]

    stepped = [s["id"] for s in graph.steps if s["item_kind"] == "entity"]
    sources = [e for e in stepped if not graph.incoming[e]]
    sinks = [e for e in stepped if not graph.outgoing[e]]
    pumps = [e for e in stepped if project["model"]["entities"][e]["kind"] == "centrifugal_pump"]
    if not (sources and sinks):
        return GENERIC
    text = f"{join(sources)} TO {join(sinks)}"
    return f"{text} VIA {join(pumps)}" if pumps else text


def without_entity(project, eid):
    """``project`` without one entity, its ports, symbols and placement (read for the title only)."""
    p = copy.deepcopy(project)
    model, pid = p["model"], p["pid"]
    del model["entities"][eid]
    for port_id in [k for k, port in model["ports"].items() if port["entity_id"] == eid]:
        del model["ports"][port_id]
    for sid in [k for k, symbol in pid["symbols"].items() if symbol["entity_id"] == eid]:
        del pid["symbols"][sid]
    p["physical"]["placements"].pop(eid, None)
    return p


def pump_flow(extra=None):
    """Acceptance stages 1-3, the single flow TK-101 > HV-101 > HV-102 > P-101 > OUT-101, then ``extra`` added."""
    document = new_document("proj-title")
    batches = [fx.stage1_operations(), fx.stage2_operations(), fx.stage3_operations()] + ([extra] if extra else [])
    for number, operations in enumerate(batches, 1):
        document, _ = apply_command(document, fx.batch(document, f"cmd-title-{number}", operations))
    return document["current"]


def draft_exchanger():
    hx = fx.add_exchanger("ent-hx-101", "HX-101", (700.0, 120.0), "asm-hx-101", "asm-dn50-liquid")
    hx["assumptions"] = {"asm-hx-101": fx.assumption("Illustrative exchanger from the example definition defaults.")}
    return hx


def separate_exchanger():
    """Tube circuit TK-201 > P-201 > E-101 tube side > OUT-201 and shell circuit TK-202 > E-101 shell side > HV-202 >
    OUT-202, every port joined."""
    equipment, lines = fx.exchanger_operations()
    document = new_document("proj-title-hx")
    document, _ = apply_command(document, fx.batch(document, "cmd-title-hx-1", equipment))
    document, _ = apply_command(document, fx.batch(document, "cmd-title-hx-2", lines))
    return document["current"]


def series_exchanger(declared=True):
    """One flow through both sides of E-101 and no other item: TK-201 > P-201 > tube side > HV-202 > shell side >
    OUT-201. ``declared`` False strips the definition's flow paths (the legacy single-node reading)."""
    equipment, lines = fx.exchanger_operations()
    if not declared:
        del equipment[0]["payload"]["entity"]["definition"]["flow_paths"]
    kept = [op for op in equipment if op["payload"]["entity"]["id"] not in ("ent-tk-202", "ent-out-202")]
    hx, valve = "ent-e-101", "ent-hv-202"
    between = [
        fx.connect("conn-l-210", "L-210", fx.port_id(hx, "tube_outlet"), fx.port_id(valve, "inlet"), "sym-e-101",
                   "sym-hv-202", "asm-dn50-pipe"),
        fx.connect("conn-l-211", "L-211", fx.port_id(valve, "outlet"), fx.port_id(hx, "shell_inlet"), "sym-hv-202",
                   "sym-e-101", "asm-dn50-pipe"),
        fx.connect("conn-l-212", "L-212", fx.port_id(hx, "shell_outlet"), fx.port_id("ent-out-201", "inlet"),
                   "sym-e-101", "sym-out-201", "asm-dn50-pipe"),
    ]
    document = new_document("proj-title-series")
    document, _ = apply_command(document, fx.batch(document, "cmd-title-series-1", kept))
    document, _ = apply_command(document, fx.batch(document, "cmd-title-series-2", lines[:2] + between))
    return document["current"]


def separate_builtin_flows():
    """TK-101 > HV-101 (stage 1) beside TK-201 > P-201 > OUT-201: two flows of built-in items, none unconnected."""
    equipment, lines = fx.exchanger_operations(place=False)
    tank, pump, out = equipment[1:4]
    tank["assumptions"] = {"asm-items-hx": equipment[0]["assumptions"]["asm-items-hx"]}
    del lines[0]["assumptions"]  # asm-dn50-pipe already exists from stage 1
    discharge = fx.connect("conn-l-299", "L-299", fx.port_id("ent-p-201", "outlet"), fx.port_id("ent-out-201", "inlet"),
                           "sym-p-201", "sym-out-201", "asm-dn50-pipe")
    document = new_document("proj-title-flows")
    for number, operations in enumerate((fx.stage1_operations(), [tank, pump, out, lines[0], discharge]), 1):
        document, _ = apply_command(document, fx.batch(document, f"cmd-title-flows-{number}", operations))
    return document["current"]


class TitleCase(unittest.TestCase):
    def read_title(self, project):
        """(subtitle, legacy wording); reading changes neither project nor flow graph, nor depends on status."""
        before = copy.deepcopy(project)
        graph = FlowGraph(project)
        steps, groups = copy.deepcopy(graph.steps), copy.deepcopy(graph.flow_groups())
        sheet = PidSheet(project, graph)
        subtitles = {sheet.title_fields(status, 2, preview).subtitle
                     for status, preview in (("complete", None), ("pending", None), ("failed", None),
                                             ("pending", (1, 4)))}
        self.assertEqual(len(subtitles), 1)
        self.assertEqual(graph.steps, steps)
        self.assertEqual(graph.flow_groups(), groups)
        self.assertEqual(project, before)
        return subtitles.pop(), legacy_subtitle(project, graph)

    def built_titles(self, project):
        """The drawn title-block subtitle and the description title of a full drafting build."""
        with tempfile.TemporaryDirectory() as directory:
            build_drafting(copy.deepcopy(project), directory)
            pid = ET.parse(Path(directory) / "pid.svg").getroot()
            description = json.loads((Path(directory) / "description.json").read_text(encoding="utf-8"))
        drawn = [e.text for e in pid.iter() if e.get("data-role") == "drawing-subtitle"]
        self.assertEqual(len(drawn), 1)
        return drawn[0], description["title"]


class LegacyTitleTests(TitleCase):
    def test_sample_keeps_the_exact_legacy_wording(self):
        project = fixture_document()["current"]
        self.assertEqual(self.read_title(project), (SAMPLE, SAMPLE))
        self.assertEqual(self.built_titles(project), (SAMPLE, SAMPLE))

    def test_single_flow_edits_keep_the_legacy_wording(self):
        for project in (insert_hv103(fixture_document())["current"], remove_check_valve(fixture_document()["current"]),
                        pump_flow()):
            self.assertEqual(self.read_title(project), (SAMPLE, SAMPLE))

    def test_definition_without_flow_paths_keeps_the_legacy_wording(self):
        project = series_exchanger(declared=False)
        self.assertEqual(self.read_title(project), ("TK-201 TO OUT-201 VIA P-201", "TK-201 TO OUT-201 VIA P-201"))


class CreationTitleTests(TitleCase):
    def test_actual_root_r3_title_no_longer_puts_the_draft_exchanger_in_the_pump_flow(self):
        raw = ROOT_R3.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ROOT_R3_SHA256)
        project = json.loads(raw)["current"]
        self.assertEqual(project["revision"], 3)
        groups, unconnected = FlowGraph(project).flow_groups()
        self.assertEqual(unconnected, ["ent-hx-101"])  # all four HX-101 ports open, no line
        self.assertEqual(len(groups), 1)
        self.assertNotIn("ent-hx-101", groups[0]["entities"])
        subtitle, legacy = self.read_title(project)
        self.assertIn("HX-101", legacy)  # the old wording made HX-101 a start and an end of the P-101 flow
        self.assertIn("VIA P-101", legacy)
        self.assertEqual(subtitle, GENERIC)
        self.assertEqual(self.built_titles(project), (GENERIC, GENERIC))
        # The same flow without the draft exchanger reads exactly as before.
        self.assertEqual(self.read_title(without_entity(project, "ent-hx-101")), (SAMPLE, SAMPLE))

    def test_draft_exchanger_beside_the_pump_flow_is_generic(self):
        project = pump_flow([draft_exchanger()])
        subtitle, legacy = self.read_title(project)
        self.assertIn("HX-101", legacy)
        self.assertEqual(subtitle, GENERIC)
        self.assertEqual(self.built_titles(project), (GENERIC, GENERIC))

    def test_unconnected_builtin_draft_item_is_generic(self):
        tank = fx.add_builtin("ent-tk-102", "tank", "TK-102", {"diameter": 1.0, "height": 1.6, "outlet_height": 0.2},
                              (120.0, 480.0), "asm-tk-101", "asm-dn50-liquid")
        subtitle, legacy = self.read_title(pump_flow([tank]))
        self.assertIn("TK-102", legacy)
        self.assertEqual(subtitle, GENERIC)

    def test_fully_connected_separate_circuit_exchanger_is_generic(self):
        project = separate_exchanger()
        model = project["model"]
        joined = {c[end] for c in model["connections"].values() for end in ("from_port_id", "to_port_id")}
        self.assertEqual(joined, set(model["ports"]))  # every port is on a line: nothing is a draft
        groups, unconnected = FlowGraph(project).flow_groups()
        self.assertEqual((len(groups), unconnected), (2, []))
        subtitle, legacy = self.read_title(project)
        self.assertIn("VIA P-201", legacy)  # the old wording put P-201 on the shell circuit as well
        self.assertEqual(subtitle, GENERIC)
        self.assertEqual(self.built_titles(project), (GENERIC, GENERIC))

    def test_one_flow_through_both_declared_paths_is_generic(self):
        project = series_exchanger()
        groups, unconnected = FlowGraph(project).flow_groups()
        self.assertEqual((len(groups), unconnected), (1, []))
        self.assertEqual(self.read_title(project)[0], GENERIC)

    def test_separate_builtin_flows_are_generic(self):
        project = separate_builtin_flows()
        groups, unconnected = FlowGraph(project).flow_groups()
        self.assertEqual((len(groups), unconnected), (2, []))
        subtitle, legacy = self.read_title(project)
        self.assertIn("TK-101 AND TK-201", legacy)  # the old wording put P-201 in the TK-101 flow
        self.assertEqual(subtitle, GENERIC)

    def test_empty_project_is_generic(self):
        project = new_document("proj-title-empty")["current"]
        self.assertEqual(self.read_title(project), (GENERIC, GENERIC))
        self.assertEqual(self.built_titles(project), (GENERIC, GENERIC))


if __name__ == "__main__":
    unittest.main()
