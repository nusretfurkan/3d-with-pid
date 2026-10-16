"""Template records, port frames, primitives, and the pump-skid fixture."""

from __future__ import annotations

import ast
import copy
import json
import math
import re
import unittest
from pathlib import Path

from examples import make_fixture
from skid.templates import (
    diagram_port_position,
    entity_primitives,
    get_template,
    local_port_position,
    world_port_position,
)

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 1e-9

_VALVE_PORTS = {"inlet": ("in", [-16, 0]), "outlet": ("out", [16, 0])}

# kind -> (required parameters, {port: (direction, diagram anchor)})
EXPECTED_TEMPLATES = {
    "tank": (("diameter", "height", "outlet_height"), {"outlet": ("out", [40, 20])}),
    "centrifugal_pump": (
        ("body_length", "body_width", "body_height", "motor_length", "motor_diameter"),
        {"inlet": ("in", [-28, 0]), "outlet": ("out", [0, -28])},
    ),
    "isolation_valve": (("length", "body_diameter"), _VALVE_PORTS),
    "check_valve": (("length", "body_diameter"), _VALVE_PORTS),
    "indicator": (("diameter",), {}),
    "boundary": (("size",), {"inlet": ("in", [-18, 0])}),
}

# Uneven dimensions so that frames cannot agree by accident.
SAMPLE_DIMENSIONS = {
    "tank": {"diameter": 1.3, "height": 2.1, "outlet_height": 0.35},
    "centrifugal_pump": {
        "body_length": 0.61,
        "body_width": 0.33,
        "body_height": 0.47,
        "motor_length": 0.52,
        "motor_diameter": 0.29,
    },
    "isolation_valve": {"length": 0.23, "body_diameter": 0.13},
    "check_valve": {"length": 0.27, "body_diameter": 0.15},
    "indicator": {"diameter": 0.1},
    "boundary": {"size": 0.31},
}

# kind -> {port: (local position, outward axis)} implied by the documented frames.
SAMPLE_LOCAL_PORTS = {
    "tank": {"outlet": ((0.75, 0.0, 0.35), (1, 0, 0))},
    "centrifugal_pump": {
        "inlet": ((-0.405, 0.0, 0.235), (-1, 0, 0)),
        "outlet": ((0.0, 0.0, 0.57), (0, 0, 1)),
    },
    "isolation_valve": {
        "inlet": ((-0.115, 0.0, 0.0), (-1, 0, 0)),
        "outlet": ((0.115, 0.0, 0.0), (1, 0, 0)),
    },
    "check_valve": {
        "inlet": ((-0.135, 0.0, 0.0), (-1, 0, 0)),
        "outlet": ((0.135, 0.0, 0.0), (1, 0, 0)),
    },
    "indicator": {},
    "boundary": {"inlet": ((-0.155, 0.0, 0.0), (-1, 0, 0))},
}

SHAPE_FIELDS = {
    "box": {"key", "shape", "center_m", "rotation_deg", "size_m"},
    "cylinder": {"key", "shape", "center_m", "rotation_deg", "radius_m", "length_m"},
    "sphere": {"key", "shape", "center_m", "rotation_deg", "radius_m"},
}

FIXTURE_ENTITIES = {
    "TK-101": ("tank", {"diameter": 0.8, "height": 1.2, "outlet_height": 0.2}),
    "HV-101": ("isolation_valve", {"length": 0.16, "body_diameter": 0.1}),
    "P-101": (
        "centrifugal_pump",
        {
            "body_length": 0.45,
            "body_width": 0.3,
            "body_height": 0.4,
            "motor_length": 0.35,
            "motor_diameter": 0.22,
        },
    ),
    "NRV-101": ("check_valve", {"length": 0.18, "body_diameter": 0.11}),
    "HV-102": ("isolation_valve", {"length": 0.16, "body_diameter": 0.1}),
    "OUT-101": ("boundary", {"size": 0.2}),
    "LI-101": ("indicator", {"diameter": 0.08}),
    "PI-101": ("indicator", {"diameter": 0.08}),
}
PROCESS_ORDER = ("TK-101", "HV-101", "P-101", "NRV-101", "HV-102", "OUT-101")
LINE_TAGS = ("L-101", "L-102", "L-103", "L-104", "L-105")

