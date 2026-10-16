"""Straight sections of a physical route and the deterministic section-move transform.

An editable section is one maximal straight centreline leg of a route under
its stable connection ID: the leg between two bends, or a terminal leg
between a port and its first bend. A section index is scoped to one source
revision and its exact stored points; it is never a persisted identity.

Pure standard library plus ``skid.clearance``/``skid.templates``. The core
``move_route_segment`` command calls ``move_section``; the browser preview
ports ``move_points`` exactly (``tests/fixtures/pipe-segments.json``).

Algorithm (all arithmetic is IEEE-754 double addition, subtraction,
multiplication, ``min`` and ``abs`` in the order written; nothing is
rounded). ``AXIS_TOL = 1e-6`` m, ``STUB = 0.16`` m, ``MAX_OFFSET = 10`` m,
``MAX_POINTS = 16``, ``MIN_LEG = 0.001`` m, ``BEND = 1.5 * DN / 1000`` m,
``ROOM = 0.49``, ``r = OD / 2``.

1. Legs of a point list: leg ``i`` runs from point ``i`` to ``i + 1``. Its
   axis is the only coordinate whose ``abs`` difference exceeds ``AXIS_TOL``;
   none is ``route_edit_zero_leg``, two or more is
   ``route_edit_not_orthogonal``. Its sign is that difference's sign
   (``+1``/``-1``) and its length that difference's ``abs``.
2. ``simplify(points)``: (a) walk the points keeping a list; a point whose
   every coordinate is within ``AXIS_TOL`` of the last kept point replaces it
   (the first point is never replaced, so both ends stay exact). (b) Compute
   the legs; while two consecutive legs share an axis, delete their shared
   point when their signs agree, otherwise refuse
   ``route_edit_reverses``; recompute and repeat. A result with fewer than
   two points is ``route_edit_zero_leg``.
3. ``corner cut`` at interior point ``i`` (right angles only after step 2):
   ``min(BEND, ROOM * min(length[i - 1], length[i]))``; 0 at both ends.
4. ``sections(points)``: simplify, then for each leg ``k`` the record
   ``index k, point_start k, point_end k + 1, axis, start_m, end_m,
   midpoint_m, length_m, move_axes``. ``start_m`` is point ``k`` with
   ``start_m[axis] += sign * cut[k]``; ``end_m`` is point ``k + 1`` with
   ``end_m[axis] -= sign * cut[k + 1]``; ``midpoint_m[c] = (start_m[c] +
   end_m[c]) / 2``; ``length_m = abs(end_m[axis] - start_m[axis])``;
   ``move_axes`` are the other two axes ascending. These are the bend
   tangency limits of the CAD sweep.
5. ``move_points(points, start_direction, end_direction, k, offset, OD, DN,
   obstacles)``; ``points`` are simplified first (``V``, ``n`` legs).
   ``k`` must be an integer in ``[0, n)`` (``route_edit_unknown_section``).
   ``offset`` must be three finite numbers (``route_edit_invalid_offset``);
   all zero is ``route_edit_no_op``; more than one nonzero component is
   ``route_edit_invalid_offset``; the nonzero axis ``m`` equal to leg ``k``'s
   axis is ``route_edit_offset_parallel``; ``abs(d) < MIN_LEG`` (``d =
   offset[m]``) is ``route_edit_offset_too_small`` (a smaller move can vanish
   in simplify or only drift the stored points); ``abs(d) > MAX_OFFSET`` is
   ``route_edit_offset_too_large``. Exactly ``+/-MIN_LEG`` is admissible.
   Start of the moved leg: if ``k == 0`` the port keeps a fixed stub: leg 0
   must be longer than ``STUB`` (else ``route_edit_stub_too_short``),
   ``s = V[0]`` with ``s[axis0] += sign0 * STUB``, head ``[V[0], s]``, moved
   start ``s`` with ``[m] += d``. Otherwise the moved start is ``V[k]`` with
   ``[m] += d``; when leg ``k - 1``'s axis is ``m`` that leg simply stretches
   (head ``V[0..k-1]``), else a connector along ``m`` is added (head
   ``V[0..k]``). The end is symmetric: if ``k == n - 1``, leg ``k`` must be
   longer than ``STUB`` (both stubs when ``n == 1``: longer than ``2 *
   STUB``), ``e = V[n]`` with ``e[axis_k] -= sign_k * STUB``, tail ``[e,
   V[n]]``, moved end ``e`` with ``[m] += d``; otherwise the moved end is
   ``V[k + 1]`` with ``[m] += d`` and the tail is ``V[k+2..n]`` when leg
   ``k + 1``'s axis is ``m``, else ``V[k+1..n]``. The candidate is ``head +
   [moved start, moved end] + tail``, then simplified (step 2).
6. Checks on the candidate, in this order, first failure wins: more than
   ``MAX_POINTS`` points ``route_edit_too_many_points``; first leg's axis and
   sign differ from ``start_direction`` or last leg's from ``-end_direction``
   ``route_edit_port_tangent``; a leg shorter than ``MIN_LEG``
   ``route_edit_short_leg``; a corner cut ``<= 1.01 * r``
   ``route_edit_bend_room``; two legs ``i < j`` with ``j - i >= 2`` whose
   axis-aligned centreline boxes are closer than ``OD`` (Euclidean gap)
   ``route_edit_self_clearance``; then the conservative envelope of
   ``skid.clearance.route_envelope`` overlapping any obstacle box by more
   than ``CONTACT_TOLERANCE_M`` on every axis ``route_edit_intrusion``.
   Otherwise the candidate is the result.

Refusals raise ``RouteEditError`` with a stable ``code``; parity compares
codes and exact point values, not message text.
"""

