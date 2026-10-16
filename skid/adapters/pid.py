"""P&ID adapter: one read-only project snapshot rendered as SVG plus a manifest.

``render_pid(project, selected_ids=())`` returns ``(svg_text, manifest)`` and has
no filesystem side effects. It never mutates its arguments and never moves
anything: symbols are drawn at their stored ``x``, ``y`` and ``rotation_deg``
and edges along their stored points, with port anchors taken from
``skid.templates.diagram_port_position``. Labels are the only derived layout;
they are placed deterministically around the fixed symbols and lines.

The manifest has exactly the contracted fields ``{adapter, adapter_version,
project_id, source_revision, input_digest, status, diagnostics,
representations}``:

- ``input_digest`` is ``skid.core.validation.canonical_digest(project)``.
- ``status`` is ``failed`` when the project has structural errors (no drawing
  is produced), ``pending`` when any warning remains (unresolved P&ID or
  physical presentation, an unknown selected ID, or text XML cannot carry),
  and ``complete`` otherwise. ``diagnostics`` holds the validator's findings
  and the adapter's own, in a stable order.
- ``representations`` maps every entity, connection, and port ID to the sorted
  occurrence IDs drawn for it: symbol IDs, edge IDs, ``<symbol>#<port>`` port
  anchors, and ``<indicator symbol>#observes`` indication relations (listed
  under the indicator). An empty list means nothing was drawn. The map is
  empty when generation fails.

SVG occurrence groups carry ``data-semantic-id``, ``data-semantic-kind``,
``data-occurrence-id``, ``data-occurrence-kind``, and ``data-selected``
independently of their labels. ``<metadata>`` repeats the source identity,
status, diagnostics, and representations in ``METADATA_NAMESPACE``.

Styles: current process lines are solid black; a ``needs_reroute`` edge keeps
its stored points in red dash-dot; an ``unresolved`` edge keeps them in red
dots, or shows only a dotted topology hint between its anchors when no path
exists; detached ends of retained paths are dotted to the anchors. Conceptual
indication relations are dashed blue. Unresolved physical placement and routes
are written in amber beside the affected occurrence.
"""

from __future__ import annotations

import copy
import math
import re
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..core.validation import canonical_digest, validate_project
from ..templates import diagram_port_position, entity_template
from .pid_symbols import FALLBACK_SYMBOL, SYMBOLS

__all__ = ["ADAPTER", "ADAPTER_VERSION", "METADATA_NAMESPACE", "render_pid"]

ADAPTER = "pid"
ADAPTER_VERSION = "0.1.0"
METADATA_NAMESPACE = "urn:x-skid:pid-metadata:1"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

_INK = "#111111"
_MUTED = "#4a4a4a"
_PAPER = "#ffffff"
_RELATION = "#1f5fa8"
_STALE = "#c62828"
_PENDING = "#9a5b00"
_HALO_FILL = "#ffd54f"
_HALO_EDGE = "#f9a825"
_STATUS_COLOURS = {"complete": "#2e7d32", "pending": _PENDING, "failed": _STALE}

_PATH_STYLES = {
    "current": (("stroke", _INK), ("stroke-width", "1.6")),
    "needs_reroute": (("stroke", _STALE), ("stroke-width", "1.6"), ("stroke-dasharray", "9 3 2 3")),
    "unresolved": (("stroke", _STALE), ("stroke-width", "1.4"), ("stroke-dasharray", "2 3")),
}
_HINT_STYLE = (("stroke", _STALE), ("stroke-width", "1"), ("stroke-dasharray", "1 3"))
_RELATION_STYLE = (("stroke", _RELATION), ("stroke-width", "1"), ("stroke-dasharray", "5 4"))

_FONT = "Menlo, Consolas, 'DejaVu Sans Mono', monospace"
_CHAR = 0.6  # advance of one monospace glyph per unit of font size
_LEADING = 1.25
_TITLE_SIZE = 13.0
_TAG_SIZE = 9.0
_LINE_TAG_SIZE = 8.5
_NOTE_SIZE = 7.5
_INNER_MAX_SIZE = 10.0
_INNER_MIN_SIZE = 6.0
_MIN_WIDTH = 820
_MARGIN = 28.0
_PAD = 14.0
_TOLERANCE = 1e-6
_MAX_LISTED_DIAGNOSTICS = 40

_XML_INVALID = re.compile("[^\t\n\r\x20-\U0000d7ff\U0000e000-\U0000fffd\U00010000-\U0010ffff]")
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def render_pid(project: dict, selected_ids: tuple[str, ...] = ()) -> tuple[str, dict]:
    """Render ``project`` as P&ID SVG text and its manifest; highlight ``selected_ids``."""
    if isinstance(selected_ids, (str, bytes)):
        raise TypeError("selected_ids must be a collection of IDs, not a single string")
    selection = tuple(selected_ids)
    diagnostics = []
    snapshot, digest = None, None
    try:
        snapshot = copy.deepcopy(project)
    except Exception as exc:  # an uncopyable snapshot cannot be rendered faithfully
        diagnostics.append(_diagnostic("snapshot_unreadable", "error", [], f"project could not be copied ({_describe(exc)})"))
    if snapshot is not None:
        try:
            digest = canonical_digest(snapshot)
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                _diagnostic("digest_unavailable", "error", [], f"canonical input digest failed ({_describe(exc)})")
            )
        diagnostics.extend(validate_project(snapshot))
    project_id, revision = _identity(snapshot)

    drawing = None
    if not _has_error(diagnostics):
        try:
            drawing = _Drawing(snapshot, selection)
        except Exception as exc:  # an adapter fault must never pass for a drawing
            diagnostics.append(_diagnostic("pid_render_failed", "error", [], f"P&ID rendering failed ({_describe(exc)})"))
        else:
            diagnostics.extend(drawing.diagnostics)
    manifest = _manifest(project_id, revision, digest, diagnostics, drawing)
    try:
        svg = _compose(manifest, drawing)
    except Exception as exc:  # fall back to a failure sheet rather than a partial drawing
        diagnostics.append(_diagnostic("pid_render_failed", "error", [], f"SVG composition failed ({_describe(exc)})"))
        manifest = _manifest(project_id, revision, digest, diagnostics, None)
        svg = _compose(manifest, None)
    return svg, manifest


