"""Validated, atomic command application for skid engineering documents.

``apply_command`` validates the input document, rejects stale revisions, applies
the command to a deep copy, validates the complete result (current project and
history), and only then returns the new document. Rejections raise
``CommandError``; neither the input document nor the command is ever mutated.

A ``batch`` command runs 1-12 ordinary operations, in order, through the same
handlers on that one working copy. Only the final result is validated and
returned; it advances one revision and adds one undo snapshot, a wholly
unchanged batch is one no-op, and any rejected operation rejects the batch
with diagnostics naming the operation index. A route or diagram edge that
was current before the batch, was invalidated by it, is otherwise unchanged
and fits the final frames again (for example a dimension changed and
restored) is restored, so such a batch can be a no-op. Returned warnings
describe the committed result: an operation's invalidation or
unresolved-placement warning that a later operation resolved is dropped. Dry
runs and commits share this path: callers simply do not persist a dry run's
result.

``move_route_segment`` moves one straight section of a current physical route
perpendicular to itself (``skid.route_edit``). Its payload is exactly
``{connection_id, segment_index, offset_m, assumption_id}``: the index counts
the route's maximal straight legs at ``expected_revision``, the offset has one
nonzero world-axis component of at most 10 m, and the assumption must be
added by the same command (or batch operation). Both port positions and
directions stay fixed; the new points are derived from the authoritative
stored route, become a ``manual`` current route, and are screened for bend
room and equipment-body clearance, or the command is refused unchanged.

``set_route`` and ``move_route_segment`` refuse routes whose conservative
envelope enters a placed equipment body (``skid.clearance``); a batch
re-screens those routes in its final result. Existing routes are not
refused: validation reports them as ``route_body_intrusion`` warnings.

``add_equipment`` (``{entity, ports, symbol, placement?}``) adds one entity of
any supported built-in template or with a validated composed definition,
exactly its declared ports and one P&ID symbol; ``connect_ports``
(``{connection, edge, route?}``) joins two existing unused ports by stable ID
with a new line and P&ID edge (an empty, non-manual current edge path is
derived as a guided path), and an optional route. ``add_equipment`` marks each
new required port ``connection_status: "unresolved"`` (an explicit open draft
port, a pending ``required_port_unconnected`` warning); ``connect_ports``
clears the marker on both ends and inline insertion refuses it, so no handler
leaves a connected port marked. Unmarked unconnected required ports remain
validation errors. An omitted placement or route is recorded
``unresolved``. A current ``connect_ports`` route is screened like
``set_route`` and re-screened in a batch's final result. Records created
earlier in a batch can be referenced by later operations. See
``docs/EMPTY_START.md``.

``rename_project`` sets the optional display ``name`` (trimmed; see
``validation.project_name_problem``) and never changes the project id. History
snapshots carry ``name`` only when the project had one, and undo/redo restore
its presence or absence exactly.
"""

from __future__ import annotations

import copy
import json
import math

from ..equipment import (
    COMPOSED_KIND,
    DefinitionError,
    entity_template,
    inline_ports,
    is_composed,
    resolve_definition,
    validate_definition,
)
from ..clearance import ClearanceError, route_report
from ..route_edit import RouteEditError, move_section
from ..templates import diagram_port_position, world_port_position
from .errors import CommandError
from .validation import project_name_problem, validate_document, validate_project

__all__ = ["apply_command", "COMMAND_TYPES", "MAX_BATCH_OPERATIONS"]

ROUTE_TOLERANCE_M = 1e-6
DIAGRAM_TOLERANCE = 1e-6
VALVE_KINDS = ("isolation_valve", "check_valve")
ROUTE_MODES = ("guided", "manual")
PATH_STATUSES = ("current", "needs_reroute", "unresolved")
MAX_BATCH_OPERATIONS = 12

_ENVELOPE_FIELDS = ("id", "expected_revision", "type", "payload")
_OPERATION_FIELDS = ("type", "payload")
_SNAPSHOT_FIELDS = ("model", "pid", "physical")
# Optional project fields carried in history: present or absent exactly as they were at that revision.
_OPTIONAL_SNAPSHOT_FIELDS = ("name",)
_PLACEMENT_FIELDS = ("position_m", "rotation_deg", "status", "manual", "assumption_id")
_ROUTE_FIELDS = ("points_m", "mode", "status", "assumption_id")
_EDGE_FIELDS = ("id", "connection_id", "from_symbol_id", "to_symbol_id", "points", "manual", "status")
_ENTITY_FIELDS = ("id", "kind", "tag", "template_version", "parameters")
_PORT_FIELDS = ("id", "entity_id", "name", "direction", "required", "nominal_diameter_mm", "service", "assumption_id")
_SYMBOL_FIELDS = ("id", "entity_id", "x", "y", "rotation_deg", "manual")
_CONNECTION_FIELDS = ("id", "tag", "from_port_id", "to_port_id", "nominal_diameter_mm", "outside_diameter_m",
                      "assumption_id")
OPEN_PORT = "unresolved"  # the explicit draft marker value of an intentionally open required port
_TEMPLATE_ERRORS = (LookupError, TypeError, ValueError)
_POSITION_ERRORS = (ArithmeticError, LookupError, TypeError, ValueError)


# Diagnostics and payload checks


def _diagnostic(code, message, ids=(), severity="error"):
    return {"code": code, "severity": severity, "entity_ids": list(ids), "message": message}


def _reject(code, message, ids=()):
    return CommandError([_diagnostic(code, message, ids)])


def _errors(diagnostics):
    return [d for d in diagnostics if d.get("severity") != "warning"]


def _warnings(diagnostics):
    return [d for d in diagnostics if d.get("severity") == "warning"]


def _is_id(value):
    return isinstance(value, str) and value != ""


def _is_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # integers beyond float range are not usable values
        return False


