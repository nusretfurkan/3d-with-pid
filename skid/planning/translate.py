"""Translate a schema-checked plan into existing core operations and ONE ``batch`` envelope.

The planner names only existing stable IDs, new entity IDs and its own
assumption keys. This module resolves every reference against the revision
the plan was made for (tracking the records earlier operations in the same
plan create), derives the IDs of inserted records, converts quantities to
metres, attaches each newly declared assumption to the first operation that
references it (with a service-written source), resolves composed-equipment
definitions through the pure ``skid.equipment`` validator and builds complete
``insert_valve``/``insert_equipment`` payloads from the item's template.
It never mutates the project. ``apply_command`` remains the only mutation
path: the envelope is dry-run and later committed through it unchanged.

``add_equipment`` builds the core ``add_equipment`` payload (entity, every
declared port, symbol, placement) for a built-in kind or a composed
definition; every new required port carries the explicit open marker
``connection_status: "unresolved"`` until a ``connect_ports`` connects it. ``connect_ports`` builds the core ``connect_ports`` payload
(connection, diagram edge, physical route): the edge gets the core's guided
orthogonal path between the two symbol anchors; the route is either the
planner's explicit points (a manual route), ``none`` (an explicitly
unresolved route with a reason) or ``guided``. Guided routes are computed
after translation by ``skid.interactive.rerouting.plan_reroute`` on the
dry-run result of the whole batch, so they fit the final frames and are
screened against every placed body of the final result; a refusal, or a later
operation in the same plan that moves an endpoint, leaves the route
explicitly unresolved with the reason (never invented points). Ports join
only by the stable IDs the plan names, never by proximity.

With a verified research record, each recorded assumption's source names the
research identity and the URLs of the brief sources it cites (``source_ids``);
if no recorded assumption cites one, the first lists every brief source, so
the URLs persist in the model.
"""

from __future__ import annotations

import copy

from ..core import CommandError, apply_command
from ..core.validation import SCHEMA_VERSION
from ..templates import diagram_port_position, get_template
from . import evidence
from .bridge import MODEL
from .context import COMPOSED_KIND, equipment_api, template_for
from .schema import ADDABLE_KINDS, strict_json
from .vocabulary import port_id_suffix

__all__ = ["TranslationError", "batch_envelope", "translate_plan"]

DIAGRAM_TOLERANCE = 1e-6
MAX_PROBLEMS = 20
OPEN_PORT = "unresolved"  # core port marker: a deliberately open (not yet connected) port
_FRAME_OPS = ("move_equipment", "place_equipment", "set_parameter")


class TranslationError(Exception):
    def __init__(self, diagnostics):
        super().__init__(f"{len(diagnostics)} plan problem(s)")
        self.diagnostics = diagnostics


def _diagnostic(code, message, ids=()):
    return {"code": code, "severity": "error", "entity_ids": [str(i) for i in ids if i], "message": message}


class _Reject(Exception):
    def __init__(self, code, message, ids=()):
        super().__init__(message)
        self.diagnostic = _diagnostic(code, message, ids)


def guided_path(start, end):
    """The core's deterministic guided P&ID path (straight if aligned, else a Z through mid-x); core re-validates."""
    (x1, y1), (x2, y2) = start, end
    if abs(x1 - x2) <= DIAGRAM_TOLERANCE or abs(y1 - y2) <= DIAGRAM_TOLERANCE:
        return [[x1, y1], [x2, y2]]
    mid = (x1 + x2) / 2
    return [[x1, y1], [mid, y1], [mid, y2], [x2, y2]]


def _metres(value, unit):
    return round(value * (0.001 if unit == "mm" else 1.0), 12) + 0.0


def _floats(values):
    return [float(v) + 0.0 for v in values]


