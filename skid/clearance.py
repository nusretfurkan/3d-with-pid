"""Conservative equipment-body clearance for physical pipe routes (prototype, axis-aligned boxes).

This is a bounded intrusion screen, not a plant clash engine. It answers one
question: does a route's swept DN pipe (straight legs, bends and route-side
end flanges) enter the space occupied by a placed equipment body? Both sides
are over-approximated by world axis-aligned boxes, so a reported clear route
is clear of the modelled bodies; a reported intrusion may be a near miss.

Bodies come from the authoritative project only:

- built-in kinds: the template-version-1 primitives of ``skid.templates``
  (``entity_primitives``). The detailed CAD recipes (``skid.geometry``) add
  illustrative features (flange rims, handwheels, base details) that can
  extend beyond these primitives; they are not screened here.
- ``composed_equipment``: every non-display component of the resolved
  definition (``skid.equipment.resolve_definition``), bounded part by part.
  ``loft`` and ``volute_casing`` parts have no proven bound and make the
  entity unbounded; any unknown part does too.

Every part of every placed entity blocks, including the route's own
endpoint owners and their nozzles. No component is exempt by name or by
port mapping: an intended joint needs no exemption because a nozzle's
geometry ends at its port face (built-in nozzle stubs end there; composed
``flanged_nozzle`` faces lie on it) while the route envelope starts there, so
the two only touch. The one exemption is a built-in ``indicator``'s ``stem``:
the instrument's mount foot, which the prototype places on a line without
modelling the mount connection (0.15 mm into L-103 in the sample). Its dial
body still blocks.

The route envelope: each straight leg is the box of its two centreline points
grown by the pipe radius; each corner adds the box of its two bend tangency
points and the corner (the bend arc lies in that triangle), grown by the pipe
radius, using the same bend radius as the CAD sweep (1.5 DN reduced to 0.49 x
the shorter adjacent run); each end adds the route flange (outer radius 75 mm,
plate 18 mm + hub 30 mm outward of the port face) and its front nuts. The
nozzle-side half of each bolted joint (back nuts and stud ends behind the
port face) is not part of the route envelope. A corner whose terminal leg is
shorter than about two pipe radii can reach behind the port face; that errs
towards refusal.

Overlaps of at most ``CONTACT_TOLERANCE_M`` along any axis are contact, not
intrusion. Other routes, supports, stress, access and maintenance space are
not checked. Pure standard library plus ``skid.templates``/``skid.equipment``.
"""

from __future__ import annotations

import math

from .equipment import DefinitionError, is_composed, resolve_definition
from .templates import entity_primitives

__all__ = [
    "BEND_RADIUS_FACTOR",
    "CONTACT_TOLERANCE_M",
    "CORNER_ROOM",
    "ClearanceError",
    "FLANGE_DEPTH_M",
    "FLANGE_RADIUS_M",
    "body_boxes",
    "boxes_overlap",
    "corner_cuts",
    "intrusions",
    "port_direction",
    "route_envelope",
    "route_report",
]

BEND_RADIUS_FACTOR = 1.5  # bend centreline radius / DN, as skid.geometry.routes
CORNER_ROOM = 0.49  # share of the shorter adjacent run a corner may use, as skid.geometry.kernel.pipe
FLANGE_RADIUS_M = 0.075  # DN50 route flange outer radius (skid.geometry.equipment FLANGE_DN50)
FLANGE_DEPTH_M = 0.048  # plate 18 mm + hub 30 mm outward of the joint face; front nuts reach 35 mm
CONTACT_TOLERANCE_M = 1e-6
MODEL = "conservative axis-aligned boxes"
_UNBOUNDED_PARTS = frozenset({"loft", "volute_casing"})
_QUARTER = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))


class ClearanceError(Exception):
    """Geometry that cannot be bounded honestly; ``code``, ``message`` and the IDs involved."""

    def __init__(self, code, message, ids=()):
        super().__init__(message)
        self.code, self.message, self.ids = code, message, list(ids)


# --- Box helpers ------------------------------------------------------------------


def _box(points, grow=(0.0, 0.0, 0.0)):
    return [[min(p[i] for p in points) - grow[i] for i in range(3)],
            [max(p[i] for p in points) + grow[i] for i in range(3)]]