from __future__ import annotations

import math

from . import clearance
from .templates import world_port_position

__all__ = [
    "MAX_OFFSET_M",
    "MAX_POINTS",
    "MIN_OFFSET_M",
    "RouteEditError",
    "TERMINAL_STUB_M",
    "move_points",
    "move_section",
    "port_frame",
    "section_records",
    "simplify",
    "straight_sections",
]

AXIS_TOLERANCE_M = 1e-6  # core ROUTE_TOLERANCE_M
TERMINAL_STUB_M = 0.16  # as skid.interactive.rerouting: a full 75 mm bend clear of the 48 mm flange joint
MAX_OFFSET_M = 10.0
MAX_POINTS = 16
MIN_LEG_M = 0.001
MIN_OFFSET_M = MIN_LEG_M  # a smaller move would vanish in simplify or only drift the stored points
RADIUS_MARGIN = 1.01


class RouteEditError(Exception):
    """A refused section edit: stable ``code``, message, IDs and optional ``details``."""

    def __init__(self, code, message, ids=(), details=None):
        super().__init__(message)
        self.code, self.message, self.ids, self.details = code, message, list(ids), details

    @property
    def diagnostic(self):
        return {"code": self.code, "severity": "error", "entity_ids": list(self.ids), "message": self.message}


# --- Pure geometry ----------------------------------------------------------------


def _legs(points):
    """``[(axis, sign, length)]``; raises for a zero-length or non-orthogonal leg."""
    legs = []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        moving = [i for i in range(3) if abs(b[i] - a[i]) > AXIS_TOLERANCE_M]
        if not moving:
            raise RouteEditError("route_edit_zero_leg", f"leg {index} has zero length")
        if len(moving) > 1:
            raise RouteEditError("route_edit_not_orthogonal", f"leg {index} is not parallel to one axis")
        axis = moving[0]
        delta = b[axis] - a[axis]
        legs.append((axis, 1.0 if delta > 0 else -1.0, abs(delta)))
    return legs