# Local port positions chosen for the fixture dimensions (template version 1).
FIXTURE_LOCAL_PORTS = {
    ("TK-101", "outlet"): (0.5, 0.0, 0.2),
    ("HV-101", "inlet"): (-0.08, 0.0, 0.0),
    ("HV-101", "outlet"): (0.08, 0.0, 0.0),
    ("P-101", "inlet"): (-0.325, 0.0, 0.2),
    ("P-101", "outlet"): (0.0, 0.0, 0.5),
    ("NRV-101", "inlet"): (-0.09, 0.0, 0.0),
    ("NRV-101", "outlet"): (0.09, 0.0, 0.0),
    ("HV-102", "inlet"): (-0.08, 0.0, 0.0),
    ("HV-102", "outlet"): (0.08, 0.0, 0.0),
    ("OUT-101", "inlet"): (-0.1, 0.0, 0.0),
}


# --- Helpers -----------------------------------------------------------------


def rotation_matrix(rotation_deg):
    """Independent reference for XYZ order: R = Rz @ Ry @ Rx."""
    ax, ay, az = (math.radians(a) for a in rotation_deg)
    rx = ((1, 0, 0), (0, math.cos(ax), -math.sin(ax)), (0, math.sin(ax), math.cos(ax)))
    ry = ((math.cos(ay), 0, math.sin(ay)), (0, 1, 0), (-math.sin(ay), 0, math.cos(ay)))
    rz = ((math.cos(az), -math.sin(az), 0), (math.sin(az), math.cos(az), 0), (0, 0, 1))
    return matmul(rz, matmul(ry, rx))


def matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def apply(matrix, vector):
    return tuple(sum(row[k] * vector[k] for k in range(3)) for row in matrix)


def world_point(placement, point):
    rotated = apply(rotation_matrix(placement["rotation_deg"]), point)
    return tuple(p + r for p, r in zip(placement["position_m"], rotated))


def nozzle_end(primitive):
    """Return a cylinder's local +z end point and +z axis."""
    axis = apply(rotation_matrix(primitive["rotation_deg"]), (0.0, 0.0, 1.0))
    end = tuple(c + a * primitive["length_m"] / 2 for c, a in zip(primitive["center_m"], axis))
    return end, axis


def make_entity(kind, dimensions):
    return {
        "id": "ent-x",
        "kind": kind,
        "tag": "X-1",
        "template_version": 1,
        "parameters": {
            name: {"value": value, "unit": "m", "assumption_id": "asm-test"}
            for name, value in dimensions.items()
        },
    }


def make_project(kind, placement=None, symbol=None):
    ports = {
        f"port-{name}": {
            "id": f"port-{name}",
            "entity_id": "ent-x",
            "name": name,
            **spec,
            "assumption_id": "asm-test",
        }
        for name, spec in get_template(kind, 1)["ports"].items()
    }
    project = {
        "id": "project-test",
        "revision": 0,
        "model": {
            "entities": {"ent-x": make_entity(kind, SAMPLE_DIMENSIONS[kind])},
            "ports": ports,
            "connections": {},
            "assumptions": {},
        },
        "pid": {"symbols": {}, "edges": {}},
        "physical": {"placements": {}, "routes": {}},
    }
    if placement is not None:
        project["physical"]["placements"]["ent-x"] = placement
    if symbol is not None:
        project["pid"]["symbols"]["sym-x"] = {"id": "sym-x", "entity_id": "ent-x", **symbol}
    return project


def placed(position, rotation):
    return {
        "position_m": list(position),
        "rotation_deg": list(rotation),
        "status": "placed",
        "manual": False,
        "assumption_id": "asm-test",
    }


