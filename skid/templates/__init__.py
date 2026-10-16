"""Versioned equipment templates for the pump-skid prototype.

Each template declares its parameters, process ports, and P&ID port anchors,
and derives illustrative physical primitives from an entity's recorded
parameters. The geometry is a parameterized visualization, not a vessel, pump,
or valve design. This module must not import ``skid.core``.

Conventions for template version 1:

- Physical lengths are metres in a right-handed frame with +z up. Angles are
  degrees applied in XYZ order about fixed axes, ``R = Rz @ Ry @ Rx``, which
  matches Blender's ``XYZ`` Euler mode.
- Primitive ``center_m`` and ``rotation_deg`` are in the entity's local frame;
  the entity's placement maps that frame into the world. A cylinder's axis is
  its own local +z.
- Every port has a cylinder keyed ``nozzle/<port name>`` whose +z end is the
  port position and whose +z axis is the port's outward direction.
- P&ID anchors are rotated by the symbol's ``rotation_deg`` and then translated
  by its ``x`` and ``y``, as SVG ``translate(x y) rotate(angle)`` does. With +y
  down, positive angles turn clockwise on screen.

Local port frames, with ``P = NOZZLE_PROJECTION_M``:

- ``tank``: origin at the shell base centre, shell axis +z. ``outlet`` at
  ``(diameter/2 + P, 0, outlet_height)``, facing +x.
- ``centrifugal_pump``: origin at the body base centre, shaft along +x, motor
  on the +x side. ``inlet`` (end suction) at
  ``(-body_length/2 - P, 0, body_height/2)``, facing -x. ``outlet`` (top
  discharge) at ``(0, 0, body_height + P)``, facing +z.
- ``isolation_valve`` and ``check_valve``: origin at the body centre on the
  pipe axis, flow along +x. ``inlet`` at ``(-length/2, 0, 0)``, facing -x;
  ``outlet`` at ``(length/2, 0, 0)``, facing +x.
- ``indicator``: origin at the mount point, stem along +z, dial facing -y. No
  ports.
- ``boundary``: origin at the marker centre. ``inlet`` at ``(-size/2, 0, 0)``,
  facing -x.

Unknown templates, entities, ports, parameters, and symbols raise ``KeyError``.
Invalid values raise ``ValueError``. Inter-parameter rules, such as a tank
outlet lying within the tank height, belong to project validation.

``composed_equipment`` entities carry their own definition (``composed``;
public interface ``skid.equipment``) and have no entry in this module's fixed
table: ``get_template`` keeps rejecting that kind, and generic callers use
``entity_template(entity)``. Their local port positions come from
``resolve_definition`` (the definition's millimetre port frames at the
entity's current parameters, divided by 1000) and their P&ID anchors from the
definition's symbol; placement and symbol transforms are the same as above.
They have no version-1 primitives: ``entity_primitives`` raises
``DefinitionError`` (a ``ValueError``) rather than inventing generic solids.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .composed import composed_template, is_composed, resolve_definition
from .errors import DefinitionError

__all__ = [
    "NOZZLE_PROJECTION_M",
    "NOZZLE_RADIUS_M",
    "diagram_port_position",
    "entity_primitives",
    "entity_template",
    "get_template",
    "local_port_position",
    "world_port_position",
]

NOZZLE_RADIUS_M = 0.03
"""Illustrative nozzle stub radius, about half the assumed DN50 pipe OD."""

NOZZLE_PROJECTION_M = 0.10
"""Distance a tank or pump nozzle projects beyond the body surface."""

# Results are rounded to remove floating-point noise far below the 1e-6 m
# endpoint tolerance, so fixture coordinates can be written as plain decimals.
_DIGITS = 12

# Outward axis -> (unit vector, rotation turning a primitive's +z onto it).
_AXES = {
    "+x": ((1.0, 0.0, 0.0), (0.0, 90.0, 0.0)),
    "-x": ((-1.0, 0.0, 0.0), (0.0, -90.0, 0.0)),
    "-y": ((0.0, -1.0, 0.0), (90.0, 0.0, 0.0)),
    "+z": ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)),
}

_QUARTER_TURNS = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))

# Port name -> (local point, outward axis, nozzle stub length).
_PortFrames = dict[str, tuple[tuple[float, float, float], str, float]]


@dataclass(frozen=True)
class _Template:
    parameters: tuple[str, ...]
    ports: tuple[tuple[str, str, tuple[int, int]], ...]  # name, direction, anchor
    geometry: Callable[[dict[str, float]], tuple[list[dict], _PortFrames]]


# --- Primitive builders ------------------------------------------------------


def _clean(value: float) -> float:
    return round(float(value), _DIGITS) + 0.0  # + 0.0 turns -0.0 into 0.0


def _point(values) -> list[float]:
    return [_clean(v) for v in values]


def _box(key: str, center, size) -> dict:
    return {
        "key": key,
        "shape": "box",
        "center_m": _point(center),
        "rotation_deg": [0.0, 0.0, 0.0],
        "size_m": _point(size),
    }


def _cylinder(key: str, center, axis: str, radius: float, length: float) -> dict:
    return {
        "key": key,
        "shape": "cylinder",
        "center_m": _point(center),
        "rotation_deg": list(_AXES[axis][1]),
        "radius_m": _clean(radius),
        "length_m": _clean(length),
    }


def _sphere(key: str, center, radius: float) -> dict:
    return {
        "key": key,
        "shape": "sphere",
        "center_m": _point(center),
        "rotation_deg": [0.0, 0.0, 0.0],
        "radius_m": _clean(radius),
    }


# --- Template geometry -------------------------------------------------------


def _tank(p: dict[str, float]):
    radius, height = p["diameter"] / 2, p["height"]
    bodies = [_cylinder("body", (0, 0, height / 2), "+z", radius, height)]
    # The stub starts one nozzle radius inside the curved shell to avoid a gap.
    outlet = (radius + NOZZLE_PROJECTION_M, 0.0, p["outlet_height"])
    ports = {"outlet": (outlet, "+x", NOZZLE_PROJECTION_M + NOZZLE_RADIUS_M)}
    return bodies, ports


def _centrifugal_pump(p: dict[str, float]):
    length, width, height = p["body_length"], p["body_width"], p["body_height"]
    motor_length = p["motor_length"]
    shaft_z = height / 2
    bodies = [
        _box("body", (0, 0, height / 2), (length, width, height)),
        _cylinder(
            "motor",
            (length / 2 + motor_length / 2, 0, shaft_z),
            "+x",
            p["motor_diameter"] / 2,
            motor_length,
        ),
    ]
    ports = {
        "inlet": ((-length / 2 - NOZZLE_PROJECTION_M, 0.0, shaft_z), "-x", NOZZLE_PROJECTION_M),
        "outlet": ((0.0, 0.0, height + NOZZLE_PROJECTION_M), "+z", NOZZLE_PROJECTION_M),
    }
    return bodies, ports


def _valve_ports(length: float) -> _PortFrames:
    # End stubs run from the body centre to each face.
    half = length / 2
    return {
        "inlet": ((-half, 0.0, 0.0), "-x", half),
        "outlet": ((half, 0.0, 0.0), "+x", half),
    }


def _isolation_valve(p: dict[str, float]):
    length, diameter = p["length"], p["body_diameter"]
    bodies = [
        _sphere("body", (0, 0, 0), diameter / 2),
        _cylinder("stem", (0, 0, diameter / 2), "+z", diameter / 10, diameter),
        _box("handle", (0, 0, diameter + diameter / 20), (length, diameter / 5, diameter / 10)),
    ]
    return bodies, _valve_ports(length)


def _check_valve(p: dict[str, float]):
    length, diameter = p["length"], p["body_diameter"]
    bodies = [
        _cylinder("body", (0, 0, 0), "+x", diameter / 2, length / 2),
        _box("cover", (0, 0, diameter / 2), (length / 3, diameter / 2, diameter / 4)),
    ]
    return bodies, _valve_ports(length)


def _indicator(p: dict[str, float]):
    diameter = p["diameter"]
    bodies = [
        _cylinder("stem", (0, 0, diameter / 4), "+z", diameter / 10, diameter / 2),
        _cylinder("body", (0, 0, diameter), "-y", diameter / 2, diameter / 4),
    ]
    return bodies, {}


def _boundary(p: dict[str, float]):
    size = p["size"]
    bodies = [_box("body", (0, 0, 0), (size / 4, size, size))]
    return bodies, {"inlet": ((-size / 2, 0.0, 0.0), "-x", size / 2)}


_VALVE_PORTS = (("inlet", "in", (-16, 0)), ("outlet", "out", (16, 0)))

_TEMPLATES = {
    ("tank", 1): _Template(
        ("diameter", "height", "outlet_height"),
        (("outlet", "out", (40, 20)),),
        _tank,
    ),
    ("centrifugal_pump", 1): _Template(
        ("body_length", "body_width", "body_height", "motor_length", "motor_diameter"),
        (("inlet", "in", (-28, 0)), ("outlet", "out", (0, -28))),
        _centrifugal_pump,
    ),
    ("isolation_valve", 1): _Template(("length", "body_diameter"), _VALVE_PORTS, _isolation_valve),
    ("check_valve", 1): _Template(("length", "body_diameter"), _VALVE_PORTS, _check_valve),
    ("indicator", 1): _Template(("diameter",), (), _indicator),
    ("boundary", 1): _Template(("size",), (("inlet", "in", (-18, 0)),), _boundary),
}


# --- Lookups and value checks ------------------------------------------------


def _template(kind, version) -> _Template:
    if isinstance(kind, str) and type(version) is int:
        template = _TEMPLATES.get((kind, version))
        if template is not None:
            return template
    raise KeyError(f"no equipment template {kind!r} version {version!r}")


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, not {value!r}")
    return float(value)


def _triple(values, label: str) -> tuple[float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"{label} must contain three numbers, not {values!r}")
    return tuple(_number(v, label) for v in values)


def _dimensions(entity: dict, template: _Template) -> dict[str, float]:
    parameters = entity["parameters"]
    values = {}
    for name in template.parameters:
        if name not in parameters:
            raise KeyError(f"entity {entity.get('id')!r} has no parameter {name!r}")
        quantity = parameters[name]
        if quantity.get("unit") != "m":
            raise ValueError(f"parameter {name!r} must be in metres")
        value = _number(quantity.get("value"), f"parameter {name!r}")
        if value <= 0:
            raise ValueError(f"parameter {name!r} must be positive, not {value!r}")
        values[name] = value
    return values


def _resolve(entity: dict):
    template = _template(entity["kind"], entity["template_version"])
    bodies, frames = template.geometry(_dimensions(entity, template))
    return template, bodies, frames


def _port_and_entity(project: dict, port_id: str) -> tuple[dict, dict]:
    model = project["model"]
    port = model["ports"].get(port_id)
    if port is None:
        raise KeyError(f"unknown port {port_id!r}")
    entity = model["entities"].get(port["entity_id"])
    if entity is None:
        raise KeyError(f"port {port_id!r} references unknown entity {port['entity_id']!r}")
    return port, entity


def _cos_sin(degrees: float) -> tuple[float, float]:
    quarters, remainder = divmod(degrees, 90)
    if remainder == 0:  # exact values keep axis-aligned layouts free of noise
        return _QUARTER_TURNS[int(quarters) % 4]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def _rotate(rotation_deg, vector) -> tuple[float, float, float]:
    x, y, z = vector
    c, s = _cos_sin(rotation_deg[0])
    y, z = y * c - z * s, y * s + z * c
    c, s = _cos_sin(rotation_deg[1])
    x, z = x * c + z * s, -x * s + z * c
    c, s = _cos_sin(rotation_deg[2])
    x, y = x * c - y * s, x * s + y * c
    return x, y, z


# --- Public interface --------------------------------------------------------


def get_template(kind: str, version: int) -> dict:
    """Return a fresh template record for ``kind`` at ``version``."""
    template = _template(kind, version)
    return {
        "kind": kind,
        "version": version,
        "parameters": {name: {"unit": "m", "min_exclusive": 0} for name in template.parameters},
        "ports": {
            name: {
                "direction": direction,
                "required": True,
                "nominal_diameter_mm": 50,
                "service": "liquid",
            }
            for name, direction, _ in template.ports
        },
        "diagram_anchors": {name: list(anchor) for name, _, anchor in template.ports},
    }


def entity_template(entity: dict) -> dict:
    """Return the template record of any entity, built-in or composed.

    Built-in kinds return ``get_template(kind, template_version)`` unchanged
    (``KeyError`` when unknown). ``composed_equipment`` entities return the
    record derived from their validated definition. Raises
    ``DefinitionError`` for a missing, invalid or unsupported definition, or
    for a definition carried by a built-in kind.
    """
    if not isinstance(entity, dict):
        raise DefinitionError("entity must be an object")
    if is_composed(entity):
        return composed_template(entity)
    if "definition" in entity:
        raise DefinitionError(f"only composed_equipment entities carry a definition, not kind {entity.get('kind')!r}")
    return get_template(entity.get("kind"), entity.get("template_version"))


def entity_primitives(entity: dict) -> list[dict]:
    """Return the entity's primitives in its local frame, bodies first."""
    if is_composed(entity):
        raise DefinitionError(f"composed equipment {entity.get('id')!r} has no template-version-1 primitive "
                              f"representation; its geometry is built from its definition by skid.geometry")
    template, bodies, frames = _resolve(entity)
    nozzles = []
    for name, _, _ in template.ports:
        point, axis, length = frames[name]
        vector = _AXES[axis][0]
        center = [c - v * length / 2 for c, v in zip(point, vector)]
        nozzles.append(_cylinder(f"nozzle/{name}", center, axis, NOZZLE_RADIUS_M, length))
    return bodies + nozzles


