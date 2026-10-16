# CAD and 3D display geometry (`skid.geometry`)

Current guide to the generator that builds CadQuery/OCCT solids, a verified STEP assembly and the tessellated 3D display scene from **one** parameterized definition per item or route. (The file name records that this began as a CAD spike; the module is now the production geometry generator for the five-output workflow.) Contract: [workflow contracts](WORKFLOW_CONTRACTS.md#shared-geometry-conventions). Requires the CAD requirements ([getting started](GETTING_STARTED.md#4-interactive-five-output-workspace)). Source is authoritative.

## Interface and CLI

```python
from skid.geometry import build_geometry, rebuild_from_recipe
result = build_geometry(project, output_dir, emit=None, cancelled=None)   # {"display": manifest, "cad": manifest}
```

```sh
python -m skid.geometry build PROJECT.json OUT_DIR             # exit 0 for complete AND pending; read the statuses
python -m skid.geometry rebuild OUT_DIR/geometry.recipe.json NEW_DIR   # replay an unchanged snapshot; edited ones exit 2
python -m skid.geometry recipe examples/recipes/horizontal_drum.recipe.json OUT_DIR --set shell_length=1.6
```

Writes `preview/display-NNNN.json` (immutable cumulative snapshots), `scene.json`, `display.manifest.json`, `geometry.recipe.json`, `model.step`, `cad.verification.json`, `cad.manifest.json`. The standalone CLI publishes nothing and compares nothing with a source ([capabilities](CAPABILITIES.md#three-ways-to-generate-outputs)).

| Module | Role |
| --- | --- |
| `kernel.py` | OCP primitives, swept pipes with tangent bends, measurement, tessellation |
| `parts.py` | Closed, validated part vocabulary and composites (`flange`, `flanged_nozzle`, `bolting`, `volute_casing`, `finned_frame`, `handwheel`, …) |
| `recipes.py` | Component compiler, port-face proof, authored-recipe schema |
| `equipment.py`, `routes.py` | Detailed recipes for the six built-in kinds and for current/pending routes; `RECIPE_ASSUMPTIONS`; `SUPPORTED_RANGES_M` |
| `composed.py` | Composed equipment via `skid.equipment.resolve_definition` (no per-type handler) |
| `step.py` | Named, coloured millimetre STEP assembly writer and reader |
| `snapshot.py`, `build.py`, `authored.py` | Snapshot digest and replay checks; progressive build, export and verification; authored recipes |

## Conventions

- **Units and frames.** Kernel and STEP in millimetres (checked in the reimported file). Scene in world metres, right-handed, +Z up; vertices already in world coordinates. Placement rotation `R = Rz·Ry·Rx`, matching `skid.templates`.
- **Identity.** Component ID `<semantic_id>#<component_key>` with fixed keys per kind (for example pump `body`, `motor`, `nozzle/inlet`; routes `pipe`, `flange/from`, `pending/path`). STEP tree: project root → group per entity or connection → part per component, named by component ID; the header carries project, revision and input digest.
- **Ports.** Taken from the templates or resolved definitions and never moved. Each nozzle flange face is proven on the port plane in the source shapes, the reimported STEP and the display mesh.
- **Verification.** STEP is reimported and checked for valid solids, counts, bounds, placements, port faces and (for built-ins) envelope checks against the declared dimensions. Reimport failure is failed CAD; no closed-mesh substitute is used.
- **Status.** `complete` only without warnings. Unplaced equipment is omitted (`geometry_omitted`); a non-current route becomes a red display-only `pending/path` (`route_pending`); both make display and CAD pending. Display-only components are excluded from STEP. Validation errors or recipe faults give `failed` with no `scene.json` or `model.step`. Values outside `SUPPORTED_RANGES_M` fail the build explicitly.
- **Events.** Activity before each unit; a preview after each immutable snapshot (equipment in flow order, then routes) with real counts; CAD activity for export and reimport; the `model.step` artifact event only after verification. `cancelled()` is checked between units; STEP export and verification run uninterrupted.

## Appearance and assumptions

The recipes are original OCCT construction: recognizable end-suction pump with volute, bearing frame, coupling guard, finned motor and baseplate; flanged valves (isolation valves with a handwheel so no open/closed position is implied; the check valve with a flow arrow); blank indicator dials; routes as single swept solids with 1.5 × DN bends and flanges at each joint. External reference material was read for technique only; no code or assets were copied.

Every added detail cites a recipe assumption (`rcp-*`, scope `recipe`) beside the project assumptions for dimensions and placement. Colours, fin counts, bolt sizes, handwheels and flange proportions (OD 150 mm, resembling DN50 practice, no rating or standard) are appearance-only. Pipes and vessels are solid outside-diameter envelopes (wall thickness unknown); casing internals and supports are absent.

For the unchanged reference fixture, `scripts/reference_expected.json` records the expected counts that the reference script rechecks (67 STEP parts, 350 solids, 68 display components, 10 port flange faces, 12 envelope checks). They are expectations, not recorded results ([reference example](REFERENCE_EXAMPLE.md#reference-script)).

## Snapshot provenance

`geometry.recipe.json` (schema `skid.geometry.snapshot-recipe/2`) is the normalized record of what a build compiled: source identity, generator, conventions, tessellation, every unit's parameters, placement matrix, port frames, envelope checks, part calls, mappings, assumptions and materials. It carries `recipe_sha256` over all its other content, distinct from the project `input_digest`.

`rebuild` checks the digest and exact compatibility (generator version, conventions, materials, assumptions, structure) before building anything; any mismatch is `SnapshotError` with an empty output. Outputs are stamped `provenance.kind` `project_build`, `authored_recipe` or `snapshot_rebuild`. The digest detects accidental change; it is **not** a signature, and a deliberately re-hashed consistent edit will replay under its own new digest. Design changes go through validated project commands or an authored recipe, not snapshot edits.

## Limits

- Illustrative prototype geometry; a valid STEP establishes neither fabrication readiness nor vendor accuracy. DN50 flanges only.
- STEP bytes differ between identical builds (kernel metadata); compare structure and geometry.
- OCCT global settings make builds process-global: one build per process at a time, as the service does.
- Not modelled: supports, gaskets, internal passages, general equipment generation.
