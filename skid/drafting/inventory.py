"""Drawing inventory and tagged-item index, derived from the emitted P&ID SVG.

The inventory is read back from the serialized sheet, not from the drawing
model: every element carrying ``data-legend-keys`` contributes its keys, and
every group carrying both ``data-semantic-id`` and ``data-occurrence-id`` is a
semantic occurrence. The P&ID never contains legend samples or selection or
progress graphics with those attributes, so they cannot enter the inventory.
The index is built from the drawing model and must agree exactly with the
inventory; any disagreement is an error.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import conventions as conv
from . import paper
from .definitions import definition_identity, definition_name, is_composed
from .symbols import FALLBACK, symbol_for_entity

SCHEMA_VERSION = 1
INDEXED_KINDS = ("symbol", "edge", "observation")
CATEGORY_ORDER = {"equipment": 0, "valve": 1, "instrument": 2, "boundary": 3, "line": 4}


def _error(code, ids, message):
    return {"code": code, "severity": "error", "entity_ids": [i for i in dict.fromkeys(ids) if i], "message": message}


def parse_inventory(svg_text: str) -> dict:
    """Read the legend-key uses and semantic occurrences that an SVG sheet actually emits."""
    root = ET.fromstring(svg_text.encode("utf-8"))
    uses, occurrences, samples = [], [], 0
    seen_uses = set()

    def walk(element, owner, annotation):
        nonlocal samples
        if element.get("data-occurrence-id") is not None and element.get("data-semantic-id") is not None:
            owner = {
                "semantic_id": element.get("data-semantic-id"),
                "semantic_kind": element.get("data-semantic-kind"),
                "occurrence_id": element.get("data-occurrence-id"),
                "occurrence_kind": element.get("data-occurrence-kind"),
            }
            record = dict(owner)
            for name in ("data-tag", "data-paper-x", "data-paper-y", "data-zone", "data-zones", "data-ref"):
                if element.get(name) is not None:
                    record[name[5:].replace("-", "_")] = element.get(name)
            occurrences.append(record)
        if element.get("data-annotation-of") is not None:
            annotation = element.get("data-annotation-of")
        if element.get("data-legend-sample") is not None:
            samples += 1
        for key in (element.get("data-legend-keys") or "").split():
            use = (key, owner["semantic_id"] if owner else None, owner["occurrence_id"] if owner else None, annotation)
            if use not in seen_uses:
                seen_uses.add(use)
                uses.append({"key": key, "semantic_id": use[1], "occurrence_id": use[2], "annotation_of": annotation})
        for child in element:
            walk(child, owner, annotation)

    walk(root, None, None)
    keys = conv.sort_keys({use["key"] for use in uses})
    tagged = sorted({o["semantic_id"] for o in occurrences
                     if o["occurrence_kind"] in INDEXED_KINDS and o["semantic_kind"] in ("entity", "connection")})
    return {
        "schema_version": SCHEMA_VERSION,
        "sheet_id": root.get("data-sheet-id"),
        "project_id": root.get("data-project-id"),
        "source_revision": int(root.get("data-source-revision")) if root.get("data-source-revision", "").isdigit() else None,
        "input_digest": root.get("data-input-digest"),
        "source": "pid.svg",
        "keys": keys,
        "uses": uses,
        "occurrences": occurrences,
        "tagged_ids": tagged,
        "legend_samples_found": samples,
        "excluded": "Legend samples, selection highlighting and progress graphics are not inventory sources; "
        "the P&ID emits none of them with legend keys or semantic occurrence attributes.",
        "paper": paper.paper_record(),
    }


def item_description(project, semantic_id, graph):
    model = project["model"]
    if semantic_id in model["connections"]:
        connection = model["connections"][semantic_id]
        source, target = graph.ends[semantic_id]
        return (f"LINE {model['entities'][source]['tag']} TO {model['entities'][target]['tag']}, "
                f"DN{_dn(connection['nominal_diameter_mm'])}")
    entity = model["entities"][semantic_id]
    spec = symbol_for_entity(entity) or FALLBACK
    if is_composed(entity):
        return f"{definition_name(entity).upper()} (COMPOSED EQUIPMENT)"
    if entity["kind"] == "indicator" and isinstance(entity.get("observes"), dict):
        subject = model["entities"].get(entity["observes"]["entity_id"], {}).get("tag", entity["observes"]["entity_id"])
        return f"INDICATOR: {entity['observes']['variable'].replace('_', ' ').upper()} OF {subject}"
    return spec.title


def _dn(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def build_index(project, sheet, inventory) -> dict:
    """Tagged-item index from the drawing model, one entry per represented tagged item."""
    model = project["model"]
    keys_by_id = {}
    for use in inventory["uses"]:
        if use["semantic_id"] is not None:
            keys_by_id.setdefault(use["semantic_id"], set()).add(use["key"])
        if use["annotation_of"] is not None:
            keys_by_id.setdefault(use["annotation_of"], set()).add(use["key"])
    by_id = {}
    for record in sheet.occurrence_records():
        by_id.setdefault(record["semantic_id"], []).append(record)
    items = []
    for semantic_id, records in by_id.items():
        if semantic_id in model["connections"]:
            item_kind, kind, category = "connection", "process_line", "line"
            rank = sheet.graph.connection_rank.get(semantic_id, len(sheet.graph.steps))
        else:
            entity = model["entities"][semantic_id]
            item_kind, kind = "entity", entity["kind"]
            category = (symbol_for_entity(entity) or FALLBACK).category
            rank = sheet.graph.entity_order.index(semantic_id)
        record = model["connections"].get(semantic_id) or model["entities"][semantic_id]
        occurrences = []
        for occurrence in records:
            occurrences.append({
                "occurrence_id": occurrence["occurrence_id"],
                "occurrence_kind": occurrence["occurrence_kind"],
                "sheet_id": paper.SHEET_ID,
                "zone": occurrence["zone"],
                "ref": paper.zone_ref(occurrence["zone"]),
                "position": occurrence["position"],
                "zones": occurrence["zones"],
            })
        item = {
            "semantic_id": semantic_id,
            "item_kind": item_kind,
            "kind": kind,
            "category": category,
            "tag": record["tag"],
            "description": item_description(project, semantic_id, sheet.graph),
            "legend_keys": conv.sort_keys(keys_by_id.get(semantic_id, ())),
            "occurrences": occurrences,
            "_rank": (CATEGORY_ORDER.get(category, 9), rank, semantic_id),
        }
        if item_kind == "entity" and is_composed(record):
            item["definition"] = definition_identity(record)
        items.append(item)
    items.sort(key=lambda item: item.pop("_rank"))
    return {
        "schema_version": SCHEMA_VERSION,
        "sheet_id": paper.SHEET_ID,
        "legend_sheet_id": paper.LEGEND_SHEET_ID,
        "project_id": project["id"],
        "source_revision": project["revision"],
        "items": items,
        "paper": paper.paper_record(),
    }


def verify_drawing(inventory, index) -> list[dict]:
    """Errors unless the inventory and index agree exactly with each other and with the zone contract."""
    problems = []
    if inventory["legend_samples_found"]:
        problems.append(_error("inventory_contains_legend_samples", [], "the P&ID contains legend samples"))
    unknown = [k for k in inventory["keys"] if k not in conv.CONVENTIONS]
    if unknown:
        problems.append(_error("legend_key_unknown", [], f"drawn legend keys have no legend entry: {', '.join(unknown)}"))
    index_ids = {item["semantic_id"] for item in index["items"]}
    drawn_ids = set(inventory["tagged_ids"])
    if index_ids != drawn_ids:
        missing, surplus = sorted(drawn_ids - index_ids), sorted(index_ids - drawn_ids)
        problems.append(_error("index_mismatch", missing + surplus, f"item index differs from the drawn tagged items "
                               f"(missing {missing}, surplus {surplus})"))
    drawn = {}
    for occurrence in inventory["occurrences"]:
        if occurrence["occurrence_kind"] in INDEXED_KINDS:
            drawn.setdefault(occurrence["semantic_id"], {})[occurrence["occurrence_id"]] = occurrence
    for item in index["items"]:
        emitted = drawn.get(item["semantic_id"], {})
        listed = {o["occurrence_id"]: o for o in item["occurrences"]}
        if set(listed) != set(emitted):
            problems.append(_error("index_occurrence_mismatch", [item["semantic_id"]], f"index occurrences of "
                                   f"{item['tag']} {sorted(listed)} differ from the drawing {sorted(emitted)}"))
            continue
        for occurrence_id, entry in listed.items():
            svg = emitted[occurrence_id]
            px, py = float(svg["paper_x"]), float(svg["paper_y"])
            zone = paper.zone_of(px, py)
            if (svg.get("zone") or None) != zone or entry["zone"] != zone or [px, py] != list(entry["position"]):
                problems.append(_error("zone_mismatch", [item["semantic_id"], occurrence_id], f"zone of {occurrence_id} "
                                       f"is inconsistent: drawing {svg.get('zone')!r}, index {entry['zone']!r}, "
                                       f"paper position gives {zone!r}"))
    return problems


def parse_legend(svg_text: str) -> dict:
    """Read the entries, samples and index rows a legend sheet actually emits."""
    root = ET.fromstring(svg_text.encode("utf-8"))
    entries, rows, samples, stray = [], [], [], 0
    for element in root.iter():
        if element.get("data-legend-entry") is not None:
            entries.append(element.get("data-legend-entry"))
        if element.get("data-index-row") is not None:
            rows.append(element.get("data-semantic-id"))
        if element.get("data-legend-sample") is not None:
            samples.append(element.get("data-legend-sample"))
        if element.get("data-legend-keys") is not None:
            stray += 1
    return {"entries": entries, "index_rows": rows, "samples": samples, "legend_key_attributes": stray}


def verify_legend(legend, inventory, index) -> list[dict]:
    problems = []
    entries = legend["entries"]
    if len(entries) != len(set(entries)):
        problems.append(_error("legend_duplicate_entry", [], "the legend lists a key more than once"))
    if set(entries) != set(inventory["keys"]):
        missing = sorted(set(inventory["keys"]) - set(entries))
        surplus = sorted(set(entries) - set(inventory["keys"]))
        problems.append(_error("legend_mismatch", [], f"legend entries differ from the drawn inventory "
                               f"(missing {missing}, surplus {surplus})"))
    if sorted(set(legend["samples"])) != sorted(set(entries)):
        problems.append(_error("legend_sample_mismatch", [], "legend samples do not correspond one-to-one to entries"))
    if legend["legend_key_attributes"]:
        problems.append(_error("legend_recursive_keys", [], "legend sheet elements carry drawing legend keys"))
    rows = legend["index_rows"]
    if len(rows) != len(set(rows)) or set(rows) != {item["semantic_id"] for item in index["items"]}:
        problems.append(_error("legend_index_mismatch", [], "legend index rows differ from the item index"))
    return problems