def containers(value):
    found, stack = [], [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            found.append(item)
            stack.extend(item)
    return found


def strict_json(text):
    def pairs_hook(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate keys in {keys}")
        return dict(pairs)

    def reject_constant(name):
        raise ValueError(f"non-finite number {name}")

    return json.loads(text, object_pairs_hook=pairs_hook, parse_constant=reject_constant)


class Assertions(unittest.TestCase):
    def assert_close(self, actual, expected, tolerance=TOLERANCE):
        self.assertEqual(len(actual), len(expected), f"{actual} != {expected}")
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, delta=tolerance, msg=f"{actual} != {expected}")

    def assert_no_shared_containers(self, first, second):
        first_ids = {id(item) for item in containers(first)}
        shared = [item for item in containers(second) if id(item) in first_ids]
        self.assertEqual(shared, [])

    def axis_steps(self, points):
        """Assert every segment moves along exactly one axis; return unit steps."""
        steps = []
        for start, end in zip(points, points[1:]):
            delta = [e - s for s, e in zip(start, end)]
            moving = [i for i, d in enumerate(delta) if abs(d) > TOLERANCE]
            self.assertEqual(len(moving), 1, f"segment {start} -> {end} is not orthogonal")
            step = [0.0] * len(delta)
            step[moving[0]] = math.copysign(1.0, delta[moving[0]])
            steps.append(tuple(step))
        return steps


# --- Template records --------------------------------------------------------


class TemplateRecordTests(Assertions):
    def test_records_match_the_boundary_table(self):
        for kind, (parameters, ports) in EXPECTED_TEMPLATES.items():
            with self.subTest(kind=kind):
                expected = {
                    "kind": kind,
                    "version": 1,
                    "parameters": {name: {"unit": "m", "min_exclusive": 0} for name in parameters},
                    "ports": {
                        name: {
                            "direction": direction,
                            "required": True,
                            "nominal_diameter_mm": 50,
                            "service": "liquid",
                        }
                        for name, (direction, _) in ports.items()
                    },
                    "diagram_anchors": {name: anchor for name, (_, anchor) in ports.items()},
                }
                self.assertEqual(get_template(kind, 1), expected)

    def test_records_are_fresh_copies(self):
        first = get_template("centrifugal_pump", 1)
        second = get_template("centrifugal_pump", 1)
        self.assertEqual(first, second)
        self.assert_no_shared_containers(first, second)
        first["ports"]["inlet"]["direction"] = "out"
        first["diagram_anchors"]["inlet"][0] = 99
        first["parameters"].clear()
        self.assertEqual(get_template("centrifugal_pump", 1), second)

    def test_unknown_templates_raise_key_error(self):
        cases = [("reactor", 1), ("tank", 2), ("tank", 0), ("tank", "1"), ("tank", True), ("tank", 1.0), (None, 1)]
        for kind, version in cases:
            with self.subTest(kind=kind, version=version):
                with self.assertRaises(KeyError):
                    get_template(kind, version)

    def test_templates_do_not_import_core(self):
        for path in sorted((ROOT / "skid" / "templates").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = ["." * node.level + (node.module or "")]
                else:
                    continue
                for name in names:
                    with self.subTest(path=path.name, name=name):
                        self.assertFalse(name == "skid" or name.startswith(("skid.", "..")))


# --- Primitives and local frames ---------------------------------------------


class PrimitiveTests(Assertions):
    def test_primitive_records_are_well_formed(self):
        for kind, dimensions in SAMPLE_DIMENSIONS.items():
            with self.subTest(kind=kind):
                primitives = entity_primitives(make_entity(kind, dimensions))
                keys = [primitive["key"] for primitive in primitives]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertIn("body", keys)
                self.assertEqual(
                    sorted(key for key in keys if key.startswith("nozzle/")),
                    sorted(f"nozzle/{name}" for name in EXPECTED_TEMPLATES[kind][1]),
                )
                for primitive in primitives:
                    self.assertEqual(set(primitive), SHAPE_FIELDS[primitive["shape"]])
                    vectors = [primitive["center_m"], primitive["rotation_deg"]]
                    if "size_m" in primitive:
                        vectors.append(primitive["size_m"])
                    for vector in vectors:
                        self.assertIsInstance(vector, list)
                        self.assertEqual(len(vector), 3)
                    lengths = [primitive[f] for f in ("radius_m", "length_m") if f in primitive]
                    lengths += primitive.get("size_m", [])
                    for value in [v for vector in vectors for v in vector] + lengths:
                        self.assertIs(type(value), float)
                        self.assertTrue(math.isfinite(value))
                    for value in lengths:
                        self.assertGreater(value, 0)

    def test_primitives_are_deterministic_fresh_copies(self):
        entity = make_entity("centrifugal_pump", SAMPLE_DIMENSIONS["centrifugal_pump"])
        original = copy.deepcopy(entity)
        first = entity_primitives(entity)
        second = entity_primitives(entity)
        self.assertEqual(first, second)
        self.assert_no_shared_containers(first, second)
        first[0]["center_m"][0] = 99.0
        self.assertEqual(entity_primitives(entity), second)
        self.assertEqual(entity, original)

    def test_nozzle_ends_coincide_with_local_ports(self):
        for kind, ports in SAMPLE_LOCAL_PORTS.items():
            project = make_project(kind)
            primitives = {p["key"]: p for p in entity_primitives(project["model"]["entities"]["ent-x"])}
            for name, (expected, outward) in ports.items():
                with self.subTest(kind=kind, port=name):
                    local = local_port_position(project, f"port-{name}")
                    self.assertIsInstance(local, tuple)
                    self.assert_close(local, expected)
                    nozzle = primitives[f"nozzle/{name}"]
                    self.assertEqual(nozzle["shape"], "cylinder")
                    end, axis = nozzle_end(nozzle)
                    self.assert_close(end, local)
                    self.assert_close(axis, outward)

    def test_every_parameter_drives_geometry(self):
        for kind, dimensions in SAMPLE_DIMENSIONS.items():
            baseline = entity_primitives(make_entity(kind, dimensions))
            for name, value in dimensions.items():
                with self.subTest(kind=kind, parameter=name):
                    changed = make_entity(kind, {**dimensions, name: value * 1.1})
                    self.assertNotEqual(entity_primitives(changed), baseline)

    def test_tank_height_moves_shell_but_not_outlet(self):
        project = make_project("tank")
        project["model"]["entities"]["ent-x"]["parameters"]["height"]["value"] = 2.5
        body = entity_primitives(project["model"]["entities"]["ent-x"])[0]
        self.assertEqual((body["key"], body["length_m"], body["center_m"]), ("body", 2.5, [0.0, 0.0, 1.25]))
        self.assertEqual(local_port_position(project, "port-outlet"), (0.75, 0.0, 0.35))

    def test_invalid_parameters_are_rejected(self):
        for value in (-0.8, 0, 0.0, math.nan, math.inf, True, "0.8", None):
            with self.subTest(value=value):
                entity = make_entity("tank", SAMPLE_DIMENSIONS["tank"])
                entity["parameters"]["diameter"]["value"] = value
                with self.assertRaises(ValueError):
                    entity_primitives(entity)
        entity = make_entity("tank", SAMPLE_DIMENSIONS["tank"])
        entity["parameters"]["height"]["unit"] = "mm"
        with self.assertRaises(ValueError):
            entity_primitives(entity)
        entity = make_entity("tank", SAMPLE_DIMENSIONS["tank"])
        del entity["parameters"]["outlet_height"]
        with self.assertRaises(KeyError):
            entity_primitives(entity)
        entity = make_entity("tank", SAMPLE_DIMENSIONS["tank"])
        entity["template_version"] = 2
        with self.assertRaises(KeyError):
            entity_primitives(entity)

    def test_port_lookup_errors(self):
        project = make_project("isolation_valve")
        with self.assertRaises(KeyError):
            local_port_position(project, "port-missing")
        project["model"]["ports"]["port-inlet"]["name"] = "drain"
        with self.assertRaises(KeyError):
            local_port_position(project, "port-inlet")
        project = make_project("isolation_valve")
        project["model"]["ports"]["port-inlet"]["entity_id"] = "ent-missing"
        with self.assertRaises(KeyError):
            local_port_position(project, "port-inlet")


# --- World transforms ----------------------------------------------------------


class TransformTests(Assertions):
    GENERAL = placed((0.3, -1.2, 2.0), (30.0, -45.0, 120.0))

    def test_unrotated_placement_translates(self):
        project = make_project("isolation_valve", placement=placed((1.0, 2.0, 3.0), (0, 0, 0)))
        self.assertEqual(world_port_position(project, "port-outlet"), (1.115, 2.0, 3.0))

    def test_quarter_turns_are_exact(self):
        project = make_project("isolation_valve", placement=placed((1.0, 2.0, 3.0), (0, 0, 90)))
        self.assertEqual(world_port_position(project, "port-outlet"), (1.0, 2.115, 3.0))
        self.assertEqual(world_port_position(project, "port-inlet"), (1.0, 1.885, 3.0))
        project = make_project("isolation_valve", placement=placed((0, 0, 0), (0, 90, 0)))
        self.assertEqual(world_port_position(project, "port-outlet"), (0.0, 0.0, -0.115))

    def test_rotation_order_is_x_then_y_then_z(self):
        # Pump outlet (0, 0, 0.57): Rx(90) gives (0, -0.57, 0), then Rz(90) gives
        # (0.57, 0, 0). Applying Z before X would give (0, -0.57, 0) instead.
        project = make_project("centrifugal_pump", placement=placed((0, 0, 0), (90, 0, 90)))
        self.assertEqual(world_port_position(project, "port-outlet"), (0.57, 0.0, 0.0))

    def test_general_rotation_matches_reference_matrix(self):
        project = make_project("centrifugal_pump", placement=copy.deepcopy(self.GENERAL))
        for name, (local, _) in SAMPLE_LOCAL_PORTS["centrifugal_pump"].items():
            with self.subTest(port=name):
                self.assert_close(world_port_position(project, f"port-{name}"), world_point(self.GENERAL, local))

    def test_nozzle_ends_coincide_with_world_ports(self):
        for kind in SAMPLE_DIMENSIONS:
            project = make_project(kind, placement=copy.deepcopy(self.GENERAL))
            for primitive in entity_primitives(project["model"]["entities"]["ent-x"]):
                if primitive["key"].startswith("nozzle/"):
                    name = primitive["key"].removeprefix("nozzle/")
                    with self.subTest(kind=kind, port=name):
                        end, _ = nozzle_end(primitive)
                        self.assert_close(
                            world_point(self.GENERAL, end), world_port_position(project, f"port-{name}")
                        )

    def test_unplaced_entities_have_no_world_position(self):
        unresolved = {
            "position_m": None,
            "rotation_deg": None,
            "status": "unresolved",
            "manual": False,
            "assumption_id": "asm-test",
            "reason": "awaiting placement",
        }
        self.assertIsNone(world_port_position(make_project("check_valve", placement=unresolved), "port-inlet"))
        self.assertIsNone(world_port_position(make_project("check_valve"), "port-inlet"))

    def test_invalid_placements_are_rejected(self):
        cases = [
            {**placed((0, 0, 0), (0, 0, 0)), "status": "floating"},
            placed((0, math.nan, 0), (0, 0, 0)),
            placed((0, 0), (0, 0, 0)),
            placed((0, 0, 0), (0, True, 0)),
        ]
        for placement in cases:
            with self.subTest(placement=placement):
                with self.assertRaises(ValueError):
                    world_port_position(make_project("check_valve", placement=placement), "port-inlet")


# --- P&ID anchors --------------------------------------------------------------


class DiagramTests(Assertions):
    @staticmethod
    def pump_symbol(rotation):
        symbol = {"x": 100.0, "y": 200.0, "rotation_deg": rotation, "manual": False}
        return make_project("centrifugal_pump", symbol=symbol)

    def test_anchor_rotation_follows_svg_transform(self):
        # Positive angles turn clockwise on screen because +y points down.
        cases = {
            0: {"inlet": (72.0, 200.0), "outlet": (100.0, 172.0)},
            90: {"inlet": (100.0, 172.0), "outlet": (128.0, 200.0)},
            180: {"inlet": (128.0, 200.0), "outlet": (100.0, 228.0)},
            270: {"inlet": (100.0, 228.0), "outlet": (72.0, 200.0)},
            -90: {"inlet": (100.0, 228.0), "outlet": (72.0, 200.0)},
        }
        for rotation, expected in cases.items():
            project = self.pump_symbol(float(rotation))
            for name, point in expected.items():
                with self.subTest(rotation=rotation, port=name):
                    self.assertEqual(diagram_port_position(project, f"port-{name}", "sym-x"), point)

    def test_general_angle(self):
        project = self.pump_symbol(30.0)
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        self.assert_close(diagram_port_position(project, "port-inlet", "sym-x"), (100 - 28 * c, 200 - 28 * s))
        self.assert_close(diagram_port_position(project, "port-outlet", "sym-x"), (100 + 28 * s, 200 - 28 * c))

    def test_symbol_must_represent_the_port_entity(self):
        project = self.pump_symbol(0.0)
        project["pid"]["symbols"]["sym-x"]["entity_id"] = "ent-other"
        with self.assertRaises(ValueError):
            diagram_port_position(project, "port-inlet", "sym-x")
        with self.assertRaises(KeyError):
            diagram_port_position(self.pump_symbol(0.0), "port-inlet", "sym-missing")


# --- Fixture -------------------------------------------------------------------


class FixtureTests(Assertions):
    def setUp(self):
        self.document = make_fixture.make_document()
        self.project = self.document["current"]
        self.model = self.project["model"]
        self.entities = self.model["entities"]
        self.ports = self.model["ports"]
        self.connections = self.model["connections"]
        self.symbols = self.project["pid"]["symbols"]
        self.edges = self.project["pid"]["edges"]
        self.placements = self.project["physical"]["placements"]
        self.routes = self.project["physical"]["routes"]
        self.by_tag = {entity["tag"]: entity for entity in self.entities.values()}

    def port_of(self, tag, name):
        entity_id = self.by_tag[tag]["id"]
        (port,) = [p for p in self.ports.values() if p["entity_id"] == entity_id and p["name"] == name]
        return port

    def world_nozzle(self, port_id):
        port = self.ports[port_id]
        entity = self.entities[port["entity_id"]]
        placement = self.placements[entity["id"]]
        (nozzle,) = [p for p in entity_primitives(entity) if p["key"] == f"nozzle/{port['name']}"]
        end, axis = nozzle_end(nozzle)
        return world_point(placement, end), apply(rotation_matrix(placement["rotation_deg"]), axis)

    def test_builder_returns_deterministic_fresh_documents(self):
        first = make_fixture.make_document()
        second = make_fixture.make_document()
        self.assertEqual(first, second)
        self.assert_no_shared_containers(first, second)
        first["current"]["physical"]["routes"]["conn-discharge-1"]["points_m"][0][0] = 9.0
        self.assertEqual(make_fixture.make_document(), second)

    def test_committed_json_matches_builder(self):
        text = (ROOT / "examples" / "pump_skid.json").read_text(encoding="utf-8")
        self.assertEqual(strict_json(text), self.document)

    def test_committed_json_is_builder_output_verbatim(self):
        text = (ROOT / "examples" / "pump_skid.json").read_text(encoding="utf-8")
        self.assertEqual(text, make_fixture.dumps_document(self.document))

    def test_envelope_and_sections(self):
        self.assertEqual(set(self.document), {"schema_version", "current", "undo", "redo"})
        self.assertEqual((self.document["schema_version"], self.document["undo"], self.document["redo"]), (1, [], []))
        self.assertEqual(set(self.project), {"id", "revision", "model", "pid", "physical"})
        self.assertIs(type(self.project["revision"]), int)
        self.assertEqual(self.project["revision"], 0)
        self.assertEqual(set(self.model), {"entities", "ports", "connections", "assumptions"})
        self.assertEqual(set(self.project["pid"]), {"symbols", "edges"})
        self.assertEqual(set(self.project["physical"]), {"placements", "routes"})
        self.assertEqual((len(self.entities), len(self.ports), len(self.connections)), (8, 10, 5))

    def test_record_fields_are_exact(self):
        fields = {
            "ports": (self.ports, {"id", "entity_id", "name", "direction", "required", "nominal_diameter_mm", "service", "assumption_id"}),
            "connections": (self.connections, {"id", "tag", "from_port_id", "to_port_id", "nominal_diameter_mm", "outside_diameter_m", "assumption_id"}),
            "symbols": (self.symbols, {"id", "entity_id", "x", "y", "rotation_deg", "manual"}),
            "edges": (self.edges, {"id", "connection_id", "from_symbol_id", "to_symbol_id", "points", "manual", "status"}),
        }
        for section, (records, expected) in fields.items():
            for record_id, record in records.items():
                with self.subTest(section=section, id=record_id):
                    self.assertEqual(set(record), expected)
                    self.assertEqual(record["id"], record_id)
        for entity_id, entity in self.entities.items():
            optional = {"observes"} if entity["kind"] == "indicator" else set()
            self.assertEqual(set(entity), {"id", "kind", "tag", "template_version", "parameters"} | optional)
            self.assertEqual(entity["id"], entity_id)
        for record in self.placements.values():
            self.assertEqual(set(record), {"position_m", "rotation_deg", "status", "manual", "assumption_id"})
        for record in self.routes.values():
            self.assertEqual(set(record), {"points_m", "mode", "status", "assumption_id"})
        for record in self.model["assumptions"].values():
            self.assertEqual(set(record), {"description", "source"})

    def test_entities_match_documented_skid(self):
        self.assertEqual(set(self.by_tag), set(FIXTURE_ENTITIES))
        for tag, (kind, dimensions) in FIXTURE_ENTITIES.items():
            entity = self.by_tag[tag]
            with self.subTest(tag=tag):
                self.assertEqual((entity["kind"], entity["template_version"]), (kind, 1))
                self.assertEqual(set(entity["parameters"]), set(get_template(kind, 1)["parameters"]))
                for name, quantity in entity["parameters"].items():
                    self.assertEqual(set(quantity), {"value", "unit", "assumption_id"})
                    self.assertEqual((quantity["value"], quantity["unit"]), (dimensions[name], "m"))
        tank = self.by_tag["TK-101"]["parameters"]
        self.assertLess(tank["outlet_height"]["value"], tank["height"]["value"])
        self.assertEqual(
            self.by_tag["LI-101"]["observes"], {"entity_id": self.by_tag["TK-101"]["id"], "variable": "level"}
        )
        self.assertEqual(
            self.by_tag["PI-101"]["observes"],
            {"entity_id": self.by_tag["P-101"]["id"], "variable": "discharge_pressure"},
        )

    def test_ids_are_unique_and_not_derived_from_tags(self):
        ids = [self.project["id"]]
        for records in (self.entities, self.ports, self.connections, self.model["assumptions"], self.symbols, self.edges):
            ids.extend(records)
        self.assertEqual(len(ids), len(set(ids)))
        tags = [e["tag"] for e in self.entities.values()] + [c["tag"] for c in self.connections.values()]

        def squash(text):
            return re.sub(r"[^a-z0-9]", "", text.lower())

        for identifier in ids:
            self.assertIsInstance(identifier, str)
            self.assertTrue(identifier.strip())
            for tag in tags:
                self.assertNotIn(squash(tag), squash(identifier))

    def test_tags_are_unique_ignoring_case_and_whitespace(self):
        tags = [e["tag"] for e in self.entities.values()] + [c["tag"] for c in self.connections.values()]
        normalized = [" ".join(tag.split()).casefold() for tag in tags]
        self.assertEqual(len(normalized), len(set(normalized)))

    def test_assumptions_are_complete_and_resolved(self):
        referenced = {q["assumption_id"] for e in self.entities.values() for q in e["parameters"].values()}
        for records in (self.ports, self.connections, self.placements, self.routes):
            referenced.update(record["assumption_id"] for record in records.values())
        self.assertEqual(referenced, set(self.model["assumptions"]))
        for assumption in self.model["assumptions"].values():
            for field in ("description", "source"):
                self.assertIsInstance(assumption[field], str)
                self.assertTrue(assumption[field].strip())
        # Assumption texts describe roles, so renaming equipment cannot stale them.
        tags = [e["tag"] for e in self.entities.values()] + [c["tag"] for c in self.connections.values()]
        for assumption in self.model["assumptions"].values():
            for tag in tags:
                self.assertNotIn(tag, assumption["description"] + assumption["source"])

    def test_ports_follow_templates_and_connect_once(self):
        for entity in self.entities.values():
            template = get_template(entity["kind"], entity["template_version"])
            names = [p["name"] for p in self.ports.values() if p["entity_id"] == entity["id"]]
            with self.subTest(tag=entity["tag"]):
                self.assertEqual(sorted(names), sorted(template["ports"]))
            for port in self.ports.values():
                if port["entity_id"] == entity["id"]:
                    spec = template["ports"][port["name"]]
                    self.assertEqual({k: port[k] for k in spec}, spec)
        endpoints = [c[k] for c in self.connections.values() for k in ("from_port_id", "to_port_id")]
        self.assertEqual(sorted(endpoints), sorted(self.ports))
        for connection in self.connections.values():
            source, target = self.ports[connection["from_port_id"]], self.ports[connection["to_port_id"]]
            self.assertEqual((source["direction"], target["direction"]), ("out", "in"))
            self.assertEqual(connection["nominal_diameter_mm"], 50)
            self.assertEqual(connection["outside_diameter_m"], 0.0603)
            self.assertEqual((source["nominal_diameter_mm"], target["nominal_diameter_mm"]), (50, 50))

    def test_connections_follow_process_order(self):
        by_line = {c["tag"]: c for c in self.connections.values()}
        self.assertEqual(sorted(by_line), list(LINE_TAGS))
        for index, line in enumerate(LINE_TAGS):
            connection = by_line[line]
            upstream = self.entities[self.ports[connection["from_port_id"]]["entity_id"]]["tag"]
            downstream = self.entities[self.ports[connection["to_port_id"]]["entity_id"]]["tag"]
            self.assertEqual((upstream, downstream), PROCESS_ORDER[index : index + 2])

    def test_local_port_frames_are_the_recorded_choices(self):
        for (tag, name), expected in FIXTURE_LOCAL_PORTS.items():
            with self.subTest(tag=tag, port=name):
                self.assertEqual(local_port_position(self.project, self.port_of(tag, name)["id"]), expected)

    def test_every_entity_is_placed(self):
        self.assertEqual(set(self.placements), set(self.entities))
        for entity_id, placement in self.placements.items():
            with self.subTest(entity=entity_id):
                self.assertEqual(placement["status"], "placed")
                self.assertIs(type(placement["manual"]), bool)
                for field in ("position_m", "rotation_deg"):
                    self.assertEqual(len(placement[field]), 3)
                    self.assertTrue(all(type(v) is float and math.isfinite(v) for v in placement[field]))

    def test_nozzle_ends_coincide_with_world_ports(self):
        for port_id in self.ports:
            with self.subTest(port=port_id):
                end, _ = self.world_nozzle(port_id)
                self.assert_close(end, world_port_position(self.project, port_id))

    def test_routes_are_current_orthogonal_and_end_at_nozzles(self):
        self.assertEqual(set(self.routes), set(self.connections))
        for connection_id, route in self.routes.items():
            connection = self.connections[connection_id]
            with self.subTest(connection=connection["tag"]):
                self.assertEqual(route["status"], "current")
                self.assertIn(route["mode"], ("guided", "manual"))
                points = route["points_m"]
                self.assertGreaterEqual(len(points), 2)
                self.assert_close(points[0], world_port_position(self.project, connection["from_port_id"]), 1e-6)
                self.assert_close(points[-1], world_port_position(self.project, connection["to_port_id"]), 1e-6)
                steps = self.axis_steps(points)
                # Pipes leave and enter each nozzle along its axis.
                self.assert_close(steps[0], self.world_nozzle(connection["from_port_id"])[1])
                self.assert_close(steps[-1], [-a for a in self.world_nozzle(connection["to_port_id"])[1]])

    def test_pid_layout_runs_left_to_right(self):
        self.assertEqual(sorted(s["entity_id"] for s in self.symbols.values()), sorted(self.entities))
        symbol_of = {s["entity_id"]: s for s in self.symbols.values()}
        for symbol in self.symbols.values():
            self.assertTrue(all(type(symbol[k]) is float and math.isfinite(symbol[k]) for k in ("x", "y", "rotation_deg")))
            self.assertIs(type(symbol["manual"]), bool)
        xs = [symbol_of[self.by_tag[tag]["id"]]["x"] for tag in PROCESS_ORDER]
        self.assertEqual(xs, sorted(set(xs)))
        for tag in ("LI-101", "PI-101"):
            indicator = self.by_tag[tag]
            self.assertLess(symbol_of[indicator["id"]]["y"], symbol_of[indicator["observes"]["entity_id"]]["y"])

    def test_edges_are_current_orthogonal_and_end_at_anchors(self):
        self.assertEqual(sorted(e["connection_id"] for e in self.edges.values()), sorted(self.connections))
        for edge in self.edges.values():
            connection = self.connections[edge["connection_id"]]
            with self.subTest(connection=connection["tag"]):
                self.assertEqual(edge["status"], "current")
                self.assertIs(type(edge["manual"]), bool)
                source, target = (self.ports[connection[k]] for k in ("from_port_id", "to_port_id"))
                self.assertEqual(self.symbols[edge["from_symbol_id"]]["entity_id"], source["entity_id"])
                self.assertEqual(self.symbols[edge["to_symbol_id"]]["entity_id"], target["entity_id"])
                start = diagram_port_position(self.project, source["id"], edge["from_symbol_id"])
                end = diagram_port_position(self.project, target["id"], edge["to_symbol_id"])
                self.assert_close(edge["points"][0], start)
                self.assert_close(edge["points"][-1], end)
                self.axis_steps(edge["points"])

    def test_manual_presentation_is_exercised(self):
        self.assertTrue(any(symbol["manual"] for symbol in self.symbols.values()))
        self.assertTrue(any(edge["manual"] for edge in self.edges.values()))
        self.assertTrue(any(route["mode"] == "manual" for route in self.routes.values()))


if __name__ == "__main__":
    unittest.main()