def _json_copy(value, what):
    """Detach caller data and require finite, JSON-compatible values."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise _reject("unsupported_payload", f"{what} is not finite JSON data: {exc}") from None


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _fields(value, required, optional=(), what="payload"):
    if not isinstance(value, dict):
        raise _reject("unsupported_payload", f"{what} must be an object")
    missing = [name for name in required if name not in value]
    if missing:
        raise _reject("unsupported_payload", f"{what} is missing {', '.join(missing)}")
    extra = sorted(set(value) - set(required) - set(optional))
    if extra:
        raise _reject("unsupported_payload", f"{what} has unsupported fields {', '.join(extra)}")
    return value


def _require_id(value, what):
    if not _is_id(value):
        raise _reject("unsupported_payload", f"{what} must be a nonempty string")
    return value


def _lookup(mapping, record_id, what):
    _require_id(record_id, f"{what} id")
    record = mapping.get(record_id)
    if record is None:
        raise _reject("unknown_reference", f"{what} {record_id!r} does not exist", [record_id])
    return record


def _number(value, what, ids=()):
    if not _is_number(value):
        raise _reject("invalid_value", f"{what} must be a finite number", ids)
    return value


def _points(value, dims, what, minimum=2):
    if not isinstance(value, list) or len(value) < minimum:
        raise _reject("invalid_route", f"{what} must be a list of at least {minimum} points")
    for point in value:
        if not isinstance(point, list) or len(point) != dims or not all(_is_number(v) for v in point):
            raise _reject("invalid_route", f"{what} must contain [{'x, y' if dims == 2 else 'x, y, z'}] finite numbers")
    return value


def _require_assumption(project, assumption_id, ids=()):
    _require_id(assumption_id, "assumption_id")
    if assumption_id not in project["model"]["assumptions"]:
        raise _reject("unknown_assumption", f"assumption {assumption_id!r} does not exist", [assumption_id, *ids])


def _tag_key(tag):
    return " ".join(tag.split()).casefold()


def _check_tag_free(project, tag, owner_id, also=()):
    """Tags are compared whitespace-normalized and case-insensitively."""
    if not isinstance(tag, str) or not tag.strip():
        raise _reject("invalid_value", "tag must be a nonempty string", [owner_id])
    key = _tag_key(tag)
    model = project["model"]
    for collection in ("entities", "connections"):
        for record_id, record in model[collection].items():
            other = record.get("tag")
            if record_id != owner_id and isinstance(other, str) and other.strip() and _tag_key(other) == key:
                raise _reject("duplicate_tag", f"tag {tag!r} is already used by {record_id}", [owner_id, record_id])
    for other_id, other in also:
        if _tag_key(other) == key:
            raise _reject("duplicate_tag", f"tag {tag!r} is already used by {other_id}", [owner_id, other_id])


def _all_ids(project):
    model, pid = project["model"], project["pid"]
    ids = set()
    for collection in (model["entities"], model["ports"], model["connections"], model["assumptions"],
                       pid["symbols"], pid["edges"]):
        ids.update(collection)
    return ids


def _template(entity):
    try:
        return entity_template(entity)
    except DefinitionError as exc:
        entity_id = str(entity.get("id", ""))
        if not is_composed(entity):
            raise _reject("invalid_definition", str(exc), [entity_id]) from None
        if "definition" not in entity:
            raise _reject("missing_definition", str(exc), [entity_id]) from None
        problems = validate_definition(entity["definition"])
        if problems:
            raise CommandError([_diagnostic(p["code"], f"definition of {entity_id}: {p['message']}", [entity_id])
                                for p in problems]) from None
        raise _reject("missing_template", str(exc), [entity_id]) from None  # an unsupported template_version
    except _TEMPLATE_ERRORS as exc:
        raise _reject("missing_template", f"no template for kind {entity.get('kind')!r} version "
                      f"{entity.get('template_version')!r}: {exc}", [entity.get("id", "")]) from None


def _placement(project, value, entity_id, require_placed):
    _fields(value, _PLACEMENT_FIELDS, ("reason",), what="placement")
    status = value["status"]
    if status == "placed":
        for key in ("position_m", "rotation_deg"):
            item = value[key]
            if not isinstance(item, list) or len(item) != 3 or not all(_is_number(v) for v in item):
                raise _reject("invalid_value", f"placement {key} must be three finite numbers", [entity_id])
    elif status == "unresolved" and not require_placed:
        if value["position_m"] is not None or value["rotation_deg"] is not None:
            raise _reject("invalid_value", "an unresolved placement has null position and rotation", [entity_id])
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise _reject("invalid_value", "an unresolved placement requires a reason", [entity_id])
    else:
        allowed = "placed" if require_placed else "placed or unresolved"
        raise _reject("invalid_value", f"placement status must be {allowed}", [entity_id])
    if not isinstance(value["manual"], bool):
        raise _reject("invalid_value", "placement manual must be a boolean", [entity_id])
    _require_assumption(project, value["assumption_id"], [entity_id])
    return value


def _quantity(project, value, declaration, entity_id, name):
    if not isinstance(value, dict):
        raise _reject("unsupported_payload", "quantity must be an object", [entity_id])
    extra = sorted(set(value) - {"value", "unit", "assumption_id", "source"})
    if extra or "value" not in value or "unit" not in value:
        raise _reject("unsupported_payload", "quantity must be {value, unit} plus one provenance field", [entity_id])
    provenance = [key for key in ("assumption_id", "source") if key in value]
    if not provenance:
        raise _reject("missing_provenance", f"parameter {name} requires an assumption_id or source", [entity_id])
    if len(provenance) > 1:
        raise _reject("unsupported_payload", "quantity must carry exactly one of assumption_id or source", [entity_id])
    if "assumption_id" in value:
        _require_assumption(project, value["assumption_id"], [entity_id])
    elif not isinstance(value["source"], str) or not value["source"].strip():
        raise _reject("missing_provenance", "quantity source must be a nonempty string", [entity_id])
    number = _number(value["value"], f"parameter {name}", [entity_id])
    if value["unit"] != declaration.get("unit"):
        raise _reject("invalid_unit", f"parameter {name} must use unit {declaration.get('unit')!r}", [entity_id])
    bounds = (("min_exclusive", lambda b: number > b), ("min_inclusive", lambda b: number >= b),
              ("max_exclusive", lambda b: number < b), ("max_inclusive", lambda b: number <= b))
    for key, accepts in bounds:
        if key in declaration and not accepts(declaration[key]):
            raise _reject("invalid_value", f"parameter {name} violates {key} {declaration[key]}", [entity_id])
    return value


# Geometry helpers shared with templates


def _world(project, port_id):
    try:
        position = world_port_position(project, port_id)
    except _POSITION_ERRORS:
        return None
    return None if position is None else tuple(float(v) for v in position)


def _anchor(project, port_id, symbol_id):
    try:
        return tuple(float(v) for v in diagram_port_position(project, port_id, symbol_id))
    except _POSITION_ERRORS:
        return None


def _near(point, target, tolerance):
    return len(point) == len(target) and all(abs(a - b) <= tolerance for a, b in zip(point, target))


def _route_problems(project, connection_id, points):
    """Explain why physical points are not a supported current route (empty if valid)."""
    connection = project["model"]["connections"][connection_id]
    start = _world(project, connection["from_port_id"])
    end = _world(project, connection["to_port_id"])
    if start is None or end is None:
        return [f"route {connection_id} has an endpoint port without a resolved world position"]
    if len(points) < 2:
        return [f"route {connection_id} needs at least two points"]
    problems = []
    if not _near(points[0], start, ROUTE_TOLERANCE_M):
        problems.append(f"route {connection_id} must begin at {list(start)}")
    if not _near(points[-1], end, ROUTE_TOLERANCE_M):
        problems.append(f"route {connection_id} must end at {list(end)}")
    for index, (a, b) in enumerate(zip(points, points[1:])):
        moving = [axis for axis in range(3) if abs(b[axis] - a[axis]) > ROUTE_TOLERANCE_M]
        if not moving:
            problems.append(f"route {connection_id} segment {index} has zero length")
        elif len(moving) > 1:
            problems.append(f"route {connection_id} segment {index} is not orthogonal")
    return problems


def _edge_anchors(project, edge):
    connection = project["model"]["connections"].get(edge.get("connection_id"))
    if connection is None:
        return None
    start = _anchor(project, connection["from_port_id"], edge.get("from_symbol_id"))
    end = _anchor(project, connection["to_port_id"], edge.get("to_symbol_id"))
    return None if start is None or end is None else (start, end)


def _edge_problems(project, edge, points):
    anchors = _edge_anchors(project, edge)
    if anchors is None:
        return [f"edge {edge.get('id')} has an endpoint anchor that cannot be resolved"]
    if len(points) < 2:
        return [f"edge {edge.get('id')} needs at least two points"]
    problems = []
    if not _near(points[0], anchors[0], DIAGRAM_TOLERANCE):
        problems.append(f"edge {edge.get('id')} must begin at {list(anchors[0])}")
    if not _near(points[-1], anchors[1], DIAGRAM_TOLERANCE):
        problems.append(f"edge {edge.get('id')} must end at {list(anchors[1])}")
    return problems


def _guided_path(start, end):
    """Deterministic orthogonal diagram path: straight if aligned, else a Z through mid-x."""
    (x1, y1), (x2, y2) = start, end
    if abs(x1 - x2) <= DIAGRAM_TOLERANCE or abs(y1 - y2) <= DIAGRAM_TOLERANCE:
        return [[x1, y1], [x2, y2]]
    mid = (x1 + x2) / 2
    return [[x1, y1], [mid, y1], [mid, y2], [x2, y2]]


def _entity_port_ids(project, entity_id):
    return {port_id for port_id, port in project["model"]["ports"].items() if port.get("entity_id") == entity_id}


def _incident_connections(project, port_ids):
    return sorted(cid for cid, connection in project["model"]["connections"].items()
                  if connection["from_port_id"] in port_ids or connection["to_port_id"] in port_ids)


def _unplaced_endpoints(project, connection_id):
    """Entities at either end without a ``placed`` placement record.

    Decided from the placement record, not from whether a world frame can be
    computed: inside a batch a placed composed entity's geometry may be
    temporarily unresolvable (a constraint violated until a later operation),
    which is rerouting work, not missing placement.
    """
    connection = project["model"]["connections"][connection_id]
    ports, placements = project["model"]["ports"], project["physical"]["placements"]
    unplaced = set()
    for port_id in (connection["from_port_id"], connection["to_port_id"]):
        owner = ports.get(port_id, {}).get("entity_id", port_id)
        if (placements.get(owner) or {}).get("status") != "placed":
            unplaced.add(owner)
    return sorted(unplaced)


def _invalidate_route(project, connection_id, cause, status=None):
    """Flag a physical route as not current, retaining its points, mode and assumption."""
    route = project["physical"]["routes"].get(connection_id)
    if route is None:
        return []
    unplaced = _unplaced_endpoints(project, connection_id)
    if status is None:
        status = "unresolved" if unplaced else "needs_reroute"
    if unplaced:
        reason = f"{cause}; placement unresolved for {', '.join(unplaced)}"
    else:
        reason = f"{cause}; supply validated route points"
    route["status"] = status
    route["reason"] = reason
    return [_diagnostic("route_invalidated", f"physical route {connection_id} is {status}: {reason}",
                        [connection_id, *unplaced], "warning")]


def _invalidate_edge(project, edge_id, cause, status=None):
    """Flag a diagram edge as not current, retaining its points."""
    edge = project["pid"]["edges"][edge_id]
    if status is None:
        status = "needs_reroute" if _edge_anchors(project, edge) is not None else "unresolved"
    edge["status"] = status
    edge["reason"] = cause
    return [_diagnostic("diagram_edge_invalidated", f"diagram edge {edge_id} is {status}: {cause}",
                        [edge_id, edge["connection_id"]], "warning")]


def _refresh_edge(project, edge_id, cause):
    """Regenerate a guided edge; flag, never replace, a manual edge whose endpoints moved."""
    edge = project["pid"]["edges"][edge_id]
    anchors = _edge_anchors(project, edge)
    if edge.get("manual"):
        if edge.get("status") == "current" and not _edge_problems(project, edge, edge["points"]):
            return []
        return _invalidate_edge(project, edge_id, f"{cause}; manual path preserved and needs repair")
    if anchors is None:
        return _invalidate_edge(project, edge_id, f"{cause}; endpoint anchor unresolved")
    edge["points"] = _guided_path(*anchors)
    edge["status"] = "current"
    edge.pop("reason", None)
    return []


# Command handlers: each mutates the working project copy and returns warning diagnostics.


def _rename_entity(project, payload):
    _fields(payload, ("entity_id", "tag"))
    entity = _lookup(project["model"]["entities"], payload["entity_id"], "entity")
    _check_tag_free(project, payload["tag"], payload["entity_id"])
    entity["tag"] = payload["tag"]
    return []


def _rename_project(project, payload):
    """Set the human-facing project name (trimmed); the project id never changes."""
    _fields(payload, ("name",))
    name = payload["name"].strip() if isinstance(payload["name"], str) else payload["name"]
    problem = project_name_problem(name)
    if problem is not None:
        raise _reject("invalid_project_name", problem, [project["id"]])
    project["name"] = name
    return []


def _set_parameter(project, payload):
    _fields(payload, ("entity_id", "name", "quantity"))
    entity_id = payload["entity_id"]
    entity = _lookup(project["model"]["entities"], entity_id, "entity")
    name = _require_id(payload["name"], "parameter name")
    declaration = _template(entity).get("parameters", {}).get(name)
    if declaration is None:
        raise _reject("unsupported_property", f"{entity.get('kind')} has no parameter {name!r}", [entity_id])
    entity["parameters"][name] = _quantity(project, payload["quantity"], declaration, entity_id, name)
    # Port frames may depend on dimensions: no current route may keep stale endpoints.
    warnings = []
    for connection_id in _incident_connections(project, _entity_port_ids(project, entity_id)):
        route = project["physical"]["routes"].get(connection_id)
        if route and route.get("status") == "current" and _route_problems(project, connection_id, route["points_m"]):
            warnings += _invalidate_route(project, connection_id, f"parameter {name} of {entity_id} moved a port")
    return warnings


def _set_port_size(project, payload):
    _fields(payload, ("port_id", "nominal_diameter_mm", "assumption_id"))
    port_id = payload["port_id"]
    model = project["model"]
    port = _lookup(model["ports"], port_id, "port")
    size = _number(payload["nominal_diameter_mm"], "nominal_diameter_mm", [port_id])
    if size <= 0:
        raise _reject("invalid_value", "nominal_diameter_mm must be positive", [port_id])
    _require_assumption(project, payload["assumption_id"], [port_id])
    port["nominal_diameter_mm"] = size
    port["assumption_id"] = payload["assumption_id"]
    problems = []
    # A template's declared DN is its only supported size: version 1 nozzle geometry does not scale with DN.
    entity = model["entities"][port["entity_id"]]
    supported = _template(entity).get("ports", {}).get(port["name"], {}).get("nominal_diameter_mm")
    if supported is not None and size != supported:
        problems.append(_diagnostic("unsupported_nominal_diameter", f"{entity['kind']} template version "
                                    f"{entity['template_version']} supports only DN {supported} on port "
                                    f"{port['name']}, not DN {size}", [port["entity_id"], port_id]))
    for connection_id in _incident_connections(project, {port_id}):
        connection = model["connections"][connection_id]
        other_id = connection["to_port_id"] if connection["from_port_id"] == port_id else connection["from_port_id"]
        other = model["ports"].get(other_id, {})
        if connection.get("nominal_diameter_mm") != size or other.get("nominal_diameter_mm") != size:
            problems.append(_diagnostic("dn_incompatible", f"DN {size} on port {port_id} is incompatible with "
                                        f"connection {connection_id}; the whole connection must change together",
                                        [port["entity_id"], port_id, connection_id, other_id]))
    if problems:
        raise CommandError(problems)
    return []


def _port_records(value):
    if isinstance(value, dict):
        records = []
        for key, record in value.items():
            if not isinstance(record, dict) or record.get("id") != key:
                raise _reject("unsupported_payload", f"ports entry {key!r} must be a port record with that id")
            records.append(record)
        return records
    if isinstance(value, list) and all(isinstance(record, dict) for record in value):
        return list(value)
    raise _reject("unsupported_payload", "ports must be a list or map of port records")


def _insert_valve(project, payload):
    return _insert_inline(project, payload, "valve")


def _insert_equipment(project, payload):
    return _insert_inline(project, payload, "equipment")


def _insert_inline(project, payload, noun):
    """Rewire ``A -> B`` into ``A -> item.inlet, item.outlet -> B`` for a valve or composed equipment."""
    _fields(payload, ("connection_id", "entity", "ports", "downstream_connection", "symbol", "downstream_edge"),
            ("placement", "upstream_route", "downstream_route"))
    model, pid, physical = project["model"], project["pid"], project["physical"]
    upstream_id = payload["connection_id"]
    upstream = _lookup(model["connections"], upstream_id, "connection")
    entity = _fields(payload["entity"], ("id", "kind", "tag", "template_version", "parameters"),
                     ("observes",) if noun == "valve" else ("definition",), what="entity")
    ports = _port_records(payload["ports"])
    downstream = _fields(payload["downstream_connection"], ("id", "tag", "from_port_id", "to_port_id",
                         "nominal_diameter_mm", "outside_diameter_m", "assumption_id"), what="downstream_connection")
    symbol = _fields(payload["symbol"], ("id", "entity_id", "x", "y", "rotation_deg", "manual"), what="symbol")
    down_edge = _fields(payload["downstream_edge"], _EDGE_FIELDS, ("reason",), what="downstream_edge")

    if noun == "valve":
        if entity["kind"] not in VALVE_KINDS:
            raise _reject("unsupported_property", f"insert_valve supports kinds {', '.join(VALVE_KINDS)}",
                          [str(entity["id"])])
        if "observes" in entity:
            raise _reject("unsupported_property", "an inserted valve cannot observe another entity",
                          [str(entity["id"])])
    else:
        if entity["kind"] != COMPOSED_KIND:
            raise _reject("unsupported_property", f"insert_equipment supports kind {COMPOSED_KIND} with a definition; "
                          f"use insert_valve for {', '.join(VALVE_KINDS)}", [str(entity["id"])])
        if "definition" not in entity:
            raise _reject("missing_definition", f"{COMPOSED_KIND} entity {entity['id']!r} needs a definition",
                          [str(entity["id"])])
    template = _template(entity)
    if noun == "equipment":
        try:
            inline_ports(template)
        except DefinitionError as exc:
            raise _reject("unsupported_property", f"insert_equipment: {exc}", [str(entity["id"])]) from None

    new_ids = [entity["id"], *(port.get("id") for port in ports), downstream["id"], symbol["id"], down_edge["id"]]
    for new_id in new_ids:
        _require_id(new_id, "new record id")
    existing = _all_ids(project)
    seen = set()
    for new_id in new_ids:
        if new_id in existing or new_id in seen:
            raise _reject("duplicate_id", f"id {new_id!r} is already in use", [new_id])
        seen.add(new_id)
    _check_tag_free(project, entity["tag"], entity["id"])
    _check_tag_free(project, downstream["tag"], downstream["id"], also=[(entity["id"], entity["tag"])])
    # Port positions derive from these quantities, so they must hold before any geometry is resolved.
    declarations = template.get("parameters", {})
    _fields(entity["parameters"], tuple(declarations), what="entity parameters")
    for name, declaration in declarations.items():
        _quantity(project, entity["parameters"][name], declaration, entity["id"], name)
    if noun == "equipment":
        # Current values, never recipe defaults, must satisfy the definition's own constraints.
        try:
            resolve_definition(entity)
        except DefinitionError as exc:
            raise _reject("definition_resolution_failed", f"{entity['id']}: {exc}", [entity["id"]]) from None
    for key in ("x", "y", "rotation_deg"):
        _number(symbol[key], f"symbol {key}", [symbol["id"]])

    template_ports = template.get("ports", {})
    by_name = {}
    for port in ports:
        if port.get("entity_id") != entity["id"]:
            raise _reject("topology_mismatch", f"port {port['id']} must belong to {entity['id']}", [port["id"]])
        if "connection_status" in port:  # both inserted ports are connected by the insertion itself
            raise _reject("invalid_connection_status", f"port {port['id']} is connected by this insertion, so it "
                          f"cannot carry connection_status", [port["id"]])
        name = port.get("name")
        if not isinstance(name, str) or name in by_name or name not in template_ports:
            raise _reject("topology_mismatch", f"port {port['id']} has a duplicate or unknown name", [port["id"]])
        by_name[name] = port
    if set(by_name) != set(template_ports):
        raise _reject("topology_mismatch", f"ports must be exactly {sorted(template_ports)}", [entity["id"]])
    inlets = [by_name[n] for n, spec in template_ports.items() if spec.get("direction") == "in"]
    outlets = [by_name[n] for n, spec in template_ports.items() if spec.get("direction") == "out"]
    if len(inlets) != 1 or len(outlets) != 1:
        raise _reject("missing_template", f"{noun} template must declare one inlet and one outlet", [entity["id"]])
    inlet, outlet = inlets[0], outlets[0]
    original_to = upstream["to_port_id"]
    if downstream["from_port_id"] != outlet["id"] or downstream["to_port_id"] != original_to:
        raise _reject("topology_mismatch", f"downstream_connection must run {outlet['id']} -> {original_to}",
                      [downstream["id"], upstream_id])

    edge_ids = sorted(eid for eid, edge in pid["edges"].items() if edge.get("connection_id") == upstream_id)
    if len(edge_ids) != 1:
        raise _reject("unsupported_payload", f"connection {upstream_id} must have exactly one diagram edge "
                      f"to split (found {len(edge_ids)})", [upstream_id, *edge_ids])
    up_edge_id = edge_ids[0]
    up_edge = pid["edges"][up_edge_id]
    if symbol["entity_id"] != entity["id"]:
        raise _reject("topology_mismatch", f"symbol {symbol['id']} must reference {entity['id']}", [symbol["id"]])
    if (down_edge["connection_id"] != downstream["id"] or down_edge["from_symbol_id"] != symbol["id"]
            or down_edge["to_symbol_id"] != up_edge["to_symbol_id"]):
        raise _reject("topology_mismatch", f"downstream_edge must map {downstream['id']} from {symbol['id']} "
                      f"to {up_edge['to_symbol_id']}", [down_edge["id"]])
    if down_edge["status"] not in PATH_STATUSES:
        raise _reject("invalid_value", "downstream_edge status is not supported", [down_edge["id"]])
    _points(down_edge["points"], 2, "downstream_edge points", 2 if down_edge["status"] == "current" else 0)
    if not isinstance(down_edge["manual"], bool):
        raise _reject("invalid_value", "downstream_edge manual must be a boolean", [down_edge["id"]])

    placement = payload.get("placement")
    if placement is not None:
        _placement(project, placement, entity["id"], require_placed=False)
    split = {}
    for connection_id, key in ((upstream_id, "upstream_route"), (downstream["id"], "downstream_route")):
        record = payload.get(key)
        if record is not None:
            _fields(record, _ROUTE_FIELDS, ("reason",), what=key)
            if record["status"] not in PATH_STATUSES or record["mode"] not in ROUTE_MODES:
                raise _reject("invalid_value", f"{key} status or mode is not supported", [connection_id])
            _points(record["points_m"], 3, f"{key} points_m", 2 if record["status"] == "current" else 0)
            _require_assumption(project, record["assumption_id"], [connection_id])
        split[connection_id] = record

    # Rewire A -> B into A -> item.inlet, item.outlet -> B; upstream IDs are retained.
    model["entities"][entity["id"]] = entity
    for port in ports:
        model["ports"][port["id"]] = port
    upstream["to_port_id"] = inlet["id"]
    model["connections"][downstream["id"]] = downstream
    pid["symbols"][symbol["id"]] = symbol
    up_edge["to_symbol_id"] = symbol["id"]
    pid["edges"][down_edge["id"]] = down_edge

    warnings = []
    if placement is None:
        placement = {"position_m": None, "rotation_deg": None, "status": "unresolved", "manual": False,
                     "assumption_id": downstream["assumption_id"],
                     "reason": f"no physical placement supplied when {entity['id']} was inserted"}
    if placement["status"] != "placed":
        warnings.append(_diagnostic("placement_unresolved", f"{entity['id']} has no physical placement: "
                                    f"{placement['reason']}", [entity["id"]], "warning"))
    physical["placements"][entity["id"]] = placement
    warnings += _split_edges(project, up_edge_id, down_edge["id"], entity["id"], noun)
    warnings += _split_routes(project, upstream_id, downstream["id"], entity["id"], split, noun)
    return warnings


def _split_edges(project, up_edge_id, down_edge_id, valve_id, noun="valve"):
    down_edge = project["pid"]["edges"][down_edge_id]
    if down_edge["status"] == "current":
        problems = _edge_problems(project, down_edge, down_edge["points"])
        if problems:
            cause = f"diagram split at {valve_id} could not be validated ({'; '.join(problems)})"
            return (_invalidate_edge(project, up_edge_id, cause, "unresolved")
                    + _invalidate_edge(project, down_edge_id, cause, "unresolved"))
        down_edge.pop("reason", None)
    return _refresh_edge(project, up_edge_id, f"{noun} {valve_id} inserted")


def _split_routes(project, upstream_id, downstream_id, valve_id, split, noun="valve"):
    routes = project["physical"]["routes"]
    prior = routes.get(upstream_id)
    problems = []
    if any(record is not None and record["status"] == "current" for record in split.values()):
        # An explicit split counts only if both halves are supplied and validate.
        for connection_id, record in split.items():
            if record is None:
                problems.append(f"no route supplied for {connection_id}")
            elif record["status"] == "current":
                problems += _route_problems(project, connection_id, record["points_m"])
    if not problems:
        # A route-valid current half is a new route submission: refuse it, as set_route does, if it enters a body.
        for connection_id, record in split.items():
            if record is not None and record["status"] == "current":
                _screen_route(project, connection_id, record["points_m"], f"{noun} {valve_id} split: ")
    warnings = []
    for connection_id, record in split.items():
        if record is not None and not problems:
            if record["status"] == "current":
                record.pop("reason", None)
            routes[connection_id] = record
            continue
        # Retain prior points for the upstream connection; otherwise the supplied points, if any.
        base = prior if connection_id == upstream_id and prior is not None else record
        fallback = prior or project["model"]["connections"][connection_id]
        routes[connection_id] = {
            "points_m": copy.deepcopy(base["points_m"]) if base else [],
            "mode": base["mode"] if base else "guided",
            "status": "unresolved",
            "assumption_id": base["assumption_id"] if base else fallback["assumption_id"],
        }
        if problems:
            cause = f"route split at {valve_id} could not be validated ({'; '.join(problems)})"
            warnings += _invalidate_route(project, connection_id, cause, "unresolved")
        else:
            warnings += _invalidate_route(project, connection_id, f"{noun} {valve_id} inserted without this route")
    return warnings


def _new_ids(project, ids):
    """Every ID must be a nonempty string unused in the project and among ``ids``."""
    for new_id in ids:
        _require_id(new_id, "new record id")
    existing, seen = _all_ids(project), set()
    for new_id in ids:
        if new_id in existing or new_id in seen:
            raise _reject("duplicate_id", f"id {new_id!r} is already in use", [new_id])
        seen.add(new_id)


def _same_typed(value, expected):
    """Exact declared values: booleans only equal booleans, numbers compare numerically, text exactly."""
    if isinstance(expected, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value == expected
    if _is_number(expected):
        return _is_number(value) and value == expected
    return isinstance(value, str) and value == expected


def _symbol_record(project, symbol, entity_id):
    _fields(symbol, _SYMBOL_FIELDS, what="symbol")
    if symbol["entity_id"] != entity_id:
        raise _reject("topology_mismatch", f"symbol {symbol['id']} must reference {entity_id}", [symbol["id"]])
    for key in ("x", "y", "rotation_deg"):
        _number(symbol[key], f"symbol {key}", [symbol["id"]])
    if not isinstance(symbol["manual"], bool):
        raise _reject("invalid_value", "symbol manual must be a boolean", [symbol["id"]])
    return symbol


def _add_equipment(project, payload):
    """Add one entity with exactly its declared ports, one P&ID symbol and an optional placement.

    Any supported built-in template or a validated composed definition. Its
    parameters must be exactly the declared ones with provenance and resolve at
    their current values; each port record must repeat its declared direction,
    required flag, DN and service. Nothing is connected, so every required port
    is recorded with the explicit draft marker ``connection_status:
    "unresolved"`` (supplied or derived here; no other value and no marker on
    an optional port): validation then reports it as a pending
    ``required_port_unconnected`` warning until ``connect_ports`` clears it.
    Without a placement the entity is recorded ``unresolved``.
    """
    _fields(payload, ("entity", "ports", "symbol"), ("placement",))
    model, pid, physical = project["model"], project["pid"], project["physical"]
    entity = _fields(payload["entity"], _ENTITY_FIELDS, ("definition", "observes"), what="entity")
    entity_id = _require_id(entity["id"], "entity id")
    if not isinstance(entity["kind"], str) or not entity["kind"].strip():
        raise _reject("unsupported_payload", "entity kind must be a nonempty string", [entity_id])
    if "observes" in entity and entity["kind"] != "indicator":
        raise _reject("invalid_observes", "only indicators observe other entities", [entity_id])
    if entity["kind"] == COMPOSED_KIND and "definition" not in entity:
        raise _reject("missing_definition", f"{COMPOSED_KIND} entity {entity_id!r} needs a definition", [entity_id])
    template = _template(entity)
    ports = _port_records(payload["ports"])
    symbol = payload["symbol"]
    _fields(symbol, _SYMBOL_FIELDS, what="symbol")
    _new_ids(project, [entity_id, *(port.get("id") for port in ports), symbol["id"]])
    _check_tag_free(project, entity["tag"], entity_id)

    declarations = template.get("parameters", {})
    _fields(entity["parameters"], tuple(declarations), what="entity parameters")
    for name, declaration in declarations.items():
        _quantity(project, entity["parameters"][name], declaration, entity_id, name)
    if is_composed(entity):
        # Current values, never recipe defaults, must satisfy the definition's own constraints.
        try:
            resolve_definition(entity)
        except DefinitionError as exc:
            raise _reject("definition_resolution_failed", f"{entity_id}: {exc}", [entity_id]) from None
    if "observes" in entity:
        observes = _fields(entity["observes"], ("entity_id", "variable"), what="entity observes")
        _lookup(model["entities"], observes["entity_id"], "observed entity")
        if not isinstance(observes["variable"], str) or not observes["variable"].strip():
            raise _reject("invalid_observes", "observes variable must be a nonempty string", [entity_id])

    template_ports = template.get("ports", {})
    by_name = {}
    for port in ports:
        port_id = port["id"]
        _fields(port, _PORT_FIELDS, ("connection_status",), what=f"port {port_id}")
        if port["entity_id"] != entity_id:
            raise _reject("topology_mismatch", f"port {port_id} must belong to {entity_id}", [port_id])
        name = port["name"]
        if not isinstance(name, str) or name not in template_ports:
            raise _reject("unknown_port", f"port {port_id} name {name!r} is not declared by {entity_id}'s template "
                          f"(declared: {', '.join(sorted(template_ports)) or 'none'})", [port_id, entity_id])
        if name in by_name:
            raise _reject("duplicate_port_name", f"{entity_id} has more than one port named {name!r}",
                          [entity_id, by_name[name]["id"], port_id])
        spec = template_ports[name]
        for key, code in (("direction", "wrong_port_direction"), ("required", "port_template_mismatch"),
                          ("nominal_diameter_mm", "unsupported_nominal_diameter"), ("service", "port_template_mismatch")):
            if not _same_typed(port[key], spec.get(key)):
                raise _reject(code, f"port {port_id} {key} {port[key]!r} must be exactly the declared "
                              f"{spec.get(key)!r} of port {name!r}", [port_id, entity_id])
        _require_assumption(project, port["assumption_id"], [port_id])
        if "connection_status" in port and (port["connection_status"] != OPEN_PORT or spec.get("required") is not True):
            raise _reject("invalid_connection_status", f"port {port_id} connection_status may only be {OPEN_PORT!r}, on "
                          f"a required port", [port_id, entity_id])
        if spec.get("required") is True:
            port["connection_status"] = OPEN_PORT  # a new port is never connected yet: explicitly open, not broken
        by_name[name] = port
    if set(by_name) != set(template_ports):
        raise _reject("topology_mismatch", f"ports must be exactly {sorted(template_ports)} for {entity_id}, not "
                      f"{sorted(by_name)}", [entity_id])
    _symbol_record(project, symbol, entity_id)
    placement = payload.get("placement")
    if placement is not None:
        _placement(project, placement, entity_id, require_placed=False)

    model["entities"][entity_id] = entity
    for port in ports:
        model["ports"][port["id"]] = port
    pid["symbols"][symbol["id"]] = symbol
    if placement is None:
        placement = {"position_m": None, "rotation_deg": None, "status": "unresolved", "manual": False,
                     "assumption_id": None, "reason": f"no physical placement supplied when {entity_id} was added"}
    physical["placements"][entity_id] = placement
    if placement["status"] != "placed":
        return [_diagnostic("placement_unresolved", f"{entity_id} has no physical placement: {placement['reason']}",
                            [entity_id], "warning")]
    return []


def _connect_edge(project, edge, connection):
    """Validate the new connection's P&ID edge; an empty guided current path is derived deterministically."""
    edge_id, cid = edge["id"], connection["id"]
    if edge["connection_id"] != cid:
        raise _reject("topology_mismatch", f"edge {edge_id} must map connection {cid}", [edge_id, cid])
    ports, symbols = project["model"]["ports"], project["pid"]["symbols"]
    for role in ("from", "to"):
        symbol_id = edge[f"{role}_symbol_id"]
        symbol = _lookup(symbols, symbol_id, f"{role} symbol")
        owner = ports[connection[f"{role}_port_id"]]["entity_id"]
        if symbol.get("entity_id") != owner:
            raise _reject("occurrence_mismatch", f"edge {edge_id} {role}_symbol_id {symbol_id} shows "
                          f"{symbol.get('entity_id')}, but the {role} port belongs to {owner}", [edge_id, symbol_id])
    if edge["status"] not in PATH_STATUSES:
        raise _reject("invalid_value", f"edge status must be one of {', '.join(PATH_STATUSES)}", [edge_id])
    if not isinstance(edge["manual"], bool):
        raise _reject("invalid_value", "edge manual must be a boolean", [edge_id])
    if edge["status"] != "current":
        if not isinstance(edge.get("reason"), str) or not edge["reason"].strip():
            raise _reject("invalid_value", f"a {edge['status']} edge needs a nonempty reason", [edge_id])
        _points(edge["points"], 2, "edge points", 0)
        return [_diagnostic("diagram_edge_invalidated", f"diagram edge {edge_id} is {edge['status']}: "
                            f"{edge['reason']}", [edge_id, cid], "warning")]
    if "reason" in edge:
        raise _reject("unsupported_payload", "a current edge carries no reason", [edge_id])
    if edge["points"] == [] and not edge["manual"]:
        anchors = _edge_anchors(project, edge)
        if anchors is None:
            raise _reject("invalid_route", f"edge {edge_id} endpoint anchors cannot be resolved", [edge_id])
        edge["points"] = _guided_path(*anchors)
        return []
    _points(edge["points"], 2, "edge points", 2)
    problems = _edge_problems(project, edge, edge["points"])
    if problems:
        raise CommandError([_diagnostic("invalid_route", problem, [edge_id]) for problem in problems])
    return []


