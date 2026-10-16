"""Shared helpers for the drafting tests (no test cases here).

Scenario projects are built from copies of the persisted fixture, either
through the real command layer or as explicit structurally valid edits that
the tests check with the real validator first.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from skid.core.commands import apply_command
from skid.core.storage import load_document
from skid.core.validation import validate_project
from skid.drafting import build_drafting

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "pump_skid.json"
SVG = "{http://www.w3.org/2000/svg}"
EVENT_FIELDS = {"kind", "artifact", "message", "path", "completed", "total"}
MANIFEST_FIELDS = {"adapter", "adapter_version", "project_id", "source_revision", "input_digest", "status",
                   "diagnostics", "representations", "files"}
FIXTURE_TAGS = ("TK-101", "HV-101", "P-101", "NRV-101", "HV-102", "OUT-101", "LI-101", "PI-101",
                "L-101", "L-102", "L-103", "L-104", "L-105")


def fixture_document():
    return load_document(str(FIXTURE))


def run(document, kind, payload, command_id=None):
    command = {"id": command_id or f"cmd-{kind}", "expected_revision": document["current"]["revision"],
               "type": kind, "payload": payload}
    return apply_command(document, command)[0]


def undo(document):
    return run(document, "undo", {}, "cmd-undo")


def insert_hv103(document, placement=None):
    payload = {
        "connection_id": "conn-discharge-2",
        "entity": {"id": "ent-hv103", "kind": "isolation_valve", "tag": "HV-103", "template_version": 1,
                   "parameters": {
                       "length": {"value": 0.16, "unit": "m", "assumption_id": "asm-isolation-valve-dimensions"},
                       "body_diameter": {"value": 0.1, "unit": "m", "assumption_id": "asm-isolation-valve-dimensions"}}},
        "ports": [
            {"id": "port-hv103-in", "entity_id": "ent-hv103", "name": "inlet", "direction": "in", "required": True,
             "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"},
            {"id": "port-hv103-out", "entity_id": "ent-hv103", "name": "outlet", "direction": "out", "required": True,
             "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"},
        ],
        "downstream_connection": {"id": "conn-hv103-out", "tag": "L-106", "from_port_id": "port-hv103-out",
                                  "to_port_id": "port-discharge-valve-inlet", "nominal_diameter_mm": 50,
                                  "outside_diameter_m": 0.0603, "assumption_id": "asm-dn50-pipe"},
        "symbol": {"id": "sym-hv103", "entity_id": "ent-hv103", "x": 540.0, "y": 220.0, "rotation_deg": 0.0,
                   "manual": True},
        "downstream_edge": {"id": "edge-hv103-out", "connection_id": "conn-hv103-out", "from_symbol_id": "sym-hv103",
                            "to_symbol_id": "sym-discharge-valve", "points": [[556.0, 220.0], [584.0, 220.0]],
                            "manual": False, "status": "current"},
    }
    if placement is not None:
        payload["placement"] = placement
    return run(document, "insert_valve", payload, "cmd-insert-hv103")


def remove_check_valve(project):
    """NRV-101 removed with its downstream line; L-103 rewired to HV-102 so the topology stays valid."""
    p = copy.deepcopy(project)
    model, pid, physical = p["model"], p["pid"], p["physical"]
    del model["entities"]["ent-check-valve"]
    del model["ports"]["port-check-valve-inlet"]
    del model["ports"]["port-check-valve-outlet"]
    del model["connections"]["conn-discharge-2"]
    model["connections"]["conn-discharge-1"]["to_port_id"] = "port-discharge-valve-inlet"
    del pid["symbols"]["sym-check-valve"]
    del pid["edges"]["edge-discharge-2"]
    edge = pid["edges"]["edge-discharge-1"]
    edge["to_symbol_id"] = "sym-discharge-valve"
    edge["points"] = [[360.0, 272.0], [360.0, 220.0], [584.0, 220.0]]
    del physical["placements"]["ent-check-valve"]
    del physical["routes"]["conn-discharge-2"]
    physical["routes"]["conn-discharge-1"]["points_m"] = [[1.5, 0.0, 0.5], [1.5, 0.0, 0.7], [2.7, 0.0, 0.7],
                                                          [2.7, -0.37, 0.7]]
    return p


def remove_indicator(project, entity_id):
    p = copy.deepcopy(project)
    del p["model"]["entities"][entity_id]
    for sid in [s for s, record in p["pid"]["symbols"].items() if record["entity_id"] == entity_id]:
        del p["pid"]["symbols"][sid]
    del p["physical"]["placements"][entity_id]
    return p


def errors(project):
    return [d for d in validate_project(project) if d["severity"] == "error"]


def parse(path):
    return ET.parse(str(path)).getroot()


def visible_text(root):
    """Text a reader sees: every text element outside title, desc and metadata."""
    parts = []

    def walk(element, hidden):
        tag = element.tag.replace(SVG, "")
        hidden = hidden or tag in ("title", "desc", "metadata")
        if not hidden and tag in ("text", "tspan") and element.text:
            parts.append(element.text)
        for child in element:
            walk(child, hidden)

    walk(root, False)
    return " ".join(parts)


def occurrence_groups(root):
    return {e.get("data-occurrence-id"): e for e in root.iter()
            if e.get("data-occurrence-id") is not None and e.get("data-semantic-id") is not None}


class DraftingCase(unittest.TestCase):
    maxDiff = None

    def output_dir(self):
        handle = tempfile.TemporaryDirectory(prefix="skid-drafting-test-")
        self.addCleanup(handle.cleanup)
        return Path(handle.name)

    def build(self, project, **kwargs):
        out = self.output_dir()
        events = []
        manifests = build_drafting(project, str(out), emit=kwargs.pop("emit", events.append), **kwargs)
        return out, manifests, events

    def read_json(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def legend_entries(self, out):
        return sorted(e.get("data-legend-entry") for e in parse(out / "legend.svg").iter()
                      if e.get("data-legend-entry") is not None)

    def inventory(self, out):
        return self.read_json(out / "drawing.inventory.json")

    def index(self, out):
        return self.read_json(out / "item-index.json")

    def index_by_tag(self, out):
        return {item["tag"]: item for item in self.index(out)["items"]}

    def assertNoFile(self, path):
        self.assertFalse(os.path.exists(path), f"{path} should not exist")
