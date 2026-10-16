"""Sheet frame (trim line, inner border, zone margins) and the title/revision block."""

from __future__ import annotations

from dataclasses import dataclass

from . import paper
from .svgkit import INK, MUTED, PAPER, PENDING, fix, keys_attr, num, sub, text_width, wrap

ZONE_LABEL_SIZE = 8.5


def draw_frame(parent, with_keys: bool):
    """Paper, trim line, inner border, and 1-10 / A-I zone margins (A bottom, I top)."""
    layer = sub(parent, "g", (("data-layer", "sheet-frame"),))
    sub(layer, "rect", {"x": "0", "y": "0", "width": str(paper.WIDTH), "height": str(paper.HEIGHT), "fill": PAPER})
    tx0, ty0, tx1, ty1 = paper.TRIM
    bx0, by0, bx1, by1 = paper.BORDER
    sub(layer, "rect", {"x": str(tx0), "y": str(ty0), "width": str(tx1 - tx0), "height": str(ty1 - ty0),
                        "fill": "none", "stroke": INK, "stroke-width": "0.6"})
    sub(layer, "rect", {"x": str(bx0), "y": str(by0), "width": str(bx1 - bx0), "height": str(by1 - by0),
                        "fill": "none", "stroke": INK, "stroke-width": "1.4", "data-role": "inner-border"})
    zones = sub(layer, "g", (("data-role", "zone-frame"),) + (keys_attr(("convention/zone-grid",)) if with_keys else ()))
    # Grid ticks use the canonical boundary values at full precision, never rounded drawing coordinates.
    for index, x in enumerate(paper.column_lines(), 1):
        for y_a, y_b in ((ty0, by0), (by1, ty1)):
            sub(zones, "line", {"x1": num(x), "y1": str(y_a), "x2": num(x), "y2": str(y_b), "stroke": INK,
                                "stroke-width": "0.6", "data-grid-line": "column", "data-boundary-index": str(index)})
    for index, y in enumerate(paper.row_lines(), 1):
        for x_a, x_b in ((tx0, bx0), (bx1, tx1)):
            sub(zones, "line", {"x1": str(x_a), "y1": num(y), "x2": str(x_b), "y2": num(y), "stroke": INK,
                                "stroke-width": "0.6", "data-grid-line": "row", "data-boundary-index": str(index)})
    for column in range(1, paper.COLUMNS + 1):
        cx = paper.column_centre(column)
        for edge, cy in (("top", (ty0 + by0) / 2), ("bottom", (by1 + ty1) / 2)):
            sub(zones, "text", {"x": fix(cx), "y": fix(cy + ZONE_LABEL_SIZE * 0.36), "font-size": fix(ZONE_LABEL_SIZE),
                                "text-anchor": "middle", "fill": INK, "data-zone-column": str(column),
                                "data-margin": edge}, str(column))
    for letter in paper.ROW_LETTERS:
        cy = paper.row_centre(letter)
        for edge, cx in (("left", (tx0 + bx0) / 2), ("right", (bx1 + tx1) / 2)):
            sub(zones, "text", {"x": fix(cx), "y": fix(cy + ZONE_LABEL_SIZE * 0.36), "font-size": fix(ZONE_LABEL_SIZE),
                                "text-anchor": "middle", "fill": INK, "data-zone-row": letter, "data-margin": edge},
                letter)
    return layer


@dataclass
class TitleFields:
    title: str
    subtitle: str
    drawing_no: str
    project: str
    revision: str
    revision_description: str
    status: str
    status_pending: bool
    status_progress: bool
    drawn: str
    sheet: str = "1 OF 1"
    scale: str = "NONE"
    checked: str = "NOT CHECKED"
    approved: str = "NOT APPROVED"
    banner: str = "PROTOTYPE — NOT FOR DESIGN, PROCUREMENT OR CONSTRUCTION"


def _fit(text, size, width, minimum=5.0, bold=True):
    while size > minimum and text_width(text, size, bold) > width:
        size -= 0.25
    return size


