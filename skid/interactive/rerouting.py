"""Deterministic guided rerouting of one physical connection (prototype DN50 orthogonal routes).

``plan_reroute(project, connection_id)`` derives both endpoint frames (world
position and outward unit direction) from the authoritative current project:
the entity's placement record plus its built-in template or composed
definition, the same sources the core validator and the CAD/display
compiler use. Installed scenes, published CAD and previews are never read.

A bounded, fixed candidate set of axis-aligned paths is generated (a direct
straight run, outward stubs of several lengths including stubs that stop at
the other port's coordinate or at the midpoint of a short facing gap, an
optional sideways detour, and every axis order between the stubs). Each
candidate is checked against the rules of the CAD pipe compiler
(``skid.geometry.kernel.pipe`` with ``skid.geometry.routes``): exact
endpoints, axis-parallel segments, first segment leaving along the start
port's outward direction and last segment entering along the end port's
inward direction, no reversal, and room at every corner for a bend larger
than 1.01 x the pipe radius (bend radius 1.5 DN, reduced to 0.49 x the
shorter adjacent run). Non-adjacent runs must stay at least one outside
diameter apart. Every candidate that passes is screened against the placed
equipment bodies (``skid.clearance``: conservative axis-aligned boxes of the
swept pipe, its bends and route-side end flanges against template primitives
and composed definition parts, endpoint owners and their nozzles included)
and rejected when it enters one. The best
remaining candidate is chosen by a fixed ranking (``_rank``). Other pipes,
supports, stress, access and fabrication rules are not checked, and an entity
whose geometry cannot be bounded refuses the reroute; this is not a plant
router.

Pure standard library plus ``skid.templates``/``skid.equipment``/
``skid.clearance``; no CAD import, no I/O, no locks. Rejections raise
``RerouteError``.
"""

from __future__ import annotations

import itertools
import math
import re

from .. import clearance
from ..equipment import DefinitionError, is_composed, resolve_definition
from ..templates import entity_primitives, local_port_position, world_port_position

__all__ = ["RerouteError", "port_frame", "path_report", "plan_reroute", "reroute_command",
           "BEND_RADIUS_FACTOR", "SUPPORTED_DN"]

SUPPORTED_DN = 50
BEND_RADIUS_FACTOR = 1.5  # bend centreline radius / DN, as skid.geometry.routes
CORNER_ROOM = 0.49  # share of the shorter adjacent run a corner may use, as skid.geometry.kernel.pipe
RADIUS_MARGIN = 1.01  # a bend must exceed this multiple of the pipe radius, as skid.geometry.kernel.pipe
JOINT_DEPTH_MM = 48.0  # DN50 route flange thickness 18 + hub 30 (skid.geometry.equipment/routes)
STUBS_M = (0.16, 0.25, 0.4)  # 0.16 m leaves a full 75 mm bend clear of the 48 mm flange joint
DETOURS_M = (0.2, 0.4)
MAX_POINTS = 8
MIN_SEGMENT_M = 0.001
TOLERANCE_M = 1e-6  # core ROUTE_TOLERANCE_M
EXACT = 1e-9
_QUARTER_TURNS = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))


class RerouteError(Exception):
    """A refused reroute: HTTP ``status``, diagnostic ``code``, message and the IDs involved."""

    def __init__(self, status, code, message, ids=()):
        super().__init__(message)
        self.status, self.code, self.message, self.ids = status, code, message, list(ids)

    @property
    def diagnostic(self):
        return {"code": self.code, "severity": "error", "entity_ids": list(self.ids), "message": self.message}


# --- Endpoint frames ------------------------------------------------------------


def _quarter(degrees):
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)) or not math.isfinite(degrees):
        return None
    quarters, remainder = divmod(float(degrees), 90.0)
    return int(quarters) % 4 if remainder == 0 else None


def _rotate(turns, vector):
    """``R = Rz @ Ry @ Rx`` by quarter turns (the template convention), exact for axis vectors."""
    x, y, z = vector
    c, s = _QUARTER_TURNS[turns[0]]
    y, z = y * c - z * s, y * s + z * c
    c, s = _QUARTER_TURNS[turns[1]]
    x, z = x * c + z * s, -x * s + z * c
    c, s = _QUARTER_TURNS[turns[2]]
    x, y = x * c - y * s, x * s + y * c
    return [v + 0.0 for v in (x, y, z)]


