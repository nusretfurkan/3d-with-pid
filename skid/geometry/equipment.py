"""Detailed recipes for the template-version-1 equipment kinds.

Each generator turns an entity's recorded parameters into components made of
``parts`` calls in the entity's local frame, in millimetres. Port positions and
directions come from ``skid.templates`` (the source of route endpoints) and
are never adjusted here: every nozzle flange face is built exactly on its
template port. Envelopes keep their meaning: the pump casing, bearing frame
and drive fit inside ``body_length x body_width x body_height`` and the motor
frame inside ``motor_length x motor_diameter``. Everything else is an
illustrative detail recorded in ``RECIPE_ASSUMPTIONS``.
"""

from __future__ import annotations

import math

from skid.templates import entity_primitives

from .kernel import rotation_matrix
from .recipes import RecipeError

__all__ = [
    "FLANGE_DN50",
    "RECIPE_ASSUMPTIONS",
    "SUPPORTED_RANGES_M",
    "entity_recipe",
    "template_ports",
]

GENERATOR = "skid.geometry.equipment"

FLANGE_DN50 = {"outer_radius": 75.0, "thickness": 18.0, "bolt_count": 4, "bolt_circle_radius": 60.0,
               "hole_radius": 9.0, "phase_deg": 45.0}
BOLTING_DN50 = {"stud_radius": 7.5, "nut_across_flats": 24.0, "nut_height": 13.0}
NECK_RADIUS = 36.0
BORE_RADIUS = 26.0

# Recipe-level assumptions. Project assumptions (dimensions, layout) are cited
# alongside them by ID from the entity's parameter and placement records.
RECIPE_ASSUMPTIONS = {
    "rcp-appearance": {
        "category": "appearance",
        "description": "Colours, metalness and roughness are appearance-only. They assert no material of "
                       "construction, coating or finish.",
    },
    "rcp-flange-dn50": {
        "category": "illustrative_detail",
        "description": "Illustrative DN50 flange proportions on every process port: outside diameter 150 mm, "
                       "thickness 18 mm, four 18 mm holes on a 120 mm bolt circle straddling the centrelines. "
                       "Resembles common DN50 flanges; no pressure rating, facing or standard is selected.",
    },
    "rcp-nozzle-neck": {
        "category": "illustrative_detail",
        "description": "Equipment nozzle necks are 72 mm outside diameter with an illustrative 52 mm bore shown "
                       "at the flange. Nozzle stubs keep the template lengths, and each flange joint face lies "
                       "exactly on its template port.",
    },
    "rcp-pump-casing": {
        "category": "illustrative_detail",
        "description": "End-suction pump casing drawn as a spiral volute with a top discharge neck, axial suction "
                       "neck, suction cover, casing cover, bearing frame and cast feet, proportioned from "
                       "body_length, body_width and body_height and kept inside that envelope. The volute lies in "
                       "the plane of the template discharge port. Impeller, internal passages, seals, bearings "
                       "and wall thicknesses are not modeled.",
    },
    "rcp-pump-drive": {
        "category": "illustrative_detail",
        "description": "Exposed shaft, jaw-type flexible coupling and an open-bottom coupling guard with two legs, "
                       "placed between the bearing frame and the motor inside the body envelope. No coupling "
                       "rating or guarding standard is implied.",
    },
    "rcp-pump-baseplate": {
        "category": "illustrative_detail",
        "description": "Common baseplate under pump and motor: 25 mm (or 8 % of body height if less) thick, from "
                       "the suction end of the body envelope to the motor end, body_width wide, resting on the "
                       "deck at z = 0 with six anchor bolts. Not a foundation or grouting design.",
    },
    "rcp-motor-details": {
        "category": "illustrative_detail",
        "description": "Motor frame with end shields, 28 radial cooling fins (none at the bottom) and a domed fan "
                       "cowl, drawn inside the motor_length x motor_diameter envelope. Terminal box (above) and "
                       "feet (below) are illustrative additions outside that envelope. No power, speed, "
                       "efficiency or enclosure rating is implied.",
    },
    "rcp-fasteners": {
        "category": "illustrative_detail",
        "description": "Hex nuts with protruding stud ends mark bolted joints (casing cover, feet, bonnets, "
                       "anchors). Sizes are illustrative; no bolt specification or torque is implied.",
    },
    "rcp-tank-details": {
        "category": "illustrative_detail",
        "description": "Vertical tank drawn as a closed envelope solid with a base ring, top rim, two "
                       "circumferential seam bands, four anchor chairs and a nozzle reinforcing pad. Wall "
                       "thickness, heads, internals and openings other than the modeled outlet are not modeled.",
    },
    "rcp-isolation-valve": {
        "category": "illustrative_detail",
        "description": "Isolation valve drawn as a flanged body with a bolted bonnet and a handwheel operator. A "
                       "handwheel was chosen so that no open/closed position is implied. Valve type, trim and "
                       "normal position remain unknown.",
    },
    "rcp-check-valve": {
        "category": "illustrative_detail",
        "description": "Check valve drawn as a flanged body with a bolted top cover and a raised flow arrow from "
                       "the inlet port toward the outlet port. Disc, seat and closure behavior are not modeled.",
    },
    "rcp-indicator": {
        "category": "illustrative_detail",
        "description": "Conceptual indication drawn as a generic dial (hex socket, stem, case and blank white "
                       "face) at its assumed mount. No tapping, hookup, instrument type, range or reading is "
                       "modeled.",
    },
    "rcp-boundary": {
        "category": "illustrative_detail",
        "description": "Scope boundary drawn as a flange and short pipe stub at the boundary port, included in "
                       "CAD. The marker plate is display-only and excluded from CAD.",
    },
    "rcp-route-pipe": {
        "category": "approximation",
        "description": "Pipe drawn as a solid outside-diameter envelope (wall thickness unknown) swept along the "
                       "route points, with bends of centreline radius 1.5 x DN (75 mm for DN50) reduced only "
                       "where adjacent runs are too short. Bends replace the sharp polyline corners within the "
                       "bend zone; endpoints are the exact route points.",
    },
    "rcp-route-joints": {
        "category": "illustrative_detail",
        "description": "Each current route end carries a weld-neck-style mating flange and four studs with nuts "
                       "through the equipment flange. Gaskets are omitted.",
    },
    "rcp-pending-route": {
        "category": "approximation",
        "description": "A route that is not current is shown only as a thin red display-only path through its "
                       "retained points. It is excluded from CAD and is not piping.",
    },
}