def _connect_ports(project, payload):
    """Join two existing, unused ports by stable identity with a new line, its P&ID edge and an optional route.

    Never by proximity. The ``from`` port must be ``out``/``both`` and the
    ``to`` port ``in``/``both``; line and both ports share one DN and service.
    Both ports lose their ``connection_status`` draft marker. A current route
    must start and end on the port frames and is refused if it enters a placed
    body; an omitted route is recorded ``unresolved``.
    """
    _fields(payload, ("connection", "edge"), ("route",))
    model, pid, physical = project["model"], project["pid"], project["physical"]
    connection = _fields(payload["connection"], _CONNECTION_FIELDS, what="connection")
    edge = _fields(payload["edge"], _EDGE_FIELDS, ("reason",), what="edge")
    cid, edge_id = connection["id"], edge["id"]
    _new_ids(project, [cid, edge_id])
    _check_tag_free(project, connection["tag"], cid)
    ends = {}
    for role in ("from", "to"):
        ends[role] = _lookup(model["ports"], connection[f"{role}_port_id"], f"{role} port")
    from_id, to_id = connection["from_port_id"], connection["to_port_id"]
    if from_id == to_id:
        raise _reject("self_connection", f"connection {cid} joins port {from_id} to itself", [cid, from_id])
    for role, allowed in (("from", ("out", "both")), ("to", ("in", "both"))):
        port = ends[role]
        if port.get("direction") not in allowed:
            raise _reject("wrong_port_direction", f"{role} port {port['id']} has direction {port.get('direction')!r}; "
                          f"expected {' or '.join(allowed)}", [cid, port["id"], port.get("entity_id", "")])
    size = connection["nominal_diameter_mm"]
    if not _is_number(size) or size <= 0 or not float(size).is_integer():
        raise _reject("invalid_nominal_diameter", "nominal_diameter_mm must be a positive whole-number DN", [cid])
    for role in ("from", "to"):
        port = ends[role]
        if port.get("nominal_diameter_mm") != size:
            raise _reject("dn_mismatch", f"connection {cid} is DN{size:g} but its {role} port {port['id']} is "
                          f"DN{port.get('nominal_diameter_mm')}", [cid, port["id"]])
    if ends["from"].get("service") != ends["to"].get("service"):
        raise _reject("service_mismatch", f"connection {cid} joins {ends['from'].get('service')!r} and "
                      f"{ends['to'].get('service')!r} service ports", [cid, from_id, to_id])
    outside = _number(connection["outside_diameter_m"], "outside_diameter_m", [cid])
    if outside <= 0:
        raise _reject("invalid_value", "outside_diameter_m must be positive", [cid])
    _require_assumption(project, connection["assumption_id"], [cid])
    for port_id in (from_id, to_id):
        used = _incident_connections(project, {port_id})
        if used:
            raise _reject("duplicate_port_use", f"port {port_id} is already used by {', '.join(used)}; each port "
                          "takes one connection and branches need an explicit junction entity", [port_id, *used, cid])

    model["connections"][cid] = connection
    for role in ("from", "to"):
        ends[role].pop("connection_status", None)  # joined: no longer an open draft port
    pid["edges"][edge_id] = edge
    warnings = _connect_edge(project, edge, connection)
    route = payload.get("route")
    if route is None:
        physical["routes"][cid] = {"points_m": [], "mode": "guided", "status": "unresolved",
                                   "assumption_id": connection["assumption_id"]}
        return warnings + _invalidate_route(project, cid, f"no physical route supplied when {cid} was connected",
                                            "unresolved")
    _fields(route, _ROUTE_FIELDS, ("reason",), what="route")
    if route["status"] not in PATH_STATUSES or route["mode"] not in ROUTE_MODES:
        raise _reject("invalid_value", "route status or mode is not supported", [cid])
    _require_assumption(project, route["assumption_id"], [cid])
    if route["status"] != "current":
        if not isinstance(route.get("reason"), str) or not route["reason"].strip():
            raise _reject("invalid_value", f"a {route['status']} route needs a nonempty reason", [cid])
        _points(route["points_m"], 3, "route points_m", 0)
        physical["routes"][cid] = route
        return warnings + [_diagnostic("route_invalidated", f"physical route {cid} is {route['status']}: "
                                       f"{route['reason']}", [cid], "warning")]
    if "reason" in route:
        raise _reject("unsupported_payload", "a current route carries no reason", [cid])
    points = _points(route["points_m"], 3, "route points_m")
    problems = _route_problems(project, cid, points)
    if problems:
        raise CommandError([_diagnostic("invalid_route", problem, [cid]) for problem in problems])
    _screen_route(project, cid, points)
    physical["routes"][cid] = route
    return warnings