def _axis_vector(vector):
    """The exact signed unit axis a vector lies on, or ``None``."""
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        return None
    moving = [i for i in range(3) if abs(vector[i]) > EXACT]
    if len(moving) != 1 or abs(abs(vector[moving[0]]) - 1.0) > EXACT:
        return None
    unit = [0.0, 0.0, 0.0]
    unit[moving[0]] = math.copysign(1.0, vector[moving[0]])
    return unit


def _local_direction(entity, port_name):
    if is_composed(entity):
        ports = resolve_definition(entity)["ports"]
        if port_name not in ports:
            raise KeyError(f"definition has no port {port_name!r}")
        return list(ports[port_name]["direction"])
    for primitive in entity_primitives(entity):
        if primitive["key"] == f"nozzle/{port_name}":
            turns = [_quarter(a) for a in primitive["rotation_deg"]]
            if None in turns:
                return None
            return _rotate(turns, (0.0, 0.0, 1.0))
    raise KeyError(f"template {entity.get('kind')!r} has no nozzle for port {port_name!r}")


def port_frame(project, port_id) -> dict:
    """World ``{id, entity_id, name, position_m, direction}`` of a port in the current project.

    Raises ``RerouteError`` for an unplaced owner, a placement rotation that is not a whole number of
    quarter turns about each axis, or an outward direction that is not parallel to a world axis.
    """
    model = project["model"]
    port = model["ports"].get(port_id)
    entity = model["entities"].get(port["entity_id"]) if port else None
    if entity is None:
        raise RerouteError(400, "reroute_unknown_port", f"port {port_id!r} or its entity does not exist", [port_id])
    eid, tag = entity["id"], entity.get("tag", entity["id"])
    placement = project["physical"]["placements"].get(eid)
    if not placement or placement.get("status") != "placed":
        reason = (placement or {}).get("reason") or "no placement record"
        raise RerouteError(400, "reroute_unplaced_endpoint", f"{tag} ({eid}) is not placed ({reason}); place it "
                           "before rerouting", [eid, port_id])
    turns = [_quarter(a) for a in placement.get("rotation_deg") or (None,)]
    if len(turns) != 3 or None in turns:
        raise RerouteError(400, "reroute_unsupported_rotation", f"{tag} ({eid}) has rotation "
                           f"{placement.get('rotation_deg')}; guided rerouting supports whole quarter turns only",
                           [eid, port_id])
    try:
        position = world_port_position(project, port_id)
        local_position = local_port_position(project, port_id)
        local_direction = _local_direction(entity, port["name"])
    except (DefinitionError, KeyError, ValueError, TypeError, ArithmeticError) as exc:
        raise RerouteError(400, "reroute_frame_unavailable", f"{tag}.{port['name']} has no resolvable port frame "
                           f"({exc})", [eid, port_id]) from None
    local_axis = _axis_vector(local_direction)
    direction = _axis_vector(_rotate(turns, local_axis)) if local_axis else None
    if direction is None:
        raise RerouteError(400, "reroute_unsupported_direction", f"{tag}.{port['name']} faces {local_direction} "
                           "locally, not along a world axis; guided rerouting supports axis-parallel ports only",
                           [eid, port_id])
    # Self-check: the quarter-turn convention here must reproduce the template's world position.
    rotated = _rotate(turns, local_position)
    expected = [p + o for p, o in zip(placement["position_m"], rotated)]
    if position is None or math.dist(expected, position) > TOLERANCE_M:
        raise RerouteError(500, "reroute_frame_mismatch", f"{tag}.{port['name']} world frame disagrees with its "
                           "template position", [eid, port_id])
    return {"id": port_id, "entity_id": eid, "name": port["name"], "position_m": [float(v) for v in position],
            "direction": direction}


# --- Path rules -----------------------------------------------------------------


def _segments(points):
    out = []
    for a, b in zip(points, points[1:]):
        moving = [i for i in range(3) if abs(b[i] - a[i]) > TOLERANCE_M]
        if len(moving) != 1 or any(abs(b[i] - a[i]) > EXACT for i in range(3) if i not in moving):
            out.append(None)
            continue
        axis = moving[0]
        unit = [0.0, 0.0, 0.0]
        unit[axis] = math.copysign(1.0, b[axis] - a[axis])
        out.append((unit, abs(b[axis] - a[axis])))
    return out


