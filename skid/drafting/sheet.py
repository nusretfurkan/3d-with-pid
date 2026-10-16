"""P&ID engineering sheet: progressive drawing state, layout and SVG composition.

Stored geometry is never moved. Symbols are drawn at the paper position of
their stored occurrence with their stored rotation; edges follow their stored
points; port anchors come from ``skid.templates.diagram_port_position``. Only
derived annotation is laid out here: tags, line designations, status badges,
flow arrows, conceptual observation relations, equipment data, notes and the
title block. Each unit of drawing work is applied with ``run_unit`` so that a
preview can be rendered from the state actually reached.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from ..templates import diagram_port_position
from . import conventions as conv
from . import paper
from .definitions import definition_name, definition_of, is_composed, kind_label, parameter_order
from .frame import TitleFields, draw_frame, draw_title_block
from .svgkit import (
    FONT_FAMILY,
    INK,
    MUTED,
    PAPER,
    SVG_NAMESPACE,
    Line,
    Run,
    block_size,
    collinear_overlap,
    contains,
    draw_block,
    draw_text_line,
    fix,
    has_invalid_text,
    inflate,
    keys_attr,
    length,
    line,
    num,
    overlaps,
    parallel_proximity,
    placed_box,
    points_attr,
    polyline_midpoint,
    rotate_point,
    segment_crossing,
    segment_hits_rect,
    serialize,
    sub,
    text_width,
    wrap,
    xml_text,
)
from .symbols import FALLBACK, symbol_for_entity

ADAPTER = "pid"
ADAPTER_VERSION = "0.2.0"
METADATA_NAMESPACE = "urn:x-skid:drafting:1"

DRAWING_AREA = inflate(paper.BORDER, -6)
TITLE_KEEP_OUT = inflate(paper.TITLE_BLOCK, 6)
LABEL_GAP = 4.0
LABEL_LEVELS = 4
LEVEL_STEP = 8.0
LEADER_FRACTIONS = (0.5, 0.25, 0.75)
LINE_TAG_SIZE = 8.0
CALLOUT_SIZE = 7.0
CALLOUT_TAG_SIZE = 9.5
NOTE_SIZE = 7.0
NOTE_TITLE_SIZE = 8.5
EQUIPMENT_DATA_KINDS = ("tank", "centrifugal_pump")
CALLOUT_PARAMETERS = 2  # composed equipment: parameters shown in its data block, in template order
CALLOUT_TEXT_WIDTH = 150.0
INNER_MIN_SIZE = 6.5
OPEN_ITEM_PHRASES = {
    "placement_unresolved": "3D PLACEMENT PENDING",
    "placement_missing": "3D PLACEMENT MISSING",
    "route_not_current": "3D ROUTE PENDING",
    "route_missing": "3D ROUTE MISSING",
    "edge_not_current": "P&ID PATH NOT CURRENT",
    "edge_missing": "NOT DRAWN (NO P&ID EDGE)",
    "symbol_missing": "NOT DRAWN (NO P&ID SYMBOL)",
    "out_of_sheet": "OUTSIDE SHEET",
    "outside_drawing_frame": "EXTENDS BEYOND BORDER",
    "title_block_overlap": "OVERLAPS TITLE BLOCK",
    "pid_symbol_missing": "PLACEHOLDER SYMBOL",
    "pid_anchor_unmatched": "CONNECTION POINT NOT ON SYMBOL",
}
MAX_OPEN_ITEMS = 6


def _diagnostic(code, severity, ids, message):
    return {"code": code, "severity": severity, "entity_ids": [i for i in dict.fromkeys(ids) if isinstance(i, str) and i],
            "message": message}


def zones_along(points):
    """Zones crossed by a polyline, in path order (grid lines split segments)."""
    found = []
    columns, rows = paper.column_lines(), paper.row_lines()
    for a, b in zip(points, points[1:]):
        ts = {0.0, 1.0}
        for grid in columns:
            if (a[0] - grid) * (b[0] - grid) < 0:
                ts.add((grid - a[0]) / (b[0] - a[0]))
        for grid in rows:
            if (a[1] - grid) * (b[1] - grid) < 0:
                ts.add((grid - a[1]) / (b[1] - a[1]))
        ordered = sorted(ts)
        for t0, t1 in zip(ordered, ordered[1:]):
            t = (t0 + t1) / 2
            zone = paper.zone_of(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if zone is not None and zone not in found:
                found.append(zone)
    if len(points) == 1:
        zone = paper.zone_of(*points[0])
        if zone is not None:
            found.append(zone)
    return found


def _split_tag(tag):
    """Split an instrument tag into the two lines shown inside its bubble."""
    if "-" in tag:
        head, tail = tag.split("-", 1)
        if head and tail:
            return head, tail
    for index, char in enumerate(tag):
        if char.isdigit() and index > 0:
            return tag[:index], tag[index:]
    return tag, ""


def project_label(identity):
    """Human-facing project label: the display name followed by the stable id, or the id alone if unnamed."""
    name = identity.get("project_name")
    return f"{name} ({identity['project_id']})" if name else str(identity["project_id"])


def _join_tags(tags):
    if len(tags) <= 1:
        return "".join(tags)
    return ", ".join(tags[:-1]) + " AND " + tags[-1]


class PidSheet:
    """Drawing state for sheet P&ID-001, advanced one unit of work at a time."""

    def __init__(self, project, graph, model_diagnostics=()):
        model, pid, physical = project["model"], project["pid"], project["physical"]
        self.project = project
        self.graph = graph
        self.model_diagnostics = list(model_diagnostics)
        self.entities = model["entities"]
        self.ports = model["ports"]
        self.connections = model["connections"]
        self.symbols = pid["symbols"]
        self.edges = pid["edges"]
        self.placements = physical["placements"]
        self.routes = physical["routes"]
        self.diagnostics = []
        self.layout_findings = []
        self.symbols_of = {eid: [] for eid in self.entities}
        for sid in sorted(self.symbols):
            self.symbols_of[self.symbols[sid]["entity_id"]].append(sid)
        self.edges_of = {cid: [] for cid in self.connections}
        for edge_id in sorted(self.edges):
            self.edges_of[self.edges[edge_id]["connection_id"]].append(edge_id)
        self.symbol_items, self.symbol_by_id = [], {}
        self.edge_items, self.relation_items = [], []
        self.frame_done = False
        self.labels_done = False
        self.panels_done = False
        self.callouts = []
        self.notes = None
        self.units = self._plan()
        self._check_text()

    # --- Plan and units of work -----------------------------------------------------

    def tag(self, semantic_id):
        record = self.entities.get(semantic_id) or self.connections.get(semantic_id)
        return record["tag"] if record else semantic_id

    def _plan(self):
        units = [("frame", None, "Laying out sheet P&ID-001: border, zones 1-10 and A-I, title block")]
        for eid in self.graph.entity_order:
            entity = self.entities[eid]
            for sid in self.symbols_of[eid]:
                units.append(("symbol", sid, f"Placing {entity['tag']} ({kind_label(entity)}) symbol {sid}"))
        for cid in self.graph.connection_order:
            connection = self.connections[cid]
            source, target = self.graph.ends[cid]
            for edge_id in self.edges_of[cid]:
                units.append(("edge", edge_id, f"Drawing {connection['tag']} from {self.tag(source)} to "
                               f"{self.tag(target)} along stored path {edge_id}"))
        for eid in self.graph.entity_order:
            observes = self.entities[eid].get("observes")
            if not isinstance(observes, dict) or not self.symbols_of.get(observes["entity_id"]):
                continue
            for sid in self.symbols_of[eid]:
                units.append(("relation", sid, f"Drawing observation relation {self.tag(eid)} to "
                              f"{self.tag(observes['entity_id'])}"))
        units.append(("labels", None, "Placing tags, line designations, flow arrows and status badges"))
        units.append(("panels", None, "Adding equipment data, notes and the title/revision block"))
        return units

    def run_unit(self, unit):
        """Do one unit of drawing work; return a message describing what was drawn."""
        kind, target, _ = unit
        if kind == "frame":
            self.frame_done = True
            return "Sheet P&ID-001 frame, zone margins and title block outline drawn"
        if kind == "symbol":
            item = self._add_symbol(target)
            return f"Placed {item['entity']['tag']} at {paper.zone_ref(item['zone'])}"
        if kind == "edge":
            item = self._add_edge(target)
            return f"Drew {item['connection']['tag']} ({item['status'].replace('_', ' ')}) through {paper.zone_ref(item['zone'])}"
        if kind == "relation":
            item = self._add_relation(target)
            if item is None:
                return f"No observation relation drawn for {target}"
            return f"Drew observation relation {self.tag(item['entity_id'])} to {self.tag(item['subject_id'])}"
        if kind == "labels":
            self._place_labels()
            return f"Placed {sum(1 for i in self.symbol_items + self.edge_items if i['label'])} label blocks"
        if kind == "panels":
            self._place_panels()
            return f"Added {len(self.callouts)} equipment data blocks and {len(self.notes['notes'])} notes"
        raise ValueError(f"unknown drawing unit {kind!r}")

    # --- Geometry ---------------------------------------------------------------------

    def placement_state(self, eid):
        placement = self.placements.get(eid)
        if placement is None:
            return "missing"
        return "placed" if placement["status"] == "placed" else "unresolved"

    def route_state(self, cid):
        route = self.routes.get(cid)
        return "missing" if route is None else route["status"]

    def _warn(self, code, ids, message):
        self.diagnostics.append(_diagnostic(code, "warning", ids, message))

    def status_diagnostics(self):
        """Model findings followed by the sheet's own, as known at this point of the work."""
        return self.model_diagnostics + self.diagnostics

    def _add_symbol(self, sid):
        symbol = self.symbols[sid]
        eid = symbol["entity_id"]
        entity = self.entities[eid]
        spec = symbol_for_entity(entity, self.graph.templates[eid].get("diagram_anchors"))
        if spec is None and is_composed(entity):
            spec = FALLBACK
            shape = (definition_of(entity).get("symbol") or {}).get("shape")
            self._warn("pid_symbol_missing", [eid, sid], f"no generic outline {shape!r} is available for "
                       f"{entity['tag']} ({sid}); it is drawn as a placeholder")
        elif spec is None:
            spec = FALLBACK
            self._warn("pid_symbol_missing", [eid, sid], f"no sheet symbol is defined for kind {entity['kind']!r}; "
                       f"{entity['tag']} ({sid}) is drawn as a placeholder")
        elif spec.glyph is not None and spec.glyph.unmatched:
            self._warn("pid_anchor_unmatched", [eid, sid], f"{entity['tag']} ({sid}): connection point(s) "
                       f"{', '.join(spec.glyph.unmatched)} lie inside the {spec.glyph.family.replace('_', ' ')} outline "
                       "at its smallest drawn size, so no line can meet the symbol there; they are marked on the sheet")
        px, py = paper.to_paper(symbol["x"], symbol["y"])
        angle = float(symbol["rotation_deg"])
        box = placed_box(spec.bbox, angle, px, py)
        anchors = []
        for port_id in self.graph.ports_of[eid]:
            ax, ay = diagram_port_position(self.project, port_id, sid)
            anchors.append((port_id, paper.to_paper(ax, ay)))
        zone = paper.zone_of(px, py)
        if zone is None:
            self._warn("out_of_sheet", [eid, sid], f"{entity['tag']} ({sid}) is at paper ({num(px)}, {num(py)}), "
                       "outside the inner border of P&ID-001; it has no zone reference")
        elif not contains(paper.BORDER, box):
            self._warn("outside_drawing_frame", [eid, sid], f"{entity['tag']} ({sid}) extends beyond the inner "
                       "border of P&ID-001")
        if overlaps(box, paper.TITLE_BLOCK):
            self._warn("title_block_overlap", [eid, sid], f"{entity['tag']} ({sid}) overlaps the title block")
        item = {
            "id": sid,
            "entity_id": eid,
            "entity": entity,
            "symbol": symbol,
            "spec": spec,
            "x": px,
            "y": py,
            "angle": angle,
            "box": box,
            "anchors": anchors,
            "zone": zone,
            "inner": self._inner_text(entity, spec, angle),
            "label": None,
        }
        self.symbol_items.append(item)
        self.symbol_by_id[sid] = item
        return item

    def _inner_text(self, entity, spec, angle=0.0):
        tag = entity["tag"]
        if spec.inner == "single":
            width = spec.inner_width
            if spec.inner_width_across and angle % 180:
                width = spec.inner_width_across
            size = spec.tag_size
            while size > INNER_MIN_SIZE and text_width(tag, size, True) > width:
                size -= 0.25
            if text_width(tag, size, True) <= width:
                return [line(tag, size, bold=True, attrs=(("data-role", "tag"),))]
        elif spec.inner == "split":
            head, tail = _split_tag(tag)
            sizes = (spec.tag_size, spec.tag_size - 0.5)
            parts = [(head, sizes[0], True)] + ([(tail, sizes[1], False)] if tail else [])
            while True:
                if all(text_width(t, s, b) <= spec.inner_width for t, s, b in parts):
                    return [line(t, s, bold=b, attrs=(("data-role", "tag"),)) for t, s, b in parts]
                if parts[0][1] <= INNER_MIN_SIZE:
                    break
                parts = [(t, s - 0.25, b) for t, s, b in parts]
        return None

    def _add_edge(self, edge_id):
        edge = self.edges[edge_id]
        cid = edge["connection_id"]
        connection = self.connections[cid]
        start = paper.to_paper(*diagram_port_position(self.project, connection["from_port_id"], edge["from_symbol_id"]))
        end = paper.to_paper(*diagram_port_position(self.project, connection["to_port_id"], edge["to_symbol_id"]))
        status = edge["status"]
        points = [paper.to_paper(x, y) for x, y in edge["points"]]
        path, hint, detached = None, None, []
        if len(points) >= 2:
            path = points
            if status != "current":
                if math.dist(start, points[0]) > 1e-6:
                    detached.append((start, points[0]))
                if math.dist(points[-1], end) > 1e-6:
                    detached.append((points[-1], end))
        else:
            hint = [start, end]
        drawn = path or hint
        segments = [s for s in zip(drawn, drawn[1:]) if length(s) > 1e-6] or [(drawn[0], drawn[-1])]
        key = conv.PATH_STATUS_KEYS[status] if path else "line/process/topology-hint"
        midpoint = polyline_midpoint(drawn)
        zone = paper.zone_of(*midpoint)
        every = drawn + [p for pair in detached for p in pair]
        outside = [p for p in every if not paper.inside(*p)]
        if outside:
            self._warn("out_of_sheet", [cid, edge_id], f"{connection['tag']} ({edge_id}) has {len(outside)} point(s) "
                       "outside the inner border of P&ID-001")
        if any(segment_hits_rect(a, b, paper.TITLE_BLOCK) for a, b in segments + detached):
            self._warn("title_block_overlap", [cid, edge_id], f"{connection['tag']} ({edge_id}) crosses the title block")
        item = {
            "id": edge_id,
            "edge": edge,
            "connection_id": cid,
            "connection": connection,
            "status": status,
            "key": key,
            "path": path,
            "hint": hint,
            "detached": detached,
            "segments": segments,
            "midpoint": midpoint,
            "zone": zone,
            "zones": zones_along(drawn),
            "label": None,
        }
        self.edge_items.append(item)
        return item

    def _attach_centre(self, item):
        if item["spec"].attach == "circle":
            return item["x"], item["y"]
        x0, y0, x1, y1 = item["box"]
        return (x0 + x1) / 2, (y0 + y1) / 2

    def _boundary_point(self, item, direction):
        """Point where a ray from the attach centre along ``direction`` leaves the symbol outline."""
        cx, cy = self._attach_centre(item)
        dx, dy = direction
        if item["spec"].attach == "circle":
            return cx + dx * item["spec"].radius, cy + dy * item["spec"].radius
        x0, y0, x1, y1 = item["box"]
        t = min((x1 - x0) / 2 / abs(dx) if dx else math.inf, (y1 - y0) / 2 / abs(dy) if dy else math.inf)
        return cx + dx * t, cy + dy * t

    def _edge_hit(self, item, x=None, y=None):
        """Point on the outline where a vertical line (given x) or horizontal line (given y) first meets it."""
        cx, cy = self._attach_centre(item)
        if item["spec"].attach == "circle":
            r = item["spec"].radius
            if x is not None:
                offset = x - cx
                if abs(offset) > r * 0.92:
                    return None
                return math.sqrt(r * r - offset * offset)
            offset = y - cy
            if abs(offset) > r * 0.92:
                return None
            return math.sqrt(r * r - offset * offset)
        x0, y0, x1, y1 = item["box"]
        if x is not None:
            return (y1 - y0) / 2 if x0 + 2 <= x <= x1 - 2 else None
        return (x1 - x0) / 2 if y0 + 2 <= y <= y1 - 2 else None

    def _relation_candidates(self, source, target):
        sx, sy = source["x"], source["y"]
        tx, ty = self._attach_centre(target)
        rs = source["spec"].radius or 16.0
        candidates = []
        # Straight vertical or horizontal when the bubble lies within the target's extent.
        half = self._edge_hit(target, x=sx)
        if half is not None and abs(sy - ty) > rs + half:
            sign = 1 if ty > sy else -1
            candidates.append(([(sx, sy + sign * rs), (sx, ty - sign * half)], "straight"))
        half = self._edge_hit(target, y=sy)
        if half is not None and abs(sx - tx) > rs + half:
            sign = 1 if tx > sx else -1
            candidates.append(([(sx + sign * rs, sy), (tx - sign * half, sy)], "straight"))
        # Vertical first, then horizontal into the target's side.
        if abs(sx - tx) > 1e-6 and abs(sy - ty) > rs:
            half = self._edge_hit(target, y=ty)
            sign_y = 1 if ty > sy else -1
            sign_x = 1 if tx > sx else -1
            if half is not None and abs(sx - tx) > half + 4:
                candidates.append(([(sx, sy + sign_y * rs), (sx, ty), (tx - sign_x * half, ty)], "elbow"))
        # Horizontal first, then vertical into the target's top or bottom.
        if abs(sy - ty) > 1e-6 and abs(sx - tx) > rs:
            half = self._edge_hit(target, x=tx)
            sign_y = 1 if ty > sy else -1
            sign_x = 1 if tx > sx else -1
            if half is not None and abs(sy - ty) > half + 4:
                candidates.append(([(sx + sign_x * rs, sy), (tx, sy), (tx, ty - sign_y * half)], "elbow"))
        # Direct line between the outlines as the last resort.
        dx, dy = tx - sx, ty - sy
        distance = math.hypot(dx, dy) or 1.0
        direction = (dx / distance, dy / distance)
        start = (sx + direction[0] * rs, sy + direction[1] * rs)
        end = self._boundary_point(target, (-direction[0], -direction[1]))
        candidates.append(([start, end], "diagonal"))
        return candidates

    def _relation_score(self, points, shape, source, target):
        segments = list(zip(points, points[1:]))
        score = sum(length(s) for s in segments) / 100.0 + 0.5 * (len(segments) - 1)
        if shape == "diagonal":
            score += 6.0
        process = [s for item in self.edge_items for s in item["segments"] + item["detached"]]
        for segment in segments:
            for other in process:
                if segment_crossing(segment, other):
                    score += 3.0
                if collinear_overlap(segment, other) > 0.5:
                    score += 100.0
                if parallel_proximity(segment, other, 24.0) > 4.0:
                    score += 8.0
            for other in (s for r in self.relation_items for s in zip(r["points"], r["points"][1:])):
                if segment_crossing(segment, other) or collinear_overlap(segment, other) > 0.5:
                    score += 10.0
            for item in self.symbol_items:
                if item is source or item is target:
                    continue
                if segment_hits_rect(segment[0], segment[1], inflate(item["box"], 2)):
                    score += 50.0
        end = points[-1]
        for _, anchor in target["anchors"]:
            if math.dist(end, anchor) < 8.0:
                score += 20.0
        return score

    def _add_relation(self, sid):
        source = self.symbol_by_id.get(sid)
        if source is None:
            return None
        eid = source["entity_id"]
        observes = self.entities[eid]["observes"]
        subject = observes["entity_id"]
        targets = [self.symbol_by_id[t] for t in self.symbols_of.get(subject, []) if t in self.symbol_by_id]
        if not targets:
            return None
        target = min(targets, key=lambda t: (math.dist((source["x"], source["y"]), (t["x"], t["y"])), t["id"]))
        best = None
        for points, shape in self._relation_candidates(source, target):
            score = self._relation_score(points, shape, source, target)
            if best is None or score < best[0] - 1e-9:
                best = (score, points, shape)
        _, points, shape = best
        midpoint = polyline_midpoint(points)
        item = {
            "id": f"{sid}#observes",
            "entity_id": eid,
            "symbol_id": sid,
            "subject_id": subject,
            "target_symbol_id": target["id"],
            "variable": observes["variable"],
            "points": points,
            "shape": shape,
            "midpoint": midpoint,
            "zone": paper.zone_of(*midpoint),
        }
        self.relation_items.append(item)
        return item

    # --- Crossings and arrows --------------------------------------------------------

    def crossings(self):
        """Edge ID -> crossing points where it passes over an earlier line (relations lie under all edges)."""
        found = {}
        earlier = [(s, "relation") for r in self.relation_items for s in zip(r["points"], r["points"][1:])]
        for item in self.edge_items:
            own = item["segments"] + item["detached"]
            points = []
            for segment in own:
                for other, _ in earlier:
                    point = segment_crossing(segment, other)
                    if point is not None:
                        dx, dy = other[1][0] - other[0][0], other[1][1] - other[0][1]
                        run = math.hypot(dx, dy) or 1.0
                        points.append((point, (dx / run, dy / run)))
            found[item["id"]] = points
            earlier.extend((s, item["id"]) for s in own)
        return found

    def arrow_for(self, item, crossing_points):
        """Arrow tip and direction on the longest segment, kept clear of crossings."""
        candidates = sorted(item["segments"], key=lambda s: -length(s))
        for segment in candidates:
            run = length(segment)
            if run < 18:
                continue
            (x1, y1), (x2, y2) = segment
            ux, uy = (x2 - x1) / run, (y2 - y1) / run
            for fraction in (0.5, 0.35, 0.65, 0.25, 0.75):
                centre = (x1 + (x2 - x1) * fraction, y1 + (y2 - y1) * fraction)
                if any(math.dist(centre, p) < 12 for p, _ in crossing_points):
                    continue
                if math.dist(centre, segment[0]) < 10 or math.dist(centre, segment[1]) < 10:
                    continue
                tip = (centre[0] + ux * conv.ARROW_LENGTH / 2, centre[1] + uy * conv.ARROW_LENGTH / 2)
                return tip, (ux, uy)
        return None

    @staticmethod
    def arrow_rect(arrow):
        tip, (ux, uy) = arrow
        base = (tip[0] - ux * conv.ARROW_LENGTH, tip[1] - uy * conv.ARROW_LENGTH)
        half = conv.ARROW_HALF_WIDTH
        xs = (tip[0], base[0] - uy * half, base[0] + uy * half)
        ys = (tip[1], base[1] + ux * half, base[1] - ux * half)
        return (min(xs), min(ys), max(xs), max(ys))

    # --- Labels -------------------------------------------------------------------------

    def _all_segments(self):
        segments = []
        for item in self.edge_items:
            segments.extend(item["segments"] + item["detached"])
        for item in self.relation_items:
            segments.extend(zip(item["points"], item["points"][1:]))
        return segments

    def _symbol_lines(self, item):
        entity, spec = item["entity"], item["spec"]
        lines = []
        if item["inner"] is None:
            lines.append(line(entity["tag"], spec.tag_size, bold=True, attrs=(("data-role", "tag"),)))
        if entity["kind"] == "boundary":
            lines.append(line("CONTINUES OUTSIDE MODELED SCOPE", 6.2, fill=MUTED, attrs=(("data-role", "scope-note"),)))
        if self.placement_state(item["entity_id"]) != "placed":
            lines.append(conv.badge_line("marker/physical-placement-pending"))
        return lines

    def _edge_lines(self, item):
        connection = item["connection"]
        designation = Line(
            (Run(connection["tag"], True), Run(f" DN{num(connection['nominal_diameter_mm'])}")),
            LINE_TAG_SIZE,
            keys=("convention/line-designation", "abbr/DN"),
            attrs=(("data-role", "tag"),),
        )
        lines = [designation]
        if item["key"] != "line/process":
            lines.append(conv.badge_line(item["key"]))
        if self.route_state(item["connection_id"]) != "current":
            lines.append(conv.badge_line("marker/physical-route-pending"))
        return lines

    # Label candidates are (rect, text anchor, leader or None, level). Level 0 sits beside its item; higher
    # levels stand further off and are joined to the item by a thin leader, as a drafter would do in a
    # crowded run. Collision-free positions always win; nearer and earlier-listed positions break ties.

    @staticmethod
    def _around_box(box, width, height, sides, gap=LABEL_GAP):
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        candidates = []
        for level in range(LABEL_LEVELS):
            extra = level * (LEVEL_STEP + height / 2)
            for side in sides:
                if side in ("below", "above"):
                    top = y1 + gap + extra if side == "below" else y0 - gap - extra - height
                    for left in (cx - width / 2, x0, x1 - width, cx - width / 2 + 14, cx - width / 2 - 14):
                        leader = None
                        if level:
                            lx = min(max(cx, left + 2, x0 + 2), left + width - 2, x1 - 2)
                            if not (left + 2 <= lx <= left + width - 2 and x0 <= lx <= x1):
                                continue
                            edge = y1 if side == "below" else y0
                            leader = ((lx, edge), (lx, top if side == "below" else top + height))
                        candidates.append(((left, top, left + width, top + height), "middle", leader, level))
                else:
                    left, anchor = ((x1 + gap + extra, "start") if side == "right" else (x0 - gap - extra - width, "end"))
                    for top in (cy - height / 2, y0, y1 - height, y0 - height / 2, y1 - height / 2):
                        leader = None
                        if level:
                            ly = min(max(cy, top + 2, y0 + 2), top + height - 2, y1 - 2)
                            if not (top + 2 <= ly <= top + height - 2 and y0 <= ly <= y1):
                                continue
                            edge = x1 if side == "right" else x0
                            leader = ((edge, ly), (left if side == "right" else left + width, ly))
                        candidates.append(((left, top, left + width, top + height), anchor, leader, level))
        return candidates

    @staticmethod
    def _along_segments(segments, width, height, gap=5.0, inset=8.0):
        candidates = []
        ranked = sorted(range(len(segments)), key=lambda i: (-length(segments[i]), i))
        for level in range(LABEL_LEVELS):
            extra = level * (LEVEL_STEP + height / 2)
            for index in ranked:
                (x1, y1), (x2, y2) = segments[index]
                if abs(y1 - y2) <= 1e-6:
                    low, high = min(x1, x2), max(x1, x2)
                    mid = (low + high) / 2
                    for side in ("above", "below"):
                        top = y1 - gap - extra - height if side == "above" else y1 + gap + extra
                        if level == 0:
                            for left in (mid - width / 2, low + inset, high - inset - width):
                                candidates.append(((left, top, left + width, top + height), "middle", None, 0))
                            continue
                        for fraction in LEADER_FRACTIONS:
                            px = low + (high - low) * fraction
                            end = top + height if side == "above" else top
                            for left in (px - width / 2, px - width + 6, px - 6):
                                candidates.append(((left, top, left + width, top + height), "middle",
                                                   ((px, y1), (px, end)), level))
                elif abs(x1 - x2) <= 1e-6:
                    low, high = min(y1, y2), max(y1, y2)
                    mid = (low + high) / 2
                    for side in ("right", "left"):
                        left = x1 + gap + 1 + extra if side == "right" else x1 - gap - 1 - extra - width
                        anchor = "start" if side == "right" else "end"
                        if level == 0:
                            for top in (mid - height / 2, low + inset, high - inset - height):
                                candidates.append(((left, top, left + width, top + height), anchor, None, 0))
                            continue
                        for fraction in LEADER_FRACTIONS:
                            py = low + (high - low) * fraction
                            end = left if side == "right" else left + width
                            top = py - height / 2
                            candidates.append(((left, top, left + width, top + height), anchor,
                                               ((x1, py), (end, py)), level))
                elif level == 0:
                    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                    candidates.append(((mx + gap, my - gap - height, mx + gap + width, my - gap), "start", None, 0))
                    candidates.append(((mx - gap - width, my + gap, mx - gap, my + gap + height), "end", None, 0))
        return candidates

    def _collisions(self, rect, obstacles, segments, labels):
        probe = inflate(rect, 1.5)
        count = sum(1 for box in obstacles if overlaps(probe, box))
        count += sum(1 for a, b in segments if segment_hits_rect(a, b, probe))
        count += sum(1 for other in labels if overlaps(probe, other))
        return count

    def _leader_collisions(self, leader, own_box, obstacles, segments, labels):
        start, end = leader
        count = 0
        for box in obstacles:
            if own_box is not None and box == own_box:
                continue
            if segment_hits_rect(start, end, inflate(box, -0.5)):
                count += 1
        for segment in segments:
            if segment_crossing(leader, segment) or collinear_overlap(leader, segment) > 0.5:
                count += 1
        count += sum(1 for other in labels if segment_hits_rect(start, end, other))
        return count

    def _choose(self, lines, candidates, obstacles, segments, labels, own_box=None):
        best = None
        for order, (rect, anchor, leader, level) in enumerate(candidates):
            penalty = order * 0.01 + level * 25
            penalty += 1000 * self._collisions(rect, obstacles, segments, labels)
            if leader is not None:
                penalty += 1000 * self._leader_collisions(leader, own_box, obstacles, segments, labels)
            if not contains(DRAWING_AREA, rect):
                penalty += 5000
            if overlaps(rect, TITLE_KEEP_OUT):
                penalty += 5000
            if best is None or penalty < best[0]:
                best = (penalty, rect, anchor, leader)
        penalty, rect, anchor, leader = best
        return {"lines": lines, "rect": rect, "anchor": anchor, "leader": leader, "clear": penalty < 1000}

    def _place_labels(self):
        crossings = self.crossings()
        obstacles = [inflate(item["box"], 1.0) for item in self.symbol_items]
        for item in self.edge_items:
            item["arrow"] = self.arrow_for(item, crossings[item["id"]])
            if item["arrow"] is not None:
                obstacles.append(self.arrow_rect(item["arrow"]))
        segments = self._all_segments()
        labels = []
        for item in self.symbol_items:
            lines = self._symbol_lines(item)
            if not lines:
                continue
            width, height = block_size(lines)
            item["label"] = self._choose(lines, self._around_box(item["box"], width, height, item["spec"].label_sides),
                                         obstacles, segments, labels, own_box=inflate(item["box"], 1.0))
            labels.append(item["label"]["rect"])
            if item["label"]["leader"]:
                segments.append(item["label"]["leader"])
        for item in self.edge_items:
            lines = self._edge_lines(item)
            width, height = block_size(lines)
            item["label"] = self._choose(lines, self._along_segments(item["segments"], width, height),
                                         obstacles, segments, labels)
            labels.append(item["label"]["rect"])
            if item["label"]["leader"]:
                segments.append(item["label"]["leader"])
        for item in self.symbol_items + self.edge_items:
            if item["label"] and not item["label"]["clear"]:
                owner = item.get("entity_id") or item.get("connection_id")
                self.layout_findings.append({"code": "label_collision", "occurrence_id": item["id"],
                                             "semantic_id": owner,
                                             "message": f"no collision-free position for the label of {self.tag(owner)}"})
        self.labels_done = True

    # --- Equipment data, notes and title ----------------------------------------------

    def content_rects(self):
        rects = [inflate(item["box"], 2) for item in self.symbol_items]
        rects += [item["label"]["rect"] for item in self.symbol_items + self.edge_items if item["label"]]
        for a, b in self._all_segments():
            rects.append(inflate((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), 2))
        return rects

    def _callout_lines(self, entity):
        kind, parameters = entity["kind"], entity["parameters"]
        lines = [line(entity["tag"], CALLOUT_TAG_SIZE, bold=True, underline=True, attrs=(("data-role", "tag"),))]

        def value(name):
            quantity = parameters[name]
            return f"{num(quantity['value'])} {quantity['unit']}", "assumption_id" in quantity

        if kind == "tank":
            diameter, d_assumed = value("diameter")
            height, h_assumed = value("height")
            assumed = d_assumed or h_assumed
            keys = ("abbr/dia",) + (("convention/assumed-value",) if assumed else ())
            lines += [
                line("VERTICAL TANK", CALLOUT_SIZE),
                line(f"Ø {diameter}, HEIGHT {height}{' *' if assumed else ''}", CALLOUT_SIZE, keys=keys,
                     attrs=(("data-role", "equipment-dimensions"),)),
                line("DESIGN DATA: NOT SPECIFIED", CALLOUT_SIZE, fill=MUTED),
            ]
        elif kind == "centrifugal_pump":
            lines += [
                line("CENTRIFUGAL PUMP", CALLOUT_SIZE),
                line("DRIVER: MOTOR, RATING NOT SPECIFIED", CALLOUT_SIZE, fill=MUTED),
                line("FLOW AND HEAD: NOT SPECIFIED", CALLOUT_SIZE, fill=MUTED),
            ]
        elif is_composed(entity):
            lines += self._composed_callout_lines(entity)
        return lines

    def _composed_callout_lines(self, entity):
        """Declared name, the first parameters in template order (authoritative current values) and the
        absent design data. Parameter names are shown in words; the description lists all of them."""
        name = wrap(definition_name(entity).upper(), CALLOUT_SIZE, CALLOUT_TEXT_WIDTH)
        lines = [line(name[0] + ("…" if len(name) > 1 else ""), CALLOUT_SIZE)]
        order = parameter_order(entity, self.graph.templates[entity["id"]])
        names = sorted(entity["parameters"], key=lambda n: (order.index(n) if n in order else len(order), n))
        for parameter in names[:CALLOUT_PARAMETERS]:
            quantity = entity["parameters"][parameter]
            assumed = "assumption_id" in quantity
            lines.append(line(f"{parameter.replace('_', ' ').upper()} {num(quantity['value'])} {quantity['unit']}"
                              f"{' *' if assumed else ''}", CALLOUT_SIZE,
                              keys=("convention/assumed-value",) if assumed else (),
                              attrs=(("data-role", "equipment-dimensions"),)))
        if len(names) > CALLOUT_PARAMETERS:
            lines.append(line(f"{len(names) - CALLOUT_PARAMETERS} MORE PARAMETER(S) IN THE DESCRIPTION", CALLOUT_SIZE,
                              fill=MUTED))
        lines.append(line("DESIGN DATA: NOT SPECIFIED", CALLOUT_SIZE, fill=MUTED))
        return lines

    def _notes(self, status_diagnostics, blocks):
        notes = [
            "PROTOTYPE DRAWING GENERATED FROM THE ENGINEERING MODEL. NOT FOR DESIGN, PROCUREMENT OR CONSTRUCTION "
            "AND NOT AN AS-BUILT RECORD. SYMBOLS ARE ORIGINAL; NO DRAFTING-STANDARD CONFORMANCE IS CLAIMED.",
            "OPERATING AND SAFETY DESIGN ARE OMITTED. FLUID, DUTY, FLOW, PRESSURE, TEMPERATURE, MATERIALS AND "
            "RATINGS ARE NOT SPECIFIED.",
        ]
        kinds = {item["entity"]["kind"] for item in self.symbol_items}
        if kinds & {"isolation_valve", "check_valve"}:
            notes.append("VALVE OPERATORS, NORMAL POSITIONS AND FAIL ACTIONS ARE NOT MODELED AND ARE NOT SHOWN.")
        if any(item["spec"].glyph is not None for item in self.symbol_items):
            notes.append("GENERIC OUTLINES SHOW PROJECT-DEFINED (COMPOSED) EQUIPMENT. THEIR NAMES AND FUNCTIONS ARE "
                         "AUTHORED DEFINITION TEXT, NOT VERIFIED DESIGN OR OPERATING DATA.")
        indicators = []
        for item in self.symbol_items:
            if item["entity"]["kind"] == "indicator" and item["entity"]["tag"] not in indicators:
                indicators.append(item["entity"]["tag"])
        if indicators:
            verb = "IS A CONCEPTUAL INDICATION" if len(indicators) == 1 else "ARE CONCEPTUAL INDICATIONS"
            notes.append(f"{_join_tags(indicators)} {verb}. DOTTED OBSERVATION RELATIONS ARE NOT PIPING, SIGNAL "
                         "LINES, HOOKUPS OR CONTROL LOOPS.")
        if any("convention/assumed-value" in item.keys for block in blocks for item in block["lines"]):
            notes.append("VALUES MARKED * ARE PROTOTYPE ASSUMPTIONS; SEE THE ASSUMPTION REGISTER IN THE PROCESS "
                         "DESCRIPTION.")
        notes.append(f"SYMBOLS, CONVENTIONS AND THE TAGGED ITEM INDEX WITH ZONE REFERENCES ARE ON LEGEND SHEET "
                     f"{paper.LEGEND_SHEET_ID}. THE PROCESS DESCRIPTION LISTS ITEM FUNCTIONS, ASSUMPTIONS AND UNKNOWNS.")
        open_items = self.open_items(status_diagnostics)
        if open_items:
            shown = open_items[:MAX_OPEN_ITEMS]
            text = "OPEN ITEMS: " + "; ".join(shown)
            if len(open_items) > len(shown):
                text += f"; AND {len(open_items) - len(shown)} MORE IN THE BUILD LOG"
            notes.append(text + ".")
        return notes

    def open_items(self, diagnostics):
        items = []
        for diagnostic in diagnostics:
            if diagnostic["severity"] != "warning":
                continue
            phrase = OPEN_ITEM_PHRASES.get(diagnostic["code"], diagnostic["code"].replace("_", " ").upper())
            owners = [i for i in diagnostic["entity_ids"] if i in self.entities or i in self.connections]
            subject = self.tag(owners[0]) if owners else "SHEET"
            text = f"{subject} {phrase}"
            if text not in items:
                items.append(text)
        return items

    def _place_panels(self):
        blocks = self._callout_blocks()
        notes = self._notes(self.status_diagnostics(), blocks)
        content = self.content_rects()
        # Notes: right-hand column above the title block when clear, else a band left of the title block.
        candidates = (
            (888.0, paper.BORDER[1] + 14.0, paper.BORDER[2] - 10.0, "right", paper.TITLE_BLOCK[1] - 10),
            (paper.BORDER[0] + 14.0, None, paper.TITLE_BLOCK[0] - 14.0, "bottom", paper.BORDER[3] - 6),
        )
        chosen = None
        for x0, top, x1, where, limit in candidates:
            lines = self._note_lines(notes, x1 - x0)
            height = sum(item.height for _, item in lines) + NOTE_TITLE_SIZE * 1.9
            if top is None:
                top = paper.BORDER[3] - 10 - height
            rect = (x0, top, x1, top + height)
            clear = rect[3] <= limit and not any(overlaps(r, rect) for r in content)
            if chosen is None or clear:
                chosen = (rect, lines, where, clear)
            if clear:
                break
        rect, lines, where, clear = chosen
        self.notes = {"notes": notes, "rect": rect, "lines": lines, "where": where}
        if not clear:
            self.layout_findings.append({"code": "notes_collision", "occurrence_id": None, "semantic_id": None,
                                         "message": "the notes panel could not be placed clear of the drawing"})
        content.append(rect)
        right_limit = (rect[0] - 16) if where == "right" else paper.BORDER[2] - 14
        self._place_callouts(blocks, content, right_limit, where)
        self.panels_done = True

    def _callout_blocks(self):
        seen, blocks = set(), []
        for item in self.symbol_items:
            entity = item["entity"]
            if (entity["kind"] not in EQUIPMENT_DATA_KINDS and not is_composed(entity)) or item["entity_id"] in seen:
                continue
            seen.add(item["entity_id"])
            lines = self._callout_lines(entity)
            width, height = block_size(lines)
            blocks.append({"entity_id": item["entity_id"], "symbol_id": item["id"], "lines": lines,
                           "width": width + 8, "height": height, "centre": item["x"]})
        return blocks

    def _note_lines(self, notes, width):
        lines = []
        for number, text in enumerate(notes, 1):
            wrapped = wrap(text, NOTE_SIZE, width - 14)
            for index, part in enumerate(wrapped):
                lines.append((f"{number}." if index == 0 else "", line(part, NOTE_SIZE, attrs=(("data-note", str(number)),))))
        return lines

    def _place_callouts(self, blocks, content, right_limit, notes_where):
        if not blocks:
            self.callouts = []
            return
        # An information strip along the bottom border, left of the title block, aligned with each item;
        # a strip along the top border when the bottom one would collide with the drawing.
        left_limit = paper.BORDER[0] + 14
        bands = []
        if notes_where != "bottom":
            bands.append(("bottom", None, paper.TITLE_BLOCK[0] - 14))
        bands.append(("top", paper.BORDER[1] + 14.0, right_limit))
        best = None
        for name, top, limit in bands:
            height = max(b["height"] for b in blocks)
            if top is None:
                top = paper.BORDER[3] - 10 - height
            rects = self._pack(blocks, left_limit, limit, top)
            hits = sum(1 for r in rects for c in content if overlaps(r, c))
            fits = rects and rects[0][0] >= left_limit - 1e-6 and rects[-1][2] <= limit + 1e-6
            score = hits + (0 if fits else 100)
            if best is None or score < best[0]:
                best = (score, name, rects)
            if score == 0:
                break
        score, name, rects = best
        self.callouts = []
        for block, rect in zip(sorted(blocks, key=lambda b: (b["centre"], b["entity_id"])), rects):
            self.callouts.append({"entity_id": block["entity_id"], "symbol_id": block["symbol_id"], "lines": block["lines"],
                                  "rect": rect, "band": name})
        if score:
            self.layout_findings.append({"code": "equipment_data_collision", "occurrence_id": None, "semantic_id": None,
                                         "message": "equipment data blocks could not all be placed clear of the drawing"})

    @staticmethod
    def _pack(blocks, left_limit, right_limit, top):
        ordered = sorted(blocks, key=lambda b: (b["centre"], b["entity_id"]))
        lefts = []
        cursor = left_limit
        for block in ordered:
            left = max(block["centre"] - block["width"] / 2, cursor)
            lefts.append(left)
            cursor = left + block["width"] + 14
        limit = right_limit
        for index in range(len(ordered) - 1, -1, -1):
            width = ordered[index]["width"]
            if lefts[index] + width > limit:
                lefts[index] = limit - width
            limit = lefts[index] - 14
        return [(left, top, left + b["width"], top + b["height"]) for left, b in zip(lefts, ordered)]

    def _subtitle(self):
        """``SOURCES TO SINKS VIA PUMPS`` read at entity level, only where that reading is exact: one connected
        flow, every process item on a line and no item declaring separate flow paths. Otherwise (no flow,
        several flows, an unconnected draft item or a multi-path item) the generic ``PROCESS SCHEMATIC``, since
        entity-level wording would join items that no line joins."""
        groups, unconnected = self.graph.flow_groups()
        if len(groups) != 1 or unconnected or any(self.graph.flow_paths(eid) for eid in self.entities):
            return "PROCESS SCHEMATIC"
        sources = [s["id"] for s in self.graph.steps if s["item_kind"] == "entity" and not self.graph.incoming[s["id"]]]
        sinks = [s["id"] for s in self.graph.steps if s["item_kind"] == "entity" and not self.graph.outgoing[s["id"]]]
        pumps = [s["id"] for s in self.graph.steps
                 if s["item_kind"] == "entity" and self.entities[s["id"]]["kind"] == "centrifugal_pump"]
        if sources and sinks:
            subtitle = f"{_join_tags([self.tag(e) for e in sources])} TO {_join_tags([self.tag(e) for e in sinks])}"
            if pumps:
                subtitle += f" VIA {_join_tags([self.tag(e) for e in pumps])}"
        else:
            subtitle = "PROCESS SCHEMATIC"
        return subtitle

    def title_fields(self, status, open_count, preview):
        subtitle = self._subtitle()
        revision = str(self.project["revision"])
        if preview is not None:
            text = f"WORKING PREVIEW {preview[0]}/{preview[1]}"
        elif status == "complete":
            text = "COMPLETE"
        elif status == "pending":
            text = f"PENDING: {open_count} OPEN ITEM{'S' if open_count != 1 else ''}"
        else:
            text = "FAILED"
        return TitleFields(
            title="PIPING AND INSTRUMENTATION DIAGRAM",
            subtitle=subtitle,
            drawing_no=paper.SHEET_ID,
            project=self.project.get("name") or self.project["id"],
            revision=revision,
            revision_description=f"GENERATED FROM ENGINEERING MODEL REVISION {revision}",
            status=text,
            status_pending=status != "complete",
            status_progress=preview is not None,
            drawn=f"GENERATED BY SKID.DRAFTING {ADAPTER_VERSION}",
        )

    # --- Mappings and checks -------------------------------------------------------------

    def representations(self):
        representations = {}
        for eid in self.entities:
            relations = [r["id"] for r in self.relation_items if r["entity_id"] == eid]
            drawn = [sid for sid in self.symbols_of[eid] if sid in self.symbol_by_id]
            representations[eid] = sorted(drawn + relations)
        drawn_edges = {item["id"] for item in self.edge_items}
        for cid in self.connections:
            representations[cid] = [e for e in self.edges_of[cid] if e in drawn_edges]
        for port_id, port in self.ports.items():
            drawn = [sid for sid in self.symbols_of[port["entity_id"]] if sid in self.symbol_by_id]
            representations[port_id] = sorted(f"{sid}#{port_id}" for sid in drawn)
        return dict(sorted(representations.items()))

    def occurrence_records(self):
        """Indexable occurrences of tagged items, in drawing order."""
        records = []
        for item in self.symbol_items:
            records.append({"semantic_id": item["entity_id"], "occurrence_id": item["id"], "occurrence_kind": "symbol",
                            "position": [item["x"], item["y"]], "zone": item["zone"], "zones": [item["zone"]] if item["zone"] else []})
        for item in self.edge_items:
            records.append({"semantic_id": item["connection_id"], "occurrence_id": item["id"], "occurrence_kind": "edge",
                            "position": list(item["midpoint"]), "zone": item["zone"], "zones": item["zones"]})
        for item in self.relation_items:
            records.append({"semantic_id": item["entity_id"], "occurrence_id": item["id"],
                            "occurrence_kind": "observation", "position": list(item["midpoint"]), "zone": item["zone"],
                            "zones": zones_along(item["points"])})
        return records

    def _check_text(self):
        owners = set()
        if has_invalid_text(self.project["id"]):
            owners.add(self.project["id"])
        for section in ("model", "pid", "physical"):
            for records in self.project[section].values():
                for key, record in records.items():
                    if has_invalid_text(key) or has_invalid_text(record):
                        owners.add(key)
        if owners:
            self._warn("xml_text_replaced", sorted(owners), "text or IDs contain characters that XML 1.0 cannot "
                       "represent; the sheets show U+FFFD in their place")

    # --- SVG composition -----------------------------------------------------------------

    def render(self, identity, status, diagnostics, preview=None):
        """Serialize the current drawing state. ``preview`` is ``(step, total)`` for a working preview."""
        root = ET.Element("svg", {name: xml_text(value) for name, value in {
            "xmlns": SVG_NAMESPACE,
            "version": "1.1",
            "width": str(paper.WIDTH),
            "height": str(paper.HEIGHT),
            "viewBox": f"0 0 {paper.WIDTH} {paper.HEIGHT}",
            "font-family": FONT_FAMILY,
            "data-sheet-id": paper.SHEET_ID,
            "data-adapter": ADAPTER,
            "data-adapter-version": ADAPTER_VERSION,
            "data-project-id": identity["project_id"],
            "data-source-revision": str(identity["source_revision"]),
            "data-input-digest": identity["input_digest"],
            "data-status": "preview" if preview else status,
            "data-preview": "true" if preview else "false",
        }.items()})
        if preview:
            root.set("data-preview-step", str(preview[0]))
            root.set("data-preview-total", str(preview[1]))
        sub(root, "title", (), f"{paper.SHEET_ID} · {project_label(identity)} · revision "
            f"{identity['source_revision']} · {'working preview' if preview else status}")
        sub(root, "desc", (), "Prototype piping and instrumentation diagram generated by skid.drafting from one "
            "engineering-model snapshot. Original symbols; no drafting-standard conformance is claimed.")
        metadata = sub(root, "metadata")
        sub(metadata, "skid:drawing", {
            "xmlns:skid": METADATA_NAMESPACE,
            "sheet-id": paper.SHEET_ID,
            "adapter": ADAPTER,
            "adapter-version": ADAPTER_VERSION,
            "project-id": identity["project_id"],
            "source-revision": str(identity["source_revision"]),
            "input-digest": identity["input_digest"],
            "status": "preview" if preview else status,
        })
        if self.frame_done:
            draw_frame(root, with_keys=True)
        drawing = sub(root, "g", (("data-layer", "drawing"),))
        relations = sub(drawing, "g", (("data-layer", "observation-relations"),))
        for item in self.relation_items:
            self._draw_relation(relations, item)
        process = sub(drawing, "g", (("data-layer", "process-lines"),))
        crossings = self.crossings()
        for item in self.edge_items:
            self._draw_edge(process, item, crossings[item["id"]])
        symbols = sub(drawing, "g", (("data-layer", "symbols"),))
        for item in self.symbol_items:
            self._draw_symbol(symbols, item)
        if self.panels_done:
            self._draw_callouts(root)
            self._draw_notes(root)
        if self.frame_done:
            open_count = len(self.open_items(diagnostics))
            draw_title_block(root, self.title_fields(status, open_count, preview))
        return serialize(root)

    def _occurrence_attrs(self, semantic_id, semantic_kind, occurrence_id, occurrence_kind, position, zone):
        return [
            ("data-occurrence-id", occurrence_id),
            ("data-occurrence-kind", occurrence_kind),
            ("data-semantic-id", semantic_id),
            ("data-semantic-kind", semantic_kind),
            ("data-tag", self.tag(semantic_id)),
            ("data-paper-x", num(position[0])),
            ("data-paper-y", num(position[1])),
            ("data-zone", zone or ""),
            ("data-ref", paper.zone_ref(zone)),
        ]

    def _draw_relation(self, parent, item):
        attrs = self._occurrence_attrs(item["entity_id"], "entity", item["id"], "observation", item["midpoint"],
                                       item["zone"])
        attrs += [("data-indicator-symbol-id", item["symbol_id"]), ("data-observed-entity-id", item["subject_id"]),
                  ("data-observed-symbol-id", item["target_symbol_id"]), ("data-variable", item["variable"])]
        group = sub(parent, "g", attrs)
        sub(group, "title", (), f"{self.tag(item['entity_id'])} observes {item['variable'].replace('_', ' ')} of "
            f"{self.tag(item['subject_id'])} (conceptual relation, not piping or a control loop)")
        style = dict(conv.LINE_STYLES["line/observation"])
        style.update({"points": points_attr(item["points"]), "fill": "none", "data-role": "observation-relation"})
        style.update(dict(keys_attr(("line/observation",))))
        sub(group, "polyline", style)

    def _draw_edge(self, parent, item, crossing_points):
        edge, connection = item["edge"], item["connection"]
        attrs = self._occurrence_attrs(item["connection_id"], "connection", item["id"], "edge", item["midpoint"],
                                       item["zone"])
        attrs += [
            ("data-zones", " ".join(item["zones"])),
            ("data-from-port-id", connection["from_port_id"]),
            ("data-to-port-id", connection["to_port_id"]),
            ("data-from-symbol-id", edge["from_symbol_id"]),
            ("data-to-symbol-id", edge["to_symbol_id"]),
            ("data-status", item["status"]),
            ("data-manual", "true" if edge["manual"] else "false"),
            ("data-physical-route-status", self.route_state(item["connection_id"])),
        ]
        group = sub(parent, "g", attrs)
        source, target = self.graph.ends[item["connection_id"]]
        sub(group, "title", (), f"{connection['tag']}: {self.tag(source)} to {self.tag(target)}, "
            f"DN{num(connection['nominal_diameter_mm'])}; P&ID path {item['status'].replace('_', ' ')}")
        for point, (ux, uy) in crossing_points:
            sub(group, "line", {
                "x1": fix(point[0] - ux * 4.5), "y1": fix(point[1] - uy * 4.5),
                "x2": fix(point[0] + ux * 4.5), "y2": fix(point[1] + uy * 4.5),
                "stroke": PAPER, "stroke-width": "4", "data-role": "crossing-gap",
                "data-legend-keys": "marker/crossing-gap",
            })
        line_points = item["path"] or item["hint"]
        role = "path" if item["path"] and item["status"] == "current" else ("retained-path" if item["path"] else "topology-hint")
        style = dict(conv.LINE_STYLES[item["key"]])
        style.update({"points": points_attr(line_points), "fill": "none", "stroke-linejoin": "miter",
                      "data-role": role, "data-legend-keys": item["key"]})
        sub(group, "polyline", style)
        for start, end in item["detached"]:
            style = dict(conv.LINE_STYLES["line/process/detached-end"])
            style.update({"points": points_attr((start, end)), "fill": "none", "data-role": "detached-end",
                          "data-legend-keys": "line/process/detached-end"})
            sub(group, "polyline", style)
        arrow = item.get("arrow") if self.labels_done else self.arrow_for(item, crossing_points)
        if arrow is not None:
            colour = dict(conv.LINE_STYLES[item["key"]])["stroke"]
            sub(group, "path", {"d": conv.arrow_path(*arrow), "fill": colour, "data-role": "flow-arrow",
                                "data-legend-keys": "marker/flow-arrow"})
        self._draw_label(group, item["label"])

    @staticmethod
    def _draw_label(group, label):
        if not label:
            return
        layer = sub(group, "g", (("data-role", "label"),))
        if label["leader"]:
            (x1, y1), (x2, y2) = label["leader"]
            sub(layer, "line", {"x1": fix(x1), "y1": fix(y1), "x2": fix(x2), "y2": fix(y2), "stroke": INK,
                                "stroke-width": "0.5", "data-role": "leader",
                                "data-legend-keys": "convention/label-leader"})
        draw_block(layer, label["lines"], label["rect"], label["anchor"])

    def _draw_symbol(self, parent, item):
        entity, symbol, spec = item["entity"], item["symbol"], item["spec"]
        attrs = self._occurrence_attrs(item["entity_id"], "entity", item["id"], "symbol", (item["x"], item["y"]),
                                       item["zone"])
        attrs += [
            ("data-entity-kind", entity["kind"]),
            ("data-x", num(symbol["x"])),
            ("data-y", num(symbol["y"])),
            ("data-rotation-deg", num(item["angle"])),
            ("data-manual", "true" if symbol["manual"] else "false"),
            ("data-physical-status", self.placement_state(item["entity_id"])),
        ]
        if spec.glyph is not None:
            attrs += [("data-symbol-shape", spec.glyph.family),
                      ("data-definition-id", str(definition_of(entity).get("id", "")))]
        group = sub(parent, "g", attrs)
        sub(group, "title", (), f"{entity['tag']}: {kind_label(entity)} ({paper.zone_ref(item['zone'])})")
        body = sub(group, "g", (("data-role", "symbol-body"),
                                ("transform", f"translate({num(item['x'])} {num(item['y'])}) rotate({num(item['angle'])})"),
                                ("data-legend-keys", spec.key)))
        for tag, primitive in spec.draw(1.0):
            sub(body, tag, primitive)
        if item["inner"]:
            ox, oy = rotate_point(spec.inner_offset, item["angle"])
            cx, cy = item["x"] + ox, item["y"] + oy
            gap = 2.2
            y = cy - (sum(part.size * 0.72 for part in item["inner"]) + gap * (len(item["inner"]) - 1)) / 2
            for part in item["inner"]:
                baseline = y + part.size * 0.72
                run = part.runs[0]
                attrs = {"x": fix(cx), "y": fix(baseline), "font-size": fix(part.size), "text-anchor": "middle",
                         "fill": run.fill, "data-role": "tag"}
                if run.bold:
                    attrs["font-weight"] = "bold"
                sub(group, "text", attrs, run.text)
                y = baseline + gap
        for port_id, (ax, ay) in item["anchors"]:
            port_group = sub(group, "g", (
                ("data-occurrence-id", f"{item['id']}#{port_id}"),
                ("data-occurrence-kind", "port-anchor"),
                ("data-semantic-id", port_id),
                ("data-semantic-kind", "port"),
                ("data-port-name", self.ports[port_id]["name"]),
                ("data-paper-x", num(ax)),
                ("data-paper-y", num(ay)),
            ))
            sub(port_group, "circle", {"data-role": "port-anchor", "cx": num(ax), "cy": num(ay), "r": "2.5",
                                       "fill": "none", "stroke": "none"})
        self._draw_label(group, item["label"])

    def _draw_callouts(self, root):
        layer = sub(root, "g", (("data-layer", "equipment-data"),))
        for callout in self.callouts:
            x0, y0, x1, y1 = callout["rect"]
            group = sub(layer, "g", (("data-annotation-of", callout["entity_id"]),
                                     ("data-annotation-kind", "equipment-data"),
                                     ("data-symbol-id", callout["symbol_id"]),
                                     ("data-legend-keys", "convention/equipment-data")))
            draw_block(group, callout["lines"], (x0, y0, x1, y1), "middle")

    def _draw_notes(self, root):
        notes = self.notes
        x0, y0, x1, _ = notes["rect"]
        layer = sub(root, "g", (("data-layer", "notes"), ("data-notes-position", notes["where"])))
        sub(layer, "text", {"x": fix(x0), "y": fix(y0 + NOTE_TITLE_SIZE), "font-size": fix(NOTE_TITLE_SIZE),
                            "font-weight": "bold", "fill": INK, "letter-spacing": "0.6"}, "NOTES")
        rule_y = y0 + NOTE_TITLE_SIZE * 1.35
        sub(layer, "line", {"x1": fix(x0), "y1": fix(rule_y), "x2": fix(x1), "y2": fix(rule_y), "stroke": INK,
                            "stroke-width": "0.6"})
        top = y0 + NOTE_TITLE_SIZE * 1.9
        for number, item in notes["lines"]:
            if number:
                sub(layer, "text", {"x": fix(x0), "y": fix(top + item.size * 0.86), "font-size": fix(item.size),
                                    "font-weight": "bold", "fill": INK}, number)
            draw_text_line(layer, item, x0 + 14, top, "start")
            top += item.height
