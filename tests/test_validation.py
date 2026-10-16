"""Validation contract tests against the shared pump-skid fixture.

Each malformed variant is a copy of ``examples.make_fixture.make_document()``
altered in one place. Validation must report it as diagnostics without raising
or mutating its input.
"""

import copy
import hashlib
import json
import math
import unittest

from examples.make_fixture import make_document
from skid.core import canonical_digest, validate_document, validate_project
from skid.templates import world_port_position

NAN = float("nan")
INF = float("inf")
MAPS = (
    ("model", "entities"),
    ("model", "ports"),
    ("model", "connections"),
    ("model", "assumptions"),
    ("pid", "symbols"),
    ("pid", "edges"),
    ("physical", "placements"),
    ("physical", "routes"),
)
GARBAGE = (None, "", "x", 0, -1, 1.5, True, [], [None], {}, {"x": 1}, NAN, INF, 10**400)
OPTIONAL_FIELDS = ("observes", "reason")


def errors(diagnostics):
    return {d["code"] for d in diagnostics if d["severity"] == "error"}


def warnings(diagnostics):
    return {d["code"] for d in diagnostics if d["severity"] == "warning"}


def section(document, name):
    """Return one map of the current project, for example ``section(doc, "ports")``."""
    project = document["current"]
    for part in ("model", "pid", "physical"):
        if name in project[part]:
            return project[part][name]
    raise KeyError(name)


def entity_of_kind(document, kind):
    return next(eid for eid, entity in section(document, "entities").items() if entity["kind"] == kind)


def port_of(document, entity_id, name):
    return next(
        pid
        for pid, port in section(document, "ports").items()
        if port["entity_id"] == entity_id and port["name"] == name
    )


def connection_at(document, port_id):
    return next(
        cid
        for cid, connection in section(document, "connections").items()
        if port_id in (connection["from_port_id"], connection["to_port_id"])
    )


def connections_of(document, entity_id):
    ports = section(document, "ports")
    return [
        cid
        for cid, connection in section(document, "connections").items()
        if entity_id in (ports[connection["from_port_id"]]["entity_id"], ports[connection["to_port_id"]]["entity_id"])
    ]


def edges_of(document, connection_id):
    return [eid for eid, edge in section(document, "edges").items() if edge["connection_id"] == connection_id]


def symbols_of(document, entity_id):
    return [sid for sid, symbol in section(document, "symbols").items() if symbol["entity_id"] == entity_id]


def remove_connection(document, connection_id):
    del section(document, "connections")[connection_id]
    for edge_id in edges_of(document, connection_id):
        del section(document, "edges")[edge_id]
    section(document, "routes").pop(connection_id, None)


def snapshot_of(document):
    project = document["current"]
    return copy.deepcopy({key: project[key] for key in ("model", "pid", "physical")})


def shift(points, index, axis, delta):
    """Return a copy of a point list with one coordinate moved."""
    moved = [list(point) for point in points]
    moved[index][axis] += delta
    return moved