class _Scope:
    """Lookup state of the revision plus records created by earlier operations of the same plan."""

    def __init__(self, project, proposal_id, assumptions, examples, research=None):
        model, pid, physical = project["model"], project["pid"], project["physical"]
        self.project = project
        self.proposal_id = proposal_id
        self.examples = examples
        self.research = research
        self.guided = []  # (operation index, connection ID) whose physical route is computed after translation
        self.ids = set()
        for collection in (model["entities"], model["ports"], model["connections"], model["assumptions"],
                           pid["symbols"], pid["edges"]):
            self.ids.update(collection)
        self.existing_assumptions = set(model["assumptions"])
        self.entities = {eid: {"record": e, "placed": (physical["placements"].get(eid) or {}).get("status") == "placed"}
                         for eid, e in model["entities"].items()}
        self.ports = {pid_: dict(p) for pid_, p in model["ports"].items()}
        self.connections = {cid: dict(c) for cid, c in model["connections"].items()}
        self.symbols = {sid: dict(s) for sid, s in pid["symbols"].items()}
        self.edges = {eid: dict(e) for eid, e in pid["edges"].items()}
        # Plan assumption keys -> global IDs and records; attached once, to the first referencing operation.
        self.declared = {}
        known_sources = {s["id"] for s in research["brief"]["sources"]} if research else set()
        for item in assumptions:
            key = item["key"]
            if key in self.declared:
                raise _Reject("plan_invalid_assumption", f"assumption key {key!r} is declared twice")
            if key in self.existing_assumptions:
                raise _Reject("plan_invalid_assumption", f"assumption key {key!r} equals an existing assumption ID; "
                              "declare a distinct key", [key])
            global_id = f"asm-{proposal_id}-{key}"
            if global_id in self.ids:
                raise _Reject("plan_invalid_assumption", f"assumption ID {global_id} already exists", [global_id])
            cited = list(item["source_ids"])
            if cited and research is None:
                raise _Reject("plan_unknown_source", f"assumption {key!r} cites research sources, but this plan has "
                              "no research brief", [key])
            unknown = sorted({sid for sid in cited if sid not in known_sources})
            if unknown:
                raise _Reject("plan_unknown_source", f"assumption {key!r} cites {', '.join(unknown)}, which the "
                              f"research brief does not contain", [key])
            if len(set(cited)) != len(cited):
                raise _Reject("plan_invalid_assumption", f"assumption {key!r} cites a source twice", [key])
            self.declared[key] = {"id": global_id, "description": item["description"], "attached": False,
                                  "source_ids": cited}
        self.pending = {}

    def source_text(self, entry, fallback_all=False):
        base = (f"engineering plan {self.proposal_id} ({MODEL} through the local Claude Code CLI); proposed value, "
                "reviewed before apply; not a vendor or design value")
        if self.research is None:
            return base
        return f"{base}. {evidence.provenance(self.research, entry['source_ids'], fallback_all)}"

    def assumption(self, ref):
        if ref in self.declared:
            entry = self.declared[ref]
            if not entry["attached"]:
                entry["attached"] = True
                self.pending[entry["id"]] = {"description": entry["description"], "source": self.source_text(entry)}
            return entry["id"]
        if ref in self.existing_assumptions:
            return ref
        raise _Reject("plan_unknown_assumption", f"assumption {ref[:80]!r} is neither declared in the plan nor an "
                      "existing assumption ID", [ref])

    def take_assumptions(self):
        taken, self.pending = self.pending, {}
        return taken

    def entity(self, entity_id):
        entry = self.entities.get(entity_id)
        if entry is None:
            raise _Reject("plan_unknown_reference", f"entity {entity_id!r} does not exist", [entity_id])
        return entry

    def lookup(self, table, record_id, what):
        record = table.get(record_id)
        if record is None:
            raise _Reject("plan_unknown_reference", f"{what} {record_id!r} does not exist", [record_id])
        return record

    def probe(self):
        """A read-only project view including records created so far (for diagram anchor lookups only)."""
        project = self.project
        return {"id": project["id"], "revision": project["revision"],
                "model": dict(project["model"], entities={k: v["record"] for k, v in self.entities.items()},
                              ports=self.ports, connections=self.connections),
                "pid": dict(project["pid"], symbols=self.symbols, edges=self.edges),
                "physical": project["physical"]}

    def new_ids(self, ids):
        """Refuse new record IDs that exist (or repeat each other); then reserve them."""
        taken = sorted({i for i in ids if i in self.ids} | {i for i in ids if ids.count(i) > 1})
        if taken:
            raise _Reject("plan_duplicate_id", f"IDs {', '.join(taken)} already exist", taken)

    def port_user(self, port_id):
        """The connection (in the revision or created earlier in the plan) that already uses ``port_id``."""
        for connection_id, connection in sorted(self.connections.items()):
            if port_id in (connection["from_port_id"], connection["to_port_id"]):
                return connection_id
        return None


def _parameters(scope, entries, declarations, entity_id):
    names = [entry["name"] for entry in entries]
    duplicate = sorted({n for n in names if names.count(n) > 1})
    if duplicate:
        raise _Reject("plan_invalid_operation", f"parameters {', '.join(duplicate)} are given twice", [entity_id])
    missing = sorted(set(declarations) - set(names))
    extra = sorted(set(names) - set(declarations))
    if missing or extra:
        raise _Reject("plan_invalid_operation", f"parameters must be exactly the declared {sorted(declarations)}"
                      f" (missing {missing}, undeclared {extra})", [entity_id])
    quantities = {}
    for entry in entries:
        declaration = declarations[entry["name"]]
        if declaration.get("unit") != "m":
            raise _Reject("plan_invalid_operation", f"parameter {entry['name']} is declared in "
                          f"{declaration.get('unit')!r}; only metre parameters are supported", [entity_id])
        quantities[entry["name"]] = {"value": _metres(entry["value"], entry["unit"]), "unit": "m",
                                     "assumption_id": scope.assumption(entry["assumption"])}
    return dict(sorted(quantities.items()))


