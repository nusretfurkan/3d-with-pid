"""Ask respects declared flow paths: separate circuits through one item are never joined, and relations follow each
declared pair from its inlet to its outlet only (docs/ASK.md).

    .venv-cad/bin/python -m unittest tests.test_assistant_flow_paths -v

Valid documents are built through the real command layer from the foundation's synthetic exchanger fixture
(``tests/empty_start_support.py``, used read-only): E-101 declares ``tube_inlet -> tube_outlet`` and
``shell_inlet -> shell_outlet``. Malformed variants are plain-dict edits of those documents (inspectable data,
not loadable files). Nothing here calls a model.
"""

from __future__ import annotations

import copy
import json
import unittest

from skid.assistant import context as ask_context
from skid.assistant.context import MARKER
from skid.assistant.query import DIRECTION_NOTE, ProjectQuery
from skid.core import apply_command, new_document
from skid.interactive.service import InteractiveService
from tests import empty_start_support as fx
from tests.test_assistant import DtoMixin, ask, fixture, local
from tests.test_assistant_fakes import AskPlanner, answer, splitter_definition, statement, write_document
from tests.test_interactive_fakes import FAKE_SUITE

HX = "ent-e-101"
TUBE_IN, TUBE_OUT = fx.port_id(HX, "tube_inlet"), fx.port_id(HX, "tube_outlet")
SHELL_IN, SHELL_OUT = fx.port_id(HX, "shell_inlet"), fx.port_id(HX, "shell_outlet")


def _build(project_id, equipment, lines):
    """The whole document after one equipment batch and one line batch through ``apply_command``."""
    document = new_document(project_id)
    document, _ = apply_command(document, fx.batch(document, "cmd-equipment", equipment))
    if lines:
        lines[0]["assumptions"] = {"asm-dn50-pipe": fx.assumption("DN50 line with an assumed 60.3 mm outside "
                                                                  "diameter.")}
        document, _ = apply_command(document, fx.batch(document, "cmd-lines", lines))
    return document


def _equipment(*tags, flow_paths="declared"):
    """The fixture's exchanger plus the named items (by tag); ``flow_paths`` None strips the declaration."""
    (hx, *items), _ = fx.exchanger_operations()
    definition = hx["payload"]["entity"]["definition"]
    if flow_paths is None:
        del definition["flow_paths"]
    elif flow_paths != "declared":
        definition["flow_paths"] = flow_paths
    return [hx] + [item for item in items if item["payload"]["entity"]["tag"] in tags]


def _line(cid, tag, source, target):
    """``connect_ports`` from ``(entity id, port name)`` to ``(entity id, port name)``."""
    return fx.connect(cid, tag, fx.port_id(*source), fx.port_id(*target), f"sym-{source[0][4:]}",
                      f"sym-{target[0][4:]}", "asm-dn50-pipe")


def exchanger_document(flow_paths="declared"):
    """Tube circuit TK-201 -> P-201 -> E-101 tube side -> OUT-201; shell circuit TK-202 -> E-101 shell side ->
    HV-202 -> OUT-202 (the foundation scenario, all six lines)."""
    equipment = _equipment("TK-201", "P-201", "OUT-201", "TK-202", "HV-202", "OUT-202", flow_paths=flow_paths)
    return _build("proj-ask-hx", equipment, fx.exchanger_operations()[1])


def exchanger(flow_paths="declared"):
    return exchanger_document(flow_paths)["current"]


def cross_connected(link=True):
    """TK-201 -> P-201 -> E-101 tube side -> L-303 -> HV-202 -> L-304 -> E-101 shell side -> L-305 -> OUT-202.
    The explicit line L-304 is the only thing that joins the circuits; ``link=False`` omits it."""
    equipment = _equipment("TK-201", "P-201", "HV-202", "OUT-202")
    lines = [_line("conn-l-201", "L-201", ("ent-tk-201", "outlet"), ("ent-p-201", "inlet")),
             _line("conn-l-202", "L-202", ("ent-p-201", "outlet"), (HX, "tube_inlet")),
             _line("conn-l-303", "L-303", (HX, "tube_outlet"), ("ent-hv-202", "inlet")),
             _line("conn-l-304", "L-304", ("ent-hv-202", "outlet"), (HX, "shell_inlet")),
             _line("conn-l-305", "L-305", (HX, "shell_outlet"), ("ent-out-202", "inlet"))]
    if not link:
        del lines[3]
    return _build("proj-ask-hx-cross", equipment, lines)["current"]


def tube_loop():
    """E-101 tube outlet -> L-401 -> HV-202 -> L-402 -> E-101 tube inlet: a genuine loop through one declared path;
    the shell side is left unconnected (marked unresolved)."""
    lines = [_line("conn-l-401", "L-401", (HX, "tube_outlet"), ("ent-hv-202", "inlet")),
             _line("conn-l-402", "L-402", ("ent-hv-202", "outlet"), (HX, "tube_inlet"))]
    return _build("proj-ask-hx-loop", _equipment("HV-202"), lines)["current"]


def same_item_line(target):
    """One line from E-101's tube outlet straight to another E-101 port (``tube_inlet``: a recirculation within the
    tube path; ``shell_inlet``: into the shell path). HV-202 is present but unconnected."""
    lines = [_line("conn-l-601", "L-601", (HX, "tube_outlet"), (HX, target))]
    return _build("proj-ask-hx-same-item", _equipment("HV-202"), lines)["current"]


def both_ports_reversed():
    """E-101's tube ports are bidirectional (``both``) and wired against the declared pair order:
    TK-201 -> P-201 -> L-502 -> tube_outlet; tube_inlet -> L-503 -> OUT-201."""
    equipment = _equipment("TK-201", "P-201", "OUT-201")
    payload = equipment[0]["payload"]
    for port in payload["ports"]:
        if port["name"] in ("tube_inlet", "tube_outlet"):
            port["direction"] = payload["entity"]["definition"]["ports"][port["name"]]["direction"] = "both"
    lines = [_line("conn-l-201", "L-201", ("ent-tk-201", "outlet"), ("ent-p-201", "inlet")),
             _line("conn-l-502", "L-502", ("ent-p-201", "outlet"), (HX, "tube_outlet")),
             _line("conn-l-503", "L-503", (HX, "tube_inlet"), ("ent-out-201", "inlet"))]
    return _build("proj-ask-hx-both", equipment, lines)["current"]