def local_port_position(project: dict, port_id: str) -> tuple[float, float, float]:
    """Return a port's position in its entity's local frame."""
    port, entity = _port_and_entity(project, port_id)
    if is_composed(entity):
        ports = resolve_definition(entity)["ports"]
        if port["name"] not in ports:
            raise KeyError(f"definition of {entity.get('id')!r} has no port {port['name']!r}")
        return tuple(_point(c / 1000.0 for c in ports[port["name"]]["position_mm"]))
    _, _, frames = _resolve(entity)
    if port["name"] not in frames:
        raise KeyError(f"template {entity['kind']!r} has no port {port['name']!r}")
    return tuple(_point(frames[port["name"]][0]))


def world_port_position(project: dict, port_id: str) -> tuple[float, float, float] | None:
    """Return a port's world position, or None when its entity is not placed."""
    port, _ = _port_and_entity(project, port_id)
    placement = project["physical"]["placements"].get(port["entity_id"])
    if placement is None or placement.get("status") == "unresolved":
        return None
    if placement.get("status") != "placed":
        raise ValueError(f"unknown placement status {placement.get('status')!r}")
    position = _triple(placement.get("position_m"), "position_m")
    rotation = _triple(placement.get("rotation_deg"), "rotation_deg")
    offset = _rotate(rotation, local_port_position(project, port_id))
    return tuple(_clean(p + o) for p, o in zip(position, offset))


def diagram_port_position(project: dict, port_id: str, symbol_id: str) -> tuple[float, float]:
    """Return a port's P&ID anchor on the given symbol occurrence."""
    port, entity = _port_and_entity(project, port_id)
    if is_composed(entity):
        anchors = composed_template(entity)["diagram_anchors"]
    else:
        template = _template(entity["kind"], entity["template_version"])
        anchors = {name: anchor for name, _, anchor in template.ports}
    if port["name"] not in anchors:
        raise KeyError(f"template {entity['kind']!r} has no port {port['name']!r}")
    symbol = project["pid"]["symbols"].get(symbol_id)
    if symbol is None:
        raise KeyError(f"unknown symbol {symbol_id!r}")
    if symbol.get("entity_id") != port["entity_id"]:
        raise ValueError(f"symbol {symbol_id!r} does not represent the entity of port {port_id!r}")
    x, y, angle = (_number(symbol.get(k), k) for k in ("x", "y", "rotation_deg"))
    ax, ay = anchors[port["name"]]
    c, s = _cos_sin(angle)
    return _clean(x + ax * c - ay * s), _clean(y + ax * s + ay * c)
