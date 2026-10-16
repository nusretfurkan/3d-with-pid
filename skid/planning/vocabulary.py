"""Equipment-creation vocabulary for the planner context, derived from the template code itself.

``builtin_vocabulary()`` describes every built-in kind an engineering plan may
add with ``add_equipment``: declared parameters and their bounds, every named
port (direction, required flag, DN, service, the derived port-ID suffix, the
P&ID anchor relative to the symbol origin, the outward nozzle direction in the
local frame and the local port position as a formula of the parameters), and
the local body extent. Nothing here is a separately maintained copy of the
templates: positions and directions are evaluated through
``skid.templates.local_port_position``/``entity_primitives`` and
``skid.clearance`` on a synthetic one-entity project, the formulas are fitted
by finite differences and accepted only when a third evaluation reproduces
them exactly (otherwise only the evaluated values are given).

``composed_example_frames(definition)`` gives the same port and extent facts
for a composed example definition at its own authored defaults.

Reference values are read from the repository sample fixture
(``examples/pump_skid.json``) and labelled as illustrative prototype values;
the planner must still choose and justify its own values with assumptions.
Instruments (``indicator``) are not offered: new instruments, control loops and
safety functions remain outside the supported operation scope.

Pure standard library plus ``skid.templates``/``skid.equipment``/
``skid.clearance``; no CAD import, no I/O beyond reading the bounded fixture.
"""

from __future__ import annotations

import copy
import os

from .. import clearance
from ..templates import entity_primitives, get_template, local_port_position
from .schema import ADDABLE_KINDS, strict_json

__all__ = ["ADDABLE_KINDS", "EXCLUDED_KINDS", "builtin_vocabulary", "composed_example_frames", "port_id_suffix",
           "reference_values"]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REFERENCE_FIXTURE = os.path.join(ROOT, "examples", "pump_skid.json")
REFERENCE_LABEL = ("illustrative prototype values of the repository sample fixture examples/pump_skid.json; not "
                   "design, vendor or duty values")
MAX_FIXTURE_BYTES = 1 << 20
TEMPLATE_VERSION = 1
EXCLUDED_KINDS = {"indicator": "an instrument observing another entity; new instruments, control loops, alarms and "
                               "safety functions are outside the supported scope"}
DIGITS = 9
_STEP = 0.125  # finite-difference step (metres); exact in binary, so linear templates fit without noise


def port_id_suffix(name: str) -> str:
    """The port-ID suffix of a port name: underscores become hyphens (port names never contain hyphens)."""
    return name.replace("_", "-")


def _round(value):
    return round(float(value), DIGITS) + 0.0


def reference_values() -> dict:
    """``{kind: {parameter: value_m}}`` from the first fixture entity of each built-in kind; ``{}`` if unreadable."""
    try:
        if os.path.getsize(REFERENCE_FIXTURE) > MAX_FIXTURE_BYTES:
            return {}
        with open(REFERENCE_FIXTURE, encoding="utf-8") as handle:
            document = strict_json(handle.read())
        entities = document["current"]["model"]["entities"]
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return {}
    found = {}
    for _, entity in sorted(entities.items()):
        kind = entity.get("kind") if isinstance(entity, dict) else None
        if kind in ADDABLE_KINDS and kind not in found:
            values = {}
            for name, quantity in sorted((entity.get("parameters") or {}).items()):
                value = quantity.get("value") if isinstance(quantity, dict) else None
                if quantity.get("unit") == "m" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[name] = float(value)
            found[kind] = values
    return found


def _probe_project(entity, ports):
    """A synthetic one-entity project placed at the origin with no rotation (lookups only)."""
    return {"id": "vocabulary-probe", "revision": 0,
            "model": {"entities": {entity["id"]: entity}, "ports": {p["id"]: p for p in ports}, "connections": {},
                      "assumptions": {}},
            "pid": {"symbols": {}, "edges": {}},
            "physical": {"placements": {entity["id"]: {"position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0],
                                                        "status": "placed", "manual": False,
                                                        "assumption_id": "vocabulary-probe"}},
                         "routes": {}}}


def _entity(kind, values, version=TEMPLATE_VERSION, definition=None):
    entity = {"id": "ent-vocabulary-probe", "kind": kind, "tag": "PROBE", "template_version": version,
              "parameters": {name: {"value": value, "unit": "m", "assumption_id": "vocabulary-probe"}
                             for name, value in values.items()}}
    if definition is not None:
        entity["definition"] = definition
    return entity


def _ports(entity, template):
    return [{"id": f"port-probe-{name}", "entity_id": entity["id"], "name": name, "direction": spec["direction"],
             "required": spec.get("required", True), "nominal_diameter_mm": spec.get("nominal_diameter_mm"),
             "service": spec.get("service")} for name, spec in sorted(template["ports"].items())]


def _positions(kind, template, values):
    entity = _entity(kind, values)
    ports = _ports(entity, template)
    project = _probe_project(entity, ports)
    return {port["name"]: [float(v) for v in local_port_position(project, port["id"])] for port in ports}


def _directions_and_extent(entity, template):
    ports = _ports(entity, template)
    project = _probe_project(entity, ports)
    directions = {}
    for port in ports:
        direction = clearance.port_direction(project, port["id"])
        directions[port["name"]] = None if direction is None else [_round(v) for v in direction]
    try:
        boxes = clearance.body_boxes(project)
    except clearance.ClearanceError:
        boxes = []
    extent = None
    if boxes:
        low = [min(b["box"][0][axis] for b in boxes) for axis in range(3)]
        high = [max(b["box"][1][axis] for b in boxes) for axis in range(3)]
        extent = {"min": [_round(v) for v in low], "max": [_round(v) for v in high]}
    return directions, extent


