"""Fixed paper contract for the drafting sheets (WORKFLOW_CONTRACTS.md, version 0.2).

The landscape sheet is 1200 x 840 drawing units. The inner border runs from
x = 30 to 1170 and y = 30 to 810, with +x right and +y down. It is divided
into 10 columns, numbered 1-10 left to right on the top and bottom margins,
and 9 rows, lettered I-A top to bottom on both side margins (A at the bottom).

Stored diagram coordinates map to paper coordinates by ``x' = x + 100`` and
``y' = y + 100`` (IEEE double addition). There is no fit-to-content transform,
so edits never rescale or shift the sheet.

Grid lines are binary64 numbers, and those exact numbers are the canonical
zone boundaries (WORKFLOW_CONTRACTS.md, sheet boundary precision). Each line
is the correctly rounded double of its exact position, ``30 + 114 k`` for
columns and ``30 + 780 k / 9`` for rows; the same values are used for lookup,
drawn at full precision in the zone margins, and exported in the inventory.
Paper coordinates are binary64 too (other numbers are first rounded with
``float``). A point exactly on an internal line belongs to the cell to its
right (columns) or below it (rows); the next representable value on either
side belongs to that side's cell, with no tolerance. The maximum border
(x = 1170 or y = 810) belongs to the last column or row. Points outside the
inner border, and non-finite values, have no zone; callers report them as
diagnostics instead of clamping them.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from fractions import Fraction

SHEET_ID = "P&ID-001"
LEGEND_SHEET_ID = "LEG-001"
WIDTH = 1200
HEIGHT = 840
BORDER = (30, 30, 1170, 810)
TRIM = (10, 10, 1190, 830)
COLUMNS = 10
ROWS = 9
ROW_LETTERS = "IHGFEDCBA"  # top to bottom; A is the bottom row
OFFSET = (100.0, 100.0)
COLUMN_WIDTH = Fraction(BORDER[2] - BORDER[0], COLUMNS)
ROW_HEIGHT = Fraction(BORDER[3] - BORDER[1], ROWS)
TITLE_BLOCK = (830.0, 662.0, 1170.0, 810.0)
# Canonical binary64 boundaries, inner border included: 11 column edges left to right, 10 row edges top to bottom.
COLUMN_BOUNDARIES = tuple(float(BORDER[0] + COLUMN_WIDTH * k) for k in range(COLUMNS + 1))
ROW_BOUNDARIES = tuple(float(BORDER[1] + ROW_HEIGHT * k) for k in range(ROWS + 1))
BOUNDARY_POLICY = (
    "Grid lines are the binary64 values listed in column_boundaries and row_boundaries_top_to_bottom (the "
    "correctly rounded doubles of 30 + 114k and 30 + 780k/9); these exact values are drawn in the margins and "
    "used for zone lookup on binary64 paper coordinates, with no tolerance. A point on an internal line belongs "
    "to the cell to its right (columns) or below it (rows); the next representable value on either side belongs "
    "to that side. x = 1170 belongs to column 10 and y = 810 to row A. Points outside the inner border "
    "(x 30..1170, y 30..810) have no zone and are reported as diagnostics."
)


def to_paper(x, y) -> tuple[float, float]:
    """Map stored diagram coordinates to fixed paper coordinates."""
    return float(x) + OFFSET[0], float(y) + OFFSET[1]


def _cell(value, boundaries) -> int | None:
    """Zero-based cell of a binary64 coordinate between canonical boundaries; a line opens the next cell."""
    value = float(value)
    if not math.isfinite(value) or value < boundaries[0] or value > boundaries[-1]:
        return None
    if value == boundaries[-1]:
        return len(boundaries) - 2
    return bisect_right(boundaries, value) - 1


def column_of(px) -> int | None:
    index = _cell(px, COLUMN_BOUNDARIES)
    return None if index is None else index + 1


def row_of(py) -> str | None:
    index = _cell(py, ROW_BOUNDARIES)
    return None if index is None else ROW_LETTERS[index]


def zone_of(px, py) -> str | None:
    """Zone such as ``E6`` for a paper point, or None outside the inner border."""
    column, row = column_of(px), row_of(py)
    if column is None or row is None:
        return None
    return f"{row}{column}"


def zone_ref(zone, sheet_id=SHEET_ID) -> str:
    return f"{sheet_id} / {zone}" if zone else f"{sheet_id} / outside sheet"


def column_lines() -> list[float]:
    """Canonical paper x of the internal column boundaries (left to right)."""
    return list(COLUMN_BOUNDARIES[1:-1])


def row_lines() -> list[float]:
    """Canonical paper y of the internal row boundaries (top to bottom)."""
    return list(ROW_BOUNDARIES[1:-1])


def column_centre(column: int) -> float:
    return float(BORDER[0] + COLUMN_WIDTH * (Fraction(column) - Fraction(1, 2)))


def row_centre(letter: str) -> float:
    index = ROW_LETTERS.index(letter)
    return float(BORDER[1] + ROW_HEIGHT * (Fraction(index) + Fraction(1, 2)))


def inside(px, py) -> bool:
    return BORDER[0] <= px <= BORDER[2] and BORDER[1] <= py <= BORDER[3]


def paper_record() -> dict:
    """Machine-readable sheet definition recorded in the inventory and index."""
    return {
        "sheet_id": SHEET_ID,
        "width": WIDTH,
        "height": HEIGHT,
        "units": "drawing units",
        "inner_border": {"x0": BORDER[0], "y0": BORDER[1], "x1": BORDER[2], "y1": BORDER[3]},
        "columns": COLUMNS,
        "rows": ROWS,
        "column_labels": "1-10 left to right, top and bottom margins",
        "row_labels_top_to_bottom": ROW_LETTERS,
        "column_boundaries": list(COLUMN_BOUNDARIES),
        "row_boundaries_top_to_bottom": list(ROW_BOUNDARIES),
        "transform": "x' = x + 100, y' = y + 100 (stored diagram units to paper units, no fit or scaling)",
        "boundary_policy": BOUNDARY_POLICY,
    }