# Parameter ranges (metres) within which the detailed recipes keep their
# proportions; outside them generation fails explicitly instead of drawing
# overlapping or inverted details.
SUPPORTED_RANGES_M = {
    "centrifugal_pump": {"body_length": (0.3, 1.5), "body_width": (0.18, 1.0), "body_height": (0.22, 1.2),
                         "motor_length": (0.15, 2.0), "motor_diameter": (0.1, 1.0)},
    "tank": {"diameter": (0.25, 6.0), "height": (0.3, 12.0), "outlet_height": (0.08, 12.0)},
    "isolation_valve": {"length": (0.08, 1.0), "body_diameter": (0.06, 0.6)},
    "check_valve": {"length": (0.08, 1.0), "body_diameter": (0.06, 0.6)},
    "indicator": {"diameter": (0.03, 0.3)},
    "boundary": {"size": (0.1, 1.0)},
}

_X, _Y, _Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


def _rotate(rotation_deg, vector):
    """Rz @ Ry @ Rx applied to ``vector`` (exact for quarter turns)."""
    m = rotation_matrix(rotation_deg)
    return tuple(round(sum(m[i][j] * vector[j] for j in range(3)), 12) + 0.0 for i in range(3))


def template_ports(entity) -> dict:
    """Local port frames in millimetres from the source template's nozzle primitives."""
    ports = {}
    for primitive in entity_primitives(entity):
        key = primitive["key"]
        if not key.startswith("nozzle/"):
            continue
        direction = _rotate(primitive["rotation_deg"], _Z)
        half = primitive["length_m"] / 2
        position = [round((c + d * half) * 1000.0, 9) + 0.0 for c, d in zip(primitive["center_m"], direction)]
        ports[key.split("/", 1)[1]] = {
            "position_mm": position,
            "direction": list(direction),
            "stub_length_mm": round(primitive["length_m"] * 1000.0, 9),
        }
    return ports


def _component(key, material, assumptions, parts_, display_only=False):
    return {"key": key, "material": material, "display_only": display_only,
            "assumptions": sorted(set(assumptions)), "parts": parts_}


def _along(point, direction, distance):
    return [p + d * distance for p, d in zip(point, direction)]


def _nozzle(port, assumptions, key, material, neck_length=None):
    length = port["stub_length_mm"] if neck_length is None else neck_length
    return _component(key, material, assumptions + ["rcp-flange-dn50", "rcp-nozzle-neck"], [{
        "part": "flanged_nozzle", "port": port["position_mm"], "direction": port["direction"],
        "neck_length": length, "neck_radius": NECK_RADIUS, "bore_radius": BORE_RADIUS, "flange": dict(FLANGE_DN50),
    }])