def _definition(scope, args, equipment, op="insert_equipment"):
    ref, text = args["definition_ref"].strip(), args["definition_json"].strip()
    if bool(ref) == bool(text):
        raise _Reject("plan_invalid_operation", f"{op} needs exactly one of definition_ref or definition_json")
    if text:
        try:
            definition = strict_json(text)
        except ValueError as exc:
            raise _Reject("definition_invalid", f"definition_json is not strict JSON ({str(exc)[:160]})") from None
        origin = "inline definition"
    elif ref.startswith("example:"):
        name = ref[len("example:"):]
        if name not in scope.examples:
            raise _Reject("definition_invalid", f"no example definition {name[:60]!r}; available: "
                          f"{', '.join(sorted(scope.examples)) or 'none'}")
        definition = copy.deepcopy(scope.examples[name])
        origin = f"example {name}"
    elif ref.startswith("entity:"):
        source_id = ref[len("entity:"):]
        record = scope.entity(source_id)["record"]
        if record.get("kind") != COMPOSED_KIND or not isinstance(record.get("definition"), dict):
            raise _Reject("definition_invalid", f"{source_id} is not a composed item with a definition", [source_id])
        definition = copy.deepcopy(record["definition"])
        origin = f"copy of {source_id}"
    else:
        raise _Reject("definition_invalid", "definition_ref must be 'example:<name>' or 'entity:<entity id>'")
    if not isinstance(definition, dict):
        raise _Reject("definition_invalid", "the definition must be a JSON object")
    try:
        problems = [d for d in equipment.validate_definition(definition) if d.get("severity") != "warning"]
    except Exception as exc:  # noqa: BLE001 - a validator fault rejects the definition, it never passes it
        raise _Reject("definition_invalid", f"definition validation failed ({type(exc).__name__}: "
                      f"{str(exc)[:160]})") from None
    if problems:
        detail = "; ".join(f"[{d.get('code')}] {str(d.get('message'))[:160]}" for d in problems[:6])
        raise _Reject("definition_invalid", f"the {origin} is rejected by definition validation: {detail}")
    return definition, origin


def _insert(scope, op, args):
    entity_id = args["entity_id"]
    stem = entity_id[len("ent-"):]
    derived = {"inlet": f"port-{stem}-inlet", "outlet": f"port-{stem}-outlet", "connection": f"conn-{stem}-down",
               "symbol": f"sym-{stem}", "edge": f"edge-{stem}-down"}
    taken = sorted(i for i in [entity_id, *derived.values()] if i in scope.ids)
    if taken:
        raise _Reject("plan_duplicate_id", f"IDs {', '.join(taken)} already exist", taken)
    upstream = scope.lookup(scope.connections, args["connection_id"], "connection")
    if op == "insert_valve":
        entity = {"id": entity_id, "kind": args["kind"], "tag": args["tag"], "template_version": 1}
        template = get_template(args["kind"], 1)
        origin = None
    else:
        equipment, reason = equipment_api()
        if equipment is None:
            raise _Reject("composition_unavailable", f"composed equipment cannot be inserted: {reason}")
        definition, origin = _definition(scope, args, equipment)
        entity = {"id": entity_id, "kind": COMPOSED_KIND, "tag": args["tag"], "template_version": 1,
                  "definition": definition}
        try:
            template = equipment.entity_template(dict(entity, parameters={}))
        except Exception as exc:  # noqa: BLE001 - DefinitionError or any lookup fault rejects the item
            raise _Reject("definition_invalid", f"the definition has no usable template "
                          f"({type(exc).__name__}: {str(exc)[:160]})", [entity_id]) from None
    ports = template.get("ports") or {}
    directions = {name: (spec or {}).get("direction") for name, spec in ports.items()}
    if directions != {"inlet": "in", "outlet": "out"}:
        raise _Reject("plan_invalid_operation", f"{op} needs a two-port inline item with inlet (in) and outlet "
                      f"(out); this item declares {directions}", [entity_id])
    entity["parameters"] = _parameters(scope, args["parameters"], template.get("parameters") or {}, entity_id)
    entity = {key: entity[key] for key in ("id", "kind", "tag", "template_version", "parameters", "definition")
              if key in entity}

    destination = upstream["to_port_id"]
    port_assumption = scope.lookup(scope.ports, destination, "port")["assumption_id"]
    port_records = []
    for name in ("inlet", "outlet"):
        spec = ports[name]
        if "nominal_diameter_mm" not in spec:
            raise _Reject("plan_invalid_operation", f"port {name} declares no nominal diameter", [entity_id])
        port_records.append({"id": derived[name], "entity_id": entity_id, "name": name, "direction": spec["direction"],
                             "required": bool(spec.get("required", True)),
                             "nominal_diameter_mm": spec["nominal_diameter_mm"],
                             "service": spec.get("service", "liquid"), "assumption_id": port_assumption})
    downstream = {"id": derived["connection"], "tag": args["downstream_tag"], "from_port_id": derived["outlet"],
                  "to_port_id": destination, "nominal_diameter_mm": upstream["nominal_diameter_mm"],
                  "outside_diameter_m": upstream["outside_diameter_m"], "assumption_id": upstream["assumption_id"]}
    edges = sorted(eid for eid, edge in scope.edges.items() if edge["connection_id"] == args["connection_id"])
    if len(edges) != 1:
        raise _Reject("plan_invalid_operation", f"connection {args['connection_id']} has {len(edges)} diagram edges; "
                      "inline insertion needs exactly one", [args["connection_id"], *edges])
    up_edge = scope.edges[edges[0]]
    symbol_args = args["symbol"]
    symbol = {"id": derived["symbol"], "entity_id": entity_id, "x": float(symbol_args["x"]) + 0.0,
              "y": float(symbol_args["y"]) + 0.0, "rotation_deg": float(symbol_args["rotation_deg"]) + 0.0,
              "manual": True}

    requested = args["placement"]
    placed = requested["status"] == "placed"
    placement = None
    if placed:
        if len(requested["position_m"]) != 3 or len(requested["rotation_deg"]) != 3 or not requested["assumption"]:
            raise _Reject("plan_invalid_operation", "a placed insertion needs position_m [x, y, z], rotation_deg "
                          "[rx, ry, rz] and an assumption", [entity_id])
        placement = {"position_m": _floats(requested["position_m"]), "rotation_deg": _floats(requested["rotation_deg"]),
                     "status": "placed", "manual": True, "assumption_id": scope.assumption(requested["assumption"])}
    elif requested["position_m"] or requested["rotation_deg"]:
        raise _Reject("plan_invalid_operation", "an unresolved placement must have empty position_m and "
                      "rotation_deg", [entity_id])

    # Lookup-only view with the new records, to give the downstream edge the core's guided path.
    scope.entities[entity_id] = {"record": entity, "placed": False}
    for record in port_records:
        scope.ports[record["id"]] = record
    scope.symbols[symbol["id"]] = symbol
    probe = scope.probe()
    try:
        start = diagram_port_position(probe, derived["outlet"], symbol["id"])
        end = diagram_port_position(probe, destination, up_edge["to_symbol_id"])
        down_edge = {"id": derived["edge"], "connection_id": derived["connection"], "from_symbol_id": symbol["id"],
                     "to_symbol_id": up_edge["to_symbol_id"],
                     "points": guided_path(_floats(start[:2]), _floats(end[:2])), "manual": False, "status": "current"}
    except Exception:  # noqa: BLE001 - an unresolved anchor leaves the edge explicitly pending, never guessed
        down_edge = {"id": derived["edge"], "connection_id": derived["connection"], "from_symbol_id": symbol["id"],
                     "to_symbol_id": up_edge["to_symbol_id"], "points": [], "manual": False, "status": "unresolved",
                     "reason": f"diagram anchors of {entity_id} could not be resolved when it was planned"}

    payload = {"connection_id": args["connection_id"], "entity": entity, "ports": port_records,
               "downstream_connection": downstream, "symbol": symbol, "downstream_edge": down_edge}
    if placement is not None:
        payload["placement"] = placement

    # Bookkeeping for later operations: the upstream line now ends at the item; the new line continues.
    scope.entities[entity_id]["placed"] = placed
    scope.ids.update([entity_id, *derived.values()])
    scope.connections[args["connection_id"]] = dict(upstream, to_port_id=derived["inlet"])
    scope.connections[derived["connection"]] = downstream
    scope.edges[edges[0]] = dict(up_edge, to_symbol_id=symbol["id"])
    scope.edges[derived["edge"]] = down_edge
    ids = [entity_id, *derived.values(), args["connection_id"]]
    note = f" from the {origin}" if origin else ""
    summary = (f"Insert {args['tag']} ({entity_id}, {entity['kind']}{note}) into {upstream['tag']} "
               f"({args['connection_id']}); new line {args['downstream_tag']} ({derived['connection']}); "
               f"{'placed' if placed else 'physical placement left unresolved'}.")
    return op, payload, ids, summary


