"""Small SVG, text-metric, and geometry helpers shared by the drafting sheets.

Text widths are estimated from Helvetica advance widths (the sheets ask for
Helvetica/Arial/Liberation Sans), rounded up slightly so that collision checks
err toward more space. They are layout estimates, not font measurements.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
FONT_FAMILY = "Helvetica, Arial, 'Liberation Sans', sans-serif"

INK = "#141414"
MUTED = "#5b5f66"
PAPER = "#ffffff"
RULE = "#8a8f98"
RELATION = "#2f5d8a"
PENDING = "#c62828"

_XML_INVALID = re.compile("[^\t\n\r\x20-\U0000d7ff\U0000e000-\U0000fffd\U00010000-\U0010ffff]")
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"

# Helvetica advance widths per 1000 em (regular), for printable ASCII.
_REGULAR = dict(
    zip(
        " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
        (
            278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
            556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
            1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
            667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
            333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
            556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
        ),
    )
)
# Bold widths where they differ from regular.
_BOLD = dict(_REGULAR)
_BOLD.update(
    zip(
        "!\"'()*:;?ABJKLabcdefghijklmnopqrstuvwxyz",
        (333, 474, 238, 333, 333, 389, 333, 333, 611, 722, 722, 556, 722, 611,
         556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
         611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500),
    )
)
_EXTRA = {"Ø": 778, "×": 584, "→": 1000, "—": 1000, "–": 556, "·": 278, "…": 1000}
_SAFETY = 1.04


def xml_text(text: str) -> str:
    """Replace characters that XML 1.0 cannot carry, even as references."""
    return _XML_INVALID.sub(_REPLACEMENT, text)


def has_invalid_text(value) -> bool:
    if isinstance(value, str):
        return _XML_INVALID.search(value) is not None
    if isinstance(value, dict):
        return any(has_invalid_text(k) or has_invalid_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_invalid_text(v) for v in value)
    return False


def text_width(text: str, size: float, bold: bool = False) -> float:
    table = _BOLD if bold else _REGULAR
    total = 0
    for char in text:
        width = table.get(char) or _EXTRA.get(char)
        if width is None:
            width = 1000 if ord(char) > 0x2E80 else 640
        total += width
    return total / 1000.0 * size * _SAFETY


def wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    """Greedy word wrap by estimated width; overlong words are split."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if text_width(candidate, size, bold) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while text_width(word, size, bold) > width and len(word) > 1:
            cut = len(word)
            while cut > 1 and text_width(word[:cut], size, bold) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def num(value) -> str:
    """Shortest exact text of a number; stored coordinates keep every digit."""
    value = float(value)
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def fix(value) -> str:
    """Derived geometry rounded to 0.01 drawing units."""
    return num(round(float(value), 2))


def points_attr(points) -> str:
    return " ".join(f"{num(x)},{num(y)}" for x, y in points)


def sub(parent, tag, attrs=(), text=None):
    items = attrs.items() if isinstance(attrs, dict) else attrs
    element = ET.SubElement(parent, tag, {name: xml_text(str(value)) for name, value in items})
    if text is not None:
        element.text = xml_text(text)
    return element


def serialize(root) -> str:
    """Indented SVG text; mixed-weight text keeps no indentation whitespace between its runs."""
    ET.indent(root, space=" ")
    for element in root.iter("text"):
        if len(element):
            if element.text is not None and not element.text.strip():
                element.text = None
            for child in element:
                if child.tail is not None and not child.tail.strip():
                    child.tail = None
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


@dataclass(frozen=True)
class Run:
    """A text run; several runs make one line (for mixed weights)."""

    text: str
    bold: bool = False
    fill: str = INK


@dataclass(frozen=True)
class Line:
    """One line of a label block."""

    runs: tuple
    size: float
    badge: bool = False  # drawn inside an outlined badge
    keys: tuple = ()  # legend keys carried by this line
    attrs: tuple = ()
    underline: bool = False

    @property
    def width(self) -> float:
        width = sum(text_width(run.text, self.size, run.bold) for run in self.runs)
        return width + (4.0 if self.badge else 0.0)

    @property
    def height(self) -> float:
        return self.size * (1.5 if self.badge else 1.28)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


def line(text, size, bold=False, fill=INK, **kwargs) -> Line:
    return Line((Run(text, bold, fill),), size, **kwargs)


def block_size(lines) -> tuple[float, float]:
    if not lines:
        return 0.0, 0.0
    return max(item.width for item in lines), sum(item.height for item in lines)


def draw_text_line(parent, item: Line, x: float, top: float, anchor: str):
    """Draw one Line whose box starts at ``top``; ``x`` is interpreted per ``anchor``."""
    baseline = top + item.size * (1.02 if item.badge else 0.86)
    group = parent
    if item.badge:
        left = {"start": x, "middle": x - item.width / 2, "end": x - item.width}[anchor]
        fill = item.runs[0].fill
        group = sub(parent, "g", (("data-role", "badge"),) + _keys(item.keys))
        sub(
            group,
            "rect",
            {
                "x": fix(left),
                "y": fix(top + item.size * 0.12),
                "width": fix(item.width),
                "height": fix(item.size * 1.26),
                "rx": "1.5",
                "fill": PAPER,
                "stroke": fill,
                "stroke-width": "0.6",
            },
        )
        x = {"start": x + 2, "middle": x, "end": x - 2}[anchor]
        keys = ()
    else:
        keys = item.keys
    if len(item.runs) == 1:
        attrs = {"x": fix(x), "y": fix(baseline), "font-size": fix(item.size)}
        if anchor != "start":
            attrs["text-anchor"] = anchor
        attrs.update(dict(item.attrs))
        attrs.update(dict(_keys(keys)))
        run = item.runs[0]
        attrs["fill"] = run.fill
        if run.bold:
            attrs["font-weight"] = "bold"
        element = sub(group, "text", attrs, run.text)
    else:
        element = _draw_runs(group, item.runs, item.size, x, baseline, anchor,
                             dict(item.attrs) | dict(_keys(keys)), item.width)
    if item.underline:
        left = {"start": x, "middle": x - item.width / 2, "end": x - item.width}[anchor]
        sub(
            group,
            "line",
            {
                "x1": fix(left),
                "y1": fix(baseline + item.size * 0.18),
                "x2": fix(left + item.width),
                "y2": fix(baseline + item.size * 0.18),
                "stroke": item.runs[0].fill,
                "stroke-width": "0.6",
            },
        )
    return element


