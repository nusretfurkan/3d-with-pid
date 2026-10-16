"""OCCT primitive kernel: millimetre solids, frames, measurement and tessellation.

Every function here works in millimetres in a right-handed frame. Callers pass
plain tuples; OCP objects never leak into recipes or JSON. Solids are built
directly (prisms, revolutions, lofts, sweeps) and grouped into compounds, so a
component needs no Boolean operation unless its recipe asks for a fuse.

Rotations given as ``rotation_deg`` follow the project convention: XYZ order
about fixed axes, ``R = Rz @ Ry @ Rx`` (see ``skid.templates``).
"""

from __future__ import annotations

import math

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet2d
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe, BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeRevol,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.GProp import GProp_GProps
from OCP.Message import Message, Message_Gravity
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.gp import gp_Ax1, gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

__all__ = [
    "KernelError",
    "bounds",
    "box",
    "compound",
    "cone",
    "cylinder",
    "faces",
    "frame_axes",
    "fuse",
    "hex_prism",
    "is_valid",
    "located",
    "loft",
    "mesh",
    "pipe",
    "placement_matrix",
    "prism",
    "revolve",
    "rotation_matrix",
    "solid_count",
    "sphere",
    "torus",
    "transform_point",
    "transform_vector",
    "trsf",
    "volume",
]

EPS = 1e-9
_QUARTER = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))

# Keep OCCT's console statistics quiet, as CadQuery does on import; failures still print.
for _printer in Message.DefaultMessenger_s().Printers():
    _printer.SetTraceLevel(Message_Gravity.Message_Fail)


class KernelError(ValueError):
    """A primitive received invalid arguments or OCCT could not build it."""


# --- Vectors and frames ------------------------------------------------------


def _num(value, label) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise KernelError(f"{label} must be a finite number, not {value!r}")
    return float(value)


def _positive(value, label) -> float:
    value = _num(value, label)
    if value <= 0:
        raise KernelError(f"{label} must be positive, not {value!r}")
    return value


def _vec3(value, label) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise KernelError(f"{label} must be three numbers, not {value!r}")
    return tuple(_num(v, label) for v in value)


def _unit(value, label) -> tuple[float, float, float]:
    x, y, z = _vec3(value, label)
    n = math.sqrt(x * x + y * y + z * z)
    if n < EPS:
        raise KernelError(f"{label} must be a nonzero vector")
    return (x / n, y / n, z / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a, b, s=1.0):
    return (a[0] + s * b[0], a[1] + s * b[1], a[2] + s * b[2])


def frame_axes(normal, u_axis=None):
    """Return orthonormal ``(u, v, n)`` with ``u x v = n``.

    Without ``u_axis`` the reference is the world axis least aligned with the
    normal (lowest index on ties), projected into the plane. The rule depends
    only on the normal's line, so both faces of a flanged joint share it.
    """
    n = _unit(normal, "normal")
    if u_axis is None:
        magnitudes = [abs(c) for c in n]
        index = magnitudes.index(min(magnitudes))
        a = [0.0, 0.0, 0.0]
        a[index] = 1.0
    else:
        a = _unit(u_axis, "u_axis")
    d = _dot(a, n)
    u = (a[0] - d * n[0], a[1] - d * n[1], a[2] - d * n[2])
    length = math.sqrt(_dot(u, u))
    if length < 1e-6:
        raise KernelError("u_axis must not be parallel to the normal")
    u = (u[0] / length, u[1] / length, u[2] / length)
    v = _cross(n, u)
    return u, v, n


def _pnt(p):
    return gp_Pnt(*p)


def _dir(d):
    return gp_Dir(*d)


def _cos_sin(degrees):
    quarters, remainder = divmod(degrees, 90.0)
    if remainder == 0:
        return _QUARTER[int(quarters) % 4]
    r = math.radians(degrees)
    return math.cos(r), math.sin(r)


def rotation_matrix(rotation_deg) -> tuple[tuple[float, float, float], ...]:
    """``Rz @ Ry @ Rx`` for XYZ fixed-axis degrees, exact for quarter turns."""
    rx, ry, rz = _vec3(rotation_deg, "rotation_deg")
    cx, sx = _cos_sin(rx)
    cy, sy = _cos_sin(ry)
    cz, sz = _cos_sin(rz)
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def placement_matrix(position, rotation_deg, scale=1.0):
    """3x4 affine matrix of a placement; ``scale`` multiplies the translation."""
    r = rotation_matrix(rotation_deg)
    p = _vec3(position, "position")
    return tuple(tuple(r[i]) + (p[i] * scale,) for i in range(3))


