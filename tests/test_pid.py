"""P&ID adapter tests against the persisted fixture, the real validator, and real commands."""

from __future__ import annotations

import copy
import itertools
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from examples.make_fixture import make_document
from skid.adapters.pid import ADAPTER, ADAPTER_VERSION, METADATA_NAMESPACE, render_pid
from skid.adapters.pid_symbols import SYMBOLS
from skid.core.commands import apply_command
from skid.core.storage import load_document
from skid.core.validation import canonical_digest, validate_project
from skid.templates import diagram_port_position, get_template, world_port_position

FIXTURE = Path(__file__).resolve().parent.parent / "examples" / "pump_skid.json"
SVG = "{http://www.w3.org/2000/svg}"
META = "{" + METADATA_NAMESPACE + "}"
MANIFEST_KEYS = {
    "adapter",
    "adapter_version",
    "project_id",
    "source_revision",
    "input_digest",
    "status",
    "diagnostics",
    "representations",
}
KINDS = ("tank", "centrifugal_pump", "isolation_valve", "check_valve", "indicator", "boundary")
LINE_ROLES = ("path", "retained-path", "unresolved-hint", "detached-end")
GEOMETRY_ROLES = {
    "symbol-body": ("transform",),
    "path": ("points",),
    "retained-path": ("points",),
    "unresolved-hint": ("points",),
    "detached-end": ("points",),
    "port-anchor": ("cx", "cy"),
    "indication-relation": ("x1", "y1", "x2", "y2"),
}
PUMP_MOVED = {
    "position_m": [1.6, 0.0, 0.0],
    "rotation_deg": [0.0, 0.0, 0.0],
    "status": "placed",
    "manual": True,
    "assumption_id": "asm-skid-layout",
}


# --- Helpers ------------------------------------------------------------------------


def parse(svg):
    return ET.fromstring(svg.encode("utf-8"))


def occurrences(root):
    found = {}
    for element in root.iter():
        if element.get("data-occurrence-id") is not None:
            found.setdefault(element.get("data-occurrence-id"), []).append(element)
    return found


def occurrence(root, occurrence_id):
    (element,) = occurrences(root)[occurrence_id]
    return element


def role(group, name):
    return [element for element in group.iter() if element.get("data-role") == name]


def polyline(element):
    return [tuple(float(v) for v in pair.split(",")) for pair in element.get("points").split()]


def texts(element):
    return [text.text or "" for text in element.iter(SVG + "text")]


def layer(root, name):
    (group,) = [g for g in root.iter(SVG + "g") if g.get("data-layer") == name]
    return group


def selected(root):
    return {e.get("data-occurrence-id") for e in root.iter() if e.get("data-selected") == "true"}


def parents(root):
    return {child: parent for parent in root.iter() for child in parent}


def geometry(root):
    """Drawn positions by owning occurrence: symbol transforms, paths, anchors, relations."""
    up = parents(root)
    found = {}
    for element in root.iter():
        names = GEOMETRY_ROLES.get(element.get("data-role"))
        if not names:
            continue
        owner = element
        while owner.get("data-occurrence-id") is None:
            owner = up[owner]
        found.setdefault(owner.get("data-occurrence-id"), []).append(
            (element.get("data-role"),) + tuple(element.get(name) for name in names)
        )
    return found


def transform(group):
    (body,) = role(group, "symbol-body")
    match = re.fullmatch(r"translate\((\S+) (\S+)\) rotate\((\S+)\)", body.get("transform"))
    return tuple(float(v) for v in match.groups())


def parameter_texts(group):
    return {t.get("data-parameter"): t for t in group.iter(SVG + "text") if t.get("data-parameter")}


def errors(diagnostics):
    return [d for d in diagnostics if d["severity"] == "error"]


def codes(manifest, severity=None):
    return {d["code"] for d in manifest["diagnostics"] if severity is None or d["severity"] == severity}


def flagged(manifest, code):
    return {i for d in manifest["diagnostics"] if d["code"] == code for i in d["entity_ids"]}


def command(document, kind, payload, **extra):
    envelope = {
        "id": f"cmd-{kind}",
        "expected_revision": document["current"]["revision"],
        "type": kind,
        "payload": payload,
    }
    envelope.update(extra)
    new, _ = apply_command(document, envelope)
    return new