def _manifest(project_id, revision, digest, diagnostics, drawing):
    ordered = sorted(
        diagnostics,
        key=lambda d: (d["severity"] != "error", d["code"], d["message"], tuple(d["entity_ids"])),
    )
    if _has_error(ordered):
        status, representations = "failed", {}
    else:
        status = "pending" if ordered else "complete"
        representations = copy.deepcopy(drawing.representations)
    return {
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "project_id": project_id,
        "source_revision": revision,
        "input_digest": digest,
        "status": status,
        "diagnostics": copy.deepcopy(ordered),
        "representations": representations,
    }


# --- Small helpers ------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    text: str
    size: float
    fill: str = _INK
    bold: bool = False
    attrs: tuple = ()

    @property
    def width(self) -> float:
        return _CHAR * self.size * len(self.text)

    @property
    def height(self) -> float:
        return self.size * _LEADING


def _diagnostic(code, severity, ids, message):
    return {"code": code, "severity": severity, "entity_ids": [i for i in ids if isinstance(i, str)], "message": message}


def _has_error(diagnostics):
    return any(d.get("severity") == "error" for d in diagnostics)


def _describe(exc):
    text = str(exc)
    text = text if len(text) <= 160 else text[:157] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _identity(project):
    if not isinstance(project, dict):
        return None, None
    project_id, revision = project.get("id"), project.get("revision")
    if not (isinstance(project_id, str) and project_id.strip()):
        project_id = None
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        revision = None
    return project_id, revision


def _xml(text: str) -> str:
    """Replace characters that XML 1.0 cannot carry, even as references."""
    return _XML_INVALID.sub(_REPLACEMENT, text)


def _has_invalid_text(value) -> bool:
    if isinstance(value, str):
        return _XML_INVALID.search(value) is not None
    if isinstance(value, dict):
        return any(_has_invalid_text(k) or _has_invalid_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_invalid_text(v) for v in value)
    return False


def _num(value) -> str:
    """Shortest exact text of a number; stored coordinates keep every digit."""
    value = float(value)
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _fix(value) -> str:
    """Derived geometry, rounded to 0.01 drawing units."""
    return _num(round(value, 2))


def _points(points) -> str:
    return " ".join(f"{_num(x)},{_num(y)}" for x, y in points)


def _sub(parent, tag, attrs=(), text=None):
    element = ET.SubElement(parent, tag, {name: _xml(value) for name, value in dict(attrs).items()})
    if text is not None:
        element.text = _xml(text)
    return element


def _cos_sin(degrees):
    quarters, remainder = divmod(degrees, 90.0)
    if remainder == 0:  # exact quarter turns, as skid.templates uses for anchors
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(quarters) % 4]
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def _placed_box(local, angle, x, y):
    c, s = _cos_sin(angle)
    xs, ys = [], []
    for lx in (local[0], local[2]):
        for ly in (local[1], local[3]):
            xs.append(x + lx * c - ly * s)
            ys.append(y + lx * s + ly * c)
    return (min(xs), min(ys), max(xs), max(ys))


def _inflate(rect, by):
    return (rect[0] - by, rect[1] - by, rect[2] + by, rect[3] + by)


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _segment_hits(start, end, rect):
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


def _length(segment):
    return math.dist(segment[0], segment[1])


def _block_size(lines):
    return max(line.width for line in lines), sum(line.height for line in lines)