def transform_point(matrix, point):
    return tuple(_clean(sum(matrix[i][j] * point[j] for j in range(3)) + matrix[i][3]) for i in range(3))


def transform_vector(matrix, vector):
    return tuple(_clean(sum(matrix[i][j] * vector[j] for j in range(3))) for i in range(3))


def _clean(value, digits=12):
    return round(float(value), digits) + 0.0


def trsf(matrix) -> gp_Trsf:
    t = gp_Trsf()
    (a, b, c, d), (e, f, g, h), (i, j, k, l) = matrix
    t.SetValues(a, b, c, d, e, f, g, h, i, j, k, l)
    return t


def located(shape, matrix):
    """Return ``shape`` moved by a 3x4 matrix, sharing its geometry."""
    return shape.Moved(TopLoc_Location(trsf(matrix)))


# --- Primitive solids --------------------------------------------------------


def _done(builder, what):
    try:
        builder.Build()
    except Exception as exc:  # OCCT raises Standard_Failure subclasses
        raise KernelError(f"{what} failed: {exc}") from exc
    if not builder.IsDone():
        raise KernelError(f"{what} failed")
    return builder.Shape()


def box(center, size, rotation_deg=(0.0, 0.0, 0.0)):
    sx, sy, sz = (_positive(s, "box size") for s in _vec3(size, "box size"))
    cx, cy, cz = _vec3(center, "box center")
    solid = BRepPrimAPI_MakeBox(gp_Pnt(-sx / 2, -sy / 2, -sz / 2), sx, sy, sz).Shape()
    return located(solid, placement_matrix((cx, cy, cz), rotation_deg))


def cylinder(start, end, radius):
    start, end = _vec3(start, "cylinder start"), _vec3(end, "cylinder end")
    axis = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
    length = math.sqrt(_dot(axis, axis))
    if length < EPS:
        raise KernelError("cylinder start and end coincide")
    ax = gp_Ax2(_pnt(start), _dir(axis))
    return BRepPrimAPI_MakeCylinder(ax, _positive(radius, "cylinder radius"), length).Shape()


def cone(start, end, radius_start, radius_end):
    start, end = _vec3(start, "cone start"), _vec3(end, "cone end")
    axis = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
    length = math.sqrt(_dot(axis, axis))
    if length < EPS:
        raise KernelError("cone start and end coincide")
    r1, r2 = _num(radius_start, "cone radius"), _num(radius_end, "cone radius")
    if r1 < 0 or r2 < 0 or (r1 == 0 and r2 == 0) or abs(r1 - r2) < EPS:
        raise KernelError(f"cone radii {r1!r}, {r2!r} must be nonnegative, distinct and not both zero")
    return BRepPrimAPI_MakeCone(gp_Ax2(_pnt(start), _dir(axis)), r1, r2, length).Shape()


def sphere(center, radius):
    return BRepPrimAPI_MakeSphere(_pnt(_vec3(center, "sphere center")), _positive(radius, "sphere radius")).Shape()


def torus(center, axis, major_radius, minor_radius):
    major, minor = _positive(major_radius, "torus major radius"), _positive(minor_radius, "torus minor radius")
    if minor >= major:
        raise KernelError("torus minor radius must be smaller than its major radius")
    ax = gp_Ax2(_pnt(_vec3(center, "torus center")), _dir(_unit(axis, "torus axis")))
    return BRepPrimAPI_MakeTorus(ax, major, minor).Shape()


def _polygon_wire(points):
    poly = BRepBuilderAPI_MakePolygon()
    for p in points:
        poly.Add(_pnt(p))
    poly.Close()
    if not poly.IsDone():
        raise KernelError("polygon wire failed")
    return poly.Wire()


def _signed_area(points2d):
    area = 0.0
    for i, (x0, y0) in enumerate(points2d):
        x1, y1 = points2d[(i + 1) % len(points2d)]
        area += x0 * y1 - x1 * y0
    return area / 2


def _points2d(points, label, minimum=3):
    if not isinstance(points, (list, tuple)) or len(points) < minimum:
        raise KernelError(f"{label} needs at least {minimum} points")
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise KernelError(f"{label} points must be [u, v] pairs, not {p!r}")
        out.append((_num(p[0], label), _num(p[1], label)))
    for i in range(len(out)):
        if math.dist(out[i], out[(i + 1) % len(out)]) < 1e-6:
            raise KernelError(f"{label} has coincident consecutive points")
    return out


