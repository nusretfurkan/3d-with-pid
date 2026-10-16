"""Legend sheet LEG-001: exactly the conventions drawn on P&ID-001, plus the tagged-item index.

Entries are generated from a drawing inventory (``inventory.parse_inventory``),
never from the symbol library: a key appears once however many times it is
drawn, and a key that is no longer drawn disappears. Samples carry only
``data-legend-sample``; index rows carry ``data-semantic-id`` and
``data-occurrence-id = index/<semantic id>`` for selection.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import conventions as conv
from . import paper
from .frame import TitleFields, draw_frame, draw_title_block
from .sheet import ADAPTER_VERSION as PID_VERSION, project_label
from .svgkit import FONT_FAMILY, INK, MUTED, PAPER, RULE, SVG_NAMESPACE, fix, serialize, sub, wrap, xml_text

ADAPTER = "legend"
ADAPTER_VERSION = "0.2.0"
ENTRY_TOP = 84.0
ENTRY_BOTTOM = paper.BORDER[3] - 10.0
COLUMNS = ((44.0, 410.0), (428.0, 794.0))
SAMPLE_WIDTH, SAMPLE_HEIGHT = 66.0, 30.0
INDEX_LEFT, INDEX_RIGHT = 816.0, 1160.0
INDEX_TOP = 44.0
INDEX_BOTTOM = paper.TITLE_BLOCK[1] - 12.0
SECTION_HEIGHT = 16.0
MAX_USED_BY = 8


def _used_by_text(tags):
    if not tags:
        return None
    shown = tags[:MAX_USED_BY]
    text = "USED BY: " + ", ".join(shown)
    if len(tags) > len(shown):
        text += f" AND {len(tags) - len(shown)} MORE"
    return text


def _entry_layout(key, used_by, text_width_available):
    convention = conv.CONVENTIONS[key]
    title = wrap(convention.title, 7.5, text_width_available, True)
    description = wrap(convention.description, 6.5, text_width_available)
    used = wrap(_used_by_text(used_by), 6.2, text_width_available) if used_by else []
    text_height = len(title) * 9.0 + len(description) * 8.0 + len(used) * 7.6
    return title, description, used, max(SAMPLE_HEIGHT + 4, text_height + 6)


def render_legend(identity, status, open_count, keys, used_by, index, preview=None) -> tuple[str, list[dict]]:
    """Return the legend SVG and layout warnings (an overflow is an open item, never a silent omission)."""
    warnings = []
    root = ET.Element("svg", {name: xml_text(value) for name, value in {
        "xmlns": SVG_NAMESPACE,
        "version": "1.1",
        "width": str(paper.WIDTH),
        "height": str(paper.HEIGHT),
        "viewBox": f"0 0 {paper.WIDTH} {paper.HEIGHT}",
        "font-family": FONT_FAMILY,
        "data-sheet-id": paper.LEGEND_SHEET_ID,
        "data-legend-for": paper.SHEET_ID,
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
    sub(root, "title", (), f"{paper.LEGEND_SHEET_ID} legend for {paper.SHEET_ID} · {project_label(identity)} · "
        f"revision {identity['source_revision']} · {'working preview' if preview else status}")
    sub(root, "desc", (), "Legend generated from the conventions actually drawn on the P&ID sheet, with the tagged-item "
        "index. Original symbols; no drafting-standard conformance is claimed.")
    draw_frame(root, with_keys=False)
    heading = sub(root, "g", (("data-layer", "legend-heading"),))
    sub(heading, "text", {"x": "44", "y": "56", "font-size": "11", "font-weight": "bold", "fill": INK,
                          "letter-spacing": "0.5"}, f"LEGEND — CONVENTIONS USED ON {paper.SHEET_ID}")
    sub(heading, "text", {"x": "44", "y": "69", "font-size": "6.8", "fill": MUTED},
        "Lists only what the sheet draws. Repeated symbols appear once; used-by tags and the index give every "
        "occurrence.")
    entries_layer = sub(root, "g", (("data-layer", "legend-entries"),))
    if not keys:
        sub(entries_layer, "text", {"x": "44", "y": fix(ENTRY_TOP + 10), "font-size": "7.5", "fill": MUTED},
            "No conventions drawn yet.")
    blocks = []
    by_section = {}
    for key in conv.sort_keys(keys):
        by_section.setdefault(conv.CONVENTIONS[key].section, []).append(key)
    for section, title in conv.SECTIONS:
        if section not in by_section:
            continue
        blocks.append(("section", section, title, SECTION_HEIGHT))
        for key in by_section[section]:
            layout = _entry_layout(key, used_by.get(key, []), _text_width(0))
            blocks.append(("entry", key, layout, layout[3]))
    split = _balanced_split(blocks)
    for column, part in enumerate((blocks[:split], blocks[split:])):
        y = ENTRY_TOP
        x0, x1 = COLUMNS[column]
        for kind, key, payload, height in part:
            if y + height > ENTRY_BOTTOM:
                warnings.append({"code": "legend_overflow", "severity": "warning", "entity_ids": [],
                                 "message": f"legend {kind} {key} does not fit on {paper.LEGEND_SHEET_ID}"})
            if kind == "section":
                sub(entries_layer, "text", {"x": fix(x0), "y": fix(y + 8), "font-size": "7.8", "font-weight": "bold",
                                            "fill": INK, "letter-spacing": "0.5", "data-legend-section": key}, payload)
                sub(entries_layer, "line", {"x1": fix(x0), "y1": fix(y + 11), "x2": fix(x1), "y2": fix(y + 11),
                                            "stroke": INK, "stroke-width": "0.6"})
            else:
                _draw_entry(entries_layer, key, COLUMNS[column], y, payload, used_by.get(key, []))
            y += height
    warnings += _draw_index(root, index)
    fields = TitleFields(
        title="LEGEND AND TAGGED ITEM INDEX",
        subtitle=f"FOR {paper.SHEET_ID}: SYMBOLS AND CONVENTIONS USED ON THE SHEET",
        drawing_no=paper.LEGEND_SHEET_ID,
        project=identity.get("project_name") or identity["project_id"],
        revision=str(identity["source_revision"]),
        revision_description=f"GENERATED FROM ENGINEERING MODEL REVISION {identity['source_revision']}",
        status=(f"WORKING PREVIEW {preview[0]}/{preview[1]}" if preview else
                "COMPLETE" if status == "complete" else
                f"PENDING: {open_count} OPEN ITEM{'S' if open_count != 1 else ''}" if status == "pending" else "FAILED"),
        status_pending=status != "complete",
        status_progress=preview is not None,
        drawn=f"GENERATED BY SKID.DRAFTING {PID_VERSION}",
        scale="NONE",
    )
    draw_title_block(root, fields)
    return serialize(root), warnings


def _text_width(column):
    x0, x1 = COLUMNS[column]
    return x1 - x0 - SAMPLE_WIDTH - 10


def _balanced_split(blocks):
    """Index splitting blocks into two columns of similar height, never leaving a section title at a column end."""
    if sum(1 for b in blocks if b[0] == "entry") < 4:
        return len(blocks)
    best = None
    for index in range(1, len(blocks)):
        if blocks[index - 1][0] == "section":
            continue
        left = sum(b[3] for b in blocks[:index])
        right = sum(b[3] for b in blocks[index:])
        score = max(left, right)
        if best is None or score < best[0]:
            best = (score, index)
    return best[1] if best else len(blocks)


def _draw_entry(parent, key, column, y, layout, used_by):
    x0, _ = column
    title, description, used, height = layout
    convention = conv.CONVENTIONS[key]
    attrs = {"data-legend-entry": key, "data-occurrence-id": f"legend/{key}"}
    if used_by:
        attrs["data-used-by"] = " ".join(used_by)
    group = sub(parent, "g", attrs)
    sub(group, "rect", {"x": fix(x0), "y": fix(y + 1), "width": fix(SAMPLE_WIDTH), "height": fix(SAMPLE_HEIGHT),
                        "fill": PAPER, "stroke": RULE, "stroke-width": "0.4"})
    convention.paint(group, x0, y + 1, SAMPLE_WIDTH, SAMPLE_HEIGHT)
    tx = x0 + SAMPLE_WIDTH + 8
    ty = y + 8
    for text in title:
        sub(group, "text", {"x": fix(tx), "y": fix(ty), "font-size": "7.5", "font-weight": "bold", "fill": INK}, text)
        ty += 9.0
    for text in description:
        sub(group, "text", {"x": fix(tx), "y": fix(ty), "font-size": "6.5", "fill": INK}, text)
        ty += 8.0
    for text in used:
        sub(group, "text", {"x": fix(tx), "y": fix(ty), "font-size": "6.2", "fill": MUTED, "data-role": "used-by"}, text)
        ty += 7.6


def _index_rows(index, size):
    widths = (70.0, 196.0, 74.0)
    rows = []
    for item in index["items"]:
        zones = []
        for occurrence in item["occurrences"]:
            zone = occurrence["zone"] or "OUTSIDE SHEET"
            if zone not in zones:
                zones.append(zone)
        tag_lines = wrap(item["tag"], size, widths[0] - 6, True)
        description_lines = wrap(item["description"], size, widths[1] - 6)
        zone_lines = wrap(", ".join(zones), size, widths[2] - 6)
        count = max(len(tag_lines), len(description_lines), len(zone_lines))
        rows.append((item, tag_lines, description_lines, zone_lines, count * size * 1.22 + 4))
    return rows, widths


def _draw_index(root, index):
    layer = sub(root, "g", (("data-layer", "item-index"),))
    sub(layer, "text", {"x": fix(INDEX_LEFT), "y": fix(INDEX_TOP + 12), "font-size": "9.5", "font-weight": "bold",
                        "fill": INK, "letter-spacing": "0.5"}, f"TAGGED ITEM INDEX — {paper.SHEET_ID}")
    sub(layer, "text", {"x": fix(INDEX_LEFT), "y": fix(INDEX_TOP + 23), "font-size": "6.2", "fill": MUTED},
        "Zone references are row letter + column number on the P&ID sheet.")
    size = 7.0
    rows, widths = _index_rows(index, size)
    available = INDEX_BOTTOM - (INDEX_TOP + 44)
    while sum(r[4] for r in rows) > available and size > 5.6:
        size -= 0.2
        rows, widths = _index_rows(index, size)
    warnings = []
    if sum(r[4] for r in rows) > available:
        warnings.append({"code": "index_overflow", "severity": "warning", "entity_ids": [],
                         "message": f"the tagged item index does not fit on {paper.LEGEND_SHEET_ID}; see item-index.json"})
    header_y = INDEX_TOP + 30
    columns = (INDEX_LEFT, INDEX_LEFT + widths[0], INDEX_LEFT + widths[0] + widths[1])
    sub(layer, "rect", {"x": fix(INDEX_LEFT), "y": fix(header_y), "width": fix(INDEX_RIGHT - INDEX_LEFT), "height": "13",
                        "fill": INK})
    for x, label in zip(columns, ("TAG", "ITEM", f"ZONE ON {paper.SHEET_ID}")):
        sub(layer, "text", {"x": fix(x + 3), "y": fix(header_y + 9), "font-size": "6.4", "font-weight": "bold",
                            "fill": PAPER, "letter-spacing": "0.4"}, label)
    y = header_y + 13
    if not rows:
        sub(layer, "text", {"x": fix(INDEX_LEFT + 3), "y": fix(y + 10), "font-size": "7", "fill": MUTED},
            "No tagged items drawn yet.")
    for number, (item, tag_lines, description_lines, zone_lines, height) in enumerate(rows):
        refs = " ".join(o["ref"].replace(" ", "") for o in item["occurrences"])
        group = sub(layer, "g", (("data-index-row", item["semantic_id"]), ("data-semantic-id", item["semantic_id"]),
                                 ("data-occurrence-id", f"index/{item['semantic_id']}"), ("data-tag", item["tag"]),
                                 ("data-refs", refs)))
        if number % 2:
            sub(group, "rect", {"x": fix(INDEX_LEFT), "y": fix(y), "width": fix(INDEX_RIGHT - INDEX_LEFT),
                                "height": fix(height), "fill": "#eef1f5"})
        for x, lines, bold in ((columns[0], tag_lines, True), (columns[1], description_lines, False),
                               (columns[2], zone_lines, True)):
            ty = y + size + 1.5
            for text in lines:
                attrs = {"x": fix(x + 3), "y": fix(ty), "font-size": fix(size), "fill": INK}
                if bold:
                    attrs["font-weight"] = "bold"
                sub(group, "text", attrs, text)
                ty += size * 1.22
        y += height
        sub(layer, "line", {"x1": fix(INDEX_LEFT), "y1": fix(y), "x2": fix(INDEX_RIGHT), "y2": fix(y), "stroke": RULE,
                            "stroke-width": "0.4"})
    sub(layer, "rect", {"x": fix(INDEX_LEFT), "y": fix(header_y), "width": fix(INDEX_RIGHT - INDEX_LEFT),
                        "height": fix(y - header_y), "fill": "none", "stroke": INK, "stroke-width": "0.8"})
    for x in columns[1:]:
        sub(layer, "line", {"x1": fix(x), "y1": fix(header_y), "x2": fix(x), "y2": fix(y), "stroke": RULE,
                            "stroke-width": "0.4"})
    return warnings


def used_by_tags(project, inventory):
    """Key -> current tags of the items whose drawn elements use it, in index order."""
    model = project["model"]
    found = {}
    for use in inventory["uses"]:
        owner = use["semantic_id"] or use["annotation_of"]
        if owner is None:
            continue
        if owner in model["entities"]:
            tag = model["entities"][owner]["tag"]
        elif owner in model["connections"]:
            tag = model["connections"][owner]["tag"]
        elif owner in model["ports"]:
            tag = model["entities"][model["ports"][owner]["entity_id"]]["tag"]
        else:
            continue
        tags = found.setdefault(use["key"], [])
        if tag not in tags:
            tags.append(tag)
    return found
