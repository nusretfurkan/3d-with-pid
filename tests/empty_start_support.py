"""Synthetic empty-start fixtures: exact ``add_equipment`` / ``connect_ports`` payloads (docs/EMPTY_START.md).

Every value here is an illustrative test assumption. Stage 1 adds a tank and its
outlet valve with one line; stage 2 adds a pump and a separate suction valve
with two lines; the exchanger scenario adds a shell-and-tube exchanger whose tube
and shell circuits are separate. Commands are plain JSON envelopes that also
serve as published examples for planner integration.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HX_DEFINITION_PATH = ROOT / "examples" / "recipes" / "shell_and_tube_exchanger.definition.json"
SOURCE = "synthetic empty-start acceptance fixture (tests/empty_start_support.py)"
DN50_OD_M = 0.0603

TEMPLATE_PORTS = {
    "tank": {"outlet": "out"},
    "centrifugal_pump": {"inlet": "in", "outlet": "out"},
    "isolation_valve": {"inlet": "in", "outlet": "out"},
    "check_valve": {"inlet": "in", "outlet": "out"},
    "boundary": {"inlet": "in"},
    "indicator": {},
}


def hx_definition():
    with open(HX_DEFINITION_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def assumption(description):
    return {"description": description, "source": SOURCE}


def ports_for(entity_id, directions, assumption_id):
    return [{"id": f"port-{entity_id[4:]}-{name.replace('_', '-')}", "entity_id": entity_id, "name": name,
             "direction": direction, "required": True, "nominal_diameter_mm": 50, "service": "liquid",
             "assumption_id": assumption_id} for name, direction in directions.items()]


def port_id(entity_id, name):
    return f"port-{entity_id[4:]}-{name.replace('_', '-')}"


def placed(position, assumption_id, rotation=(0.0, 0.0, 0.0)):
    return {"position_m": list(position), "rotation_deg": list(rotation), "status": "placed", "manual": False,
            "assumption_id": assumption_id}


def add_builtin(entity_id, kind, tag, parameters, symbol_xy, assumption_id, ports_assumption, placement=None):
    """``add_equipment`` operation for a built-in kind; parameters ``{name: value_m}``."""
    payload = {
        "entity": {"id": entity_id, "kind": kind, "tag": tag, "template_version": 1,
                   "parameters": {name: {"value": value, "unit": "m", "assumption_id": assumption_id}
                                  for name, value in parameters.items()}},
        "ports": ports_for(entity_id, TEMPLATE_PORTS[kind], ports_assumption),
        "symbol": {"id": f"sym-{entity_id[4:]}", "entity_id": entity_id, "x": symbol_xy[0], "y": symbol_xy[1],
                   "rotation_deg": 0.0, "manual": False},
    }
    if placement is not None:
        payload["placement"] = placement
    return {"type": "add_equipment", "payload": payload}


def add_exchanger(entity_id, tag, symbol_xy, assumption_id, ports_assumption, placement=None, **values):
    definition = hx_definition()
    parameters = {name: {"value": values.get(name, record["value"]), "unit": "m", "assumption_id": assumption_id}
                  for name, record in definition["geometry"]["parameters"].items()}
    payload = {
        "entity": {"id": entity_id, "kind": "composed_equipment", "tag": tag, "template_version": 1,
                   "definition": definition, "parameters": parameters},
        "ports": ports_for(entity_id, {name: record["direction"] for name, record in definition["ports"].items()},
                           ports_assumption),
        "symbol": {"id": f"sym-{entity_id[4:]}", "entity_id": entity_id, "x": symbol_xy[0], "y": symbol_xy[1],
                   "rotation_deg": 0.0, "manual": False},
    }
    if placement is not None:
        payload["placement"] = placement
    return {"type": "add_equipment", "payload": payload}


def connect(connection_id, tag, from_port, to_port, from_symbol, to_symbol, assumption_id, route=None,
            route_assumption=None):
    """``connect_ports`` operation; an empty non-manual current edge asks the core for the guided P&ID path."""
    payload = {
        "connection": {"id": connection_id, "tag": tag, "from_port_id": from_port, "to_port_id": to_port,
                       "nominal_diameter_mm": 50, "outside_diameter_m": DN50_OD_M, "assumption_id": assumption_id},
        "edge": {"id": f"edge-{connection_id[5:]}", "connection_id": connection_id, "from_symbol_id": from_symbol,
                 "to_symbol_id": to_symbol, "points": [], "manual": False, "status": "current"},
    }
    if route is not None:
        payload["route"] = {"points_m": [list(p) for p in route], "mode": "manual", "status": "current",
                            "assumption_id": route_assumption}
    return {"type": "connect_ports", "payload": payload}


def batch(document, command_id, operations):
    return {"id": command_id, "expected_revision": document["current"]["revision"], "type": "batch",
            "payload": {"operations": copy.deepcopy(operations)}}


# --- Stage 1: tank and its outlet valve -----------------------------------------------------------


def stage1_operations():
    tank = add_builtin("ent-tk-101", "tank", "TK-101", {"diameter": 1.0, "height": 1.6, "outlet_height": 0.2},
                       (120.0, 300.0), "asm-tk-101", "asm-dn50-liquid",
                       placed((0.0, 0.0, 0.0), "asm-layout-stage-1"))
    tank["assumptions"] = {
        "asm-tk-101": assumption("Illustrative vertical tank: 1.0 m diameter, 1.6 m shell height, side outlet "
                                 "0.2 m above the base. Capacity, levels and design conditions are unknown."),
        "asm-dn50-liquid": assumption("Process ports are DN50 in liquid service; duty, pressure, temperature and "
                                      "material are unknown."),
        "asm-layout-stage-1": assumption("Illustrative stage-1 layout on the skid floor: tank at the origin, outlet "
                                         "valve 1.0 m along +x at the outlet height."),
    }
    valve = add_builtin("ent-hv-101", "isolation_valve", "HV-101", {"length": 0.16, "body_diameter": 0.1},
                        (240.0, 320.0), "asm-hv-dimensions", "asm-dn50-liquid",
                        placed((1.0, 0.0, 0.2), "asm-layout-stage-1"))
    valve["assumptions"] = {"asm-hv-dimensions": assumption("Illustrative DN50 isolation valve: 0.16 m face to "
                                                            "face, 0.1 m body; operator and normal position unknown.")}
    line = connect("conn-l-101", "L-101", port_id("ent-tk-101", "outlet"), port_id("ent-hv-101", "inlet"),
                   "sym-tk-101", "sym-hv-101", "asm-dn50-pipe", [[0.6, 0.0, 0.2], [0.92, 0.0, 0.2]],
                   "asm-route-stage-1")
    line["assumptions"] = {
        "asm-dn50-pipe": assumption("DN50 line with an assumed 60.3 mm outside diameter; schedule and material "
                                    "unknown."),
        "asm-route-stage-1": assumption("Illustrative straight manual route at the outlet height."),
    }
    return [tank, valve, line]


# --- Stage 2: pump and a separate suction valve ------------------------------------------------------


def stage2_operations():
    pump = add_builtin("ent-p-101", "centrifugal_pump", "P-101",
                       {"body_length": 0.4, "body_width": 0.3, "body_height": 0.35, "motor_length": 0.35,
                        "motor_diameter": 0.22},
                       (460.0, 320.0), "asm-p-101", "asm-dn50-liquid", placed((2.0, 0.0, 0.025), "asm-layout-stage-2"))
    pump["assumptions"] = {
        "asm-p-101": assumption("Illustrative end-suction centrifugal pump envelope; duty, speed and driver rating "
                                "unknown."),
        "asm-layout-stage-2": assumption("Illustrative stage-2 layout: suction valve 1.4 m and pump 2.0 m along +x, "
                                         "pump inlet at the 0.2 m line height."),
    }
    suction = add_builtin("ent-hv-102", "isolation_valve", "HV-102", {"length": 0.16, "body_diameter": 0.1},
                          (340.0, 320.0), "asm-hv-dimensions", "asm-dn50-liquid",
                          placed((1.4, 0.0, 0.2), "asm-layout-stage-2"))
    first = connect("conn-l-102", "L-102", port_id("ent-hv-101", "outlet"), port_id("ent-hv-102", "inlet"),
                    "sym-hv-101", "sym-hv-102", "asm-dn50-pipe", [[1.08, 0.0, 0.2], [1.32, 0.0, 0.2]],
                    "asm-route-stage-2")
    first["assumptions"] = {"asm-route-stage-2": assumption("Illustrative straight manual routes at 0.2 m.")}
    second = connect("conn-l-103", "L-103", port_id("ent-hv-102", "outlet"), port_id("ent-p-101", "inlet"),
                     "sym-hv-102", "sym-p-101", "asm-dn50-pipe", [[1.48, 0.0, 0.2], [1.7, 0.0, 0.2]],
                     "asm-route-stage-2")
    return [pump, suction, first, second]


def stage3_operations():
    """A discharge scope boundary closes the last open port (P-101 outlet): every required port is then joined."""
    boundary = add_builtin("ent-out-101", "boundary", "OUT-101", {"size": 0.2}, (578.0, 292.0), "asm-out-101",
                           "asm-dn50-liquid", placed((2.6, 0.0, 0.775), "asm-layout-stage-3"))
    boundary["assumptions"] = {
        "asm-out-101": assumption("Illustrative 0.2 m scope boundary marker; the destination is outside the model."),
        "asm-layout-stage-3": assumption("Illustrative stage-3 layout: discharge boundary 0.3 m above the pump "
                                         "outlet and 0.6 m along +x."),
    }
    line = connect("conn-l-104", "L-104", port_id("ent-p-101", "outlet"), port_id("ent-out-101", "inlet"),
                   "sym-p-101", "sym-out-101", "asm-dn50-pipe",
                   [[2.0, 0.0, 0.475], [2.0, 0.0, 0.775], [2.5, 0.0, 0.775]], "asm-route-stage-3")
    line["assumptions"] = {"asm-route-stage-3": assumption("Illustrative manual discharge route: up 0.3 m, then "
                                                           "0.5 m along +x.")}
    return [boundary, line]


# --- Exchanger with two separate circuits -------------------------------------------------------------


def exchanger_operations(place=True):
    """Tube circuit TK-201 -> P-201 -> E-101 tube side -> OUT-201; shell circuit TK-202 -> E-101 shell side ->
    HV-202 -> OUT-202. Routes are left unresolved (pending): circuit topology is the point."""
    def at(position):
        return placed(position, "asm-layout-hx") if place else None

    # P&ID positions keep the existing guided (Z-shaped) edges clear of the symbols: the tube circuit enters and
    # leaves at the channel end on the left, the shell circuit below and above the shell.
    hx = add_exchanger("ent-e-101", "E-101", (520.0, 300.0), "asm-e-101", "asm-dn50-liquid", at((2.6, 0.0, 0.0)))
    hx["assumptions"] = {
        "asm-e-101": assumption("Illustrative exchanger dimensions from the example definition defaults; duty and "
                                "sizing are unknown and not verified."),
        "asm-dn50-liquid": assumption("Process ports are DN50 in liquid service; duty, pressure, temperature and "
                                      "material are unknown."),
        "asm-items-hx": assumption("Illustrative tank, pump, valve and boundary sizes for the exchanger scenario."),
    }
    if place:
        hx["assumptions"]["asm-layout-hx"] = assumption("Illustrative exchanger skid layout; routes are left pending.")
    items = [
        add_builtin("ent-tk-201", "tank", "TK-201", {"diameter": 0.8, "height": 1.2, "outlet_height": 0.2},
                    (100.0, 322.0), "asm-items-hx", "asm-dn50-liquid", at((0.0, -1.5, 0.0))),
        add_builtin("ent-p-201", "centrifugal_pump", "P-201",
                    {"body_length": 0.4, "body_width": 0.3, "body_height": 0.35, "motor_length": 0.35,
                     "motor_diameter": 0.22}, (300.0, 342.0), "asm-items-hx", "asm-dn50-liquid",
                    at((1.2, -1.5, 0.0))),
        add_builtin("ent-out-201", "boundary", "OUT-201", {"size": 0.2}, (300.0, 200.0), "asm-items-hx",
                    "asm-dn50-liquid", at((4.2, -1.5, 1.2))),
        add_builtin("ent-tk-202", "tank", "TK-202", {"diameter": 0.8, "height": 1.2, "outlet_height": 0.2},
                    (100.0, 522.0), "asm-items-hx", "asm-dn50-liquid", at((0.0, 1.5, 0.0))),
        add_builtin("ent-hv-202", "isolation_valve", "HV-202", {"length": 0.16, "body_diameter": 0.1},
                    (700.0, 242.0), "asm-items-hx", "asm-dn50-liquid", at((3.4, 1.5, 0.3))),
        add_builtin("ent-out-202", "boundary", "OUT-202", {"size": 0.2}, (800.0, 242.0), "asm-items-hx",
                    "asm-dn50-liquid", at((4.2, 1.5, 0.3))),
    ]
    items[2]["payload"]["symbol"]["rotation_deg"] = 180.0  # the tube return leaves to the left
    lines = [
        connect("conn-l-201", "L-201", port_id("ent-tk-201", "outlet"), port_id("ent-p-201", "inlet"),
                "sym-tk-201", "sym-p-201", "asm-dn50-pipe"),
        connect("conn-l-202", "L-202", port_id("ent-p-201", "outlet"), port_id("ent-e-101", "tube_inlet"),
                "sym-p-201", "sym-e-101", "asm-dn50-pipe"),
        connect("conn-l-203", "L-203", port_id("ent-e-101", "tube_outlet"), port_id("ent-out-201", "inlet"),
                "sym-e-101", "sym-out-201", "asm-dn50-pipe"),
        connect("conn-l-204", "L-204", port_id("ent-tk-202", "outlet"), port_id("ent-e-101", "shell_inlet"),
                "sym-tk-202", "sym-e-101", "asm-dn50-pipe"),
        connect("conn-l-205", "L-205", port_id("ent-e-101", "shell_outlet"), port_id("ent-hv-202", "inlet"),
                "sym-e-101", "sym-hv-202", "asm-dn50-pipe"),
        connect("conn-l-206", "L-206", port_id("ent-hv-202", "outlet"), port_id("ent-out-202", "inlet"),
                "sym-hv-202", "sym-out-202", "asm-dn50-pipe"),
    ]
    lines[0]["assumptions"] = {"asm-dn50-pipe": assumption("DN50 line with an assumed 60.3 mm outside diameter.")}
    return [hx, *items], lines