def _set_placement(project, payload):
    _fields(payload, ("entity_id", "placement"))
    entity_id = payload["entity_id"]
    _lookup(project["model"]["entities"], entity_id, "entity")
    placement = _placement(project, payload["placement"], entity_id, require_placed=True)
    placements = project["physical"]["placements"]
    if entity_id in placements and _same(placements[entity_id], placement):
        return []
    placements[entity_id] = placement
    warnings = []
    for connection_id in _incident_connections(project, _entity_port_ids(project, entity_id)):
        warnings += _invalidate_route(project, connection_id, f"equipment {entity_id} placement changed")
    return warnings


def _move_symbol(project, payload):
    _fields(payload, ("symbol_id", "x", "y", "rotation_deg"))
    symbol_id = payload["symbol_id"]
    symbol = _lookup(project["pid"]["symbols"], symbol_id, "symbol")
    values = {key: _number(payload[key], key, [symbol_id]) for key in ("x", "y", "rotation_deg")}
    if all(symbol.get(key) == value for key, value in values.items()):
        return []
    symbol.update(values)
    symbol["manual"] = True
    warnings = []
    for edge_id, edge in sorted(project["pid"]["edges"].items()):
        if symbol_id in (edge.get("from_symbol_id"), edge.get("to_symbol_id")):
            warnings += _refresh_edge(project, edge_id, f"symbol {symbol_id} moved")
    return warnings