def _bolt(seat, axis, size):
    """Nut and stud end sized from a nominal nut across-flats ``size``."""
    return {"part": "bolt", "seat": list(seat), "axis": list(axis), "across_flats": size,
            "height": round(size * 0.55, 6), "stud_radius": round(size * 0.3, 6), "stud_length": round(size * 0.9, 6)}


def _rbox(center, size, radius):
    """A rounded-corner block: a prism whose outline rounds the vertical edges."""
    cx, cy, cz = center
    sx, sy, sz = size
    return {"part": "prism", "origin": [cx, cy, cz - sz / 2], "normal": list(_Z),
            "outline": [[-sx / 2, -sy / 2], [sx / 2, -sy / 2], [sx / 2, sy / 2], [-sx / 2, sy / 2]],
            "depth": sz, "u_axis": list(_X), "corner_radius": radius}


# --- Centrifugal pump ---------------------------------------------------------


def _pump(p, ports, cited):
    L, W, H = p["body_length"], p["body_width"], p["body_height"]
    ML, MD = p["motor_length"], p["motor_diameter"]
    zc, R = H / 2, MD / 2
    s = min(zc, W / 2)
    base_t = min(25.0, 0.08 * H)
    if zc - R < base_t + 15:
        raise RecipeError(f"motor_diameter {MD / 1000:g} m leaves no room for motor feet above the baseplate "
                          f"under a {zc / 1000:g} m shaft centreline")
    cited_body = cited + ["rcp-pump-casing"]
    origin = [0.0, 0.0, zc]

    # Volute casing around the shaft, its discharge neck rising to the body top at x = 0.
    rA, rB = 0.60 * s, 0.84 * s
    hw = min(0.27 * s, 0.5 * NECK_RADIUS + 0.2 * s)
    cw = min(max(0.24 * L, 0.45 * s), 0.4 * L)
    ch = 0.12 * cw
    xv0, xv1 = -cw / 2, cw / 2
    eye_r = 0.62 * rA
    neck_start = xv0 - 10.0
    taper = neck_start - 0.35 * (neck_start + L / 2)
    # The volute throat ends just above the spiral; a round cast neck carries it to the body top.
    throat_top = min(zc + 1.02 * rB, H - 0.25 * (H - zc))
    neck_r0 = min(hw + 2.0, cw / 2 - ch)
    neck_r1 = NECK_RADIUS + 4.0
    zn = throat_top - 12.0
    body_parts = [
        {"part": "volute_casing", "center": origin, "width": cw, "tongue_radius": rA, "outer_radius": rB,
         "neck_half_width": hw, "neck_top": throat_top, "chamfer": ch},
        {"part": "revolve", "origin": [0.0, 0.0, zn], "axis": list(_Z), "ref": list(_X), "profile": [
            [0.0, 0.0], [neck_r0, 0.0], [neck_r0, 12.0], [0.5 * (neck_r0 + neck_r1), 0.45 * (H - zn)],
            [neck_r1, 0.8 * (H - zn)], [neck_r1, H - zn], [0.0, H - zn]]},
        # Suction cover boss and the axial suction neck reaching the envelope face.
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, xv0 - 10.0], [0.80 * rA, xv0 - 10.0], [0.86 * rA, xv0 - 4.0], [0.86 * rA, xv0 + ch],
            [0.0, xv0 + ch]]},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, -L / 2], [NECK_RADIUS, -L / 2], [NECK_RADIUS, taper], [eye_r, neck_start - 4.0],
            [eye_r, neck_start + 1.0], [0.0, neck_start + 1.0]]},
    ]
    rcov = rA - ch - 2.0
    x_cov = xv1 + 12.0
    body_parts.append({"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
        [0.0, xv1 - ch], [rcov, xv1 - ch], [rcov, x_cov], [0.0, x_cov]]})

    # Bearing frame: adapter cone and ribbed housing.
    r_bh = 0.36 * s
    r_sh = 0.10 * s
    cpl_len = 0.13 * L
    x_cp0 = L / 2 - cpl_len
    x_bh1 = x_cp0 - 14.0
    x_bh0 = x_cov + 0.07 * L
    if x_bh1 - x_bh0 < 20.0:
        raise RecipeError(f"body_length {L / 1000:g} m leaves no room for the bearing frame")
    frame_parts = [
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, x_cov - 1.0], [0.70 * rA, x_cov - 1.0], [0.70 * rA, x_cov + 6.0], [r_bh + 4.0, x_bh0],
            [0.0, x_bh0]]},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, x_bh0], [r_bh, x_bh0], [r_bh, x_bh1 - 6.0], [r_bh - 8.0, x_bh1], [0.0, x_bh1]]},
    ]
    for i in range(3):
        x = x_bh0 + (x_bh1 - x_bh0 - 6.0) * (i + 1) / 4
        frame_parts.append({"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [r_bh - 1.0, x - 2.5], [r_bh + 5.0, x - 2.0], [r_bh + 5.0, x + 2.0], [r_bh - 1.0, x + 2.5]]})

    # Drive: shaft, coupling halves with a spider ring, guard with legs.
    r_cp = 0.30 * s
    x_mid = (x_cp0 + L / 2) / 2
    shaft_parts = [{"part": "cylinder", "start": [x_bh1 - 4.0, 0.0, zc], "end": [L / 2 + 2.0, 0.0, zc],
                    "radius": r_sh}]
    coupling_parts = [
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [r_sh, x_cp0], [0.85 * r_cp, x_cp0], [r_cp, x_cp0 + 4.0], [r_cp, x_mid - 3.0], [r_sh, x_mid - 3.0]]},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [r_sh, x_mid - 3.0], [0.9 * r_cp, x_mid - 3.0], [0.9 * r_cp, x_mid + 3.0], [r_sh, x_mid + 3.0]]},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [r_sh, x_mid + 3.0], [r_cp, x_mid + 3.0], [r_cp, L / 2 - 6.0], [0.85 * r_cp, L / 2 - 2.0],
            [r_sh, L / 2 - 2.0]]},
    ]
    r_g = r_cp + 10.0
    x_g0, x_g1 = x_bh1 - 4.0, L / 2 - 1.0
    edge = math.radians(115.0)
    edge_y, edge_z = (r_g + 1.5) * math.sin(edge), zc + (r_g + 1.5) * math.cos(edge)
    leg_x0, leg_x1 = x_g0 + 0.2 * (x_g1 - x_g0), x_g1 - 0.2 * (x_g1 - x_g0)
    guard_parts = [{"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z),
                    "start_deg": -115.0, "angle_deg": 230.0,
                    "profile": [[r_g, x_g0], [r_g + 3.0, x_g0], [r_g + 3.0, x_g1], [r_g, x_g1]]}]
    for side in (-1.0, 1.0):
        y = side * edge_y
        guard_parts.append({"part": "box", "center": [(leg_x0 + leg_x1) / 2, y, (base_t + edge_z + 3.0) / 2],
                            "size": [leg_x1 - leg_x0, 3.0, edge_z + 3.0 - base_t]})
        guard_parts.append({"part": "box", "center": [(leg_x0 + leg_x1) / 2, y + side * 9.0, base_t + 1.5],
                            "size": [leg_x1 - leg_x0, 21.0, 3.0]})

    # Motor frame inside its envelope; terminal box and feet outside it.
    m0, m1 = L / 2, L / 2 + ML
    core = 0.86 * R
    motor_parts = [
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, m0], [0.78 * R, m0], [0.94 * R, m0 + 0.04 * ML], [0.94 * R, m0 + 0.10 * ML],
            [0.0, m0 + 0.10 * ML]]},
        {"part": "finned_frame", "origin": [m0 + 0.08 * ML, 0.0, zc], "axis": list(_X), "length": 0.72 * ML,
         "core_radius": core, "fin_tip_radius": R, "fin_thickness": max(2.0, 0.035 * R), "fin_count": 28,
         "up": list(_Z), "skip_center_deg": 180.0, "skip_half_width_deg": 34.0, "fin_inset": 0.02 * ML},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, m0 + 0.78 * ML], [0.94 * R, m0 + 0.78 * ML], [0.94 * R, m0 + 0.83 * ML], [0.0, m0 + 0.83 * ML]]},
        {"part": "revolve", "origin": origin, "axis": list(_X), "ref": list(_Z), "profile": [
            [0.0, m0 + 0.81 * ML], [0.97 * R, m0 + 0.81 * ML], [0.97 * R, m1 - 0.06 * ML],
            [0.90 * R, m1 - 0.015 * ML], [0.55 * R, m1], [0.0, m1]]},
    ]
    tb_len, tb_w, tb_h = 0.26 * ML, 0.75 * R, 0.30 * R
    tb_x = m0 + 0.42 * ML
    tb_z0 = zc + core - 4.0
    tb_z1 = zc + R + tb_h
    terminal_parts = [
        _rbox([tb_x, 0.0, (tb_z0 + tb_z1) / 2], [tb_len, tb_w, tb_z1 - tb_z0], 0.08 * tb_w),
        _rbox([tb_x, 0.0, tb_z1 + 2.0], [tb_len + 6.0, tb_w + 6.0, 4.0], 0.08 * tb_w + 3.0),
        {"part": "cylinder", "start": [tb_x, -tb_w / 2 + 2.0, zc + R + 0.4 * tb_h],
         "end": [tb_x, -tb_w / 2 - 0.12 * R, zc + R + 0.4 * tb_h], "radius": 0.07 * R},
    ]
    foot_y = 0.6 * R
    rail_top = zc - math.sqrt(core * core - foot_y * foot_y) + 6.0
    feet_parts, motor_bolts = [], []
    for side in (-1.0, 1.0):
        y = side * foot_y
        feet_parts.append({"part": "box", "center": [m0 + 0.45 * ML, y, (base_t + rail_top) / 2],
                           "size": [0.62 * ML, 0.16 * R, rail_top - base_t]})
        for x in (m0 + 0.21 * ML, m0 + 0.69 * ML):
            feet_parts.append(_rbox([x, y + side * 0.06 * R, base_t + 6.0], [0.14 * ML, 0.36 * R, 12.0], 3.0))
            motor_bolts.append(_bolt([x, y + side * 0.16 * R, base_t + 12.0], _Z, max(10.0, 0.13 * R)))

    # Cast feet under the volute and the bearing housing.
    z_ft = zc - 0.75 * rA
    support_parts = [
        {"part": "prism", "origin": [-0.38 * cw, 0.0, 0.0], "normal": list(_X), "u_axis": list(_Y),
         "outline": [[-0.36 * W, base_t], [0.36 * W, base_t], [0.2 * W, z_ft], [-0.2 * W, z_ft]],
         "depth": 0.76 * cw},
        _rbox([0.0, 0.0, base_t + 6.0], [0.9 * cw, 0.8 * W, 12.0], 4.0),
        {"part": "prism", "origin": [x_bh0 + 4.0, 0.0, 0.0], "normal": list(_X), "u_axis": list(_Y),
         "outline": [[-0.24 * W, base_t], [0.24 * W, base_t], [0.12 * W, zc - 0.8 * r_bh],
                     [-0.12 * W, zc - 0.8 * r_bh]], "depth": max(8.0, x_bh1 - x_bh0 - 14.0)},
        _rbox([(x_bh0 + x_bh1) / 2, 0.0, base_t + 5.0], [x_bh1 - x_bh0, 0.56 * W, 10.0], 3.0),
    ]
    nut = max(10.0, 0.11 * s)
    fastener_parts = [
        {"part": "polar_array", "origin": origin, "axis": list(_X), "count": 8, "start_deg": 22.5,
         "item": _bolt([x_cov, 0.0, zc + 0.84 * rcov], _X, max(8.0, 0.07 * s))},
        _bolt([0.0, -0.32 * W, base_t + 12.0], _Z, nut), _bolt([0.0, 0.32 * W, base_t + 12.0], _Z, nut),
        _bolt([(x_bh0 + x_bh1) / 2, -0.2 * W, base_t + 10.0], _Z, nut * 0.85),
        _bolt([(x_bh0 + x_bh1) / 2, 0.2 * W, base_t + 10.0], _Z, nut * 0.85),
    ] + motor_bolts
    plate_x0, plate_x1 = -L / 2, m1
    for x in (plate_x0 + 30.0, (plate_x0 + plate_x1) / 2, plate_x1 - 30.0):
        for side in (-1.0, 1.0):
            fastener_parts.append(_bolt([x, side * (W / 2 - 22.0), base_t], _Z, 16.0))
    baseplate_parts = [
        {"part": "prism", "origin": [0.0, 0.0, 0.0], "normal": list(_Z), "u_axis": list(_X),
         "outline": [[plate_x0, -W / 2], [plate_x1, -W / 2], [plate_x1, W / 2], [plate_x0, W / 2]],
         "depth": base_t, "corner_radius": 10.0},
    ]

    paint = "pump_paint"
    return [
        _component("body", paint, cited_body, body_parts),
        _component("bearing_frame", paint, cited_body, frame_parts),
        _component("support", paint, cited_body, support_parts),
        _component("shaft", "machined_steel", cited + ["rcp-pump-drive"], shaft_parts),
        _component("coupling", "machined_steel", cited + ["rcp-pump-drive"], coupling_parts),
        _component("coupling_guard", "guard_paint", cited + ["rcp-pump-drive"], guard_parts),
        _component("motor", "motor_paint", cited + ["rcp-motor-details"], motor_parts),
        _component("motor_terminal_box", "motor_paint", cited + ["rcp-motor-details"], terminal_parts),
        _component("motor_feet", "motor_paint", cited + ["rcp-motor-details"], feet_parts),
        _component("baseplate", "base_paint", cited + ["rcp-pump-baseplate"], baseplate_parts),
        _component("fasteners", "fastener_zinc", cited + ["rcp-fasteners"], fastener_parts),
        _nozzle(ports["inlet"], cited, "nozzle/inlet", paint),
        _nozzle(ports["outlet"], cited, "nozzle/outlet", paint),
    ]