def _draw_runs(parent, runs, size, x, baseline, anchor, attrs, width):
    """Mixed-weight text laid out explicitly so every SVG renderer shows the same spacing.

    Renderers differ in how they apply ``text-anchor`` and collapse whitespace across
    ``<tspan>`` boundaries (CairoSVG anchors each run on its own, which cancels a leading
    space). The line is therefore start-anchored at the left edge computed from the
    measured width used for layout, and a run's leading spaces become an explicit ``dx``
    of the same measured width instead of whitespace characters.
    """
    left = {"start": x, "middle": x - width / 2, "end": x - width}[anchor]
    element = sub(parent, "text", {"x": fix(left), "y": fix(baseline), "font-size": fix(size), **attrs})
    for run in runs:
        text = run.text.lstrip(" ")
        span = {"fill": run.fill}
        if run.bold:
            span["font-weight"] = "bold"
        if len(text) < len(run.text):
            span["dx"] = fix(text_width(run.text[: len(run.text) - len(text)], size, run.bold))
        sub(element, "tspan", span, text)
    return element


def draw_block(parent, lines, rect, anchor):
    x0, y0, x1, _ = rect
    x = {"start": x0, "middle": (x0 + x1) / 2, "end": x1}[anchor]
    for item in lines:
        draw_text_line(parent, item, x, y0, anchor)
        y0 += item.height


def _keys(keys):
    return (("data-legend-keys", " ".join(keys)),) if keys else ()


def keys_attr(keys):
    return _keys(tuple(keys))


# --- Geometry -------------------------------------------------------------------------


def cos_sin(degrees):
    quarters, remainder = divmod(float(degrees), 90.0)
    if remainder == 0:  # exact quarter turns, as skid.templates uses for anchors
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(quarters) % 4]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def placed_box(local, angle, x, y):
    c, s = cos_sin(angle)
    xs, ys = [], []
    for lx in (local[0], local[2]):
        for ly in (local[1], local[3]):
            xs.append(x + lx * c - ly * s)
            ys.append(y + lx * s + ly * c)
    return (min(xs), min(ys), max(xs), max(ys))


def rotate_point(point, angle):
    c, s = cos_sin(angle)
    return point[0] * c - point[1] * s, point[0] * s + point[1] * c


def inflate(rect, by):
    return (rect[0] - by, rect[1] - by, rect[2] + by, rect[3] + by)


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def union(rects):
    rects = list(rects)
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def length(segment):
    return math.dist(segment[0], segment[1])


def segment_hits_rect(start, end, rect):
    """Liang-Barsky test of a segment against an axis-aligned rectangle."""
    x, y = start
    dx, dy = end[0] - x, end[1] - y
    low, high = 0.0, 1.0
    for p, q in ((-dx, x - rect[0]), (dx, rect[2] - x), (-dy, y - rect[1]), (dy, rect[3] - y)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return False
    return True


def segment_crossing(a, b, tolerance=1e-9):
    """Proper interior crossing point of two segments, or None (touching ends do not count)."""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) <= tolerance:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denominator
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denominator
    margin = 1e-6
    if margin < t < 1 - margin and margin < u < 1 - margin:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def collinear_overlap(a, b, tolerance=0.75):
    """Length of the shared run of two axis-parallel segments lying on one line."""
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    if abs(ay1 - ay2) <= 1e-9 and abs(by1 - by2) <= 1e-9 and abs(ay1 - by1) <= tolerance:
        low, high = max(min(ax1, ax2), min(bx1, bx2)), min(max(ax1, ax2), max(bx1, bx2))
        return max(0.0, high - low)
    if abs(ax1 - ax2) <= 1e-9 and abs(bx1 - bx2) <= 1e-9 and abs(ax1 - bx1) <= tolerance:
        low, high = max(min(ay1, ay2), min(by1, by2)), min(max(ay1, ay2), max(by1, by2))
        return max(0.0, high - low)
    return 0.0


def parallel_proximity(a, b, distance):
    """Shared extent of two axis-parallel segments closer than ``distance`` but not collinear."""
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    if abs(ay1 - ay2) <= 1e-9 and abs(by1 - by2) <= 1e-9:
        gap = abs(ay1 - by1)
        low, high = max(min(ax1, ax2), min(bx1, bx2)), min(max(ax1, ax2), max(bx1, bx2))
    elif abs(ax1 - ax2) <= 1e-9 and abs(bx1 - bx2) <= 1e-9:
        gap = abs(ax1 - bx1)
        low, high = max(min(ay1, ay2), min(by1, by2)), min(max(ay1, ay2), max(by1, by2))
    else:
        return 0.0
    if 0.75 < gap < distance:
        return max(0.0, high - low)
    return 0.0


def polyline_midpoint(points):
    """Point halfway along a polyline by length."""
    segments = [(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(length(s) for s in segments)
    if total <= 0:
        return points[0]
    remaining = total / 2
    for start, end in segments:
        run = length((start, end))
        if run >= remaining and run > 0:
            t = remaining / run
            return (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
        remaining -= run
    return points[-1]