def _screen_route(project, connection_id, points, where=""):
    """Refuse points whose conservative envelope enters a placed equipment body (or cannot be screened)."""
    try:
        report = route_report(project, connection_id, points)
    except ClearanceError as exc:
        raise _reject("route_clearance_unavailable", f"{where}route {connection_id}: {exc.message}",
                      [connection_id, *exc.ids]) from None
    if report["clear"]:
        return
    seen, diagnostics = set(), []
    for found in report["intrusions"]:
        key = (found["entity_id"], found["component"])
        if key in seen:
            continue
        seen.add(key)
        depth = ", ".join(f"{d * 1000:.1f}" for d in found["overlap_m"])
        diagnostics.append(_diagnostic("route_intrusion", f"{where}route {connection_id} would enter {found['tag']} "
                                       f"{found['component']} at its {found['route_part']} {found['route_index']} "
                                       f"(overlap {depth} mm; {report['model']})",
                                       [connection_id, found["entity_id"]]))
    raise CommandError(diagnostics)


def _set_route(project, payload):
    _fields(payload, ("connection_id", "points_m", "mode", "assumption_id"))
    connection_id = payload["connection_id"]
    _lookup(project["model"]["connections"], connection_id, "connection")
    points = _points(payload["points_m"], 3, "points_m")
    if payload["mode"] not in ROUTE_MODES:
        raise _reject("invalid_value", f"route mode must be one of {', '.join(ROUTE_MODES)}", [connection_id])
    _require_assumption(project, payload["assumption_id"], [connection_id])
    problems = _route_problems(project, connection_id, points)
    if problems:
        raise CommandError([_diagnostic("invalid_route", problem, [connection_id]) for problem in problems])
    _screen_route(project, connection_id, points)
    project["physical"]["routes"][connection_id] = {
        "points_m": points, "mode": payload["mode"], "status": "current", "assumption_id": payload["assumption_id"],
    }
    return []


