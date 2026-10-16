"""The authoritative, JSON-serializable planning context for one project revision.

``build_context(document, text)`` describes the current revision for the
planner: identity and digest, stable IDs with current tags, topology with
port world positions and diagram anchors, parameters with provenance and
declared bounds, placements, physical routes and diagram edges (manual or
guided, current or not), assumptions, open validation items, the supported
operation vocabulary with its derived-ID rules and limits, and the
composed-equipment vocabulary (definition schema, symbol shapes, recipe part
calls and materials, example definitions). The request text is embedded as a
JSON string, so it is data, never prompt structure.

The ``creation`` section is present even for an empty model: every built-in
kind a plan may add (parameters, named ports with direction, DN, service,
derived ID suffix, P&ID anchor, outward direction and local position
formula, body extent and labelled reference values; ``vocabulary``), the
line conventions and the composed examples' port frames. With research, the
verified brief appears as ``research``, marked untrusted (``evidence``).

Nothing here imports CadQuery. The part signatures, material names, symbol
shapes and limits come from the pure ``skid.equipment`` validator, the same
vocabulary that later accepts or rejects a definition.
"""

from __future__ import annotations

import os
import re

from .. import clearance
from ..core import canonical_digest, validate_project
from ..templates import diagram_port_position, get_template, world_port_position
from . import evidence
from .schema import (MAX_OPERATIONS, NEW_CONNECTION_PATTERN, NEW_ENTITY_PATTERN, OPERATION_TYPES, ROUTE_MODES,
                     strict_json)
from .vocabulary import builtin_vocabulary, composed_example_frames

__all__ = ["CONTEXT_SCHEMA", "build_context", "equipment_api", "example_definitions", "template_for"]

CONTEXT_SCHEMA = "skid.planning.context/1"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLES_DIR = os.path.join(ROOT, "examples", "recipes")
EXAMPLE_FILE = re.compile(r"^([a-z0-9_]{1,48})\.definition\.json$")
MAX_EXAMPLE_BYTES = 65536
SYMBOL_SHAPES = ("horizontal_vessel", "vertical_vessel", "box_equipment")
COMPOSED_KIND = "composed_equipment"
STANDING_UNKNOWNS = (
    "The model records no process duty, flow rate, fluid properties, pressures, temperatures, materials of "
    "construction, pressure ratings, valve normal/fail positions or control philosophy.",
    "All dimensions are illustrative prototype values with recorded assumptions; none is a vendor, design or "
    "fabrication value.",
)


def equipment_api():
    """``(module, None)`` when ``skid.equipment`` provides the composed-equipment interface, else ``(None, reason)``."""
    try:
        from .. import equipment
    except ImportError as exc:
        return None, f"skid.equipment is not available ({type(exc).__name__}: {exc})"
    missing = [name for name in ("validate_definition", "entity_template", "DefinitionError")
               if not hasattr(equipment, name)]
    if missing:
        return None, f"skid.equipment lacks {', '.join(missing)}"
    return equipment, None


def template_for(entity):
    """The template contract of an entity (built-in or composed), or ``None`` when it cannot be resolved."""
    equipment, _ = equipment_api()
    try:
        if equipment is not None:
            return equipment.entity_template(entity)
        return get_template(entity.get("kind"), entity.get("template_version"))
    except Exception:  # noqa: BLE001 - any lookup fault means "unknown" here; validators report it precisely
        return None


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def vocabulary(equipment) -> dict:
    """The pure validator's own vocabulary (parts, materials, symbol shapes, limits); empty when unavailable."""
    if equipment is None:
        return {"part_vocabulary": {}, "materials": [], "symbol_shapes": list(SYMBOL_SHAPES), "limits": {}}
    parts = {}
    for name, signature in sorted(getattr(equipment, "PART_SIGNATURES", {}).items()):
        required, optional = signature if isinstance(signature, tuple) and len(signature) == 2 else (signature, ())
        parts[name] = {"required": list(required), "optional": list(optional)}
    return {"part_vocabulary": parts, "materials": sorted(getattr(equipment, "MATERIAL_NAMES", ())),
            "symbol_shapes": list(getattr(equipment, "SYMBOL_SHAPES", SYMBOL_SHAPES)),
            "limits": _jsonable(getattr(equipment, "LIMITS", {}))}