def _placement_record(scope, requested, entity_id, tag, fallback_assumption):
    """A core placement record: placed (three numbers each, cited) or explicitly unresolved with a reason."""
    if requested["status"] == "placed":
        if len(requested["position_m"]) != 3 or len(requested["rotation_deg"]) != 3 or not requested["assumption"]:
            raise _Reject("plan_invalid_operation", "a placed item needs position_m [x, y, z], rotation_deg "
                          "[rx, ry, rz] and an assumption", [entity_id])
        return {"position_m": _floats(requested["position_m"]), "rotation_deg": _floats(requested["rotation_deg"]),
                "status": "placed", "manual": True, "assumption_id": scope.assumption(requested["assumption"])}
    if requested["position_m"] or requested["rotation_deg"]:
        raise _Reject("plan_invalid_operation", "an unresolved placement must have empty position_m and "
                      "rotation_deg", [entity_id])
    assumption = scope.assumption(requested["assumption"]) if requested["assumption"] else fallback_assumption
    return {"position_m": None, "rotation_deg": None, "status": "unresolved", "manual": False,
            "assumption_id": assumption,
            "reason": f"engineering plan {scope.proposal_id} proposed no physical position for {tag}"}


def _add(scope, args):
    """``add_equipment``: one new built-in or composed item with every declared port, its symbol and placement."""
    entity_id, kind, tag = args["entity_id"], args["kind"], args["tag"]
    stem = entity_id[len("ent-"):]
    if kind == COMPOSED_KIND:
        equipment, reason = equipment_api()
        if equipment is None:
            raise _Reject("composition_unavailable", f"composed equipment cannot be added: {reason}")
        definition, origin = _definition(scope, args, equipment, "add_equipment")
        entity = {"id": entity_id, "kind": COMPOSED_KIND, "tag": tag, "template_version": 1, "definition": definition}
        try:
            template = equipment.entity_template(dict(entity, parameters={}))
        except Exception as exc:  # noqa: BLE001 - DefinitionError or any lookup fault rejects the item
            raise _Reject("definition_invalid", f"the definition has no usable template "
                          f"({type(exc).__name__}: {str(exc)[:160]})", [entity_id]) from None
    else:
        if kind not in ADDABLE_KINDS:
            raise _Reject("plan_invalid_operation", f"kind {kind!r} cannot be added", [entity_id])
        if args["definition_ref"].strip() or args["definition_json"].strip():
            raise _Reject("plan_invalid_operation", f"a built-in {kind} takes no definition; leave definition_ref "
                          "and definition_json empty", [entity_id])
        entity = {"id": entity_id, "kind": kind, "tag": tag, "template_version": 1}
        template = get_template(kind, 1)
        origin = None
    ports = template.get("ports") or {}
    if not ports:
        raise _Reject("plan_invalid_operation", f"{kind} declares no process ports; only ported equipment can be "
                      "added", [entity_id])
    derived = {name: f"port-{stem}-{port_id_suffix(name)}" for name in sorted(ports)}
    symbol_id = f"sym-{stem}"
    scope.new_ids([entity_id, symbol_id, *derived.values()])
    entity["parameters"] = _parameters(scope, args["parameters"], template.get("parameters") or {}, entity_id)
    entity = {key: entity[key] for key in ("id", "kind", "tag", "template_version", "parameters", "definition")
              if key in entity}
    port_assumption = scope.assumption(args["port_assumption"])
    port_records = []
    for name in sorted(ports):
        spec = ports[name] or {}
        if spec.get("direction") not in ("in", "out", "both") or "nominal_diameter_mm" not in spec:
            raise _Reject("plan_invalid_operation", f"port {name} declares no direction or nominal diameter",
                          [entity_id])
        record = {"id": derived[name], "entity_id": entity_id, "name": name, "direction": spec["direction"],
                  "required": bool(spec.get("required", True)), "nominal_diameter_mm": spec["nominal_diameter_mm"],
                  "service": spec.get("service", "liquid"), "assumption_id": port_assumption}
        if record["required"]:  # the core's explicit open marker exists only for required ports
            record["connection_status"] = OPEN_PORT
        port_records.append(record)
    symbol_args = args["symbol"]
    symbol = {"id": symbol_id, "entity_id": entity_id, "x": float(symbol_args["x"]) + 0.0,
              "y": float(symbol_args["y"]) + 0.0, "rotation_deg": float(symbol_args["rotation_deg"]) + 0.0,
              "manual": True}
    placement = _placement_record(scope, args["placement"], entity_id, tag, port_assumption)
    placed = placement["status"] == "placed"
    payload = {"entity": entity, "ports": port_records, "symbol": symbol, "placement": placement}

    scope.entities[entity_id] = {"record": entity, "placed": placed}
    for record in port_records:
        scope.ports[record["id"]] = record
    scope.symbols[symbol_id] = symbol
    scope.ids.update([entity_id, symbol_id, *derived.values()])
    note = f" from the {origin}" if origin else ""
    names = ", ".join(f"{name} ({derived[name]}, {ports[name]['direction']})" for name in sorted(ports))
    summary = (f"Add {tag} ({entity_id}, {entity['kind']}{note}) with ports {names}; P&ID symbol {symbol_id}; "
               f"{'placed at ' + str(placement['position_m']) + ' m' if placed else 'physical placement unresolved'}"
               "; its ports stay open until connected.")
    return "add_equipment", payload, [entity_id, symbol_id, *derived.values()], summary


