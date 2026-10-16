"""Pure geometry for the Blender adapter: transforms, meshes, and pipe pieces.

Nothing here imports ``bpy``. Conventions follow ``skid.templates``: metres in
a right-handed frame with +z up, and Euler angles in degrees applied in XYZ
order about fixed axes, ``R = Rz @ Ry @ Rx``, which is Blender's ``XYZ``
rotation mode. A 4x4 matrix is a tuple of rows with the translation in the last
column. Meshes are ``{vertices, faces, smooth}`` with outward face winding;
cylinders and pipe pieces run along their local +z axis.
"""

from __future__ import annotations

import math

__all__ = [
    "AXIS_ROTATIONS",
    "BEND_RADIUS_FACTOR",
    "affine",
    "bend_centreline",
    "bend_mesh",
    "box_mesh",
    "clean",
    "clean_point",
    "compose",
    "cylinder_mesh",
    "cylinder_segments",
    "direction_rotation",
    "ghost_pieces",
    "mesh_for",
    "pipe_pieces",
    "rotation_matrix",
    "sphere_mesh",
    "transform_point",
    "transform_vector",
]

# Results are rounded far below the 1e-6 m endpoint tolerance so that repeated
# builds produce identical numbers.
DIGITS = 12
TOLERANCE = 1e-9

BEND_RADIUS_FACTOR = 1.5
"""Elbow centreline bend radius as a multiple of the pipe outside diameter."""

BEND_STEPS = 12
SPHERE_SEGMENTS = 32
SPHERE_RINGS = 16

_QUARTER_TURNS = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))

# Unit axis -> XYZ Euler rotation turning a primitive's local +z onto it. The
# entries shared with ``skid.templates`` use the same angles.
AXIS_ROTATIONS = {
    (1.0, 0.0, 0.0): (0.0, 90.0, 0.0),
    (-1.0, 0.0, 0.0): (0.0, -90.0, 0.0),
    (0.0, 1.0, 0.0): (-90.0, 0.0, 0.0),
    (0.0, -1.0, 0.0): (90.0, 0.0, 0.0),
    (0.0, 0.0, 1.0): (0.0, 0.0, 0.0),
    (0.0, 0.0, -1.0): (180.0, 0.0, 0.0),
}


def clean(value: float) -> float:
    return round(float(value), DIGITS) + 0.0  # + 0.0 turns -0.0 into 0.0


def clean_point(values) -> list[float]:
    return [clean(v) for v in values]


# --- Vectors and transforms --------------------------------------------------


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _scale(a, factor):
    return tuple(x * factor for x in a)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(a):
    return math.sqrt(_dot(a, a))


def _cos_sin(degrees: float) -> tuple[float, float]:
    quarters, remainder = divmod(float(degrees), 90.0)
    if remainder == 0:  # exact values keep axis-aligned layouts free of noise
        return _QUARTER_TURNS[int(quarters) % 4]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def rotation_matrix(rotation_deg) -> tuple:
    """Return the 3x3 matrix of XYZ Euler angles in degrees, ``Rz @ Ry @ Rx``."""
    cx, sx = _cos_sin(rotation_deg[0])
    cy, sy = _cos_sin(rotation_deg[1])
    cz, sz = _cos_sin(rotation_deg[2])
    rx = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
    ry = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    rz = ((cz, -sz, 0.0), (sz, cz, 0.0), (0.0, 0.0, 1.0))
    return _multiply(rz, _multiply(ry, rx))


def _multiply(a, b):
    size = len(b)
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(size)) for j in range(len(b[0]))) for i in range(len(a)))


def affine(position, rotation_deg=(0.0, 0.0, 0.0)) -> tuple:
    """Return the 4x4 matrix that rotates (XYZ Euler) and then translates."""
    rotation = rotation_matrix(rotation_deg)
    rows = tuple(tuple(rotation[i]) + (float(position[i]),) for i in range(3))
    return rows + ((0.0, 0.0, 0.0, 1.0),)


def compose(outer, inner) -> tuple:
    """Return ``outer @ inner`` for 4x4 matrices."""
    return _multiply(outer, inner)


def transform_point(matrix, point) -> tuple[float, float, float]:
    return tuple(sum(matrix[i][k] * point[k] for k in range(3)) + matrix[i][3] for i in range(3))


def transform_vector(matrix, vector) -> tuple[float, float, float]:
    return tuple(sum(matrix[i][k] * vector[k] for k in range(3)) for i in range(3))