def revolve(origin, axis, ref, profile, angle_deg=360.0, start_deg=0.0):
    """Revolve a closed ``[[radial, axial], ...]`` polygon about ``axis``.

    ``ref`` is the radial direction of the profile plane at ``start_deg``.
    Radial coordinates must be nonnegative; the profile may touch the axis.
    """
    origin = _vec3(origin, "revolve origin")
    a = _unit(axis, "revolve axis")
    u, _, _ = frame_axes(a, ref)
    pts = _points2d(profile, "revolve profile")
    if any(r < -1e-9 for r, _ in pts):
        raise KernelError("revolve profile radial coordinates must be nonnegative")
    if abs(_signed_area(pts)) < 1e-6:
        raise KernelError("revolve profile has no area")
    angle = _num(angle_deg, "revolve angle")
    if not 0 < angle <= 360:
        raise KernelError("revolve angle must be in (0, 360]")
    start = math.radians(_num(start_deg, "revolve start"))
    # Rotate the reference radial direction about the axis by start_deg (Rodrigues).
    c, s = math.cos(start), math.sin(start)
    axu = _cross(a, u)
    u = (u[0] * c + axu[0] * s, u[1] * c + axu[1] * s, u[2] * c + axu[2] * s)
    world = [_add(_add(origin, u, max(r, 0.0)), a, h) for r, h in pts]
    face = BRepBuilderAPI_MakeFace(_polygon_wire(world), True)
    if not face.IsDone():
        raise KernelError("revolve profile face failed")
    builder = BRepPrimAPI_MakeRevol(face.Face(), gp_Ax1(_pnt(origin), _dir(a)), math.radians(angle), False)
    return _done(builder, "revolve")


def _circle_wire(center, normal, radius):
    edge = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(_pnt(center), _dir(normal)), radius)).Edge()
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _fillet_corners(face, radius):
    fillet = BRepFilletAPI_MakeFillet2d(face)
    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_VERTEX, vertices)
    for i in range(1, vertices.Extent() + 1):
        fillet.AddFillet(TopoDS.Vertex_s(vertices.FindKey(i)), radius)
    return _done(fillet, "corner fillet")


def prism(origin, normal, outline, depth, u_axis=None, holes=(), corner_radius=0.0):
    """Extrude a planar outline with circular holes along ``normal`` by ``depth``.

    ``outline`` is ``[[u, v], ...]`` (a closed polygon) or ``{"circle": r}``;
    ``holes`` are ``{"center": [u, v], "radius": r}``. ``corner_radius`` rounds
    every polygon corner.
    """
    origin = _vec3(origin, "prism origin")
    u, v, n = frame_axes(normal, u_axis)
    depth = _positive(depth, "prism depth")

    def at(p):
        return _add(_add(origin, u, p[0]), v, p[1])

    if isinstance(outline, dict):
        if set(outline) != {"circle"}:
            raise KernelError(f"prism outline object must be {{'circle': r}}, not {outline!r}")
        outer = _circle_wire(origin, n, _positive(outline["circle"], "prism circle"))
        face = BRepBuilderAPI_MakeFace(outer, True).Face()
    else:
        pts = _points2d(outline, "prism outline")
        if _signed_area(pts) < 0:
            pts.reverse()
        face = BRepBuilderAPI_MakeFace(_polygon_wire([at(p) for p in pts]), True).Face()
        radius = _num(corner_radius, "corner_radius")
        if radius > 0:
            face = TopoDS.Face_s(_fillet_corners(face, radius))
    if holes:
        maker = BRepBuilderAPI_MakeFace(BRepTools.OuterWire_s(face), True)
        for hole in holes:
            if not isinstance(hole, dict) or set(hole) != {"center", "radius"}:
                raise KernelError(f"prism hole must be {{center, radius}}, not {hole!r}")
            center = hole["center"]
            if not isinstance(center, (list, tuple)) or len(center) != 2:
                raise KernelError(f"hole center must be [u, v], not {center!r}")
            c = (_num(center[0], "hole center"), _num(center[1], "hole center"))
            wire = _circle_wire(at(c), n, _positive(hole["radius"], "hole radius"))
            maker.Add(TopoDS.Wire_s(wire.Reversed()))
        if not maker.IsDone():
            raise KernelError("prism face with holes failed")
        face = maker.Face()
    builder = BRepPrimAPI_MakePrism(face, gp_Vec(n[0] * depth, n[1] * depth, n[2] * depth), False, True)
    return _done(builder, "prism")