def split_feed(flow_paths="declared"):
    """TK-201 -> L-201 -> T-301, a LABELED test junction that feeds both sides of E-101: L-311 to the tube inlet
    (-> L-203 -> OUT-201) and L-312 to the shell inlet (-> L-205 -> HV-202 -> L-206 -> OUT-202)."""
    equipment = _equipment("TK-201", "OUT-201", "HV-202", "OUT-202", flow_paths=flow_paths)
    definition = splitter_definition()
    junction = {"type": "add_equipment", "payload": {
        "entity": {"id": "ent-t-301", "kind": "composed_equipment", "tag": "T-301", "template_version": 1,
                   "definition": definition,
                   "parameters": {name: {"value": record["value"], "unit": "m", "assumption_id": "asm-items-hx"}
                                  for name, record in definition["geometry"]["parameters"].items()}},
        "ports": fx.ports_for("ent-t-301", {"inlet": "in", "outlet": "out", "outlet_b": "out"}, "asm-dn50-liquid"),
        "symbol": {"id": "sym-t-301", "entity_id": "ent-t-301", "x": 300.0, "y": 420.0, "rotation_deg": 0.0,
                   "manual": False}}}
    lines = [_line("conn-l-201", "L-201", ("ent-tk-201", "outlet"), ("ent-t-301", "inlet")),
             _line("conn-l-311", "L-311", ("ent-t-301", "outlet"), (HX, "tube_inlet")),
             _line("conn-l-312", "L-312", ("ent-t-301", "outlet_b"), (HX, "shell_inlet")),
             _line("conn-l-203", "L-203", (HX, "tube_outlet"), ("ent-out-201", "inlet")),
             _line("conn-l-205", "L-205", (HX, "shell_outlet"), ("ent-hv-202", "inlet")),
             _line("conn-l-206", "L-206", ("ent-hv-202", "outlet"), ("ent-out-202", "inlet"))]
    return _build("proj-ask-hx-split", equipment + [junction], lines)["current"]


def malformed(variant):
    """Inspectable (not loadable) variants of the exchanger project."""
    project = copy.deepcopy(exchanger())
    model = project["model"]
    entity = model["entities"][HX]
    if variant == "unknown_kind":  # no template at all
        entity["kind"] = "mystery_exchanger"
        del entity["definition"]
    elif variant == "invalid_paths":  # declared paths that do not validate (an unknown port)
        entity["definition"]["flow_paths"] = [["tube_inlet", "vent"], ["shell_inlet", "shell_outlet"]]
    elif variant == "garbage_paths":
        entity["definition"]["flow_paths"] = {"tube_inlet": 7}
    elif variant == "broken_definition":  # a definition that cannot resolve at all, with valid-looking paths
        del entity["definition"]["geometry"]
    elif variant == "renamed_port":  # a port record outside every declared path (an auxiliary port)
        model["ports"][SHELL_IN]["name"] = "vent"
    elif variant == "orphan_port":  # a port whose owner is not a recorded entity
        model["ports"]["port-ghost"] = dict(model["ports"][TUBE_IN], id="port-ghost", entity_id="ent-ghost")
    else:
        raise ValueError(variant)
    return project


TUBE = "E-101 (flow path tube_inlet to tube_outlet)"
SHELL = "E-101 (flow path shell_inlet to shell_outlet)"
END_DOWN = "[end: no further outgoing process connection]"
END_UP = "[end: no further incoming process connection]"
NO_PATH = "No directed process path connects"


def trace_text(project, record_id, direction):
    return "\n".join(ProjectQuery(project).trace_lines(record_id, direction)[0])


def inside(side):
    """The relation path from a declared inlet to its own outlet through E-101 alone."""
    return (f"E-101.{side}_inlet → E-101.{side}_outlet inside E-101, along its declared flow path {side}_inlet to "
            f"{side}_outlet with no external line (0 connection(s)).")


class CircuitRelationTests(DtoMixin, unittest.TestCase):
    def test_a_circuit_never_reaches_the_other_circuits_items(self):
        project = exchanger()
        query = ProjectQuery(project)
        for first, second in (("TK-201", "OUT-202"), ("TK-202", "OUT-201"), ("P-201", "HV-202"), ("L-202", "L-205"),
                              ("L-204", "L-203"), ("E-101.tube_outlet", "E-101.shell_inlet"),
                              ("E-101.shell_outlet", "E-101.tube_inlet"), ("E-101.tube_inlet", "OUT-202"),
                              ("L-201", "E-101.shell_outlet")):
            for question in (f"Is {first} upstream of {second}?", f"Is {second} downstream of {first}?",
                             f"Is {second} upstream of {first}?", f"What is between {first} and {second}?"):
                with self.subTest(question=question):
                    response = self.assertDto(local(project, question))
                    self.assertEqual(response["responder"]["query"], "relation")
                    self.assertTrue(response["answer"].startswith(f"{NO_PATH} "), response["answer"])
                    self.assertIn("Declared flow paths were followed one at a time: E-101 (tube_inlet to tube_outlet; "
                                  "shell_inlet to shell_outlet).", response["answer"])
            ids = [query.mentions(tag).targets[0] for tag in (first, second)]
            with self.subTest(pair=(first, second)):
                self.assertEqual(query.path_between(*ids)[0], None)
                self.assertEqual(query.path_between(*reversed(ids))[0], None)
        self.assertEqual(local(project, "Is TK-201 upstream of OUT-202?")["answer"].splitlines()[0],
                         f"{NO_PATH} TK-201 and OUT-202 in either direction in the current model (searched 3 "
                         "connection(s)).")

    def test_same_side_paths_are_found_upstream_and_downstream(self):
        project = exchanger()
        tube = f"TK-201 → L-201 → P-201 → L-202 → {TUBE} → L-203 → OUT-201 (3 connection(s))."
        shell = f"TK-202 → L-204 → {SHELL} → L-205 → HV-202 → L-206 → OUT-202 (3 connection(s))."
        cases = {"Is TK-201 upstream of OUT-201?": f"Yes. TK-201 is upstream of OUT-201: following directed process "
                                                   f"connections downstream from TK-201 reaches OUT-201: {tube}",
                 "Is OUT-201 downstream of TK-201?": f"Yes. OUT-201 is downstream of TK-201: following directed "
                                                     f"process connections downstream from TK-201 reaches OUT-201: "
                                                     f"{tube}",
                 "Is OUT-201 upstream of TK-201?": f"No. OUT-201 is downstream of TK-201, not upstream: following "
                                                   f"directed process connections downstream from TK-201 reaches "
                                                   f"OUT-201: {tube}",
                 "Is TK-202 upstream of OUT-202?": f"Yes. TK-202 is upstream of OUT-202: following directed process "
                                                   f"connections downstream from TK-202 reaches OUT-202: {shell}",
                 "Is OUT-202 downstream of TK-202?": f"Yes. OUT-202 is downstream of TK-202: following directed "
                                                     f"process connections downstream from TK-202 reaches OUT-202: "
                                                     f"{shell}",
                 "What is between TK-202 and OUT-202?": f"A directed process path runs from TK-202 to OUT-202: {shell}",
                 "Is E-101.shell_inlet upstream of OUT-202?": f"Yes. E-101.shell_inlet is upstream of OUT-202: "
                                                              f"following directed process connections downstream "
                                                              f"from E-101.shell_inlet reaches OUT-202: "
                                                              f"E-101.shell_inlet → L-205 → HV-202 → L-206 → OUT-202 "
                                                              "(2 connection(s)).",
                 "Is E-101 upstream of OUT-202?": f"Yes. E-101 is upstream of OUT-202: following directed process "
                                                  f"connections downstream from E-101 reaches OUT-202: {SHELL} → "
                                                  "L-205 → HV-202 → L-206 → OUT-202 (2 connection(s)).",
                 "Is TK-201 upstream of E-101?": f"Yes. TK-201 is upstream of E-101: following directed process "
                                                 f"connections downstream from TK-201 reaches E-101: TK-201 → L-201 → "
                                                 f"P-201 → L-202 → {TUBE} (2 connection(s)).",
                 }
        for question, first_line in cases.items():
            with self.subTest(question=question):
                response = self.assertDto(local(project, question))
                self.assertEqual(response["answer"].splitlines()[0], first_line)
        lines = local(project, "Is L-204 upstream of L-205?")["answer"]  # line to line, through the shell path
        self.assertTrue(lines.startswith("Yes. L-204 is upstream of L-205"), lines)
        self.assertIn(f"{SHELL} → L-205 → HV-202 (2 connection(s)).", lines)
        response = local(project, "Is TK-201 upstream of OUT-201?")
        self.assertEqual(self.ids(response), ["ent-tk-201", "ent-out-201", "conn-l-201", "ent-p-201", "conn-l-202",
                                              "ent-e-101", "conn-l-203"])
        query = ProjectQuery(project)  # the public path format is unchanged: (connection, entity) pairs
        self.assertEqual(query.path_between("ent-tk-201", "ent-out-201"),
                         ([("conn-l-201", "ent-p-201"), ("conn-l-202", "ent-e-101"), ("conn-l-203", "ent-out-201")],
                          3, False))

    def test_negative_control_without_declared_paths_keeps_the_entity_level_reading(self):
        project = exchanger(flow_paths=None)
        self.assertEqual(ProjectQuery(project).flow_paths, {})
        text = local(project, "Is TK-201 upstream of OUT-202?")["answer"]
        self.assertTrue(text.startswith("Yes. TK-201 is upstream of OUT-202: following directed process connections "
                                        "downstream from TK-201 reaches OUT-202: TK-201 → L-201 → P-201 → L-202 → "
                                        "E-101 → L-205 → HV-202 → L-206 → OUT-202 (4 connection(s))."), text)
        self.assertNotIn("Declared flow paths", text)