def _formula(constant, coefficients):
    terms = []
    for name, coefficient in coefficients:
        if abs(coefficient) < 1e-12:
            continue
        sign = "-" if coefficient < 0 else "+"
        magnitude = abs(coefficient)
        text = name if abs(magnitude - 1.0) < 1e-12 else f"{magnitude:g}*{name}"
        terms.append((sign, text))
    if abs(constant) >= 1e-12 or not terms:
        terms.insert(0, ("-" if constant < 0 else "+", f"{abs(constant):g}"))
    first_sign, first = terms[0]
    out = ("-" if first_sign == "-" else "") + first
    for sign, text in terms[1:]:
        out += f" {sign} {text}"
    return out


def _fit(kind, template, base):
    """``{port: [x, y, z] formula strings}`` when the local port positions are exactly linear, else ``None``."""
    names = sorted(template["parameters"])
    at_base = _positions(kind, template, base)
    slopes = {}
    for name in names:
        moved = dict(base, **{name: base[name] + _STEP})
        positions = _positions(kind, template, moved)
        slopes[name] = {port: [(positions[port][axis] - at_base[port][axis]) / _STEP for axis in range(3)]
                        for port in at_base}
    constants = {port: [at_base[port][axis] - sum(slopes[n][port][axis] * base[n] for n in names)
                        for axis in range(3)] for port in at_base}
    check = {name: base[name] * 1.375 + 0.0625 for name in names}  # a third, independent evaluation
    actual = _positions(kind, template, check)
    for port in at_base:
        for axis in range(3):
            predicted = constants[port][axis] + sum(slopes[n][port][axis] * check[n] for n in names)
            if abs(predicted - actual[port][axis]) > 1e-9:
                return None
    return {port: [_formula(_round(constants[port][axis]), [(n, _round(slopes[n][port][axis])) for n in names])
                   for axis in range(3)] for port in at_base}


def _builtin(kind, reference):
    template = get_template(kind, TEMPLATE_VERSION)
    names = sorted(template["parameters"])
    base = {name: float(reference.get(name, 1.0)) for name in names}
    formulas = _fit(kind, template, base)
    at_reference = _positions(kind, template, base)
    directions, extent = _directions_and_extent(_entity(kind, base), template)
    ports = []
    for name, spec in sorted(template["ports"].items()):
        ports.append({
            "name": name, "direction": spec["direction"], "required": spec.get("required", True),
            "nominal_diameter_mm": spec.get("nominal_diameter_mm"), "service": spec.get("service"),
            "port_id_suffix": port_id_suffix(name),
            "diagram_anchor": [float(v) for v in template["diagram_anchors"][name]],
            "local_outward_direction": directions.get(name),
            "local_position_m": formulas[name] if formulas else None,
            "local_position_m_at_reference": [_round(v) for v in at_reference[name]],
        })
    return {
        "kind": kind, "template_version": TEMPLATE_VERSION,
        "parameters": {name: dict(template["parameters"][name]) for name in names},
        "reference_values_m": {name: base[name] for name in names} if reference else None,
        "ports": ports,
        "local_body_extent_m_at_reference": extent,
    }


def builtin_vocabulary() -> dict:
    """The addable built-in kinds (see module docstring); a kind whose template cannot be resolved is omitted."""
    references = reference_values()
    kinds = {}
    for kind in ADDABLE_KINDS:
        try:
            kinds[kind] = _builtin(kind, references.get(kind) or {})
        except Exception:  # noqa: BLE001 - an unresolvable template is simply not offered
            continue
    return {
        "kinds": kinds,
        "excluded_kinds": dict(EXCLUDED_KINDS),
        "reference_values_basis": REFERENCE_LABEL,
        "frames": ("Local frame: metres, right-handed, +Z up; the placement maps it into the world (position_m, then "
                   "rotation_deg about fixed X, Y, Z axes, R = Rz Ry Rx). Port world position = placement position + "
                   "R x local port position. A port's nozzle points along its outward direction; a pipe leaves an "
                   "outlet and enters an inlet along that axis."),
        "diagram": ("diagram_anchor is the port's P&ID anchor relative to the symbol origin before the symbol's "
                    "rotation (stored units, +y down, positive rotation clockwise)."),
    }


def composed_example_frames(definition) -> dict | None:
    """Local port frames and extent of a composed definition at its own authored defaults, or ``None``."""
    try:
        from .. import equipment
    except ImportError:
        return None
    try:
        recipe = definition["geometry"]
        values = {name: float(record["value"]) for name, record in recipe["parameters"].items()}
        entity = _entity(equipment.COMPOSED_KIND, values, version=equipment.COMPOSED_VERSION,
                         definition=copy.deepcopy(definition))
        template = equipment.entity_template(entity)
        resolved = equipment.resolve_definition(entity)
        directions, extent = _directions_and_extent(entity, template)
    except Exception:  # noqa: BLE001 - an example that cannot be resolved has no frames to show
        return None
    ports = []
    for name, spec in sorted(template["ports"].items()):
        frame = resolved["ports"].get(name) or {}
        position = frame.get("position_mm")
        ports.append({
            "name": name, "direction": spec["direction"], "required": spec.get("required", True),
            "nominal_diameter_mm": spec.get("nominal_diameter_mm"), "service": spec.get("service"),
            "port_id_suffix": port_id_suffix(name),
            "diagram_anchor": [float(v) for v in template["diagram_anchors"][name]],
            "local_outward_direction": directions.get(name),
            "local_position_m_at_defaults": None if position is None else [_round(v / 1000.0) for v in position],
        })
    return {"parameters_at_defaults_m": values, "ports": ports, "local_body_extent_m_at_defaults": extent}