def _connect(scope, args, index):
    """``connect_ports``: one new line between two named ports with its guided P&ID edge and a physical route."""
    connection_id = args["connection_id"]
    stem = connection_id[len("conn-"):]
    edge_id = f"edge-{stem}"
    scope.new_ids([connection_id, edge_id])
    ends = {}
    for role in ("from", "to"):
        port_id, symbol_id = args[f"{role}_port_id"], args[f"{role}_symbol_id"]
        port = scope.lookup(scope.ports, port_id, f"{role} port")
        symbol = scope.lookup(scope.symbols, symbol_id, f"{role} symbol")
        owner = scope.entities.get(port["entity_id"], {}).get("record", {})
        if symbol["entity_id"] != port["entity_id"]:
            raise _Reject("plan_invalid_operation", f"{role} symbol {symbol_id} shows {symbol['entity_id']}, not "
                          f"{port['entity_id']} which owns {port_id}", [symbol_id, port_id])
        allowed = ("out", "both") if role == "from" else ("in", "both")
        if port["direction"] not in allowed:
            raise _Reject("plan_invalid_operation", f"{port_id} ({owner.get('tag')} {port['name']}) is an "
                          f"'{port['direction']}' port; a line {'starts at an out' if role == 'from' else 'ends at an in'}"
                          " port (flow runs from_port -> to_port)", [port_id])
        user = scope.port_user(port_id)
        if user is not None:
            raise _Reject("plan_invalid_operation", f"{port_id} ({owner.get('tag')} {port['name']}) is already "
                          f"connected by {user}; a port joins one line (no branches)", [port_id, user])
        ends[role] = (port_id, port, symbol_id, owner)
    (from_id, from_port, from_symbol, from_owner), (to_id, to_port, to_symbol, to_owner) = ends["from"], ends["to"]
    if from_id == to_id or from_port["entity_id"] == to_port["entity_id"]:
        raise _Reject("plan_invalid_operation", "a line joins ports of two different items", [from_id, to_id])
    if from_port["nominal_diameter_mm"] != to_port["nominal_diameter_mm"]:
        raise _Reject("plan_invalid_operation", f"{from_id} is DN{from_port['nominal_diameter_mm']} but {to_id} is "
                      f"DN{to_port['nominal_diameter_mm']}", [from_id, to_id])
    if from_port.get("service") != to_port.get("service"):
        raise _Reject("plan_invalid_operation", f"{from_id} ({from_port.get('service')}) and {to_id} "
                      f"({to_port.get('service')}) are different services", [from_id, to_id])
    assumption = scope.assumption(args["assumption"])
    connection = {"id": connection_id, "tag": args["tag"], "from_port_id": from_id, "to_port_id": to_id,
                  "nominal_diameter_mm": from_port["nominal_diameter_mm"],
                  "outside_diameter_m": round(float(args["outside_diameter_mm"]) / 1000.0, 9) + 0.0,
                  "assumption_id": assumption}
    probe = scope.probe()
    try:
        start = diagram_port_position(probe, from_id, from_symbol)
        end = diagram_port_position(probe, to_id, to_symbol)
        edge = {"id": edge_id, "connection_id": connection_id, "from_symbol_id": from_symbol,
                "to_symbol_id": to_symbol, "points": guided_path(_floats(start[:2]), _floats(end[:2])),
                "manual": False, "status": "current"}
    except Exception:  # noqa: BLE001 - an unresolved anchor leaves the edge explicitly pending, never guessed
        edge = {"id": edge_id, "connection_id": connection_id, "from_symbol_id": from_symbol,
                "to_symbol_id": to_symbol, "points": [], "manual": False, "status": "unresolved",
                "reason": f"diagram anchors of {connection_id} could not be resolved when it was planned"}

    request = args["route"]
    mode = request["mode"]
    if mode == "points":
        if len(request["points_m"]) < 2 or not request["assumption"]:
            raise _Reject("plan_invalid_operation", "route mode 'points' needs at least two points_m and an "
                          "assumption", [connection_id])
        route = {"points_m": [_floats(p) for p in request["points_m"]], "mode": "manual", "status": "current",
                 "assumption_id": scope.assumption(request["assumption"])}
        described = f"explicit route ({len(route['points_m'])} points)"
    else:
        if request["points_m"]:
            raise _Reject("plan_invalid_operation", f"route mode {mode!r} takes no points_m", [connection_id])
        if mode == "guided":
            reason = f"guided route requested by engineering plan {scope.proposal_id}; not yet computed"
            scope.guided.append((index, connection_id))
            described = "guided physical route (computed on the checked batch, or left pending with the reason)"
        else:
            reason = (f"engineering plan {scope.proposal_id} proposed no physical route; place both ends and use the "
                      "guided Reroute or enter a route")
            described = "physical route left unresolved"
        route = {"points_m": [], "mode": "guided", "status": "unresolved", "assumption_id": assumption,
                 "reason": reason}
    payload = {"connection": connection, "edge": edge, "route": route}

    scope.connections[connection_id] = connection
    scope.edges[edge_id] = edge
    scope.ids.update([connection_id, edge_id])
    for port_id in (from_id, to_id):
        scope.ports[port_id] = {k: v for k, v in scope.ports[port_id].items() if k != "connection_status"}
    summary = (f"Connect {from_owner.get('tag')} {from_port['name']} ({from_id}) -> {to_owner.get('tag')} "
               f"{to_port['name']} ({to_id}) as line {args['tag']} ({connection_id}); P&ID edge {edge_id} "
               f"{'guided' if edge['status'] == 'current' else 'unresolved'}; {described}.")
    return "connect_ports", payload, [connection_id, edge_id, from_id, to_id, from_port["entity_id"],
                                      to_port["entity_id"]], summary