class CircuitTraceTests(DtoMixin, unittest.TestCase):
    def test_traces_from_incoming_lines_stay_in_their_circuit(self):
        project = exchanger()
        cases = {("conn-l-202", "downstream"): f"L-202 → {TUBE} → L-203 → OUT-201 {END_DOWN}",
                 ("conn-l-204", "downstream"): f"L-204 → {SHELL} → L-205 → HV-202 → L-206 → OUT-202 {END_DOWN}",
                 ("conn-l-205", "upstream"): f"L-205 ← {SHELL} ← L-204 ← TK-202 {END_UP}",
                 ("conn-l-203", "upstream"): f"L-203 ← {TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 {END_UP}"}
        for (cid, direction), expected in cases.items():
            with self.subTest(cid=cid, direction=direction):
                lines, result = ProjectQuery(project).trace_lines(cid, direction)
                self.assertEqual(lines[1:], [expected])
                self.assertEqual(result["start"], HX)
                self.assertIn("flow_path", result)
        _, result = ProjectQuery(project).trace_lines("conn-l-202", "downstream")
        self.assertEqual(result["flow_path"], ["tube_inlet", "tube_outlet"])
        self.assertEqual(result["branches"], [{"connection": "conn-l-203", "entity": "ent-out-201", "end": "end",
                                               "next": []}])
        self.assertEqual((result["edges"], result["truncated"]), (1, False))
        text = local(project, "trace downstream of L-202")["answer"]
        self.assertIn("Flow path: it enters E-101 through its declared flow path tube_inlet to tube_outlet (at "
                      "E-101.tube_inlet); traversal continues only within it.", text)
        for other in ("L-204", "L-205", "L-206", "HV-202", "OUT-202", "TK-202"):
            self.assertNotIn(other, text)
        self.assertEqual(local(project, "trace downstream of edge-l-202")["answer"], text)  # P&ID edge ID mention

    def test_traces_from_exact_port_ids_stay_in_their_circuit(self):
        project = exchanger()
        cases = {(TUBE_IN, "downstream"): f"{TUBE} → L-203 → OUT-201 {END_DOWN}",
                 (TUBE_OUT, "downstream"): f"{TUBE} → L-203 → OUT-201 {END_DOWN}",
                 (TUBE_IN, "upstream"): f"{TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 {END_UP}",
                 (TUBE_OUT, "upstream"): f"{TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 {END_UP}",
                 (SHELL_IN, "downstream"): f"{SHELL} → L-205 → HV-202 → L-206 → OUT-202 {END_DOWN}",
                 (SHELL_OUT, "upstream"): f"{SHELL} ← L-204 ← TK-202 {END_UP}"}
        for (port, direction), expected in cases.items():
            with self.subTest(port=port, direction=direction):
                self.assertEqual(ProjectQuery(project).trace_lines(port, direction)[0][1:], [expected])
        response = self.assertDto(local(project, "trace downstream of it", [TUBE_IN]))  # a selected port ID
        self.assertIn(f"Downstream path from E-101.tube_inlet (bounded expansion, every branch listed):\n{TUBE} → "
                      f"L-203 → OUT-201 {END_DOWN}", response["answer"])
        self.assertIn("E-101.tube_inlet is in the declared flow path tube_inlet to tube_outlet of E-101; through E-101 "
                      "it is linked only with E-101.tube_outlet, never with another declared path.", response["answer"])
        self.assertNotIn("L-205", response["answer"])
        ids = self.ids(response)
        self.assertEqual(ids[0], TUBE_IN)
        self.assertTrue({HX, "conn-l-203", "ent-out-201"} <= set(ids), ids)
        self.assertFalse({"conn-l-204", "conn-l-205", "ent-hv-202"} & set(ids), ids)

    def test_entity_level_port_traces_keep_their_own_connection_rule(self):
        # Ports of items without declared flow paths follow only their own connections first, exactly as before.
        query = ProjectQuery(fixture())
        self.assertEqual(query.trace_lines("port-pump-inlet", "downstream")[0][1:], [f"P-101 {END_DOWN}"])
        self.assertEqual(query.trace_lines("port-pump-outlet", "upstream")[0][1:], [f"P-101 {END_UP}"])
        self.assertNotIn("flow_path", query.trace("port-pump-inlet", "upstream"))

    def test_entity_start_identifies_each_circuit(self):
        project = exchanger()
        header = ("E-101 declares 2 separate flow paths; each is traced on its own, and none continues into another "
                  "through E-101 itself (only a recorded connection can join them):")
        lines, result = ProjectQuery(project).trace_lines(HX, "downstream")
        self.assertEqual(lines[1:], [header, f"{TUBE} → L-203 → OUT-201 {END_DOWN}",
                                     f"{SHELL} → L-205 → HV-202 → L-206 → OUT-202 {END_DOWN}"])
        self.assertEqual(result["circuits"], 2)
        self.assertEqual([(b["connection"], b["entity"], b.get("flow_path")) for b in result["branches"]],
                         [(None, HX, ["tube_inlet", "tube_outlet"]), (None, HX, ["shell_inlet", "shell_outlet"])])
        self.assertEqual(result["edges"], 3)
        lines, _ = ProjectQuery(project).trace_lines(HX, "upstream")
        self.assertEqual(lines[1:], [header, f"{TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 {END_UP}",
                                     f"{SHELL} ← L-204 ← TK-202 {END_UP}"])
        response = self.assertDto(local(project, "trace upstream and downstream of E-101"))
        self.assertEqual(response["responder"]["query"], "expanded_connectivity")
        self.assertNotIn("branches", response["answer"])

    def test_sources_and_sinks_trace_only_their_own_circuit(self):
        project = exchanger()
        cases = {("ent-tk-201", "downstream"): f"TK-201 → L-201 → P-201 → L-202 → {TUBE} → L-203 → OUT-201 {END_DOWN}",
                 ("ent-tk-202", "downstream"): f"TK-202 → L-204 → {SHELL} → L-205 → HV-202 → L-206 → OUT-202 "
                                               f"{END_DOWN}",
                 ("ent-out-201", "upstream"): f"OUT-201 ← L-203 ← {TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 {END_UP}",
                 ("ent-out-202", "upstream"): f"OUT-202 ← L-206 ← HV-202 ← L-205 ← {SHELL} ← L-204 ← TK-202 {END_UP}"}
        for (eid, direction), expected in cases.items():
            with self.subTest(eid=eid, direction=direction):
                self.assertEqual(ProjectQuery(project).trace_lines(eid, direction)[0][1:], [expected])


