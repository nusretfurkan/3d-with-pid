"""LABELED FAKES for empty-project creation and equipment research. Nothing here calls Claude, the CLI or any
network service, and nothing here is evidence that a real planner or researcher produced anything.

- ``tank_and_valve_plan`` / ``pump_train_plan`` / ``exchanger_plan``: schema-shaped FAKE engineering plans using
  the C-owned ``add_equipment`` / ``connect_ports`` arguments (docs/CREATION_AND_RESEARCH_UI.md). Their values
  are illustrative test inputs written by hand.
- ``fake_brief``: a FAKE ``skid.equipment.research/1`` brief with invented example sources; it passes the
  research package's exported validator, but its URLs were never observed in any search.
- ``FakeResearcher``: an in-process object with the researcher interface (``describe``, ``run``,
  ``shutdown``), returning a given reply or raising a given error, optionally blocking until released, and
  recording exactly what it was sent.
- ``blank_document`` / ``new_blank_project``: a genuinely empty document from ``skid.core.new_document``.

This module defines no test cases.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
from pathlib import Path

from skid.core import new_document, save_document
from skid.planning.bridge import MODEL
from skid.research import EFFORT
from tests.test_planning_fakes import op, plan

RESEARCH_LABEL = "FAKE researcher (no model, no web search)"
TEXT_WARNING = ("Research used text web-search results only; no images, drawings or CAD files were inspected, and any "
                "dimensions are illustrative assumptions, not source data.")


def blank_document(project_id="fake-blank-project"):
    return new_document(project_id)


def new_blank_project(test, name="blank project.json", project_id="fake-blank-project"):
    """A genuinely empty project saved in a temporary directory with spaces in its path."""
    tmp = tempfile.TemporaryDirectory(prefix="skid creation ")
    test.addCleanup(tmp.cleanup)
    directory = Path(tmp.name) / "my blank project"
    directory.mkdir()
    path = directory / name
    save_document(str(path), blank_document(project_id), None)
    return path


def q(name, value, assumption, unit="m"):
    return {"name": name, "value": value, "unit": unit, "assumption": assumption}


def placed(position, assumption="layout", rotation=(0.0, 0.0, 0.0)):
    return {"status": "placed", "position_m": list(position), "rotation_deg": list(rotation), "assumption": assumption}


UNPLACED = {"status": "unresolved", "position_m": [], "rotation_deg": [], "assumption": ""}


def add(entity_id, tag, kind, parameters, x, y, placement, port_assumption="dn50", definition_ref="",
        definition_json=""):
    return op("add_equipment", entity_id=entity_id, tag=tag, kind=kind, definition_ref=definition_ref,
              definition_json=definition_json, parameters=parameters, port_assumption=port_assumption,
              symbol={"x": float(x), "y": float(y), "rotation_deg": 0.0}, placement=placement)


def connect(connection_id, tag, from_port, to_port, from_symbol, to_symbol, mode="guided", points=(),
            assumption="pipe", route_assumption=""):
    return op("connect_ports", connection_id=connection_id, tag=tag, from_port_id=from_port, to_port_id=to_port,
              from_symbol_id=from_symbol, to_symbol_id=to_symbol, outside_diameter_mm=60.3, assumption=assumption,
              route={"mode": mode, "points_m": [list(p) for p in points], "assumption": route_assumption})


BASE_ASSUMPTIONS = [
    ("dims", "FAKE illustrative prototype dimensions chosen for the test."),
    ("dn50", "FAKE: DN50 liquid service ports as declared by the templates."),
    ("layout", "FAKE illustrative physical layout chosen for the test."),
    ("pipe", "FAKE: DN50 pipe, 60.3 mm outside diameter."),
]


def tank_and_valve_plan(valve_placement=None, route_mode="guided"):
    """FAKE plan 1: tank TK-101 and its outlet isolation valve HV-101, joined by L-101 (valve outlet stays open)."""
    return plan(
        operations=[
            add("ent-tk101", "TK-101", "tank", [q("diameter", 0.8, "dims"), q("height", 1.2, "dims"),
                                                q("outlet_height", 0.2, "dims")], 120, 280, placed((0, 0, 0))),
            add("ent-hv101", "HV-101", "isolation_valve", [q("length", 160, "dims", "mm"),
                                                           q("body_diameter", 0.1, "dims")],
                260, 300, valve_placement or placed((1.0, 0.0, 0.2))),
            connect("conn-l101", "L-101", "port-tk101-outlet", "port-hv101-inlet", "sym-tk101", "sym-hv101",
                    mode=route_mode),
        ],
        assumptions=BASE_ASSUMPTIONS,
        summary="FAKE: add TK-101 with outlet valve HV-101 on line L-101.",
        unknowns=["FAKE: duty, fluid and ratings are unknown."],
        impacts=[{"ids": ["ent-tk101", "ent-hv101"], "effect": "FAKE: new tank and valve"}])


def pump_train_plan():
    """FAKE plan 2 (after plan 1): pump P-101 with its own suction valve HV-102 and a terminal boundary OUT-101."""
    return plan(
        operations=[
            add("ent-hv102", "HV-102", "isolation_valve", [q("length", 0.16, "dims"), q("body_diameter", 0.1, "dims")],
                380, 300, placed((1.6, 0.0, 0.2))),
            add("ent-p101", "P-101", "centrifugal_pump", [
                q("body_length", 0.45, "dims"), q("body_width", 0.3, "dims"), q("body_height", 0.4, "dims"),
                q("motor_length", 0.35, "dims"), q("motor_diameter", 0.22, "dims")], 520, 300, placed((2.6, 0.0, 0.0))),
            add("ent-out101", "OUT-101", "boundary", [q("size", 0.2, "dims")], 700, 220,
                placed((3.4, 0.0, 1.0)), port_assumption="dn50"),
            connect("conn-l102", "L-102", "port-hv101-outlet", "port-hv102-inlet", "sym-hv101", "sym-hv102"),
            connect("conn-l103", "L-103", "port-hv102-outlet", "port-p101-inlet", "sym-hv102", "sym-p101"),
            connect("conn-l104", "L-104", "port-p101-outlet", "port-out101-inlet", "sym-p101", "sym-out101"),
        ],
        assumptions=BASE_ASSUMPTIONS,
        summary="FAKE: add P-101 with suction valve HV-102 after HV-101 and a terminal boundary OUT-101.",
        impacts=[{"ids": ["ent-hv101"], "effect": "FAKE: its open outlet is connected"}])


HX_PARAMETERS = (("shell_length", 1.6), ("shell_diameter", 0.32), ("centreline_height", 0.62),
                 ("channel_length", 0.3), ("shell_nozzle_offset", 0.22))


def exchanger_plan(assumption_sources=(), placement=None):
    """FAKE: add the shell-and-tube example HX-101 with all four ports open (no connections)."""
    return plan(
        operations=[add("ent-hx101", "HX-101", "composed_equipment",
                        [q(n, v, "hx-dims") for n, v in HX_PARAMETERS], 400, 300, placement or UNPLACED,
                        definition_ref="example:shell_and_tube_exchanger")],
        assumptions=[("hx-dims", "FAKE illustrative exchanger dimensions (the example's defaults).",
                      tuple(assumption_sources)),
                     ("dn50", "FAKE: DN50 liquid service on all four nozzles."),
                     ("layout", "FAKE illustrative position beside the other equipment.")],
        summary="FAKE: add shell-and-tube exchanger HX-101 with separate shell and tube circuits.",
        unknowns=["FAKE: thermal duty unknown."])


def fake_brief(query, sources=None, features=None):
    """A FAKE research brief for ``query`` (invented example sources; passes the exported validator)."""
    sources = sources if sources is not None else [
        {"id": "S1", "url": "https://www.fake-exchanger-maker.com/products/shell-and-tube",
         "title": "FAKE shell-and-tube product overview",
         "observations": ["FAKE: one fluid flows through the tubes and the other around them inside the shell."]},
        {"id": "S2", "url": "https://engineering-notes.fake-reference.org/heat-exchangers/types",
         "title": "FAKE <b>exchanger</b> types & nozzles",
         "observations": ["FAKE: four nozzles; tube-side inlet/outlet on the channel, shell-side on the shell."]},
    ]
    features = features if features is not None else [
        {"text": "Two separate circuits: tube side and shell side.", "source_ids": ["S1"]},
        {"text": "Four named nozzles: tube inlet/outlet and shell inlet/outlet.", "source_ids": ["S1", "S2"]},
    ]
    return {"schema": "skid.equipment.research/1", "query": query, "equipment_type": "Shell-and-tube heat exchanger",
            "summary": "FAKE summary: a cylindrical shell with a tube bundle; two separate liquid circuits.",
            "sources": sources, "features": features,
            "assumptions": ["Dimensions are illustrative and not taken from any source."],
            "unknowns": ["Thermal duty, tube count and ratings were not established."],
            "warnings": [TEXT_WARNING]}


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fake_metadata(brief, **overrides):
    """FAKE metadata SHAPED like a verified research run (every field the research package's
    ``validate_research_result`` checks), so the integration's strict re-check can pass. It is labelled fake:
    nothing was searched and no model ran. ``observed_urls`` defaults to the brief's source URLs."""
    sources = [s for s in brief.get("sources", []) if isinstance(s, dict)] if isinstance(brief, dict) else []
    urls = [s.get("url") for s in sources]
    query = brief.get("query", "") if isinstance(brief, dict) else ""
    canonical = json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    metadata = {
        "fake": True, "label": RESEARCH_LABEL, "note": "FAKE metadata shaped like a verified run; nothing was searched",
        "verified": True, "model": MODEL, "effort": {"requested": EFFORT, "reported": None}, "init_model": MODEL,
        "response_models": [MODEL], "usage_models": [MODEL], "internal_retrieval": None,
        "auth": {"method": "claude.ai", "subscription": "max"}, "api_key_source": "none",
        "tools": ["StructuredOutput", "WebSearch"], "mcp_servers": [], "search_calls": 1, "successful_searches": 1,
        "failed_searches": 0, "searches": [{"query": "FAKE query (nothing searched)", "status": "ok",
                                            "links": max(1, len(urls))}],
        "structured_output_calls": 1, "observed_url_count": len(urls), "observed_urls": list(urls),
        "verified_sources": len(urls), "source_urls": list(urls), "request_sha256": _sha256_text(query),
        "prompt_sha256": "0" * 64, "output_sha256": "0" * 64,
        "brief_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "image_inspection": False, "session_persistence": False,
    }
    metadata.update(overrides)
    if "observed_urls" in overrides and isinstance(overrides["observed_urls"], list):
        metadata.setdefault("observed_url_count", len(overrides["observed_urls"]))
        metadata["observed_url_count"] = len(overrides["observed_urls"])
    return metadata


