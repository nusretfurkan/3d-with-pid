"""Pure planning of the Blender representation of one project snapshot.

``plan_scene`` validates a project with the core validator and turns it into an
ordered list of object specifications, a one-to-many representation map,
diagnostics, and a status. It never imports ``bpy`` and never mutates its
input, so the geometry contract can be tested without Blender;
``blender_support.scene`` realizes a plan inside Blender.

Identity. Every object has a representation ID ``<owner id>#<component key>``
(component keys never contain ``#``). Owners are entity, connection, or
project IDs, never tags; tags appear only in label and status-board text and
as a ``tag`` property on label objects. Component keys within an entity are ``frame`` (an
empty at the placement), the template primitive keys such as ``body``,
``motor`` and ``nozzle/inlet``, ``label`` and ``label/plate``. Within a
connection they are ``route`` (an empty at the first route point),
``segment/<i>``, ``elbow/<j>`` and ``joint/<j>`` for current piping, or
``stale/segment/<i>`` and ``stale/joint/<j>`` for retained non-current points,
plus ``label`` and ``label/plate``. ``i`` is the route segment index and ``j``
the route point index; they identify representations, not pipe components.
The project owns ``status`` and ``status/plate``.

Geometry. Entity primitives are children of the entity frame with the
template's local ``center_m``/``rotation_deg``; the frame carries the
placement, so Blender composes them exactly as ``world_port_position`` does.
Equipment without a resolved placement is omitted and mapped to an explicit
empty list. A route that is not current is never drawn as piping: retained
points become thin red ghost geometry with a warning label, and a route with
fewer than two points is omitted. Either case makes the status ``pending``.
Labels face -y (front elevation) and read along +x.
"""

from __future__ import annotations

import copy
import math

from skid.core.validation import canonical_digest, validate_project
from skid.equipment import is_composed
from skid.templates import entity_primitives, world_port_position

from . import geometry as geo

__all__ = [
    "ADAPTER",
    "ADAPTER_VERSION",
    "COLLECTION_ROLES",
    "GENERATOR",
    "MANIFEST_FIELDS",
    "MATERIAL_STYLES",
    "diagnostic",
    "manifest_from_plan",
    "object_name",
    "plan_scene",
    "representation_id",
    "text_extent",
]

ADAPTER = "blender"
ADAPTER_VERSION = "0.1.0"
GENERATOR = "skid.adapters.blender"
"""Value of the ``skid_generator`` property on every generated datablock."""

COLLECTION_ROLES = ("equipment", "piping", "annotations")
MANIFEST_FIELDS = (
    "adapter",
    "adapter_version",
    "project_id",
    "source_revision",
    "input_digest",
    "status",
    "diagnostics",
    "representations",
)

# Validator warnings about P&ID presentation do not affect the 3D view; the SVG
# adapter reports them. Any other warning code is treated as physical.
PID_WARNINGS = frozenset({"edge_not_current", "symbol_missing", "edge_missing"})

LABEL_SIZE_M = 0.07
LINE_LABEL_SIZE_M = 0.055
STATUS_SIZE_M = 0.055
LABEL_ROTATION_DEG = (90.0, 0.0, 0.0)  # text +z faces -y; text +y is world +z
LABEL_GAP_M = 0.02
PLATE_OFFSET_M = 0.004
PLATE_THICKNESS_M = 0.002
GHOST_RADIUS_FACTOR = 0.3
GHOST_JOINT_FACTOR = 1.6
MAX_STATUS_ITEMS = 8

# Material role -> (sRGB colour, roughness, metallic, emission strength).
MATERIAL_STYLES = {
    "equipment": ("#A0A8B0", 0.5, 0.1, 0.0),
    "vessel": ("#B9C3CC", 0.45, 0.2, 0.0),
    "pump": ("#2F6DB5", 0.4, 0.1, 0.0),
    "motor": ("#46505C", 0.5, 0.2, 0.0),
    "valve": ("#C0392B", 0.45, 0.1, 0.0),
    "trim": ("#3C4146", 0.5, 0.3, 0.0),
    "handle": ("#F1C40F", 0.5, 0.0, 0.0),
    "boundary": ("#E67E22", 0.6, 0.0, 0.0),
    "indicator": ("#F4F6F7", 0.3, 0.0, 0.0),
    "nozzle": ("#7B8794", 0.4, 0.4, 0.0),
    "pipe": ("#9AA5B1", 0.35, 0.5, 0.0),
    "stale": ("#FF2D2D", 0.6, 0.0, 0.6),
    "label": ("#111820", 0.8, 0.0, 0.0),
    "label_warning": ("#D0021B", 0.8, 0.0, 0.3),
    "plate": ("#FFFFFF", 0.9, 0.0, 0.4),
    "selected": ("#FF8C00", 0.4, 0.0, 0.8),
}