def _around_box(box, width, height, sides, gap=5.0):
    """Label rectangles beside a symbol, in the symbol's preferred side order."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    candidates = []
    for side in sides:
        if side in ("below", "above"):
            top = y1 + gap if side == "below" else y0 - gap - height
            for left in (cx - width / 2, x0, x1 - width):
                candidates.append(((left, top, left + width, top + height), "middle"))
        else:
            left, anchor = (x1 + gap, "start") if side == "right" else (x0 - gap - width, "end")
            for top in (cy - height / 2, y0, y1 - height):
                candidates.append(((left, top, left + width, top + height), anchor))
    return candidates


def _along_segments(segments, width, height, gap=5.0, inset=6.0):
    """Label rectangles beside an edge, longest segment first."""
    candidates = []
    ranked = sorted(range(len(segments)), key=lambda i: (-_length(segments[i]), i))
    for index in ranked:
        (x1, y1), (x2, y2) = segments[index]
        if abs(y1 - y2) <= _TOLERANCE:
            low, high = min(x1, x2), max(x1, x2)
            for top in (y1 - gap - height, y1 + gap):
                for left in ((low + high) / 2 - width / 2, high - inset - width, low + inset):
                    candidates.append(((left, top, left + width, top + height), "middle"))
        elif abs(x1 - x2) <= _TOLERANCE:
            low, high = min(y1, y2), max(y1, y2)
            for left, anchor in ((x1 + gap, "start"), (x1 - gap - width, "end")):
                for top in ((low + high) / 2 - height / 2, low + inset, high - inset - height):
                    candidates.append(((left, top, left + width, top + height), anchor))
        else:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            candidates.append(((mx + gap, my - gap - height, mx + gap + width, my - gap), "start"))
            candidates.append(((mx - gap - width, my + gap, mx - gap, my + gap + height), "end"))
    return candidates


def _wrap(text, size, width):
    columns = max(12, int(width / (_CHAR * size)))
    return textwrap.wrap(text, columns, break_on_hyphens=False) or [""]


# --- Drawing model ----------------------------------------------------------------


class _Drawing:
    """Resolved occurrences, derived geometry, label placement, and mappings."""

    def __init__(self, project, selection):
        model, pid, physical = project["model"], project["pid"], project["physical"]
        self.project = project
        self.entities = model["entities"]
        self.ports = model["ports"]
        self.connections = model["connections"]
        self.assumptions = model["assumptions"]
        self.symbols = pid["symbols"]
        self.edges = pid["edges"]
        self.placements = physical["placements"]
        self.routes = physical["routes"]
        self.diagnostics = []
        self.obstacles = []
        self.segments = []
        self.symbol_items = []
        self.symbol_by_id = {}
        self.edge_items = []
        self.relation_items = []
        self._index()
        self._resolve_symbols()
        self._resolve_edges()
        self._resolve_relations()
        self._place_labels()
        self.representations = self._representations()
        self.selected = self._select(selection)
        self._check_occurrence_ids()
        self._check_text()
        self.bounds = self._bounds()

    def _warn(self, code, ids, message):
        self.diagnostics.append(_diagnostic(code, "warning", ids, message))

    # Indexes and physical state

    def _index(self):
        self.semantic_kind = {}
        self.symbols_of = {eid: [] for eid in self.entities}
        for sid in sorted(self.symbols):
            self.symbols_of[self.symbols[sid]["entity_id"]].append(sid)
        self.edges_of = {cid: [] for cid in self.connections}
        for edge_id in sorted(self.edges):
            self.edges_of[self.edges[edge_id]["connection_id"]].append(edge_id)
        self.ports_of = {eid: [] for eid in self.entities}
        for port_id in sorted(self.ports):
            self.ports_of[self.ports[port_id]["entity_id"]].append(port_id)
        for eid, port_ids in self.ports_of.items():
            entity = self.entities[eid]
            order = list(entity_template(entity)["ports"])
            port_ids.sort(key=lambda port_id: (order.index(self.ports[port_id]["name"]), port_id))
        for kind, records in (("entity", self.entities), ("connection", self.connections), ("port", self.ports)):
            self.semantic_kind.update((key, kind) for key in records)

    def placement_state(self, eid):
        placement = self.placements.get(eid)
        if placement is None:
            return "missing"
        return "placed" if placement["status"] == "placed" else "unresolved"

    def route_state(self, cid):
        route = self.routes.get(cid)
        return "missing" if route is None else route["status"]

    # Geometry

    def _resolve_symbols(self):
        for sid in sorted(self.symbols):
            symbol = self.symbols[sid]
            eid = symbol["entity_id"]
            entity = self.entities[eid]
            spec = SYMBOLS.get(entity["kind"])
            if spec is None:
                spec = FALLBACK_SYMBOL
                definition = entity.get("definition") if isinstance(entity.get("definition"), dict) else None
                what = (f" (definition {definition.get('id')!r} declares symbol "
                        f"{(definition.get('symbol') or {}).get('shape')!r}, which this offline adapter does not draw)"
                        if definition is not None else "")
                self._warn(
                    "pid_symbol_missing",
                    [eid, sid],
                    f"no P&ID symbol is defined for kind {entity['kind']!r}{what}; occurrence {sid!r} is a placeholder",
                )
            x, y, angle = float(symbol["x"]), float(symbol["y"]), float(symbol["rotation_deg"])
            anchors = [(port_id, diagram_port_position(self.project, port_id, sid)) for port_id in self.ports_of[eid]]
            item = {
                "id": sid,
                "entity_id": eid,
                "entity": entity,
                "symbol": symbol,
                "spec": spec,
                "x": x,
                "y": y,
                "angle": angle,
                "box": _placed_box(spec.bbox, angle, x, y),
                "anchors": anchors,
            }
            self.symbol_items.append(item)
            self.symbol_by_id[sid] = item
            self.obstacles.append(item["box"])

    def _resolve_edges(self):
        for edge_id in sorted(self.edges):
            edge = self.edges[edge_id]
            connection = self.connections[edge["connection_id"]]
            start = diagram_port_position(self.project, connection["from_port_id"], edge["from_symbol_id"])
            end = diagram_port_position(self.project, connection["to_port_id"], edge["to_symbol_id"])
            status = edge["status"]
            points = [(float(x), float(y)) for x, y in edge["points"]]
            path, hint, detached = None, None, []
            if len(points) >= 2:
                path = points
                if status != "current":  # current paths already end on their anchors
                    if math.dist(start, points[0]) > _TOLERANCE:
                        detached.append((start, points[0]))
                    if math.dist(points[-1], end) > _TOLERANCE:
                        detached.append((points[-1], end))
            else:
                hint = [start, end]  # no drawable path: show topology only, flagged
            line = path or hint
            segments = [s for s in zip(line, line[1:]) if _length(s) > _TOLERANCE] or [(line[0], line[-1])]
            self.segments.extend(segments + detached)
            self.edge_items.append(
                {
                    "id": edge_id,
                    "edge": edge,
                    "connection_id": edge["connection_id"],
                    "connection": connection,
                    "status": status,
                    "path": path,
                    "hint": hint,
                    "detached": detached,
                    "segments": segments,
                }
            )

    def _attach_centre(self, item):
        if item["spec"].attach == "circle":
            return item["x"], item["y"]
        x0, y0, x1, y1 = item["box"]
        return (x0 + x1) / 2, (y0 + y1) / 2

    def _attach(self, item, toward):
        cx, cy = self._attach_centre(item)
        dx, dy = toward[0] - cx, toward[1] - cy
        distance = math.hypot(dx, dy)
        if distance <= _TOLERANCE:
            return cx, cy
        if item["spec"].attach == "circle":
            t = item["spec"].radius / distance
        else:
            x0, y0, x1, y1 = item["box"]
            t = min((x1 - x0) / 2 / abs(dx) if dx else math.inf, (y1 - y0) / 2 / abs(dy) if dy else math.inf)
        t = min(1.0, t)
        return cx + dx * t, cy + dy * t

    def _resolve_relations(self):
        for eid in sorted(self.entities):
            observes = self.entities[eid].get("observes")
            if not isinstance(observes, dict):
                continue
            subject = observes["entity_id"]
            targets = self.symbols_of.get(subject, [])
            for sid in self.symbols_of[eid]:
                if not targets:  # the validator reports the subject's missing occurrence
                    continue
                source = self.symbol_by_id[sid]
                origin = (source["x"], source["y"])
                target_id = min(
                    targets,
                    key=lambda t: (math.dist(origin, (self.symbol_by_id[t]["x"], self.symbol_by_id[t]["y"])), t),
                )
                target = self.symbol_by_id[target_id]
                line = (
                    self._attach(source, self._attach_centre(target)),
                    self._attach(target, self._attach_centre(source)),
                )
                self.segments.append(line)
                self.relation_items.append(
                    {
                        "id": f"{sid}#observes",
                        "entity_id": eid,
                        "symbol_id": sid,
                        "subject_id": subject,
                        "target_symbol_id": target_id,
                        "variable": observes["variable"],
                        "line": line,
                    }
                )

    # Labels

    def _place_labels(self):
        for item in self.symbol_items:
            lines, item["inner"] = self._symbol_lines(item)
            width, height = _block_size(lines) if lines else (0, 0)
            item["label"] = self._place(lines, _around_box(item["box"], width, height, item["spec"].label_sides))
        for item in self.edge_items:
            lines = self._edge_lines(item)
            width, height = _block_size(lines)
            item["label"] = self._place(lines, _along_segments(item["segments"], width, height))

    def _place(self, lines, candidates):
        if not lines or not candidates:
            return None
        best = None
        for rect, anchor in candidates:
            probe = _inflate(rect, 2.0)
            hits = sum(1 for box in self.obstacles if _overlaps(probe, box))
            hits += sum(1 for start, end in self.segments if _segment_hits(start, end, probe))
            if best is None or hits < best[0]:
                best = (hits, rect, anchor)
                if hits == 0:
                    break
        _, rect, anchor = best
        self.obstacles.append(rect)
        return {"lines": lines, "rect": rect, "anchor": anchor}

    def _symbol_lines(self, item):
        entity, spec = item["entity"], item["spec"]
        tag = entity["tag"]
        inner = None
        if spec.inner_text_width:
            size = min(_INNER_MAX_SIZE, spec.inner_text_width / (_CHAR * len(tag)))
            if size >= _INNER_MIN_SIZE:
                inner = _Line(tag, round(size, 2), bold=True, attrs=(("data-role", "tag"),))
        lines = [] if inner else [_Line(tag, _TAG_SIZE, bold=True, attrs=(("data-role", "tag"),))]
        observes = entity.get("observes")
        if isinstance(observes, dict):
            subject = self.entities[observes["entity_id"]]
            lines.append(
                _Line(
                    f"observes {observes['variable']} of {subject['tag']}",
                    _NOTE_SIZE,
                    fill=_RELATION,
                    attrs=(("data-role", "observes"), ("data-observed-entity-id", observes["entity_id"])),
                )
            )
        for name in entity_template(entity)["parameters"]:
            quantity = entity["parameters"][name]
            if "assumption_id" in quantity:
                mark, provenance = "*", ("data-assumption-id", quantity["assumption_id"])
            else:
                mark, provenance = "", ("data-source", quantity["source"])
            lines.append(
                _Line(
                    f"{name} {_num(quantity['value'])} {quantity['unit']}{mark}",
                    _NOTE_SIZE,
                    fill=_MUTED,
                    attrs=(("data-role", "parameter"), ("data-parameter", name), provenance),
                )
            )
        state = self.placement_state(item["entity_id"])
        if state != "placed":
            lines.append(
                _Line(f"3D PLACEMENT {state.upper()}", _NOTE_SIZE, fill=_PENDING, bold=True, attrs=(("data-role", "physical-status"),))
            )
        return lines, inner

    def _edge_lines(self, item):
        connection = item["connection"]
        mark = "*" if "assumption_id" in connection else ""
        lines = [
            _Line(
                f"{connection['tag']} DN{_num(connection['nominal_diameter_mm'])}{mark}",
                _LINE_TAG_SIZE,
                bold=True,
                attrs=(("data-role", "tag"),),
            )
        ]
        if item["status"] != "current":
            text = f"P&ID PATH {item['status'].replace('_', ' ').upper()}"
            lines.append(_Line(text, _NOTE_SIZE, fill=_STALE, bold=True, attrs=(("data-role", "path-status"),)))
        route = self.route_state(item["connection_id"])
        if route != "current":
            text = f"3D ROUTE {route.replace('_', ' ').upper()}"
            lines.append(_Line(text, _NOTE_SIZE, fill=_PENDING, bold=True, attrs=(("data-role", "physical-status"),)))
        return lines

    # Mappings, selection, and checks

    def _representations(self):
        representations = {}
        for eid in self.entities:
            relations = [r["id"] for r in self.relation_items if r["entity_id"] == eid]
            representations[eid] = sorted(self.symbols_of[eid] + relations)
        for cid in self.connections:
            representations[cid] = list(self.edges_of[cid])
        for port_id, port in self.ports.items():
            representations[port_id] = sorted(f"{sid}#{port_id}" for sid in self.symbols_of[port["entity_id"]])
        return dict(sorted(representations.items()))

    def _select(self, selection):
        selected, unknown = set(), {}
        for item in selection:
            if isinstance(item, str) and item in self.representations:
                selected.update(self.representations[item])
            else:
                unknown.setdefault(repr(item), item)
        for shown in sorted(unknown):
            item = unknown[shown]
            hint = ""
            if isinstance(item, str) and (item in self.symbols or item in self.edges):
                hint = "; it is a P&ID occurrence ID, and selection uses semantic IDs"
            self._warn(
                "selection_unknown_id",
                [item],
                f"selected ID {shown} is not an entity, connection, or port of project "
                f"{self.project['id']!r} at revision {self.project['revision']}{hint}; nothing is highlighted for it",
            )
        return selected

    def _check_occurrence_ids(self):
        persistent = set(self.symbols) | set(self.edges) | set(self.semantic_kind) | set(self.assumptions)
        seen = set()
        for semantic_id, occurrences in self.representations.items():
            for occurrence in occurrences:
                derived = occurrence not in self.symbols and occurrence not in self.edges
                if (derived and occurrence in persistent) or occurrence in seen:
                    self._warn(
                        "occurrence_id_collision",
                        [semantic_id, occurrence],
                        f"derived occurrence ID {occurrence!r} of {semantic_id!r} is not unique in the drawing",
                    )
                seen.add(occurrence)

    def _check_text(self):
        owners = set()
        if _has_invalid_text(self.project["id"]):
            owners.add(self.project["id"])
        for section in ("model", "pid", "physical"):
            for records in self.project[section].values():
                for key, record in records.items():
                    if _has_invalid_text(key) or _has_invalid_text(record):
                        owners.add(key)
        if owners:
            self._warn(
                "xml_text_replaced",
                sorted(owners),
                "text or IDs contain characters that XML 1.0 cannot represent; the SVG shows U+FFFD in their place, "
                "so its attributes do not reproduce those IDs exactly",
            )

    def _bounds(self):
        rects = []
        for item in self.symbol_items:
            rects.append(_inflate(item["box"], 6.0))
            if item["label"]:
                rects.append(item["label"]["rect"])
        for item in self.edge_items:
            for point in (item["path"] or item["hint"]) + [p for pair in item["detached"] for p in pair]:
                rects.append(_inflate((point[0], point[1], point[0], point[1]), 5.0))
            if item["label"]:
                rects.append(item["label"]["rect"])
        for item in self.relation_items:
            for x, y in item["line"]:
                rects.append((x, y, x, y))
        if not rects:
            return None
        return (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )


# --- SVG composition ----------------------------------------------------------------


def _compose(manifest, drawing):
    bounds = drawing.bounds if drawing is not None else None
    if bounds is None:
        content = (0.0, 0.0, 0.0, 0.0)
        content_top = content_bottom = 0
    else:
        content = bounds
        content_top = math.floor(bounds[1] - _MARGIN)
        content_bottom = math.ceil(bounds[3] + _MARGIN)
    width = max(_MIN_WIDTH, math.ceil(content[2] - content[0] + 2 * _MARGIN))
    left = math.floor((content[0] + content[2]) / 2 - width / 2)
    header = _header_rows(manifest, width)
    footer = _footer_rows(drawing, width) if drawing is not None else []
    header_height = math.ceil(_rows_height(header, width) + 2 * _PAD)
    footer_height = math.ceil(_rows_height(footer, width) + 2 * _PAD) if footer else 0
    top = content_top - header_height
    height = header_height + (content_bottom - content_top) + footer_height
    status = manifest["status"]

    attributes = {
        "xmlns": SVG_NAMESPACE,
        "version": "1.1",
        "width": _num(width),
        "height": _num(height),
        "viewBox": f"{_num(left)} {_num(top)} {_num(width)} {_num(height)}",
        "font-family": _FONT,
        "data-adapter": ADAPTER,
        "data-adapter-version": ADAPTER_VERSION,
        "data-project-id": manifest["project_id"] or "",
        "data-source-revision": "" if manifest["source_revision"] is None else str(manifest["source_revision"]),
        "data-input-digest": manifest["input_digest"] or "",
        "data-status": status,
    }
    root = ET.Element("svg", {name: _xml(value) for name, value in attributes.items()})
    shown_id = manifest["project_id"] if manifest["project_id"] is not None else "(unknown)"
    shown_revision = manifest["source_revision"] if manifest["source_revision"] is not None else "(unknown)"
    _sub(root, "title", (), f"P&ID · project {shown_id} · revision {shown_revision} · {status}")
    _sub(
        root,
        "desc",
        (),
        f"Prototype P&ID drawn by skid.adapters.pid {ADAPTER_VERSION} from one project snapshot. "
        "Original symbols; no drafting-standard conformance is claimed.",
    )
    _metadata(root, manifest, drawing)
    _sub(root, "rect", {"x": _num(left), "y": _num(top), "width": _num(width), "height": _num(height), "fill": _PAPER})
    _sub(
        root,
        "rect",
        {
            "x": _num(left + 4),
            "y": _num(top + 4),
            "width": _num(width - 8),
            "height": _num(height - 8),
            "fill": "none",
            "stroke": _INK,
            "stroke-width": "1",
        },
    )
    layer = _sub(root, "g", {"data-layer": "header"})
    _draw_rows(layer, header, left + _PAD, top + _PAD, width)
    _status_badge(layer, status, left + width - _PAD, top + _PAD)
    _rule(root, left, top + header_height, width)
    if drawing is not None:
        _draw_content(_sub(root, "g", {"data-layer": "drawing"}), drawing)
    if footer:
        _rule(root, left, content_bottom, width)
        _draw_rows(_sub(root, "g", {"data-layer": "footer"}), footer, left + _PAD, content_bottom + _PAD, width)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _metadata(root, manifest, drawing):
    container = _sub(root, "metadata")
    info = _sub(
        container,
        "skid:pid",
        {
            "xmlns:skid": METADATA_NAMESPACE,
            "adapter": manifest["adapter"],
            "adapter-version": manifest["adapter_version"],
            "project-id": manifest["project_id"] or "",
            "source-revision": "" if manifest["source_revision"] is None else str(manifest["source_revision"]),
            "input-digest": manifest["input_digest"] or "",
            "digest-algorithm": "SHA-256 of canonical project JSON (sorted keys, compact separators, UTF-8)",
            "status": manifest["status"],
        },
    )
    for semantic_id, occurrences in manifest["representations"].items():
        kind = drawing.semantic_kind[semantic_id] if drawing is not None else ""
        representation = _sub(info, "skid:representation", {"semantic-id": semantic_id, "semantic-kind": kind})
        for occurrence in occurrences:
            _sub(representation, "skid:occurrence", {"occurrence-id": occurrence})
    for diagnostic in manifest["diagnostics"]:
        element = _sub(
            info,
            "skid:diagnostic",
            {"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"]},
        )
        for entity_id in diagnostic["entity_ids"]:
            _sub(element, "skid:entity-id", {"value": entity_id})


def _rule(parent, left, y, width):
    _sub(
        parent,
        "line",
        {"x1": _num(left + 4), "y1": _num(y), "x2": _num(left + width - 4), "y2": _num(y), "stroke": _INK, "stroke-width": "1"},
    )


# Header and footer rows are (lines, gap after) or ("legend", gap after).


def _header_rows(manifest, width):
    status = manifest["status"]
    project_id = manifest["project_id"] if manifest["project_id"] is not None else "(unknown)"
    revision = manifest["source_revision"] if manifest["source_revision"] is not None else "(unknown)"
    text_width = width - 2 * _PAD
    title_width = text_width - _badge_width(status) - 10
    rows = [
        ([_Line(t, _TITLE_SIZE, bold=True) for t in _wrap(f"P&ID · project {project_id}", _TITLE_SIZE, title_width)], 3),
        (
            [
                _Line(t, _TAG_SIZE)
                for t in _wrap(
                    f"Revision {revision} · status {status.upper()} · adapter {ADAPTER} {ADAPTER_VERSION} · "
                    "drawn from one project snapshot",
                    _TAG_SIZE,
                    text_width,
                )
            ],
            1,
        ),
        (
            [
                _Line(t, _NOTE_SIZE, fill=_MUTED)
                for t in _wrap(
                    f"Input digest (SHA-256, canonical project JSON): {manifest['input_digest'] or 'unavailable'}",
                    _NOTE_SIZE,
                    text_width,
                )
            ],
            4,
        ),
    ]
    notes = (
        "PROTOTYPE DRAWING: illustrative only, not for design, procurement, or construction. Symbols are original "
        "to this project; no ISA-5.1, ISO 10628, or other drafting-standard conformance is claimed.",
        "Operating and safety design are omitted: duty, flow, pressure, temperature, material, and wall thickness "
        "are not modeled.",
        "Values marked * cite a prototype assumption listed under ASSUMPTIONS. Dashed blue lines are conceptual "
        "indication relations, not process piping or instrument hookups.",
    )
    for note in notes:
        rows.append(([_Line(t, _NOTE_SIZE, fill=_MUTED) for t in _wrap(note, _NOTE_SIZE, text_width)], 1))
    rows.append(("legend", 6))
    diagnostics = manifest["diagnostics"]
    colour = _STATUS_COLOURS[status]
    if status == "complete":
        summary = (
            "No open items: the P&ID and physical presentation records are complete for this revision. "
            "This drawing alone does not show that a 3D build succeeded, and it is not a process-design check."
        )
        rows.append(([_Line(t, _NOTE_SIZE, fill=colour, bold=True) for t in _wrap(summary, _NOTE_SIZE, text_width)], 0))
        return rows
    if status == "pending":
        summary = f"OPEN ITEMS ({len(diagnostics)}): this drawing is not a complete synchronized result."
    else:
        errors = sum(1 for d in diagnostics if d["severity"] == "error")
        summary = f"GENERATION FAILED ({errors} error(s)): no drawing was produced for this snapshot."
    rows.append(([_Line(t, _LINE_TAG_SIZE, fill=colour, bold=True) for t in _wrap(summary, _LINE_TAG_SIZE, text_width)], 2))
    for diagnostic in diagnostics[:_MAX_LISTED_DIAGNOSTICS]:
        text = f"- {diagnostic['severity'].upper()} [{diagnostic['code']}] {diagnostic['message']}"
        fill = _STALE if diagnostic["severity"] == "error" else _INK
        rows.append(([_Line(t, _NOTE_SIZE, fill=fill) for t in _wrap(text, _NOTE_SIZE, text_width)], 1))
    hidden = len(diagnostics) - _MAX_LISTED_DIAGNOSTICS
    if hidden > 0:
        rows.append(([_Line(f"... {hidden} more in the manifest", _NOTE_SIZE, fill=_MUTED)], 0))
    return rows


def _footer_rows(drawing, width):
    cited = set()
    for semantic_id, occurrences in drawing.representations.items():
        if not occurrences:
            continue
        kind = drawing.semantic_kind[semantic_id]
        if kind == "entity":
            parameters = drawing.entities[semantic_id]["parameters"].values()
            cited.update(q["assumption_id"] for q in parameters if "assumption_id" in q)
        else:
            record = drawing.connections[semantic_id] if kind == "connection" else drawing.ports[semantic_id]
            cited.add(record["assumption_id"])
    if not cited:
        return []
    text_width = width - 2 * _PAD
    rows = [([_Line("ASSUMPTIONS CITED BY DRAWN VALUES", _LINE_TAG_SIZE, bold=True)], 3)]
    for assumption_id in sorted(cited):
        record = drawing.assumptions[assumption_id]
        text = f"{assumption_id}: {record['description']} Source: {record['source']}"
        rows.append(([_Line(t, _NOTE_SIZE, fill=_MUTED) for t in _wrap(text, _NOTE_SIZE, text_width)], 2))
    return rows


_LEGEND = (
    ("process line", _PATH_STYLES["current"]),
    ("P&ID path needs reroute (stored points kept)", _PATH_STYLES["needs_reroute"]),
    ("P&ID path unresolved (stored points kept)", _PATH_STYLES["unresolved"]),
    ("no valid path: topology hint / detached end", _HINT_STYLE),
    ("conceptual indication relation", _RELATION_STYLE),
)
_LEGEND_ROW = 12.0


def _legend_layout(width):
    """Return legend entries as (row, x offset, label, style); style None marks the selection swatch."""
    entries = list(_LEGEND) + [("selected", None)]
    placed, row, x = [], 0, 0.0
    for label, style in entries:
        span = 22 + 4 + _CHAR * _NOTE_SIZE * len(label) + 14
        if x > 0 and x + span > width - 2 * _PAD:
            row, x = row + 1, 0.0
        placed.append((row, x, label, style))
        x += span
    return placed


def _rows_height(rows, width):
    height = 0.0
    for content, gap in rows:
        if content == "legend":
            height += _LEGEND_ROW * (1 + max(r for r, *_ in _legend_layout(width))) + gap
        else:
            height += sum(line.height for line in content) + gap
    return height


def _draw_rows(parent, rows, x, y, width):
    for content, gap in rows:
        if content == "legend":
            layout = _legend_layout(width)
            group = _sub(parent, "g", {"data-role": "legend"})
            for row, offset, label, style in layout:
                mid = y + row * _LEGEND_ROW + _LEGEND_ROW / 2
                left = x + offset
                if style is None:
                    _sub(
                        group,
                        "rect",
                        {
                            "x": _fix(left),
                            "y": _fix(mid - 4),
                            "width": "22",
                            "height": "8",
                            "rx": "2",
                            "fill": _HALO_FILL,
                            "fill-opacity": "0.6",
                            "stroke": _HALO_EDGE,
                        },
                    )
                else:
                    _sub(
                        group,
                        "line",
                        dict((("x1", _fix(left)), ("y1", _fix(mid)), ("x2", _fix(left + 22)), ("y2", _fix(mid))) + style),
                    )
                _sub(
                    group,
                    "text",
                    {"x": _fix(left + 26), "y": _fix(mid + _NOTE_SIZE * 0.35), "font-size": _fix(_NOTE_SIZE), "fill": _INK},
                    label,
                )
            y += _LEGEND_ROW * (1 + max(r for r, *_ in layout)) + gap
            continue
        for line in content:
            _text(parent, line, x, y, "start")
            y += line.height
        y += gap


def _badge_width(status):
    return _CHAR * 10 * len(status) + 14


def _status_badge(parent, status, right, top):
    width = _badge_width(status)
    group = _sub(parent, "g", {"data-role": "status-badge", "data-status": status})
    _sub(
        group,
        "rect",
        {"x": _fix(right - width), "y": _fix(top), "width": _fix(width), "height": "16", "rx": "3", "fill": _STATUS_COLOURS[status]},
    )
    _sub(
        group,
        "text",
        {
            "x": _fix(right - width / 2),
            "y": _fix(top + 11.5),
            "font-size": "10",
            "font-weight": "bold",
            "fill": _PAPER,
            "text-anchor": "middle",
        },
        status.upper(),
    )


def _text(parent, line, x, top, anchor):
    attrs = {"x": _fix(x), "y": _fix(top + line.size * 0.95), "font-size": _fix(line.size), "fill": line.fill}
    if anchor != "start":
        attrs["text-anchor"] = anchor
    if line.bold:
        attrs["font-weight"] = "bold"
    attrs.update(line.attrs)
    return _sub(parent, "text", attrs, line.text)


def _draw_block(parent, label):
    if label is None:
        return
    x0, y0, x1, _ = label["rect"]
    x = {"start": x0, "middle": (x0 + x1) / 2, "end": x1}[label["anchor"]]
    for line in label["lines"]:
        _text(parent, line, x, y0, label["anchor"])
        y0 += line.height


def _flag(value):
    return "true" if value else "false"


def _arrow(parent, segment, fill):
    (x1, y1), (x2, y2) = segment
    length = math.dist(segment[0], segment[1])
    if length < 16:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    tip = (mx + ux * 4, my + uy * 4)
    left = (mx - ux * 4 - uy * 3, my - uy * 4 + ux * 3)
    right = (mx - ux * 4 + uy * 3, my - uy * 4 - ux * 3)
    d = f"M {_fix(tip[0])} {_fix(tip[1])} L {_fix(left[0])} {_fix(left[1])} L {_fix(right[0])} {_fix(right[1])} Z"
    _sub(parent, "path", {"data-role": "flow-arrow", "d": d, "fill": fill})


def _draw_content(layer, drawing):
    selected = drawing.selected
    lines_layer = _sub(layer, "g", {"data-layer": "process-lines"})
    for item in drawing.edge_items:
        _draw_edge(lines_layer, drawing, item, item["id"] in selected)
    relations_layer = _sub(layer, "g", {"data-layer": "indication-relations"})
    for item in drawing.relation_items:
        _draw_relation(relations_layer, drawing, item, item["id"] in selected)
    symbols_layer = _sub(layer, "g", {"data-layer": "symbols"})
    for item in drawing.symbol_items:
        _draw_symbol(symbols_layer, drawing, item, selected)


def _draw_edge(parent, drawing, item, selected):
    edge, connection = item["edge"], item["connection"]
    ports = drawing.ports
    source, target = ports[connection["from_port_id"]], ports[connection["to_port_id"]]
    group = _sub(
        parent,
        "g",
        {
            "data-occurrence-id": item["id"],
            "data-occurrence-kind": "edge",
            "data-semantic-id": item["connection_id"],
            "data-semantic-kind": "connection",
            "data-tag": connection["tag"],
            "data-from-port-id": connection["from_port_id"],
            "data-to-port-id": connection["to_port_id"],
            "data-from-symbol-id": edge["from_symbol_id"],
            "data-to-symbol-id": edge["to_symbol_id"],
            "data-status": item["status"],
            "data-manual": _flag(edge["manual"]),
            "data-physical-route-status": drawing.route_state(item["connection_id"]),
            "data-selected": _flag(selected),
        },
    )
    _sub(
        group,
        "title",
        (),
        f"{connection['tag']} ({item['connection_id']}): {drawing.entities[source['entity_id']]['tag']} "
        f"{source['name']} -> {drawing.entities[target['entity_id']]['tag']} {target['name']}; "
        f"P&ID path {item['status']}",
    )
    line = item["path"] or item["hint"]
    if selected:
        _sub(
            group,
            "polyline",
            {
                "data-role": "selection-halo",
                "points": _points(line),
                "fill": "none",
                "stroke": _HALO_FILL,
                "stroke-opacity": "0.75",
                "stroke-width": "9",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
            },
        )
    if item["path"] is not None:
        style = _PATH_STYLES[item["status"]]
        role = "path" if item["status"] == "current" else "retained-path"
        _sub(group, "polyline", dict((("data-role", role), ("points", _points(item["path"])), ("fill", "none")) + style))
    else:
        style = _HINT_STYLE
        _sub(group, "polyline", dict((("data-role", "unresolved-hint"), ("points", _points(item["hint"])), ("fill", "none")) + style))
    for start, end in item["detached"]:
        _sub(group, "polyline", dict((("data-role", "detached-end"), ("points", _points((start, end))), ("fill", "none")) + _HINT_STYLE))
    longest = max(item["segments"], key=_length)
    _arrow(group, longest, dict(style)["stroke"])
    _draw_block(group, item["label"])


def _draw_relation(parent, drawing, item, selected):
    indicator = drawing.entities[item["entity_id"]]
    subject = drawing.entities[item["subject_id"]]
    group = _sub(
        parent,
        "g",
        {
            "data-occurrence-id": item["id"],
            "data-occurrence-kind": "indication-relation",
            "data-semantic-id": item["entity_id"],
            "data-semantic-kind": "entity",
            "data-indicator-symbol-id": item["symbol_id"],
            "data-observed-entity-id": item["subject_id"],
            "data-observed-symbol-id": item["target_symbol_id"],
            "data-variable": item["variable"],
            "data-selected": _flag(selected),
        },
    )
    _sub(
        group,
        "title",
        (),
        f"{indicator['tag']} observes {item['variable']} of {subject['tag']} (conceptual indication relation)",
    )
    (x1, y1), (x2, y2) = item["line"]
    ends = (("x1", _fix(x1)), ("y1", _fix(y1)), ("x2", _fix(x2)), ("y2", _fix(y2)))
    if selected:
        _sub(
            group,
            "line",
            dict(
                (("data-role", "selection-halo"),)
                + ends
                + (("stroke", _HALO_FILL), ("stroke-opacity", "0.75"), ("stroke-width", "8"), ("stroke-linecap", "round"))
            ),
        )
    _sub(group, "line", dict((("data-role", "indication-relation"),) + ends + _RELATION_STYLE))


def _draw_symbol(parent, drawing, item, selected):
    entity, symbol, spec = item["entity"], item["symbol"], item["spec"]
    is_selected = item["id"] in selected
    group = _sub(
        parent,
        "g",
        {
            "data-occurrence-id": item["id"],
            "data-occurrence-kind": "symbol",
            "data-semantic-id": item["entity_id"],
            "data-semantic-kind": "entity",
            "data-entity-kind": entity["kind"],
            "data-tag": entity["tag"],
            "data-x": _num(item["x"]),
            "data-y": _num(item["y"]),
            "data-rotation-deg": _num(item["angle"]),
            "data-manual": _flag(symbol["manual"]),
            "data-physical-status": drawing.placement_state(item["entity_id"]),
            "data-selected": _flag(is_selected),
        },
    )
    _sub(group, "title", (), f"{entity['tag']} ({item['entity_id']}): {entity['kind']} occurrence {item['id']}")
    if is_selected:
        x0, y0, x1, y1 = _inflate(item["box"], 5.0)
        _sub(
            group,
            "rect",
            {
                "data-role": "selection-halo",
                "x": _fix(x0),
                "y": _fix(y0),
                "width": _fix(x1 - x0),
                "height": _fix(y1 - y0),
                "rx": "4",
                "fill": _HALO_FILL,
                "fill-opacity": "0.55",
                "stroke": _HALO_EDGE,
                "stroke-width": "1.5",
            },
        )
    body = _sub(
        group,
        "g",
        {
            "data-role": "symbol-body",
            "transform": f"translate({_num(item['x'])} {_num(item['y'])}) rotate({_num(item['angle'])})",
        },
    )
    for tag, attrs in spec.primitives:
        _sub(body, tag, attrs)
    inner = item["inner"]
    if inner is not None:
        _text(group, inner, item["x"], item["y"] + inner.size * 0.35 - inner.size * 0.95, "middle")
    _draw_block(group, item["label"])
    for port_id, (ax, ay) in item["anchors"]:
        occurrence = f"{item['id']}#{port_id}"
        port_selected = occurrence in selected
        port_group = _sub(
            group,
            "g",
            {
                "data-occurrence-id": occurrence,
                "data-occurrence-kind": "port-anchor",
                "data-semantic-id": port_id,
                "data-semantic-kind": "port",
                "data-port-name": drawing.ports[port_id]["name"],
                "data-selected": _flag(port_selected),
            },
        )
        if port_selected:
            _sub(
                port_group,
                "circle",
                {
                    "data-role": "selection-halo",
                    "cx": _num(ax),
                    "cy": _num(ay),
                    "r": "6",
                    "fill": _HALO_FILL,
                    "fill-opacity": "0.8",
                    "stroke": _HALO_EDGE,
                },
            )
        _sub(
            port_group,
            "circle",
            {"data-role": "port-anchor", "cx": _num(ax), "cy": _num(ay), "r": "2.5", "fill": "none", "stroke": "none"},
        )