def _translate_one(scope, op, args, index=0):
    if op == "add_equipment":
        return _add(scope, args)
    if op == "connect_ports":
        return _connect(scope, args, index)
    if op == "rename_entity":
        entry = scope.entity(args["entity_id"])
        old = entry["record"]["tag"]
        entry["record"] = dict(entry["record"], tag=args["tag"])
        return op, {"entity_id": args["entity_id"], "tag": args["tag"]}, [args["entity_id"]], \
            f"Rename {old} ({args['entity_id']}) to {args['tag']}."
    if op == "set_parameter":
        entry = scope.entity(args["entity_id"])
        template = template_for(entry["record"])
        declarations = (template or {}).get("parameters") or {}
        declaration = declarations.get(args["name"])
        if declaration is None:
            raise _Reject("plan_invalid_operation", f"{entry['record']['tag']} ({entry['record']['kind']}) declares "
                          f"no parameter {args['name']!r}; declared: {', '.join(sorted(declarations)) or 'none'}",
                          [args["entity_id"]])
        if declaration.get("unit") != "m":
            raise _Reject("plan_invalid_operation", f"parameter {args['name']} is not a metre parameter",
                          [args["entity_id"]])
        value = _metres(args["value"], args["unit"])
        quantity = {"value": value, "unit": "m", "assumption_id": scope.assumption(args["assumption"])}
        return op, {"entity_id": args["entity_id"], "name": args["name"], "quantity": quantity}, \
            [args["entity_id"]], f"Set {entry['record']['tag']} {args['name']} to {value:g} m."
    if op == "move_equipment":
        entry = scope.entity(args["entity_id"])
        kind = "move_equipment" if entry["placed"] else "place_equipment"
        entry["placed"] = True
        placement = {"position_m": _floats(args["position_m"]), "rotation_deg": _floats(args["rotation_deg"]),
                     "status": "placed", "manual": True, "assumption_id": scope.assumption(args["assumption"])}
        return kind, {"entity_id": args["entity_id"], "placement": placement}, [args["entity_id"]], \
            f"Place {entry['record']['tag']} at {placement['position_m']} m, rotation {placement['rotation_deg']} deg."
    if op == "move_symbol":
        symbol = scope.lookup(scope.symbols, args["symbol_id"], "symbol")
        payload = {"symbol_id": args["symbol_id"], "x": float(args["x"]) + 0.0, "y": float(args["y"]) + 0.0,
                   "rotation_deg": float(args["rotation_deg"]) + 0.0}
        scope.symbols[args["symbol_id"]] = dict(symbol, x=payload["x"], y=payload["y"],
                                                rotation_deg=payload["rotation_deg"])
        return op, payload, [args["symbol_id"], symbol["entity_id"]], \
            f"Move P&ID symbol {args['symbol_id']} to ({payload['x']:g}, {payload['y']:g})."
    if op == "set_route":
        connection = scope.lookup(scope.connections, args["connection_id"], "connection")
        payload = {"connection_id": args["connection_id"], "points_m": [_floats(p) for p in args["points_m"]],
                   "mode": "manual", "assumption_id": scope.assumption(args["assumption"])}
        return op, payload, [args["connection_id"]], \
            f"Set an explicit physical route for {connection['tag']} ({len(payload['points_m'])} points)."
    if op == "set_diagram_route":
        edge = scope.lookup(scope.edges, args["edge_id"], "diagram edge")
        return op, {"edge_id": args["edge_id"], "points": [_floats(p) for p in args["points"]]}, \
            [args["edge_id"], edge["connection_id"]], f"Set a manual P&ID path for edge {args['edge_id']}."
    return _insert(scope, op, args)


