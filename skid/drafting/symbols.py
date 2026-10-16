"""Original sheet symbols for the drafting adapter.

Every outline was drawn for this project from plain SVG primitives; none is
copied or traced from ISA-5.1, ISO 10628, or another symbol library, and no
drafting-standard conformance is claimed. Symbols are drawn in local units
(+x right, +y down) and placed by ``translate(x' y') rotate(angle)`` at the
paper position of their stored occurrence. Their size is fixed: each outline
passes through the diagram anchors that ``skid.templates`` defines, so stored
edge points keep meeting the symbol at its ports.

Line-weight hierarchy (drawing units): process lines 1.4, equipment 1.2,
valves and boundary flags 1.0, instrument bubbles 0.9, conceptual relations
0.9 dotted, detail lines 0.45.

Composed equipment (``kind: composed_equipment``) has no kind-specific
artwork. Its definition names one of a few reusable glyph families
(``FAMILIES``) and declares the anchor of each port. The family outline is
scaled about the symbol origin to the largest size, within fixed bounds, at
which no anchor lies inside it; anchors off the outline get a straight
nozzle stub from the outline. An anchor that still lies inside the outline at
the smallest drawn size cannot be met and is marked as unmatched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from .svgkit import INK, PAPER, PENDING, fix, num

__all__ = ["COMPOSED_KIND", "FALLBACK", "FAMILIES", "Glyph", "SYMBOLS", "SymbolSpec", "composed_spec", "symbol_for",
           "symbol_for_entity"]

COMPOSED_KIND = "composed_equipment"

EQUIPMENT_WEIGHT = 1.2
VALVE_WEIGHT = 1.0
INSTRUMENT_WEIGHT = 0.9
DETAIL_WEIGHT = 0.45
PROCESS_WEIGHT = 1.4


def _w(weight, scale):
    return f"{weight / scale:g}"


def _outline(d, weight, scale, fill=PAPER, **extra):
    attrs = {"d": d, "fill": fill, "stroke": INK, "stroke-width": _w(weight, scale), "stroke-linejoin": "round"}
    attrs.update(extra)
    return ("path", attrs)


def _stroke(d, weight, scale, **extra):
    attrs = {"d": d, "fill": "none", "stroke": INK, "stroke-width": _w(weight, scale)}
    attrs.update(extra)
    return ("path", attrs)


def _circle(r, weight, scale, fill=PAPER, cx=0, cy=0):
    return ("circle", {"cx": f"{cx:g}", "cy": f"{cy:g}", "r": f"{r:g}", "fill": fill, "stroke": INK,
                       "stroke-width": _w(weight, scale)})


@dataclass(frozen=True)
class SymbolSpec:
    """Artwork and layout hints for one equipment kind."""

    kind: str
    key: str  # legend key
    title: str  # legend title
    description: str  # legend description
    draw: Callable[[float], list]  # stroke scale -> [(tag, attrs)]
    bbox: tuple  # local (x0, y0, x1, y1)
    attach: str  # "circle" (radius about the origin) or "box"
    radius: float = 0.0
    inner: str = ""  # "" (tag outside), "single" (one line inside), "split" (two lines inside a bubble)
    inner_offset: tuple = (0.0, 0.0)
    inner_width: float = 0.0
    label_sides: tuple = ("below", "above", "right", "left")
    tag_size: float = 9.0
    category: str = "equipment"
    inner_width_across: float = 0.0  # inner width when the occurrence is turned a quarter turn (0: same)
    glyph: object = field(default=None, compare=False)  # fitted Glyph of a composed-equipment symbol


def _tank(scale):
    return [
        _outline("M -40 -38 A 40 14 0 0 1 40 -38 L 40 46 A 40 14 0 0 1 -40 46 Z", EQUIPMENT_WEIGHT, scale),
        _stroke("M -40 -38 L 40 -38 M -40 46 L 40 46", DETAIL_WEIGHT, scale, **{"stroke-dasharray": "4 2"}),
    ]


def _pump(scale):
    return [
        _stroke("M -17 22.2 L -23 34 M 17 22.2 L 23 34 M -29 34 L 29 34", EQUIPMENT_WEIGHT, scale),
        _circle(28, EQUIPMENT_WEIGHT, scale),
        _stroke("M 0 -3.5 L 0 -28", VALVE_WEIGHT, scale),
        ("circle", {"cx": "0", "cy": "0", "r": "3.5", "fill": INK, "stroke": "none"}),
    ]


def _isolation_valve(scale):
    return [_outline("M -16 -8 L 16 8 L 16 -8 L -16 8 Z", VALVE_WEIGHT, scale)]


def _check_valve(scale):
    return [
        _outline("M -16 -8 L 4 0 L -16 8 Z", VALVE_WEIGHT, scale),
        _stroke("M 4 -8.5 L 4 8.5", PROCESS_WEIGHT, scale),
        _stroke("M 4 0 L 16 0", PROCESS_WEIGHT, scale),
    ]


def _indicator(scale):
    return [_circle(16, INSTRUMENT_WEIGHT, scale)]


def _boundary(scale):
    return [
        _outline("M -18 -9 L 8 -9 L 18 0 L 8 9 L -18 9 Z", VALVE_WEIGHT, scale),
        _stroke("M -4 -4.5 L 1 0 L -4 4.5", DETAIL_WEIGHT * 2, scale, **{"stroke-linejoin": "round"}),
    ]


def _fallback(scale):
    return [
        ("rect", {"x": "-20", "y": "-15", "width": "40", "height": "30", "fill": PAPER, "stroke": INK,
                  "stroke-width": _w(VALVE_WEIGHT, scale), "stroke-dasharray": "4 2"}),
        _stroke("M -6 -6 L 6 6 M 6 -6 L -6 6", VALVE_WEIGHT, scale),
    ]


SYMBOLS = {
    # Outlet anchor (40, 20) lies on the right shell wall.
    "tank": SymbolSpec(
        kind="tank",
        key="symbol/tank",
        title="TANK",
        description="Vertical vessel outline with dished heads; dashed lines mark the head-to-shell transitions. "
        "Capacity, levels and design data are not modeled.",
        draw=_tank,
        bbox=(-40, -52, 40, 60),
        attach="box",
        inner="single",
        inner_offset=(0.0, 4.0),
        inner_width=66.0,
        label_sides=("below", "left", "right", "above"),
        tag_size=10.0,
    ),
    # Inlet anchor (-28, 0) and outlet anchor (0, -28) lie on the casing circle.
    "centrifugal_pump": SymbolSpec(
        kind="centrifugal_pump",
        key="symbol/centrifugal_pump",
        title="CENTRIFUGAL PUMP",
        description="Casing circle with impeller hub and radial discharge, on a base. Suction and discharge "
        "follow the drawn port positions. Duty and driver rating are not modeled.",
        draw=_pump,
        bbox=(-29, -28, 29, 34),
        attach="circle",
        radius=28.0,
        label_sides=("below", "right", "left", "above"),
        tag_size=10.0,
    ),
    # Anchors (-16, 0) and (16, 0) lie on the end faces.
    "isolation_valve": SymbolSpec(
        kind="isolation_valve",
        key="symbol/isolation_valve",
        title="ISOLATION VALVE",
        description="Two-way valve body. Operator, normal position and fail action are not modeled, so none is shown.",
        draw=_isolation_valve,
        bbox=(-16, -8, 16, 8),
        attach="box",
        label_sides=("above", "below", "right", "left"),
        tag_size=8.5,
        category="valve",
    ),
    "check_valve": SymbolSpec(
        kind="check_valve",
        key="symbol/check_valve",
        title="CHECK (NON-RETURN) VALVE",
        description="The triangle points in the permitted flow direction; the bar marks the blocked reverse "
        "direction. Valve type and rating are not modeled.",
        draw=_check_valve,
        bbox=(-16, -8.5, 16, 8.5),
        attach="box",
        label_sides=("above", "below", "right", "left"),
        tag_size=8.5,
        category="valve",
    ),
    "indicator": SymbolSpec(
        kind="indicator",
        key="symbol/indicator",
        title="INDICATOR (CONCEPTUAL)",
        description="Bubble with the indicator tag inside. Mounting location, range, hookup, alarms and control "
        "functions are not modeled.",
        draw=_indicator,
        bbox=(-16, -16, 16, 16),
        attach="circle",
        radius=16.0,
        inner="split",
        inner_width=27.0,
        label_sides=("right", "left", "above", "below"),
        tag_size=8.0,
        category="instrument",
    ),
    # Inlet anchor (-18, 0) lies on the flag's tail.
    "boundary": SymbolSpec(
        kind="boundary",
        key="symbol/boundary",
        title="SCOPE BOUNDARY",
        description="Flow continues beyond the modeled scope; the destination is not modeled.",
        draw=_boundary,
        bbox=(-18, -9, 18, 9),
        attach="box",
        label_sides=("below", "above", "right", "left"),
        tag_size=8.5,
        category="boundary",
    ),
}

FALLBACK = SymbolSpec(
    kind="",
    key="symbol/unsupported",
    title="UNSUPPORTED SYMBOL",
    description="Placeholder for an item kind without sheet artwork; the build reports it as an open item.",
    draw=_fallback,
    bbox=(-20, -15, 20, 15),
    attach="box",
    category="equipment",
)


def symbol_for(kind: str) -> SymbolSpec | None:
    return SYMBOLS.get(kind)


# --- Reusable glyph families for composed equipment --------------------------------------

ON_OUTLINE = 0.5  # an anchor this close to the outline meets it without a stub (drawing units)
MIN_SCALE = 0.6
MAX_SCALE = 3.0


@dataclass(frozen=True)
class Family:
    """A generic outline: a straight section of half-length ``tangent`` along ``axis`` with
    semi-elliptical heads of depth ``head`` (0 for a rectangle), ``half`` wide either side.

    ``detail`` names optional interior artwork drawn at the fitted scale (``""``: dashed
    tangent lines on headed outlines); ``tag_inside`` False places the tag outside the body
    because interior artwork would collide with it."""

    name: str
    axis: str  # "x" (horizontal) or "y" (vertical)
    tangent: float
    head: float
    half: float
    title: str
    description: str
    label_sides: tuple
    sample_anchors: tuple  # ((port name, (x, y)), ...) for the legend sample
    detail: str = ""
    tag_inside: bool = True


FAMILIES = {
    "horizontal_vessel": Family(
        "horizontal_vessel", "x", 40.0, 12.0, 24.0,
        "HORIZONTAL VESSEL (COMPOSED EQUIPMENT)",
        "Generic outline for project-defined equipment: horizontal shell with dished ends; dashed lines mark the "
        "tangent lines. Stubs lead to the connection points the definition declares. The outline implies no "
        "capacity, internals, supports or design data.",
        ("below", "above", "right", "left"),
        (("a", (-52.0, 0.0)), ("b", (16.0, -34.0))),
    ),
    "vertical_vessel": Family(
        "vertical_vessel", "y", 40.0, 12.0, 24.0,
        "VERTICAL VESSEL (COMPOSED EQUIPMENT)",
        "Generic outline for project-defined equipment: vertical shell with dished ends; dashed lines mark the "
        "tangent lines. Stubs lead to the connection points the definition declares. The outline implies no "
        "capacity, internals, supports or design data.",
        ("right", "left", "below", "above"),
        (("a", (0.0, 52.0)), ("b", (-34.0, -16.0))),
    ),
    "box_equipment": Family(
        "box_equipment", "x", 30.0, 0.0, 22.0,
        "EQUIPMENT, GENERIC OUTLINE (COMPOSED EQUIPMENT)",
        "Rectangular outline for project-defined equipment without a more specific generic form. Stubs lead to the "
        "connection points the definition declares. The outline implies no function, rating or internal arrangement.",
        ("below", "above", "right", "left"),
        (("a", (-30.0, 0.0)), ("b", (0.0, -30.0))),
    ),
    "heat_exchanger": Family(
        "heat_exchanger", "x", 44.0, 12.0, 22.0,
        "SHELL-AND-TUBE HEAT EXCHANGER (COMPOSED EQUIPMENT)",
        "Generic outline for a project-defined exchanger: horizontal shell with a front channel divided by a pass "
        "partition, solid tubesheet lines and a schematic tube bundle; the right head is a return bonnet. Stubs "
        "lead to the connection points the definition declares. Which ports form separate circuits comes from the "
        "definition's declared flow paths, not from this artwork. No duty, area, pass count, rating or internal "
        "design is implied.",
        ("below", "above", "right", "left"),
        (("tube_in", (-90.0, 14.0)), ("tube_out", (-90.0, -14.0)), ("shell_in", (40.0, 58.0)),
         ("shell_out", (-10.0, -58.0))),
        detail="shell_and_tube",
        tag_inside=False,
    ),
}


@dataclass(frozen=True)
class Glyph:
    """A family outline fitted to declared anchors (symbol-local units, +y down)."""

    family: str
    scale: float
    tangent: float
    head: float
    half: float
    axis: str
    anchors: tuple  # ((name, (x, y)), ...) as declared
    stubs: tuple  # ((name, outline point, anchor), ...)
    unmatched: tuple  # names of anchors inside the outline

    @property
    def extent(self):
        """Half extents (x, y) of the body."""
        along = self.tangent + self.head
        return (along, self.half) if self.axis == "x" else (self.half, along)

    @property
    def body_box(self):
        hx, hy = self.extent
        return (-hx, -hy, hx, hy)


def _to_body(axis, point):
    """(along, across) coordinates of a symbol-local point."""
    return (point[0], point[1]) if axis == "x" else (point[1], point[0])


def _from_body(axis, along, across):
    return (along, across) if axis == "x" else (across, along)


def _inside(family, scale, point):
    """True when ``point`` lies strictly inside the family outline drawn at ``scale``."""
    u, v = _to_body(family.axis, point)
    u, v = abs(u), abs(v)
    t, h, w = family.tangent * scale, family.head * scale, family.half * scale
    if v >= w:
        return False
    if u < t:
        return True
    return h > 0 and ((u - t) / h) ** 2 + (v / w) ** 2 < 1.0


def boundary_scale(family, point):
    """Largest scale at which ``point`` is not inside the outline: the gauge of the convex outline,
    approached from below so that an outline drawn at this scale passes through or inside the point."""
    if math.hypot(*point) == 0:
        return 0.0
    low, high = 0.0, 1.0
    while not _inside(family, high, point) and high < 2.0 ** 60:
        low, high = high, high * 2.0
    for _ in range(80):
        middle = (low + high) / 2
        if _inside(family, middle, point):
            high = middle
        else:
            low = middle
    return low


def _outline_point(family, scale, point):
    """Where a nozzle stub from an outside ``point`` meets the outline: perpendicular to the
    side or head it faces, or toward the symbol origin from a corner region."""
    u, v = _to_body(family.axis, point)
    t, h, w = family.tangent * scale, family.head * scale, family.half * scale
    end = t + h
    if abs(u) <= t:
        return _from_body(family.axis, u, math.copysign(w, v))
    if abs(u) < end and v != 0:
        across = w * math.sqrt(max(0.0, 1.0 - ((abs(u) - t) / h) ** 2))
        return _from_body(family.axis, u, math.copysign(across, v))
    if abs(v) < w:
        along = t + (h * math.sqrt(max(0.0, 1.0 - (v / w) ** 2)) if h > 0 else 0.0)
        return _from_body(family.axis, math.copysign(along, u), v)
    gauge = boundary_scale(family, point)
    return (point[0] * scale / gauge, point[1] * scale / gauge)


def fit_glyph(family, anchors):
    """Fit ``family`` to ``anchors`` ({port name: (x, y)}); see the module docstring."""
    declared = tuple(sorted((str(name), (float(xy[0]), float(xy[1]))) for name, xy in anchors.items()))
    gauges = [boundary_scale(family, xy) for _, xy in declared]
    scale = min(gauges) if gauges else 1.0
    scale = min(MAX_SCALE, max(MIN_SCALE, scale))
    stubs, unmatched = [], []
    for name, xy in declared:
        if _inside(family, scale, xy):
            unmatched.append(name)
            continue
        start = _outline_point(family, scale, xy)
        if math.dist(start, xy) > ON_OUTLINE:
            stubs.append((name, start, xy))
    return Glyph(family.name, scale, family.tangent * scale, family.head * scale, family.half * scale, family.axis,
                 declared, tuple(stubs), tuple(unmatched))


def _body_path(glyph):
    t, h, w = glyph.tangent, glyph.head, glyph.half
    if h <= 0:
        x, y = glyph.extent
        return f"M {fix(-x)} {fix(-y)} L {fix(x)} {fix(-y)} L {fix(x)} {fix(y)} L {fix(-x)} {fix(y)} Z"
    if glyph.axis == "x":
        return (f"M {fix(-t)} {fix(-w)} L {fix(t)} {fix(-w)} A {fix(h)} {fix(w)} 0 0 1 {fix(t)} {fix(w)} "
                f"L {fix(-t)} {fix(w)} A {fix(h)} {fix(w)} 0 0 1 {fix(-t)} {fix(-w)} Z")
    return (f"M {fix(-w)} {fix(-t)} A {fix(w)} {fix(h)} 0 0 1 {fix(w)} {fix(-t)} L {fix(w)} {fix(t)} "
            f"A {fix(w)} {fix(h)} 0 0 1 {fix(-w)} {fix(t)} Z")


def _shell_and_tube_detail(glyph, scale):
    """Interior artwork of the exchanger family (horizontal axis), drawn inside the fitted outline:
    a solid front tubesheet line closing a channel, a pass-partition line across the channel, a solid
    rear tubesheet line at the right tangent (the right head is the return bonnet) and four tube
    lines between the tubesheets. Original schematic artwork, not a standard or vendor symbol."""
    t, h, w = glyph.tangent, glyph.head, glyph.half
    front = -0.4 * t
    partition = -t - 0.7 * h
    tubes = " ".join(f"M {fix(front)} {fix(y)} L {fix(t)} {fix(y)}" for y in (-0.55 * w, -0.2 * w, 0.2 * w, 0.55 * w))
    return [
        _stroke(f"M {fix(front)} {fix(-w)} L {fix(front)} {fix(w)} M {fix(t)} {fix(-w)} L {fix(t)} {fix(w)}",
                DETAIL_WEIGHT * 2, scale, **{"data-role": "tubesheet"}),
        _stroke(f"M {fix(partition)} 0 L {fix(front)} 0", DETAIL_WEIGHT * 2, scale, **{"data-role": "pass-partition"}),
        _stroke(tubes, DETAIL_WEIGHT, scale, **{"data-role": "tube-bundle"}),
    ]


def _glyph_drawing(glyph):
    def draw(scale):
        items = [_outline(_body_path(glyph), EQUIPMENT_WEIGHT, scale, **{"data-role": "body"})]
        t, w = glyph.tangent, glyph.half
        if FAMILIES[glyph.family].detail == "shell_and_tube":
            items += _shell_and_tube_detail(glyph, scale)
        elif glyph.head > 0:
            tangents = (f"M {fix(-t)} {fix(-w)} L {fix(-t)} {fix(w)} M {fix(t)} {fix(-w)} L {fix(t)} {fix(w)}"
                        if glyph.axis == "x" else
                        f"M {fix(-w)} {fix(-t)} L {fix(w)} {fix(-t)} M {fix(-w)} {fix(t)} L {fix(w)} {fix(t)}")
            items.append(_stroke(tangents, DETAIL_WEIGHT, scale, **{"stroke-dasharray": "4 2"}))
        for name, start, end in glyph.stubs:
            items.append(_stroke(f"M {fix(start[0])} {fix(start[1])} L {num(end[0])} {num(end[1])}", EQUIPMENT_WEIGHT,
                                 scale, **{"data-role": "nozzle-stub", "data-port-name": name}))
        anchors = dict(glyph.anchors)
        for name in glyph.unmatched:
            x, y = anchors[name]
            items.append(("circle", {"cx": num(x), "cy": num(y), "r": "4.5", "fill": "none", "stroke": PENDING,
                                     "stroke-width": _w(VALVE_WEIGHT, scale), "stroke-dasharray": "1.6 1.4",
                                     "data-role": "unmatched-anchor", "data-port-name": name,
                                     "data-legend-keys": "marker/unmatched-anchor"}))
        return items

    return draw


@lru_cache(maxsize=256)
def _composed_spec(shape, anchors):
    family = FAMILIES[shape]
    glyph = fit_glyph(family, dict(anchors))
    xs = [glyph.body_box[0], glyph.body_box[2]] + [xy[0] for _, xy in glyph.anchors]
    ys = [glyph.body_box[1], glyph.body_box[3]] + [xy[1] for _, xy in glyph.anchors]
    straight = 2 * glyph.tangent * 0.92 if glyph.head > 0 else 2 * glyph.tangent * 0.85
    across = 2 * glyph.half * 0.85
    along_width, turned_width = (straight, across) if glyph.axis == "x" else (across, straight)
    if not family.tag_inside:
        along_width = turned_width = 0.0
    return SymbolSpec(
        kind=COMPOSED_KIND,
        key=f"symbol/{shape}",
        title=family.title,
        description=family.description,
        draw=_glyph_drawing(glyph),
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        attach="box",
        inner="single" if family.tag_inside else "",
        inner_width=along_width,
        inner_width_across=turned_width,
        label_sides=family.label_sides,
        tag_size=10.0,
        category="equipment",
        glyph=glyph,
    )


def composed_spec(shape: str, anchors) -> SymbolSpec | None:
    """Artwork for one family fitted to ``anchors`` ({port name: [x, y]}); None for an unknown family."""
    if shape not in FAMILIES or not isinstance(anchors, dict):
        return None
    try:
        key = tuple(sorted((str(name), (float(xy[0]), float(xy[1]))) for name, xy in anchors.items()))
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(c) for _, xy in key for c in xy):
        return None
    return _composed_spec(shape, key)


def family_sample(shape: str) -> SymbolSpec:
    """Nominal artwork of a family for its legend sample (never carries drawing legend keys)."""
    return composed_spec(shape, dict(FAMILIES[shape].sample_anchors))


def symbol_for_entity(entity, anchors=None) -> SymbolSpec | None:
    """Artwork for one entity: built-in kinds by kind; composed equipment from the glyph family its
    definition declares, fitted to ``anchors`` (the entity template's diagram anchors; the
    definition's own symbol anchors when omitted). None means no artwork is available."""
    kind = entity.get("kind")
    definition = entity.get("definition")
    if kind != COMPOSED_KIND:
        return symbol_for(kind)
    symbol = definition.get("symbol") if isinstance(definition, dict) else None
    if not isinstance(symbol, dict):
        return None
    return composed_spec(symbol.get("shape"), symbol.get("anchors") if anchors is None else anchors)