class SharedEntityTests(DtoMixin, unittest.TestCase):
    """Two independent runs through one exchanger are neither re-joins nor loops."""

    def test_a_junction_feeding_both_sides_lists_two_runs_without_a_join(self):
        project = split_feed()
        self.assertEqual(ProjectQuery(project).trace_lines("ent-tk-201", "downstream")[0][1:], [
            "TK-201 → L-201 → T-301, which has 2 branches:",
            f"  branch 1: T-301 → L-311 → {TUBE} → L-203 → OUT-201 {END_DOWN}",
            f"  branch 2: T-301 → L-312 → {SHELL} → L-205 → HV-202 → L-206 → OUT-202 {END_DOWN}"])
        self.assertEqual(ProjectQuery(project).trace_lines("ent-out-202", "upstream")[0][1:], [
            f"OUT-202 ← L-206 ← HV-202 ← L-205 ← {SHELL} ← L-312 ← T-301 ← L-201 ← TK-201 {END_UP}"])
        self.assertEqual(ProjectQuery(project).trace_lines("ent-out-201", "upstream")[0][1:], [
            f"OUT-201 ← L-203 ← {TUBE} ← L-311 ← T-301 ← L-201 ← TK-201 {END_UP}"])
        self.assertTrue(local(project, "Is TK-201 upstream of OUT-202?")["answer"].startswith("Yes."))
        self.assertTrue(local(project, "Is L-311 upstream of OUT-202?")["answer"].startswith(NO_PATH))
        self.assertTrue(local(project, "Is L-312 upstream of OUT-201?")["answer"].startswith(NO_PATH))
        legacy = trace_text(split_feed(flow_paths=None), "ent-tk-201", "downstream")  # the reading paths replace
        self.assertIn("branch 2: T-301 → L-312 → E-101 [joins E-101, listed above; not repeated]", legacy)

    def test_an_explicit_line_between_the_circuits_is_followed_and_is_no_loop(self):
        project = cross_connected()
        through = (f"{TUBE} → L-303 → HV-202 → L-304 → {SHELL} → L-305 → OUT-202")
        self.assertEqual(ProjectQuery(project).trace_lines("ent-tk-201", "downstream")[0][1:], [
            f"TK-201 → L-201 → P-201 → L-202 → {through} {END_DOWN}"])
        self.assertEqual(ProjectQuery(project).trace_lines(HX, "downstream")[0][2:], [
            f"{through} {END_DOWN}",
            f"{SHELL} [reached above through a recorded connection; listed there, not repeated]"])
        self.assertEqual(ProjectQuery(project).trace_lines("ent-out-202", "upstream")[0][1:], [
            f"OUT-202 ← L-305 ← {SHELL} ← L-304 ← HV-202 ← L-303 ← {TUBE} ← L-202 ← P-201 ← L-201 ← TK-201 "
            f"{END_UP}"])
        self.assertEqual(local(project, "Is TK-201 upstream of OUT-202?")["answer"].splitlines()[0],
                         "Yes. TK-201 is upstream of OUT-202: following directed process connections downstream from "
                         f"TK-201 reaches OUT-202: TK-201 → L-201 → P-201 → L-202 → {through} (5 connection(s)).")
        self.assertTrue(local(project, "Is E-101.tube_outlet upstream of E-101.shell_inlet?")["answer"].startswith(
            "Yes. E-101.tube_outlet is upstream of E-101.shell_inlet"))
        self.assertNotIn("loop", trace_text(project, "ent-p-201", "downstream"))

    def test_without_that_line_the_circuits_stay_separate(self):
        project = cross_connected(link=False)
        for question in ("Is TK-201 upstream of OUT-202?", "Is E-101.tube_outlet upstream of E-101.shell_inlet?",
                         "Is HV-202 upstream of OUT-202?", "Is L-202 upstream of L-305?"):
            with self.subTest(question=question):
                self.assertTrue(local(project, question)["answer"].startswith(NO_PATH))
        self.assertEqual(ProjectQuery(project).trace_lines("ent-tk-201", "downstream")[0][1:], [
            f"TK-201 → L-201 → P-201 → L-202 → {TUBE} → L-303 → HV-202 {END_DOWN}"])
        self.assertEqual(ProjectQuery(project).trace_lines("ent-out-202", "upstream")[0][1:], [
            f"OUT-202 ← L-305 ← {SHELL} {END_UP}"])

    def test_a_genuine_loop_through_one_declared_path_is_still_a_loop(self):
        project = tube_loop()
        self.assertEqual(ProjectQuery(project).trace_lines("ent-hv-202", "downstream")[0][1:], [
            f"HV-202 → L-402 → {TUBE} → L-401 → HV-202 [loop: returns to HV-202 on this path; traversal stops]"])
        self.assertEqual(ProjectQuery(project).trace_lines(HX, "downstream")[0][2:], [
            f"{TUBE} → L-401 → HV-202 → L-402 → {TUBE} [loop: returns to {TUBE} on this path; traversal stops]",
            f"{SHELL} {END_DOWN}"])
        self.assertTrue(local(project, "Is HV-202 upstream of E-101.tube_outlet?")["answer"].startswith("Yes."))
        self.assertTrue(local(project, "Is HV-202 upstream of E-101.shell_outlet?")["answer"].startswith(NO_PATH))