def _segment_distance(a0, a1, b0, b1):
    """Exact distance between two axis-parallel segments (each is its own degenerate box)."""
    total = 0.0
    for i in range(3):
        lo1, hi1 = sorted((a0[i], a1[i]))
        lo2, hi2 = sorted((b0[i], b1[i]))
        gap = max(0.0, lo2 - hi1, lo1 - hi2)
        total += gap * gap
    return math.sqrt(total)


def path_report(points, start, end, outside_diameter_m, nominal_diameter_mm) -> dict:
    """Check a path against the CAD pipe compiler's rules; ``{problems, bends, ...}``.

    ``start``/``end`` are port frames. ``problems`` empty means the pipe sweep can be built with
    endpoint tangents along the port axes. ``joint_short`` counts end runs whose straight part
    (after the bend tangent) is shorter than the flange joint depth; that is a preference, not a
    compiler rule.
    """
    problems = []
    pipe_radius = outside_diameter_m * 500.0
    bend = BEND_RADIUS_FACTOR * nominal_diameter_mm
    report = {"problems": problems, "bends": 0, "bend_radii_mm": [], "reduced_bends": 0, "joint_short": 0,
              "behind": 0, "length_m": 0.0}
    if len(points) < 2:
        problems.append("a route needs at least two points")
        return report
    if len(points) > MAX_POINTS:
        problems.append(f"more than {MAX_POINTS} points")
    if list(points[0]) != list(start["position_m"]) or list(points[-1]) != list(end["position_m"]):
        problems.append("the path does not begin and end exactly at the port positions")
    segments = _segments(points)
    if None in segments:
        problems.append(f"segment {segments.index(None)} is not parallel to one axis or has zero length")
        return report
    lengths = [length for _, length in segments]
    report["length_m"] = round(sum(lengths), 9)
    if min(lengths) < MIN_SEGMENT_M:
        problems.append(f"segment {lengths.index(min(lengths))} is shorter than {MIN_SEGMENT_M * 1000:g} mm")
    if segments[0][0] != start["direction"]:
        problems.append("the first segment does not leave along the start port's outward direction")
    if segments[-1][0] != [-c for c in end["direction"]]:
        problems.append("the last segment does not enter along the end port's inward direction")
    cuts = [0.0] * len(points)
    for i in range(1, len(points) - 1):
        (d0, l0), (d1, l1) = segments[i - 1], segments[i]
        cosine = sum(a * b for a, b in zip(d0, d1))
        if cosine > 0.5:
            continue  # straight through: the compiler makes no bend
        if cosine < -0.5:
            problems.append(f"the path reverses direction at point {i}")
            continue
        radius = min(bend, CORNER_ROOM * min(l0, l1) * 1000.0)  # tan(45 deg) = 1 for a right-angle corner
        if radius <= pipe_radius * RADIUS_MARGIN:
            problems.append(f"corner {i} has no room for a bend larger than the pipe radius "
                            f"({radius:.1f} mm <= {pipe_radius * RADIUS_MARGIN:.1f} mm)")
            continue
        cuts[i] = radius
        report["bends"] += 1
        report["bend_radii_mm"].append(round(radius, 6))
        report["reduced_bends"] += radius < bend - 1e-9
    if len(points) > 2:
        report["joint_short"] = int(lengths[0] * 1000.0 - cuts[1] < JOINT_DEPTH_MM) + int(
            lengths[-1] * 1000.0 - cuts[-2] < JOINT_DEPTH_MM)
    for i, j in itertools.combinations(range(len(segments)), 2):
        if j - i >= 2 and _segment_distance(points[i], points[i + 1], points[j], points[j + 1]) < outside_diameter_m:
            problems.append(f"segments {i} and {j} pass closer than one pipe diameter")
            break
    for p in points[1:-1]:
        report["behind"] += sum(1 for frame in (start, end) if sum(
            (p[k] - frame["position_m"][k]) * frame["direction"][k] for k in range(3)) < -EXACT)
    return report