def example_definitions() -> dict:
    """``{name: definition}`` for valid ``examples/recipes/<name>.definition.json`` files (pure validation)."""
    equipment, _ = equipment_api()
    found = {}
    try:
        names = sorted(os.listdir(EXAMPLES_DIR))
    except OSError:
        return found
    for name in names:
        match = EXAMPLE_FILE.match(name)
        path = os.path.join(EXAMPLES_DIR, name)
        if not match or os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > MAX_EXAMPLE_BYTES:
                continue
            with open(path, encoding="utf-8") as handle:
                definition = strict_json(handle.read())
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if equipment is not None:
            try:
                problems = [d for d in equipment.validate_definition(definition) if d.get("severity") != "warning"]
            except Exception:  # noqa: BLE001 - an example that cannot be checked is not offered
                continue
            if problems:
                continue
        found[match.group(1)] = definition
    return found


def _safe(function, *args):
    try:
        value = function(*args)
    except Exception:  # noqa: BLE001 - unresolved geometry is reported as null, never guessed
        return None
    return None if value is None else [round(float(v), 9) + 0.0 for v in value]


def _provenance(quantity):
    if "assumption_id" in quantity:
        return {"assumption_id": quantity["assumption_id"]}
    return {"source": quantity.get("source")}


def _entities(project):
    model, pid, physical = project["model"], project["pid"], project["physical"]
    connected = {}
    for connection_id, connection in model["connections"].items():
        connected[connection["from_port_id"]] = connection_id
        connected[connection["to_port_id"]] = connection_id
    rows = []
    for entity_id, entity in sorted(model["entities"].items()):
        template = template_for(entity)
        symbols = sorted((s for s in pid["symbols"].values() if s["entity_id"] == entity_id), key=lambda s: s["id"])
        ports = []
        for port_id, port in sorted(model["ports"].items()):
            if port["entity_id"] != entity_id:
                continue
            row = {
                "id": port_id, "name": port["name"], "direction": port["direction"], "required": port["required"],
                "nominal_diameter_mm": port["nominal_diameter_mm"], "service": port["service"],
                "assumption_id": port["assumption_id"], "connection_id": connected.get(port_id),
                "world_position_m": _safe(world_port_position, project, port_id),
                "world_outward_direction": _safe(clearance.port_direction, project, port_id),
                "diagram_anchors": {s["id"]: _safe(diagram_port_position, project, port_id, s["id"]) for s in symbols},
            }
            if "connection_status" in port:
                row["connection_status"] = port["connection_status"]
            ports.append(row)
        row = {
            "id": entity_id, "tag": entity["tag"], "kind": entity["kind"], "template_version": entity["template_version"],
            "parameters": {name: {"value": q["value"], "unit": q["unit"], **_provenance(q)}
                           for name, q in sorted(entity["parameters"].items())},
            "parameter_declarations": template.get("parameters") if template else None,
            "ports": ports,
            "placement": physical["placements"].get(entity_id),
            "symbols": [{key: s[key] for key in ("id", "x", "y", "rotation_deg", "manual")} for s in symbols],
        }
        if "observes" in entity:
            row["observes"] = entity["observes"]
        if entity.get("kind") == COMPOSED_KIND and "definition" in entity:
            row["definition"] = entity["definition"]
        rows.append(row)
    return rows


def _connections(project):
    model, pid, physical = project["model"], project["pid"], project["physical"]
    ports, entities = model["ports"], model["entities"]

    def end(port_id):
        port = ports.get(port_id, {})
        entity = entities.get(port.get("entity_id"), {})
        return {"port_id": port_id, "entity_id": port.get("entity_id"), "entity_tag": entity.get("tag"),
                "port_name": port.get("name")}

    rows = []
    for connection_id, connection in sorted(model["connections"].items()):
        edges = [{key: edge[key] for key in ("id", "from_symbol_id", "to_symbol_id", "points", "manual", "status")
                  if key in edge} | ({"reason": edge["reason"]} if "reason" in edge else {})
                 for edge in sorted(pid["edges"].values(), key=lambda e: e["id"])
                 if edge["connection_id"] == connection_id]
        rows.append({
            "id": connection_id, "tag": connection["tag"], "from": end(connection["from_port_id"]),
            "to": end(connection["to_port_id"]), "nominal_diameter_mm": connection["nominal_diameter_mm"],
            "outside_diameter_m": connection["outside_diameter_m"], "assumption_id": connection["assumption_id"],
            "route": physical["routes"].get(connection_id), "diagram_edges": edges,
        })
    return rows