def simplify(points):
    """Drop repeated points and merge same-direction consecutive legs (algorithm step 2)."""
    kept = []
    for p in points:
        point = [float(c) for c in p]
        if kept and all(abs(a - b) <= AXIS_TOLERANCE_M for a, b in zip(kept[-1], point)):
            if len(kept) > 1:
                kept[-1] = point
            continue
        kept.append(point)
    if len(kept) < 2:
        raise RouteEditError("route_edit_zero_leg", "the route has no length")
    while True:
        legs = _legs(kept)
        for i in range(1, len(legs)):
            if legs[i - 1][0] == legs[i][0]:
                if legs[i - 1][1] != legs[i][1]:
                    raise RouteEditError("route_edit_reverses", f"the route reverses direction at point {i}")
                del kept[i]
                break
        else:
            return kept


def _cuts(legs, nominal_diameter_mm):
    bend = clearance.BEND_RADIUS_FACTOR * nominal_diameter_mm / 1000.0
    cuts = [0.0] * (len(legs) + 1)
    for i in range(1, len(legs)):
        cuts[i] = min(bend, clearance.CORNER_ROOM * min(legs[i - 1][2], legs[i][2]))
    return cuts


def section_records(points, nominal_diameter_mm):
    """Straight-section records of a point list (algorithm step 4)."""
    vertices = simplify(points)
    legs = _legs(vertices)
    cuts = _cuts(legs, nominal_diameter_mm)
    out = []
    for k, (axis, sign, _) in enumerate(legs):
        start, end = list(vertices[k]), list(vertices[k + 1])
        start[axis] += sign * cuts[k]
        end[axis] -= sign * cuts[k + 1]
        out.append({"index": k, "point_start": k, "point_end": k + 1, "axis": axis, "start_m": start, "end_m": end,
                    "midpoint_m": [(a + b) / 2 for a, b in zip(start, end)],
                    "length_m": abs(end[axis] - start[axis]), "move_axes": [i for i in range(3) if i != axis]})
    return vertices, out


def _offset(offset):
    if not isinstance(offset, (list, tuple)) or len(offset) != 3 or any(
            isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) for c in offset):
        raise RouteEditError("route_edit_invalid_offset", "offset_m must be three finite numbers")
    moving = [i for i in range(3) if offset[i] != 0]
    if not moving:
        raise RouteEditError("route_edit_no_op", "offset_m is zero; nothing would move")
    if len(moving) > 1:
        raise RouteEditError("route_edit_invalid_offset", "offset_m must move along exactly one world axis")
    return moving[0], float(offset[moving[0]])


def _shifted(point, axis, delta):
    q = list(point)
    q[axis] += delta
    return q


def _gap(a0, a1, b0, b1):
    total = 0.0
    for i in range(3):
        gap = max(0.0, min(b0[i], b1[i]) - max(a0[i], a1[i]), min(a0[i], a1[i]) - max(b0[i], b1[i]))
        total += gap * gap
    return math.sqrt(total)


def _direction(axis, sign):
    unit = [0.0, 0.0, 0.0]
    unit[axis] = sign
    return unit