class PortOrderTests(DtoMixin, unittest.TestCase):
    """A declared ``[inlet, outlet]`` pair is directional in relations: inlet to outlet through the item alone, and
    outlet back to inlet only through recorded connections."""

    def test_an_outlet_is_not_upstream_of_its_own_inlet_without_a_return_line(self):
        project = exchanger()
        query = ProjectQuery(project)
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN), (None, 1, False))  # L-203 leads only to OUT-201
        self.assertEqual(query.path_between(SHELL_OUT, SHELL_IN), (None, 2, False))  # L-205, L-206 to OUT-202
        for side in ("tube", "shell"):
            inlet, outlet, internal = f"E-101.{side}_inlet", f"E-101.{side}_outlet", inside(side)
            cases = {f"Is {outlet} upstream of {inlet}?": f"No. {outlet} is downstream of {inlet}, not upstream: "
                                                          f"following directed process connections downstream from "
                                                          f"{inlet} reaches {outlet}: {internal}",
                     f"Is {inlet} downstream of {outlet}?": f"No. {inlet} is upstream of {outlet}, not downstream: "
                                                            f"following directed process connections downstream from "
                                                            f"{inlet} reaches {outlet}: {internal}",
                     f"What is between {outlet} and {inlet}?": f"A directed process path runs from {inlet} to "
                                                               f"{outlet}: {internal}"}
            for question, first_line in cases.items():
                with self.subTest(question=question):
                    response = self.assertDto(local(project, question))
                    self.assertEqual(response["responder"]["query"], "relation")
                    self.assertEqual(response["answer"].splitlines()[0], first_line)
                    self.assertNotIn(f"{outlet} is upstream of {inlet}", response["answer"])
                    self.assertNotIn(f"path runs from {outlet} to {inlet}", response["answer"])

    def test_an_inlet_reaches_its_own_outlet_through_the_item_alone(self):
        project = exchanger()
        query = ProjectQuery(project)
        for inlet, outlet, side in ((TUBE_IN, TUBE_OUT, "tube"), (SHELL_IN, SHELL_OUT, "shell")):
            with self.subTest(side=side):
                self.assertEqual(query.path_between(inlet, outlet), ([], 0, False))
                self.assertEqual(local(project, f"Is E-101.{side}_inlet upstream of E-101.{side}_outlet?")["answer"]
                                 .splitlines()[0],
                                 f"Yes. E-101.{side}_inlet is upstream of E-101.{side}_outlet: following directed "
                                 f"process connections downstream from E-101.{side}_inlet reaches E-101.{side}_outlet: "
                                 f"{inside(side)}")
                self.assertEqual(local(project, f"Is E-101.{side}_outlet downstream of E-101.{side}_inlet?")["answer"]
                                 .splitlines()[0],
                                 f"Yes. E-101.{side}_outlet is downstream of E-101.{side}_inlet: following directed "
                                 f"process connections downstream from E-101.{side}_inlet reaches "
                                 f"E-101.{side}_outlet: {inside(side)}")
        response = self.assertDto(local(project, "Is E-101.tube_inlet upstream of E-101.tube_outlet?"))
        self.assertEqual(self.ids(response), [TUBE_IN, TUBE_OUT])
        self.assertIn("flow path: E-101.tube_inlet is in the declared flow path tube_inlet to tube_outlet of E-101; "
                      "through E-101 it is linked only with E-101.tube_outlet, never with another declared path.",
                      response["references"][0]["facts"])
        self.assertIn("Declared flow paths were followed one at a time: E-101 (tube_inlet to tube_outlet; "
                      "shell_inlet to shell_outlet).", response["answer"])
        self.assertTrue(response["answer"].endswith(DIRECTION_NOTE), response["answer"])

    def test_a_return_loop_reaches_the_inlet_only_through_its_external_lines(self):
        project = tube_loop()
        query = ProjectQuery(project)
        loop = [("conn-l-401", "ent-hv-202"), ("conn-l-402", HX)]
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN), (loop, 2, False))
        self.assertEqual(query.path_between(TUBE_IN, TUBE_OUT), ([], 0, False))  # the shorter internal route
        through = f"E-101.tube_outlet → L-401 → HV-202 → L-402 → {TUBE} (2 connection(s))."
        cases = {"Is E-101.tube_outlet upstream of E-101.tube_inlet?": "Yes. E-101.tube_outlet is upstream of "
                                                                        "E-101.tube_inlet: following directed process "
                                                                        "connections downstream from E-101.tube_outlet "
                                                                        f"reaches E-101.tube_inlet: {through}",
                 "Is E-101.tube_inlet downstream of E-101.tube_outlet?": "Yes. E-101.tube_inlet is downstream of "
                                                                          "E-101.tube_outlet: following directed "
                                                                          "process connections downstream from "
                                                                          "E-101.tube_outlet reaches E-101.tube_inlet: "
                                                                          f"{through}",
                 "What is between E-101.tube_outlet and E-101.tube_inlet?": "A directed process path runs from "
                                                                             "E-101.tube_outlet to E-101.tube_inlet: "
                                                                             f"{through}"}
        for question, first_line in cases.items():
            with self.subTest(question=question):
                response = self.assertDto(local(project, question))
                self.assertEqual(response["answer"].splitlines()[0], first_line)
                self.assertTrue({"conn-l-401", "ent-hv-202", "conn-l-402"} <= set(self.ids(response)))
        response = local(project, "Is E-101.tube_outlet upstream of E-101.tube_inlet?")
        self.assertEqual(self.ids(response), [TUBE_OUT, TUBE_IN, "conn-l-401", "ent-hv-202", "conn-l-402", HX])
        # Line anchors on the same path: arriving at the inlet may leave from the outlet, never the reverse.
        self.assertEqual(query.path_between("conn-l-402", "conn-l-401"),
                         ([("conn-l-402", HX), ("conn-l-401", "ent-hv-202")], 0, False))
        self.assertEqual(query.path_between("conn-l-402", TUBE_OUT), ([("conn-l-402", HX)], 0, False))
        self.assertEqual(query.path_between(TUBE_OUT, "conn-l-401"), ([("conn-l-401", "ent-hv-202")], 0, False))
        self.assertEqual(query.path_between(TUBE_OUT, "conn-l-402"), (loop, 1, False))
        self.assertEqual(query.path_between("conn-l-401", TUBE_IN), (loop, 1, False))
        # The loop is the tube path's own: the shell path still has no return, and the circuits stay apart.
        self.assertEqual(query.path_between(SHELL_OUT, SHELL_IN), (None, 0, False))
        for pair in ((TUBE_OUT, SHELL_IN), (TUBE_IN, SHELL_OUT), (SHELL_OUT, TUBE_IN)):
            with self.subTest(pair=pair):
                self.assertIsNone(query.path_between(*pair)[0])

    def test_the_return_search_keeps_its_bound(self):
        query = ProjectQuery(tube_loop())
        loop = [("conn-l-401", "ent-hv-202"), ("conn-l-402", HX)]
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN, 0), (None, 0, True))
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN, 1), (None, 1, True))
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN, 2), (loop, 2, False))
        self.assertEqual(query.path_between(TUBE_IN, TUBE_OUT, 0), ([], 0, False))  # the internal route needs none

    def test_a_line_between_ports_of_one_item_is_an_external_edge(self):
        project = same_item_line("tube_inlet")  # tube outlet -> L-601 -> tube inlet
        query = ProjectQuery(project)
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN), ([("conn-l-601", HX)], 1, False))
        self.assertEqual(query.path_between(TUBE_IN, TUBE_OUT), ([], 0, False))
        self.assertEqual(local(project, "Is E-101.tube_outlet upstream of E-101.tube_inlet?")["answer"].splitlines()[0],
                         "Yes. E-101.tube_outlet is upstream of E-101.tube_inlet: following directed process "
                         f"connections downstream from E-101.tube_outlet reaches E-101.tube_inlet: E-101.tube_outlet → "
                         f"L-601 → {TUBE} (1 connection(s)).")
        self.assertEqual(query.path_between(SHELL_OUT, SHELL_IN), (None, 0, False))
        project = same_item_line("shell_inlet")  # tube outlet -> L-601 -> shell inlet
        query = ProjectQuery(project)
        self.assertEqual(query.path_between(TUBE_OUT, SHELL_IN), ([("conn-l-601", HX)], 1, False))
        self.assertEqual(query.path_between(TUBE_IN, SHELL_OUT), ([("conn-l-601", HX)], 1, False))
        for pair in ((SHELL_IN, TUBE_OUT), (SHELL_OUT, TUBE_IN), (TUBE_OUT, TUBE_IN), (SHELL_OUT, SHELL_IN)):
            with self.subTest(pair=pair):
                self.assertIsNone(query.path_between(*pair)[0])
        self.assertEqual(local(project, "Is E-101.shell_outlet upstream of E-101.tube_inlet?")["answer"].splitlines()[0],
                         "No. E-101.shell_outlet is downstream of E-101.tube_inlet, not upstream: following directed "
                         "process connections downstream from E-101.tube_inlet reaches E-101.shell_outlet: "
                         f"E-101.tube_inlet → L-601 → {SHELL} (1 connection(s)).")

    def test_an_explicit_cross_circuit_line_respects_both_pair_orders(self):
        query = ProjectQuery(cross_connected())  # tube outlet -> L-303 -> HV-202 -> L-304 -> shell inlet
        through = [("conn-l-303", "ent-hv-202"), ("conn-l-304", HX)]
        self.assertEqual(query.path_between(TUBE_OUT, SHELL_IN)[0], through)
        self.assertEqual(query.path_between(TUBE_IN, SHELL_OUT)[0], through)
        for pair in ((SHELL_IN, TUBE_OUT), (SHELL_OUT, TUBE_IN), (TUBE_OUT, TUBE_IN), (SHELL_OUT, SHELL_IN)):
            with self.subTest(pair=pair):
                self.assertIsNone(query.path_between(*pair)[0])
        self.assertEqual(query.relation_lines(SHELL_OUT, TUBE_IN, "upstream")[0][0],
                         "No. E-101.shell_outlet is downstream of E-101.tube_inlet, not upstream: following directed "
                         "process connections downstream from E-101.tube_inlet reaches E-101.shell_outlet: "
                         f"E-101.tube_inlet → L-303 → HV-202 → L-304 → {SHELL} (2 connection(s)).")
        self.assertIsNone(ProjectQuery(cross_connected(link=False)).path_between(TUBE_OUT, SHELL_IN)[0])

    def test_bidirectional_ports_follow_the_declared_pair_order(self):
        project = both_ports_reversed()  # P-201 -> L-502 -> tube_outlet (both); tube_inlet (both) -> L-503 -> OUT-201
        query = ProjectQuery(project)
        self.assertEqual(query.flow_paths[HX][0], ["tube_inlet", "tube_outlet"])
        self.assertEqual({project["model"]["ports"][p]["direction"] for p in (TUBE_IN, TUBE_OUT)}, {"both"})
        self.assertEqual(query.path_between("ent-p-201", "ent-out-201"), (None, 1, False))
        self.assertTrue(local(project, "Is P-201 upstream of OUT-201?")["answer"].startswith(
            f"{NO_PATH} P-201 and OUT-201 in either direction"))
        self.assertEqual(query.path_between(TUBE_OUT, TUBE_IN), (None, 0, False))
        self.assertEqual(query.path_between("conn-l-502", "conn-l-503"), (None, 0, False))
        self.assertEqual(query.path_between("conn-l-502", TUBE_IN), (None, 0, False))
        self.assertEqual(query.path_between(TUBE_OUT, "conn-l-503"), (None, 0, False))
        self.assertEqual(query.path_between(TUBE_IN, TUBE_OUT), ([], 0, False))
        self.assertEqual(query.path_between("conn-l-502", TUBE_OUT), ([("conn-l-502", HX)], 0, False))
        self.assertEqual(query.path_between(TUBE_IN, "conn-l-503"), ([("conn-l-503", "ent-out-201")], 0, False))
        # The item itself still touches both lines (entity anchors span the whole item).
        self.assertEqual(query.path_between("ent-p-201", HX), ([("conn-l-502", HX)], 1, False))
        self.assertEqual(query.path_between(HX, "ent-out-201"), ([("conn-l-503", "ent-out-201")], 1, False))

    def test_entity_self_and_legacy_queries_keep_their_reading(self):
        project = exchanger()
        query = ProjectQuery(project)
        for pair in ((HX, TUBE_IN), (TUBE_OUT, HX), (TUBE_OUT, TUBE_OUT), (HX, HX)):
            with self.subTest(pair=pair):
                self.assertEqual(query.path_between(*pair), ([], 0, False))
        self.assertEqual(local(project, "Is E-101 upstream of E-101.tube_inlet?")["answer"].splitlines()[0],
                         "Yes. E-101 is upstream of E-101.tube_inlet: following directed process connections "
                         f"downstream from E-101 reaches E-101.tube_inlet: {TUBE} (0 connection(s)).")
        self.assertEqual(local(project, "Is E-101.tube_outlet upstream of E-101?")["answer"].splitlines()[0],
                         "Yes. E-101.tube_outlet is upstream of E-101: following directed process connections "
                         "downstream from E-101.tube_outlet reaches E-101: E-101.tube_outlet (0 connection(s)).")
        legacy = exchanger(flow_paths=None)  # one entity-level node: every port pair is the same item, as before
        self.assertEqual(ProjectQuery(legacy).path_between(TUBE_OUT, TUBE_IN), ([], 0, False))
        text = local(legacy, "Is E-101.tube_outlet upstream of E-101.tube_inlet?")["answer"]
        self.assertEqual(text.splitlines()[0], "Yes. E-101.tube_outlet is upstream of E-101.tube_inlet: following "
                                               "directed process connections downstream from E-101.tube_outlet "
                                               "reaches E-101.tube_inlet: E-101.tube_outlet (0 connection(s)).")
        self.assertNotIn("inside E-101", text)
        self.assertNotIn("Declared flow paths", text)