def _corners(box):
    lo, hi = box
    return [[(lo, hi)[(n >> i) & 1][i] for i in range(3)] for n in range(8)]


def _disc(axis, radius):
    """Half extents of a disc of ``radius`` perpendicular to unit ``axis``."""
    return [radius * math.sqrt(max(0.0, 1.0 - c * c)) for c in axis]


def _rod(start, end, radius):
    """Box of a cylinder between two points (exact for an axis-parallel one)."""
    axis = _unit([e - s for s, e in zip(start, end)])
    return _box([start, end], _disc(axis, radius))


def boxes_overlap(a, b, tolerance=CONTACT_TOLERANCE_M):
    """Overlap depth along each axis when both boxes overlap by more than ``tolerance`` on every axis, else None."""
    depth = [min(a[1][i], b[1][i]) - max(a[0][i], b[0][i]) for i in range(3)]
    return depth if all(d > tolerance for d in depth) else None


# --- Rotations --------------------------------------------------------------------


def _cos_sin(degrees):
    quarters, remainder = divmod(degrees, 90.0)
    if remainder == 0:
        return _QUARTER[int(quarters) % 4]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def _euler(rotation_deg):
    """``Rz @ Ry @ Rx`` (fixed-axis XYZ degrees, the template convention), exact for quarter turns."""
    cx, sx = _cos_sin(rotation_deg[0])
    cy, sy = _cos_sin(rotation_deg[1])
    cz, sz = _cos_sin(rotation_deg[2])
    return ((cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
            (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
            (-sy, cy * sx, cy * cx))


def _about(axis, degrees):
    x, y, z = axis
    c, s = _cos_sin(degrees)
    t = 1 - c
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c))


def _apply(matrix, vector):
    return [sum(matrix[i][j] * vector[j] for j in range(3)) for i in range(3)]


def _unit(vector):
    length = math.sqrt(sum(c * c for c in vector))
    if not length > 1e-12:
        raise ValueError("zero-length direction")
    return [c / length for c in vector]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _plane(normal, u_axis=None):
    """``(u, v, n)`` as ``skid.geometry.kernel.frame_axes``."""
    n = _unit(normal)
    if u_axis is None:
        magnitudes = [abs(c) for c in n]
        a = [0.0, 0.0, 0.0]
        a[magnitudes.index(min(magnitudes))] = 1.0
    else:
        a = _unit(u_axis)
    d = sum(x * y for x, y in zip(a, n))
    u = _unit([a[i] - d * n[i] for i in range(3)])
    return u, _cross(n, u), n


def _along(point, direction, distance):
    return [p + d * distance for p, d in zip(point, direction)]


def _moved(box, offset):
    return [[c + o for c, o in zip(box[0], offset)], [c + o for c, o in zip(box[1], offset)]]


def _rotated(box, matrix, origin=(0.0, 0.0, 0.0)):
    return _box([[o + c for o, c in zip(origin, _apply(matrix, [p - o for p, o in zip(corner, origin)]))]
                 for corner in _corners(box)])


# --- Composed part bounds (local millimetres) -------------------------------------