# --- Candidates -----------------------------------------------------------------


def _axis_of(direction):
    axis = next(i for i in range(3) if direction[i] != 0.0)
    return axis, direction[axis]


def _moved(point, axis, value):
    q = list(point)
    q[axis] = value
    return q


def _stubs(frame, other):
    """``(length, coordinate)`` choices for a port's outward stub, in a fixed order."""
    axis, sign = _axis_of(frame["direction"])
    p, q = frame["position_m"][axis], other["position_m"][axis]
    ahead = (q - p) * sign
    choices = [(s, round(p + sign * s, 6) + 0.0) for s in STUBS_M]
    if ahead > TOLERANCE_M:
        choices.append((ahead, q))  # stop level with the other port
        choices.append((ahead / 2, (p + q) / 2))  # meet a facing port halfway across a short gap
    seen, out = set(), []
    for length, value in choices:
        if value not in seen:
            seen.add(value)
            out.append(_moved(frame["position_m"], axis, value))
    return out


def _simplify(points):
    """Drop repeated points and merge same-direction collinear runs; keep both ends exactly."""
    out = []
    for p in points:
        if out and all(abs(a - b) <= EXACT for a, b in zip(out[-1], p)):
            if len(out) > 1:
                out[-1] = p
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        same = [k for k in range(3) if abs(a[k] - b[k]) <= EXACT and abs(b[k] - c[k]) <= EXACT]
        between = all(min(a[k], c[k]) - EXACT <= b[k] <= max(a[k], c[k]) + EXACT for k in range(3))
        if len(same) >= 2 and between:
            del out[i]
        else:
            i += 1
    return out


def candidates(start, end):
    """Yield candidate point lists in a fixed deterministic order (bounded: about 1,500)."""
    a, b = start["position_m"], end["position_m"]
    yield [list(a), list(b)]
    start_axis, _ = _axis_of(start["direction"])
    for a1, b1 in itertools.product(_stubs(start, end), _stubs(end, start)):
        detours = [None]
        for axis in range(3):
            if axis == start_axis:
                continue
            for side, margin in itertools.product((1, -1), DETOURS_M):
                edge = max(a1[axis], b1[axis]) if side > 0 else min(a1[axis], b1[axis])
                detours.append(_moved(a1, axis, round(edge + side * margin, 6) + 0.0))
        for detour in detours:
            head = [list(a), a1] + ([detour] if detour is not None else [])
            for order in itertools.permutations(range(3)):
                path, cursor = list(head), head[-1]
                for axis in order:
                    if abs(cursor[axis] - b1[axis]) > EXACT:
                        cursor = _moved(cursor, axis, b1[axis])
                        path.append(cursor)
                yield _simplify(path + [b1, list(b)])


def _rank(report, index):
    """Fewest flange-joint and behind-port compromises, then fewest bends (a detour only when it is needed for
    bend room), then fewest reduced-radius bends, then shortest, then generation order."""
    return (report["joint_short"], report["behind"], report["bends"], report["reduced_bends"],
            round(report["length_m"], 6), index)