def direction_rotation(direction) -> tuple[float, float, float]:
    """Return XYZ Euler degrees turning local +z onto a unit ``direction``."""
    for axis, rotation in AXIS_ROTATIONS.items():
        if all(abs(a - d) <= TOLERANCE for a, d in zip(axis, direction)):
            return rotation
    x, y, z = direction
    polar = math.degrees(math.acos(max(-1.0, min(1.0, z))))
    return (0.0, polar, math.degrees(math.atan2(y, x)))


# --- Meshes ------------------------------------------------------------------


def _mesh(vertices, faces, smooth) -> dict:
    return {
        "vertices": [clean_point(v) for v in vertices],
        "faces": [list(face) for face in faces],
        "smooth": list(smooth),
    }


def cylinder_segments(radius: float) -> int:
    """Return a deterministic side count giving a smooth silhouette at preview scale."""
    if radius >= 0.2:
        return 48
    return 32 if radius >= 0.04 else 24


def box_mesh(size) -> dict:
    hx, hy, hz = (float(s) / 2 for s in size)
    vertices = [
        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
    ]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return _mesh(vertices, faces, [False] * 6)


def cylinder_mesh(radius: float, length: float, segments: int) -> dict:
    """Capped cylinder centred on the origin along +z.

    The caps use their own vertices so that smooth side shading is not bent by
    the flat caps.
    """
    half = float(length) / 2
    ring = [_cos_sin(360.0 * i / segments) for i in range(segments)]
    vertices = []
    for z in (-half, half, -half, half):  # side bottom, side top, cap bottom, cap top
        vertices.extend((radius * c, radius * s, z) for c, s in ring)
    n = segments
    faces = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    faces.append(tuple(2 * n + i for i in reversed(range(n))))
    faces.append(tuple(3 * n + i for i in range(n)))
    return _mesh(vertices, faces, [True] * n + [False, False])


def sphere_mesh(radius: float, segments: int = SPHERE_SEGMENTS, rings: int = SPHERE_RINGS) -> dict:
    ring = [_cos_sin(360.0 * i / segments) for i in range(segments)]
    vertices = [(0.0, 0.0, radius)]
    for k in range(1, rings):
        cp, sp = _cos_sin(180.0 * k / rings)
        vertices.extend((radius * sp * c, radius * sp * s, radius * cp) for c, s in ring)
    vertices.append((0.0, 0.0, -radius))
    n = segments
    bottom = len(vertices) - 1
    faces = [(0, 1 + i, 1 + (i + 1) % n) for i in range(n)]
    for k in range(rings - 2):
        a, b = 1 + k * n, 1 + (k + 1) * n
        faces.extend((a + i, b + i, b + (i + 1) % n, a + (i + 1) % n) for i in range(n))
    last = 1 + (rings - 2) * n
    faces.extend((last + i, bottom, last + (i + 1) % n) for i in range(n))
    return _mesh(vertices, faces, [True] * len(faces))


def bend_centreline(incoming, outgoing, bend_radius: float, steps: int = BEND_STEPS) -> list[tuple]:
    """Return the quarter-circle centreline of a right-angle bend, relative to its corner.

    The arc leaves ``corner - incoming * r`` along ``incoming`` and arrives at
    ``corner + outgoing * r`` along ``outgoing``.
    """
    a, b, r = tuple(incoming), tuple(outgoing), float(bend_radius)
    centre = _add(_scale(a, -r), _scale(b, r))
    points = []
    for k in range(steps + 1):
        c, s = _cos_sin(90.0 * k / steps)
        points.append(_add(centre, _scale(_add(_scale(b, -c), _scale(a, s)), r)))
    return points


def bend_mesh(incoming, outgoing, bend_radius: float, pipe_radius: float, steps: int, segments: int) -> dict:
    """Open tube swept along ``bend_centreline``; ring ``k`` holds vertices ``k*segments...``."""
    a, b, r = tuple(incoming), tuple(outgoing), float(bend_radius)
    binormal = _cross(a, b)
    centre = _add(_scale(a, -r), _scale(b, r))
    around = [_cos_sin(360.0 * j / segments) for j in range(segments)]
    vertices = []
    for k in range(steps + 1):
        c, s = _cos_sin(90.0 * k / steps)
        radial = _add(_scale(b, -c), _scale(a, s))
        point = _add(centre, _scale(radial, r))
        for cj, sj in around:
            offset = _add(_scale(radial, pipe_radius * cj), _scale(binormal, pipe_radius * sj))
            vertices.append(_add(point, offset))
    n = segments
    faces = [
        (k * n + j, (k + 1) * n + j, (k + 1) * n + (j + 1) % n, k * n + (j + 1) % n)
        for k in range(steps)
        for j in range(n)
    ]
    return _mesh(vertices, faces, [True] * len(faces))