def _part_box(call):
    """Box of one resolved part call in its local frame; ``ClearanceError`` when it has no proven bound."""
    name = call.get("part")
    get = call.get
    if name in _UNBOUNDED_PARTS:
        raise ClearanceError("clearance_unbounded_geometry", f"part {name!r} has no proven bound in the clearance "
                             "screen")
    if name == "box":
        size = [s / 2 for s in get("size")]
        local = [[-s for s in size], size]
        return _moved(_rotated(local, _euler(get("rotation_deg") or (0.0, 0.0, 0.0))), get("center"))
    if name == "cylinder":
        return _rod(get("start"), get("end"), get("radius"))
    if name == "cone":
        return _rod(get("start"), get("end"), max(get("radius_start"), get("radius_end")))
    if name == "sphere":
        r = get("radius")
        return _box([get("center")], (r, r, r))
    if name == "torus":
        axis, big, small = _unit(get("axis")), get("major_radius"), get("minor_radius")
        return _box([get("center")], [d + small * abs(a) for d, a in zip(_disc(axis, big + small), axis)])
    if name == "revolve":
        axis, origin = _unit(get("axis")), get("origin")
        radial = max(r for r, _ in get("profile"))
        heights = [h for _, h in get("profile")]
        return _box([_along(origin, axis, min(heights)), _along(origin, axis, max(heights))], _disc(axis, radial))
    if name == "prism":
        u, v, n = _plane(get("normal"), get("u_axis"))
        origin, depth, outline = get("origin"), get("depth"), get("outline")
        if isinstance(outline, dict):
            base = _box([origin], _disc(n, outline["circle"]))
            return _box(_corners(base) + _corners(_moved(base, [c * depth for c in n])))
        points = [[origin[i] + u[i] * p[0] + v[i] * p[1] for i in range(3)] for p in outline]
        return _box(points + [_along(p, n, depth) for p in points])
    if name == "hex_prism":
        axis = _unit(get("axis"))
        return _rod(get("start"), _along(get("start"), axis, get("height")), get("across_flats"))
    if name == "pipe":
        r = get("radius")
        return _box(get("points"), (r, r, r))
    if name == "tube":
        return _rod(get("start"), get("end"), get("outer_radius"))
    if name == "flange":
        n = _unit(get("normal"))
        back = get("thickness") + (get("hub_length") or 0.0)
        return _rod(get("face_center"), _along(get("face_center"), n, -back), get("outer_radius"))
    if name == "flanged_nozzle":
        d = _unit(get("direction"))
        radius = max(get("neck_radius"), get("flange")["outer_radius"])
        return _rod(_along(get("port"), d, -get("neck_length")), get("port"), radius)
    if name == "bolting":
        n, h, p = _unit(get("normal")), get("nut_height"), get("protrusion", 4.0)
        radius = get("bolt_circle_radius") + max(get("stud_radius"), get("nut_across_flats"))
        face = get("face_center")
        return _rod(_along(face, n, -(get("thickness_back") + h + p)), _along(face, n, get("thickness_front") + h + p),
                    radius)
    if name == "bolt":
        a, h, length = _unit(get("axis")), get("height"), get("stud_length")
        return _rod(_along(get("seat"), a, -min(length, 2 * h)), _along(get("seat"), a, max(length, h)),
                    max(get("across_flats"), get("stud_radius")))
    if name == "finned_frame":
        # Each fin is a radial plate whose outer corners lie fin_thickness / 2 beside the tip radius.
        a = _unit(get("axis"))
        return _rod(get("origin"), _along(get("origin"), a, get("length")),
                    math.hypot(get("fin_tip_radius"), get("fin_thickness") / 2))
    if name == "handwheel":
        a, rim, t, hub = _unit(get("axis")), get("rim_radius"), get("rim_thickness"), get("hub_height")
        half = max(t / 2, hub / 2)
        return _box([get("center")], [d + half * abs(c) for d, c in zip(_disc(a, rim + t / 2), a)])
    if name == "arrow":
        reach = math.hypot(get("length") / 2, get("width") / 2) + get("depth")
        return _box([get("origin")], (reach, reach, reach))
    if name == "instances":
        inner = _part_box(get("item"))
        return _box([c for offset in get("offsets") for c in _corners(_moved(inner, offset))])
    if name == "linear_array":
        inner = _part_box(get("item"))
        step = get("step")
        return _box([c for i in range(get("count")) for c in _corners(_moved(inner, [s * i for s in step]))])
    if name == "polar_array":
        inner, axis, origin = _part_box(get("item")), _unit(get("axis")), get("origin")
        if get("angles_deg") is not None:
            angles = list(get("angles_deg"))
        else:
            count = get("count")
            angles = [get("start_deg", 0.0) + 360.0 * i / count for i in range(count)]
        return _box([c for a in angles for c in _corners(_rotated(inner, _about(axis, a), origin))])
    raise ClearanceError("clearance_unbounded_geometry", f"part {name!r} is not known to the clearance screen")


# --- Bodies -----------------------------------------------------------------------