class FactsAndContextTests(DtoMixin, unittest.TestCase):
    SUMMARY = ("Declared flow paths of E-101: tube_inlet to tube_outlet; shell_inlet to shell_outlet. They are separate "
               "circuits through E-101: flow entering one path continues only from that path's ports, never into "
               "another path through the item itself; only a recorded connection can join them.")

    def test_identity_references_and_immediate_connectivity_name_the_circuits(self):
        project = exchanger()
        identity = self.assertDto(local(project, "What is E-101?"))
        self.assertIn(self.SUMMARY, identity["answer"].splitlines())
        reference = next(r for r in identity["references"] if r["id"] == HX)
        self.assertEqual(reference["facts"][3], self.SUMMARY)
        self.assertIn("model.entities[ent-e-101].definition.flow_paths", reference["field_paths"])
        near = self.assertDto(local(project, "What is upstream and downstream of E-101?"))["answer"]
        self.assertIn("Upstream (incoming), 2 lines: L-202 from P-201.outlet into E-101.tube_inlet (flow path "
                      "tube_inlet to tube_outlet); L-204 from TK-202.outlet into E-101.shell_inlet (flow path "
                      "shell_inlet to shell_outlet).", near)
        self.assertIn("Downstream (outgoing), 2 lines: L-203 from E-101.tube_outlet to OUT-201.inlet (flow path "
                      "tube_inlet to tube_outlet); L-205 from E-101.shell_outlet to HV-202.inlet (flow path "
                      "shell_inlet to shell_outlet).", near)
        self.assertIn(self.SUMMARY, near)
        self.assertNotIn("branches", near)
        query = ProjectQuery(project)
        self.assertIn("flow path: E-101.shell_outlet is in the declared flow path shell_inlet to shell_outlet of "
                      "E-101; through E-101 it is linked only with E-101.shell_inlet, never with another declared "
                      "path.", query.reference(SHELL_OUT)["facts"])
        self.assertIn("flow path: it enters E-101 through its declared flow path tube_inlet to tube_outlet (at "
                      "E-101.tube_inlet)", query.reference("conn-l-202")["facts"])
        self.assertIn("flow path: it leaves E-101 through its declared flow path shell_inlet to shell_outlet (at "
                      "E-101.shell_outlet)", query.reference("conn-l-205")["facts"])
        self.assertEqual([f for f in query.reference("conn-l-201")["facts"] if f.startswith("flow path")], [])
        line = local(project, "What is L-205?")["answer"]
        self.assertIn("Flow path: it leaves E-101 through its declared flow path shell_inlet to shell_outlet (at "
                      "E-101.shell_outlet).", line)

    def test_model_context_carries_the_declared_paths(self):
        project = exchanger()
        prepared = ask(project, "Why does E-101 keep the tube and shell circuits apart?")
        self.assertIsNotNone(prepared.job)
        context = json.loads(prepared.job.prompt.split(MARKER, 1)[1])
        self.assertEqual(context["conventions"].get("flow_paths"), ask_context.FLOW_PATHS_CONVENTION)
        row = next(e for e in context["entities"] if e["id"] == HX)
        self.assertEqual(row["flow_paths"], [["tube_inlet", "tube_outlet"], ["shell_inlet", "shell_outlet"]])
        self.assertNotIn("ports_outside_flow_paths", row)
        self.assertEqual(set(row["definition"]), {"id", "name", "function", "unknowns"})
        lines = {c["id"]: c for c in context["connections"]}
        self.assertEqual(lines["conn-l-202"]["to"]["flow_path"], ["tube_inlet", "tube_outlet"])
        self.assertEqual(lines["conn-l-205"]["from"]["flow_path"], ["shell_inlet", "shell_outlet"])
        self.assertNotIn("flow_path", lines["conn-l-202"]["from"])
        self.assertNotIn("flow_paths", next(e for e in context["entities"] if e["id"] == "ent-p-201"))
        evidence = context["evidence"][0]
        self.assertEqual(evidence["id"], HX)
        self.assertIn(self.SUMMARY, evidence["facts"])
        self.assertIn(f"{TUBE} → L-203 → OUT-201 {END_DOWN}", evidence["traces"]["downstream"])
        self.assertIn(f"{SHELL} ← L-204 ← TK-202 {END_UP}", evidence["traces"]["upstream"])
        self.assertIn("not hydraulic or heat-transfer results", ask_context.FLOW_PATHS_CONVENTION)

    def test_projects_without_declared_paths_get_no_flow_path_context(self):
        for project in (fixture(), exchanger(flow_paths=None)):
            prepared = ask(project, "Why is P-101 there?" if "ent-pump" in project["model"]["entities"] else
                           "Why is E-101 there?")
            context = json.loads(prepared.job.prompt.split(MARKER, 1)[1])
            self.assertNotIn("flow_paths", context["conventions"])
            self.assertFalse(any("flow_paths" in e or "flow_paths_unverified" in e for e in context["entities"]))
            self.assertFalse(any("flow_path" in c["from"] or "flow_path" in c["to"] for c in context["connections"]))