def check_points(points, start_direction, end_direction, outside_diameter_m, nominal_diameter_mm, obstacles=()):
    """Algorithm step 6 on an already simplified candidate; returns nothing or raises ``RouteEditError``."""
    if len(points) > MAX_POINTS:
        raise RouteEditError("route_edit_too_many_points", f"the edited route would have {len(points)} points "
                             f"(at most {MAX_POINTS})")
    legs = _legs(points)
    if _direction(legs[0][0], legs[0][1]) != [float(c) for c in start_direction]:
        raise RouteEditError("route_edit_port_tangent", "the first leg would not leave along the start port's "
                             "outward direction")
    if _direction(legs[-1][0], -legs[-1][1]) != [float(c) for c in end_direction]:
        raise RouteEditError("route_edit_port_tangent", "the last leg would not enter along the end port's "
                             "inward direction")
    for index, (_, _, length) in enumerate(legs):
        if length < MIN_LEG_M:
            raise RouteEditError("route_edit_short_leg", f"leg {index} would be shorter than "
                                 f"{MIN_LEG_M * 1000:g} mm")
    radius = outside_diameter_m / 2
    for i, cut in enumerate(_cuts(legs, nominal_diameter_mm)):
        if 0 < i < len(legs) and cut <= radius * RADIUS_MARGIN:
            raise RouteEditError("route_edit_bend_room", f"corner {i} would have no room for a bend larger than "
                                 f"the pipe radius ({cut * 1000:.1f} mm <= {radius * RADIUS_MARGIN * 1000:.1f} mm)")
    for i in range(len(legs)):
        for j in range(i + 2, len(legs)):
            if _gap(points[i], points[i + 1], points[j], points[j + 1]) < outside_diameter_m:
                raise RouteEditError("route_edit_self_clearance", f"legs {i} and {j} would pass closer than one "
                                     "pipe diameter")
    if obstacles:
        envelope = clearance.route_envelope(points, outside_diameter_m, nominal_diameter_mm, start_direction,
                                            end_direction)
        found = clearance.intrusions(envelope, obstacles)
        if found:
            names = sorted({f"{f.get('tag', '?')} {f.get('component', '')}".strip() for f in found})
            raise RouteEditError("route_edit_intrusion", "the edited route would enter equipment bodies ("
                                 f"{clearance.MODEL}): {', '.join(names)}", details=found)


def move_points(points, start_direction, end_direction, segment_index, offset_m, outside_diameter_m,
                nominal_diameter_mm, obstacles=()):
    """Move straight section ``segment_index`` by ``offset_m`` (algorithm step 5-6); return the new points.

    ``start_direction``/``end_direction`` are the ports' outward world axis
    unit vectors. ``obstacles`` are ``{box: [[min xyz], [max xyz]], ...}``
    records (``skid.clearance.body_boxes``); empty skips the body screen.
    """
    vertices = simplify(points)
    legs = _legs(vertices)
    n = len(legs)
    if isinstance(segment_index, bool) or not isinstance(segment_index, int) or not 0 <= segment_index < n:
        raise RouteEditError("route_edit_unknown_section", f"section {segment_index!r} does not exist; the route "
                             f"has {n} straight section(s) 0-{n - 1}")
    k = segment_index
    m, d = _offset(offset_m)
    axis, sign, length = legs[k]
    if m == axis:
        raise RouteEditError("route_edit_offset_parallel", f"section {k} runs along axis {'xyz'[axis]}; an offset "
                             "must be perpendicular to it")
    if abs(d) < MIN_OFFSET_M:
        raise RouteEditError("route_edit_offset_too_small", f"offset {d:g} m is below the minimum "
                             f"{MIN_OFFSET_M:g} m ({MIN_OFFSET_M * 1000:g} mm)")
    if abs(d) > MAX_OFFSET_M:
        raise RouteEditError("route_edit_offset_too_large", f"offset {d:g} m exceeds {MAX_OFFSET_M:g} m")
    stubs = (k == 0) + (k == n - 1)
    if stubs and not length > stubs * TERMINAL_STUB_M:
        raise RouteEditError("route_edit_stub_too_short", f"terminal section {k} is {length:.4f} m long; moving it "
                             f"keeps a fixed {TERMINAL_STUB_M:g} m nozzle stub at each port end, so it must be "
                             f"longer than {stubs * TERMINAL_STUB_M:g} m")
    if k == 0:
        stub = _shifted(vertices[0], axis, sign * TERMINAL_STUB_M)
        head, moved_start = [vertices[0], stub], _shifted(stub, m, d)
    else:
        moved_start = _shifted(vertices[k], m, d)
        head = vertices[:k] if legs[k - 1][0] == m else vertices[:k + 1]
    if k == n - 1:
        stub = _shifted(vertices[n], axis, -sign * TERMINAL_STUB_M)
        tail, moved_end = [stub, vertices[n]], _shifted(stub, m, d)
    else:
        moved_end = _shifted(vertices[k + 1], m, d)
        tail = vertices[k + 2:] if legs[k + 1][0] == m else vertices[k + 1:]
    candidate = simplify(head + [moved_start, moved_end] + tail)
    check_points(candidate, start_direction, end_direction, outside_diameter_m, nominal_diameter_mm, obstacles)
    return candidate