def _primitive_box(primitive):
    """Local-frame box of a template-version-1 primitive (metres)."""
    rotation = _euler(primitive["rotation_deg"])
    center = primitive["center_m"]
    shape = primitive["shape"]
    if shape == "box":
        half = [s / 2 for s in primitive["size_m"]]
        return _moved(_rotated([[-h for h in half], half], rotation), center)
    if shape == "cylinder":
        axis = _apply(rotation, (0.0, 0.0, 1.0))
        half = primitive["length_m"] / 2
        return _rod(_along(center, axis, -half), _along(center, axis, half), primitive["radius_m"])
    if shape == "sphere":
        r = primitive["radius_m"]
        return _box([center], (r, r, r))
    raise ClearanceError("clearance_unbounded_geometry", f"primitive shape {shape!r} is not known")


def _entity_local_boxes(entity):
    """``[(component key, local box in metres)]`` of one entity's blocking geometry."""
    out = []
    if is_composed(entity):
        resolved = resolve_definition(entity)
        for component in resolved["components"]:
            if component.get("display_only"):
                continue
            for index, call in enumerate(component["parts"]):
                try:
                    box = _part_box(call)
                except ClearanceError as exc:
                    raise ClearanceError(exc.code, f"{component['key']} part {index}: {exc.message}") from None
                out.append((component["key"], [[c / 1000.0 for c in box[0]], [c / 1000.0 for c in box[1]]]))
        return out
    for primitive in entity_primitives(entity):
        key = primitive["key"]
        if entity.get("kind") == "indicator" and key == "stem":
            continue
        out.append((key, _primitive_box(primitive)))
    return out


def body_boxes(project) -> list[dict]:
    """World boxes ``{entity_id, tag, component, box}`` of every placed entity's blocking geometry.

    One box per part (composed) or primitive (built-in). Unplaced entities
    occupy no physical space and are skipped. Raises ``ClearanceError`` (``clearance_unbounded_geometry``)
    when an entity's geometry cannot be resolved or bounded.
    """
    out = []
    placements = project["physical"]["placements"]
    for entity_id, entity in sorted(project["model"]["entities"].items()):
        placement = placements.get(entity_id)
        if not placement or placement.get("status") != "placed":
            continue
        tag = entity.get("tag") or entity_id
        try:
            local = _entity_local_boxes(entity)
            rotation = _euler([float(v) for v in placement["rotation_deg"]])
            position = [float(v) for v in placement["position_m"]]
        except ClearanceError as exc:
            raise ClearanceError(exc.code, f"{tag} ({entity_id}): {exc.message}; route clearance cannot be "
                                 "confirmed", [entity_id]) from None
        except (DefinitionError, ArithmeticError, LookupError, TypeError, ValueError) as exc:
            raise ClearanceError("clearance_unbounded_geometry", f"{tag} ({entity_id}) has no resolvable body "
                                 f"geometry ({exc}); route clearance cannot be confirmed", [entity_id]) from None
        for key, box in local:
            out.append({"entity_id": entity_id, "tag": tag, "component": key,
                        "box": _moved(_rotated(box, rotation), position)})
    return out


# --- Route envelope ---------------------------------------------------------------


def corner_cuts(points, nominal_diameter_mm):
    """Bend tangent distance (metres) at each point: 0 at the ends and straight-through points.

    A right-angle corner uses ``min(1.5 DN, 0.49 x shorter adjacent run)``; a
    general corner uses the CAD sweep's ``r * tan(theta / 2)`` with the same
    limit, so the value matches ``skid.geometry.kernel.pipe``.
    """
    cuts = [0.0] * len(points)
    bend = BEND_RADIUS_FACTOR * nominal_diameter_mm / 1000.0
    for i in range(1, len(points) - 1):
        a, b, c = points[i - 1], points[i], points[i + 1]
        d0 = [y - x for x, y in zip(a, b)]
        d1 = [y - x for x, y in zip(b, c)]
        l0, l1 = math.sqrt(sum(x * x for x in d0)), math.sqrt(sum(x * x for x in d1))
        if l0 <= 0.0 or l1 <= 0.0:
            continue
        cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(d0, d1)) / (l0 * l1)))
        theta = math.acos(cosine)
        if theta < 1e-9 or theta > math.pi - 1e-6:
            continue
        half = math.tan(theta / 2)
        cuts[i] = min(bend, CORNER_ROOM * min(l0, l1) / half) * half
    return cuts


