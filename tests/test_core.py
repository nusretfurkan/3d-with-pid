"""Command transaction tests against the shared fixture, validator, templates and storage."""

import copy
import os
import tempfile
import unittest

from examples.make_fixture import make_document
from skid.clearance import port_direction
from skid.core.commands import apply_command
from skid.core.errors import CommandError, DocumentError
from skid.core.storage import load_document, save_document
from skid.core.validation import validate_document
from skid.templates import diagram_port_position, get_template, world_port_position

SNAPSHOT_KEYS = ("model", "pid", "physical")
VALVE = "ent-hv-103"
VALVE_IN = "port-hv-103-inlet"
VALVE_OUT = "port-hv-103-outlet"
DOWNSTREAM = "conn-l-106"
VALVE_SYMBOL = "sym-hv-103"
DOWNSTREAM_EDGE = "edge-l-106"
PLACEMENT_ASSUMPTION = "asm-hv-103-placement"
HUGE = 10**1000  # an integer beyond float range: math.isfinite raises OverflowError for it


def errors(diagnostics):
    return [d for d in diagnostics if d["severity"] == "error"]


def codes(diagnostics):
    return {d["code"] for d in diagnostics}


def content(project):
    return {key: project[key] for key in SNAPSHOT_KEYS}


def entity_id(document, tag):
    (match,) = [eid for eid, e in document["current"]["model"]["entities"].items() if e["tag"] == tag]
    return match


def port_id(document, owner, name):
    ports = document["current"]["model"]["ports"]
    (match,) = [pid for pid, p in ports.items() if p["entity_id"] == owner and p["name"] == name]
    return match


def owner_of(document, port):
    return document["current"]["model"]["ports"][port]["entity_id"]


def connection_between(document, upstream_tag, downstream_tag):
    upstream, downstream = entity_id(document, upstream_tag), entity_id(document, downstream_tag)
    (match,) = [cid for cid, c in document["current"]["model"]["connections"].items()
                if owner_of(document, c["from_port_id"]) == upstream
                and owner_of(document, c["to_port_id"]) == downstream]
    return match


def incident_connections(document, owner):
    return sorted(cid for cid, c in document["current"]["model"]["connections"].items()
                  if owner in (owner_of(document, c["from_port_id"]), owner_of(document, c["to_port_id"])))


def symbol_of(document, owner):
    (match,) = [sid for sid, s in document["current"]["pid"]["symbols"].items() if s["entity_id"] == owner]
    return match


def edge_of(document, connection):
    (match,) = [eid for eid, e in document["current"]["pid"]["edges"].items() if e["connection_id"] == connection]
    return match


def process_order(document):
    model = document["current"]["model"]
    by_from = {c["from_port_id"]: c for c in model["connections"].values()}
    entity, order = entity_id(document, "TK-101"), []
    while True:
        order.append(model["entities"][entity]["tag"])
        outs = [pid for pid, p in model["ports"].items() if p["entity_id"] == entity and p["direction"] == "out"]
        if not outs:
            return order
        (out,) = outs
        entity = owner_of(document, by_from[out]["to_port_id"])


def orthogonal(start, end):
    corners = [list(start), [end[0], start[1], start[2]], [end[0], end[1], start[2]], list(end)]
    path = [corners[0]]
    for point in corners[1:]:
        if max(abs(a - b) for a, b in zip(point, path[-1])) > 1e-9:
            path.append(point)
    return path


def guided(start, end):
    if abs(start[0] - end[0]) < 1e-9 or abs(start[1] - end[1]) < 1e-9:
        return [list(start), list(end)]
    mid = (start[0] + end[0]) / 2
    return [list(start), [mid, start[1]], [mid, end[1]], list(end)]


def route_points(document, connection):
    project = document["current"]
    c = project["model"]["connections"][connection]
    return orthogonal(world_port_position(project, c["from_port_id"]), world_port_position(project, c["to_port_id"]))


def tangent_points(document, connection):
    """Like ``route_points`` but leaving and entering 0.16 m along each port's outward axis.

    A corner path that turns at a port can run through that item's own nozzle or body, which the core now refuses.
    """
    project = document["current"]
    c = project["model"]["connections"][connection]
    ends = []
    for port in (c["from_port_id"], c["to_port_id"]):
        position, outward = world_port_position(project, port), port_direction(project, port)
        ends.append((list(position), [p + 0.16 * d for p, d in zip(position, outward)]))
    return [ends[0][0]] + orthogonal(ends[0][1], ends[1][1]) + [ends[1][0]]


def edge_points(document, edge):
    project = document["current"]
    record = project["pid"]["edges"][edge]
    c = project["model"]["connections"][record["connection_id"]]
    return guided(diagram_port_position(project, c["from_port_id"], record["from_symbol_id"]),
                  diagram_port_position(project, c["to_port_id"], record["to_symbol_id"]))


def make_command(document, kind, payload, revision=None, **extra):
    envelope = {"id": f"cmd-{kind}", "type": kind, "payload": payload,
                "expected_revision": document["current"]["revision"] if revision is None else revision}
    envelope.update(extra)
    return envelope