_PRIMITIVE_MATERIALS = {
    ("tank", "body"): "vessel",
    ("centrifugal_pump", "body"): "pump",
    ("centrifugal_pump", "motor"): "motor",
    ("isolation_valve", "body"): "valve",
    ("isolation_valve", "stem"): "trim",
    ("isolation_valve", "handle"): "handle",
    ("check_valve", "body"): "valve",
    ("check_valve", "cover"): "trim",
    ("indicator", "stem"): "trim",
    ("indicator", "body"): "indicator",
    ("boundary", "body"): "boundary",
}

# Approximate advance widths, in ems, of Blender's built-in text font.
_NARROW = set("iljtfrI.,:;'!|")
_WIDE = set("mwMW")


def representation_id(owner_id: str, component_key: str) -> str:
    return f"{owner_id}#{component_key}"


def object_name(owner_id: str, component_key: str) -> str:
    """Preferred Blender name; Blender adds a suffix if a user ID holds it."""
    return f"{owner_id}/{component_key}"


def diagnostic(code: str, severity: str, ids, message: str) -> dict:
    entity_ids = []
    for item in ids:
        if isinstance(item, str) and item and item not in entity_ids:
            entity_ids.append(item)
    return {"code": code, "severity": severity, "entity_ids": entity_ids, "message": message}


def text_extent(body: str, size: float) -> tuple[float, float]:
    """Estimate the width and height of label text, for its backing plate."""

    def advance(char):
        if char == " ":
            return 0.28
        if char in _NARROW:
            return 0.3
        if char in _WIDE:
            return 0.85
        if char in "-()":
            return 0.38
        if char.islower():
            return 0.55
        return 0.66

    lines = body.split("\n")
    width = max(sum(advance(c) for c in line) for line in lines) * size
    height = size * (0.75 + 1.2 * (len(lines) - 1))
    return width, height


def manifest_from_plan(plan: dict, status: str | None = None, diagnostics=()) -> dict:
    """Return the contracted manifest; a failed build maps nothing."""
    status = status or plan["status"]
    return {
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "project_id": plan["project_id"],
        "source_revision": plan["source_revision"],
        "input_digest": plan["input_digest"],
        "status": status,
        "diagnostics": copy.deepcopy(plan["diagnostics"]) + list(diagnostics),
        "representations": {} if status == "failed" else copy.deepcopy(plan["representations"]),
    }


def plan_scene(project: dict, selected_ids=()) -> dict:
    """Plan the Blender objects for ``project``; never raises for invalid input.

    Returns ``{project_id, source_revision, input_digest, status, diagnostics,
    objects, representations, selected_ids}``. ``status`` is ``failed`` (no
    objects) when the project cannot be digested, has structural validation
    errors, or its geometry cannot be resolved.
    """
    plan = {
        "project_id": None,
        "source_revision": None,
        "input_digest": None,
        "status": "failed",
        "diagnostics": [],
        "objects": [],
        "representations": {},
        "selected_ids": [],
    }
    if not isinstance(project, dict):
        plan["diagnostics"].append(diagnostic("invalid_input", "error", [], "project must be an object"))
        return plan
    if isinstance(project.get("id"), str):
        plan["project_id"] = project["id"]
    revision = project.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        plan["source_revision"] = revision
    try:
        plan["input_digest"] = canonical_digest(project)
    except (TypeError, ValueError) as exc:
        plan["diagnostics"].append(diagnostic("invalid_input", "error", [], f"project is not canonical JSON: {exc}"))
        return plan
    selected, selection_notes = _selection(selected_ids)
    plan["selected_ids"] = list(selected)
    found = validate_project(project)
    relevant = [d for d in found if d["severity"] == "error" or d["code"] not in PID_WARNINGS]
    if any(d["severity"] == "error" for d in found):
        plan["diagnostics"] = relevant
        return plan
    builder = _Builder(copy.deepcopy(project), plan["input_digest"])
    try:
        builder.run(other_warnings=[d for d in relevant if d["code"] not in _OWN_WARNINGS])
    except Exception as exc:  # a template fault must fail the plan, not escape
        plan["diagnostics"] = relevant + [
            diagnostic("geometry_unavailable", "error", [], f"physical geometry could not be planned ({type(exc).__name__}: {exc})")
        ]
        return plan
    selection_notes += builder.select(selected)
    plan.update(
        status=builder.status,
        diagnostics=relevant + builder.diagnostics + selection_notes,
        objects=builder.objects,
        representations=builder.representations,
    )
    return plan