def mesh_for(spec: dict) -> dict:
    """Build the mesh described by a plan's mesh specification."""
    shape = spec["shape"]
    if shape == "box":
        return box_mesh(spec["size_m"])
    if shape == "cylinder":
        return cylinder_mesh(spec["radius_m"], spec["length_m"], spec["segments"])
    if shape == "sphere":
        return sphere_mesh(spec["radius_m"], spec["segments"], spec["rings"])
    if shape == "bend":
        return bend_mesh(
            spec["incoming"], spec["outgoing"], spec["bend_radius_m"], spec["pipe_radius_m"], spec["steps"], spec["segments"]
        )
    raise ValueError(f"unknown mesh shape {shape!r}")


# --- Pipe pieces -------------------------------------------------------------


def _segments(points):
    directions, lengths = [], []
    for start, end in zip(points, points[1:]):
        delta = _sub(end, start)
        size = _length(delta)
        directions.append(_scale(delta, 1.0 / size) if size > TOLERANCE else None)
        lengths.append(size)
    return directions, lengths


def pipe_pieces(points, outside_diameter: float, bend_factor: float = BEND_RADIUS_FACTOR) -> list[dict]:
    """Split a current route polyline into straight segments, elbows, and joints.

    Segment ``i`` runs along route segment ``i``; corner ``j`` is route point
    ``j``. A right-angle corner becomes an elbow whose bend radius is
    ``bend_factor * outside_diameter``, limited to the whole adjacent segment
    when it ends at a port and to half of it when it ends at another corner.
    Adjacent segments are shortened by that radius, so the first segment still
    starts at the first route point and the last still ends at the final one.
    A corner too tight for a bend, or a reversal, becomes a spherical joint;
    collinear points need no piece.
    """
    points = [tuple(float(c) for c in p) for p in points]
    radius = float(outside_diameter) / 2
    count = len(points)
    directions, lengths = _segments(points)
    trims = [0.0] * count
    corners = {}
    for j in range(1, count - 1):
        a, b = directions[j - 1], directions[j]
        if a is None or b is None:
            continue
        cosine = _dot(a, b)
        if cosine >= 1 - TOLERANCE:
            continue
        if abs(cosine) <= TOLERANCE:
            limit_in = lengths[j - 1] * (0.5 if j - 1 > 0 else 1.0)
            limit_out = lengths[j] * (0.5 if j + 1 < count - 1 else 1.0)
            trim = min(bend_factor * outside_diameter, limit_in, limit_out)
            if trim >= radius:
                trims[j] = trim
                corners[j] = "elbow"
                continue
        corners[j] = "joint"
    pieces = []
    for i in range(count - 1):
        direction = directions[i]
        if direction is not None:
            start = _add(points[i], _scale(direction, trims[i]))
            end = _sub(points[i + 1], _scale(direction, trims[i + 1]))
            if _length(_sub(end, start)) > TOLERANCE:
                pieces.append({"kind": "segment", "index": i, "start": start, "end": end, "direction": direction,
                               "radius": radius})
        j = i + 1
        if corners.get(j) == "elbow":
            a, b, trim = directions[j - 1], directions[j], trims[j]
            pieces.append({"kind": "elbow", "index": j, "corner": points[j], "incoming": a, "outgoing": b,
                           "bend_radius": trim, "start": _sub(points[j], _scale(a, trim)),
                           "end": _add(points[j], _scale(b, trim)), "radius": radius})
        elif corners.get(j) == "joint":
            pieces.append({"kind": "joint", "index": j, "corner": points[j], "radius": radius})
    return pieces


def ghost_pieces(points, radius: float) -> list[dict]:
    """Split retained, non-current route points into thin full-length segments and joints."""
    points = [tuple(float(c) for c in p) for p in points]
    directions, _ = _segments(points)
    pieces = []
    for i, direction in enumerate(directions):
        if direction is not None:
            pieces.append({"kind": "segment", "index": i, "start": points[i], "end": points[i + 1],
                           "direction": direction, "radius": float(radius)})
        if i + 1 < len(points) - 1:
            pieces.append({"kind": "joint", "index": i + 1, "corner": points[i + 1], "radius": float(radius)})
    return pieces