class CommandTestCase(unittest.TestCase):
    def setUp(self):
        self.doc = make_document()
        self.assertEqual(errors(validate_document(self.doc)), [])

    def apply(self, document, kind, payload, **extra):
        before = copy.deepcopy(document)
        new, diagnostics = apply_command(document, make_command(document, kind, payload, **extra))
        self.assertEqual(document, before, "apply_command mutated its input")
        self.assertEqual(errors(diagnostics), [])
        self.assertEqual(errors(validate_document(new)), [])
        return new, diagnostics

    def assertRejected(self, document, kind, payload, code=None, **extra):
        before = copy.deepcopy(document)
        command = make_command(document, kind, payload, **extra)
        sent = copy.deepcopy(command)
        with self.assertRaises(CommandError) as caught:
            apply_command(document, command)
        self.assertEqual(document, before, "rejected command changed the document")
        self.assertEqual(command, sent, "rejected command was mutated")
        self.assertTrue(errors(caught.exception.diagnostics))
        if code is not None:
            self.assertIn(code, codes(caught.exception.diagnostics))
        return caught.exception.diagnostics

    def assertCommitted(self, before, after):
        self.assertEqual(after["current"]["id"], before["current"]["id"])
        self.assertEqual(after["current"]["revision"], before["current"]["revision"] + 1)
        self.assertEqual(after["undo"], before["undo"] + [content(before["current"])])
        self.assertEqual(after["redo"], [])

    # Valve insertion payloads, by default between NRV-101 and HV-102.

    def valve_payload(self, document, placed=False, routes=False, route_error=0.0,
                      between=("NRV-101", "HV-102"), position=None):
        project = document["current"]
        model = project["model"]
        upstream = connection_between(document, *between)
        connection = model["connections"][upstream]
        hv102 = entity_id(document, "HV-102")
        old_edge = project["pid"]["edges"][edge_of(document, upstream)]
        entity = dict(copy.deepcopy(model["entities"][hv102]), id=VALVE, tag="HV-103")
        ports = [dict(copy.deepcopy(model["ports"][port_id(document, hv102, name)]),
                      id=VALVE_IN if name == "inlet" else VALVE_OUT, entity_id=VALVE)
                 for name in get_template("isolation_valve", 1)["ports"]]
        downstream = dict(copy.deepcopy(connection), id=DOWNSTREAM, tag="L-106",
                          from_port_id=VALVE_OUT, to_port_id=connection["to_port_id"])
        from_symbol = project["pid"]["symbols"][old_edge["from_symbol_id"]]
        to_symbol = project["pid"]["symbols"][old_edge["to_symbol_id"]]
        symbol = dict(copy.deepcopy(to_symbol), id=VALVE_SYMBOL, entity_id=VALVE, manual=False,
                      x=(from_symbol["x"] + to_symbol["x"]) / 2)
        placement = None
        if placed:
            # Unless given, on the pipe axis leaving the upstream entity, halfway along x.
            start = world_port_position(project, connection["from_port_id"])
            end = world_port_position(project, connection["to_port_id"])
            source = entity_id(document, between[0])
            placement = dict(copy.deepcopy(project["physical"]["placements"][source]), status="placed",
                             manual=True, assumption_id=PLACEMENT_ASSUMPTION,
                             position_m=position or [(start[0] + end[0]) / 2, start[1], start[2]])
            placement.pop("reason", None)

        # Resolve anchors and port positions on a scratch copy holding the new records.
        scratch = copy.deepcopy(document)
        scratch_project = scratch["current"]
        scratch_project["model"]["entities"][VALVE] = entity
        scratch_project["model"]["ports"].update({p["id"]: p for p in ports})
        scratch_project["model"]["connections"][DOWNSTREAM] = downstream
        scratch_project["pid"]["symbols"][VALVE_SYMBOL] = symbol
        if placement is not None:
            scratch_project["physical"]["placements"][VALVE] = placement
        down_edge = dict(copy.deepcopy(old_edge), id=DOWNSTREAM_EDGE, connection_id=DOWNSTREAM,
                         from_symbol_id=VALVE_SYMBOL, manual=False, status="current")
        down_edge.pop("reason", None)
        scratch_project["pid"]["edges"][DOWNSTREAM_EDGE] = down_edge
        down_edge["points"] = edge_points(scratch, DOWNSTREAM_EDGE)

        payload = {"connection_id": upstream, "entity": entity, "ports": ports, "downstream_connection": downstream,
                   "symbol": symbol, "downstream_edge": down_edge}
        extra = {}
        if placement is not None:
            payload["placement"] = placement
            extra["assumptions"] = {PLACEMENT_ASSUMPTION: {"description": f"HV-103 placed between {between[0]} "
                                                           f"and {between[1]} nozzles",
                                                           "source": "test prototype assumption"}}
        if routes:
            scratch_project["model"]["connections"][upstream] = dict(connection, to_port_id=VALVE_IN)
            assumption = project["physical"]["routes"][upstream]["assumption_id"]
            for key, cid in (("upstream_route", upstream), ("downstream_route", DOWNSTREAM)):
                points = route_points(scratch, cid)
                points[-1][2] += route_error
                payload[key] = {"points_m": points, "mode": "guided", "status": "current",
                                "assumption_id": assumption}
        return payload, extra

    def place_valve(self):
        payload, extra = self.valve_payload(self.doc, placed=True)
        return {"entity_id": VALVE, "placement": payload["placement"]}, extra


class FixtureTests(CommandTestCase):
    def test_fixture_is_complete_and_valid(self):
        project = self.doc["current"]
        self.assertEqual(process_order(self.doc), ["TK-101", "HV-101", "P-101", "NRV-101", "HV-102", "OUT-101"])
        self.assertEqual(self.doc["undo"], [])
        self.assertEqual(self.doc["redo"], [])
        self.assertTrue(all(p["status"] == "placed" for p in project["physical"]["placements"].values()))
        self.assertTrue(all(r["status"] == "current" for r in project["physical"]["routes"].values()))