def _cite_brief_sources(scope, operations):
    """With research and no recorded assumption citing a source, the first recorded one lists every source."""
    if scope.research is None:
        return
    recorded = [entry for entry in scope.declared.values() if entry["attached"]]
    if not recorded or any(entry["source_ids"] for entry in recorded):
        return
    for operation in operations:
        for assumption_id, record in (operation.get("assumptions") or {}).items():
            entry = next((e for e in recorded if e["id"] == assumption_id), None)
            if entry is not None:
                record["source"] = scope.source_text(entry, fallback_all=True)
                return


def _dry_run(document, project, operations):
    base = document if document is not None else {"schema_version": SCHEMA_VERSION, "current": project, "undo": [],
                                                  "redo": []}
    envelope = {"id": "cmd-guided-route-probe", "expected_revision": project["revision"], "type": "batch",
                "payload": {"operations": operations}}
    result, _ = apply_command(base, envelope)
    return result["current"]


def _later_changes(operations, index, connection_id, owners):
    """Why a later operation of the batch would make a route computed now stale (``None`` if none does)."""
    for later, operation in enumerate(operations[index + 1:], start=index + 1):
        kind, payload = operation["type"], operation["payload"]
        if kind in _FRAME_OPS and payload.get("entity_id") in owners:
            return f"operation {later + 1} ({kind}) changes {payload['entity_id']} after the connection"
        if kind in ("insert_valve", "insert_equipment") and payload.get("connection_id") == connection_id:
            return f"operation {later + 1} ({kind}) splits {connection_id}"
        if kind == "set_route" and payload.get("connection_id") == connection_id:
            return f"operation {later + 1} sets an explicit route for {connection_id}"
    return None