# --- Project wrappers -------------------------------------------------------------


def port_frame(project, port_id) -> dict:
    """World ``{id, entity_id, position_m, direction}`` of a port whose outward direction is a world axis."""
    model = project["model"]
    port = model["ports"].get(port_id) or {}
    entity_id = port.get("entity_id")
    placement = project["physical"]["placements"].get(entity_id) or {}
    if placement.get("status") != "placed":
        raise RouteEditError("route_edit_unplaced_endpoint", f"{entity_id} is not placed", [entity_id, port_id])
    try:
        position = world_port_position(project, port_id)
    except (ArithmeticError, LookupError, TypeError, ValueError) as exc:
        position, problem = None, exc
    else:
        problem = "no world position"
    direction = clearance.port_direction(project, port_id)
    if position is None or direction is None:
        raise RouteEditError("route_edit_frame_unavailable", f"port {port_id} has no resolvable frame ({problem})",
                             [entity_id, port_id])
    snapped = [0.0, 0.0, 0.0]
    axes = [i for i in range(3) if abs(direction[i]) > 1e-12]
    if len(axes) != 1 or abs(abs(direction[axes[0]]) - 1.0) > 1e-12:
        raise RouteEditError("route_edit_port_direction_unsupported", f"port {port_id} faces {direction}, not a "
                             "world axis; section moves support axis-parallel ports only", [entity_id, port_id])
    snapped[axes[0]] = math.copysign(1.0, direction[axes[0]])
    return {"id": port_id, "entity_id": entity_id, "position_m": [float(v) for v in position], "direction": snapped}


def _route(project, connection_id):
    connection = project["model"]["connections"].get(connection_id)
    if connection is None:
        raise RouteEditError("unknown_reference", f"connection {connection_id!r} does not exist", [connection_id])
    route = project["physical"]["routes"].get(connection_id)
    if route is None or not route.get("points_m"):
        raise RouteEditError("route_edit_no_route", f"connection {connection_id} has no stored route points",
                             [connection_id])
    return connection, route


def straight_sections(project, connection_id) -> list[dict]:
    """Section records (algorithm step 4) of a connection's stored route, using its nominal diameter."""
    connection, route = _route(project, connection_id)
    try:
        return section_records(route["points_m"], connection["nominal_diameter_mm"])[1]
    except RouteEditError as exc:
        raise RouteEditError(exc.code, f"{connection_id}: {exc.message}", [connection_id]) from None


def move_section(project, connection_id, segment_index, offset_m) -> list[list[float]]:
    """New ``points_m`` for moving one straight section of a current route, screened against every placed body.

    The route must be current; both port positions and outward directions
    come from the current project. Raises ``RouteEditError`` (``code``,
    ``details`` holding body intrusions) or ``clearance.ClearanceError`` for
    a body that cannot be bounded.
    """
    connection, route = _route(project, connection_id)
    if route.get("status") != "current":
        raise RouteEditError("route_edit_not_current", f"{connection_id} is {route.get('status')}; reroute it or "
                             "submit a manual route before moving its sections", [connection_id])
    start = port_frame(project, connection["from_port_id"])
    end = port_frame(project, connection["to_port_id"])
    obstacles = clearance.body_boxes(project)
    try:
        points = move_points(route["points_m"], start["direction"], end["direction"], segment_index, offset_m,
                             connection["outside_diameter_m"], connection["nominal_diameter_mm"], obstacles)
    except RouteEditError as exc:
        raise RouteEditError(exc.code, f"{connection_id}: {exc.message}", [connection_id, *exc.ids],
                             exc.details) from None
    return points