class RenameAndParameterTests(CommandTestCase):
    def test_rename_preserves_identity_connectivity_and_layout(self):
        pump = entity_id(self.doc, "P-101")
        new, diagnostics = self.apply(self.doc, "rename_entity", {"entity_id": pump, "tag": "P-201"})
        self.assertCommitted(self.doc, new)
        before, after = self.doc["current"], new["current"]
        self.assertEqual(after["model"]["entities"][pump]["tag"], "P-201")
        renamed = copy.deepcopy(after)
        renamed["model"]["entities"][pump]["tag"] = "P-101"
        self.assertEqual(content(renamed), content(before))
        self.assertEqual(process_order(new), ["TK-101", "HV-101", "P-201", "NRV-101", "HV-102", "OUT-101"])
        self.assertNotIn("route_invalidated", codes(diagnostics))

    def test_rename_rejects_duplicate_tags_and_invalid_tags(self):
        pump = entity_id(self.doc, "P-101")
        line = self.doc["current"]["model"]["connections"][connection_between(self.doc, "TK-101", "HV-101")]
        for tag in ("TK-101", " tk-101 ", "hv-102", line["tag"].lower()):
            with self.subTest(tag=tag):
                self.assertRejected(self.doc, "rename_entity", {"entity_id": pump, "tag": tag}, "duplicate_tag")
        self.assertRejected(self.doc, "rename_entity", {"entity_id": pump, "tag": "  "}, "invalid_value")
        self.assertRejected(self.doc, "rename_entity", {"entity_id": "missing", "tag": "X-1"}, "unknown_reference")

    def test_stale_revision_and_unsupported_commands_are_rejected(self):
        pump = entity_id(self.doc, "P-101")
        rename = {"entity_id": pump, "tag": "P-201"}
        self.assertRejected(self.doc, "rename_entity", rename, "stale_revision", revision=7)
        self.assertRejected(self.doc, "delete_entity", {"entity_id": pump}, "unsupported_command")
        self.assertRejected(self.doc, "rename_entity", dict(rename, colour="red"), "unsupported_payload")
        self.assertRejected(self.doc, "rename_entity", rename, "invalid_command", author="someone")
        self.assertRejected(self.doc, "undo", {"steps": 1}, "unsupported_payload")
        self.assertRejected(self.doc, "undo", {}, "history_empty")
        self.assertRejected(self.doc, "redo", {}, "history_empty")

    def test_set_parameter_requires_supported_typed_provenanced_values(self):
        tank = entity_id(self.doc, "TK-101")
        quantity = copy.deepcopy(self.doc["current"]["model"]["entities"][tank]["parameters"]["height"])
        cases = [
            ("unsupported_property", "colour", quantity),
            ("invalid_unit", "height", dict(quantity, unit="mm")),
            ("invalid_value", "height", dict(quantity, value=-1.0)),
            ("invalid_value", "height", dict(quantity, value=True)),
            ("missing_provenance", "height", {"value": 1.3, "unit": "m"}),
            ("unsupported_payload", "height", dict(quantity, note="x")),
            ("unknown_assumption", "height", {"value": 1.3, "unit": "m", "assumption_id": "nope"}),
        ]
        for code, name, value in cases:
            with self.subTest(code=code, name=name):
                self.assertRejected(self.doc, "set_parameter", {"entity_id": tank, "name": name, "quantity": value},
                                    code)
        # Template rule: outlet_height must lie within the tank height.
        self.assertRejected(self.doc, "set_parameter",
                            {"entity_id": tank, "name": "outlet_height", "quantity": dict(quantity, value=5.0)})

    def test_dimension_change_invalidates_only_affected_routes(self):
        tank = entity_id(self.doc, "TK-101")
        outlet = port_id(self.doc, tank, "outlet")
        tank_line = connection_between(self.doc, "TK-101", "HV-101")
        old_routes = copy.deepcopy(self.doc["current"]["physical"]["routes"])
        old_position = world_port_position(self.doc["current"], outlet)
        quantity = copy.deepcopy(self.doc["current"]["model"]["entities"][tank]["parameters"]["outlet_height"])
        quantity["value"] -= 0.05
        new, diagnostics = self.apply(self.doc, "set_parameter",
                                      {"entity_id": tank, "name": "outlet_height", "quantity": quantity})
        self.assertCommitted(self.doc, new)
        self.assertEqual(new["current"]["model"]["entities"][tank]["parameters"]["outlet_height"], quantity)
        self.assertNotEqual(world_port_position(new["current"], outlet), old_position)
        routes = new["current"]["physical"]["routes"]
        self.assertEqual(routes[tank_line]["status"], "needs_reroute")
        self.assertEqual(routes[tank_line]["points_m"], old_routes[tank_line]["points_m"])
        self.assertTrue(routes[tank_line]["reason"])
        self.assertIn("route_invalidated", codes(diagnostics))
        for cid, route in routes.items():
            if cid != tank_line:
                self.assertEqual(route, old_routes[cid])

        # A dimension that leaves the port frames alone keeps routes current.
        height = copy.deepcopy(self.doc["current"]["model"]["entities"][tank]["parameters"]["height"])
        height["value"] += 0.1
        taller, _ = self.apply(self.doc, "set_parameter", {"entity_id": tank, "name": "height", "quantity": height})
        moved = world_port_position(taller["current"], outlet) != old_position
        expected = "needs_reroute" if moved else "current"
        self.assertEqual(taller["current"]["physical"]["routes"][tank_line]["status"], expected)

        # route_points' corner path would drop onto HV-101's side, its pipe wall 0.15 mm inside the valve body sphere.
        resolved, _ = self.apply(new, "set_route", {"connection_id": tank_line, "mode": "manual",
                                                    "points_m": tangent_points(new, tank_line),
                                                    "assumption_id": old_routes[tank_line]["assumption_id"]})
        self.assertEqual(resolved["current"]["physical"]["routes"][tank_line]["status"], "current")
        self.assertNotIn("reason", resolved["current"]["physical"]["routes"][tank_line])

    def test_incompatible_port_size_is_rejected_without_change(self):
        pump = entity_id(self.doc, "P-101")
        inlet = self.doc["current"]["model"]["ports"][port_id(self.doc, pump, "inlet")]
        payload = {"port_id": inlet["id"], "nominal_diameter_mm": 80, "assumption_id": inlet["assumption_id"]}
        self.assertRejected(self.doc, "set_port_size", payload, "dn_incompatible")
        self.assertRejected(self.doc, "set_port_size", dict(payload, nominal_diameter_mm=0), "invalid_value")

    def test_template_dn_is_the_only_supported_size(self):
        # Version 1 geometry has fixed nozzle radii, so an all-DN80 skid is unsupported even though consistent.
        pump = entity_id(self.doc, "P-101")
        inlet = self.doc["current"]["model"]["ports"][port_id(self.doc, pump, "inlet")]
        self.assertRejected(self.doc, "set_port_size", {"port_id": inlet["id"], "nominal_diameter_mm": 80,
                                                        "assumption_id": inlet["assumption_id"]},
                            "unsupported_nominal_diameter")

        def all_dn80(sections):
            model = sections["model"]
            for record in (*model["ports"].values(), *model["connections"].values()):
                record["nominal_diameter_mm"] = 80

        current = copy.deepcopy(self.doc)
        all_dn80(current["current"])
        history = copy.deepcopy(self.doc)
        history["undo"] = [copy.deepcopy(content(self.doc["current"]))]
        history["current"]["revision"] = 1
        all_dn80(history["undo"][0])
        for name, document in (("current", current), ("undo", history)):
            with self.subTest(document=name):
                diagnostics = self.assertRejected(document, "rename_entity", {"entity_id": pump, "tag": "P-201"},
                                                  "invalid_document")
                self.assertIn("unsupported_nominal_diameter", codes(diagnostics))
                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, "pump_skid.json")
                    with self.assertRaises(DocumentError):
                        save_document(path, document, None)
                    self.assertFalse(os.path.exists(path))
        self.assertRejected(history, "undo", {}, "invalid_document")

    def test_out_of_range_numbers_are_rejected_in_every_numeric_payload(self):
        project = self.doc["current"]
        tank, pump = entity_id(self.doc, "TK-101"), entity_id(self.doc, "P-101")
        height = project["model"]["entities"][tank]["parameters"]["height"]
        placement = project["physical"]["placements"][pump]
        symbol_id = symbol_of(self.doc, pump)
        symbol = project["pid"]["symbols"][symbol_id]
        inlet = project["model"]["ports"][port_id(self.doc, pump, "inlet")]
        line = connection_between(self.doc, "P-101", "NRV-101")
        route = project["physical"]["routes"][line]
        edge = edge_of(self.doc, line)
        insert, insert_extra = self.valve_payload(self.doc, placed=True, routes=True)

        def points(values, value):
            changed = copy.deepcopy(values)
            changed[-1][0] = value
            return changed

        def inserted(key, change):
            changed = copy.deepcopy(insert)
            changed[key] = change(changed[key])
            return changed

        for value in (HUGE, -HUGE):
            cases = [
                ("quantity", "set_parameter", {"entity_id": tank, "name": "height",
                                               "quantity": dict(height, value=value)}, "invalid_value"),
                ("position", "move_equipment", {"entity_id": pump,
                                                "placement": dict(placement, position_m=[value, 0.0, 0.0])},
                 "invalid_value"),
                ("rotation", "place_equipment", {"entity_id": pump,
                                                 "placement": dict(placement, rotation_deg=[0.0, 0.0, value])},
                 "invalid_value"),
                ("symbol x", "move_symbol", {"symbol_id": symbol_id, "x": value, "y": symbol["y"],
                                             "rotation_deg": symbol["rotation_deg"]}, "invalid_value"),
                ("symbol rotation", "move_symbol", {"symbol_id": symbol_id, "x": symbol["x"], "y": symbol["y"],
                                                    "rotation_deg": value}, "invalid_value"),
                ("port size", "set_port_size", {"port_id": inlet["id"], "nominal_diameter_mm": value,
                                                "assumption_id": inlet["assumption_id"]}, "invalid_value"),
                ("route point", "set_route", {"connection_id": line, "points_m": points(route["points_m"], value),
                                              "mode": route["mode"], "assumption_id": route["assumption_id"]},
                 "invalid_route"),
                ("diagram point", "set_diagram_route",
                 {"edge_id": edge, "points": points(project["pid"]["edges"][edge]["points"], value)}, "invalid_route"),
            ]
            insert_cases = [
                ("inserted quantity", inserted("entity", lambda e: dict(e, parameters=dict(
                    e["parameters"], length=dict(e["parameters"]["length"], value=value)))), "invalid_value"),
                ("inserted placement", inserted("placement", lambda p: dict(p, position_m=[value, 0.0, 0.0])),
                 "invalid_value"),
                ("inserted rotation", inserted("placement", lambda p: dict(p, rotation_deg=[0.0, 0.0, value])),
                 "invalid_value"),
                ("inserted symbol", inserted("symbol", lambda s: dict(s, x=value)), "invalid_value"),
                ("inserted edge point", inserted("downstream_edge", lambda e: dict(e, points=points(e["points"], value))),
                 "invalid_route"),
                ("inserted route point", inserted("upstream_route",
                                                  lambda r: dict(r, points_m=points(r["points_m"], value))),
                 "invalid_route"),
            ]
            for name, kind, payload, code in cases:
                with self.subTest(case=name, negative=value < 0):
                    self.assertRejected(self.doc, kind, payload, code)
            for name, payload, code in insert_cases:
                with self.subTest(case=name, negative=value < 0):
                    self.assertRejected(self.doc, "insert_valve", payload, code, **insert_extra)