# --- Tank ---------------------------------------------------------------------


def _tank(p, ports, cited):
    D, H, ho = p["diameter"], p["height"], p["outlet_height"]
    R = D / 2
    if ho < FLANGE_DN50["outer_radius"] + 5.0 or ho > H - FLANGE_DN50["outer_radius"]:
        raise RecipeError(f"outlet_height {ho / 1000:g} m leaves no room for the outlet flange")
    detail = cited + ["rcp-tank-details"]
    body_parts = [{"part": "revolve", "origin": [0.0, 0.0, 0.0], "axis": list(_Z), "ref": list(_X), "profile": [
        [0.0, 0.0], [R + 12.0, 0.0], [R + 12.0, 14.0], [R, 14.0], [R, H - 12.0], [R + 8.0, H - 12.0],
        [R + 8.0, H], [0.0, H]]}]
    for z in (H / 3, 2 * H / 3):
        if 30.0 < z < H - 30.0 and abs(z - ho) > 70.0:
            body_parts.append({"part": "revolve", "origin": [0.0, 0.0, 0.0], "axis": list(_Z), "ref": list(_X),
                               "profile": [[R - 1.0, z - 2.5], [R + 1.2, z - 2.5], [R + 1.2, z + 2.5],
                                           [R - 1.0, z + 2.5]]})
    chair_h = min(70.0, 0.3 * H)
    support_parts = [{"part": "polar_array", "origin": [0.0, 0.0, 0.0], "axis": list(_Z),
                      "angles_deg": [45.0, 135.0, 225.0, 315.0], "item": {
                          "part": "instances", "offsets": [[0.0, -12.0, 0.0], [0.0, 12.0, 0.0]], "item": {
                              "part": "box", "center": [R + 26.0, 0.0, chair_h / 2], "size": [56.0, 6.0, chair_h]}}},
                     {"part": "polar_array", "origin": [0.0, 0.0, 0.0], "axis": list(_Z),
                      "angles_deg": [45.0, 135.0, 225.0, 315.0], "item": {
                          "part": "box", "center": [R + 30.0, 0.0, chair_h - 5.0], "size": [60.0, 44.0, 10.0]}}]
    fastener_parts = [{"part": "polar_array", "origin": [0.0, 0.0, 0.0], "axis": list(_Z),
                       "angles_deg": [45.0, 135.0, 225.0, 315.0],
                       "item": _bolt([R + 40.0, 0.0, chair_h], _Z, 18.0)}]
    port = ports["outlet"]
    nozzle = _nozzle(port, cited, "nozzle/outlet", "vessel_paint")
    pad_axis = port["direction"]
    pad_center = [R * d for d in pad_axis[:2]] + [port["position_mm"][2]]
    nozzle["parts"].append({"part": "cylinder", "start": _along(pad_center, pad_axis, -10.0),
                            "end": _along(pad_center, pad_axis, 6.0), "radius": 58.0})
    nozzle["assumptions"] = sorted(set(nozzle["assumptions"]) | {"rcp-tank-details"})
    return [
        _component("body", "vessel_paint", detail, body_parts),
        _component("support", "vessel_paint", detail, support_parts),
        _component("fasteners", "fastener_zinc", cited + ["rcp-fasteners"], fastener_parts),
        nozzle,
    ]


