"""LABELED FAKES and test topologies for the Ask tests. Nothing here calls Claude or any network service.

- ``graph_project``: a synthetic plain-dict project (not a loadable document)
  with a branch, a re-join, a loop, a bidirectional connection, a connection
  with a missing port and an instrument observation, for pure query tests.
- ``branched_document`` / ``cyclic_document``: VALID loadable documents built
  from the fixture: a composed three-port junction T-101 splitting the
  discharge into two lines, and a closed loop HV-101 -> P-101 -> NRV-101 ->
  HV-102 -> HV-101 without tank or boundary. ``validate_document`` reports
  warnings only (missing routes, edges, placements), no errors.
- ``answer`` / ``statement``: schema-shaped FAKE model answers.
- ``AskPlanner``: an in-process planner with the bridge's interface that
  answers Ask schemas and engineering-plan schemas from separate gates, so a
  model Ask and an engineering plan can run at the same time.

A pass with these fakes is not evidence that the real model answers well; the
GPT orchestrator runs the real Claude Max acceptance separately.

This module defines no test cases.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from examples.make_fixture import make_document
from skid.core import load_document, save_document
from skid.core.validation import validate_document
from skid.planning.bridge import EFFORT, MODEL, PlannerError
from tests.test_equipment import composed_entity, load_definition


def _entity(eid, tag, kind="test_node", **extra):
    return dict({"id": eid, "tag": tag, "kind": kind, "template_version": 1, "parameters": {}}, **extra)


def _port(pid, eid, name, direction):
    return {"id": pid, "entity_id": eid, "name": name, "direction": direction, "required": True,
            "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-test"}


def graph_project():
    """S -> T; T branches to B and C; B and C re-join at M; M -> R -> T closes a loop; M <-> U is bidirectional;
    a connection from a missing port ends at U; I observes T. Tags are deliberately not ordered by flow."""
    entities = {eid: _entity(eid, tag) for eid, tag in (
        ("ent-s", "SRC-9"), ("ent-t", "J-1"), ("ent-b", "ZB-7"), ("ent-c", "AC-3"), ("ent-m", "MX-2"),
        ("ent-r", "R-55"), ("ent-u", "U-0"))}
    entities["ent-i"] = _entity("ent-i", "LT-1", kind="indicator", observes={"entity_id": "ent-t",
                                                                               "variable": "level"})
    ports = {}
    for pid, eid, name, direction in (
            ("p-s-out", "ent-s", "outlet", "out"), ("p-t-in", "ent-t", "inlet", "in"),
            ("p-t-back", "ent-t", "return", "in"), ("p-t-a", "ent-t", "outlet_a", "out"),
            ("p-t-b", "ent-t", "outlet_b", "out"), ("p-b-in", "ent-b", "inlet", "in"),
            ("p-b-out", "ent-b", "outlet", "out"), ("p-c-in", "ent-c", "inlet", "in"),
            ("p-c-out", "ent-c", "outlet", "out"), ("p-m-in1", "ent-m", "inlet_1", "in"),
            ("p-m-in2", "ent-m", "inlet_2", "in"), ("p-m-out", "ent-m", "outlet", "out"),
            ("p-m-aux", "ent-m", "aux", "both"), ("p-r-in", "ent-r", "inlet", "in"),
            ("p-r-out", "ent-r", "outlet", "out"), ("p-u-u", "ent-u", "u", "both"),
            ("p-u-v", "ent-u", "v", "in")):
        ports[pid] = _port(pid, eid, name, direction)
    connections = {}
    for cid, tag, source, target in (
            ("c-1", "L-1", "p-s-out", "p-t-in"), ("c-2", "L-2", "p-t-a", "p-b-in"), ("c-3", "L-3", "p-t-b", "p-c-in"),
            ("c-4", "L-4", "p-b-out", "p-m-in1"), ("c-5", "L-5", "p-c-out", "p-m-in2"),
            ("c-6", "L-6", "p-m-out", "p-r-in"), ("c-7", "L-7", "p-r-out", "p-t-back"),
            ("c-8", "L-8", "p-m-aux", "p-u-u"), ("c-9", "L-9", "p-missing", "p-u-v")):
        connections[cid] = {"id": cid, "tag": tag, "from_port_id": source, "to_port_id": target,
                            "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603, "assumption_id": "asm-test"}
    return {"id": "project-test-graph", "revision": 7,
            "model": {"entities": entities, "ports": ports, "connections": connections,
                      "assumptions": {"asm-test": {"description": "FAKE test topology.", "source": "test"}}},
            "pid": {"symbols": {}, "edges": {}}, "physical": {"placements": {}, "routes": {}}}


def chain_project(length):
    """A linear synthetic chain N0 -> N1 -> ... of ``length`` entities (for traversal bounds)."""
    entities, ports, connections = {}, {}, {}
    for index in range(length):
        eid = f"ent-n{index}"
        entities[eid] = _entity(eid, f"N-{index}")
        ports[f"p-n{index}-in"] = _port(f"p-n{index}-in", eid, "inlet", "in")
        ports[f"p-n{index}-out"] = _port(f"p-n{index}-out", eid, "outlet", "out")
        if index:
            cid = f"c-n{index}"
            connections[cid] = {"id": cid, "tag": f"L-{index}", "from_port_id": f"p-n{index - 1}-out",
                                "to_port_id": f"p-n{index}-in", "nominal_diameter_mm": 50,
                                "outside_diameter_m": 0.0603, "assumption_id": "asm-test"}
    return {"id": "project-test-chain", "revision": 1,
            "model": {"entities": entities, "ports": ports, "connections": connections, "assumptions": {}},
            "pid": {"symbols": {}, "edges": {}}, "physical": {"placements": {}, "routes": {}}}


def fan_project(width):
    """One synthetic hub H with ``width`` outlet branches, each ending at its own sink."""
    entities = {"ent-h": _entity("ent-h", "H-1")}
    ports, connections = {}, {}
    for index in range(width):
        sink = f"ent-k{index}"
        entities[sink] = _entity(sink, f"K-{index}")
        ports[f"p-h-{index}"] = _port(f"p-h-{index}", "ent-h", f"outlet_{index}", "out")
        ports[f"p-k{index}"] = _port(f"p-k{index}", sink, "inlet", "in")
        connections[f"c-k{index}"] = {"id": f"c-k{index}", "tag": f"F-{index}", "from_port_id": f"p-h-{index}",
                                      "to_port_id": f"p-k{index}", "nominal_diameter_mm": 50,
                                      "outside_diameter_m": 0.0603, "assumption_id": "asm-test"}
    return {"id": "project-test-fan", "revision": 1,
            "model": {"entities": entities, "ports": ports, "connections": connections, "assumptions": {}},
            "pid": {"symbols": {}, "edges": {}}, "physical": {"placements": {}, "routes": {}}}


def hub_project(directed, undirected):
    """One synthetic hub H with ``directed`` outlet branches and ``undirected`` connections whose ports are both
    bidirectional (direction not established), each to its own item."""
    project = fan_project(directed)
    model = project["model"]
    for index in range(undirected):
        other = f"ent-v{index}"
        model["entities"][other] = _entity(other, f"V-{index}")
        model["ports"][f"p-h-aux{index}"] = _port(f"p-h-aux{index}", "ent-h", f"aux_{index}", "both")
        model["ports"][f"p-v{index}"] = _port(f"p-v{index}", other, "port", "both")
        model["connections"][f"c-v{index}"] = {"id": f"c-v{index}", "tag": f"B-{index}",
                                               "from_port_id": f"p-h-aux{index}", "to_port_id": f"p-v{index}",
                                               "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603,
                                               "assumption_id": "asm-test"}
    return project


def splitter_definition():
    """The example drum definition with a third (outlet_b) port and nozzle: an illustrative junction."""
    definition = copy.deepcopy(load_definition())
    definition.update(id="test-splitter", name="Test splitter drum",
                      function="Illustrative junction with one inlet and two outlets; duty unknown.")
    definition["ports"]["outlet_b"] = {"direction": "out", "required": True, "service": "liquid"}
    definition["symbol"]["anchors"]["outlet_b"] = [0, 40]
    geometry = definition["geometry"]
    port = copy.deepcopy(geometry["ports"]["outlet"])
    port.update(position=["=xR + Hd + 130", 0, "=Hc - 120"], component="nozzle/outlet_b")
    geometry["ports"]["outlet_b"] = port
    nozzle = copy.deepcopy(next(c for c in geometry["components"] if c["key"] == "nozzle/outlet"))
    nozzle["key"] = "nozzle/outlet_b"
    nozzle["parts"][0]["port"] = ["=xR + Hd + 130", 0, "=Hc - 120"]
    geometry["components"].append(nozzle)
    return definition


def branched_document():
    """P-101 -> L-103 -> T-101, which branches to L-201 -> NRV-101 -> ... -> OUT-101 and L-202 -> OUT-102."""
    document = make_document()
    project = document["current"]
    model = project["model"]
    model["entities"]["ent-split"] = composed_entity(splitter_definition(), "ent-split", "T-101", "asm-split")
    model["assumptions"]["asm-split"] = {"description": "FAKE test junction for branch questions.",
                                         "source": "Ask test scenario"}
    for name, direction in (("inlet", "in"), ("outlet", "out"), ("outlet_b", "out")):
        model["ports"][f"port-split-{name}"] = {"id": f"port-split-{name}", "entity_id": "ent-split", "name": name,
                                                 "direction": direction, "required": True, "nominal_diameter_mm": 50,
                                                 "service": "liquid", "assumption_id": "asm-split"}
    model["entities"]["ent-out-2"] = dict(copy.deepcopy(model["entities"]["ent-outlet-boundary"]), id="ent-out-2",
                                          tag="OUT-102")
    model["ports"]["port-out-2-inlet"] = dict(model["ports"]["port-boundary-inlet"], id="port-out-2-inlet",
                                              entity_id="ent-out-2")
    model["connections"]["conn-discharge-1"]["to_port_id"] = "port-split-inlet"
    base = dict(model["connections"]["conn-discharge-1"])
    model["connections"]["conn-split-a"] = dict(base, id="conn-split-a", tag="L-201",
                                                from_port_id="port-split-outlet", to_port_id="port-check-valve-inlet")
    model["connections"]["conn-split-b"] = dict(base, id="conn-split-b", tag="L-202",
                                                from_port_id="port-split-outlet_b", to_port_id="port-out-2-inlet")
    del project["pid"]["edges"]["edge-discharge-1"]
    del project["physical"]["routes"]["conn-discharge-1"]
    _require_valid(document)
    return document


def cyclic_document():
    """HV-101 -> L-102 -> P-101 -> L-103 -> NRV-101 -> L-104 -> HV-102 -> L-199 -> HV-101 (a closed loop)."""
    document = make_document()
    project = document["current"]
    model = project["model"]
    for eid in ("ent-tank", "ent-level-indicator", "ent-outlet-boundary"):
        del model["entities"][eid]
        project["physical"]["placements"].pop(eid, None)
        for sid in [s for s, symbol in project["pid"]["symbols"].items() if symbol["entity_id"] == eid]:
            del project["pid"]["symbols"][sid]
    for port_id in ("port-tank-outlet", "port-boundary-inlet"):
        del model["ports"][port_id]
    for cid in ("conn-suction-1", "conn-discharge-3"):
        del model["connections"][cid]
        project["physical"]["routes"].pop(cid, None)
        for edge_id in [e for e, edge in project["pid"]["edges"].items() if edge["connection_id"] == cid]:
            del project["pid"]["edges"][edge_id]
    model["connections"]["conn-return"] = {
        "id": "conn-return", "tag": "L-199", "from_port_id": "port-discharge-valve-outlet",
        "to_port_id": "port-suction-valve-inlet", "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603,
        "assumption_id": "asm-dn50-pipe"}
    _require_valid(document)
    return document


def _require_valid(document):
    errors = [d for d in validate_document(document) if d["severity"] == "error"]
    if errors:
        raise AssertionError(f"test document is invalid: {errors[:3]}")


def write_document(test, document, name="ask project.json"):
    """Save ``document`` as a fresh project in a private temporary directory with spaces in its path."""
    import tempfile

    tmp = tempfile.TemporaryDirectory(prefix="skid ask ")
    test.addCleanup(tmp.cleanup)
    directory = Path(tmp.name) / "ask project"
    directory.mkdir()
    path = directory / name
    save_document(str(path), document, None)
    load_document(str(path))
    return path


def statement(kind, text, *ids):
    return {"kind": kind, "text": text, "reference_ids": list(ids)}


def answer(outcome="answer", summary="FAKE summary of the answer.", statements=None, unknowns=(),
           clarification_question=""):
    """A schema-shaped FAKE model answer."""
    if statements is None:
        statements = [statement("recorded_fact", "FAKE: P-101 is recorded as a centrifugal pump.", "ent-pump"),
                      statement("graph_conclusion", "FAKE: L-102 feeds P-101 from HV-101.", "conn-suction-2",
                                "ent-suction-valve", "ent-pump"),
                      statement("general_explanation", "FAKE: centrifugal pumps add head to a liquid."),
                      statement("unknown", "FAKE: the duty point is not recorded.")]
    return {"outcome": outcome, "summary": summary, "statements": list(statements), "unknowns": list(unknowns),
            "clarification_question": clarification_question}


def is_ask_schema(schema):
    return isinstance(schema, dict) and "statements" in schema.get("properties", {})


class AskPlanner:
    """LABELED FAKE planner: Ask schemas get ``answer`` (or ``ask_error``) after ``ask_gate``; engineering-plan
    schemas get ``plan`` after ``plan_gate``. Both honour cancellation while they wait."""

    kind = "FAKE"

    def __init__(self, answer_value=None, ask_error=None, plan=None, ask_gated=False, plan_gated=False,
                 metadata=None):
        self.answer = answer() if answer_value is None else answer_value
        self.ask_error = ask_error
        self.plan = plan
        self.ask_gate, self.plan_gate = threading.Event(), threading.Event()
        if not ask_gated:
            self.ask_gate.set()
        if not plan_gated:
            self.plan_gate.set()
        self.ask_entered, self.plan_entered = threading.Event(), threading.Event()
        self.calls = []
        self.cancelled = []
        self.shutdowns = 0
        self.metadata = dict(metadata or {"kind": "FAKE", "model": MODEL, "effort": {"requested": EFFORT,
                                                                                     "reported": None},
                                          "verified": True, "fake": True, "auth": {"method": "claude.ai",
                                                                                    "subscription": "max"}})

    def describe(self):
        return {"kind": "FAKE", "model": MODEL, "effort": EFFORT, "cli": None, "cli_present": False}

    def asks(self):
        return [c for c in self.calls if c["ask"]]

    def run(self, prompt, schema, cancel=None):
        ask = is_ask_schema(schema)
        self.calls.append({"prompt": prompt, "schema": schema, "ask": ask})
        gate, entered = (self.ask_gate, self.ask_entered) if ask else (self.plan_gate, self.plan_entered)
        entered.set()
        while not gate.wait(0.01):
            if cancel is not None and cancel.is_set():
                self.cancelled.append("ask" if ask else "plan")
                raise PlannerError("planner_cancelled", "FAKE planner cancelled", {"fake": True})
        if cancel is not None and cancel.is_set():
            self.cancelled.append("ask" if ask else "plan")
            raise PlannerError("planner_cancelled", "FAKE planner cancelled", {"fake": True})
        if ask:
            if self.ask_error is not None:
                raise self.ask_error
            return {"plan": json.loads(json.dumps(self.answer)), "metadata": dict(self.metadata)}
        return {"plan": json.loads(json.dumps(self.plan)), "metadata": dict(self.metadata)}

    def shutdown(self):
        self.shutdowns += 1