def _cell(parent, x0, y0, x1, y1, label, value, size=7.5, fill=INK, role=None, bold=True):
    sub(parent, "rect", {"x": fix(x0), "y": fix(y0), "width": fix(x1 - x0), "height": fix(y1 - y0), "fill": "none",
                         "stroke": INK, "stroke-width": "0.7"})
    sub(parent, "text", {"x": fix(x0 + 3), "y": fix(y0 + 6.2), "font-size": "4.8", "fill": MUTED,
                         "letter-spacing": "0.3"}, label)
    width = x1 - x0 - 6
    size = _fit(value, size, width, bold=bold)
    lines = [value] if text_width(value, size, bold) <= width else wrap(value, size, width, bold)
    baseline = y1 - 4.5 - (len(lines) - 1) * size * 1.15
    for text in lines:
        attrs = {"x": fix(x0 + 3), "y": fix(baseline), "font-size": fix(size), "fill": fill}
        if bold:
            attrs["font-weight"] = "bold"
        if role:
            attrs["data-role"] = role
        sub(parent, "text", attrs, text)
        baseline += size * 1.15


def draw_title_block(parent, fields: TitleFields):
    x0, y0, x1, y1 = paper.TITLE_BLOCK
    block = sub(parent, "g", (("data-layer", "title-block"),))
    sub(block, "rect", {"x": fix(x0), "y": fix(y0), "width": fix(x1 - x0), "height": fix(y1 - y0), "fill": PAPER,
                        "stroke": INK, "stroke-width": "1.2"})
    status_fill = PENDING if (fields.status_pending or fields.status_progress) else INK
    row = y0
    # Revision row.
    _cell(block, x0, row, x0 + 34, row + 26, "REV", fields.revision, 8.5)
    _cell(block, x0 + 34, row, x1 - 104, row + 26, "REVISION DESCRIPTION", fields.revision_description, 6.6)
    _cell(block, x1 - 104, row, x1, row + 26, "GENERATION STATUS", fields.status, 7.0, status_fill,
          role="progress" if fields.status_progress else "status")
    row += 26
    # Prototype banner.
    sub(block, "rect", {"x": fix(x0), "y": fix(row), "width": fix(x1 - x0), "height": "14", "fill": INK})
    size = _fit(fields.banner, 6.6, x1 - x0 - 8)
    sub(block, "text", {"x": fix((x0 + x1) / 2), "y": fix(row + 9.6), "font-size": fix(size), "font-weight": "bold",
                        "text-anchor": "middle", "fill": PAPER, "letter-spacing": "0.4"}, fields.banner)
    row += 14
    # Title.
    sub(block, "rect", {"x": fix(x0), "y": fix(row), "width": fix(x1 - x0), "height": "40", "fill": "none",
                        "stroke": INK, "stroke-width": "0.7"})
    sub(block, "text", {"x": fix(x0 + 3), "y": fix(row + 6.2), "font-size": "4.8", "fill": MUTED,
                        "letter-spacing": "0.3"}, "TITLE")
    size = _fit(fields.title, 11.0, x1 - x0 - 10)
    sub(block, "text", {"x": fix((x0 + x1) / 2), "y": fix(row + 20), "font-size": fix(size), "font-weight": "bold",
                        "text-anchor": "middle", "fill": INK, "data-role": "drawing-title"}, fields.title)
    size = _fit(fields.subtitle, 8.5, x1 - x0 - 10)
    sub(block, "text", {"x": fix((x0 + x1) / 2), "y": fix(row + 33), "font-size": fix(size), "font-weight": "bold",
                        "text-anchor": "middle", "fill": INK, "data-role": "drawing-subtitle"}, fields.subtitle)
    row += 40
    _cell(block, x0, row, x1, row + 22, "PROJECT", fields.project, 8.0)
    row += 22
    _cell(block, x0, row, x0 + 128, row + 24, "DRAWING NO.", fields.drawing_no, 10.0)
    _cell(block, x0 + 128, row, x0 + 196, row + 24, "SHEET", fields.sheet, 8.0)
    _cell(block, x0 + 196, row, x0 + 250, row + 24, "REV", fields.revision, 8.0)
    _cell(block, x0 + 250, row, x1, row + 24, "SCALE", fields.scale, 8.0)
    row += 24
    _cell(block, x0, row, x0 + 160, y1, "DRAWN", fields.drawn, 6.4)
    _cell(block, x0 + 160, row, x0 + 250, y1, "CHECKED", fields.checked, 6.4)
    _cell(block, x0 + 250, row, x1, y1, "APPROVED", fields.approved, 6.4)
    return block