# --- Valves -------------------------------------------------------------------


def _valve_body(p):
    L, D = p["length"], p["body_diameter"]
    t = FLANGE_DN50["thickness"]
    xe = L / 2 - t
    rn = min(NECK_RADIUS, 0.42 * D)
    if xe < 20.0 or rn <= BORE_RADIUS + 1.0:
        raise RecipeError(f"valve length {L / 1000:g} m or body_diameter {D / 1000:g} m is too small for DN50 "
                          f"flanged ends")
    profile = [[0.0, -xe], [rn, -xe], [rn, -0.8 * xe], [D / 2, -0.45 * xe], [D / 2, 0.45 * xe], [rn, 0.8 * xe],
               [rn, xe], [0.0, xe]]
    return xe, rn, {"part": "revolve", "origin": [0.0, 0.0, 0.0], "axis": list(_X), "ref": list(_Z),
                    "profile": profile}


def _valve_ends(ports, cited, xe):
    neck = FLANGE_DN50["thickness"] + 0.25 * xe
    return [_nozzle(ports["inlet"], cited, "nozzle/inlet", "valve_body", neck),
            _nozzle(ports["outlet"], cited, "nozzle/outlet", "valve_body", neck)]


def _isolation_valve(p, ports, cited):
    D = p["body_diameter"]
    xe, _, body = _valve_body(p)
    detail = cited + ["rcp-isolation-valve"]
    stem_parts = [
        {"part": "cylinder", "start": [0.0, 0.0, 0.30 * D], "end": [0.0, 0.0, 0.62 * D], "radius": 0.24 * D},
        {"part": "cylinder", "start": [0.0, 0.0, 0.56 * D], "end": [0.0, 0.0, 0.64 * D], "radius": 0.34 * D},
        {"part": "hex_prism", "start": [0.0, 0.0, 0.64 * D], "axis": list(_Z), "across_flats": 0.26 * D,
         "height": 0.08 * D},
        {"part": "cylinder", "start": [0.0, 0.0, 0.70 * D], "end": [0.0, 0.0, 1.04 * D], "radius": 0.06 * D},
    ]
    handle_parts = [{"part": "handwheel", "center": [0.0, 0.0, 1.02 * D], "axis": list(_Z),
                     "rim_radius": 0.45 * D, "rim_thickness": 0.09 * D, "spokes": 3, "hub_radius": 0.1 * D,
                     "hub_height": 0.12 * D}]
    fastener_parts = [{"part": "polar_array", "origin": [0.0, 0.0, 0.0], "axis": list(_Z), "count": 4,
                       "start_deg": 45.0, "item": _bolt([0.29 * D, 0.0, 0.64 * D], _Z, max(6.0, 0.1 * D))}]
    return [
        _component("body", "valve_body", detail, [body]),
        _component("stem", "valve_body", detail, stem_parts),
        _component("handle", "handwheel", detail, handle_parts),
        _component("fasteners", "fastener_zinc", cited + ["rcp-fasteners"], fastener_parts),
        *_valve_ends(ports, cited, xe),
    ]