def route_envelope(points, outside_diameter_m, nominal_diameter_mm, start_direction=None, end_direction=None):
    """Boxes ``{part, index, box}`` containing the swept route: ``leg`` i, ``bend`` at point i, end ``flange``.

    ``start_direction``/``end_direction`` are the ports' outward unit
    directions; ``None`` leaves that end's flange out.
    """
    r = outside_diameter_m / 2
    grow = (r, r, r)
    # A leg ends flat at its points (the corner's own box covers the joint), so it grows only across its axis.
    out = [{"part": "leg", "index": i, "box": _rod(list(a), list(b), r)} for i, (a, b) in enumerate(zip(points, points[1:]))]
    cuts = corner_cuts(points, nominal_diameter_mm)
    for i in range(1, len(points) - 1):
        if cuts[i] <= 0.0:
            continue
        corner, back, ahead = points[i], math.dist(points[i - 1], points[i]), math.dist(points[i + 1], points[i])
        before = [p + (a - p) * cuts[i] / back for a, p in zip(points[i - 1], corner)]
        after = [p + (c - p) * cuts[i] / ahead for c, p in zip(points[i + 1], corner)]
        out.append({"part": "bend", "index": i, "box": _box([before, points[i], after], grow)})
    for index, (point, direction) in enumerate(((points[0], start_direction), (points[-1], end_direction))):
        if direction is None:
            continue
        n = _unit(direction)
        out.append({"part": "flange", "index": 0 if index == 0 else len(points) - 1,
                    "box": _rod(list(point), _along(point, n, FLANGE_DEPTH_M), FLANGE_RADIUS_M)})
    return out


def intrusions(envelope, obstacles, tolerance=CONTACT_TOLERANCE_M) -> list[dict]:
    """Every (route part, obstacle) pair whose boxes overlap beyond contact, in envelope then obstacle order."""
    found = []
    for part in envelope:
        for obstacle in obstacles:
            depth = boxes_overlap(part["box"], obstacle["box"], tolerance)
            if depth is not None:
                found.append({"route_part": part["part"], "route_index": part["index"],
                              **{k: obstacle[k] for k in ("entity_id", "tag", "component") if k in obstacle},
                              "overlap_m": [round(d, 6) for d in depth]})
    return found


def port_direction(project, port_id):
    """Outward world unit direction of a port, or None when it cannot be resolved."""
    model = project["model"]
    port = model["ports"].get(port_id) or {}
    entity = model["entities"].get(port.get("entity_id"))
    placement = project["physical"]["placements"].get(port.get("entity_id")) or {}
    if entity is None or placement.get("status") != "placed":
        return None
    try:
        if is_composed(entity):
            local = list(resolve_definition(entity)["ports"][port["name"]]["direction"])
        else:
            nozzle = next(p for p in entity_primitives(entity) if p["key"] == f"nozzle/{port['name']}")
            local = _apply(_euler(nozzle["rotation_deg"]), (0.0, 0.0, 1.0))
        return _unit(_apply(_euler([float(v) for v in placement["rotation_deg"]]), local))
    except (DefinitionError, StopIteration, ArithmeticError, LookupError, TypeError, ValueError):
        return None


def route_report(project, connection_id, points) -> dict:
    """Screen ``points`` as the route of ``connection_id`` against every placed body in ``project``.

    Returns ``{connection_id, model, clear, intrusions, bodies_checked, flanges_checked}``.
    Raises ``ClearanceError`` when a body cannot be bounded.
    """
    connection = project["model"]["connections"][connection_id]
    ends = (connection["from_port_id"], connection["to_port_id"])
    bodies = body_boxes(project)
    directions = [port_direction(project, port_id) for port_id in ends]
    envelope = route_envelope(points, connection["outside_diameter_m"], connection["nominal_diameter_mm"],
                              *directions)
    found = intrusions(envelope, bodies)
    return {"connection_id": connection_id, "model": MODEL, "clear": not found, "intrusions": found,
            "bodies_checked": len(bodies), "flanges_checked": sum(d is not None for d in directions)}
