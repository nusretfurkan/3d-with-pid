"""Build the prototype pump-skid fixture document.

Process order: TK-101 -> HV-101 -> P-101 -> NRV-101 -> HV-102 -> OUT-101, with
LI-101 and PI-101 as conceptual indications. Every dimension, placement, and
route is an illustrative prototype assumption recorded in the assumption map.
IDs are fixed and never derived from tags; assumption texts describe roles
rather than tags so that renaming leaves them accurate. Physical frames follow
``skid.templates`` version 1.

Regenerate ``examples/pump_skid.json`` from the workspace root with::

    python3 -m examples.make_fixture
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_PATH = Path(__file__).with_name("pump_skid.json")
PROJECT_ID = "project-pump-skid"

# (assumption ID, description, source)
_ASSUMPTIONS = (
    (
        "asm-tank-dimensions",
        "Illustrative vertical cylindrical tank: diameter 0.8 m and height 1.2 m. "
        "The outlet nozzle centreline is 0.2 m above the tank base, chosen to align "
        "with the pump suction centreline. Not a vessel design.",
        "Diameter and height: docs/PUMP_SKID.md prototype values. "
        "Outlet height: E1a fixture choice.",
    ),
    (
        "asm-pump-dimensions",
        "Illustrative centrifugal pump envelopes: body 0.45 m long (x) by 0.30 m wide (y) "
        "by 0.40 m high (z); motor 0.35 m long by 0.22 m diameter on the shaft axis. "
        "End suction and top discharge follow template centrifugal_pump version 1. "
        "Not a vendor selection.",
        "docs/PUMP_SKID.md prototype values.",
    ),
    (
        "asm-isolation-valve-dimensions",
        "Illustrative DN50 suction and discharge isolation valves: face-to-face length "
        "0.16 m and body diameter 0.10 m.",
        "Length: docs/PUMP_SKID.md prototype value. Body diameter: E1a fixture choice.",
    ),
    (
        "asm-check-valve-dimensions",
        "Illustrative DN50 check valve: face-to-face length 0.18 m and body diameter "
        "0.11 m. Its inlet-to-outlet direction follows process flow.",
        "Length: docs/PUMP_SKID.md prototype value. Body diameter: E1a fixture choice.",
    ),
    (
        "asm-indicator-dial",
        "Conceptual level and pressure indications drawn with an illustrative 0.08 m dial. "
        "Not an instrument specification or hookup design.",
        "docs/PUMP_SKID.md prototype value.",
    ),
    (
        "asm-boundary-marker",
        "Scope boundary marker with an illustrative 0.2 m size. Downstream plant is "
        "outside the modeled scope.",
        "Size: E1a fixture choice. Boundary role: docs/PUMP_SKID.md.",
    ),
    (
        "asm-dn50-liquid-service",
        "All process ports are DN50 (a nominal designation in millimetres, not an outside "
        "diameter) in liquid service. Duty, flow, pressure, temperature, material, and "
        "wall thickness are unknown. The prototype omits operating and safety design.",
        "docs/PUMP_SKID.md prototype assumption.",
    ),
    (
        "asm-dn50-pipe",
        "All five process connections are DN50 with an assumed 0.0603 m outside diameter. "
        "This is not a selected pipe schedule or fabrication specification. Liquid service "
        "with unknown duty, flow, pressure, temperature, material, and wall thickness.",
        "docs/PUMP_SKID.md prototype assumption.",
    ),
    (
        "asm-skid-layout",
        "Illustrative skid arrangement. The deck top is z = 0 m; process flow runs along +x "
        "at y = 0 until the discharge turns toward -y. Placement origins use template "
        "version 1 local frames: tank shell base centre (0, 0, 0); suction valve centre "
        "(0.8, 0, 0.2); pump body base centre (1.5, 0, 0); check valve centre (2.3, 0, 0.7); "
        "discharge valve centre (2.7, -0.45, 0.7) and boundary marker centre "
        "(2.7, -0.85, 0.7), both rotated -90 degrees about z. Tank and pump nozzles project "
        "0.10 m from their bodies. Not a plant layout, support, or access design.",
        "E1a fixture choice.",
    ),
    (
        "asm-indicator-mounts",
        "Conceptual indication mount points: level indication on the tank top centre "
        "(0, 0, 1.2); pressure indication on top of the pump discharge line at "
        "(1.9, 0, 0.73). No tapping, hookup, or support is modeled.",
        "E1a fixture choice.",
    ),
    (
        "asm-guided-routes",
        "Guided orthogonal centreline routes chosen once for the fixture, leaving and "
        "entering each nozzle along its axis: tank to suction valve (0.5, 0, 0.2) to "
        "(0.72, 0, 0.2); suction valve to pump (0.88, 0, 0.2) to (1.175, 0, 0.2); check "
        "valve to discharge valve (2.39, 0, 0.7) via (2.7, 0, 0.7) to (2.7, -0.37, 0.7); "
        "discharge valve to boundary (2.7, -0.53, 0.7) to (2.7, -0.75, 0.7). No fittings, "
        "supports, slope, or clash checks.",
        "E1a fixture choice.",
    ),
    (
        "asm-manual-route",
        "Manually chosen pump discharge route: rises from the pump outlet (1.5, 0, 0.5) to "
        "(1.5, 0, 0.7), then runs along +x above the motor to the check valve inlet "
        "(2.21, 0, 0.7). Included to exercise preservation of manual routing.",
        "E1a fixture choice.",
    ),
)

# (entity ID, kind, tag, parameters in metres, parameter assumption ID)
_ENTITIES = (
    (
        "ent-tank",
        "tank",
        "TK-101",
        (("diameter", 0.8), ("height", 1.2), ("outlet_height", 0.2)),
        "asm-tank-dimensions",
    ),
    (
        "ent-suction-valve",
        "isolation_valve",
        "HV-101",
        (("length", 0.16), ("body_diameter", 0.1)),
        "asm-isolation-valve-dimensions",
    ),
    (
        "ent-pump",
        "centrifugal_pump",
        "P-101",
        (
            ("body_length", 0.45),
            ("body_width", 0.3),
            ("body_height", 0.4),
            ("motor_length", 0.35),
            ("motor_diameter", 0.22),
        ),
        "asm-pump-dimensions",
    ),
    (
        "ent-check-valve",
        "check_valve",
        "NRV-101",
        (("length", 0.18), ("body_diameter", 0.11)),
        "asm-check-valve-dimensions",
    ),
    (
        "ent-discharge-valve",
        "isolation_valve",
        "HV-102",
        (("length", 0.16), ("body_diameter", 0.1)),
        "asm-isolation-valve-dimensions",
    ),
    ("ent-outlet-boundary", "boundary", "OUT-101", (("size", 0.2),), "asm-boundary-marker"),
    ("ent-level-indicator", "indicator", "LI-101", (("diameter", 0.08),), "asm-indicator-dial"),
    ("ent-pressure-indicator", "indicator", "PI-101", (("diameter", 0.08),), "asm-indicator-dial"),
)

# indicator entity ID -> (observed entity ID, variable)
_OBSERVES = {
    "ent-level-indicator": ("ent-tank", "level"),
    "ent-pressure-indicator": ("ent-pump", "discharge_pressure"),
}

# (port ID, entity ID, template port name, direction)
_PORTS = (
    ("port-tank-outlet", "ent-tank", "outlet", "out"),
    ("port-suction-valve-inlet", "ent-suction-valve", "inlet", "in"),
    ("port-suction-valve-outlet", "ent-suction-valve", "outlet", "out"),
    ("port-pump-inlet", "ent-pump", "inlet", "in"),
    ("port-pump-outlet", "ent-pump", "outlet", "out"),
    ("port-check-valve-inlet", "ent-check-valve", "inlet", "in"),
    ("port-check-valve-outlet", "ent-check-valve", "outlet", "out"),
    ("port-discharge-valve-inlet", "ent-discharge-valve", "inlet", "in"),
    ("port-discharge-valve-outlet", "ent-discharge-valve", "outlet", "out"),
    ("port-boundary-inlet", "ent-outlet-boundary", "inlet", "in"),
)

# (connection ID, tag, from port ID, to port ID), in process order.
_CONNECTIONS = (
    ("conn-suction-1", "L-101", "port-tank-outlet", "port-suction-valve-inlet"),
    ("conn-suction-2", "L-102", "port-suction-valve-outlet", "port-pump-inlet"),
    ("conn-discharge-1", "L-103", "port-pump-outlet", "port-check-valve-inlet"),
    ("conn-discharge-2", "L-104", "port-check-valve-outlet", "port-discharge-valve-inlet"),
    ("conn-discharge-3", "L-105", "port-discharge-valve-outlet", "port-boundary-inlet"),
)

# P&ID layout in drawing units, +x right and +y down, left to right with room
# above the equipment for labels and indications. All rotations are 0.
# entity ID -> (symbol ID, x, y, manual)
_SYMBOLS = {
    "ent-tank": ("sym-tank", 120.0, 280.0, False),
    "ent-suction-valve": ("sym-suction-valve", 240.0, 300.0, False),
    "ent-pump": ("sym-pump", 360.0, 300.0, False),
    "ent-check-valve": ("sym-check-valve", 480.0, 220.0, True),
    "ent-discharge-valve": ("sym-discharge-valve", 600.0, 220.0, False),
    "ent-outlet-boundary": ("sym-outlet-boundary", 720.0, 220.0, False),
    "ent-level-indicator": ("sym-level-indicator", 120.0, 140.0, False),
    "ent-pressure-indicator": ("sym-pressure-indicator", 420.0, 140.0, False),
}

# connection ID -> (edge ID, points, manual); endpoints sit on template anchors.
_EDGES = {
    "conn-suction-1": ("edge-suction-1", ((160.0, 300.0), (224.0, 300.0)), False),
    "conn-suction-2": ("edge-suction-2", ((256.0, 300.0), (332.0, 300.0)), False),
    "conn-discharge-1": (
        "edge-discharge-1",
        ((360.0, 272.0), (360.0, 220.0), (464.0, 220.0)),
        True,
    ),
    "conn-discharge-2": ("edge-discharge-2", ((496.0, 220.0), (584.0, 220.0)), False),
    "conn-discharge-3": ("edge-discharge-3", ((616.0, 220.0), (702.0, 220.0)), False),
}

# entity ID -> (position_m, rotation_deg, assumption ID)
_PLACEMENTS = {
    "ent-tank": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "asm-skid-layout"),
    "ent-suction-valve": ((0.8, 0.0, 0.2), (0.0, 0.0, 0.0), "asm-skid-layout"),
    "ent-pump": ((1.5, 0.0, 0.0), (0.0, 0.0, 0.0), "asm-skid-layout"),
    "ent-check-valve": ((2.3, 0.0, 0.7), (0.0, 0.0, 0.0), "asm-skid-layout"),
    "ent-discharge-valve": ((2.7, -0.45, 0.7), (0.0, 0.0, -90.0), "asm-skid-layout"),
    "ent-outlet-boundary": ((2.7, -0.85, 0.7), (0.0, 0.0, -90.0), "asm-skid-layout"),
    "ent-level-indicator": ((0.0, 0.0, 1.2), (0.0, 0.0, 0.0), "asm-indicator-mounts"),
    "ent-pressure-indicator": ((1.9, 0.0, 0.73), (0.0, 0.0, 0.0), "asm-indicator-mounts"),
}

# connection ID -> (points_m, mode, assumption ID); endpoints are world port positions.
_ROUTES = {
    "conn-suction-1": (((0.5, 0.0, 0.2), (0.72, 0.0, 0.2)), "guided", "asm-guided-routes"),
    "conn-suction-2": (((0.88, 0.0, 0.2), (1.175, 0.0, 0.2)), "guided", "asm-guided-routes"),
    "conn-discharge-1": (
        ((1.5, 0.0, 0.5), (1.5, 0.0, 0.7), (2.21, 0.0, 0.7)),
        "manual",
        "asm-manual-route",
    ),
    "conn-discharge-2": (
        ((2.39, 0.0, 0.7), (2.7, 0.0, 0.7), (2.7, -0.37, 0.7)),
        "guided",
        "asm-guided-routes",
    ),
    "conn-discharge-3": (((2.7, -0.53, 0.7), (2.7, -0.75, 0.7)), "guided", "asm-guided-routes"),
}


def make_document() -> dict:
    """Return a fresh pump-skid fixture document with empty history."""
    return {"schema_version": 1, "current": _project(), "undo": [], "redo": []}


def _project() -> dict:
    entities = {}
    for entity_id, kind, tag, parameters, assumption_id in _ENTITIES:
        entity = {
            "id": entity_id,
            "kind": kind,
            "tag": tag,
            "template_version": 1,
            "parameters": {
                name: {"value": value, "unit": "m", "assumption_id": assumption_id}
                for name, value in parameters
            },
        }
        if entity_id in _OBSERVES:
            subject_id, variable = _OBSERVES[entity_id]
            entity["observes"] = {"entity_id": subject_id, "variable": variable}
        entities[entity_id] = entity

    ports = {
        port_id: {
            "id": port_id,
            "entity_id": entity_id,
            "name": name,
            "direction": direction,
            "required": True,
            "nominal_diameter_mm": 50,
            "service": "liquid",
            "assumption_id": "asm-dn50-liquid-service",
        }
        for port_id, entity_id, name, direction in _PORTS
    }

    connections = {}
    edges = {}
    for connection_id, tag, from_port_id, to_port_id in _CONNECTIONS:
        connections[connection_id] = {
            "id": connection_id,
            "tag": tag,
            "from_port_id": from_port_id,
            "to_port_id": to_port_id,
            "nominal_diameter_mm": 50,
            "outside_diameter_m": 0.0603,
            "assumption_id": "asm-dn50-pipe",
        }
        edge_id, points, manual = _EDGES[connection_id]
        edges[edge_id] = {
            "id": edge_id,
            "connection_id": connection_id,
            "from_symbol_id": _SYMBOLS[ports[from_port_id]["entity_id"]][0],
            "to_symbol_id": _SYMBOLS[ports[to_port_id]["entity_id"]][0],
            "points": [list(point) for point in points],
            "manual": manual,
            "status": "current",
        }

    symbols = {
        symbol_id: {
            "id": symbol_id,
            "entity_id": entity_id,
            "x": x,
            "y": y,
            "rotation_deg": 0.0,
            "manual": manual,
        }
        for entity_id, (symbol_id, x, y, manual) in _SYMBOLS.items()
    }

    placements = {
        entity_id: {
            "position_m": list(position),
            "rotation_deg": list(rotation),
            "status": "placed",
            "manual": False,
            "assumption_id": assumption_id,
        }
        for entity_id, (position, rotation, assumption_id) in _PLACEMENTS.items()
    }

    routes = {
        connection_id: {
            "points_m": [list(point) for point in points],
            "mode": mode,
            "status": "current",
            "assumption_id": assumption_id,
        }
        for connection_id, (points, mode, assumption_id) in _ROUTES.items()
    }

    return {
        "id": PROJECT_ID,
        "revision": 0,
        "model": {
            "entities": entities,
            "ports": ports,
            "connections": connections,
            "assumptions": {
                assumption_id: {"description": description, "source": source}
                for assumption_id, description, source in _ASSUMPTIONS
            },
        },
        "pid": {"symbols": symbols, "edges": edges},
        "physical": {"placements": placements, "routes": routes},
    }


def dumps_document(document: dict) -> str:
    """Serialize a document the way the committed fixture is written."""
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return text + "\n"


def main() -> int:
    FIXTURE_PATH.write_text(dumps_document(make_document()), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