def _check_valve(p, ports, cited):
    D = p["body_diameter"]
    xe, _, body = _valve_body(p)
    detail = cited + ["rcp-check-valve"]
    inlet, outlet = ports["inlet"]["position_mm"], ports["outlet"]["position_mm"]
    flow = [o - i for i, o in zip(inlet, outlet)]
    arrow = {"part": "arrow", "origin": [0.0, -(D / 2) + 6.0, 0.0], "normal": [0.0, -1.0, 0.0], "direction": flow,
             "length": 1.6 * 0.45 * xe, "width": 0.3 * D, "depth": 7.5}
    cover_parts = [
        {"part": "cylinder", "start": [0.0, 0.0, 0.30 * D], "end": [0.0, 0.0, 0.56 * D], "radius": 0.30 * D},
        {"part": "cylinder", "start": [0.0, 0.0, 0.50 * D], "end": [0.0, 0.0, 0.58 * D], "radius": 0.40 * D},
    ]
    fastener_parts = [{"part": "polar_array", "origin": [0.0, 0.0, 0.0], "axis": list(_Z), "count": 4,
                       "start_deg": 45.0, "item": _bolt([0.34 * D, 0.0, 0.58 * D], _Z, max(6.0, 0.09 * D))}]
    return [
        _component("body", "valve_body", detail, [body]),
        _component("flow_arrow", "valve_body", detail, [arrow]),
        _component("cover", "valve_body", detail, cover_parts),
        _component("fasteners", "fastener_zinc", cited + ["rcp-fasteners"], fastener_parts),
        *_valve_ends(ports, cited, xe),
    ]