def insert_hv103(document):
    """Insert HV-103 into L-104 (NRV-101 -> HV-102) without a physical placement."""
    project = document["current"]
    parameters = project["model"]["entities"]["ent-discharge-valve"]["parameters"]
    valve = {
        "id": "ent-hv-103",
        "kind": "isolation_valve",
        "tag": "HV-103",
        "template_version": 1,
        "parameters": copy.deepcopy(parameters),
    }
    ports = [
        {
            "id": f"port-hv-103-{name}",
            "entity_id": "ent-hv-103",
            "name": name,
            "direction": spec["direction"],
            "required": True,
            "nominal_diameter_mm": 50,
            "service": "liquid",
            "assumption_id": "asm-dn50-liquid-service",
        }
        for name, spec in get_template("isolation_valve", 1)["ports"].items()
    ]
    downstream = {
        "id": "conn-l-106",
        "tag": "L-106",
        "from_port_id": "port-hv-103-outlet",
        "to_port_id": "port-discharge-valve-inlet",
        "nominal_diameter_mm": 50,
        "outside_diameter_m": 0.0603,
        "assumption_id": "asm-dn50-pipe",
    }
    symbol = {"id": "sym-hv-103", "entity_id": "ent-hv-103", "x": 540.0, "y": 220.0, "rotation_deg": 0.0, "manual": False}
    scratch = copy.deepcopy(project)
    scratch["model"]["entities"]["ent-hv-103"] = valve
    scratch["model"]["ports"].update((port["id"], port) for port in ports)
    scratch["pid"]["symbols"]["sym-hv-103"] = symbol
    points = [
        list(diagram_port_position(scratch, "port-hv-103-outlet", "sym-hv-103")),
        list(diagram_port_position(scratch, "port-discharge-valve-inlet", "sym-discharge-valve")),
    ]
    edge = {
        "id": "edge-l-106",
        "connection_id": "conn-l-106",
        "from_symbol_id": "sym-hv-103",
        "to_symbol_id": "sym-discharge-valve",
        "points": points,
        "manual": False,
        "status": "current",
    }
    payload = {
        "connection_id": "conn-discharge-2",
        "entity": valve,
        "ports": ports,
        "downstream_connection": downstream,
        "symbol": symbol,
        "downstream_edge": edge,
    }
    return command(document, "insert_valve", payload)


def resolve_hv103(document):
    """Place HV-103 on the discharge line and supply both validated route halves."""
    placement = {
        "position_m": [2.55, 0.0, 0.7],
        "rotation_deg": [0.0, 0.0, 0.0],
        "status": "placed",
        "manual": True,
        "assumption_id": "asm-test-hv-103",
    }
    note = {"description": "Test placement of HV-103 between the check and discharge valves.", "source": "tests/test_pid.py"}
    document = command(
        document,
        "place_equipment",
        {"entity_id": "ent-hv-103", "placement": placement},
        assumptions={"asm-test-hv-103": note},
    )
    project = document["current"]
    start = list(world_port_position(project, "port-check-valve-outlet"))
    valve_in = list(world_port_position(project, "port-hv-103-inlet"))
    valve_out = list(world_port_position(project, "port-hv-103-outlet"))
    end = list(world_port_position(project, "port-discharge-valve-inlet"))
    route = {"mode": "guided", "assumption_id": "asm-guided-routes"}
    document = command(document, "set_route", dict(route, connection_id="conn-discharge-2", points_m=[start, valve_in]))
    corner = [end[0], valve_out[1], valve_out[2]]
    return command(document, "set_route", dict(route, connection_id="conn-l-106", points_m=[valve_out, corner, end]))


