# Workflow contracts: five coordinated outputs (version 0.2)

Current summary of the interface between the engineering document, the two generators and the interactive service. It extends the [core contracts](CONTRACTS.md) without changing the schema-1 document or the offline path. Module details: [CAD geometry](CAD_SPIKE.md), [drafting](DRAFTING.md), [service](INTERACTIVE.md), [UI](UI.md). Source is authoritative.

## Generators

```python
skid.geometry.build_geometry(project, output_dir, emit=None, cancelled=None)
    -> {"display": manifest, "cad": manifest}
skid.drafting.build_drafting(project, output_dir, emit=None, cancelled=None, manual_notes=None)
    -> {"pid": manifest, "legend": manifest, "description": manifest}
```

- Input is a read-only snapshot; `output_dir` already exists. Both may write into the same build directory.
- Manifests keep the 0.1 common fields (`adapter, adapter_version, project_id, source_revision, input_digest, status, diagnostics, representations`) plus `files` (relative paths). Adapter names: `display`, `cad`, `pid`, `legend`, `description`; `adapter_version` is `0.2.0` (the service's integrity check accepts only that value).
- `status` is `complete` only with no warnings; open items give `pending`; errors give `failed`.
- `emit(event)` receives `{kind: activity|preview|artifact, artifact, message, path?, completed?, total?}`. Every `path` exists before the callback. Counts are real units of work; there are no synthetic percentages or delays.
- `cancelled()` is checked between units of work; `True` raises `InterruptedError`. Progressive previews (`preview/display-NNNN.json`, `preview/pid-NNNN.svg`, `preview/legend-NNNN.svg`) are immutable; final files are written only when ready.

Files per output: 3D display `scene.json`; CAD `model.step`, `geometry.recipe.json`, `cad.verification.json`; P&ID `pid.svg`, `drawing.inventory.json`; legend `legend.svg`, `item-index.json`; description `description.json`, `description.html`. Each has `<adapter>.manifest.json`.

## Shared geometry conventions

- Scene: `{schema_version: 1, project_id, source_revision, input_digest, units: "m", up_axis: "Z", components, ports, assumptions, bounds_m}`. Vertices are world metres, right-handed, Z up; the viewer applies no conversion.
- Component IDs are `<semantic_id>#<component_key>`; tags never enter IDs. Scene ports carry world position and outward unit direction.
- CAD solids are built in millimetres, exported to STEP, reimported and verified (valid solids, counts, bounds, port faces, envelopes) before CAD counts as complete. STEP bytes can differ between unchanged builds; compare geometry and mappings, not bytes.
- Retained pending routes are red display-only components, excluded from STEP. Display-only items are recorded separately from engineering geometry.
- A semantically valid edit outside a recipe's supported range produces an explicit failed build, never silent success.

## Sheet contract

One landscape sheet `P&ID-001`, 1200 × 840 drawing units, inner border x 30–1170, y 30–810; columns 1–10 and rows A–I with A at the bottom. Stored diagram coordinates map to paper by x + 100, y + 100, with no auto-fit after edits. Zone lookup uses the exported binary64 grid boundaries with right/below ownership; out-of-sheet positions are diagnostics, not clamped. The legend lists exactly the keys drawn; the item index lists exactly the drawn tagged items. Original artwork only; no standards conformance is claimed.

## Publication

- The service builds in a separate worker process. One build runs and at most one is queued; a newer revision supersedes older work.
- A build publishes only when all five outputs are `complete` for the same project, revision and digest, every file verifies from disk, and the source rereads unchanged under the document lock. Then `<stem>.workflow/current.json` is replaced atomically.
- Failed, pending, cancelled, superseded and source-changed builds never move the pointer. Pending builds remain inspectable as labelled working previews.
- The published set is rechecked on start-up, reopen and pointer or file changes. A tampered set makes `sync.state` `invalid` and is withdrawn from display.
- 0.1 two-view synchronization is never reused as five-output completion.

Pointer fields, build directory layout and retention: [reference example](REFERENCE_EXAMPLE.md#output-files).

## Service transport and identity rules

Loopback-only HTTP JSON API (`skid.interactive`); full endpoint list in [service](INTERACTIVE.md). Selection is transient (`{sequence, semantic_ids, origin}`) and never changes the revision. In selection results a display mapping of `null` means unknown; only an explicit `[]` means a verified absence (for example a conceptual indicator with no 3D body).

A view's badge describes the content actually installed in it, not the newest file requested. Only content whose identity matches the verified current publication may be labelled current. `synchronized` means the verified five-output publication matches the current revision and digest and the manual-notes sidecar is readable; `blocked` means the sidecar exists but cannot be read.

The geometry snapshot `geometry.recipe.json` carries its own content digest (`recipe_sha256`), distinct from the project `input_digest`. Standalone rebuilds check it before generating. It detects changes; it is not an authenticity signature.
