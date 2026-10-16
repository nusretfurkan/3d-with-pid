"""Stable legend keys, their sheet styles, legend text and legend sample painters.

A key names one drawn symbol variant, line style, marker, annotation
convention or abbreviation. The P&ID tags every emitted element that uses a
convention with ``data-legend-keys``; the legend sheet lists exactly the keys
found in the emitted P&ID. Legend samples are painted here and carry only
``data-legend-sample``, so they can never be counted as drawing inventory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from . import symbols as symbol_defs
from .svgkit import INK, MUTED, PAPER, PENDING, RELATION, Line, Run, draw_text_line, fix, line, sub, text_width

SECTIONS = (
    ("sheet", "SHEET REFERENCES"),
    ("equipment", "EQUIPMENT, VALVES AND BOUNDARIES"),
    ("instrument", "INSTRUMENTS"),
    ("line", "LINES AND RELATIONS"),
    ("marker", "MARKERS"),
    ("annotation", "ANNOTATION CONVENTIONS"),
    ("abbreviation", "ABBREVIATIONS"),
)

LINE_STYLES = {
    "line/process": (("stroke", INK), ("stroke-width", "1.4")),
    "line/process/needs-reroute": (("stroke", PENDING), ("stroke-width", "1.4"), ("stroke-dasharray", "9 3 2 3")),
    "line/process/unresolved": (("stroke", PENDING), ("stroke-width", "1.2"), ("stroke-dasharray", "2.5 2.5")),
    "line/process/topology-hint": (("stroke", PENDING), ("stroke-width", "0.8"), ("stroke-dasharray", "6 3")),
    "line/process/detached-end": (("stroke", PENDING), ("stroke-width", "0.9"), ("stroke-dasharray", "1 2.5")),
    "line/observation": (
        ("stroke", RELATION),
        ("stroke-width", "0.9"),
        ("stroke-dasharray", "0.1 2.6"),
        ("stroke-linecap", "round"),
    ),
}
PATH_STATUS_KEYS = {
    "current": "line/process",
    "needs_reroute": "line/process/needs-reroute",
    "unresolved": "line/process/unresolved",
}
BADGE_TEXT = {
    "line/process/needs-reroute": "P&ID PATH NEEDS REROUTE",
    "line/process/unresolved": "P&ID PATH UNRESOLVED",
    "line/process/topology-hint": "NO DRAWABLE P&ID PATH",
    "marker/physical-placement-pending": "3D PLACEMENT PENDING",
    "marker/physical-route-pending": "3D ROUTE PENDING",
}
ARROW_LENGTH = 7.0
ARROW_HALF_WIDTH = 2.8
BADGE_SIZE = 5.6


@dataclass(frozen=True)
class Convention:
    key: str
    section: str
    title: str
    description: str
    paint: Callable  # (group, x, y, width, height) -> None


def arrow_path(tip, direction):
    """Filled flow arrowhead with its tip at ``tip`` pointing along unit ``direction``."""
    ux, uy = direction
    bx, by = tip[0] - ux * ARROW_LENGTH, tip[1] - uy * ARROW_LENGTH
    left = (bx - uy * ARROW_HALF_WIDTH, by + ux * ARROW_HALF_WIDTH)
    right = (bx + uy * ARROW_HALF_WIDTH, by - ux * ARROW_HALF_WIDTH)
    return f"M {fix(tip[0])} {fix(tip[1])} L {fix(left[0])} {fix(left[1])} L {fix(right[0])} {fix(right[1])} Z"


def _sample(group, key):
    return sub(group, "g", (("data-legend-sample", key),))


def _paint_symbol(kind, spec=None):
    spec = spec or symbol_defs.SYMBOLS.get(kind, symbol_defs.FALLBACK)

    def paint(group, x, y, width, height):
        x0, y0, x1, y1 = spec.bbox
        scale = min(1.0, (width - 6) / (x1 - x0), (height - 4) / (y1 - y0))
        cx, cy = x + width / 2 - (x0 + x1) / 2 * scale, y + height / 2 - (y0 + y1) / 2 * scale
        sample = _sample(group, spec.key)
        body = sub(sample, "g", (("transform", f"translate({fix(cx)} {fix(cy)}) scale({scale:g})"),))
        for tag, attrs in spec.draw(scale):
            sub(body, tag, attrs)
        if spec.inner == "split":
            sub(sample, "text", {"x": fix(cx), "y": fix(cy - 1), "font-size": "7", "font-weight": "bold",
                                 "text-anchor": "middle", "fill": INK}, "XX")
            sub(sample, "text", {"x": fix(cx), "y": fix(cy + 7), "font-size": "6.5", "text-anchor": "middle",
                                 "fill": INK}, "000")

    return paint


def _paint_line(key):
    def paint(group, x, y, width, height):
        sample = _sample(group, key)
        mid = y + height / 2
        attrs = {"x1": fix(x + 6), "y1": fix(mid), "x2": fix(x + width - 6), "y2": fix(mid), "fill": "none"}
        attrs.update(dict(LINE_STYLES[key]))
        sub(sample, "line", attrs)
        if key in BADGE_TEXT:
            _badge(sample, BADGE_TEXT[key], x + width / 2, mid - 9)

    return paint


def _badge(parent, text, cx, top, max_width=60.0):
    size = BADGE_SIZE
    while size > 3.6 and text_width(text, size, True) + 4 > max_width:
        size -= 0.1
    width = text_width(text, size, True) + 4
    sub(parent, "rect", {"x": fix(cx - width / 2), "y": fix(top), "width": fix(width), "height": fix(size * 1.26),
                         "rx": "1.5", "fill": PAPER, "stroke": PENDING, "stroke-width": "0.6"})
    sub(parent, "text", {"x": fix(cx), "y": fix(top + size * 0.9), "font-size": fix(size),
                         "font-weight": "bold", "text-anchor": "middle", "fill": PENDING}, text)


def _paint_observation(group, x, y, width, height):
    sample = _sample(group, "line/observation")
    mid = y + height / 2
    sub(sample, "circle", {"cx": fix(x + 12), "cy": fix(mid), "r": "6", "fill": PAPER, "stroke": INK, "stroke-width": "0.9"})
    sub(sample, "rect", {"x": fix(x + width - 18), "y": fix(mid - 6), "width": "12", "height": "12", "fill": PAPER,
                         "stroke": INK, "stroke-width": "1"})
    attrs = {"x1": fix(x + 18), "y1": fix(mid), "x2": fix(x + width - 18), "y2": fix(mid), "fill": "none"}
    attrs.update(dict(LINE_STYLES["line/observation"]))
    sub(sample, "line", attrs)


def _paint_unmatched(group, x, y, width, height):
    sample = _sample(group, "marker/unmatched-anchor")
    mid, cx = y + height / 2, x + width / 2
    sub(sample, "rect", {"x": fix(cx - 22), "y": fix(mid - 9), "width": "44", "height": "18", "fill": PAPER,
                         "stroke": INK, "stroke-width": "1.2"})
    sub(sample, "circle", {"cx": fix(cx + 8), "cy": fix(mid), "r": "4.5", "fill": "none", "stroke": PENDING,
                           "stroke-width": "1", "stroke-dasharray": "1.6 1.4"})


def _paint_arrow(group, x, y, width, height):
    sample = _sample(group, "marker/flow-arrow")
    mid = y + height / 2
    attrs = {"x1": fix(x + 6), "y1": fix(mid), "x2": fix(x + width - 6), "y2": fix(mid)}
    attrs.update(dict(LINE_STYLES["line/process"]))
    sub(sample, "line", attrs)
    sub(sample, "path", {"d": arrow_path((x + width / 2 + ARROW_LENGTH / 2, mid), (1.0, 0.0)), "fill": INK})


def _paint_crossing(group, x, y, width, height):
    sample = _sample(group, "marker/crossing-gap")
    mid, cx = y + height / 2, x + width / 2
    under = {"x1": fix(cx), "y1": fix(y + 3), "x2": fix(cx), "y2": fix(y + height - 3)}
    under.update(dict(LINE_STYLES["line/process"]))
    sub(sample, "line", under)
    sub(sample, "line", {"x1": fix(cx), "y1": fix(mid - 4.5), "x2": fix(cx), "y2": fix(mid + 4.5), "stroke": PAPER,
                         "stroke-width": "4"})
    over = {"x1": fix(x + 6), "y1": fix(mid), "x2": fix(x + width - 6), "y2": fix(mid)}
    over.update(dict(LINE_STYLES["line/process"]))
    sub(sample, "line", over)


def _paint_badge(key):
    def paint(group, x, y, width, height):
        sample = _sample(group, key)
        _badge(sample, BADGE_TEXT[key], x + width / 2, y + height / 2 - BADGE_SIZE * 0.63)

    return paint


def _paint_text(key, text, size=8.0, bold=True):
    def paint(group, x, y, width, height):
        sample = _sample(group, key)
        attrs = {"x": fix(x + width / 2), "y": fix(y + height / 2 + size * 0.35), "font-size": fix(size),
                 "text-anchor": "middle", "fill": INK}
        if bold:
            attrs["font-weight"] = "bold"
        sub(sample, "text", attrs, text)

    return paint


def _paint_designation(group, x, y, width, height):
    sample = _sample(group, "convention/line-designation")
    mid = y + height / 2 + 5
    attrs = {"x1": fix(x + 4), "y1": fix(mid), "x2": fix(x + width - 4), "y2": fix(mid)}
    attrs.update(dict(LINE_STYLES["line/process"]))
    sub(sample, "line", attrs)
    designation = Line((Run("TAG", True), Run(" DN")), 6.5)  # same explicit run layout as the sheet labels
    draw_text_line(sample, designation, x + width / 2, mid - 4 - 6.5 * 0.86, "middle")


def _paint_leader(group, x, y, width, height):
    sample = _sample(group, "convention/label-leader")
    line_y = y + height - 7
    attrs = {"x1": fix(x + 4), "y1": fix(line_y), "x2": fix(x + width - 4), "y2": fix(line_y)}
    attrs.update(dict(LINE_STYLES["line/process"]))
    sub(sample, "line", attrs)
    sub(sample, "line", {"x1": fix(x + width / 2), "y1": fix(line_y), "x2": fix(x + width / 2), "y2": fix(y + 12),
                         "stroke": INK, "stroke-width": "0.5"})
    sub(sample, "text", {"x": fix(x + width / 2), "y": fix(y + 10), "font-size": "6.5", "font-weight": "bold",
                         "text-anchor": "middle", "fill": INK}, "TAG")


def _paint_equipment_data(group, x, y, width, height):
    sample = _sample(group, "convention/equipment-data")
    rows = (("TAG", True, 7.0), ("ITEM TYPE", False, 5.5), ("DATA: NOT SPECIFIED", False, 5.5))
    top = y + 3
    for text, bold, size in rows:
        attrs = {"x": fix(x + width / 2), "y": fix(top + size), "font-size": fix(size), "text-anchor": "middle",
                 "fill": INK if bold else MUTED}
        if bold:
            attrs["font-weight"] = "bold"
        sub(sample, "text", attrs, text)
        if bold:
            w = text_width(text, size, True)
            sub(sample, "line", {"x1": fix(x + width / 2 - w / 2), "y1": fix(top + size + 1.4),
                                 "x2": fix(x + width / 2 + w / 2), "y2": fix(top + size + 1.4), "stroke": INK,
                                 "stroke-width": "0.6"})
        top += size * 1.35


def _paint_zone(group, x, y, width, height):
    sample = _sample(group, "convention/zone-grid")
    cols, rows = 3, 2
    gx, gy = x + 14, y + 4
    cw, ch = (width - 20) / cols, (height - 8) / rows
    for c in range(cols + 1):
        sub(sample, "line", {"x1": fix(gx + c * cw), "y1": fix(gy), "x2": fix(gx + c * cw), "y2": fix(gy + rows * ch),
                             "stroke": MUTED, "stroke-width": "0.5"})
    for r in range(rows + 1):
        sub(sample, "line", {"x1": fix(gx), "y1": fix(gy + r * ch), "x2": fix(gx + cols * cw), "y2": fix(gy + r * ch),
                             "stroke": MUTED, "stroke-width": "0.5"})
    for r, letter in enumerate("FE"):
        sub(sample, "text", {"x": fix(gx - 4), "y": fix(gy + (r + 0.5) * ch + 2.2), "font-size": "6",
                             "text-anchor": "end", "fill": INK}, letter)
    sub(sample, "rect", {"x": fix(gx + cw), "y": fix(gy + ch), "width": fix(cw), "height": fix(ch),
                         "fill": "#e9edf2", "stroke": INK, "stroke-width": "0.7"})
    sub(sample, "text", {"x": fix(gx + 1.5 * cw), "y": fix(gy + 1.5 * ch + 2.2), "font-size": "6", "font-weight": "bold",
                         "text-anchor": "middle", "fill": INK}, "E2")


def _symbol_convention(kind):
    spec = symbol_defs.SYMBOLS[kind]
    section = "instrument" if spec.category == "instrument" else "equipment"
    return Convention(spec.key, section, spec.title, spec.description, _paint_symbol(kind))


def _family_convention(shape):
    spec = symbol_defs.family_sample(shape)
    return Convention(spec.key, "equipment", spec.title, spec.description, _paint_symbol(None, spec))


_ALL = [
    Convention(
        "convention/zone-grid",
        "sheet",
        "ZONE REFERENCE",
        "References read SHEET / ROW+COLUMN, for example P&ID-001 / E2. Rows run A (bottom) to I (top) and "
        "columns 1 to 10 (left to right). A grid line belongs to the zone right of or below it.",
        _paint_zone,
    ),
    *(_symbol_convention(kind) for kind in symbol_defs.SYMBOLS),
    *(_family_convention(shape) for shape in symbol_defs.FAMILIES),
    Convention(
        symbol_defs.FALLBACK.key, "equipment", symbol_defs.FALLBACK.title, symbol_defs.FALLBACK.description,
        _paint_symbol(""),
    ),
    Convention("line/process", "line", "PROCESS LINE", "Modeled process connection between two ports.",
               _paint_line("line/process")),
    Convention("line/process/needs-reroute", "line", "PROCESS LINE, P&ID PATH NEEDS REROUTE",
               "The stored path is kept and drawn, but no longer meets its ports; it needs a new path.",
               _paint_line("line/process/needs-reroute")),
    Convention("line/process/unresolved", "line", "PROCESS LINE, P&ID PATH UNRESOLVED",
               "The stored path is kept and drawn, but has not been resolved.",
               _paint_line("line/process/unresolved")),
    Convention("line/process/topology-hint", "line", "CONNECTION WITHOUT A DRAWABLE PATH",
               "Joins the two ports directly to show topology only; no P&ID path is stored.",
               _paint_line("line/process/topology-hint")),
    Convention("line/process/detached-end", "line", "DETACHED PATH END",
               "Joins a retained path end to the port it no longer reaches.",
               _paint_line("line/process/detached-end")),
    Convention("line/observation", "line", "CONCEPTUAL OBSERVATION RELATION",
               "Links an indicator to the item it observes. It is not piping, a signal line, an instrument hookup "
               "or a control loop.", _paint_observation),
    Convention("marker/flow-arrow", "marker", "FLOW DIRECTION", "Direction of modeled flow from outlet port to "
               "inlet port.", _paint_arrow),
    Convention("marker/crossing-gap", "marker", "CROSSING WITHOUT CONNECTION",
               "The broken line passes under the continuous line; they are not connected.", _paint_crossing),
    Convention("marker/physical-placement-pending", "marker", "3D PLACEMENT PENDING",
               "The item has no resolved physical placement; its 3D representation is not complete.",
               _paint_badge("marker/physical-placement-pending")),
    Convention("marker/physical-route-pending", "marker", "3D ROUTE PENDING",
               "The line has no current physical route; its 3D representation is not complete.",
               _paint_badge("marker/physical-route-pending")),
    Convention("marker/unmatched-anchor", "marker", "CONNECTION POINT NOT ON SYMBOL",
               "A declared connection point lies inside the generic outline even at its smallest drawn size, so the "
               "line cannot meet the outline there. The definition's symbol anchors need review.", _paint_unmatched),
    Convention("convention/line-designation", "annotation", "LINE DESIGNATION",
               "Line tag (bold) followed by the nominal size, written beside the line.", _paint_designation),
    Convention("convention/label-leader", "annotation", "LABEL LEADER",
               "Thin line joining a tag or line designation to its item where the label cannot sit beside it.",
               _paint_leader),
    Convention("convention/equipment-data", "annotation", "EQUIPMENT DATA",
               "Tag, item type and modeled data in a band clear of the drawing, aligned with the item where space "
               "allows. NOT SPECIFIED marks data absent from the model.", _paint_equipment_data),
    Convention("convention/assumed-value", "annotation", "ASSUMED VALUE (*)",
               "Prototype assumption; see the assumption register in the process description.",
               _paint_text("convention/assumed-value", "0.8 m *", 7.5, False)),
    Convention("abbr/DN", "abbreviation", "DN", "Nominal diameter designation in millimetres; not an outside "
               "diameter.", _paint_text("abbr/DN", "DN", 8.5)),
    Convention("abbr/dia", "abbreviation", "Ø", "Diameter.", _paint_text("abbr/dia", "Ø", 9.0)),
]

CONVENTIONS = {item.key: item for item in _ALL}
SECTION_ORDER = {name: index for index, (name, _) in enumerate(SECTIONS)}
KEY_ORDER = {item.key: index for index, item in enumerate(_ALL)}


def badge_line(key):
    return line(BADGE_TEXT[key], BADGE_SIZE, bold=True, fill=PENDING, badge=True, keys=(key,))


def sort_keys(keys):
    return sorted(keys, key=lambda k: (SECTION_ORDER[CONVENTIONS[k].section], KEY_ORDER[k]) if k in CONVENTIONS
                  else (len(SECTIONS), math.inf, k))