def _move_route_segment(project, payload):
    _fields(payload, ("connection_id", "segment_index", "offset_m", "assumption_id"))
    connection_id = payload["connection_id"]
    _lookup(project["model"]["connections"], connection_id, "connection")
    index = payload["segment_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise _reject("invalid_value", "segment_index must be a nonnegative integer", [connection_id])
    offset = payload["offset_m"]
    if not isinstance(offset, list) or len(offset) != 3 or not all(_is_number(v) for v in offset):
        raise _reject("invalid_value", "offset_m must be three finite numbers [dx, dy, dz] in metres", [connection_id])
    _require_assumption(project, payload["assumption_id"], [connection_id])
    try:
        points = move_section(project, connection_id, index, offset)
    except RouteEditError as exc:
        diagnostics, seen = [_diagnostic(exc.code, exc.message, exc.ids or [connection_id])], set()
        for found in exc.details or ():
            key = (found.get("entity_id"), found.get("component"), found["route_part"], found["route_index"])
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(_diagnostic("route_intrusion", f"{found.get('tag')} {found.get('component')} at the "
                                           f"route's {found['route_part']} {found['route_index']}",
                                           [connection_id, found.get("entity_id", "")]))
        raise CommandError(diagnostics) from None
    except ClearanceError as exc:
        raise _reject("route_clearance_unavailable", f"route {connection_id}: {exc.message}",
                      [connection_id, *exc.ids]) from None
    problems = _route_problems(project, connection_id, points)
    if problems:
        raise CommandError([_diagnostic("invalid_route", problem, [connection_id]) for problem in problems])
    project["physical"]["routes"][connection_id] = {
        "points_m": points, "mode": "manual", "status": "current", "assumption_id": payload["assumption_id"],
    }
    return []


def _require_fresh_assumption(kind, payload, added, where=""):
    """A moved section is a new manual route: its assumption must be added by the same command or operation."""
    if kind == "move_route_segment" and payload.get("assumption_id") not in added:
        raise _reject("assumption_not_fresh", f"{where}move_route_segment must cite an assumption added by the same "
                      f"command, not {payload.get('assumption_id')!r}", [payload.get("connection_id", "")])


def _written_routes(operation):
    """Connections whose route ``operation`` submitted as new points: route edits, current connection routes and
    current insertion halves."""
    payload = operation["payload"]
    if operation["type"] in ("set_route", "move_route_segment"):
        return [payload["connection_id"]]
    if operation["type"] == "connect_ports":
        route = payload.get("route")
        return [payload["connection"]["id"]] if route is not None and route["status"] == "current" else []
    if operation["type"] in ("insert_valve", "insert_equipment"):
        halves = ((payload["connection_id"], payload.get("upstream_route")),
                  (payload["downstream_connection"]["id"], payload.get("downstream_route")))
        return [cid for cid, record in halves if record is not None and record["status"] == "current"]
    return []


def _screen_batch_routes(project, operations):
    """Re-screen, in the final batch result, every current route an operation submitted.

    Routes a later operation invalidated are no longer current and stay honestly pending.
    """
    for index, operation in enumerate(operations):
        for connection_id in _written_routes(operation):
            route = project["physical"]["routes"].get(connection_id)
            if route is not None and route.get("status") == "current":
                _screen_route(project, connection_id, route["points_m"],
                              f"final batch result of operations[{index}] ({operation['type']}): ")


def _set_diagram_route(project, payload):
    _fields(payload, ("edge_id", "points"))
    edge_id = payload["edge_id"]
    edge = _lookup(project["pid"]["edges"], edge_id, "edge")
    points = _points(payload["points"], 2, "points")
    problems = _edge_problems(project, edge, points)
    if problems:
        raise CommandError([_diagnostic("invalid_route", problem, [edge_id]) for problem in problems])
    # An explicitly supplied path is a manual layout choice that regeneration must keep.
    edge["points"] = points
    edge["manual"] = True
    edge["status"] = "current"
    edge.pop("reason", None)
    return []


_HANDLERS = {
    "rename_project": _rename_project,
    "rename_entity": _rename_entity,
    "set_parameter": _set_parameter,
    "set_port_size": _set_port_size,
    "insert_valve": _insert_valve,
    "insert_equipment": _insert_equipment,
    "add_equipment": _add_equipment,
    "connect_ports": _connect_ports,
    "move_equipment": _set_placement,
    "place_equipment": _set_placement,
    "move_symbol": _move_symbol,
    "set_route": _set_route,
    "move_route_segment": _move_route_segment,
    "set_diagram_route": _set_diagram_route,
}
COMMAND_TYPES = (*_HANDLERS, "batch", "undo", "redo")


# Transactions and history


def _snapshot(project):
    snapshot = {key: copy.deepcopy(project[key]) for key in _SNAPSHOT_FIELDS}
    snapshot.update((key, project[key]) for key in _OPTIONAL_SNAPSHOT_FIELDS if key in project)
    return snapshot


def _restore(project, snapshot):
    """Install a history snapshot; an optional field absent from it is removed, never kept from the present."""
    for key in _SNAPSHOT_FIELDS:
        project[key] = snapshot[key]
    for key in _OPTIONAL_SNAPSHOT_FIELDS:
        if key in snapshot:
            project[key] = snapshot[key]
        else:
            project.pop(key, None)


def _add_assumptions(project, assumptions):
    if not isinstance(assumptions, dict) or not assumptions:
        raise _reject("unsupported_payload", "assumptions must be a nonempty map of assumption records")
    existing_ids = _all_ids(project)
    target = project["model"]["assumptions"]
    for assumption_id, record in assumptions.items():
        _require_id(assumption_id, "assumption id")
        if assumption_id in existing_ids:
            raise _reject("assumption_conflict", f"id {assumption_id!r} already exists; existing assumptions "
                          "cannot be overwritten", [assumption_id])
        _fields(record, ("description", "source"), what=f"assumption {assumption_id}")
        if not all(isinstance(record[key], str) and record[key].strip() for key in ("description", "source")):
            raise _reject("invalid_value", "assumption description and source must be nonempty", [assumption_id])
        target[assumption_id] = record
    return sorted(assumptions)


def _assumption_references(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "assumption_id" and isinstance(item, str):
                found.add(item)
            elif key != "definition":  # a definition's assumption IDs are local to it, not project references
                _assumption_references(item, found)
    elif isinstance(value, list):
        for item in value:
            _assumption_references(item, found)
    return found


def _check_envelope(command):
    if not isinstance(command, dict):
        raise _reject("invalid_command", "command must be an object")
    missing = [name for name in _ENVELOPE_FIELDS if name not in command]
    extra = sorted(str(name) for name in set(command) - set(_ENVELOPE_FIELDS) - {"assumptions"})
    if missing or extra:
        raise _reject("invalid_command", f"command envelope missing {missing} or with unsupported {extra}")
    _require_id(command["id"], "command id")
    revision = command["expected_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise _reject("invalid_command", "expected_revision must be a nonnegative integer")
    if not isinstance(command["type"], str) or command["type"] not in COMMAND_TYPES:
        raise _reject("unsupported_command", f"unsupported command type {command['type']!r}")
    if not isinstance(command["payload"], dict):
        raise _reject("unsupported_payload", "payload must be an object")
    if command["type"] == "batch" and "assumptions" in command:
        raise _reject("unsupported_payload", "a batch carries assumptions on the operation that adds them, not on "
                      "its envelope")


def _batch_operations(payload):
    _fields(payload, ("operations",))
    operations = payload["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_BATCH_OPERATIONS:
        raise _reject("unsupported_payload", f"batch operations must be a list of 1 to {MAX_BATCH_OPERATIONS} "
                      f"operations")
    for index, operation in enumerate(operations):
        where = f"operations[{index}]"
        _fields(operation, _OPERATION_FIELDS, ("assumptions",), what=where)
        kind = operation["type"]
        if kind in ("batch", "undo", "redo"):
            raise _reject("unsupported_command", f"{where}: {kind} cannot run inside a batch")
        if not isinstance(kind, str) or kind not in _HANDLERS:
            raise _reject("unsupported_command", f"{where}: unsupported command type {kind!r}")
        if not isinstance(operation["payload"], dict):
            raise _reject("unsupported_payload", f"{where}: payload must be an object")
    return operations


def _in_operation(index, kind, diagnostics):
    return [dict(d, message=f"operations[{index}] ({kind}): {d['message']}") for d in diagnostics]


# Handler warnings that describe a presentation record's state: (code, section, map, resolved status, phrase).
_STATE_EFFECTS = {
    "route_invalidated": ("physical", "routes", "current", "is {status}: {reason}"),
    "diagram_edge_invalidated": ("pid", "edges", "current", "is {status}: {reason}"),
    "placement_unresolved": ("physical", "placements", "placed", "has no physical placement: {reason}"),
}


def _without_state(record):
    return {key: value for key, value in record.items() if key not in ("status", "reason")}


def _reconcile_batch(project, start):
    """Restore paths this batch invalidated whose unchanged stored points validate again at the end.

    Only a route or diagram edge that was ``current`` before the batch, whose
    connection and stored record (points, mode, assumption, symbols, manual
    flag) are unchanged apart from status and reason, and whose points fit the
    final port frames or anchors is restored to its prior record. Paths that
    were already pending, or that an operation edited, are left as they are.
    """
    connections_before = start["model"]["connections"]
    connections = project["model"]["connections"]
    for cid, before in start["physical"]["routes"].items():
        after = project["physical"]["routes"].get(cid)
        if (before.get("status") != "current" or after is None or after.get("status") == "current"
                or _without_state(after) != _without_state(before) or cid not in connections
                or not _same(connections[cid], connections_before.get(cid))):
            continue
        if not _route_problems(project, cid, after["points_m"]):
            project["physical"]["routes"][cid] = copy.deepcopy(before)
    for edge_id, before in start["pid"]["edges"].items():
        after = project["pid"]["edges"].get(edge_id)
        cid = before.get("connection_id")
        if (before.get("status") != "current" or after is None or after.get("status") == "current"
                or _without_state(after) != _without_state(before) or cid not in connections
                or not _same(connections[cid], connections_before.get(cid))):
            continue
        if not _edge_problems(project, after, after["points"]):
            project["pid"]["edges"][edge_id] = copy.deepcopy(before)


def _committed_effects(project, effects):
    """Batch warnings that still describe the final project; intermediate states never committed are dropped.

    A state warning survives only if its record is still unresolved with the
    same status and reason it reports, and only its last occurrence is kept.
    Other warnings are kept. Final validation warnings are reported separately.
    """
    kept, last = [], {}
    for diagnostic in effects:
        spec = _STATE_EFFECTS.get(diagnostic["code"])
        if spec is None:
            kept.append(diagnostic)
            continue
        section, collection, resolved, phrase = spec
        subject = diagnostic["entity_ids"][0] if diagnostic["entity_ids"] else None
        record = project[section][collection].get(subject)
        if record is None or record.get("status") == resolved:
            continue
        if phrase.format(status=record.get("status"), reason=record.get("reason")) not in diagnostic["message"]:
            continue
        last[(diagnostic["code"], subject)] = len(kept)
        kept.append(diagnostic)
    return [d for index, d in enumerate(kept) if _STATE_EFFECTS.get(d["code"]) is None
            or last[(d["code"], d["entity_ids"][0])] == index]


def _apply_batch(project, payload):
    """Run every operation on ``project`` in order; return ``(added_assumptions, warnings)``."""
    added, effects = {}, []
    for index, operation in enumerate(_batch_operations(payload)):
        kind = operation["type"]
        try:
            fresh = _add_assumptions(project, operation["assumptions"]) if "assumptions" in operation else []
            added.update((aid, index) for aid in fresh)
            effects += _in_operation(index, kind, _HANDLERS[kind](project, operation["payload"]))
            _require_fresh_assumption(kind, operation["payload"], fresh)
        except CommandError as exc:
            raise CommandError(_in_operation(index, kind, exc.diagnostics)) from None
        except (ArithmeticError, LookupError, TypeError, ValueError) as exc:
            # An earlier operation can leave an intermediate state a handler does not expect: reject, never commit.
            raise _reject("operation_failed", f"operations[{index}] ({kind}) could not be applied after the "
                          f"preceding operations: {type(exc).__name__}: {exc}") from None
    return added, effects


def apply_command(document: dict, command: dict) -> tuple[dict, list[dict]]:
    """Apply one validated command; return ``(new_document, warning_diagnostics)``.

    Raises ``CommandError`` for invalid input documents, stale revisions,
    unsupported commands/payloads, and any result that fails validation.
    No-op commands return an unchanged copy with the same revision and history.
    A ``batch`` applies its operations to one working copy and commits only
    the validated final result, as one revision and one undo snapshot.
    """
    _check_envelope(command)
    command = _json_copy(command, "command")
    if not isinstance(document, dict):
        raise _reject("invalid_document", "document must be an object")
    errors = _errors(validate_document(document))
    if errors:
        raise CommandError([_diagnostic("invalid_document", "input document does not validate")] + errors)
    current = document["current"]
    if command["expected_revision"] != current["revision"]:
        raise _reject("stale_revision", f"command expects revision {command['expected_revision']} but the "
                      f"document is at revision {current['revision']}", [current["id"]])

    work = copy.deepcopy(document)
    project = work["current"]
    kind, payload = command["type"], command["payload"]
    if kind in ("undo", "redo"):
        _fields(payload, ())
        if "assumptions" in command:
            raise _reject("unsupported_payload", f"{kind} cannot add assumptions")
        source, target = (work["undo"], work["redo"]) if kind == "undo" else (work["redo"], work["undo"])
        if not source:
            raise _reject("history_empty", f"nothing to {kind}", [project["id"]])
        restored = source.pop()
        target.append(_snapshot(project))
        _restore(project, restored)
        effects = []
    else:
        if kind == "batch":
            origins, effects = _apply_batch(project, payload)
            _reconcile_batch(project, current)
            _screen_batch_routes(project, payload["operations"])
            added = sorted(origins)
        else:
            added = _add_assumptions(project, command["assumptions"]) if "assumptions" in command else []
            effects = _HANDLERS[kind](project, payload)
            _require_fresh_assumption(kind, payload, added)
        unreferenced = sorted(set(added) - _assumption_references(
            {key: project[key] for key in _SNAPSHOT_FIELDS}, set()))
        if unreferenced:
            where = "" if kind != "batch" else " by the final batch result (added by operations " + ", ".join(
                str(index) for index in sorted({origins[aid] for aid in unreferenced})) + ")"
            raise _reject("unreferenced_assumption", f"added assumptions are not referenced{where}: "
                          f"{', '.join(unreferenced)}", unreferenced)
        if _same(_snapshot(project), _snapshot(current)):
            unchanged = copy.deepcopy(document)
            notice = _diagnostic("no_op", f"{kind} changed nothing; revision {current['revision']} retained",
                                 [current["id"]], "warning")
            return unchanged, [notice] + _warnings(validate_project(unchanged["current"]))
        work["undo"].append(_snapshot(current))
        work["redo"] = []
    project["revision"] = current["revision"] + 1

    errors = _errors(validate_document(work))
    if errors:
        raise CommandError(errors)
    if kind == "batch":
        effects = _committed_effects(project, effects)
    return work, effects + _warnings(validate_project(project))