# Validator warnings whose omission or flagging the builder itself reports.
_OWN_WARNINGS = frozenset({"placement_unresolved", "placement_missing", "route_not_current", "route_missing"})


def _selection(selected_ids):
    if not isinstance(selected_ids, (list, tuple)):
        return (), [diagnostic("selection_invalid", "warning", [], "selected_ids must be a tuple of IDs; selection ignored")]
    selected, notes = [], []
    for item in selected_ids:
        if isinstance(item, str) and item:
            if item not in selected:
                selected.append(item)
        else:
            notes.append(diagnostic("selection_invalid", "warning", [], f"ignored selected ID {item!r}"))
    return tuple(selected), notes


def _mesh_spec(primitive):
    shape = primitive["shape"]
    if shape == "box":
        return {"shape": "box", "size_m": list(primitive["size_m"])}
    if shape == "cylinder":
        radius = primitive["radius_m"]
        return {"shape": "cylinder", "radius_m": radius, "length_m": primitive["length_m"],
                "segments": geo.cylinder_segments(radius)}
    if shape == "sphere":
        return {"shape": "sphere", "radius_m": primitive["radius_m"], "segments": geo.SPHERE_SEGMENTS,
                "rings": geo.SPHERE_RINGS}
    raise ValueError(f"unsupported primitive shape {shape!r}")


def _half_extents(mesh):
    if mesh["shape"] == "box":
        return [s / 2 for s in mesh["size_m"]]
    if mesh["shape"] == "cylinder":
        return [mesh["radius_m"], mesh["radius_m"], mesh["length_m"] / 2]
    return [mesh["radius_m"]] * 3


def _world_corners(matrix, half):
    hx, hy, hz = half
    return [geo.transform_point(matrix, (sx * hx, sy * hy, sz * hz))
            for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]


def _bounds(points):
    return (tuple(min(p[i] for p in points) for i in range(3)), tuple(max(p[i] for p in points) for i in range(3)))