def _capabilities():
    return {
        "operations": {
            "rename_entity": "Change an entity's tag (identity, ports, connections and layout unchanged). Line "
                             "(connection) tags cannot be renamed.",
            "set_parameter": "Set one declared parameter of an entity (built-in or composed). value with unit m or mm "
                             "(stored in m); must reference an assumption. Incident current routes whose port moves "
                             "become needs_reroute.",
            "move_equipment": "Set a placed physical position (m) and rotation (deg, XYZ fixed axes) for an entity; "
                              "manual layout choice; incident routes become needs_reroute. Must reference an "
                              "assumption.",
            "move_symbol": "Move one P&ID symbol occurrence (stored diagram units, +y down) and set its rotation; "
                           "guided edges regenerate, manual edges are kept and flagged if their ends moved.",
            "set_route": "Set an explicit physical route (manual mode) for a connection: orthogonal polyline from "
                         "the from-port world position to the to-port world position (1e-6 m tolerance). Must "
                         "reference an assumption. Only when both ends have resolved world positions.",
            "set_diagram_route": "Set an explicit manual P&ID edge path from the from-anchor to the to-anchor.",
            "insert_valve": "Insert an isolation_valve or check_valve inline into an existing connection. The "
                            "existing connection keeps its ID and now ends at the valve inlet; a new downstream "
                            "connection runs from the valve outlet to the original destination.",
            "insert_equipment": "Insert a composed two-port inline item (ports exactly inlet/outlet, DN50 liquid) "
                                "from a self-contained definition, the same way as insert_valve. No new equipment "
                                "type registration is needed; the definition is carried in the entity.",
            "add_equipment": "Add one new, unconnected item: a built-in kind from context.creation.builtin.kinds "
                             "(template_version 1) or composed_equipment from a definition (definition_ref or "
                             "definition_json), with every declared port. Supply every declared parameter (unit m "
                             "or mm, cited), port_assumption (provenance of its port records: DN, service, "
                             "names), a P&ID symbol position and a placement (placed with three numbers each and an "
                             "assumption, or unresolved). Its ports are recorded as deliberately open "
                             "(connection_status unresolved) until a connect_ports joins them: an open required "
                             "port is a visible warning and keeps the outputs pending, never a complete state.",
            "connect_ports": "Join one out port to one in port of two different items as a new line (connection) "
                             "with its own stable ID and an editable line tag. Name both ports and both P&ID "
                             "symbol occurrences by ID (records created earlier in the same plan included); a port "
                             "joins at most one line, DN and service must match, flow runs from_port -> to_port. "
                             "outside_diameter_mm and the line's assumption are recorded on the connection. The "
                             "P&ID edge is drawn on the guided orthogonal path between the two symbol anchors. "
                             "route.mode: 'guided' asks the deterministic guided router (axis-parallel DN50 path "
                             "leaving and entering along the nozzle axes, screened against placed equipment "
                             "bodies) to compute the physical route on the checked batch; when both ends are not "
                             "placed, a later operation moves an end, or no candidate fits, the route stays "
                             "explicitly unresolved with the reason (never invented points). 'points' supplies an "
                             "explicit orthogonal route (points_m from the from-port world position to the to-port "
                             "world position, cited by route.assumption). 'none' leaves it unresolved.",
        },
        "derived_ids": {
            "rule": "For a new entity ID ent-<stem> (pattern " + NEW_ENTITY_PATTERN + "), the planner service "
                    "derives port-<stem>-<port name>, conn-<stem>-down (downstream connection), sym-<stem> (P&ID "
                    "symbol) and edge-<stem>-down (downstream diagram edge). Later operations in the same plan may "
                    "reference these IDs. None of them may already exist.",
            "add_equipment": "add_equipment with entity_id ent-<stem> derives sym-<stem> and one port "
                             "port-<stem>-<port_id_suffix> per declared port (port_id_suffix is the port name with "
                             "'_' replaced by '-', listed per port in context.creation).",
            "connect_ports": "connect_ports with connection_id conn-<stem> (pattern " + NEW_CONNECTION_PATTERN +
                             ") derives the P&ID edge edge-<stem>. Neither may already exist.",
            "inserted_records": "Ports copy the declared direction, required flag, DN and service; port provenance "
                                "copies the assumption of the port the item is inserted in front of. The downstream "
                                "connection copies DN, outside diameter and assumption of the split connection. The "
                                "downstream diagram edge gets the core's guided orthogonal path from the new "
                                "symbol's outlet anchor to the original destination anchor, marked current and "
                                "re-validated by the core; if an anchor cannot be resolved it stays unresolved "
                                "(pending) with no points, and if the core cannot validate the path both split edges "
                                "become unresolved. The split upstream edge is regenerated if guided, or kept and "
                                "flagged for repair if manual. The split physical routes become unresolved unless "
                                "later set_route operations supply both.",
        },
        "assumptions": "Every new value, placement or route references either an existing assumption ID or the "
                       "key of an assumption declared in the plan. Declared assumptions are recorded in the model "
                       "with source 'engineering plan <proposal id>' only when an operation references them.",
        "limits": {"operations_per_plan": MAX_OPERATIONS, "operation_types": list(OPERATION_TYPES),
                   "plan_output": "Every count, length, pattern and range is listed under 'Output limits' in the "
                                  "instructions, generated from the checker's schema; an output over any limit is "
                                  "rejected whole."},
        "atomicity": "All operations become one batch command: either every operation applies as one new revision "
                     "and one undo step, or nothing changes.",
        "unsupported": [
            "deleting, duplicating or replacing equipment", "branches, tees or junctions (a port joins at most one "
            "line)", "joining ports by proximity (only by named port IDs)", "changing nominal diameters (only DN50 "
            "geometry exists)", "renaming lines",
            "new instruments, control loops, alarms, interlocks or safety functions (including adding indicator "
            "items)", "setting process data (duty, flow, pressure, temperature, fluid, material, ratings)",
            "automatic whole-plant layout or routing (guided routing is per line and bounded)",
            "vendor selection, sizing, thermal/mechanical design or standards-conformance claims",
        ],
        "scope": "A prototype arrangement tool: it records equipment, named ports, lines, illustrative dimensions, "
                 "placements and routes with explicit assumptions and keeps the P&ID and 3D/CAD consistent. It is "
                 "not a process, mechanical, safety or fabrication design tool.",
    }