def fake_reply(query, **overrides):
    brief = fake_brief(query)
    return {"brief": brief, "metadata": fake_metadata(brief, **overrides)}


class FakeResearcher:
    """LABELED FAKE researcher with the ``skid.research`` interface; records what it is sent."""

    kind = "FAKE"

    def __init__(self, brief=None, error=None, gated=False, metadata=None, reply=None):
        self.brief = brief  # None: a valid fake brief for the received request text
        self.error = error
        self.reply = reply  # an exact (possibly malformed) reply object instead of {brief, metadata}
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.calls = []
        self.shutdowns = 0
        self.metadata = dict(metadata or {})  # overrides of fake_metadata

    def describe(self):
        return {"kind": "FAKE", "label": RESEARCH_LABEL, "model": "none (fake)", "effort": "n/a (fake)",
                "cli": None, "cli_present": False, "timeout_s": 30, "tools": [], "fake": True}

    def run(self, request, cancel=None):
        self.calls.append({"request": request, "type": type(request).__name__})
        self.entered.set()
        while not self.gate.wait(0.01):
            if cancel is not None and cancel.is_set():
                self.cancelled.set()
                raise _error("research_cancelled", "FAKE research cancelled")
        if cancel is not None and cancel.is_set():
            self.cancelled.set()
            raise _error("research_cancelled", "FAKE research cancelled")
        if self.error is not None:
            raise self.error
        if self.reply is not None:
            return copy.deepcopy(self.reply)
        brief = copy.deepcopy(self.brief) if self.brief is not None else fake_brief(request)
        return {"brief": brief, "metadata": fake_metadata(brief, **self.metadata)}

    def shutdown(self):
        self.shutdowns += 1


def _error(code, message):
    from skid.research import ResearchError
    return ResearchError(code, message, {"fake": True})