# --- Indicator and boundary ---------------------------------------------------


def _indicator(p, ports, cited):
    D = p["diameter"]
    detail = cited + ["rcp-indicator"]
    back = D / 8
    case = {"part": "revolve", "origin": [0.0, back, D], "axis": [0.0, -1.0, 0.0], "ref": list(_Z), "profile": [
        [0.0, 0.0], [0.5 * D, 0.0], [0.5 * D, 0.25 * D], [0.42 * D, 0.25 * D], [0.42 * D, 0.2 * D], [0.0, 0.2 * D]]}
    dial = {"part": "cylinder", "start": [0.0, back - 0.2 * D, D], "end": [0.0, back - 0.2 * D - 1.0, D],
            "radius": 0.42 * D - 0.5}
    stem_parts = [
        {"part": "hex_prism", "start": [0.0, 0.0, 0.0], "axis": list(_Z), "across_flats": 0.28 * D,
         "height": 0.15 * D},
        {"part": "cylinder", "start": [0.0, 0.0, 0.15 * D], "end": [0.0, 0.0, 0.5 * D + 3.0], "radius": 0.08 * D},
    ]
    return [
        _component("stem", "machined_steel", detail, stem_parts),
        _component("body", "gauge_case", detail, [case]),
        _component("dial", "gauge_dial", detail, [dial]),
    ]