class InsertValveTests(CommandTestCase):
    def test_insert_without_placement_preserves_order_identity_and_pending_state(self):
        payload, _ = self.valve_payload(self.doc)
        upstream = payload["connection_id"]
        before = self.doc["current"]
        original = copy.deepcopy(before["model"]["connections"][upstream])
        upstream_edge = edge_of(self.doc, upstream)
        old_edge = copy.deepcopy(before["pid"]["edges"][upstream_edge])
        old_route = copy.deepcopy(before["physical"]["routes"][upstream])

        new, diagnostics = self.apply(self.doc, "insert_valve", payload)
        self.assertCommitted(self.doc, new)
        after = new["current"]
        self.assertEqual(process_order(new),
                         ["TK-101", "HV-101", "P-101", "NRV-101", "HV-103", "HV-102", "OUT-101"])
        connections = after["model"]["connections"]
        self.assertEqual(connections[upstream], dict(original, to_port_id=VALVE_IN))
        self.assertEqual(connections[DOWNSTREAM]["from_port_id"], VALVE_OUT)
        self.assertEqual(connections[DOWNSTREAM]["to_port_id"], original["to_port_id"])
        for collection in ("entities", "ports", "connections"):
            self.assertLessEqual(set(before["model"][collection]), set(after["model"][collection]))

        edges = after["pid"]["edges"]
        self.assertEqual(edges[upstream_edge]["connection_id"], upstream)
        self.assertEqual(edges[upstream_edge]["from_symbol_id"], old_edge["from_symbol_id"])
        self.assertEqual(edges[upstream_edge]["to_symbol_id"], VALVE_SYMBOL)
        self.assertEqual(edges[DOWNSTREAM_EDGE]["from_symbol_id"], VALVE_SYMBOL)
        self.assertEqual(edges[DOWNSTREAM_EDGE]["to_symbol_id"], old_edge["to_symbol_id"])
        self.assertEqual(edges[DOWNSTREAM_EDGE]["status"], "current")
        if old_edge["manual"]:
            self.assertEqual(edges[upstream_edge]["points"], old_edge["points"])
            self.assertEqual(edges[upstream_edge]["status"], "needs_reroute")
        else:
            self.assertEqual(edges[upstream_edge]["status"], "current")
            self.assertEqual(edges[upstream_edge]["points"], edge_points(new, upstream_edge))

        placement = after["physical"]["placements"][VALVE]
        self.assertEqual((placement["status"], placement["position_m"], placement["rotation_deg"]),
                         ("unresolved", None, None))
        self.assertTrue(placement["reason"])
        routes = after["physical"]["routes"]
        self.assertEqual(routes[upstream]["status"], "unresolved")
        self.assertEqual(routes[upstream]["points_m"], old_route["points_m"])
        self.assertEqual(routes[DOWNSTREAM]["status"], "unresolved")
        self.assertTrue(routes[upstream]["reason"] and routes[DOWNSTREAM]["reason"])
        self.assertIn("placement_unresolved", codes(diagnostics))
        self.assertIn("route_invalidated", codes(diagnostics))
        # The prior route is kept in undo history.
        self.assertEqual(new["undo"][-1]["physical"]["routes"][upstream], old_route)

    def test_pending_insertion_resolves_with_placement_then_routes(self):
        payload, _ = self.valve_payload(self.doc)
        upstream = payload["connection_id"]
        pending, _ = self.apply(self.doc, "insert_valve", payload)
        place, extra = self.place_valve()
        placed, diagnostics = self.apply(pending, "place_equipment", place, **extra)
        self.assertCommitted(pending, placed)
        routes = placed["current"]["physical"]["routes"]
        self.assertEqual(placed["current"]["physical"]["placements"][VALVE]["status"], "placed")
        self.assertIn(PLACEMENT_ASSUMPTION, placed["current"]["model"]["assumptions"])
        self.assertEqual({routes[upstream]["status"], routes[DOWNSTREAM]["status"]}, {"needs_reroute"})
        self.assertIn("route_invalidated", codes(diagnostics))

        document = placed
        for cid in (upstream, DOWNSTREAM):
            document, diagnostics = self.apply(document, "set_route", {
                "connection_id": cid, "points_m": route_points(document, cid), "mode": "guided",
                "assumption_id": routes[upstream]["assumption_id"]})
        routes = document["current"]["physical"]["routes"]
        self.assertEqual({routes[upstream]["status"], routes[DOWNSTREAM]["status"]}, {"current"})
        self.assertEqual(document["current"]["revision"], self.doc["current"]["revision"] + 4)
        self.assertFalse({"route_invalidated", "placement_unresolved"} & codes(diagnostics))

    def test_insert_with_valid_explicit_split_is_current(self):
        payload, extra = self.valve_payload(self.doc, placed=True, routes=True)
        new, diagnostics = self.apply(self.doc, "insert_valve", payload, **extra)
        physical = new["current"]["physical"]
        self.assertEqual(physical["placements"][VALVE], payload["placement"])
        for key, cid in (("upstream_route", payload["connection_id"]), ("downstream_route", DOWNSTREAM)):
            self.assertEqual(physical["routes"][cid], payload[key])
        self.assertFalse({"route_invalidated", "placement_unresolved"} & codes(diagnostics))

    def test_insert_with_invalid_split_commits_topology_with_unresolved_routes(self):
        upstream = connection_between(self.doc, "NRV-101", "HV-102")
        old_route = copy.deepcopy(self.doc["current"]["physical"]["routes"][upstream])
        payload, extra = self.valve_payload(self.doc, placed=True, routes=True, route_error=0.01)
        new, diagnostics = self.apply(self.doc, "insert_valve", payload, **extra)
        routes = new["current"]["physical"]["routes"]
        self.assertEqual(routes[upstream]["status"], "unresolved")
        self.assertEqual(routes[upstream]["points_m"], old_route["points_m"])
        self.assertEqual(routes[DOWNSTREAM]["status"], "unresolved")
        self.assertEqual(routes[DOWNSTREAM]["points_m"], payload["downstream_route"]["points_m"])
        self.assertIn("route_invalidated", codes(diagnostics))
        self.assertEqual(process_order(new)[4], "HV-103")

    def test_rejected_insertions_are_atomic(self):
        pump = entity_id(self.doc, "P-101")
        document, _ = self.apply(self.doc, "rename_entity", {"entity_id": pump, "tag": "P-201"})
        payload, extra = self.valve_payload(document, placed=True)
        hv102 = entity_id(document, "HV-102")
        nrv_inlet = port_id(document, entity_id(document, "NRV-101"), "inlet")

        def variant(**changes):
            changed = copy.deepcopy(payload)
            for key, value in changes.items():
                changed[key] = value(changed[key]) if callable(value) else value
            return changed

        def with_dn(ports):
            return [dict(port, nominal_diameter_mm=80) for port in ports]

        cases = [
            ("duplicate_id", variant(entity=lambda e: dict(e, id=hv102)), extra),
            ("duplicate_id", variant(symbol=lambda s: dict(s, id=symbol_of(document, hv102))), extra),
            ("duplicate_tag", variant(entity=lambda e: dict(e, tag="hv-102")), extra),
            ("duplicate_tag", variant(downstream_connection=lambda c: dict(c, tag="HV-103")), extra),
            ("topology_mismatch", variant(downstream_connection=lambda c: dict(c, to_port_id=nrv_inlet)), extra),
            ("topology_mismatch", variant(ports=lambda ports: ports[:1]), extra),
            ("unsupported_property", variant(entity=lambda e: dict(e, kind="tank")), extra),
            ("unsupported_payload", variant(entity=lambda e: dict(e, colour="red")), extra),
            ("unknown_reference", variant(connection_id="missing"), extra),
            ("unknown_assumption", payload, {}),
            ("assumption_conflict", payload,
             {"assumptions": {next(iter(document["current"]["model"]["assumptions"])):
                              {"description": "overwrite", "source": "test"}}}),
            (None, variant(ports=with_dn), extra),
        ]
        for code, changed, envelope_extra in cases:
            with self.subTest(code=code):
                self.assertRejected(document, "insert_valve", changed, code, **envelope_extra)
        unused = {"asm-unused": {"description": "never referenced", "source": "test"}}
        self.assertRejected(document, "rename_entity", {"entity_id": pump, "tag": "P-301"},
                            "unreferenced_assumption", assumptions=unused)

    def test_malformed_inserted_parameters_are_rejected_before_geometry(self):
        for placed in (False, True):
            payload, extra = self.valve_payload(self.doc, placed=placed)
            parameters = payload["entity"]["parameters"]
            length = parameters["length"]
            cases = [
                ("unsupported_payload", {"length": 0.16, "body_diameter": 0.1}),
                ("unsupported_payload", [length]),
                ("unsupported_payload", {"length": length}),
                ("unsupported_payload", dict(parameters, colour=length)),
                ("invalid_value", dict(parameters, length=dict(length, value=0))),
                ("invalid_value", dict(parameters, length=dict(length, value="0.16"))),
                ("invalid_unit", dict(parameters, length=dict(length, unit="mm"))),
                ("missing_provenance", dict(parameters, length={"value": 0.16, "unit": "m"})),
                ("unknown_assumption", dict(parameters, length=dict(length, assumption_id="missing"))),
            ]
            for code, value in cases:
                with self.subTest(placed=placed, parameters=value):
                    changed = copy.deepcopy(payload)
                    changed["entity"]["parameters"] = value
                    self.assertRejected(self.doc, "insert_valve", changed, code, **extra)

    def test_insert_on_manual_discharge_line_preserves_manual_paths(self):
        upstream = connection_between(self.doc, "P-101", "NRV-101")
        self.assertEqual(upstream, "conn-discharge-1")
        before = self.doc["current"]
        upstream_edge = edge_of(self.doc, upstream)
        old_edge = copy.deepcopy(before["pid"]["edges"][upstream_edge])
        old_route = copy.deepcopy(before["physical"]["routes"][upstream])
        self.assertEqual((old_edge["manual"], old_route["mode"]), (True, "manual"), "fixture line should be manual")
        original = copy.deepcopy(before["model"]["connections"][upstream])
        kept = ("points_m", "mode", "assumption_id")

        for placed in (False, True):
            with self.subTest(placed=placed):
                # When placed, the valve sits on the manual route's horizontal run above the motor.
                payload, extra = self.valve_payload(self.doc, placed=placed, between=("P-101", "NRV-101"),
                                                    position=[1.7, 0.0, 0.7])
                new, diagnostics = self.apply(self.doc, "insert_valve", payload, **extra)
                self.assertCommitted(self.doc, new)
                after = new["current"]
                self.assertEqual(process_order(new),
                                 ["TK-101", "HV-101", "P-101", "HV-103", "NRV-101", "HV-102", "OUT-101"])
                connections = after["model"]["connections"]
                self.assertEqual(connections[upstream], dict(original, to_port_id=VALVE_IN))
                self.assertEqual((connections[DOWNSTREAM]["from_port_id"], connections[DOWNSTREAM]["to_port_id"]),
                                 (VALVE_OUT, original["to_port_id"]))

                edges = after["pid"]["edges"]
                edge = edges[upstream_edge]
                self.assertEqual((edge["connection_id"], edge["from_symbol_id"], edge["to_symbol_id"]),
                                 (upstream, old_edge["from_symbol_id"], VALVE_SYMBOL))
                self.assertEqual((edge["points"], edge["manual"], edge["status"]),
                                 (old_edge["points"], True, "needs_reroute"))
                self.assertTrue(edge["reason"])
                self.assertEqual((edges[DOWNSTREAM_EDGE]["from_symbol_id"], edges[DOWNSTREAM_EDGE]["to_symbol_id"]),
                                 (VALVE_SYMBOL, old_edge["to_symbol_id"]))

                routes = after["physical"]["routes"]
                route = routes[upstream]
                status = "needs_reroute" if placed else "unresolved"
                self.assertEqual({key: route[key] for key in kept}, {key: old_route[key] for key in kept})
                self.assertEqual((route["status"], routes[DOWNSTREAM]["status"]), (status, status))
                self.assertTrue(route["reason"] and routes[DOWNSTREAM]["reason"])
                self.assertTrue({"diagram_edge_invalidated", "route_invalidated"} <= codes(diagnostics))

                # Unaffected manual and guided presentation is untouched.
                for eid, old in before["pid"]["edges"].items():
                    if eid != upstream_edge:
                        self.assertEqual(edges[eid], old)
                for sid, old in before["pid"]["symbols"].items():
                    self.assertEqual(after["pid"]["symbols"][sid], old)
                for cid, old in before["physical"]["routes"].items():
                    if cid != upstream:
                        self.assertEqual(routes[cid], old)
                self.assertEqual(new["undo"][-1], content(before))

                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, "pump_skid.json")
                    save_document(path, new, None)
                    undone, _ = self.apply(load_document(path), "undo", {})
                    self.assertEqual(content(undone["current"]), content(before))
                    save_document(path, undone, new["current"]["revision"])
                    redone, _ = self.apply(load_document(path), "redo", {})
                    self.assertEqual(content(redone["current"]), content(after))
                    save_document(path, redone, undone["current"]["revision"])
                    reloaded = load_document(path)
                self.assertEqual(reloaded, redone)
                self.assertEqual(reloaded["current"]["revision"], before["revision"] + 3)
                restored = reloaded["current"]
                self.assertEqual(restored["pid"]["edges"][upstream_edge], edge)
                self.assertEqual(restored["physical"]["routes"][upstream], route)
                self.assertEqual(process_order(reloaded)[2:5], ["P-101", "HV-103", "NRV-101"])