def _composition(examples, equipment, equipment_reason):
    words = vocabulary(equipment)
    return {
        "available": equipment_reason is None,
        "unavailable_reason": equipment_reason,
        "definition_schema": "skid.equipment.definition/1",
        "definition_shape": {
            "schema": "skid.equipment.definition/1", "id": "lowercase-hyphenated id", "name": "short name",
            "function": "declared function; state unknown duty explicitly",
            "geometry": "an authored skid.geometry.recipe/1 object (see examples): schema, id, title, description, "
                        "length_unit 'mm', frame, parameters {name: {value, unit 'm', minimum, maximum, "
                        "assumption_id}}, derived [[name, '=expr'], ...], constraints [{at_least_zero: '=expr', "
                        "message}], ports {name: {position: [x,y,z], direction: [dx,dy,dz], nominal_diameter_mm: 50, "
                        "component, assumption_id}}, components [{key, material, display_only, assumptions, "
                        "parts}], assumptions {id: {category, description}}",
            "ports": "{inlet: {direction: 'in', required: true, service: 'liquid'}, outlet: {direction: 'out', "
                     "required: true, service: 'liquid'}}",
            "symbol": "{shape: one of symbol_shapes, anchors: {inlet: [x, y], outlet: [x, y]}} in diagram units "
                      "relative to the symbol origin",
            "unknowns": ["explicit statements of what is not known"],
        },
        "rules": [
            "Semantic port names, geometry recipe port names and symbol anchor names must match exactly.",
            "Expressions are strings starting with '='; lengths inside expressions are millimetres; parameters are "
            "declared in metres and appear in expressions in millimetres. Use only + - * / ** %, numbers, declared "
            "names, pi and the functions min max abs sqrt sin cos tan atan2 (degrees).",
            "Ports lie on the outward flange face of their nozzle component (flanged_nozzle 'port' equals the "
            "recipe port position, same direction).",
            "Entity parameters are the authoritative current values: supply every declared parameter on insertion "
            "with unit m or mm and an assumption reference.",
            "No arbitrary code, file paths, imports or raw SVG. Bounded size and nesting; definitions are validated "
            "before any batch is formed, and CAD/OCCT success is only known after a build runs.",
            "Use definition_ref 'example:<name>' to reuse an example below unchanged, 'entity:<entity id>' to copy "
            "an existing composed entity's definition, or definition_json with a complete definition. Do not set "
            "both.",
            "insert_equipment needs exactly the ports inlet/outlet. add_equipment accepts any validated definition "
            "with its full declared port set (port names lowercase with underscores, every port declared in "
            "ports, geometry.ports and symbol.anchors); each port becomes its own stable port record.",
            "Multi-port items whose ports form separate circuits (for example the two sides of a heat exchanger) "
            "declare them in the definition's optional flow_paths as disjoint [inlet name, outlet name] pairs when "
            "the definition schema supports it; flow is never assumed to mix between circuits of one item.",
        ],
        "example_port_frames": {name: composed_example_frames(definition) for name, definition in examples.items()},
        "symbol_shapes": words["symbol_shapes"],
        "part_vocabulary": words["part_vocabulary"],
        "materials": words["materials"],
        "definition_limits": words["limits"],
        "examples": examples,
    }