class MalformedTests(DtoMixin, unittest.TestCase):
    VARIANTS = ("unknown_kind", "invalid_paths", "garbage_paths", "broken_definition", "renamed_port", "orphan_port")

    def test_every_query_survives_malformed_inspectable_data(self):
        for variant in self.VARIANTS:
            with self.subTest(variant=variant):
                project = malformed(variant)
                before = json.dumps(project, sort_keys=True)
                query = ProjectQuery(project)
                for record_id in query.semantic_ids():
                    query.reference(record_id)
                    query.identity_lines(record_id)
                    query.connectivity_lines(record_id)
                    query.unknowns_for(record_id)
                    for direction in ("upstream", "downstream"):
                        query.trace_lines(record_id, direction)
                    query.relation_lines(record_id, "ent-out-202", "upstream")
                for question in ("trace upstream and downstream of E-101", "What is E-101?",
                                 "Is TK-201 upstream of OUT-201?", "What is upstream and downstream of L-202?"):
                    self.assertDto(local(project, question))
                prepared = ask(project, "Why is E-101 there?")
                json.loads(prepared.job.prompt.split(MARKER, 1)[1])
                self.assertEqual(json.dumps(project, sort_keys=True), before)

    def test_unvalidated_declarations_pass_no_flow(self):
        for variant in ("invalid_paths", "garbage_paths", "broken_definition"):
            with self.subTest(variant=variant):
                project = malformed(variant)
                query = ProjectQuery(project)
                self.assertEqual((query.flow_paths, query.unverified), ({}, {HX}))
                text = local(project, "Is TK-201 upstream of OUT-201?")["answer"]
                self.assertTrue(text.startswith(NO_PATH), text)
                self.assertIn("No path was followed through E-101: its declared flow paths could not be validated.",
                              text)
                self.assertEqual(query.trace_lines("ent-tk-201", "downstream")[0][1:], [
                    f"TK-201 → L-201 → P-201 → L-202 → E-101 (port tube_inlet; flow paths not validated) {END_DOWN}"])
                self.assertIn("E-101's definition declares flow paths that could not be validated, so no flow is "
                              "passed through E-101: each of its ports is treated on its own.",
                              local(project, "What is E-101?")["answer"])
                context = json.loads(ask(project, "Why is E-101 there?").job.prompt.split(MARKER, 1)[1])
                row = next(e for e in context["entities"] if e["id"] == HX)
                self.assertEqual((row.get("flow_paths_unverified"), "flow_paths" in row), (True, False))
                self.assertIn("flow_paths", context["conventions"])

    def test_a_port_outside_every_declared_path_is_no_bridge(self):
        project = malformed("renamed_port")  # the shell inlet record is now an auxiliary port "vent"
        self.assertTrue(local(project, "Is TK-202 upstream of OUT-202?")["answer"].startswith(NO_PATH))
        self.assertEqual(ProjectQuery(project).trace_lines("ent-tk-202", "downstream")[0][1:], [
            f"TK-202 → L-204 → E-101 (port vent, outside its declared flow paths) {END_DOWN}"])
        upstream = ProjectQuery(project).trace_lines(HX, "upstream")[0]
        self.assertIn("E-101 declares 2 separate flow paths and has 1 port outside them with a line in this "
                      "direction; each is traced on its own, and none continues into another through E-101 itself "
                      "(only a recorded connection can join them):", upstream)
        self.assertIn(f"{SHELL} {END_UP}", upstream)
        self.assertIn(f"E-101 (port vent, outside its declared flow paths) ← L-204 ← TK-202 {END_UP}", upstream)
        self.assertNotIn("vent", "\n".join(ProjectQuery(project).trace_lines(HX, "downstream")[0]))
        line = local(project, "What is upstream and downstream of L-204?")["answer"]
        self.assertIn("Flow path: it enters E-101 at E-101.vent, outside every declared flow path, so no flow is "
                      "passed through the item from there.", line)
        self.assertNotIn("traversal continues", line)
        unverified = local(malformed("invalid_paths"), "What is upstream and downstream of L-202?")["answer"]
        self.assertIn("Flow path: it enters E-101 at E-101.tube_inlet; the declared flow paths of E-101 could not be "
                      "validated, so no flow is passed through it.", unverified)
        self.assertNotIn("traversal continues", unverified)
        context = json.loads(ask(project, "Why is E-101 there?").job.prompt.split(MARKER, 1)[1])
        self.assertEqual(next(e for e in context["entities"] if e["id"] == HX)["ports_outside_flow_paths"], ["vent"])
        self.assertTrue(next(c for c in context["connections"] if c["id"] == "conn-l-204")["to"]["outside_flow_paths"])

    def test_unresolvable_templates_and_orphan_ports(self):
        unknown = malformed("unknown_kind")  # nothing is declared, so the entity-level reading applies
        query = ProjectQuery(unknown)
        self.assertEqual((query.flow_paths, query.unverified), ({}, set()))
        self.assertIn("kind mystery_exchanger, template version 1; its template could not be resolved",
                      query.reference(HX)["facts"])
        orphan = ProjectQuery(malformed("orphan_port"))  # before: KeyError in trace
        self.assertEqual(orphan.trace_lines("port-ghost", "downstream")[0][1:], [f"ent-ghost {END_DOWN}"])
        self.assertEqual(orphan.path_between("port-ghost", "ent-out-201"), (None, 0, False))