def _guided_routes(scope, operations, described, document):
    """Compute requested guided routes on the batch's own dry-run result, or leave them unresolved with a reason."""
    if not scope.guided:
        return
    from ..interactive import rerouting  # pure helper (no service import); deferred to keep planning importable

    def pending(index, reason):
        route = operations[index]["payload"]["route"]
        route["reason"] = f"guided route not computed: {reason}"[:600]
        described[index]["route"] = {"mode": "guided", "status": "unresolved", "reason": route["reason"]}
        described[index]["summary"] = described[index]["summary"].replace(
            "guided physical route (computed on the checked batch, or left pending with the reason)",
            "physical route left unresolved (the guided route could not be computed; see route reason)")

    try:
        final = _dry_run(document, scope.project, operations)
    except CommandError:
        for index, _ in scope.guided:  # the batch itself is invalid; the core dry-run reports why
            pending(index, "the batch did not pass the core dry run")
        return
    computed = []
    for index, connection_id in scope.guided:
        connection = final["model"]["connections"].get(connection_id)
        ports = final["model"]["ports"]
        owners = {ports.get(connection["from_port_id"], {}).get("entity_id"),
                  ports.get(connection["to_port_id"], {}).get("entity_id")} if connection else set()
        stale = _later_changes(operations, index, connection_id, owners)
        if stale is not None:
            pending(index, f"{stale}; use the guided Reroute after applying")
            continue
        try:
            plan = rerouting.plan_reroute(final, connection_id)
        except rerouting.RerouteError as exc:
            pending(index, exc.message)
            continue
        except Exception as exc:  # noqa: BLE001 - a router fault leaves the route pending, never invents points
            pending(index, f"the guided router failed ({type(exc).__name__}: {str(exc)[:160]})")
            continue
        assumption_id = f"asm-{scope.proposal_id}-route-{connection_id[len('conn-'):]}"[:80]
        if assumption_id in scope.ids:
            pending(index, f"assumption ID {assumption_id} already exists")
            continue
        command = rerouting.reroute_command(final, plan, "cmd-unused", assumption_id,
                                            f"engineering plan {scope.proposal_id}")
        record = command["assumptions"][assumption_id]
        record["description"] = record["description"].replace(
            f"against revision {final['revision']}", f"for engineering plan {scope.proposal_id} on its checked batch")
        computed.append((index, connection_id, plan, assumption_id, record))
    if not computed:
        return
    trial = copy.deepcopy(operations)
    for index, _, plan, assumption_id, record in computed:
        trial[index]["payload"]["route"] = {"points_m": [list(p) for p in plan["points_m"]], "mode": "guided",
                                            "status": "current", "assumption_id": assumption_id}
        trial[index].setdefault("assumptions", {})[assumption_id] = record
    try:
        _dry_run(document, scope.project, trial)
    except CommandError as exc:
        detail = "; ".join(str(d.get("message"))[:160] for d in exc.diagnostics[:2])
        for index, *_ in computed:
            pending(index, f"the computed route did not pass the final batch check ({detail})")
        return
    for index, connection_id, plan, assumption_id, _ in computed:
        operations[index] = trial[index]
        scope.ids.add(assumption_id)
        described[index]["new_assumptions"] = sorted(set(described[index]["new_assumptions"]) | {assumption_id})
        described[index]["route"] = {"mode": "guided", "status": "current", "points": len(plan["points_m"]),
                                     "bends": plan["bends"], "length_m": plan["length_m"],
                                     "candidates_checked": plan["candidates_checked"]}
        described[index]["summary"] = described[index]["summary"].replace(
            "guided physical route (computed on the checked batch, or left pending with the reason)",
            f"guided physical route computed ({len(plan['points_m'])} points, {plan['bends']} bend(s), "
            f"{plan['length_m']:.3f} m)")


def translate_plan(plan, project, proposal_id, examples, research=None, document=None) -> dict:
    """Checked core operations for a schema-valid plan; raise ``TranslationError`` with diagnostics otherwise.

    ``research`` is a verified research record (``evidence.verify_reply``) or ``None``; ``document`` is the
    source document (used to dry-run the batch when guided routes are requested).
    """
    try:
        scope = _Scope(project, proposal_id, plan["assumptions"], examples, research)
    except _Reject as exc:
        raise TranslationError([exc.diagnostic]) from None
    operations, described, problems = [], [], []
    for index, item in enumerate(plan["operations"]):
        op, args = item["op"], item["args"]
        try:
            kind, payload, ids, summary = _translate_one(scope, op, copy.deepcopy(args), index)
        except _Reject as exc:
            problems.append(dict(exc.diagnostic, message=f"operation {index + 1} ({op}): {exc.diagnostic['message']}"))
            scope.take_assumptions()
            if len(problems) >= MAX_PROBLEMS:
                break
            continue
        operation = {"type": kind, "payload": payload}
        attached = scope.take_assumptions()
        if attached:
            operation["assumptions"] = attached
        operations.append(operation)
        described.append({"index": index, "type": kind, "planner_op": op, "purpose": item["purpose"],
                           "summary": summary, "ids": sorted(set(ids)),
                           "new_assumptions": sorted(attached)})
    if problems:
        raise TranslationError(problems)
    _cite_brief_sources(scope, operations)
    _guided_routes(scope, operations, described, document)
    recorded = {entry["id"] for entry in scope.declared.values() if entry["attached"]}
    sources = {}
    for operation in operations:
        sources.update({aid: record["source"] for aid, record in (operation.get("assumptions") or {}).items()})
    proposed = [{"key": key, "id": entry["id"], "description": entry["description"],
                 "recorded": entry["id"] in recorded, "source_ids": list(entry["source_ids"]),
                 "source": sources.get(entry["id"])} for key, entry in scope.declared.items()]
    return {"operations": operations, "described": described, "proposed_assumptions": proposed}


def batch_envelope(proposal_id, expected_revision, operations) -> dict:
    return {"id": f"cmd-{proposal_id}", "expected_revision": expected_revision, "type": "batch",
            "payload": {"operations": operations}}