def rename_ids(value, mapping):
    if isinstance(value, dict):
        return {mapping.get(k, k): rename_ids(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_ids(v, mapping) for v in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def reverse_keys(value):
    if isinstance(value, dict):
        return {k: reverse_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reverse_keys(v) for v in value]
    return value


def text_box(element):
    """Approximate glyph box of a monospace SVG text element."""
    size = float(element.get("font-size"))
    width = 0.6 * size * len(element.text or "")
    x, y = float(element.get("x")), float(element.get("y"))
    left = {"start": x, "middle": x - width / 2, "end": x - width}[element.get("text-anchor", "start")]
    return (left, y - 0.75 * size, left + width, y + 0.2 * size)


def crosses(segment, box, step=0.5):
    (x1, y1), (x2, y2) = segment
    count = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) / step))
    for i in range(count + 1):
        x, y = x1 + (x2 - x1) * i / count, y1 + (y2 - y1) * i / count
        if box[0] < x < box[2] and box[1] < y < box[3]:
            return True
    return False


# --- Fixture rendering (A01) ------------------------------------------------------------


class FixtureRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_document(str(FIXTURE))["current"]
        cls.svg, cls.manifest = render_pid(cls.project)
        cls.root = parse(cls.svg)
        cls.model, cls.pid = cls.project["model"], cls.project["pid"]

    def test_manifest_and_svg_identify_the_persisted_source(self):
        manifest = self.manifest
        self.assertEqual(set(manifest), MANIFEST_KEYS)
        self.assertEqual((manifest["adapter"], manifest["adapter_version"]), (ADAPTER, ADAPTER_VERSION))
        self.assertEqual(manifest["project_id"], "project-pump-skid")
        self.assertEqual(manifest["source_revision"], 0)
        self.assertEqual(manifest["input_digest"], canonical_digest(self.project))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["diagnostics"], [])
        json.dumps(manifest, allow_nan=False)

        root = self.root
        self.assertEqual(root.get("data-project-id"), "project-pump-skid")
        self.assertEqual(root.get("data-source-revision"), "0")
        self.assertEqual(root.get("data-input-digest"), manifest["input_digest"])
        self.assertEqual(root.get("data-status"), "complete")
        (info,) = root.iter(META + "pid")
        self.assertEqual(
            (info.get("adapter"), info.get("adapter-version"), info.get("project-id"), info.get("source-revision")),
            (ADAPTER, ADAPTER_VERSION, "project-pump-skid", "0"),
        )
        self.assertEqual(info.get("input-digest"), manifest["input_digest"])
        mapped = {
            rep.get("semantic-id"): [o.get("occurrence-id") for o in rep]
            for rep in info.iter(META + "representation")
        }
        self.assertEqual(mapped, manifest["representations"])
        header = " ".join(texts(layer(root, "header")))
        self.assertIn("Revision 0", header)
        self.assertIn("project-pump-skid", header)
        self.assertIn(manifest["input_digest"], header)

    def test_every_semantic_record_maps_to_drawn_occurrences(self):
        reps = self.manifest["representations"]
        model, pid = self.model, self.pid
        self.assertEqual(set(reps), set(model["entities"]) | set(model["connections"]) | set(model["ports"]))
        drawn = occurrences(self.root)
        self.assertEqual(set(drawn), {o for listed in reps.values() for o in listed})
        for semantic_id, listed in reps.items():
            self.assertTrue(listed, semantic_id)
            for occurrence_id in listed:
                self.assertEqual(len(drawn[occurrence_id]), 1, occurrence_id)
                self.assertEqual(drawn[occurrence_id][0].get("data-semantic-id"), semantic_id)
        for entity_id, entity in model["entities"].items():
            symbols = sorted(s for s, record in pid["symbols"].items() if record["entity_id"] == entity_id)
            self.assertEqual([o for o in reps[entity_id] if o in pid["symbols"]], symbols)
            relations = [o for o in reps[entity_id] if drawn[o][0].get("data-occurrence-kind") == "indication-relation"]
            self.assertEqual(len(relations), len(symbols) if "observes" in entity else 0, entity_id)
        for connection_id in model["connections"]:
            edges = sorted(e for e, record in pid["edges"].items() if record["connection_id"] == connection_id)
            self.assertEqual(reps[connection_id], edges)

    def test_edges_and_port_anchors_sit_on_template_anchors(self):
        reps, up = self.manifest["representations"], parents(self.root)
        for connection_id, connection in self.model["connections"].items():
            (edge_id,) = reps[connection_id]
            edge = self.pid["edges"][edge_id]
            group = occurrence(self.root, edge_id)
            self.assertEqual(group.get("data-from-port-id"), connection["from_port_id"])
            self.assertEqual(group.get("data-to-port-id"), connection["to_port_id"])
            (path,) = role(group, "path")
            points = polyline(path)
            self.assertEqual(points, [tuple(p) for p in edge["points"]])  # stored, including manual paths
            start = diagram_port_position(self.project, connection["from_port_id"], edge["from_symbol_id"])
            end = diagram_port_position(self.project, connection["to_port_id"], edge["to_symbol_id"])
            self.assertEqual((points[0], points[-1]), (start, end))
        for port_id in self.model["ports"]:
            for occurrence_id in reps[port_id]:
                group = occurrence(self.root, occurrence_id)
                symbol_id = up[group].get("data-occurrence-id")
                (marker,) = role(group, "port-anchor")
                anchor = diagram_port_position(self.project, port_id, symbol_id)
                self.assertEqual((float(marker.get("cx")), float(marker.get("cy"))), anchor)

    def test_symbols_are_drawn_at_stored_coordinates(self):
        for symbol_id, symbol in self.pid["symbols"].items():
            group = occurrence(self.root, symbol_id)
            self.assertEqual(transform(group), (symbol["x"], symbol["y"], symbol["rotation_deg"]))
            self.assertEqual(group.get("data-manual"), "true" if symbol["manual"] else "false")

    def test_indication_relations_are_dashed_and_unlike_process_lines(self):
        observing = {e: r["observes"]["entity_id"] for e, r in self.model["entities"].items() if "observes" in r}
        groups = [g for g in self.root.iter(SVG + "g") if g.get("data-occurrence-kind") == "indication-relation"]
        self.assertEqual(sorted(g.get("data-semantic-id") for g in groups), sorted(observing))
        process = set()
        for group in self.root.iter(SVG + "g"):
            if group.get("data-occurrence-kind") == "edge":
                (path,) = role(group, "path")
                self.assertIsNone(path.get("stroke-dasharray"))
                process.add((path.get("stroke"), path.get("stroke-dasharray")))
        for group in groups:
            (line,) = role(group, "indication-relation")
            self.assertTrue(line.get("stroke-dasharray"))
            self.assertNotIn((line.get("stroke"), line.get("stroke-dasharray")), process)
            indicator = self.pid["symbols"][group.get("data-indicator-symbol-id")]
            subject = self.pid["symbols"][group.get("data-observed-symbol-id")]
            self.assertEqual(indicator["entity_id"], group.get("data-semantic-id"))
            self.assertEqual(subject["entity_id"], observing[group.get("data-semantic-id")])

    def test_tags_and_annotations_are_readable(self):
        for entity_id, entity in self.model["entities"].items():
            for symbol_id in self.manifest["representations"][entity_id]:
                if symbol_id in self.pid["symbols"]:
                    group = occurrence(self.root, symbol_id)
                    self.assertIn(entity["tag"], texts(group))
                    self.assertEqual(set(parameter_texts(group)), set(entity["parameters"]))
        for connection_id, connection in self.model["connections"].items():
            for edge_id in self.manifest["representations"][connection_id]:
                tags = [t.split()[0] for t in texts(occurrence(self.root, edge_id)) if t]
                self.assertIn(connection["tag"], tags)

        drawing = layer(self.root, "drawing")
        boxes = [(text_box(t), t.text) for t in drawing.iter(SVG + "text")]
        for (a, text_a), (b, text_b) in itertools.combinations(boxes, 2):
            overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
            self.assertFalse(overlap, f"{text_a!r} overlaps {text_b!r}")
        segments = []
        for element in drawing.iter():
            if element.get("data-role") in LINE_ROLES:
                points = polyline(element)
                segments.extend(zip(points, points[1:]))
            elif element.get("data-role") == "indication-relation":
                x1, y1, x2, y2 = (float(element.get(k)) for k in ("x1", "y1", "x2", "y2"))
                segments.append(((x1, y1), (x2, y2)))
        for box, text in boxes:
            for segment in segments:
                self.assertFalse(crosses(segment, box), f"a drawn line crosses {text!r}")