class ServiceTests(unittest.TestCase):
    def test_service_answers_circuit_questions_from_the_persisted_document(self):
        path = write_document(self, exchanger_document(), "hx project.json")
        planner = AskPlanner(answer_value=answer(statements=[
            statement("graph_conclusion", "FAKE: E-101 declares separate tube and shell flow paths.", HX)]))
        service = InteractiveService(str(path), suite=FAKE_SUITE, suite_options={}, timeout=60, terminate_grace=0.5,
                                     planner=planner)
        self.addCleanup(service.close)
        with service._lock:  # white-box read without state(), as the Ask service tests do
            revision, digest = service.document["current"]["revision"], service.digest

        def body(request_id, question):
            return {"request_id": request_id, "question": question, "selected_ids": [], "expected_revision": revision,
                    "input_digest": digest}

        relation = service.ask(body("req-hx-1", "Is TK-201 upstream of OUT-202?"))
        self.assertEqual((relation["status"], relation["responder"]["query"], relation["responder"]["model_called"]),
                         ("answered", "relation", False))
        self.assertTrue(relation["answer"].startswith(f"{NO_PATH} TK-201 and OUT-202"), relation["answer"])
        traced = service.ask(body("req-hx-2", "trace downstream of L-202"))["answer"]
        self.assertIn(f"L-202 → {TUBE} → L-203 → OUT-201 {END_DOWN}", traced)
        self.assertEqual(planner.asks(), [])
        broad = service.ask(body("req-hx-3", "Why are the E-101 circuits kept apart?"))
        self.assertEqual((broad["status"], broad["responder"]["language_model"]), ("answered", True))
        self.assertEqual(len(planner.asks()), 1)
        context = json.loads(planner.asks()[0]["prompt"].split(MARKER, 1)[1])
        self.assertEqual((context["project"]["revision"], context["project"]["input_digest"]), (revision, digest))
        self.assertEqual(next(e for e in context["entities"] if e["id"] == HX)["flow_paths"],
                         [["tube_inlet", "tube_outlet"], ["shell_inlet", "shell_outlet"]])
        self.assertIn("flow_paths", context["conventions"])


if __name__ == "__main__":
    unittest.main()
