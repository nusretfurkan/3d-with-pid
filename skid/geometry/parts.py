"""Reusable recipe parts: a closed vocabulary compiled into millimetre solids.

A recipe component is a list of part calls ``{"part": name, ...arguments}``.
Primitive parts map one-to-one onto ``kernel`` builders; array parts repeat an
``item`` call as located copies that share geometry; composite parts (flanges,
bolting, nozzles, a volute casing, a finned frame, a handwheel) expand into
primitives. Every part validates its arguments strictly and returns a list of
OCCT solids. No Boolean operations are used, so cost grows linearly.

The same calls drive the CAD export and the display tessellation. Nothing in
this module knows about project IDs, tags or semantics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from . import kernel as k

__all__ = ["PARTS", "PartError", "build", "part_names"]


class PartError(ValueError):
    """A part call is malformed or cannot be built."""


@dataclass(frozen=True)
class _Part:
    builder: Callable
    required: tuple[str, ...]
    optional: dict


PARTS: dict[str, _Part] = {}


def part(name, required=(), **optional):
    def register(fn):
        PARTS[name] = _Part(fn, tuple(required), dict(optional))
        return fn

    return register


def part_names() -> list[str]:
    return sorted(PARTS)


def build(call, path="part") -> list:
    """Compile one part call into a list of solids."""
    if not isinstance(call, dict) or not isinstance(call.get("part"), str):
        raise PartError(f"{path}: a part call must be an object with a 'part' name")
    name = call["part"]
    spec = PARTS.get(name)
    if spec is None:
        raise PartError(f"{path}: unknown part {name!r}; known parts: {', '.join(part_names())}")
    args = {key: value for key, value in call.items() if key != "part"}
    missing = [key for key in spec.required if key not in args]
    unknown = sorted(set(args) - set(spec.required) - set(spec.optional))
    if missing or unknown:
        raise PartError(f"{path} ({name}): missing arguments {missing}, unsupported arguments {unknown}")
    values = dict(spec.optional)
    values.update(args)
    try:
        shapes = spec.builder(path=f"{path}/{name}", **values)
    except (PartError, k.KernelError) as exc:
        if str(exc).startswith(path):
            raise
        raise PartError(f"{path} ({name}): {exc}") from exc
    return shapes


# --- Small vector helpers ----------------------------------------------------


def _v(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PartError(f"{label} must be three numbers, not {value!r}")
    out = []
    for c in value:
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c):
            raise PartError(f"{label} must be three finite numbers, not {value!r}")
        out.append(float(c))
    return tuple(out)


def _n(value, label, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PartError(f"{label} must be a finite number, not {value!r}")
    if positive and value <= 0:
        raise PartError(f"{label} must be positive, not {value!r}")
    if nonnegative and value < 0:
        raise PartError(f"{label} must not be negative, not {value!r}")
    return float(value)


def _count(value, label, minimum=1, maximum=512):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PartError(f"{label} must be an integer in [{minimum}, {maximum}], not {value!r}")
    return value


def _unit(value, label):
    x, y, z = _v(value, label)
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        raise PartError(f"{label} must be a nonzero vector")
    return (x / length, y / length, z / length)


def _along(point, direction, distance):
    return tuple(p + d * distance for p, d in zip(point, direction))


def _rotation_about(axis, degrees):
    """3x3 rotation about a unit axis (Rodrigues), exact for quarter turns about world axes."""
    x, y, z = axis
    quarters, remainder = divmod(degrees, 90.0)
    if remainder == 0:
        c, s = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(quarters) % 4]
    else:
        r = math.radians(degrees)
        c, s = math.cos(r), math.sin(r)
    t = 1 - c
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def _rigid(rotation, origin, translation=(0.0, 0.0, 0.0)):
    """Matrix rotating about ``origin`` then translating."""
    rows = []
    for i in range(3):
        offset = origin[i] - sum(rotation[i][j] * origin[j] for j in range(3)) + translation[i]
        rows.append(tuple(rotation[i]) + (offset,))
    return tuple(rows)


# --- Primitives --------------------------------------------------------------


@part("box", required=("center", "size"), rotation_deg=(0.0, 0.0, 0.0))
def _box(path, center, size, rotation_deg):
    return [k.box(center, size, rotation_deg)]


@part("cylinder", required=("start", "end", "radius"))
def _cylinder(path, start, end, radius):
    return [k.cylinder(start, end, radius)]


@part("cone", required=("start", "end", "radius_start", "radius_end"))
def _cone(path, start, end, radius_start, radius_end):
    return [k.cone(start, end, radius_start, radius_end)]


@part("sphere", required=("center", "radius"))
def _sphere(path, center, radius):
    return [k.sphere(center, radius)]


@part("torus", required=("center", "axis", "major_radius", "minor_radius"))
def _torus(path, center, axis, major_radius, minor_radius):
    return [k.torus(center, axis, major_radius, minor_radius)]


@part("revolve", required=("origin", "axis", "profile"), ref=None, angle_deg=360.0, start_deg=0.0)
def _revolve(path, origin, axis, profile, ref, angle_deg, start_deg):
    return [k.revolve(origin, axis, ref, profile, angle_deg, start_deg)]


@part("prism", required=("origin", "normal", "outline", "depth"), u_axis=None, holes=(), corner_radius=0.0)
def _prism(path, origin, normal, outline, depth, u_axis, holes, corner_radius):
    if not isinstance(holes, (list, tuple)):
        raise PartError("holes must be a list")
    return [k.prism(origin, normal, outline, depth, u_axis, holes, corner_radius)]


@part("hex_prism", required=("start", "axis", "across_flats", "height"), phase_deg=0.0)
def _hex_prism(path, start, axis, across_flats, height, phase_deg):
    return [k.hex_prism(start, axis, across_flats, height, phase_deg)]


@part("loft", required=("sections",), ruled=False)
def _loft(path, sections, ruled):
    if not isinstance(ruled, bool):
        raise PartError("ruled must be true or false")
    return [k.loft(sections, ruled)]


@part("pipe", required=("points", "radius", "bend_radius"))
def _pipe(path, points, radius, bend_radius):
    solid, _ = k.pipe(points, radius, bend_radius)
    return [solid]


# --- Arrays ------------------------------------------------------------------


@part("polar_array", required=("item", "origin", "axis"), count=None, angles_deg=None, start_deg=0.0)
def _polar_array(path, item, origin, axis, count, angles_deg, start_deg):
    origin, axis = _v(origin, "origin"), _unit(axis, "axis")
    if (count is None) == (angles_deg is None):
        raise PartError("give exactly one of count or angles_deg")
    if count is not None:
        n = _count(count, "count")
        start = _n(start_deg, "start_deg")
        angles = [start + 360.0 * i / n for i in range(n)]
    else:
        if not isinstance(angles_deg, (list, tuple)) or not angles_deg:
            raise PartError("angles_deg must be a nonempty list")
        angles = [_n(a, "angles_deg") for a in angles_deg]
    shapes = build(item, f"{path}/item")
    return [k.located(s, _rigid(_rotation_about(axis, a), origin)) for a in angles for s in shapes]


@part("linear_array", required=("item", "count", "step"))
def _linear_array(path, item, count, step):
    n, step = _count(count, "count"), _v(step, "step")
    shapes = build(item, f"{path}/item")
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    out = []
    for i in range(n):
        matrix = _rigid(identity, (0.0, 0.0, 0.0), tuple(c * i for c in step))
        out += [k.located(s, matrix) for s in shapes]
    return out


@part("instances", required=("item", "offsets"))
def _instances(path, item, offsets):
    if not isinstance(offsets, (list, tuple)) or not offsets:
        raise PartError("offsets must be a nonempty list of [x, y, z]")
    shapes = build(item, f"{path}/item")
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    out = []
    for index, offset in enumerate(offsets):
        matrix = _rigid(identity, (0.0, 0.0, 0.0), _v(offset, f"offsets[{index}]"))
        out += [k.located(s, matrix) for s in shapes]
    return out


# --- Composite parts ---------------------------------------------------------


def _bolt_angles(count, phase_deg):
    return [phase_deg + 360.0 * i / count for i in range(count)]


@part("tube", required=("start", "end", "outer_radius", "inner_radius"))
def _tube(path, start, end, outer_radius, inner_radius):
    start, end = _v(start, "start"), _v(end, "end")
    ro = _n(outer_radius, "outer_radius", positive=True)
    ri = _n(inner_radius, "inner_radius", positive=True)
    if ri >= ro:
        raise PartError("inner_radius must be smaller than outer_radius")
    axis = tuple(e - s for s, e in zip(start, end))
    length = math.sqrt(sum(c * c for c in axis))
    if length < 1e-6:
        raise PartError("tube start and end coincide")
    return [k.revolve(start, axis, None, [[ri, 0.0], [ro, 0.0], [ro, length], [ri, length]])]


@part("flange", required=("face_center", "normal", "outer_radius", "thickness", "bore_radius", "bolt_count",
                          "bolt_circle_radius", "hole_radius"),
      phase_deg=45.0, hub_radius=0.0, hub_length=0.0, neck_radius=0.0)
def _flange(path, face_center, normal, outer_radius, thickness, bore_radius, bolt_count, bolt_circle_radius,
            hole_radius, phase_deg, hub_radius, hub_length, neck_radius):
    """A drilled flange whose joint face lies at ``face_center`` facing ``normal``.

    The plate occupies ``thickness`` behind the face; an optional hub tapers
    from ``hub_radius`` to ``neck_radius`` over ``hub_length`` further back.
    """
    face, n = _v(face_center, "face_center"), _unit(normal, "normal")
    ro = _n(outer_radius, "outer_radius", positive=True)
    t = _n(thickness, "thickness", positive=True)
    rb = _n(bore_radius, "bore_radius", nonnegative=True)
    count = _count(bolt_count, "bolt_count", 0, 64)
    pcd = _n(bolt_circle_radius, "bolt_circle_radius", positive=True)
    rh = _n(hole_radius, "hole_radius", positive=True)
    if not rb + 1.0 < pcd - rh and count:
        raise PartError("bolt holes overlap the bore")
    if pcd + rh >= ro - 1.0:
        raise PartError("bolt holes break out of the flange rim")
    back = _along(face, n, -t)
    holes = [{"center": [0.0, 0.0], "radius": rb}] if rb > 0 else []
    holes += [{"center": [pcd * math.cos(math.radians(a)), pcd * math.sin(math.radians(a))], "radius": rh}
              for a in _bolt_angles(count, _n(phase_deg, "phase_deg"))] if count else []
    shapes = [k.prism(back, n, {"circle": ro}, t, None, holes)]
    hl, hr, nr = (_n(hub_length, "hub_length", nonnegative=True), _n(hub_radius, "hub_radius", nonnegative=True),
                  _n(neck_radius, "neck_radius", nonnegative=True))
    if hl > 0:
        if not rb < nr < hr < pcd - rh:
            raise PartError("hub needs bore < neck_radius < hub_radius < bolt circle clearance")
        shapes.append(k.revolve(back, tuple(-c for c in n), None, [[rb, 0.0], [hr, 0.0], [nr, hl], [rb, hl]]))
    return shapes


@part("flanged_nozzle", required=("port", "direction", "neck_length", "neck_radius", "bore_radius", "flange"))
def _flanged_nozzle(path, port, direction, neck_length, neck_radius, bore_radius, flange):
    """A bored neck ending in a flange whose joint face is exactly at ``port``."""
    port, d = _v(port, "port"), _unit(direction, "direction")
    if not isinstance(flange, dict):
        raise PartError("flange must be an object of flange arguments")
    allowed = {"outer_radius", "thickness", "bolt_count", "bolt_circle_radius", "hole_radius", "phase_deg"}
    if set(flange) - allowed or {"outer_radius", "thickness", "bolt_count", "bolt_circle_radius",
                                 "hole_radius"} - set(flange):
        raise PartError(f"flange arguments must be {sorted(allowed)}")
    t = _n(flange["thickness"], "flange thickness", positive=True)
    length = _n(neck_length, "neck_length", positive=True)
    if length <= t:
        raise PartError("neck_length must exceed the flange thickness")
    shapes = _tube(path, _along(port, d, -length), _along(port, d, -t), neck_radius, bore_radius)
    call = dict(flange, face_center=list(port), normal=list(d), bore_radius=bore_radius)
    return shapes + _flange(path, **{**PARTS["flange"].optional, **call})


@part("bolting", required=("face_center", "normal", "thickness_back", "thickness_front", "bolt_count",
                           "bolt_circle_radius", "stud_radius", "nut_across_flats", "nut_height"),
      phase_deg=45.0, protrusion=4.0)
def _bolting(path, face_center, normal, thickness_back, thickness_front, bolt_count, bolt_circle_radius,
             stud_radius, nut_across_flats, nut_height, phase_deg, protrusion):
    """Studs through a flanged joint with a nut on each outer face."""
    face, n = _v(face_center, "face_center"), _unit(normal, "normal")
    tb = _n(thickness_back, "thickness_back", positive=True)
    tf = _n(thickness_front, "thickness_front", positive=True)
    h = _n(nut_height, "nut_height", positive=True)
    p = _n(protrusion, "protrusion", nonnegative=True)
    pcd = _n(bolt_circle_radius, "bolt_circle_radius", positive=True)
    u, v, _ = k.frame_axes(n)
    shapes = []
    for a in _bolt_angles(_count(bolt_count, "bolt_count"), _n(phase_deg, "phase_deg")):
        c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
        centre = tuple(face[i] + pcd * (c * u[i] + s * v[i]) for i in range(3))
        back, front = _along(centre, n, -tb), _along(centre, n, tf)
        shapes.append(k.cylinder(_along(back, n, -(h + p)), _along(front, n, h + p), stud_radius))
        shapes.append(k.hex_prism(back, tuple(-c_ for c_ in n), nut_across_flats, h, 30.0))
        shapes.append(k.hex_prism(front, n, nut_across_flats, h, 30.0))
    return shapes


@part("bolt", required=("seat", "axis", "across_flats", "height", "stud_radius", "stud_length"))
def _bolt(path, seat, axis, across_flats, height, stud_radius, stud_length):
    """A hex nut seated on a surface at ``seat`` with a stud end protruding along ``axis``."""
    seat, a = _v(seat, "seat"), _unit(axis, "axis")
    length = _n(stud_length, "stud_length", positive=True)
    return [k.hex_prism(seat, a, across_flats, height, 30.0),
            k.cylinder(_along(seat, a, -min(length, 2 * height)), _along(seat, a, length), stud_radius)]


@part("volute_casing", required=("center", "width", "tongue_radius", "outer_radius", "neck_half_width",
                                 "neck_top", "chamfer"), samples=16)
def _volute_casing(path, center, width, tongue_radius, outer_radius, neck_half_width, neck_top, chamfer, samples):
    """A spiral casing around a +x shaft whose throat rises to a flat neck top at ``neck_top``.

    The section grows from ``tongue_radius`` at the tongue to ``outer_radius``
    on the -y side, then a diffuser curve rises to the neck. Sections near
    the two faces are inset to round the edges by ``chamfer``; a ruled loft
    joins them, so the solid never bulges beyond its sections.
    """
    cx, cy, cz = _v(center, "center")
    w = _n(width, "width", positive=True)
    a0 = _n(tongue_radius, "tongue_radius", positive=True)
    b0 = _n(outer_radius, "outer_radius", positive=True)
    h0 = _n(neck_half_width, "neck_half_width", positive=True)
    top = _n(neck_top, "neck_top") - cz
    c = _n(chamfer, "chamfer", nonnegative=True)
    n = _count(samples, "samples", 6, 64)
    if not (h0 + c < a0 - c and a0 < b0 and top > b0 * 0.5 and 2 * c < w):
        raise PartError("volute needs neck_half_width < tongue_radius < outer_radius, a neck above the casing "
                        "and a chamfer smaller than half the width")

    def section(x, inset):
        a, b, h = a0 - inset, b0 - inset, h0 - inset
        vt = math.sqrt(a * a - h * h)
        t0, t1 = math.atan2(h, vt), 1.5 * math.pi
        spiral = []
        for i in range(n + 1):
            f = i / n
            theta = t0 + (t1 - t0) * f
            r = a + (b - a) * f
            spiral.append([r * math.sin(theta), r * math.cos(theta)])
        p0, p1, p2, p3 = (-b, 0.0), (-b, top * 0.45), (-h, top * 0.55), (-h, top)
        rise = []
        for i in range(1, 6):
            s = i / 5
            q = [(1 - s) ** 3 * p0[j] + 3 * (1 - s) ** 2 * s * p1[j] + 3 * (1 - s) * s * s * p2[j] + s ** 3 * p3[j]
                 for j in range(2)]
            rise.append(q)
        dr = (b - a) / (t1 - t0)
        tangent = [dr * math.sin(t0) + a * math.cos(t0), dr * math.cos(t0) - a * math.sin(t0)]
        return {
            "origin": [x, cy, cz], "normal": [1.0, 0.0, 0.0], "u_axis": [0.0, 1.0, 0.0],
            "lines": [[-h, top], [h, top], [h, vt]],
            "spline": spiral + rise,
            "spline_tangents": [tangent, [0.0, 1.0]],
        }

    # A ruled loft stays inside its sections; two facets per edge approximate a round
    # of radius ``chamfer`` (a smooth loft through unevenly spaced sections bulges).
    if c > 0:
        f = 1 - math.sqrt(0.5)
        xs = [cx - w / 2, cx - w / 2 + f * c, cx - w / 2 + c, cx + w / 2 - c, cx + w / 2 - f * c, cx + w / 2]
        insets = [c, f * c, 0.0, 0.0, f * c, c]
    else:
        xs, insets = [cx - w / 2, cx + w / 2], [0.0, 0.0]
    return [k.loft([section(x, i) for x, i in zip(xs, insets)], ruled=True)]


@part("finned_frame", required=("origin", "axis", "length", "core_radius", "fin_tip_radius", "fin_thickness",
                                 "fin_count"), up=(0.0, 0.0, 1.0), skip_center_deg=180.0, skip_half_width_deg=0.0,
      fin_inset=0.0)
def _finned_frame(path, origin, axis, length, core_radius, fin_tip_radius, fin_thickness, fin_count, up,
                  skip_center_deg, skip_half_width_deg, fin_inset):
    """A cylindrical core with radial cooling fins; fins near ``skip_center_deg`` are left out.

    Angles are measured about ``axis`` from ``up``.
    """
    o, a = _v(origin, "origin"), _unit(axis, "axis")
    length = _n(length, "length", positive=True)
    rc = _n(core_radius, "core_radius", positive=True)
    rt = _n(fin_tip_radius, "fin_tip_radius", positive=True)
    ft = _n(fin_thickness, "fin_thickness", positive=True)
    inset = _n(fin_inset, "fin_inset", nonnegative=True)
    if rt <= rc or 2 * inset >= length:
        raise PartError("fins need fin_tip_radius > core_radius and an inset shorter than half the length")
    upv = _unit(up, "up")
    if abs(sum(x * y for x, y in zip(upv, a))) > 1e-9:
        raise PartError("up must be perpendicular to axis")
    count = _count(fin_count, "fin_count", 3, 256)
    skip_c, skip_w = _n(skip_center_deg, "skip_center_deg"), _n(skip_half_width_deg, "skip_half_width_deg", nonnegative=True)
    shapes = [k.cylinder(o, _along(o, a, length), rc)]
    side = tuple(a[(i + 1) % 3] * upv[(i + 2) % 3] - a[(i + 2) % 3] * upv[(i + 1) % 3] for i in range(3))
    root = rc - min(2.0, (rt - rc) / 4)
    mid = _along(_along(o, a, length / 2), upv, (root + rt) / 2)
    fin = k.prism(_along(mid, side, -ft / 2), side,
                  [[-(length / 2 - inset), -(rt - root) / 2], [length / 2 - inset, -(rt - root) / 2],
                   [length / 2 - inset, (rt - root) / 2], [-(length / 2 - inset), (rt - root) / 2]], ft,
                  u_axis=a)
    for i in range(count):
        angle = 360.0 * i / count
        offset = (angle - skip_c + 180.0) % 360.0 - 180.0
        if abs(offset) < skip_w:
            continue
        shapes.append(k.located(fin, _rigid(_rotation_about(a, angle), o)))
    return shapes


@part("handwheel", required=("center", "axis", "rim_radius", "rim_thickness", "spokes", "hub_radius",
                             "hub_height"))
def _handwheel(path, center, axis, rim_radius, rim_thickness, spokes, hub_radius, hub_height):
    """A spoked handwheel in the plane through ``center`` perpendicular to ``axis``."""
    c, a = _v(center, "center"), _unit(axis, "axis")
    R = _n(rim_radius, "rim_radius", positive=True)
    t = _n(rim_thickness, "rim_thickness", positive=True)
    hr = _n(hub_radius, "hub_radius", positive=True)
    hh = _n(hub_height, "hub_height", positive=True)
    if not hr + t < R:
        raise PartError("hub must fit inside the rim")
    shapes = [k.torus(c, a, R, t / 2), k.cylinder(_along(c, a, -hh / 2), _along(c, a, hh / 2), hr)]
    u, v, _ = k.frame_axes(a)
    for i in range(_count(spokes, "spokes", 2, 12)):
        ang = math.radians(90.0 + 360.0 * i / spokes)
        d = tuple(math.cos(ang) * u[j] + math.sin(ang) * v[j] for j in range(3))
        shapes.append(k.cylinder(_along(c, d, hr * 0.8), _along(c, d, R - t * 0.25), t * 0.32))
    return shapes


@part("arrow", required=("origin", "normal", "direction", "length", "width", "depth"))
def _arrow(path, origin, normal, direction, length, width, depth):
    """A flat arrow plate in the plane at ``origin`` pointing along ``direction``."""
    L, W = _n(length, "length", positive=True), _n(width, "width", positive=True)
    head = min(L * 0.4, W * 1.2)
    shaft = W * 0.36
    outline = [[-L / 2, -shaft / 2], [L / 2 - head, -shaft / 2], [L / 2 - head, -W / 2], [L / 2, 0.0],
               [L / 2 - head, W / 2], [L / 2 - head, shaft / 2], [-L / 2, shaft / 2]]
    return [k.prism(origin, normal, outline, depth, u_axis=direction)]