def _creation():
    return {
        "builtin": builtin_vocabulary(),
        "line_conventions": {
            "nominal_diameter_mm": 50,
            "outside_diameter_mm": 60.3,
            "basis": "Only DN50 nozzle and pipe geometry exists. 60.3 mm is the standard outside diameter of DN50 "
                     "(NPS 2) pipe; record it in the line's assumption. Wall thickness, material and rating are "
                     "unknown.",
        },
        "route_modes": list(ROUTE_MODES),
        "arrangement_notes": [
            "Positions are the item origins described per kind; compute nozzle world positions from the placement "
            "and the local port formulas before choosing positions.",
            "Guided routing needs room: each nozzle gets a straight stub of at least 0.16 m along its outward "
            "direction and DN50 bends take about 75 mm, and a route may not pass through any placed body "
            "(local_body_extent_m gives each kind's extent at its reference values).",
            "An item placed with unresolved placement keeps its lines' routes unresolved; that is a visible pending "
            "state, not an error.",
            "Lay P&ID symbols out on the sheet (see conventions.pid) with clear gaps between symbols so labels and "
            "lines stay readable.",
        ],
    }


def build_context(document, text, examples=None, research=None) -> dict:
    """The planning context; ``research`` is a verified research record (``evidence.verify_reply``) or ``None``."""
    project = document["current"]
    equipment, reason = equipment_api()
    examples = example_definitions() if examples is None else examples
    open_items = [{key: d[key] for key in ("code", "entity_ids", "message")}
                  for d in validate_project(project) if d.get("severity") == "warning"]
    return {
        "schema": CONTEXT_SCHEMA,
        "project": {"id": project["id"], "revision": project["revision"], "input_digest": canonical_digest(project),
                    "history": {"undo": len(document["undo"]), "redo": len(document["redo"])}},
        "conventions": {
            "physical": "Metres, right-handed, +Z up. Placement rotation_deg is XYZ about fixed axes (R = Rz Ry Rx). "
                        "Routes are orthogonal polylines between port world positions.",
            "pid": "Stored diagram units, +y down. The sheet is 1200 x 840 with its border at x 30..1170, "
                   "y 30..810 in paper units; paper = stored + 100 on both axes, so stored x in -70..1070 and "
                   "y in -70..710 stay on the sheet. Positive symbol rotation turns clockwise on screen.",
            "identity": "Stable IDs never change; tags are editable labels. Refer to existing records only by the "
                        "IDs given here.",
        },
        "entities": _entities(project),
        "connections": _connections(project),
        "assumptions": {aid: dict(record) for aid, record in sorted(project["model"]["assumptions"].items())},
        "open_items": open_items,
        "unknowns": list(STANDING_UNKNOWNS),
        "capabilities": _capabilities(),
        "creation": _creation(),
        "composition": _composition(examples if equipment is not None else {}, equipment, reason),
        "research": evidence.planner_view(research) if research is not None else None,
        "request": {"text": text},
    }