class MoveTests(CommandTestCase):
    def test_move_equipment_changes_placement_only_and_flags_incident_routes(self):
        pump = entity_id(self.doc, "P-101")
        before = self.doc["current"]
        placement = copy.deepcopy(before["physical"]["placements"][pump])
        placement["position_m"][0] += 0.25
        new, diagnostics = self.apply(self.doc, "move_equipment", {"entity_id": pump, "placement": placement})
        self.assertCommitted(self.doc, new)
        after = new["current"]
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["pid"], before["pid"])
        self.assertEqual(after["physical"]["placements"][pump], placement)
        incident = incident_connections(self.doc, pump)
        self.assertEqual(len(incident), 2)
        for cid, route in after["physical"]["routes"].items():
            old = before["physical"]["routes"][cid]
            if cid in incident:
                self.assertEqual(route["status"], "needs_reroute")
                self.assertEqual(route["points_m"], old["points_m"])
                self.assertEqual(route["mode"], old["mode"])
                self.assertTrue(route["reason"])
            else:
                self.assertEqual(route, old)
        self.assertIn("route_invalidated", codes(diagnostics))

        document = new
        for cid in incident:
            document, _ = self.apply(document, "set_route", {
                "connection_id": cid, "points_m": tangent_points(document, cid), "mode": "manual",
                "assumption_id": before["physical"]["routes"][cid]["assumption_id"]})
            self.assertEqual(document["current"]["physical"]["routes"][cid]["status"], "current")
        self.assertEqual(document["current"]["pid"], before["pid"])

    def test_move_symbol_changes_presentation_only_and_preserves_manual_paths(self):
        pump = entity_id(self.doc, "P-101")
        symbol_id = symbol_of(self.doc, pump)
        document = self.doc
        before = document["current"]
        incident = {eid: e["manual"] for eid, e in before["pid"]["edges"].items()
                    if symbol_id in (e["from_symbol_id"], e["to_symbol_id"])}
        self.assertEqual(set(incident.values()), {True, False}, "fixture should give the pump guided and manual edges")
        symbol = before["pid"]["symbols"][symbol_id]
        move = {"symbol_id": symbol_id, "x": symbol["x"] + 40, "y": symbol["y"] + 12,
                "rotation_deg": symbol["rotation_deg"]}
        new, diagnostics = self.apply(document, "move_symbol", move)
        self.assertCommitted(document, new)
        after = new["current"]
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["physical"], before["physical"])
        moved = after["pid"]["symbols"][symbol_id]
        self.assertEqual((moved["x"], moved["y"], moved["manual"]), (move["x"], move["y"], True))
        for sid, other in after["pid"]["symbols"].items():
            if sid != symbol_id:
                self.assertEqual(other, before["pid"]["symbols"][sid])
        repairs = []
        for eid, edge in after["pid"]["edges"].items():
            old = before["pid"]["edges"][eid]
            if symbol_id not in (old["from_symbol_id"], old["to_symbol_id"]):
                self.assertEqual(edge, old)
            elif old["manual"]:
                self.assertEqual(edge["points"], old["points"])
                self.assertEqual(edge["status"], "needs_reroute")
                self.assertTrue(edge["reason"])
                repairs.append(eid)
            else:
                self.assertEqual(edge["status"], "current")
                self.assertEqual(edge["points"], edge_points(new, eid))
        self.assertEqual(sorted(repairs), sorted(eid for eid, manual in incident.items() if manual))
        self.assertIn("diagram_edge_invalidated", codes(diagnostics))

        repaired = new
        for eid in repairs:
            repaired, _ = self.apply(repaired, "set_diagram_route", {"edge_id": eid,
                                                                     "points": edge_points(repaired, eid)})
            self.assertEqual(repaired["current"]["pid"]["edges"][eid]["status"], "current")
            self.assertNotIn("reason", repaired["current"]["pid"]["edges"][eid])
        self.assertEqual(repaired["current"]["physical"], before["physical"])

    def test_move_symbol_regenerates_guided_edges(self):
        edges = self.doc["current"]["pid"]["edges"]
        guided_edges = sorted(eid for eid, e in edges.items() if not e["manual"])
        if not guided_edges:
            self.skipTest("fixture has no guided diagram edge")
        edge = edges[guided_edges[0]]
        symbol = self.doc["current"]["pid"]["symbols"][edge["to_symbol_id"]]
        new, _ = self.apply(self.doc, "move_symbol", {"symbol_id": edge["to_symbol_id"], "x": symbol["x"] + 8,
                                                      "y": symbol["y"] + 30, "rotation_deg": symbol["rotation_deg"]})
        regenerated = new["current"]["pid"]["edges"][guided_edges[0]]
        self.assertEqual(regenerated["status"], "current")
        self.assertEqual(regenerated["points"], edge_points(new, guided_edges[0]))

    def test_explicit_routes_are_validated(self):
        line = connection_between(self.doc, "P-101", "NRV-101")
        route = self.doc["current"]["physical"]["routes"][line]
        good = route_points(self.doc, line)
        payload = {"connection_id": line, "points_m": good, "mode": "manual", "assumption_id": route["assumption_id"]}
        off_end = copy.deepcopy(good)
        off_end[-1][0] += 0.001
        diagonal = [good[0], good[-1]] if len(good) > 2 else [good[0], [good[0][0] + 0.1, good[0][1] + 0.1,
                                                                        good[0][2]], good[-1]]
        for code, changed in (("invalid_route", dict(payload, points_m=off_end)),
                              ("invalid_route", dict(payload, points_m=diagonal)),
                              ("invalid_route", dict(payload, points_m=[good[0]])),
                              ("invalid_value", dict(payload, mode="auto")),
                              ("unknown_assumption", dict(payload, assumption_id="missing"))):
            with self.subTest(code=code):
                self.assertRejected(self.doc, "set_route", changed, code)
        edge = edge_of(self.doc, line)
        bad_edge = copy.deepcopy(self.doc["current"]["pid"]["edges"][edge]["points"])
        bad_edge[-1][1] += 5
        self.assertRejected(self.doc, "set_diagram_route", {"edge_id": edge, "points": bad_edge}, "invalid_route")

        pending, _ = self.apply(self.doc, "insert_valve", self.valve_payload(self.doc)[0])
        self.assertRejected(pending, "set_route", {"connection_id": DOWNSTREAM, "points_m": good, "mode": "guided",
                                                   "assumption_id": route["assumption_id"]}, "invalid_route")


