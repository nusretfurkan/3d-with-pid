"""The closed vocabulary a definition may use: part signatures, materials, symbols.

``skid.geometry.parts`` and ``skid.geometry.materials`` own the CAD
implementation; they import OCCT, which this pure layer must not. The
signatures below mirror them for static checks, and
``tests/test_cad_equipment.py`` asserts that the two stay identical.
``estimated_solids`` gives an upper bound on the solids a part call expands to,
used to bound the work an authored definition can request.
"""

from __future__ import annotations

__all__ = [
    "BOOLEAN_ARGUMENTS",
    "FLANGE_FIELDS",
    "INTEGER_ARGUMENTS",
    "ITEM_PARTS",
    "MATERIAL_NAMES",
    "NULLABLE_ARGUMENTS",
    "PART_SIGNATURES",
    "SYMBOL_SHAPES",
    "estimated_solids",
]

# name -> (required arguments, optional arguments); mirrors skid.geometry.parts.PARTS.
PART_SIGNATURES = {
    "box": (("center", "size"), ("rotation_deg",)),
    "cylinder": (("start", "end", "radius"), ()),
    "cone": (("start", "end", "radius_start", "radius_end"), ()),
    "sphere": (("center", "radius"), ()),
    "torus": (("center", "axis", "major_radius", "minor_radius"), ()),
    "revolve": (("origin", "axis", "profile"), ("ref", "angle_deg", "start_deg")),
    "prism": (("origin", "normal", "outline", "depth"), ("u_axis", "holes", "corner_radius")),
    "hex_prism": (("start", "axis", "across_flats", "height"), ("phase_deg",)),
    "loft": (("sections",), ("ruled",)),
    "pipe": (("points", "radius", "bend_radius"), ()),
    "polar_array": (("item", "origin", "axis"), ("count", "angles_deg", "start_deg")),
    "linear_array": (("item", "count", "step"), ()),
    "instances": (("item", "offsets"), ()),
    "tube": (("start", "end", "outer_radius", "inner_radius"), ()),
    "flange": (("face_center", "normal", "outer_radius", "thickness", "bore_radius", "bolt_count",
                "bolt_circle_radius", "hole_radius"), ("phase_deg", "hub_radius", "hub_length", "neck_radius")),
    "flanged_nozzle": (("port", "direction", "neck_length", "neck_radius", "bore_radius", "flange"), ()),
    "bolting": (("face_center", "normal", "thickness_back", "thickness_front", "bolt_count", "bolt_circle_radius",
                 "stud_radius", "nut_across_flats", "nut_height"), ("phase_deg", "protrusion")),
    "bolt": (("seat", "axis", "across_flats", "height", "stud_radius", "stud_length"), ()),
    "volute_casing": (("center", "width", "tongue_radius", "outer_radius", "neck_half_width", "neck_top", "chamfer"),
                      ("samples",)),
    "finned_frame": (("origin", "axis", "length", "core_radius", "fin_tip_radius", "fin_thickness", "fin_count"),
                     ("up", "skip_center_deg", "skip_half_width_deg", "fin_inset")),
    "handwheel": (("center", "axis", "rim_radius", "rim_thickness", "spokes", "hub_radius", "hub_height"), ()),
    "arrow": (("origin", "normal", "direction", "length", "width", "depth"), ()),
}

# Parts that repeat a nested ``item`` call.
ITEM_PARTS = frozenset({"polar_array", "linear_array", "instances"})

# Integer arguments must be integer literals in these inclusive ranges (as parts enforce).
INTEGER_ARGUMENTS = {
    ("polar_array", "count"): (1, 512),
    ("linear_array", "count"): (1, 512),
    ("flange", "bolt_count"): (0, 64),
    ("bolting", "bolt_count"): (1, 512),
    ("volute_casing", "samples"): (6, 64),
    ("finned_frame", "fin_count"): (3, 256),
    ("handwheel", "spokes"): (2, 12),
}
BOOLEAN_ARGUMENTS = frozenset({("loft", "ruled")})
NULLABLE_ARGUMENTS = frozenset({("revolve", "ref"), ("prism", "u_axis"), ("polar_array", "count"),
                                ("polar_array", "angles_deg")})

# flanged_nozzle's ``flange`` object: (required, optional); bolt_count is an integer in [0, 64].
FLANGE_FIELDS = (("outer_radius", "thickness", "bolt_count", "bolt_circle_radius", "hole_radius"), ("phase_deg",))

# Mirrors the keys of skid.geometry.materials.MATERIALS (appearance only).
MATERIAL_NAMES = frozenset({
    "pump_paint", "motor_paint", "guard_paint", "base_paint", "machined_steel", "fastener_zinc", "pipe_steel",
    "vessel_paint", "valve_body", "handwheel", "gauge_case", "gauge_dial", "boundary_marker", "pending_route",
})

# The reusable P&ID symbol vocabulary a definition may name (artwork belongs to the drafting layer).
SYMBOL_SHAPES = ("horizontal_vessel", "vertical_vessel", "box_equipment", "heat_exchanger")


def _integer(call, name, default):
    value = call.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def estimated_solids(call) -> int:
    """Upper bound of the solids one checked part call produces (nested items included)."""
    name = call["part"]
    if name == "polar_array":
        angles = call.get("angles_deg")
        count = len(angles) if isinstance(angles, list) else _integer(call, "count", 1)
        return count * estimated_solids(call["item"])
    if name == "linear_array":
        return _integer(call, "count", 1) * estimated_solids(call["item"])
    if name == "instances":
        offsets = call.get("offsets")
        return (len(offsets) if isinstance(offsets, list) else 1) * estimated_solids(call["item"])
    if name == "flange":
        return 2
    if name == "flanged_nozzle":
        return 3
    if name == "bolting":
        return 3 * _integer(call, "bolt_count", 1)
    if name == "bolt":
        return 2
    if name == "finned_frame":
        return 1 + _integer(call, "fin_count", 3)
    if name == "handwheel":
        return 2 + _integer(call, "spokes", 2)
    return 1