class _Builder:
    """Accumulates object specifications for one validated project copy."""

    def __init__(self, project, digest):
        self.project = project
        self.project_id = project["id"]
        self.revision = project["revision"]
        self.digest = digest
        model, physical = project["model"], project["physical"]
        self.entities = model["entities"]
        self.ports = model["ports"]
        self.connections = model["connections"]
        self.placements = physical["placements"]
        self.routes = physical["routes"]
        self.port_ids = {(p["entity_id"], p["name"]): pid for pid, p in sorted(self.ports.items())}
        self.objects = []
        self.by_key = {}
        self.representations = {}
        self.diagnostics = []
        self.pending = []
        self.corners = []
        self.status = "complete"

    def run(self, other_warnings):
        for owner in (*sorted(self.entities), *sorted(self.ports), *sorted(self.connections), self.project_id):
            self.representations.setdefault(owner, [])
        for eid in sorted(self.entities):
            placement = self.placements.get(eid)
            if is_composed(self.entities[eid]):
                self.omit_composed(eid, placement)
            elif placement is not None and placement.get("status") == "placed":
                self.add_entity(eid, placement)
            else:
                self.omit_entity(eid, placement)
        for cid in sorted(self.connections):
            self.add_connection(cid)
        if self.pending or other_warnings:
            self.status = "pending"
        for warning in other_warnings:
            self.pending.append(f"{warning['code']}: {warning['message']}")
        self.add_status_board()

    # Object specifications

    def spec(self, owner_field, owner_id, component_key, kind, *, location, rotation=(0.0, 0.0, 0.0),
             parent=None, collection, material=None, mesh=None, text=None, empty_size=None, properties=None):
        key = representation_id(owner_id, component_key)
        local = geo.affine(location, rotation)
        world = local if parent is None else geo.compose(self.by_key[parent]["matrix_world"], local)
        props = {
            "skid_generator": GENERATOR,
            "representation_id": key,
            "project_id": self.project_id,
            "component_key": component_key,
            "source_revision": self.revision,
            "selected": False,
        }
        if owner_field is not None:
            props[owner_field] = owner_id
        if material is not None:
            props["material_role"] = material
        props.update(properties or {})
        spec = {
            "key": key,
            "name": object_name(owner_id, component_key),
            "owner_id": owner_id,
            "component_key": component_key,
            "type": kind,
            "collection": collection,
            "parent": parent,
            "location": geo.clean_point(location),
            "rotation_deg": geo.clean_point(rotation),
            "matrix_world": [geo.clean_point(row) for row in world],
            "material": material,
            "mesh": mesh,
            "text": text,
            "empty_size": empty_size,
            "properties": props,
        }
        self.objects.append(spec)
        self.by_key[key] = spec
        self.representations[owner_id].append(key)
        return spec

    def add_label(self, owner_field, owner_id, body, position, size, material, *, component_key="label",
                  align=("CENTER", "CENTER"), properties=None):
        width, height = text_extent(body, size)
        pad = 0.3 * size
        text = self.spec(owner_field, owner_id, component_key, "text", location=position, rotation=LABEL_ROTATION_DEG,
                         collection="annotations", material=material,
                         text={"body": body, "size": size, "align_x": align[0], "align_y": align[1]},
                         properties=properties)
        # The plate sits behind the text in the text's own frame, whose +z faces the reader.
        if align == ("LEFT", "TOP"):
            centre = (width / 2, -height / 2 + 0.1 * size, -PLATE_OFFSET_M)
        else:
            centre = (0.0, 0.1 * size, -PLATE_OFFSET_M)
        self.spec(owner_field, owner_id, f"{component_key}/plate", "mesh", location=centre, parent=text["key"],
                  collection="annotations", material="plate",
                  mesh={"shape": "box", "size_m": geo.clean_point((width + 2 * pad, height + 2 * pad, PLATE_THICKNESS_M))},
                  properties=None if properties is None else {k: v for k, v in properties.items() if k != "tag"})
        return text

    # Equipment

    def add_entity(self, eid, placement):
        entity = self.entities[eid]
        common = {
            "kind": entity["kind"],
            "template_version": entity["template_version"],
            "placement_status": "placed",
            "placement_manual": placement["manual"],
            "placement_assumption_id": placement["assumption_id"],
        }
        if "observes" in entity:
            common.update(conceptual=True, observes_entity_id=entity["observes"]["entity_id"],
                          observes_variable=entity["observes"]["variable"])
        frame = self.spec("entity_id", eid, "frame", "empty", location=placement["position_m"],
                          rotation=placement["rotation_deg"], collection="equipment", empty_size=0.12,
                          properties=common)
        corners = []
        for primitive in entity_primitives(entity):
            key = primitive["key"]
            props = dict(common, shape=primitive["shape"])
            port_id = None
            if key.startswith("nozzle/"):
                name = key.split("/", 1)[1]
                port_id = self.port_ids.get((eid, name))
                if port_id is not None:
                    props.update(port_id=port_id, port_name=name,
                                 port_position_m=list(world_port_position(self.project, port_id)))
                material = "nozzle"
            else:
                material = _PRIMITIVE_MATERIALS.get((entity["kind"], key), "equipment")
            mesh = _mesh_spec(primitive)
            spec = self.spec("entity_id", eid, key, "mesh", location=primitive["center_m"],
                             rotation=primitive["rotation_deg"], parent=frame["key"], collection="equipment",
                             material=material, mesh=mesh, properties=props)
            if port_id is not None:
                self.representations[port_id].append(spec["key"])
            corners.extend(_world_corners(spec["matrix_world"], _half_extents(mesh)))
        self.corners.extend(corners)
        low, high = _bounds(corners)
        position = ((low[0] + high[0]) / 2, low[1] - LABEL_GAP_M, high[2] + LABEL_GAP_M + 0.6 * LABEL_SIZE_M)
        self.add_label("entity_id", eid, entity["tag"], position, LABEL_SIZE_M, "label",
                       properties={"kind": entity["kind"], "tag": entity["tag"]})

    def omit_composed(self, eid, placement):
        # Composed equipment has only definition geometry (skid.geometry); this adapter draws template
        # primitives and must not substitute a generic solid, so the omission is explicit and pending.
        entity = self.entities[eid]
        definition = entity.get("definition") if isinstance(entity.get("definition"), dict) else {}
        state = "placed" if placement is not None and placement.get("status") == "placed" else "not placed"
        self.diagnostics.append(diagnostic(
            "geometry_unsupported", "warning", [eid],
            f"entity {eid!r} ({entity['tag']}) is composed equipment (definition {definition.get('id')!r}, {state}); "
            f"the offline Blender adapter cannot represent it, so no 3D geometry was generated for it here",
        ))
        self.pending.append(f"{entity['tag']}: composed equipment - not representable in this view")

    def omit_entity(self, eid, placement):
        entity = self.entities[eid]
        if placement is None:
            state, reason = "missing", "no physical placement record"
        else:
            state, reason = "unresolved", placement.get("reason") or "placement unresolved"
        self.diagnostics.append(diagnostic(
            "geometry_omitted", "warning", [eid],
            f"entity {eid!r} ({entity['tag']}) has no resolved placement, so no 3D geometry was generated: {reason}",
        ))
        self.pending.append(f"{entity['tag']}: placement {state} - no 3D geometry")

    # Piping

    def add_connection(self, cid):
        connection = self.connections[cid]
        tag = connection["tag"]
        route = self.routes.get(cid)
        if route is None:
            self.omit_route(cid, tag, "missing", "no physical route record")
            return
        status, points = route["status"], route["points_m"]
        outside = connection["outside_diameter_m"]
        common = {
            "route_status": status,
            "route_mode": route["mode"],
            "from_port_id": connection["from_port_id"],
            "to_port_id": connection["to_port_id"],
            "nominal_diameter_mm": connection["nominal_diameter_mm"],
            "outside_diameter_m": outside,
        }
        if route.get("assumption_id") is not None:
            common["route_assumption_id"] = route["assumption_id"]
        if status == "current":
            pieces = geo.pipe_pieces(points, outside)
            prefix, material, label_material, caption = "", "pipe", "label", tag
        else:
            reason = route.get("reason") or status
            common["route_reason"] = reason
            if len(points) < 2:
                self.omit_route(cid, tag, status, reason)
                return
            pieces = geo.ghost_pieces(points, outside / 2 * GHOST_RADIUS_FACTOR)
            prefix, material, label_material = "stale/", "stale", "label_warning"
            caption = f"{tag} {'REROUTE' if status == 'needs_reroute' else 'UNRESOLVED'}"
            self.diagnostics.append(diagnostic(
                "route_flagged", "warning", [cid],
                f"route of {cid!r} ({tag}) is {status}; its retained points are drawn as a red ghost, "
                f"not as current piping: {reason}",
            ))
            self.pending.append(f"{tag}: route {status.replace('_', ' ')} - shown as red ghost")
        origin = tuple(points[0])
        frame = self.spec("connection_id", cid, "route", "empty", location=origin, collection="piping",
                          empty_size=0.05, properties=common)
        for piece in pieces:
            self.add_piece(cid, frame, origin, piece, prefix, material, common)
        self.corners.extend(tuple(p) for p in points)
        self.add_line_label(cid, tag, points, outside, caption, label_material, common)

    def add_piece(self, cid, frame, origin, piece, prefix, material, common):
        kind, index, radius = piece["kind"], piece["index"], piece["radius"]
        props = dict(common)
        if kind == "segment":
            start, end = piece["start"], piece["end"]
            length = math.dist(start, end)
            location = [(s + e) / 2 - o for s, e, o in zip(start, end, origin)]
            mesh = {"shape": "cylinder", "radius_m": radius, "length_m": geo.clean(length),
                    "segments": geo.cylinder_segments(radius)}
            props.update(segment_index=index, start_m=geo.clean_point(start), end_m=geo.clean_point(end))
            self.spec("connection_id", cid, f"{prefix}segment/{index}", "mesh", location=location,
                      rotation=geo.direction_rotation(piece["direction"]), parent=frame["key"], collection="piping",
                      material=material, mesh=mesh, properties=props)
            return
        location = [c - o for c, o in zip(piece["corner"], origin)]
        props["corner_index"] = index
        if kind == "elbow":
            segments = geo.cylinder_segments(radius)
            mesh = {"shape": "bend", "incoming": geo.clean_point(piece["incoming"]),
                    "outgoing": geo.clean_point(piece["outgoing"]), "bend_radius_m": geo.clean(piece["bend_radius"]),
                    "pipe_radius_m": radius, "steps": geo.BEND_STEPS, "segments": segments}
            props.update(start_m=geo.clean_point(piece["start"]), end_m=geo.clean_point(piece["end"]),
                         bend_radius_m=geo.clean(piece["bend_radius"]), ring_vertices=segments)
        else:
            joint_radius = radius * (GHOST_JOINT_FACTOR if prefix else 1.0)
            mesh = {"shape": "sphere", "radius_m": geo.clean(joint_radius), "segments": 24, "rings": 12}
        self.spec("connection_id", cid, f"{prefix}{kind}/{index}", "mesh", location=location, parent=frame["key"],
                  collection="piping", material=material, mesh=mesh, properties=props)

    def add_line_label(self, cid, tag, points, outside, caption, material, common):
        # Label the longest route segment: below a run along x, on the -x side of any other run.
        best = max(range(len(points) - 1), key=lambda i: (round(math.dist(points[i], points[i + 1]), 9), -i))
        start, end = points[best], points[best + 1]
        middle = [(s + e) / 2 for s, e in zip(start, end)]
        extent = [abs(e - s) for s, e in zip(start, end)]
        radius, size = outside / 2, LINE_LABEL_SIZE_M
        width, _ = text_extent(caption, size)
        if extent[0] >= max(extent[1], extent[2]):
            position = (middle[0], middle[1] - radius - LABEL_GAP_M, middle[2] - radius - LABEL_GAP_M - size / 2)
        else:
            position = (middle[0] - radius - LABEL_GAP_M - width / 2, middle[1] - radius - LABEL_GAP_M, middle[2])
        properties = {"tag": tag, "route_status": common["route_status"]}
        self.add_label("connection_id", cid, caption, position, size, material, properties=properties)

    def omit_route(self, cid, tag, state, reason):
        self.diagnostics.append(diagnostic(
            "route_omitted", "warning", [cid],
            f"route of {cid!r} ({tag}) is {state} without drawable points; no 3D piping was generated: {reason}",
        ))
        self.pending.append(f"{tag}: route {state.replace('_', ' ')} - no 3D piping")

    # Project status

    def add_status_board(self):
        lines = [
            f"{self.project_id}  revision {self.revision}  input {self.digest[:12]}",
            f"3D status: {self.status.upper()}",
        ]
        shown = self.pending[:MAX_STATUS_ITEMS]
        lines.extend(f"- {item}" for item in shown)
        if len(self.pending) > len(shown):
            lines.append(f"- ... and {len(self.pending) - len(shown)} more (see manifest)")
        for kind, heading in (("indicator", "Conceptual indications, no hookup modeled"), ("boundary", "Scope boundaries")):
            tags = sorted(e["tag"] for e in self.entities.values() if e["kind"] == kind)
            if tags:
                lines.append(f"{heading}: {', '.join(tags)}")
        lines.append("Illustrative prototype geometry; no process, safety or fabrication design.")
        if self.corners:
            low, high = _bounds(self.corners)
        else:
            low = high = (0.0, 0.0, 0.0)
        position = (low[0], high[1] + 0.3, high[2] + 0.25 + STATUS_SIZE_M * 1.2 * (len(lines) - 1))
        material = "label" if self.status == "complete" else "label_warning"
        self.add_label(None, self.project_id, "\n".join(lines), position, STATUS_SIZE_M, material,
                       component_key="status", align=("LEFT", "TOP"),
                       properties={"status": self.status, "input_digest": self.digest})

    # Transient selection

    def select(self, selected):
        notes = []
        for sid in selected:
            keys = self.representations.get(sid)
            if keys is None:
                notes.append(diagnostic("selection_unknown", "warning", [sid],
                                        f"selected ID {sid!r} is not an entity, port, connection or project here"))
                continue
            if not keys:
                notes.append(diagnostic("selection_unrepresented", "warning", [sid],
                                        f"selected ID {sid!r} has no 3D representation at this revision"))
            for key in keys:
                spec = self.by_key[key]
                spec["properties"]["selected"] = True
                if spec["material"] not in (None, "plate"):
                    spec["material"] = "selected"
                    spec["properties"]["material_role"] = "selected"
        return notes