def hex_prism(start, axis, across_flats, height, phase_deg=0.0):
    """Hexagonal prism (nut or bolt head) from ``start`` along ``axis``."""
    af = _positive(across_flats, "across_flats")
    r = af / math.sqrt(3.0)
    phase = _num(phase_deg, "phase_deg")
    outline = [[r * math.cos(math.radians(phase + 60 * k)), r * math.sin(math.radians(phase + 60 * k))] for k in range(6)]
    return prism(start, axis, outline, height)


def _spline_edge(points, start_tangent=None, end_tangent=None):
    array = TColgp_HArray1OfPnt(1, len(points))
    for i, p in enumerate(points, 1):
        array.SetValue(i, _pnt(p))
    interp = GeomAPI_Interpolate(array, False, 1e-6)
    if start_tangent is not None and end_tangent is not None:
        interp.Load(gp_Vec(*start_tangent), gp_Vec(*end_tangent), True)
    interp.Perform()
    if not interp.IsDone():
        raise KernelError("spline interpolation failed")
    return BRepBuilderAPI_MakeEdge(interp.Curve()).Edge()


def _section_wire(section, index):
    """A loft section: straight ``lines`` then one ``spline`` back to the start."""
    if not isinstance(section, dict):
        raise KernelError(f"loft section {index} must be an object")
    origin = _vec3(section.get("origin"), f"loft section {index} origin")
    u, v, _ = frame_axes(section.get("normal"), section.get("u_axis"))

    def at(p):
        return _add(_add(origin, u, p[0]), v, p[1])

    lines = [at(p) for p in _points2d(section.get("lines"), f"loft section {index} lines", 2)]
    spline = [at(p) for p in _points2d(section.get("spline"), f"loft section {index} spline", 2)]
    if math.dist(lines[-1], spline[0]) > 1e-6 or math.dist(spline[-1], lines[0]) > 1e-6:
        raise KernelError(f"loft section {index}: spline must join the line chain at both ends")
    wire = BRepBuilderAPI_MakeWire()
    for a, b in zip(lines, lines[1:]):
        wire.Add(BRepBuilderAPI_MakeEdge(_pnt(a), _pnt(b)).Edge())
    tangents = section.get("spline_tangents")
    start_t = end_t = None
    if tangents is not None:
        t0, t1 = (_points2d(tangents, f"loft section {index} tangents", 2))
        start_t = _add(_add((0, 0, 0), u, t0[0]), v, t0[1])
        end_t = _add(_add((0, 0, 0), u, t1[0]), v, t1[1])
    wire.Add(_spline_edge(spline, start_t, end_t))
    if not wire.IsDone():
        raise KernelError(f"loft section {index} wire failed")
    return wire.Wire()


def loft(sections, ruled=False):
    if not isinstance(sections, (list, tuple)) or len(sections) < 2:
        raise KernelError("loft needs at least two sections")
    builder = BRepOffsetAPI_ThruSections(True, bool(ruled), 1e-6)
    builder.CheckCompatibility(False)
    for index, section in enumerate(sections):
        builder.AddWire(_section_wire(section, index))
    return _done(builder, "loft")


