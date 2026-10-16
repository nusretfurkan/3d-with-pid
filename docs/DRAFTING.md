# Drafting: P&ID sheet, exact legend, item index and process description

Current guide to `skid/drafting/` (standard library only). Contract context: [workflow contracts](WORKFLOW_CONTRACTS.md#sheet-contract). The narrative part of the description has its own page: [process narrative](PROCESS_NARRATIVE.md). Source is authoritative.

## Interface

```python
from skid.drafting import build_drafting
manifests = build_drafting(project, output_dir, emit=None, cancelled=None, manual_notes=None)
# {"pid": manifest, "legend": manifest, "description": manifest}
```

```sh
python -m skid.drafting --project PATH --out DIR [--notes NOTES.json] [--quiet]   # exit 0 complete, 2 pending, 1 failed
```

- The project is copied and validated with the real core validator; the input is never mutated.
- `output_dir` must exist and may be shared with the geometry generator. An existing drafting output or preview raises `FileExistsError`, so previews stay immutable.
- Files: `pid.svg`, `drawing.inventory.json`, `pid.manifest.json`; `legend.svg`, `item-index.json`, `legend.manifest.json`; `description.json`, `description.html`, `description.manifest.json`; progressive `preview/pid-NNNN.svg` and `preview/legend-NNNN.svg`. Final files are written atomically.
- Status: structural errors fail all three outputs. Any warning makes the affected output `pending`: model open items affect all three; sheet findings (for example `out_of_sheet`, `title_block_overlap`, `pid_symbol_missing`, `pid_anchor_unmatched`) affect P&ID and legend; manual-note conflicts affect the description. A disagreement between drawing, inventory, index and legend is an error that fails the build before anything final is written.

## Sheet P&ID-001

- Landscape 1200 × 840 units; inner border x 30–1170, y 30–810; ten 114-unit columns 1–10; nine rows lettered I (top) to A (bottom).
- Stored diagram coordinates map to paper by x + 100, y + 100. Nothing is refitted after edits; stored symbol positions, rotations, anchors and manual edge points are drawn exactly. Only annotation is laid out.
- Zone lookup bisects the canonical binary64 grid boundaries, exported in the inventory and index. A point on an internal line belongs to the cell to its right or below. Out-of-sheet positions get `zone: null` and an `out_of_sheet` warning, never clamping.
- References read `P&ID-001 / E2`. Symbols use their insertion point; edges and observation relations use the midpoint of the drawn polyline (edges also list every zone crossed). Diagram moves change zones; physical moves and viewer navigation cannot.

## Conventions

Original artwork (`skid/drafting/symbols.py`); no ISA-5.1, ISO 10628 or company-standard conformance is claimed.

- Isolation valves are drawn with no operator; operator, normal position and fail action are not modelled. Check valves show the permitted direction. Indicators are bubbles linked to their subject by dotted blue observation relations that never end on a port anchor.
- Line designations such as `L-101 DN50`, one flow arrow per line, and a knockout gap where lines cross without connecting. There are no junction dots, because branches need junction entities that do not exist.
- Pending state is visible: needs-reroute or unresolved paths keep their stored points in red with a badge; 3D placement or route pending shows red badges; the title block shows `PENDING: n OPEN ITEMS`.
- Title block: project (display name, else ID), drawing number, revision, status, `NOT CHECKED`, `NOT APPROVED`. Subtitle `TK-101 TO OUT-101 VIA P-101` style only for one connected flow with no unconnected process item and no declared separate paths; otherwise `PROCESS SCHEMATIC`.
- Digests, IDs and raw parameter names stay out of visible text; they are in SVG attributes, manifests, inventory and the description footer.

## Exact legend and index

Every drawn convention carries a stable key in `data-legend-keys` (vocabulary in `skid/drafting/conventions.py` and `symbols.py`; composed families use `symbol/<shape>`). The inventory is parsed back from the serialized `pid.svg`. The legend lists exactly those keys once each, with original samples that carry no drawing keys. `item-index.json` lists every drawn tagged entity and connection with every occurrence's sheet, zone, reference and paper position. Before publication the build checks key equality, sample correspondence, index/inventory ID equality and recomputed zones. Multi-sheet drawings are not implemented; legend overflow is reported as a warning.

## Process description

`description.json` holds `{schema_version, adapter, adapter_version, project_id, source_revision, input_digest, status, title, sheet_id, legend_sheet_id, basis, purpose, flow_sequence, items, assumptions, unknowns, open_items, manual_notes, conflicts}` plus additive fields such as `process_narrative`, `definition_assumptions` and `project_name`. `description.html` is a self-contained page with inline CSS, no scripts and no external references.

- Flow order comes from port connectivity only (with declared `flow_paths`), never from tags or coordinates. Text parts reference stable IDs and resolve to current tags.
- Each item has a role derived from kind and connectivity, labelled not verified operating behaviour, plus evidence, assumptions, unknowns and a fingerprint.
- Unknowns are explicit: process conditions, materials and ratings, valve operation, pump duty, instrumentation, control and safety functions (none modelled). Nothing is invented.
- Composed items quote their declared name, function and unknowns as authored, not verified, and list current parameters with provenance.

## Manual notes sidecar

`{"schema_version": 1, "project_id": ..., "notes": [{id, target_id, text, reviewed_revision, reviewed_fingerprint?, author?}]}`. Notes are copied verbatim, never rewritten, and each gets a `state`: `current`, `stale_review`, `orphan` or `invalid`. Malformed, duplicate or foreign notes are conflicts that keep the description pending. The interactive service reads `<project stem>.notes.json` and never edits it.

## Previews and cancellation

After each real drawing unit (frame, each symbol, each edge, each relation, labels, data and title block), the current sheet and a legend from that preview's own inventory are written as immutable previews before their events. There are no timed reveals. `cancelled()` is checked before each unit; already written previews stay, and no final file or manifest is written.

## Limits

Text widths are font estimates; label placement is deterministic candidate scoring, and remaining collisions are listed in `layout_findings`. Equipment data blocks exist for tank, pump and composed items only. Collinear overlaps or lines through symbols come from stored geometry and are not repaired. Composed outlines do not show internals. Authored definition text is not tag-resolved, so a tag typed into it is not updated by a rename.