class HistoryTests(CommandTestCase):
    def test_no_op_commands_keep_revision_and_history(self):
        pump = entity_id(self.doc, "P-101")
        symbol_id = symbol_of(self.doc, pump)
        symbol = self.doc["current"]["pid"]["symbols"][symbol_id]
        line = connection_between(self.doc, "P-101", "NRV-101")
        route = self.doc["current"]["physical"]["routes"][line]
        placement = self.doc["current"]["physical"]["placements"][pump]
        commands = [
            ("rename_entity", {"entity_id": pump, "tag": "P-101"}),
            ("move_symbol", {"symbol_id": symbol_id, "x": symbol["x"], "y": symbol["y"],
                             "rotation_deg": symbol["rotation_deg"]}),
            ("move_equipment", {"entity_id": pump, "placement": placement}),
            ("set_route", {"connection_id": line, "points_m": route["points_m"], "mode": route["mode"],
                           "assumption_id": route["assumption_id"]}),
        ]
        for kind, payload in commands:
            with self.subTest(kind=kind):
                result, diagnostics = apply_command(self.doc, make_command(self.doc, kind, payload))
                self.assertEqual(result, self.doc)
                self.assertIn("no_op", codes(diagnostics))
                self.assertEqual(errors(diagnostics), [])
        self.assertRejected(self.doc, "rename_entity", commands[0][1], "stale_revision", revision=3)

    def test_history_stacks_must_be_lists(self):
        pump = entity_id(self.doc, "P-101")
        renamed, _ = self.apply(self.doc, "rename_entity", {"entity_id": pump, "tag": "P-201"})
        undone, _ = self.apply(renamed, "undo", {})
        for stack, source in (("undo", renamed), ("redo", undone)):
            document = copy.deepcopy(source)
            document[stack] = tuple(document[stack])
            for kind, payload in (("rename_entity", {"entity_id": pump, "tag": "P-301"}), ("undo", {}),
                                  ("redo", {})):
                with self.subTest(stack=stack, kind=kind):
                    diagnostics = self.assertRejected(document, kind, payload, "invalid_document")
                    self.assertIn("invalid_type", codes(diagnostics))

    def test_undo_redo_across_save_and_reload(self):
        pump = entity_id(self.doc, "P-101")
        insert, insert_extra = self.valve_payload(self.doc, placed=True, routes=True)
        symbol_id = symbol_of(self.doc, pump)
        symbol = self.doc["current"]["pid"]["symbols"][symbol_id]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pump_skid.json")
            save_document(path, self.doc, None)
            document = self.doc
            states, revisions = [content(self.doc["current"])], [self.doc["current"]["revision"]]

            def step(kind, payload, **extra):
                nonlocal document
                disk = load_document(path)
                self.assertEqual(disk, document)
                document, _ = self.apply(disk, kind, payload, **extra)
                save_document(path, document, disk["current"]["revision"])
                revisions.append(document["current"]["revision"])
                return document

            step("rename_entity", {"entity_id": pump, "tag": "P-201"})
            states.append(content(document["current"]))
            step("insert_valve", insert, **insert_extra)
            states.append(content(document["current"]))
            step("move_symbol", {"symbol_id": symbol_id, "x": symbol["x"] + 40, "y": symbol["y"] - 20,
                                 "rotation_deg": symbol["rotation_deg"]})
            states.append(content(document["current"]))
            self.assertEqual(len(document["undo"]), 3)

            for expected in (states[2], states[1], states[0]):
                step("undo", {})
                self.assertEqual(content(document["current"]), expected)
            self.assertNotIn(VALVE, document["current"]["model"]["entities"])
            self.assertEqual((len(document["undo"]), len(document["redo"])), (0, 3))

            for expected in (states[1], states[2], states[3]):
                step("redo", {})
                self.assertEqual(content(document["current"]), expected)
            reloaded = load_document(path)
            self.assertEqual(reloaded, document)
            restored = reloaded["current"]
            self.assertEqual(restored["model"]["entities"][pump]["tag"], "P-201")
            self.assertEqual(restored["model"]["connections"][DOWNSTREAM]["from_port_id"], VALVE_OUT)
            self.assertEqual(restored["pid"]["symbols"][symbol_id]["manual"], True)
            self.assertEqual(restored["pid"]["symbols"][symbol_id]["x"], symbol["x"] + 40)
            self.assertEqual(process_order(reloaded)[3:5], ["NRV-101", "HV-103"])

            step("undo", {})
            step("rename_entity", {"entity_id": pump, "tag": "P-301"})
            self.assertEqual(document["redo"], [])
            self.assertEqual(revisions, list(range(revisions[0], revisions[0] + len(revisions))))
            self.assertEqual(load_document(path)["current"]["id"], self.doc["current"]["id"])


if __name__ == "__main__":
    unittest.main()