def _boundary(p, ports, cited):
    S = p["size"]
    port = ports["inlet"]
    d = port["direction"]
    t = FLANGE_DN50["thickness"]
    stub_end = _along(port["position_mm"], d, -(t + 0.25 * S))
    nozzle = _component("nozzle/inlet", "pipe_steel", cited + ["rcp-boundary", "rcp-flange-dn50"], [
        {"part": "flange", "face_center": port["position_mm"], "normal": d, "bore_radius": 30.15,
         "hub_radius": 42.0, "neck_radius": 33.15, "hub_length": 24.0, **FLANGE_DN50},
        {"part": "cylinder", "start": _along(port["position_mm"], d, -t), "end": stub_end, "radius": 30.15},
    ])
    marker = _component("body", "boundary_marker", cited + ["rcp-boundary"], [
        {"part": "prism", "origin": _along(stub_end, d, 2.0), "normal": [-c for c in d],
         "outline": [[-S / 2, -S / 2], [S / 2, -S / 2], [S / 2, S / 2], [-S / 2, S / 2]], "depth": 0.04 * S,
         "corner_radius": 0.06 * S},
    ], display_only=True)
    return [nozzle, marker]


_BUILDERS = {
    ("centrifugal_pump", 1): _pump,
    ("tank", 1): _tank,
    ("isolation_valve", 1): _isolation_valve,
    ("check_valve", 1): _check_valve,
    ("indicator", 1): _indicator,
    ("boundary", 1): _boundary,
}


def entity_recipe(entity) -> dict:
    """Return ``{generator, parameters_mm, ports, components, assumptions}`` for one entity.

    Raises ``RecipeError`` for unsupported kinds or parameters outside the
    recipe's supported range, ``KeyError``/``ValueError`` for template faults.
    """
    kind, version = entity["kind"], entity["template_version"]
    builder = _BUILDERS.get((kind, version))
    if builder is None:
        raise RecipeError(f"no detailed geometry recipe for {kind!r} version {version!r}")
    params, cited = {}, []
    for name, quantity in sorted(entity["parameters"].items()):
        value = quantity["value"]
        low, high = SUPPORTED_RANGES_M[kind].get(name, (None, None))
        if low is not None and not low <= value <= high:
            raise RecipeError(f"{kind} parameter {name} = {value:g} m is outside the detailed recipe's supported "
                              f"range [{low:g}, {high:g}] m")
        params[name] = value * 1000.0
        if quantity.get("assumption_id"):
            cited.append(quantity["assumption_id"])
    ports = template_ports(entity)
    components = builder(params, ports, sorted(set(cited)) + ["rcp-appearance"])
    keys = [c["key"] for c in components]
    if len(keys) != len(set(keys)):
        raise RecipeError(f"{kind} recipe produced duplicate component keys")
    recipe_ids = sorted({a for c in components for a in c["assumptions"] if a in RECIPE_ASSUMPTIONS})
    return {
        "generator": f"{GENERATOR}/{kind}@{version}",
        "parameters_mm": {name: round(value, 9) for name, value in params.items()},
        "ports": ports,
        "components": components,
        "recipe_assumptions": recipe_ids,
    }