# --- Engineering and presentation changes (A02-A06) -------------------------------------


class ChangeRenderTests(unittest.TestCase):
    def setUp(self):
        self.document = make_document()
        self.svg, self.manifest = render_pid(self.document["current"])
        self.root = parse(self.svg)

    def test_rename_updates_labels_without_changing_identity_or_layout(self):
        renamed = command(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        svg, manifest = render_pid(renamed["current"])
        root = parse(svg)
        self.assertEqual((manifest["status"], manifest["source_revision"]), ("complete", 1))
        self.assertNotEqual(manifest["input_digest"], self.manifest["input_digest"])
        self.assertEqual(manifest["representations"], self.manifest["representations"])
        self.assertEqual(geometry(root), geometry(self.root))
        self.assertNotIn("P-101", svg)
        pump = occurrence(root, "sym-pump")
        self.assertEqual(pump.get("data-tag"), "P-201")
        self.assertIn("P-201", texts(pump))
        self.assertTrue(any("P-201" in t for t in texts(occurrence(root, "sym-pressure-indicator"))))

    def test_parameter_change_and_provenance_are_visible(self):
        before = parameter_texts(occurrence(self.root, "sym-tank"))["height"]
        changed = command(
            self.document,
            "set_parameter",
            {
                "entity_id": "ent-tank",
                "name": "height",
                "quantity": {"value": 1.5, "unit": "m", "assumption_id": "asm-tank-dimensions"},
            },
        )
        svg, manifest = render_pid(changed["current"])
        self.assertEqual(manifest["status"], "complete")
        after = parameter_texts(occurrence(parse(svg), "sym-tank"))["height"]
        self.assertIn("1.2", before.text)
        self.assertIn("1.5", after.text)
        self.assertNotIn("1.2", after.text)
        self.assertEqual(after.get("data-assumption-id"), "asm-tank-dimensions")
        self.assertIn("*", after.text)

        sourced = command(
            changed,
            "set_parameter",
            {
                "entity_id": "ent-tank",
                "name": "height",
                "quantity": {"value": 1.5, "unit": "m", "source": "Vendor datasheet T-1 rev B"},
            },
        )
        height = parameter_texts(occurrence(parse(render_pid(sourced["current"])[0]), "sym-tank"))["height"]
        self.assertEqual(height.get("data-source"), "Vendor datasheet T-1 rev B")
        self.assertIsNone(height.get("data-assumption-id"))
        self.assertNotIn("*", height.text)

    def test_inserted_valve_without_placement_is_pending_until_resolved(self):
        inserted = insert_hv103(self.document)
        project = inserted["current"]
        svg, manifest = render_pid(project)
        root = parse(svg)
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(root.get("data-status"), "pending")
        self.assertFalse(codes(manifest, "error"))
        self.assertIn("ent-hv-103", flagged(manifest, "placement_unresolved"))
        self.assertLessEqual({"conn-discharge-2", "conn-l-106"}, flagged(manifest, "route_not_current"))

        reps = manifest["representations"]
        self.assertEqual(reps["ent-hv-103"], ["sym-hv-103"])
        self.assertEqual(reps["conn-discharge-2"], ["edge-discharge-2"])  # the upstream keeps its identity
        self.assertEqual(reps["conn-l-106"], ["edge-l-106"])
        upstream, downstream = occurrence(root, "edge-discharge-2"), occurrence(root, "edge-l-106")
        self.assertEqual(
            (upstream.get("data-from-symbol-id"), upstream.get("data-to-symbol-id")), ("sym-check-valve", "sym-hv-103")
        )
        self.assertEqual(
            (downstream.get("data-from-symbol-id"), downstream.get("data-to-symbol-id")),
            ("sym-hv-103", "sym-discharge-valve"),
        )
        self.assertEqual(polyline(role(upstream, "path")[0])[-1], diagram_port_position(project, "port-hv-103-inlet", "sym-hv-103"))
        self.assertEqual(polyline(role(downstream, "path")[0])[0], diagram_port_position(project, "port-hv-103-outlet", "sym-hv-103"))

        valve = occurrence(root, "sym-hv-103")
        self.assertEqual(transform(valve), (540.0, 220.0, 0.0))
        self.assertEqual(valve.get("data-physical-status"), "unresolved")
        self.assertTrue(role(valve, "physical-status"), "pending placement must be visible")
        for edge in (upstream, downstream):
            self.assertEqual(edge.get("data-status"), "current")
            self.assertNotEqual(edge.get("data-physical-route-status"), "current")
            self.assertTrue(role(edge, "physical-status"), "pending route must be visible")

        resolved = resolve_hv103(inserted)
        svg, after = render_pid(resolved["current"])
        root = parse(svg)
        self.assertEqual((after["status"], after["diagnostics"]), ("complete", []))
        self.assertEqual(after["representations"], manifest["representations"])
        self.assertEqual(occurrence(root, "sym-hv-103").get("data-physical-status"), "placed")
        self.assertFalse(role(root, "physical-status"))

    def test_moved_equipment_flags_routes_without_moving_the_diagram(self):
        moved = command(self.document, "move_equipment", {"entity_id": "ent-pump", "placement": PUMP_MOVED})
        svg, manifest = render_pid(moved["current"])
        root = parse(svg)
        self.assertEqual(manifest["status"], "pending")
        stale = flagged(manifest, "route_not_current") & set(moved["current"]["model"]["connections"])
        self.assertEqual(stale, {"conn-suction-2", "conn-discharge-1"})
        self.assertEqual(geometry(root), geometry(self.root))
        for connection_id, (edge_id,) in ((c, manifest["representations"][c]) for c in moved["current"]["model"]["connections"]):
            edge = occurrence(root, edge_id)
            self.assertEqual(edge.get("data-status"), "current")
            self.assertEqual(bool(role(edge, "physical-status")), connection_id in stale, connection_id)
            if connection_id in stale:
                self.assertEqual(edge.get("data-physical-route-status"), "needs_reroute")

    def test_moved_symbol_keeps_manual_path_visibly_flagged(self):
        moved = command(self.document, "move_symbol", {"symbol_id": "sym-pump", "x": 360.0, "y": 330.0, "rotation_deg": 0.0})
        project = moved["current"]
        svg, manifest = render_pid(project)
        root = parse(svg)
        self.assertEqual(manifest["status"], "pending")
        self.assertIn("edge-discharge-1", flagged(manifest, "edge_not_current"))
        for symbol_id, symbol in project["pid"]["symbols"].items():
            self.assertEqual(transform(occurrence(root, symbol_id)), (symbol["x"], symbol["y"], symbol["rotation_deg"]))

        manual = occurrence(root, "edge-discharge-1")
        self.assertEqual(manual.get("data-status"), "needs_reroute")
        self.assertFalse(role(manual, "path"), "a stale path must not be drawn as current")
        (kept,) = role(manual, "retained-path")
        stored = self.document["current"]["pid"]["edges"]["edge-discharge-1"]["points"]
        self.assertEqual(polyline(kept), [tuple(p) for p in stored])
        current = role(occurrence(root, "edge-suction-1"), "path")[0]
        (relation,) = role(occurrence(root, "sym-pressure-indicator#observes"), "indication-relation")
        style = (kept.get("stroke"), kept.get("stroke-dasharray"))
        self.assertNotEqual(style, (current.get("stroke"), current.get("stroke-dasharray")))
        self.assertNotEqual(style, (relation.get("stroke"), relation.get("stroke-dasharray")))
        self.assertTrue(role(manual, "path-status"))
        outlet = diagram_port_position(project, "port-pump-outlet", "sym-pump")
        self.assertIn([outlet, tuple(stored[0])], [polyline(e) for e in role(manual, "detached-end")])

        guided = polyline(role(occurrence(root, "edge-suction-2"), "path")[0])
        self.assertEqual(guided[-1], diagram_port_position(project, "port-pump-inlet", "sym-pump"))

    def test_edge_without_a_path_shows_only_a_flagged_topology_hint(self):
        project = copy.deepcopy(self.document["current"])
        project["pid"]["edges"]["edge-discharge-2"].update(status="unresolved", points=[], reason="path withdrawn")
        self.assertEqual(errors(validate_project(project)), [])
        svg, manifest = render_pid(project)
        self.assertEqual(manifest["status"], "pending")
        self.assertIn("edge-discharge-2", flagged(manifest, "edge_not_current"))
        group = occurrence(parse(svg), "edge-discharge-2")
        self.assertFalse(role(group, "path") or role(group, "retained-path"))
        (hint,) = role(group, "unresolved-hint")
        self.assertTrue(hint.get("stroke-dasharray"))
        connection = project["model"]["connections"]["conn-discharge-2"]
        self.assertEqual(
            polyline(hint),
            [
                diagram_port_position(project, connection["from_port_id"], "sym-check-valve"),
                diagram_port_position(project, connection["to_port_id"], "sym-discharge-valve"),
            ],
        )
        self.assertTrue(role(group, "path-status"))


# --- Selection (A07 groundwork) -----------------------------------------------------------


class SelectionTests(unittest.TestCase):
    def setUp(self):
        project = make_document()["current"]
        extra = dict(project["pid"]["symbols"]["sym-pump"], id="sym-pump-2", x=560.0, y=330.0, manual=True)
        project["pid"]["symbols"]["sym-pump-2"] = extra
        self.assertEqual(errors(validate_project(project)), [])
        self.project = project
        self.plain_svg, self.plain = render_pid(project)

    def test_entity_selection_highlights_every_occurrence(self):
        svg, manifest = render_pid(self.project, ("ent-pump",))
        root = parse(svg)
        self.assertEqual(manifest["representations"]["ent-pump"], ["sym-pump", "sym-pump-2"])
        self.assertEqual(selected(root), {"sym-pump", "sym-pump-2"})
        for symbol_id in ("sym-pump", "sym-pump-2"):
            self.assertTrue(role(occurrence(root, symbol_id), "selection-halo"))
        self.assertEqual(manifest, self.plain)  # selection is transient
        self.assertEqual(selected(parse(self.plain_svg)), set())
        self.assertFalse(role(parse(self.plain_svg), "selection-halo"))

    def test_port_connection_and_indicator_selection(self):
        root = parse(render_pid(self.project, ("port-pump-inlet",))[0])
        up = parents(root)
        markers = {o for o, (e,) in occurrences(root).items() if e.get("data-semantic-id") == "port-pump-inlet"}
        self.assertEqual(selected(root), markers)
        self.assertEqual({up[occurrence(root, o)].get("data-occurrence-id") for o in markers}, {"sym-pump", "sym-pump-2"})

        root = parse(render_pid(self.project, ("conn-discharge-1", "ent-tank"))[0])
        self.assertEqual(selected(root), {"edge-discharge-1", "sym-tank"})
        self.assertTrue(role(occurrence(root, "edge-discharge-1"), "selection-halo"))

        root = parse(render_pid(self.project, ("ent-pressure-indicator",))[0])
        kinds = {occurrence(root, o).get("data-occurrence-kind") for o in selected(root)}
        self.assertEqual(kinds, {"symbol", "indication-relation"})
        self.assertEqual(selected(root), set(self.plain["representations"]["ent-pressure-indicator"]))

    def test_unknown_selection_is_reported_not_synchronized(self):
        svg, manifest = render_pid(self.project, ["ent-ghost", "sym-pump"])
        self.assertEqual(self.plain["status"], "complete")
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(flagged(manifest, "selection_unknown_id"), {"ent-ghost", "sym-pump"})
        self.assertEqual(selected(parse(svg)), set())
        self.assertEqual(manifest["representations"], self.plain["representations"])
        self.assertEqual(manifest["input_digest"], self.plain["input_digest"])
        with self.assertRaises(TypeError):
            render_pid(self.project, "ent-pump")


# --- Failure, escaping, determinism, immutability (A09, A10) --------------------------------


class RobustnessTests(unittest.TestCase):
    def test_structural_errors_fail_without_a_drawing(self):
        project = make_document()["current"]
        project["model"]["entities"]["ent-discharge-valve"]["tag"] = " hv-101 "
        svg, manifest = render_pid(project)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("duplicate_tag", codes(manifest, "error"))
        self.assertEqual(manifest["representations"], {})
        self.assertEqual(manifest["input_digest"], canonical_digest(project))
        root = parse(svg)
        self.assertEqual(root.get("data-status"), "failed")
        self.assertEqual(occurrences(root), {})
        self.assertTrue(any("FAILED" in t for t in texts(root)))

    def test_ids_tags_and_text_are_xml_escaped(self):
        entity_id, symbol_id, connection_id = "ent<pump>&\"x\"'", "sym 'pump' & <1>", "conn]]>&amp;"
        project = rename_ids(
            make_document()["current"],
            {"ent-pump": entity_id, "sym-pump": symbol_id, "conn-suction-1": connection_id},
        )
        project["model"]["entities"][entity_id]["tag"] = "P<&>\"'-101"
        project["model"]["connections"][connection_id]["tag"] = "L-101 ]]> <b>&nbsp;"
        project["model"]["assumptions"]["asm-tank-dimensions"]["description"] = "<script>alert('x')</script> & co"
        self.assertEqual(errors(validate_project(project)), [])

        svg, manifest = render_pid(project, (entity_id,))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["representations"][entity_id], [symbol_id])
        root = parse(svg)
        pump = occurrence(root, symbol_id)
        self.assertEqual(pump.get("data-semantic-id"), entity_id)
        self.assertEqual(pump.get("data-tag"), "P<&>\"'-101")
        self.assertIn("P<&>\"'-101", texts(pump))
        self.assertEqual(pump.get("data-selected"), "true")
        line = occurrence(root, "edge-suction-1")
        self.assertEqual(line.get("data-semantic-id"), connection_id)
        self.assertTrue(any(t.startswith("L-101 ]]> <b>&nbsp;") for t in texts(line)))
        self.assertTrue(any("<script>alert('x')</script> & co" in t for t in texts(layer(root, "footer"))))
        self.assertEqual([e for e in root.iter() if e.tag in (SVG + "script", SVG + "b", "script", "b")], [])

    def test_characters_xml_cannot_carry_are_replaced_and_reported(self):
        project = make_document()["current"]
        project["model"]["entities"]["ent-pump"]["tag"] = "P\x07-101"
        self.assertEqual(errors(validate_project(project)), [])
        svg, manifest = render_pid(project)
        root = parse(svg)
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(flagged(manifest, "xml_text_replaced"), {"ent-pump"})
        self.assertIn("P\N{REPLACEMENT CHARACTER}-101", texts(occurrence(root, "sym-pump")))

    def test_rebuilding_is_deterministic(self):
        document = make_document()
        moved = command(document, "move_equipment", {"entity_id": "ent-pump", "placement": PUMP_MOVED})
        for project in (document["current"], moved["current"]):
            first = render_pid(project, ("ent-pump",))
            self.assertEqual(render_pid(project, ("ent-pump",)), first)
            self.assertEqual(render_pid(reverse_keys(project), ("ent-pump",)), first)
        self.assertEqual(render_pid(load_document(str(FIXTURE))["current"]), render_pid(document["current"]))

    def test_rendering_does_not_mutate_inputs(self):
        document = make_document()
        moved = command(document, "move_symbol", {"symbol_id": "sym-pump", "x": 380.0, "y": 300.0, "rotation_deg": 0.0})
        broken = copy.deepcopy(document["current"])
        broken["pid"]["symbols"]["sym-tank"]["entity_id"] = "ent-missing"
        for project in (document["current"], moved["current"], broken):
            before = copy.deepcopy(project)
            selection = ["ent-pump", "ent-ghost"]
            render_pid(project, selection)
            self.assertEqual(project, before)
            self.assertEqual(selection, ["ent-pump", "ent-ghost"])


# --- Original symbols ---------------------------------------------------------------------


class SymbolTests(unittest.TestCase):
    def test_every_kind_has_a_symbol_whose_outline_meets_its_anchors(self):
        self.assertEqual(set(SYMBOLS), set(KINDS))
        for kind in KINDS:
            x0, y0, x1, y1 = SYMBOLS[kind].bbox
            for name, (ax, ay) in get_template(kind, 1)["diagram_anchors"].items():
                on_side = ax in (x0, x1) and y0 <= ay <= y1
                on_top_or_bottom = ay in (y0, y1) and x0 <= ax <= x1
                self.assertTrue(on_side or on_top_or_bottom, f"{kind} anchor {name} is off the symbol outline")


if __name__ == "__main__":
    unittest.main()