def pipe(points, radius, bend_radius):
    """Sweep a circle along a polyline whose corners are tangent arcs.

    Returns ``(solid, used_bend_radii)``; a corner's radius is reduced when the
    adjacent straight runs are too short, and the reduction is reported.
    """
    pts = [_vec3(p, "pipe point") for p in points] if isinstance(points, (list, tuple)) else None
    if not pts or len(pts) < 2:
        raise KernelError("pipe needs at least two points")
    radius = _positive(radius, "pipe radius")
    bend = _positive(bend_radius, "pipe bend radius")
    dirs, lengths = [], []
    for a, b in zip(pts, pts[1:]):
        d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        length = math.sqrt(_dot(d, d))
        if length < 1e-6:
            raise KernelError("pipe has a zero-length segment")
        dirs.append((d[0] / length, d[1] / length, d[2] / length))
        lengths.append(length)
    # Tangent distance at each interior corner.
    cuts, radii = [0.0] * len(pts), []
    for i in range(1, len(pts) - 1):
        cosine = max(-1.0, min(1.0, _dot(dirs[i - 1], dirs[i])))
        theta = math.acos(cosine)
        if theta < 1e-9:
            radii.append(None)
            continue
        if theta > math.pi - 1e-6:
            raise KernelError("pipe reverses direction")
        half = math.tan(theta / 2)
        limit = 0.49 * min(lengths[i - 1], lengths[i])
        r = min(bend, limit / half)
        if r <= radius * 1.01:
            raise KernelError(f"pipe corner {i} has no room for a bend larger than the pipe radius")
        cuts[i] = r * half
        radii.append(_clean(r, 9))
    wire = BRepBuilderAPI_MakeWire()
    cursor = pts[0]
    for i in range(1, len(pts)):
        if i < len(pts) - 1 and cuts[i] > 0:
            before = _add(pts[i], dirs[i - 1], -cuts[i])
            after = _add(pts[i], dirs[i], cuts[i])
            if math.dist(cursor, before) > 1e-9:
                wire.Add(BRepBuilderAPI_MakeEdge(_pnt(cursor), _pnt(before)).Edge())
            arc = GC_MakeArcOfCircle(_pnt(before), gp_Vec(*dirs[i - 1]), _pnt(after)).Value()
            wire.Add(BRepBuilderAPI_MakeEdge(arc).Edge())
            cursor = after
        else:
            end = pts[i]
            if math.dist(cursor, end) > 1e-9:
                wire.Add(BRepBuilderAPI_MakeEdge(_pnt(cursor), _pnt(end)).Edge())
            cursor = end
    if not wire.IsDone():
        raise KernelError("pipe path wire failed")
    profile = BRepBuilderAPI_MakeFace(_circle_wire(pts[0], dirs[0], radius), True).Face()
    solid = _done(BRepOffsetAPI_MakePipe(wire.Wire(), profile), "pipe sweep")
    return solid, radii


# --- Grouping and measurement ------------------------------------------------


def compound(shapes):
    result = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(result)
    for shape in shapes:
        builder.Add(result, shape)
    return result


def fuse(shapes):
    """Fuse solids with one batched Boolean; the result is one shape."""
    shapes = list(shapes)
    if len(shapes) == 1:
        return shapes[0]
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(shapes[0])
    for shape in shapes[1:]:
        tools.Append(shape)
    op = BRepAlgoAPI_Fuse()
    op.SetArguments(arguments)
    op.SetTools(tools)
    op.SetRunParallel(False)
    op.Build()
    if not op.IsDone():
        raise KernelError("fuse failed")
    op.SimplifyResult()
    result = op.Shape()
    if not is_valid(result):
        raise KernelError("fuse produced an invalid shape")
    return result


def solids(shape):
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def solid_count(shape) -> int:
    return len(solids(shape))


def faces(shape):
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def is_valid(shape) -> bool:
    return bool(BRepCheck_Analyzer(shape).IsValid())


def volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def bounds(shape):
    """Tight axis-aligned bounds ``((xmin, ymin, zmin), (xmax, ymax, zmax))`` from exact geometry."""
    box_ = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box_, False, False)
    if box_.IsVoid():
        raise KernelError("shape has no extent")
    xmin, ymin, zmin, xmax, ymax, zmax = box_.Get()
    return (xmin, ymin, zmin), (xmax, ymax, zmax)


def mesh(shape, linear_deflection, angular_deflection):
    """Triangulate ``shape`` and return ``(positions, normals, indices)``.

    Positions and normals are flat lists in the shape's own (located) frame.
    Each face keeps its own vertices, so creases stay sharp while curved faces
    shade smoothly. Reversed faces get flipped winding and normals.
    """
    BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, False)
    positions, normals, indices = [], [], []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        explorer.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            raise KernelError("a face could not be triangulated")
        if not tri.HasNormals():
            BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, tri)
        t = loc.Transformation()
        identity = loc.IsIdentity()
        reversed_ = face.Orientation() == TopAbs_REVERSED
        sign = -1.0 if reversed_ else 1.0
        base = len(positions) // 3
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i)
            n = tri.Normal(i)
            if not identity:
                p = p.Transformed(t)
                n = n.Transformed(t)
            positions += (p.X(), p.Y(), p.Z())
            normals += (sign * n.X(), sign * n.Y(), sign * n.Z())
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            if reversed_:
                b, c = c, b
            indices += (base + a - 1, base + b - 1, base + c - 1)
    return positions, normals, indices