def reordered(value):
    if isinstance(value, dict):
        return {key: reordered(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reordered(item) for item in value]
    return value


class ValidationTestCase(unittest.TestCase):
    def setUp(self):
        self.doc = make_document()

    def check_well_formed(self, diagnostics):
        self.assertIsInstance(diagnostics, list)
        for diagnostic in diagnostics:
            self.assertEqual(set(diagnostic), {"code", "severity", "entity_ids", "message"}, diagnostic)
            self.assertIsInstance(diagnostic["code"], str)
            self.assertIn(diagnostic["severity"], ("error", "warning"))
            self.assertIsInstance(diagnostic["entity_ids"], list)
            for item in diagnostic["entity_ids"]:
                self.assertIsInstance(item, str)
            self.assertIsInstance(diagnostic["message"], str)
            self.assertNotEqual(diagnostic["code"], "validation_failed", diagnostic["message"])

    def validate(self, document):
        before = copy.deepcopy(document)
        diagnostics = validate_document(document)
        self.assertEqual(document, before, "validation mutated its input")
        self.check_well_formed(diagnostics)
        return diagnostics

    def assertRejects(self, code, document=None):
        diagnostics = self.validate(self.doc if document is None else document)
        self.assertIn(code, errors(diagnostics), diagnostics)
        return diagnostics

    def assertCommittable(self, document=None):
        """Assert there are no errors and return the warning codes."""
        diagnostics = self.validate(self.doc if document is None else document)
        self.assertEqual(errors(diagnostics), set(), diagnostics)
        return warnings(diagnostics)


class FixtureBaselineTests(ValidationTestCase):
    def test_fixture_is_valid_and_complete(self):
        self.assertEqual(self.validate(self.doc), [])

    def test_fixture_is_strict_json(self):
        self.assertEqual(json.loads(json.dumps(self.doc, allow_nan=False)), self.doc)

    def test_validate_project_reports_current_diagnostics(self):
        symbols = section(self.doc, "symbols")
        symbols[next(iter(symbols))]["x"] = NAN
        diagnostics = validate_project(self.doc["current"])
        self.check_well_formed(diagnostics)
        self.assertIn("invalid_number", errors(diagnostics))
        self.assertEqual(diagnostics, validate_document(self.doc))

    def test_validate_project_rejects_non_objects(self):
        for value in (None, [], "project", 3):
            with self.subTest(value=value):
                diagnostics = validate_project(value)
                self.check_well_formed(diagnostics)
                self.assertIn("invalid_type", errors(diagnostics))


class EnvelopeTests(ValidationTestCase):
    def test_non_object_documents(self):
        for value in (None, [], "document", 1, NAN):
            with self.subTest(value=value):
                diagnostics = validate_document(value)
                self.check_well_formed(diagnostics)
                self.assertIn("invalid_type", errors(diagnostics))

    def test_unknown_schema_versions(self):
        for version in (0, 2, "1", 1.0, True, None):
            with self.subTest(version=version):
                doc = make_document()
                doc["schema_version"] = version
                self.assertRejects("unsupported_schema_version", doc)

    def test_missing_schema_version(self):
        del self.doc["schema_version"]
        self.assertRejects("missing_field")

    def test_unknown_envelope_field(self):
        self.doc["outputs"] = {}
        self.assertRejects("unknown_field")

    def test_missing_history_stack(self):
        del self.doc["redo"]
        self.assertRejects("missing_field")

    def test_history_stacks_must_be_lists(self):
        snapshot = snapshot_of(self.doc)
        for stack in ("undo", "redo"):
            for value in ({}, None, "[]", (), (snapshot,)):
                with self.subTest(stack=stack, value=repr(value)[:20]):
                    doc = make_document()
                    doc[stack] = value
                    self.assertRejects("invalid_type", doc)


class ProjectSchemaTests(ValidationTestCase):
    def test_unknown_project_field(self):
        self.doc["current"]["generated"] = {}
        self.assertRejects("unknown_field")

    def test_invalid_revision(self):
        for value in (-1, 1.5, "3", True, None, NAN):
            with self.subTest(value=value):
                doc = make_document()
                doc["current"]["revision"] = value
                self.assertRejects("invalid_revision", doc)

    def test_invalid_project_id(self):
        for value in ("", "   ", 7, None):
            with self.subTest(value=value):
                doc = make_document()
                doc["current"]["id"] = value
                self.assertRejects("invalid_id", doc)

    def test_sections_must_be_objects(self):
        for part in ("model", "pid", "physical"):
            with self.subTest(part=part):
                doc = make_document()
                doc["current"][part] = []
                self.assertRejects("invalid_type", doc)

    def test_maps_must_be_objects(self):
        for part, name in MAPS:
            with self.subTest(map=name):
                doc = make_document()
                doc["current"][part][name] = []
                self.assertRejects("invalid_type", doc)

    def test_unknown_section_field(self):
        self.doc["current"]["pid"]["sheets"] = {}
        self.assertRejects("unknown_field")

    def test_missing_map(self):
        del self.doc["current"]["physical"]["routes"]
        self.assertRejects("missing_field")

    def test_blank_map_key(self):
        entities = section(self.doc, "entities")
        entities["  "] = copy.deepcopy(next(iter(entities.values())))
        self.assertRejects("invalid_id")

    def test_record_id_must_match_key(self):
        for name in ("entities", "ports", "connections", "symbols", "edges"):
            with self.subTest(map=name):
                doc = make_document()
                records = section(doc, name)
                key = next(iter(records))
                records[key]["id"] = key + "-other"
                self.assertRejects("id_mismatch", doc)


class ParameterTests(ValidationTestCase):
    def tank(self, document=None):
        document = self.doc if document is None else document
        return section(document, "entities")[entity_of_kind(document, "tank")]

    def test_nonfinite_or_nonnumeric_values(self):
        for value in (NAN, INF, -INF, "1.2", True, None, 10**400, [1.2]):
            with self.subTest(value=repr(value)[:20]):
                doc = make_document()
                self.tank(doc)["parameters"]["height"]["value"] = value
                self.assertRejects("invalid_number", doc)

    def test_nonpositive_dimensions(self):
        for value in (0, 0.0, -0.2):
            with self.subTest(value=value):
                doc = make_document()
                self.tank(doc)["parameters"]["diameter"]["value"] = value
                self.assertRejects("out_of_bounds", doc)

    def test_wrong_units(self):
        for unit in ("mm", "M", "", None, 1):
            with self.subTest(unit=unit):
                doc = make_document()
                self.tank(doc)["parameters"]["height"]["unit"] = unit
                self.assertRejects("invalid_unit", doc)

    def test_missing_provenance(self):
        parameters = self.tank()["parameters"]
        height = parameters["height"]
        parameters["height"] = {"value": height["value"], "unit": height["unit"]}
        self.assertRejects("missing_provenance")

    def test_blank_source_is_not_provenance(self):
        parameters = self.tank()["parameters"]
        height = parameters["height"]
        parameters["height"] = {"value": height["value"], "unit": height["unit"], "source": "  "}
        self.assertRejects("missing_provenance")

    def test_both_provenance_forms(self):
        height = self.tank()["parameters"]["height"]
        height.setdefault("assumption_id", next(iter(section(self.doc, "assumptions"))))
        height["source"] = "Vendor datasheet"
        self.assertRejects("invalid_quantity")

    def test_documented_source_is_accepted(self):
        parameters = self.tank()["parameters"]
        height = parameters["height"]
        parameters["height"] = {"value": height["value"], "unit": height["unit"], "source": "Measured on site"}
        self.assertEqual(self.validate(self.doc), [])

    def test_dangling_assumption(self):
        parameters = self.tank()["parameters"]
        height = parameters["height"]
        parameters["height"] = {"value": height["value"], "unit": height["unit"], "assumption_id": "no-such-assumption"}
        self.assertRejects("dangling_reference")

    def test_unknown_quantity_field(self):
        self.tank()["parameters"]["height"]["tolerance"] = 0.01
        self.assertRejects("unknown_field")

    def test_quantity_must_be_an_object(self):
        self.tank()["parameters"]["height"] = 1.2
        self.assertRejects("invalid_quantity")

    def test_unknown_parameter(self):
        parameters = self.tank()["parameters"]
        parameters["volume"] = copy.deepcopy(parameters["height"])
        self.assertRejects("unknown_parameter")

    def test_missing_parameter(self):
        del self.tank()["parameters"]["height"]
        self.assertRejects("missing_parameter")

    def test_outlet_must_lie_within_tank_height(self):
        for offset in (0.0, 0.1):
            with self.subTest(offset=offset):
                doc = make_document()
                parameters = self.tank(doc)["parameters"]
                parameters["outlet_height"]["value"] = parameters["height"]["value"] + offset
                self.assertRejects("out_of_bounds", doc)

    def test_unknown_template(self):
        for field, value in (("kind", "reactor"), ("template_version", 99)):
            with self.subTest(field=field):
                doc = make_document()
                self.tank(doc)[field] = value
                self.assertRejects("missing_template", doc)

    def test_invalid_template_version(self):
        for value in (0, -1, "1", 1.5, True, None):
            with self.subTest(value=value):
                doc = make_document()
                self.tank(doc)["template_version"] = value
                self.assertRejects("invalid_template_version", doc)

    def test_assumption_records(self):
        variants = (
            ("unknown_field", lambda record: record.update(note="extra")),
            ("invalid_value", lambda record: record.update(description="")),
            ("invalid_value", lambda record: record.update(source=None)),
            ("missing_field", lambda record: record.pop("source")),
        )
        for code, alter in variants:
            with self.subTest(code=code):
                doc = make_document()
                assumptions = section(doc, "assumptions")
                alter(assumptions[next(iter(assumptions))])
                self.assertRejects(code, doc)

    def test_dimension_change_that_moves_a_port_invalidates_its_route(self):
        tank_id = entity_of_kind(self.doc, "tank")
        outlet = port_of(self.doc, tank_id, "outlet")
        before = world_port_position(self.doc["current"], outlet)
        parameters = self.tank()["parameters"]
        parameters["outlet_height"]["value"] = (parameters["outlet_height"]["value"] + parameters["height"]["value"]) / 2
        after = world_port_position(self.doc["current"], outlet)
        self.assertGreater(math.dist(before, after), 1e-3, "outlet_height should move the tank outlet port")
        self.assertRejects("route_endpoint_mismatch")
        route = section(self.doc, "routes")[connection_at(self.doc, outlet)]
        route["status"] = "needs_reroute"
        route["reason"] = "tank outlet moved"
        self.assertIn("route_not_current", self.assertCommittable())


class ObservesTests(ValidationTestCase):
    def indicator(self):
        return section(self.doc, "entities")[entity_of_kind(self.doc, "indicator")]

    def test_observed_entity_must_exist(self):
        self.indicator()["observes"]["entity_id"] = "no-such-entity"
        self.assertRejects("dangling_reference")

    def test_indicator_cannot_observe_itself(self):
        indicator = self.indicator()
        indicator["observes"]["entity_id"] = indicator["id"]
        self.assertRejects("invalid_observes")

    def test_only_indicators_observe(self):
        pump = section(self.doc, "entities")[entity_of_kind(self.doc, "centrifugal_pump")]
        pump["observes"] = copy.deepcopy(self.indicator()["observes"])
        self.assertRejects("invalid_observes")

    def test_observed_variable_must_be_named(self):
        self.indicator()["observes"]["variable"] = " "
        self.assertRejects("invalid_observes")

    def test_unknown_observes_field(self):
        self.indicator()["observes"]["unit"] = "bar"
        self.assertRejects("unknown_field")


class TagAndIdTests(ValidationTestCase):
    def setUp(self):
        super().setUp()
        self.entities = section(self.doc, "entities")
        self.pump_id = entity_of_kind(self.doc, "centrifugal_pump")
        self.boundary_id = entity_of_kind(self.doc, "boundary")

    def test_duplicate_tags_ignore_case_and_outer_whitespace(self):
        self.entities[self.boundary_id]["tag"] = "  " + self.entities[self.pump_id]["tag"].lower() + " "
        diagnostics = self.assertRejects("duplicate_tag")
        duplicate = next(d for d in diagnostics if d["code"] == "duplicate_tag")
        self.assertEqual(set(duplicate["entity_ids"]), {self.pump_id, self.boundary_id})

    def test_duplicate_tags_collapse_internal_whitespace(self):
        self.entities[self.pump_id]["tag"] = "P  201"
        self.entities[self.boundary_id]["tag"] = "p 201"
        self.assertRejects("duplicate_tag")

    def test_connection_and_entity_tags_share_a_namespace(self):
        connection = next(iter(section(self.doc, "connections").values()))
        connection["tag"] = self.entities[self.pump_id]["tag"]
        self.assertRejects("duplicate_tag")

    def test_invalid_tags(self):
        for name in ("entities", "connections"):
            for tag in ("", "   ", None, 101, ["P-101"]):
                with self.subTest(map=name, tag=tag):
                    doc = make_document()
                    records = section(doc, name)
                    records[next(iter(records))]["tag"] = tag
                    self.assertRejects("invalid_tag", doc)

    def test_ids_are_unique_across_record_kinds(self):
        symbols = section(self.doc, "symbols")
        old = symbols_of(self.doc, self.pump_id)[0]
        symbol = symbols.pop(old)
        symbol["id"] = self.pump_id
        symbols[self.pump_id] = symbol
        for edge in section(self.doc, "edges").values():
            for key in ("from_symbol_id", "to_symbol_id"):
                if edge[key] == old:
                    edge[key] = self.pump_id
        diagnostics = self.validate(self.doc)
        self.assertEqual(errors(diagnostics), {"duplicate_id"}, diagnostics)


class PortAndConnectionTests(ValidationTestCase):
    def setUp(self):
        super().setUp()
        self.tank_id = entity_of_kind(self.doc, "tank")
        self.pump_id = entity_of_kind(self.doc, "centrifugal_pump")
        self.tank_outlet = port_of(self.doc, self.tank_id, "outlet")
        self.pump_inlet = port_of(self.doc, self.pump_id, "inlet")
        self.pump_outlet = port_of(self.doc, self.pump_id, "outlet")
        self.ports = section(self.doc, "ports")
        self.connections = section(self.doc, "connections")

    def test_reversed_connection_direction(self):
        connection = self.connections[connection_at(self.doc, self.tank_outlet)]
        connection["from_port_id"], connection["to_port_id"] = connection["to_port_id"], connection["from_port_id"]
        self.assertRejects("wrong_port_direction")

    def test_port_direction_must_match_template(self):
        for direction in ("in", "both"):
            with self.subTest(direction=direction):
                doc = make_document()
                section(doc, "ports")[self.pump_outlet]["direction"] = direction
                self.assertRejects("wrong_port_direction", doc)

    def test_invalid_direction_value(self):
        self.ports[self.pump_outlet]["direction"] = "sideways"
        self.assertRejects("invalid_value")

    def test_missing_required_port(self):
        remove_connection(self.doc, connection_at(self.doc, self.pump_inlet))
        del self.ports[self.pump_inlet]
        self.assertRejects("missing_required_port")

    def test_unknown_port_name(self):
        self.ports[self.pump_inlet]["name"] = "suction"
        diagnostics = self.assertRejects("unknown_port")
        self.assertIn("missing_required_port", errors(diagnostics))

    def test_duplicate_port_name(self):
        extra = copy.deepcopy(self.ports[self.pump_outlet])
        extra["id"] = "extra-port"
        self.ports["extra-port"] = extra
        self.assertRejects("duplicate_port_name")

    def test_required_port_must_be_connected(self):
        cid = connection_at(self.doc, self.tank_outlet)
        downstream = self.connections[cid]["to_port_id"]
        remove_connection(self.doc, cid)
        diagnostics = self.assertRejects("required_port_unconnected")
        flagged = {
            item for d in diagnostics if d["code"] == "required_port_unconnected" for item in d["entity_ids"]
        }
        self.assertTrue({self.tank_outlet, downstream} <= flagged, diagnostics)

    def test_port_supports_one_connection(self):
        cid = connection_at(self.doc, self.tank_outlet)
        extra = copy.deepcopy(self.connections[cid])
        extra["id"] = "extra-connection"
        extra["tag"] = "L-999"
        self.connections["extra-connection"] = extra
        self.assertRejects("duplicate_port_use")

    def test_self_connection(self):
        connection = self.connections[connection_at(self.doc, self.tank_outlet)]
        connection["to_port_id"] = connection["from_port_id"]
        self.assertRejects("self_connection")

    def test_port_dn_mismatch(self):
        self.ports[self.pump_inlet]["nominal_diameter_mm"] = 80
        self.assertRejects("dn_mismatch")

    def test_connection_dn_mismatch(self):
        self.connections[connection_at(self.doc, self.pump_inlet)]["nominal_diameter_mm"] = 65
        self.assertRejects("dn_mismatch")

    def test_template_dn_is_a_supported_size_not_a_default(self):
        # Version 1 geometry has fixed nozzle radii, so a consistent DN80 skid is still unsupported.
        for record in (*self.ports.values(), *self.connections.values()):
            record["nominal_diameter_mm"] = 80
        diagnostics = self.assertRejects("unsupported_nominal_diameter")
        self.assertNotIn("dn_mismatch", errors(diagnostics))
        flagged = {item for d in diagnostics if d["code"] == "unsupported_nominal_diameter" for item in d["entity_ids"]}
        self.assertLessEqual(set(self.ports), flagged)

    def test_whole_number_float_dn_is_compatible(self):
        port = self.ports[self.pump_inlet]
        port["nominal_diameter_mm"] = float(port["nominal_diameter_mm"])
        self.assertEqual(self.validate(self.doc), [])

    def test_invalid_dn_values(self):
        for name, key in (("ports", self.pump_inlet), ("connections", None)):
            for value in (0, -50, 50.5, "50", True, None, NAN):
                with self.subTest(map=name, value=value):
                    doc = make_document()
                    records = section(doc, name)
                    records[key or connection_at(doc, self.pump_inlet)]["nominal_diameter_mm"] = value
                    self.assertRejects("invalid_nominal_diameter", doc)

    def test_invalid_service(self):
        for value in ("gas", "", None):
            with self.subTest(value=value):
                doc = make_document()
                section(doc, "ports")[self.pump_inlet]["service"] = value
                self.assertRejects("invalid_service", doc)

    def test_required_flag_must_match_template(self):
        self.ports[self.pump_inlet]["required"] = False
        self.assertRejects("port_template_mismatch")

    def test_required_flag_must_be_boolean(self):
        self.ports[self.pump_inlet]["required"] = "yes"
        self.assertRejects("invalid_type")

    def test_outside_diameter(self):
        for value, code in ((0, "out_of_bounds"), (-0.06, "out_of_bounds"), (NAN, "invalid_number"), ("0.0603", "invalid_number"), (None, "invalid_number")):
            with self.subTest(value=value):
                doc = make_document()
                section(doc, "connections")[connection_at(doc, self.pump_inlet)]["outside_diameter_m"] = value
                self.assertRejects(code, doc)

    def test_dangling_references(self):
        cid = connection_at(self.doc, self.pump_inlet)
        variants = (
            ("connections", cid, "to_port_id"),
            ("connections", cid, "assumption_id"),
            ("ports", self.pump_inlet, "entity_id"),
            ("ports", self.pump_inlet, "assumption_id"),
        )
        for name, key, field in variants:
            with self.subTest(map=name, field=field):
                doc = make_document()
                section(doc, name)[key][field] = "no-such-record"
                self.assertRejects("dangling_reference", doc)

    def test_non_string_references(self):
        for value in (["list"], {"id": 1}, 7, None):
            with self.subTest(value=value):
                doc = make_document()
                section(doc, "connections")[connection_at(doc, self.pump_inlet)]["from_port_id"] = value
                self.assertRejects("invalid_reference", doc)


class OccurrenceTests(ValidationTestCase):
    def setUp(self):
        super().setUp()
        self.pump_id = entity_of_kind(self.doc, "centrifugal_pump")
        self.edges = section(self.doc, "edges")
        self.symbols = section(self.doc, "symbols")
        self.edge_id = next(eid for eid, edge in self.edges.items() if edge["status"] == "current")
        self.edge = self.edges[self.edge_id]

    def test_symbol_entity_must_exist(self):
        self.symbols[next(iter(self.symbols))]["entity_id"] = "no-such-entity"
        self.assertRejects("dangling_reference")

    def test_edge_connection_must_exist(self):
        self.edge["connection_id"] = "no-such-connection"
        self.assertRejects("dangling_reference")

    def test_swapped_edge_symbols(self):
        self.edge["from_symbol_id"], self.edge["to_symbol_id"] = self.edge["to_symbol_id"], self.edge["from_symbol_id"]
        self.assertRejects("occurrence_mismatch")

    def test_edge_symbol_of_unrelated_entity(self):
        self.edge["from_symbol_id"] = symbols_of(self.doc, entity_of_kind(self.doc, "indicator"))[0]
        self.assertRejects("occurrence_mismatch")

    def test_current_edge_must_end_at_port_anchors(self):
        for index in (0, -1):
            with self.subTest(index=index):
                doc = make_document()
                edge = section(doc, "edges")[self.edge_id]
                edge["points"] = shift(edge["points"], index, 0, 7.0)
                self.assertRejects("edge_endpoint_mismatch", doc)

    def test_current_edge_needs_two_points(self):
        self.edge["points"] = self.edge["points"][:1]
        self.assertRejects("too_few_points")

    def test_moving_a_symbol_requires_edge_repair(self):
        moved = symbols_of(self.doc, self.pump_id)[0]
        self.symbols[moved]["x"] += 40
        self.assertRejects("edge_endpoint_mismatch")
        for edge in self.edges.values():
            if moved in (edge["from_symbol_id"], edge["to_symbol_id"]):
                edge["status"] = "needs_reroute"
                edge["reason"] = "symbol moved"
        self.assertIn("edge_not_current", self.assertCommittable())

    def test_non_current_edge_requires_reason(self):
        for reason in (None, "  "):
            with self.subTest(reason=reason):
                doc = make_document()
                edge = section(doc, "edges")[self.edge_id]
                edge["status"] = "unresolved"
                if reason is None:
                    edge.pop("reason", None)
                else:
                    edge["reason"] = reason
                self.assertRejects("missing_reason", doc)

    def test_non_current_edge_with_reason_is_a_warning(self):
        self.edge["status"] = "unresolved"
        self.edge["reason"] = "endpoint anchor needs manual repair"
        self.assertIn("edge_not_current", self.assertCommittable())

    def test_invalid_edge_status(self):
        self.edge["status"] = "stale"
        self.assertRejects("invalid_value")

    def test_malformed_edge_points(self):
        for points, code in (("points", "invalid_type"), ([[0, "a"], [1, 2]], "invalid_number"), ([[0], [1, 2]], "invalid_number"), ([[0, NAN], [1, 2]], "invalid_number")):
            with self.subTest(points=points):
                doc = make_document()
                section(doc, "edges")[self.edge_id]["points"] = points
                self.assertRejects(code, doc)

    def test_symbol_fields(self):
        for field, value, code in (("x", NAN, "invalid_number"), ("rotation_deg", "90", "invalid_number"), ("manual", "no", "invalid_type")):
            with self.subTest(field=field):
                doc = make_document()
                symbols = section(doc, "symbols")
                symbols[next(iter(symbols))][field] = value
                self.assertRejects(code, doc)

    def test_entity_without_symbol_is_a_warning(self):
        for sid in symbols_of(self.doc, entity_of_kind(self.doc, "indicator")):
            del self.symbols[sid]
        self.assertIn("symbol_missing", self.assertCommittable())

    def test_connection_without_edge_is_a_warning(self):
        for edge_id in edges_of(self.doc, self.edge["connection_id"]):
            del self.edges[edge_id]
        self.assertIn("edge_missing", self.assertCommittable())


class PlacementAndRouteTests(ValidationTestCase):
    def setUp(self):
        super().setUp()
        self.pump_id = entity_of_kind(self.doc, "centrifugal_pump")
        self.indicator_id = entity_of_kind(self.doc, "indicator")
        self.placements = section(self.doc, "placements")
        self.routes = section(self.doc, "routes")
        self.route_id = next(cid for cid, route in self.routes.items() if route["status"] == "current")
        self.route = self.routes[self.route_id]

    def unresolve_pump(self, reason="awaiting layout"):
        placement = self.placements[self.pump_id]
        placement.update(position_m=None, rotation_deg=None, status="unresolved")
        if reason is None:
            placement.pop("reason", None)
        else:
            placement["reason"] = reason
        return placement

    def flag_pump_routes(self):
        for cid in connections_of(self.doc, self.pump_id):
            self.routes[cid]["status"] = "needs_reroute"
            self.routes[cid]["reason"] = "pump placement changed"

    def test_unresolved_placement_with_flagged_routes_is_committable(self):
        self.unresolve_pump()
        self.flag_pump_routes()
        self.assertTrue({"placement_unresolved", "route_not_current"} <= self.assertCommittable())

    def test_unresolved_placement_may_omit_assumption(self):
        self.unresolve_pump()["assumption_id"] = None
        self.flag_pump_routes()
        self.assertIn("placement_unresolved", self.assertCommittable())

    def test_unresolved_placement_blocks_current_routes(self):
        self.unresolve_pump()
        self.assertRejects("route_endpoint_unplaced")

    def test_unresolved_placement_requires_reason(self):
        self.unresolve_pump(reason=None)
        self.flag_pump_routes()
        self.assertRejects("missing_reason")

    def test_unresolved_placement_has_null_pose(self):
        self.unresolve_pump()["position_m"] = [0.0, 0.0, 0.0]
        self.flag_pump_routes()
        self.assertRejects("invalid_value")

    def test_placed_pose_must_be_finite(self):
        for field in ("position_m", "rotation_deg"):
            for value in (None, [0, 0], [0, 0, NAN], [0, "0", 0], [0, True, 0], "origin"):
                with self.subTest(field=field, value=value):
                    doc = make_document()
                    section(doc, "placements")[self.pump_id][field] = value
                    self.assertRejects("invalid_number", doc)

    def test_placed_requires_assumption(self):
        for value, code in ((None, "invalid_reference"), ("no-such-assumption", "dangling_reference")):
            with self.subTest(value=value):
                doc = make_document()
                section(doc, "placements")[self.pump_id]["assumption_id"] = value
                self.assertRejects(code, doc)

    def test_invalid_placement_status(self):
        self.placements[self.pump_id]["status"] = "floating"
        self.assertRejects("invalid_value")

    def test_placement_for_unknown_entity(self):
        self.placements["no-such-entity"] = copy.deepcopy(self.placements[self.pump_id])
        self.assertRejects("dangling_reference")

    def test_missing_placement_is_a_warning(self):
        del self.placements[self.indicator_id]
        self.assertIn("placement_missing", self.assertCommittable())

    def test_missing_placement_blocks_current_routes(self):
        del self.placements[self.pump_id]
        diagnostics = self.assertRejects("route_endpoint_unplaced")
        self.assertIn("placement_missing", warnings(diagnostics))

    def test_moving_equipment_requires_rerouting(self):
        placement = self.placements[self.pump_id]
        placement["position_m"] = shift([placement["position_m"]], 0, 0, 0.5)[0]
        self.assertRejects("route_endpoint_mismatch")
        self.flag_pump_routes()
        self.assertIn("route_not_current", self.assertCommittable())

    def test_route_endpoint_tolerance(self):
        for index in (0, -1):
            with self.subTest(index=index):
                doc = make_document()
                route = section(doc, "routes")[self.route_id]
                route["points_m"] = shift(route["points_m"], index, 0, 2e-7)
                self.assertEqual(self.validate(doc), [])
                route["points_m"] = shift(route["points_m"], index, 0, 1e-3)
                self.assertRejects("route_endpoint_mismatch", doc)

    def test_route_must_be_orthogonal(self):
        points = self.route["points_m"]
        start, end = points[0], points[-1]
        offsets = (0.0123, 0.0321, 0.0)
        middle = [(a + b) / 2 + offset for a, b, offset in zip(start, end, offsets)]
        self.route["points_m"] = [list(start), middle, list(end)]
        diagnostics = self.assertRejects("route_not_orthogonal")
        self.assertNotIn("route_endpoint_mismatch", errors(diagnostics))

    def test_route_segments_need_length(self):
        points = [list(point) for point in self.route["points_m"]]
        self.route["points_m"] = [points[0]] + points
        self.assertRejects("route_not_orthogonal")

    def test_current_route_needs_two_points(self):
        self.route["points_m"] = self.route["points_m"][:1]
        self.assertRejects("too_few_points")

    def test_non_current_route_requires_reason(self):
        self.route["status"] = "needs_reroute"
        self.route.pop("reason", None)
        self.assertRejects("missing_reason")

    def test_non_current_route_retains_points(self):
        self.route["status"] = "needs_reroute"
        self.route["reason"] = "equipment moved"
        self.route["points_m"] = shift(self.route["points_m"], 0, 2, 0.25)
        self.assertIn("route_not_current", self.assertCommittable())

    def test_unresolved_route_may_be_empty(self):
        self.route.update(points_m=[], status="unresolved", reason="no supported split", assumption_id=None)
        self.assertIn("route_not_current", self.assertCommittable())

    def test_invalid_route_values(self):
        for field, value, code in (
            ("mode", "auto", "invalid_value"),
            ("status", "stale", "invalid_value"),
            ("points_m", [[0, 0], [0, 1]], "invalid_number"),
            ("points_m", "polyline", "invalid_type"),
            ("assumption_id", None, "invalid_reference"),
        ):
            with self.subTest(field=field, value=value):
                doc = make_document()
                section(doc, "routes")[self.route_id][field] = value
                self.assertRejects(code, doc)

    def test_route_for_unknown_connection(self):
        self.routes["no-such-connection"] = copy.deepcopy(self.route)
        self.assertRejects("dangling_reference")

    def test_missing_route_is_a_warning(self):
        del self.routes[self.route_id]
        self.assertIn("route_missing", self.assertCommittable())


class HistoryTests(ValidationTestCase):
    def with_history(self, undo=1, redo=1):
        doc = make_document()
        doc["undo"] = [snapshot_of(doc) for _ in range(undo)]
        doc["redo"] = [snapshot_of(doc) for _ in range(redo)]
        doc["current"]["revision"] = max(doc["current"]["revision"], undo + redo)
        return doc

    def assertHistoryRejects(self, code, document, prefix):
        diagnostics = self.assertRejects(code, document)
        messages = [d["message"] for d in diagnostics if d["code"] == code]
        self.assertTrue(any(message.startswith(prefix) for message in messages), messages)
        return diagnostics

    def test_valid_history(self):
        self.assertEqual(self.validate(self.with_history(2, 1)), [])

    def test_dangling_reference_in_undo(self):
        doc = self.with_history()
        symbols = doc["undo"][0]["pid"]["symbols"]
        symbols[next(iter(symbols))]["entity_id"] = "no-such-entity"
        self.assertHistoryRejects("dangling_reference", doc, "undo[0]: ")

    def test_snapshot_carries_no_revision_identity_or_outputs(self):
        for field in ("revision", "id", "outputs"):
            with self.subTest(field=field):
                doc = self.with_history()
                doc["redo"][0][field] = 1
                self.assertHistoryRejects("unknown_field", doc, "redo[0]: ")

    def test_snapshot_missing_section(self):
        doc = self.with_history()
        del doc["undo"][0]["physical"]
        self.assertHistoryRejects("missing_field", doc, "undo[0]: ")

    def test_snapshot_must_be_an_object(self):
        doc = self.with_history(1, 0)
        doc["undo"].append("snapshot")
        doc["current"]["revision"] += 1
        self.assertHistoryRejects("invalid_type", doc, "undo[1]: ")

    def test_snapshot_nonfinite_value(self):
        doc = self.with_history()
        pump = entity_of_kind(doc, "centrifugal_pump")
        doc["undo"][0]["physical"]["placements"][pump]["position_m"] = [0.0, NAN, 0.0]
        self.assertHistoryRejects("invalid_number", doc, "undo[0]: ")

    def test_snapshot_duplicate_tags(self):
        doc = self.with_history()
        entities = doc["redo"][0]["model"]["entities"]
        first, second = list(entities)[:2]
        entities[second]["tag"] = entities[first]["tag"]
        self.assertHistoryRejects("duplicate_tag", doc, "redo[0]: ")

    def test_snapshot_unsupported_dn(self):
        for stack in ("undo", "redo"):
            with self.subTest(stack=stack):
                doc = self.with_history()
                model = doc[stack][0]["model"]
                for record in (*model["ports"].values(), *model["connections"].values()):
                    record["nominal_diameter_mm"] = 80
                self.assertHistoryRejects("unsupported_nominal_diameter", doc, f"{stack}[0]: ")

    def test_unresolved_presentation_in_history_is_not_reported(self):
        doc = self.with_history(1, 0)
        snapshot = doc["undo"][0]
        pump = entity_of_kind(doc, "centrifugal_pump")
        snapshot["physical"]["placements"][pump].update(
            position_m=None, rotation_deg=None, status="unresolved", reason="pending placement"
        )
        for cid in connections_of(doc, pump):
            snapshot["physical"]["routes"][cid].update(status="needs_reroute", reason="pending placement")
        self.assertEqual(self.validate(doc), [])

    def test_history_cannot_outnumber_revisions(self):
        doc = self.with_history(1, 1)
        doc["current"]["revision"] = 1
        self.assertRejects("history_revision_inconsistent", doc)

    def test_current_and_history_errors_are_both_reported(self):
        doc = self.with_history()
        section(doc, "symbols")[next(iter(section(doc, "symbols")))]["x"] = NAN
        snapshot_symbols = doc["undo"][0]["pid"]["symbols"]
        snapshot_symbols[next(iter(snapshot_symbols))]["y"] = INF
        diagnostics = self.validate(doc)
        messages = [d["message"] for d in diagnostics if d["code"] == "invalid_number"]
        self.assertTrue(any(message.startswith("undo[0]: ") for message in messages), messages)
        self.assertTrue(any(not message.startswith("undo[") for message in messages), messages)


class MalformedInputTests(ValidationTestCase):
    def test_garbage_in_every_field(self):
        base = make_document()
        for part, name in MAPS:
            records = base["current"][part][name]
            key = next(iter(records))
            for field in records[key]:
                for value in GARBAGE:
                    with self.subTest(map=name, field=field, value=repr(value)[:20]):
                        doc = copy.deepcopy(base)
                        doc["current"][part][name][key][field] = value
                        diagnostics = self.validate(doc)
                        if value is NAN or value is INF or value == {"x": 1}:
                            self.assertTrue(errors(diagnostics), diagnostics)

    def test_missing_fields(self):
        base = make_document()
        for part, name in MAPS:
            records = base["current"][part][name]
            key = next(iter(records))
            for field in records[key]:
                with self.subTest(map=name, field=field):
                    doc = copy.deepcopy(base)
                    del doc["current"][part][name][key][field]
                    diagnostics = self.validate(doc)
                    if field not in OPTIONAL_FIELDS:
                        self.assertIn("missing_field", errors(diagnostics))

    def test_garbage_records_and_maps(self):
        base = make_document()
        for part, name in MAPS:
            key = next(iter(base["current"][part][name]))
            for value in GARBAGE:
                with self.subTest(map=name, value=repr(value)[:20]):
                    doc = copy.deepcopy(base)
                    doc["current"][part][name][key] = value
                    self.assertTrue(errors(self.validate(doc)))
                    doc = copy.deepcopy(base)
                    doc["current"][part][name] = value
                    diagnostics = self.validate(doc)
                    if not (isinstance(value, dict) and not value):
                        self.assertTrue(errors(diagnostics))

    def test_garbage_in_quantities_and_relations(self):
        base = make_document()
        tank = entity_of_kind(base, "tank")
        indicator = entity_of_kind(base, "indicator")
        targets = (
            (tank, ("parameters", "height")),
            (indicator, ("observes",)),
        )
        for entity_id, path in targets:
            nested = section(base, "entities")[entity_id]
            for step in path:
                nested = nested[step]
            for field in list(nested) + [None]:
                for value in GARBAGE:
                    with self.subTest(path=path, field=field, value=repr(value)[:20]):
                        doc = copy.deepcopy(base)
                        holder = section(doc, "entities")[entity_id]
                        for step in path[:-1]:
                            holder = holder[step]
                        if field is None:
                            holder[path[-1]] = value
                        else:
                            holder[path[-1]][field] = value
                        diagnostics = self.validate(doc)
                        if field is None or value is NAN or value is INF:
                            self.assertTrue(errors(diagnostics), diagnostics)

    def test_garbage_points(self):
        base = make_document()
        for name, field in (("routes", "points_m"), ("edges", "points")):
            records = section(base, name)
            key = next(k for k, record in records.items() if record["status"] == "current")
            for index in (0, -1):
                for value in GARBAGE:
                    with self.subTest(map=name, index=index, value=repr(value)[:20]):
                        doc = copy.deepcopy(base)
                        section(doc, name)[key][field][index] = value
                        self.assertTrue(errors(self.validate(doc)))

    def test_non_json_values(self):
        for part, name in MAPS:
            with self.subTest(map=name):
                doc = make_document()
                records = doc["current"][part][name]
                record = records[next(iter(records))]
                record[next(iter(record))] = object()
                diagnostics = validate_document(doc)
                self.check_well_formed(diagnostics)
                self.assertTrue(errors(diagnostics))

    def test_garbage_in_history_snapshots(self):
        base = make_document()
        base["undo"] = [snapshot_of(base)]
        base["current"]["revision"] = max(base["current"]["revision"], 1)
        for part, name in MAPS:
            records = base["undo"][0][part][name]
            key = next(iter(records))
            for field in records[key]:
                with self.subTest(map=name, field=field):
                    doc = copy.deepcopy(base)
                    doc["undo"][0][part][name][key][field] = NAN
                    found = [d for d in self.validate(doc) if d["severity"] == "error"]
                    self.assertTrue(found)
                    for diagnostic in found:
                        self.assertTrue(diagnostic["message"].startswith("undo[0]: "), diagnostic)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.project = make_document()["current"]

    def expected(self, project, ensure_ascii=False):
        text = json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii, allow_nan=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_digest_is_sha256_of_canonical_json(self):
        self.assertEqual(canonical_digest(self.project), self.expected(self.project))

    def test_digest_ignores_key_order(self):
        self.assertEqual(canonical_digest(reordered(self.project)), canonical_digest(self.project))

    def test_digest_tracks_content(self):
        changed = copy.deepcopy(self.project)
        next(iter(changed["model"]["entities"].values()))["tag"] += "X"
        self.assertNotEqual(canonical_digest(changed), canonical_digest(self.project))

    def test_digest_encodes_text_as_utf8(self):
        next(iter(self.project["model"]["entities"].values()))["tag"] = "Pümpe-101"
        digest = canonical_digest(self.project)
        self.assertEqual(digest, self.expected(self.project))
        self.assertNotEqual(digest, self.expected(self.project, ensure_ascii=True))

    def test_digest_rejects_nonfinite_numbers(self):
        next(iter(self.project["physical"]["placements"].values()))["position_m"] = [0.0, NAN, 0.0]
        with self.assertRaises(ValueError):
            canonical_digest(self.project)

    def test_digest_does_not_mutate(self):
        before = copy.deepcopy(self.project)
        canonical_digest(self.project)
        self.assertEqual(self.project, before)


if __name__ == "__main__":
    unittest.main()
