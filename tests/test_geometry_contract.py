# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Geometry and mapping contract of the Blender adapter, checked without Blender.

These tests plan the shared fixture with ``skid.adapters.blender_support.plan``
and compare the planned object transforms and meshes against the templates'
``world_port_position`` and the persisted routes. ``tests/blender_smoke.py``
repeats the port and route checks on real Blender objects and reuses the
command helpers below.
"""

import copy
import json
import math
import os
import sys
import unittest
from collections import Counter

from skid.adapters.blender_support import geometry as geo
from skid.adapters.blender_support.plan import (
    ADAPTER,
    ADAPTER_VERSION,
    MANIFEST_FIELDS,
    MATERIAL_STYLES,
    manifest_from_plan,
    plan_scene,
)
from skid.core import apply_command, load_document
from skid.core.validation import canonical_digest
from skid.templates import entity_primitives, world_port_position

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "examples", "pump_skid.json")
TOLERANCE_M = 1e-6

PUMP = "ent-pump"
VALVE = "ent-hv-103"
VALVE_IN = "port-hv-103-inlet"
VALVE_OUT = "port-hv-103-outlet"
DOWNSTREAM = "conn-l-106"
SPLIT = "conn-discharge-2"
MOVED_ROUTES = ("conn-discharge-1", "conn-suction-2")
TEST_SOURCE = "tests/test_geometry_contract.py prototype test assumption"


# --- Fixture commands (shared with tests/blender_smoke.py) -------------------


def load_fixture():
    return load_document(FIXTURE)


def apply(document, kind, payload, assumptions=None):
    """Apply one command through the core; return the new document."""
    command = {"id": f"cmd-test-{kind}", "expected_revision": document["current"]["revision"], "type": kind,
               "payload": payload}
    known = document["current"]["model"]["assumptions"]
    fresh = {key: value for key, value in (assumptions or {}).items() if key not in known}
    if fresh:
        command["assumptions"] = fresh
    new, _ = apply_command(document, command)
    return new


def _assumption(key, description):
    return {key: {"description": description, "source": TEST_SOURCE}}


def move_pump(document, position=(1.5, 0.3, 0.0), rotation=(0.0, 0.0, 0.0)):
    placement = {"position_m": list(position), "rotation_deg": list(rotation), "status": "placed", "manual": True,
                 "assumption_id": "asm-test-pump-move"}
    return apply(document, "move_equipment", {"entity_id": PUMP, "placement": placement},
                 _assumption("asm-test-pump-move", "Pump moved to exercise stale physical routes."))


def port_axis(project, port_id):
    """World outward axis of a port: the +z axis of its nozzle primitive."""
    port = project["model"]["ports"][port_id]
    entity = project["model"]["entities"][port["entity_id"]]
    placement = project["physical"]["placements"][port["entity_id"]]
    (nozzle,) = [p for p in entity_primitives(entity) if p["key"] == f"nozzle/{port['name']}"]
    local = geo.transform_vector(geo.affine((0, 0, 0), nozzle["rotation_deg"]), (0.0, 0.0, 1.0))
    return tuple(round(v, 9) + 0.0 for v in geo.transform_vector(geo.affine((0, 0, 0), placement["rotation_deg"]), local))


def port_route(document, connection_id):
    """Orthogonal route leaving and entering both nozzles along their axes, for the cases tested here."""
    project = document["current"]
    connection = project["model"]["connections"][connection_id]
    start = world_port_position(project, connection["from_port_id"])
    end = world_port_position(project, connection["to_port_id"])
    leaving, entering = port_axis(project, connection["from_port_id"]), port_axis(project, connection["to_port_id"])
    corners = [start]
    if abs(leaving[2]) == 1.0:
        corners.append((start[0], start[1], end[2]))
    x, y, z = corners[-1]
    if abs(entering[0]) == 1.0 and abs(z - end[2]) <= 1e-9:
        if abs(y - end[1]) <= 1e-9:
            pass
        elif abs(leaving[0]) == 1.0:
            middle = (x + end[0]) / 2
            corners += [(middle, y, z), (middle, end[1], z)]
        else:
            corners.append((x, end[1], z))
    elif abs(entering[1]) == 1.0 and abs(z - end[2]) <= 1e-9:
        corners.append((end[0], y, z))
    else:
        raise ValueError(f"no test route shape for {connection_id}")
    corners.append(end)
    path = [list(corners[0])]
    for point in corners[1:]:
        if max(abs(a - b) for a, b in zip(point, path[-1])) > 1e-9:
            path.append(list(point))
    return path


def set_route(document, connection_id, points, assumption_id="asm-test-routes"):
    payload = {"connection_id": connection_id, "points_m": points, "mode": "manual", "assumption_id": assumption_id}
    return apply(document, "set_route", payload,
                 _assumption(assumption_id, "Explicit orthogonal test routes between the transformed ports."))


def reroute(document, connection_ids):
    for connection_id in connection_ids:
        document = set_route(document, connection_id, port_route(document, connection_id))
    return document


def insert_hv103(document):
    """Insert HV-103 into L-104 (NRV-101 -> HV-102) without a physical placement."""
    model = document["current"]["model"]
    connection = model["connections"][SPLIT]
    entity = dict(copy.deepcopy(model["entities"]["ent-discharge-valve"]), id=VALVE, tag="HV-103")
    ports = [
        dict(copy.deepcopy(model["ports"]["port-discharge-valve-inlet"]), id=VALVE_IN, entity_id=VALVE),
        dict(copy.deepcopy(model["ports"]["port-discharge-valve-outlet"]), id=VALVE_OUT, entity_id=VALVE),
    ]
    downstream = dict(copy.deepcopy(connection), id=DOWNSTREAM, tag="L-106", from_port_id=VALVE_OUT)
    symbol = {"id": "sym-hv-103", "entity_id": VALVE, "x": 540.0, "y": 220.0, "rotation_deg": 0.0, "manual": False}
    edge = {"id": "edge-l-106", "connection_id": DOWNSTREAM, "from_symbol_id": "sym-hv-103",
            "to_symbol_id": "sym-discharge-valve", "points": [], "manual": False, "status": "unresolved",
            "reason": "diagram path not supplied by the Blender adapter tests"}
    payload = {"connection_id": SPLIT, "entity": entity, "ports": ports, "downstream_connection": downstream,
               "symbol": symbol, "downstream_edge": edge}
    return apply(document, "insert_valve", payload)


def place_hv103(document):
    """Place HV-103 on the L-104 run and route both halves of the split.

    It sits on the leg into HV-102, turned like HV-102. Centred on the 0.31 m x run instead, its 30 mm gap to NRV-101
    left no room for the 48 mm route flanges: each cut 21,411 mm3 into the facing nozzle (OCCT), a refused intrusion.
    """
    placement = {"position_m": [2.7, -0.22, 0.7], "rotation_deg": [0.0, 0.0, -90.0], "status": "placed",
                 "manual": True, "assumption_id": "asm-test-hv-103"}
    document = apply(document, "place_equipment", {"entity_id": VALVE, "placement": placement},
                     _assumption("asm-test-hv-103", "HV-103 centred on the L-104 leg into HV-102 at y = -0.22 m."))
    return reroute(document, (SPLIT, DOWNSTREAM))


# --- Plan inspection helpers -------------------------------------------------


def specs_by_key(plan):
    return {spec["key"]: spec for spec in plan["objects"]}


def owned_specs(plan, owner):
    return [spec for spec in plan["objects"] if spec["owner_id"] == owner]


def nozzle_end(spec):
    return geo.transform_point(spec["matrix_world"], (0.0, 0.0, spec["mesh"]["length_m"] / 2))


def piece_ends(spec):
    """World start and end of a current pipe piece, derived from its transform and mesh."""
    mesh = spec["mesh"]
    if mesh["shape"] == "cylinder":
        half = mesh["length_m"] / 2
        return (geo.transform_point(spec["matrix_world"], (0.0, 0.0, -half)),
                geo.transform_point(spec["matrix_world"], (0.0, 0.0, half)))
    vertices = geo.mesh_for(mesh)["vertices"]
    ring = mesh["segments"]

    def centroid(start):
        points = vertices[start:start + ring]
        return geo.transform_point(spec["matrix_world"], [sum(p[i] for p in points) / ring for i in range(3)])

    return centroid(0), centroid(len(vertices) - ring)


def newell_normal(points):
    normal = [0.0, 0.0, 0.0]
    for (x1, y1, z1), (x2, y2, z2) in zip(points, points[1:] + points[:1]):
        normal[0] += (y1 - y2) * (z1 + z2)
        normal[1] += (z1 - z2) * (x1 + x2)
        normal[2] += (x1 - x2) * (y1 + y2)
    return normal


def edge_counts(faces):
    counts = Counter()
    for face in faces:
        for a, b in zip(face, face[1:] + face[:1]):
            counts[frozenset((a, b))] += 1
    return counts


class GeometryContractTests(unittest.TestCase):
    def setUp(self):
        self.document = load_fixture()
        self.project = self.document["current"]

    def assertClose(self, actual, expected, tolerance=TOLERANCE_M, message=None):
        self.assertEqual(len(actual), len(expected), message)
        self.assertLessEqual(math.dist(actual, expected), tolerance, message or f"{actual} != {expected}")

    def assertRoutesMatchPorts(self, document, plan):
        project = document["current"]
        specs = specs_by_key(plan)
        for cid, connection in project["model"]["connections"].items():
            route = project["physical"]["routes"][cid]
            if route["status"] != "current":
                continue
            pieces = [specs[key] for key in plan["representations"][cid]
                      if specs[key]["component_key"].split("/")[0] in ("segment", "elbow", "joint")]
            self.assertTrue(pieces, cid)
            self.assertEqual(pieces[0]["component_key"], "segment/0", cid)
            self.assertEqual(pieces[-1]["component_key"], f"segment/{len(route['points_m']) - 2}", cid)
            start = world_port_position(project, connection["from_port_id"])
            end = world_port_position(project, connection["to_port_id"])
            ends = [piece_ends(piece) for piece in pieces if piece["mesh"]["shape"] != "sphere"]
            self.assertClose(ends[0][0], start, message=f"{cid} does not start at its from port")
            self.assertClose(ends[-1][1], end, message=f"{cid} does not end at its to port")
            self.assertClose(ends[0][0], route["points_m"][0])
            self.assertClose(ends[-1][1], route["points_m"][-1])
            for (_, previous_end), (next_start, _) in zip(ends, ends[1:]):
                self.assertClose(next_start, previous_end, message=f"{cid} pieces are not continuous")
            for piece, (piece_start, piece_end) in zip([p for p in pieces if p["mesh"]["shape"] != "sphere"], ends):
                self.assertClose(piece_start, piece["properties"]["start_m"])
                self.assertClose(piece_end, piece["properties"]["end_m"])
                self.assertAlmostEqual(piece["mesh"]["pipe_radius_m" if piece["mesh"]["shape"] == "bend" else "radius_m"],
                                       connection["outside_diameter_m"] / 2)

    # Fixture mapping and identity

    def test_fixture_plan_is_complete_deterministic_and_leaves_input_alone(self):
        before = copy.deepcopy(self.project)
        plan = plan_scene(self.project)
        self.assertEqual(self.project, before)
        self.assertEqual(plan["status"], "complete")
        self.assertEqual([d for d in plan["diagnostics"] if d["severity"] == "error"], [])
        self.assertEqual(plan["input_digest"], canonical_digest(self.project))
        self.assertEqual(plan, plan_scene(copy.deepcopy(self.project)))
        json.dumps(plan, allow_nan=False)
        self.assertEqual(len(plan["objects"]), 76)
        self.assertEqual(Counter(spec["type"] for spec in plan["objects"]), {"mesh": 49, "text": 14, "empty": 13})

    def test_every_semantic_id_maps_to_its_objects(self):
        plan = plan_scene(self.project)
        model = self.project["model"]
        representations = plan["representations"]
        semantic = [*model["entities"], *model["ports"], *model["connections"], self.project["id"]]
        self.assertEqual(sorted(representations), sorted(semantic))
        for owner in semantic:
            self.assertTrue(representations[owner], owner)
        keys = [spec["key"] for spec in plan["objects"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len({spec["name"] for spec in plan["objects"]}), len(keys))
        mapped = [key for owner in [*model["entities"], *model["connections"], self.project["id"]]
                  for key in representations[owner]]
        self.assertEqual(sorted(mapped), sorted(keys))
        for port_id, port in model["ports"].items():
            self.assertEqual(representations[port_id], [f"{port['entity_id']}#nozzle/{port['name']}"])
        self.assertEqual(representations["ent-pump"][:5],
                         ["ent-pump#frame", "ent-pump#body", "ent-pump#motor", "ent-pump#nozzle/inlet",
                          "ent-pump#nozzle/outlet"])
        self.assertEqual(representations["conn-discharge-1"],
                         ["conn-discharge-1#route", "conn-discharge-1#segment/0", "conn-discharge-1#elbow/1",
                          "conn-discharge-1#segment/1", "conn-discharge-1#label", "conn-discharge-1#label/plate"])

    def test_objects_carry_identity_properties_and_tags_only_as_labels(self):
        plan = plan_scene(self.project)
        model = self.project["model"]
        for spec in plan["objects"]:
            props = spec["properties"]
            owner, component = spec["key"].rsplit("#", 1)
            self.assertEqual(props["representation_id"], spec["key"])
            self.assertEqual(props["project_id"], "project-pump-skid")
            self.assertEqual(props["component_key"], component)
            self.assertEqual(props["source_revision"], 0)
            self.assertEqual(props["skid_generator"], "skid.adapters.blender")
            if owner in model["entities"]:
                self.assertEqual(props["entity_id"], owner)
                self.assertNotIn("connection_id", props)
            elif owner in model["connections"]:
                self.assertEqual(props["connection_id"], owner)
                self.assertNotIn("entity_id", props)
            else:
                self.assertEqual(owner, "project-pump-skid")
                self.assertIn(component, ("status", "status/plate"))
            if component == "label":
                record = model["entities"].get(owner) or model["connections"][owner]
                self.assertEqual(props["tag"], record["tag"])
                self.assertTrue(spec["text"]["body"].startswith(record["tag"]))
            else:
                self.assertNotIn("tag", props)
            self.assertNotIn(model["entities"].get(owner, {}).get("tag", "#"), spec["name"])
            for value in props.values():
                self.assertIsNotNone(value)
            if spec["material"] is not None:
                self.assertIn(spec["material"], MATERIAL_STYLES)

    def test_manifest_has_exactly_the_contracted_fields(self):
        plan = plan_scene(self.project)
        manifest = manifest_from_plan(plan)
        self.assertEqual(tuple(manifest), MANIFEST_FIELDS)
        self.assertEqual(manifest["adapter"], ADAPTER)
        self.assertEqual(manifest["adapter_version"], ADAPTER_VERSION)
        self.assertEqual(manifest["project_id"], "project-pump-skid")
        self.assertEqual(manifest["source_revision"], 0)
        self.assertEqual(manifest["input_digest"], canonical_digest(self.project))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["representations"], plan["representations"])
        failed = manifest_from_plan(plan, "failed")
        self.assertEqual(failed["representations"], {})
        json.dumps(manifest, allow_nan=False)

    # Ports and routes

    def test_nozzle_ends_are_the_world_port_positions(self):
        plan = plan_scene(self.project)
        specs = specs_by_key(plan)
        for port_id in self.project["model"]["ports"]:
            (key,) = plan["representations"][port_id]
            spec = specs[key]
            expected = world_port_position(self.project, port_id)
            self.assertClose(nozzle_end(spec), expected, message=port_id)
            self.assertEqual(spec["properties"]["port_id"], port_id)
            self.assertEqual(spec["properties"]["port_position_m"], list(expected))

    def test_rotated_placement_uses_the_template_euler_convention(self):
        moved = move_pump(self.document, position=(1.6, 0.2, 0.05), rotation=(10.0, 20.0, 30.0))
        project = moved["current"]
        plan = plan_scene(project)
        self.assertEqual(plan["status"], "pending")
        specs = specs_by_key(plan)
        frame = specs["ent-pump#frame"]
        self.assertEqual(frame["location"], [1.6, 0.2, 0.05])
        self.assertEqual(frame["rotation_deg"], [10.0, 20.0, 30.0])
        for name in ("inlet", "outlet"):
            port_id = f"port-pump-{name}"
            self.assertClose(nozzle_end(specs[f"ent-pump#nozzle/{name}"]), world_port_position(project, port_id))
        # Independent check of R = Rz @ Ry @ Rx on the motor axis (local +x of the pump).
        a, b, c = (math.radians(v) for v in (10.0, 20.0, 30.0))
        expected_axis = (math.cos(c) * math.cos(b), math.sin(c) * math.cos(b), -math.sin(b))
        motor = specs["ent-pump#motor"]["matrix_world"]
        self.assertClose(geo.transform_vector(motor, (0.0, 0.0, 1.0)), expected_axis, 1e-9)

    def test_current_routes_start_and_end_at_ports_and_pieces_are_continuous(self):
        plan = plan_scene(self.project)
        self.assertRoutesMatchPorts(self.document, plan)
        specs = specs_by_key(plan)
        elbow = specs["conn-discharge-1#elbow/1"]
        self.assertAlmostEqual(elbow["properties"]["bend_radius_m"], 1.5 * 0.0603)
        for key, spec in specs.items():
            if spec["component_key"].startswith("segment/"):
                direction = geo.transform_vector(spec["matrix_world"], (0.0, 0.0, 1.0))
                self.assertEqual(sorted(abs(round(v, 9)) for v in direction), [0.0, 0.0, 1.0], key)

    def test_rerouted_and_inserted_routes_match_transformed_ports(self):
        document = reroute(move_pump(self.document), MOVED_ROUTES)
        plan = plan_scene(document["current"])
        self.assertEqual(plan["status"], "complete")
        self.assertRoutesMatchPorts(document, plan)
        document = place_hv103(insert_hv103(self.document))
        plan = plan_scene(document["current"])
        self.assertEqual(plan["status"], "complete")
        self.assertRoutesMatchPorts(document, plan)
        self.assertIn(f"{VALVE}#nozzle/inlet", plan["representations"][VALVE_IN])

    def test_labels_sit_above_their_equipment(self):
        plan = plan_scene(self.project)
        specs = specs_by_key(plan)
        for eid in self.project["model"]["entities"]:
            label = specs[f"{eid}#label"]
            tops = [geo.transform_point(spec["matrix_world"], (0.0, 0.0, 0.0))[2] for spec in owned_specs(plan, eid)
                    if spec["type"] == "mesh" and not spec["component_key"].startswith("label")]
            self.assertGreater(label["matrix_world"][2][3], max(tops), eid)
            self.assertEqual(label["rotation_deg"], [90.0, 0.0, 0.0])

    # Pending and failed states

    def test_moved_equipment_flags_incident_routes_and_never_draws_them_current(self):
        original = copy.deepcopy(self.project["physical"]["routes"])
        moved = move_pump(self.document)
        project = moved["current"]
        plan = plan_scene(project)
        self.assertEqual(plan["status"], "pending")
        specs = specs_by_key(plan)
        flagged = {d["entity_ids"][0] for d in plan["diagnostics"] if d["code"] == "route_flagged"}
        self.assertEqual(flagged, set(MOVED_ROUTES))
        for cid in MOVED_ROUTES:
            route = project["physical"]["routes"][cid]
            self.assertEqual(route["status"], "needs_reroute")
            self.assertEqual(route["points_m"], original[cid]["points_m"])
            components = [specs[key]["component_key"] for key in plan["representations"][cid]]
            self.assertFalse([c for c in components if c.split("/")[0] in ("segment", "elbow", "joint")], cid)
            ghosts = [specs[key] for key in plan["representations"][cid] if specs[key]["component_key"].startswith("stale/segment/")]
            self.assertEqual(len(ghosts), len(route["points_m"]) - 1)
            self.assertClose(ghosts[0]["properties"]["start_m"], route["points_m"][0])
            self.assertTrue(all(g["material"] == "stale" for g in ghosts))
            label = specs[f"{cid}#label"]
            self.assertEqual(label["text"]["body"], f"{project['model']['connections'][cid]['tag']} REROUTE")
            self.assertEqual(label["material"], "label_warning")
        self.assertClose(nozzle_end(specs["ent-pump#nozzle/inlet"]), world_port_position(project, "port-pump-inlet"))
        self.assertGreater(math.dist(world_port_position(project, "port-pump-inlet"),
                                     project["physical"]["routes"]["conn-suction-2"]["points_m"][-1]), 0.1)
        self.assertIn("PENDING", specs["project-pump-skid#status"]["text"]["body"])
        self.assertEqual(self.project["physical"]["routes"], original)

    def test_unplaced_insertion_is_pending_with_explicit_empty_mappings(self):
        inserted = insert_hv103(self.document)
        plan = plan_scene(inserted["current"])
        self.assertEqual(plan["status"], "pending")
        for semantic_id in (VALVE, VALVE_IN, VALVE_OUT, DOWNSTREAM):
            self.assertEqual(plan["representations"][semantic_id], [], semantic_id)
        codes = {(d["code"], d["entity_ids"][0]) for d in plan["diagnostics"] if d["severity"] == "warning" and d["entity_ids"]}
        self.assertIn(("geometry_omitted", VALVE), codes)
        self.assertIn(("route_omitted", DOWNSTREAM), codes)
        self.assertIn(("route_flagged", SPLIT), codes)
        specs = specs_by_key(plan)
        self.assertEqual(specs[f"{SPLIT}#label"]["text"]["body"], "L-104 UNRESOLVED")
        board = specs["project-pump-skid#status"]["text"]["body"]
        self.assertIn("HV-103: placement unresolved", board)
        self.assertIn("L-106: route unresolved", board)
        self.assertFalse([d for d in plan["diagnostics"] if d["code"] in ("edge_not_current", "edge_missing")])

    def test_structural_errors_and_invalid_input_fail_without_objects(self):
        broken = copy.deepcopy(self.project)
        broken["model"]["entities"][PUMP]["tag"] = "tk-101"
        plan = plan_scene(broken)
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["objects"], [])
        self.assertIn("duplicate_tag", {d["code"] for d in plan["diagnostics"]})
        self.assertEqual(manifest_from_plan(plan)["representations"], {})
        nonfinite = copy.deepcopy(self.project)
        nonfinite["physical"]["placements"][PUMP]["position_m"][0] = float("nan")
        plan = plan_scene(nonfinite)
        self.assertEqual((plan["status"], plan["input_digest"]), ("failed", None))
        self.assertEqual(plan_scene(["not", "a", "project"])["status"], "failed")

    def test_selection_highlights_without_changing_identity_or_digest(self):
        plain = plan_scene(self.project)
        selected = plan_scene(self.project, ("ent-pump", "port-tank-outlet", "conn-suction-1", "missing-id"))
        self.assertEqual(selected["status"], "complete")
        self.assertEqual(selected["input_digest"], plain["input_digest"])
        self.assertEqual(selected["representations"], plain["representations"])
        self.assertEqual([s["matrix_world"] for s in selected["objects"]], [s["matrix_world"] for s in plain["objects"]])
        self.assertIn("selection_unknown", {d["code"] for d in selected["diagnostics"]})
        specs = specs_by_key(selected)
        highlighted = {key for owner in ("ent-pump", "port-tank-outlet", "conn-suction-1")
                       for key in selected["representations"][owner]}
        for key, spec in specs.items():
            self.assertEqual(spec["properties"]["selected"], key in highlighted, key)
            if key in highlighted and spec["material"] is not None:
                self.assertEqual(spec["material"], "plate" if key.endswith("/plate") else "selected", key)
        self.assertEqual(specs["ent-tank#body"]["material"], "vessel")
        self.assertEqual(plan_scene(self.project, "ent-pump")["selected_ids"], [])

    def test_rename_changes_label_text_only(self):
        renamed = apply(self.document, "rename_entity", {"entity_id": PUMP, "tag": "P-201"})
        before, after = plan_scene(self.project), plan_scene(renamed["current"])
        self.assertEqual(after["representations"], before["representations"])
        self.assertEqual([s["name"] for s in after["objects"]], [s["name"] for s in before["objects"]])
        self.assertEqual([s["matrix_world"] for s in after["objects"]], [s["matrix_world"] for s in before["objects"]])
        changed = [b["key"] for b, a in zip(before["objects"], after["objects"])
                   if (b["text"] or {}).get("body") != (a["text"] or {}).get("body")]
        self.assertEqual(changed, ["ent-pump#label", "project-pump-skid#status"])
        self.assertEqual(specs_by_key(after)["ent-pump#label"]["text"]["body"], "P-201")

    # Meshes and pieces

    def test_primitive_meshes_have_exact_extents_and_outward_faces(self):
        cases = [
            ({"shape": "box", "size_m": [0.45, 0.3, 0.4]}, (0.0, 0.0, 0.0)),
            ({"shape": "cylinder", "radius_m": 0.4, "length_m": 1.2, "segments": 48}, (0.0, 0.0, 0.0)),
            ({"shape": "sphere", "radius_m": 0.05, "segments": 32, "rings": 16}, (0.0, 0.0, 0.0)),
        ]
        for spec, centre in cases:
            mesh = geo.mesh_for(spec)
            vertices = mesh["vertices"]
            self.assertEqual(len(mesh["smooth"]), len(mesh["faces"]))
            for face in mesh["faces"]:
                points = [vertices[i] for i in face]
                middle = [sum(p[i] for p in points) / len(points) for i in range(3)]
                normal = newell_normal(points)
                self.assertGreater(sum(n * (m - c) for n, m, c in zip(normal, middle, centre)), 0, spec["shape"])
            if spec["shape"] == "box":
                self.assertEqual({tuple(abs(c) for c in v) for v in vertices}, {(0.225, 0.15, 0.2)})
            if spec["shape"] == "cylinder":
                for v in vertices:
                    self.assertAlmostEqual(math.hypot(v[0], v[1]), 0.4)
                    self.assertAlmostEqual(abs(v[2]), 0.6)
            if spec["shape"] == "sphere":
                for v in vertices:
                    self.assertAlmostEqual(math.dist(v, (0, 0, 0)), 0.05)
            if spec["shape"] != "cylinder":  # cylinder caps keep separate vertices for flat shading
                self.assertEqual(set(edge_counts(mesh["faces"]).values()), {2}, spec["shape"])

    def test_bend_mesh_follows_its_centreline(self):
        incoming, outgoing, bend, pipe = (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), 0.09045, 0.03015
        mesh = geo.bend_mesh(incoming, outgoing, bend, pipe, 12, 24)
        centreline = geo.bend_centreline(incoming, outgoing, bend, 12)
        self.assertClose(centreline[0], (0.0, 0.0, -bend))
        self.assertClose(centreline[-1], (bend, 0.0, 0.0))
        for k, point in enumerate(centreline):
            for vertex in mesh["vertices"][k * 24:(k + 1) * 24]:
                self.assertAlmostEqual(math.dist(vertex, point), pipe, places=9)
        for face in mesh["faces"]:
            points = [mesh["vertices"][i] for i in face]
            ring = face[0] // 24
            axis = [(a + b) / 2 for a, b in zip(centreline[ring], centreline[ring + 1])]
            middle = [sum(p[i] for p in points) / 4 for i in range(3)]
            self.assertGreater(sum(n * (m - a) for n, m, a in zip(newell_normal(points), middle, axis)), 0)
        open_edges = [edge for edge, count in edge_counts(mesh["faces"]).items() if count == 1]
        self.assertEqual(len(open_edges), 48)

    def test_pipe_pieces_handle_collinear_tight_and_reversed_corners(self):
        collinear = geo.pipe_pieces([(0, 0, 0), (0.5, 0, 0), (1.0, 0, 0)], 0.06)
        self.assertEqual([p["kind"] for p in collinear], ["segment", "segment"])
        self.assertClose(collinear[0]["end"], collinear[1]["start"])
        tight = geo.pipe_pieces([(0, 0, 0), (0.02, 0, 0), (0.02, 0.5, 0)], 0.06)
        self.assertEqual([p["kind"] for p in tight], ["segment", "joint", "segment"])
        self.assertClose(tight[0]["end"], (0.02, 0, 0))
        reversed_route = geo.pipe_pieces([(0, 0, 0), (0.5, 0, 0), (0.2, 0, 0)], 0.06)
        self.assertEqual([p["kind"] for p in reversed_route], ["segment", "joint", "segment"])
        short_middle = geo.pipe_pieces([(0, 0, 0), (0, 0, 0.3), (0.1, 0, 0.3), (0.1, 0, 0.6)], 0.06)
        elbows = [p for p in short_middle if p["kind"] == "elbow"]
        self.assertEqual([e["bend_radius"] for e in elbows], [0.05, 0.05])
        self.assertClose(elbows[0]["end"], elbows[1]["start"])

    # Import boundary

    def test_adapter_imports_without_blender(self):
        try:
            import bpy  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("bpy is importable here; the boundary is exercised by plain Python")
        from skid.adapters import blender

        self.assertNotIn("bpy", sys.modules)
        self.assertNotIn("skid.adapters.blender_support.scene", sys.modules)
        with self.assertRaises(RuntimeError):
            blender.build_blender(self.project, os.path.join(ROOT, "unused.blend"))


if __name__ == "__main__":
    unittest.main()