def plan_reroute(project, connection_id) -> dict:
    """Choose the guided route for one connection of the current project, or raise ``RerouteError``."""
    model, physical = project["model"], project["physical"]
    connection = model["connections"].get(connection_id)
    if connection is None:
        raise RerouteError(400, "reroute_unknown_connection", f"connection {connection_id!r} does not exist in "
                           f"revision {project['revision']}", [connection_id])
    tag = connection.get("tag") or connection_id
    route = physical["routes"].get(connection_id)
    if route is not None and route.get("status") == "current":
        raise RerouteError(409, "route_current", f"{tag} ({connection_id}) already has a current {route.get('mode')} "
                           "route; it was not changed", [connection_id])
    sizes = {connection.get("nominal_diameter_mm")} | {
        model["ports"].get(p, {}).get("nominal_diameter_mm") for p in (connection["from_port_id"],
                                                                       connection["to_port_id"])}
    outside = connection.get("outside_diameter_m")
    if sizes != {SUPPORTED_DN} or isinstance(outside, bool) or not isinstance(outside, (int, float)) \
            or not 0 < outside < 0.2:
        raise RerouteError(400, "reroute_unsupported_size", f"{tag} ({connection_id}): guided rerouting supports "
                           f"DN{SUPPORTED_DN} connections and ports only (found {sorted(map(str, sizes))})",
                           [connection_id])
    start = port_frame(project, connection["from_port_id"])
    end = port_frame(project, connection["to_port_id"])
    if math.dist(start["position_m"], end["position_m"]) < MIN_SEGMENT_M:
        raise RerouteError(400, "reroute_impossible", f"{tag} ({connection_id}): the two ports coincide",
                           [connection_id, start["id"], end["id"]])
    try:
        obstacles = clearance.body_boxes(project)
    except clearance.ClearanceError as exc:
        raise RerouteError(400, "reroute_clearance_unavailable", f"{tag} ({connection_id}): {exc.message}",
                           [connection_id, *exc.ids]) from None
    best, seen, checked, rejected, blocked = None, set(), 0, {}, {}
    for index, points in enumerate(candidates(start, end)):
        key = tuple(tuple(p) for p in points)
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        report = path_report(points, start, end, outside, SUPPORTED_DN)
        if report["problems"]:
            first = re.sub(r"\d+", "N", report["problems"][0].split(" (")[0])
            rejected[first] = rejected.get(first, 0) + 1
            continue
        found = clearance.intrusions(clearance.route_envelope(points, outside, SUPPORTED_DN, start["direction"],
                                                              end["direction"]), obstacles)
        if found:
            reason = "enters an equipment body"
            rejected[reason] = rejected.get(reason, 0) + 1
            for tag_ in {f["tag"] for f in found}:
                blocked[tag_] = blocked.get(tag_, 0) + 1
            continue
        rank = _rank(report, index)
        if best is None or rank < best[0]:
            best = (rank, points, report)
    if best is None:
        common = sorted(rejected.items(), key=lambda item: (-item[1], item[0]))[:3]
        reasons = "; ".join(f"{reason} ({count})" for reason, count in common)
        bodies = "; blocked by " + ", ".join(f"{t} ({n})" for t, n in sorted(blocked.items())) if blocked else ""
        raise RerouteError(400, "reroute_impossible", f"{tag} ({connection_id}): none of {checked} bounded "
                           f"orthogonal candidates fits the pipe compiler's rules and clears the equipment bodies "
                           f"({reasons}{bodies}); move an endpoint or enter a manual route",
                           [connection_id, start["id"], end["id"]])
    _, points, report = best
    return {"connection_id": connection_id, "tag": tag, "points_m": [list(p) for p in points],
            "from": start, "to": end, "bends": report["bends"], "bend_radii_mm": report["bend_radii_mm"],
            "reduced_bends": report["reduced_bends"], "length_m": report["length_m"],
            "candidates_checked": checked, "candidates_blocked_by_bodies": rejected.get("enters an equipment body", 0),
            "clearance": {"model": clearance.MODEL, "bodies_checked": len(obstacles)},
            "previous": None if route is None else {
                "mode": route.get("mode"), "status": route.get("status")}}


def reroute_command(project, plan, command_id, assumption_id, stamp) -> dict:
    """The existing ``set_route`` envelope for a plan, with its own guided-prototype assumption."""
    radii = ", ".join(f"{r:g}" for r in plan["bend_radii_mm"]) or "none"
    description = (f"Guided prototype route for {plan['tag']} ({plan['connection_id']}) generated by the local "
                   f"deterministic rerouting helper against revision {project['revision']}: axis-parallel DN50 path "
                   f"between the current port frames, leaving and entering along the port axes, {plan['bends']} "
                   f"bend(s) (radius mm: {radii}; 1.5 DN, reduced only where a run is short). Screened clear of the "
                   f"placed equipment bodies as {clearance.MODEL} (template primitives and composed definition "
                   "parts; detailed CAD features beyond them are not screened). Other pipes, supports, stress and "
                   "fabrication are not checked.")
    return {"id": command_id, "expected_revision": project["revision"], "type": "set_route",
            "payload": {"connection_id": plan["connection_id"], "points_m": plan["points_m"], "mode": "guided",
                        "assumption_id": assumption_id},
            "assumptions": {assumption_id: {"description": description,
                                            "source": f"local guided rerouting (skid.interactive.rerouting), {stamp}"}}}
