# Core contracts: document, commands and offline adapters

Current summary of the engineering core and the original offline two-view path (contract version 0.1, still in force and extended compatibly by later versions). The field-level reference, with runnable examples, is [input contracts](INPUT_CONTRACTS.md). If this page and the source disagree, the source is authoritative.

## Architecture

- One authoritative schema-1 JSON document. Every output derives from it; no adapter writes engineering data back.
- Three separate concerns: `model` (engineering semantics: entities, ports, connections, assumptions), `pid` (diagram presentation: symbol occurrences, edges), `physical` (placements, routes).
- Permanent opaque IDs, supplied when a record is created and never derived from tags. Tags are editable labels, unique across equipment and lines after whitespace collapsing and case folding.
- Connectivity is by port identity. Touching meshes or crossing diagram lines mean nothing. Each process port takes at most one connection; branches would need junction entities, which do not exist.
- Indicators are conceptual `observes` relations, drawn distinctly from process piping. They are not instrument hookups or control loops.
- Open work is explicit state: unplaced equipment, `needs_reroute`/`unresolved` routes and edges, and deliberately unconnected required ports keep outputs *pending*. Nothing is silently resolved.

## Core module interface (`skid.core`)

```python
validate_project(project) -> list[diagnostic]            # also validate_document(document)
apply_command(document, command) -> (new_document, diagnostics)
load_document(path) -> document                           # strict: DocumentError on malformed input
save_document(path, document, expected_disk_revision)     # None = create; RevisionConflict on mismatch
new_document(project_id, name=None) -> document           # genuinely empty revision 0
canonical_digest(project) -> str                          # SHA-256 identity of project content
```

- `apply_command` works on a copy, validates the whole result and raises `CommandError` (with diagnostics) on rejection; inputs are never mutated. Errors are defined in `skid/core/errors.py`.
- `save_document` writes a same-directory temporary file and replaces atomically under a POSIX file lock, rereading the disk revision and project ID first. A conflicting save is rejected, never rebased.
- Diagnostics are `{code, severity: error|warning, entity_ids, message}`. Errors block commits; warnings are open items that keep outputs pending. Validation does not establish hydraulic, safety or fabrication adequacy.

Command types, payloads, revision compare-and-swap, undo/redo as new revisions and the placement/routing invalidation table are in [input contracts](INPUT_CONTRACTS.md#commands). Composed equipment and `batch` are in [extension contracts](EXTENSION_CONTRACTS.md); `add_equipment`/`connect_ports` in [empty start](EMPTY_START.md).

## Templates (`skid.templates`)

```python
get_template(kind, version) -> dict            # built-in templates, version 1
entity_template(entity) -> dict                # built-in or composed (generic lookup)
local_port_position(project, port_id) / world_port_position(project, port_id)
diagram_port_position(project, port_id, symbol_id)
entity_primitives(entity) -> list[dict]        # box | cylinder | sphere primitives, metres
```

Built-in kinds: `tank`, `centrifugal_pump`, `isolation_valve`, `check_valve`, `indicator`, `boundary`. Each declares parameter bounds, named ports with local frames, diagram anchors and simple illustrative primitives. Only DN50 liquid ports are supported; other sizes are rejected rather than drawn wrongly. Template versions never change implicitly on load. The template package imports nothing else from `skid`.

Units and frames: geometry in positive metres; angles in degrees; physical space right-handed, +z up, rotation order X then Y then Z; a current route ends on the transformed port positions within 1e-6 m; diagram units +x right, +y down.

## Offline two-view adapters (version 0.1)

```python
skid.adapters.pid.render_pid(project, selected_ids=()) -> (svg_text, manifest)       # pure
skid.adapters.blender.build_blender(project, blend_path, selected_ids=()) -> manifest # inside Blender
```

Manifests carry `{adapter, adapter_version, project_id, source_revision, input_digest, status: complete|pending|failed, diagnostics, representations}`, where `representations` maps semantic IDs to occurrence or object identifiers. SVG elements carry semantic and occurrence IDs independent of labels. Blender objects carry project, semantic ID, stable `component_key` and source revision as custom properties; only the project-owned collection is rebuilt. Stale routes are omitted or visibly marked.

`python -m skid.cli build` stages both outputs and publishes `<stem>.skid/bundles/r<revision>-<key>/` by atomically replacing `<stem>.skid/current.json`; `status` compares that pointer with the source. Exit codes and the pointer are described in [reference example](REFERENCE_EXAMPLE.md#output-files). A failure keeps the last published bundle. Composed equipment appears only as an explicit placeholder or omission and keeps the bundle pending.

This offline bundle is one of three generation paths with different meanings of success; it is not evidence for the five-output workspace ([capabilities](CAPABILITIES.md#three-ways-to-generate-outputs), [workflow contracts](WORKFLOW_CONTRACTS.md)). The Blender adapter runs inside Blender's Python API; its distribution terms are an open owner decision ([third-party notices](../THIRD_PARTY_NOTICES.md)).
