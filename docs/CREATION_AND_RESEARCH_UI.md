# Equipment creation and research: planner, service and UI

How engineering plans create equipment from a blank model, how the optional research stage runs in the service, and how both appear in the workspace. Core commands: [empty start](EMPTY_START.md). Researcher: [equipment research](EQUIPMENT_RESEARCH.md). Planner bridge and proposal record: [planning](PLANNING.md). Source is authoritative (`skid/planning/`, `skid/interactive/`, `web/js/`).

## Planner operations

### `add_equipment`

```json
{"op": "add_equipment", "purpose": "...", "args": {
  "entity_id": "ent-<stem>", "tag": "TK-101",
  "kind": "tank | centrifugal_pump | isolation_valve | check_valve | boundary | composed_equipment",
  "definition_ref": "", "definition_json": "",
  "parameters": [{"name": "diameter", "value": 0.8, "unit": "m|mm", "assumption": "<key or existing ID>"}],
  "port_assumption": "<key or existing ID>",
  "symbol": {"x": 120, "y": 280, "rotation_deg": 0},
  "placement": {"status": "placed|unresolved", "position_m": [], "rotation_deg": [], "assumption": "..."}}}
```

- `indicator` is deliberately not addable by plans, so new instruments stay out of scope. A composed item needs exactly one of `definition_ref` (`example:<name>` or `entity:<id>`) or `definition_json`, validated before any batch is formed. Any declared port set is allowed.
- Derived IDs: `sym-<stem>` and one `port-<stem>-<suffix>` per declared port, the suffix being the port name with `_` replaced by `-` (`tube_inlet` → `port-hx101-tube-inlet`). Collisions are refused.
- Every declared parameter is required, converted to metres and cited. Ports copy the declared direction, required flag, DN and service; required ports carry `connection_status: "unresolved"` until connected. The P&ID symbol is `manual: true`.

### `connect_ports`

```json
{"op": "connect_ports", "purpose": "...", "args": {
  "connection_id": "conn-<stem>", "tag": "L-101",
  "from_port_id": "port-...", "to_port_id": "port-...",
  "from_symbol_id": "sym-...", "to_symbol_id": "sym-...",
  "outside_diameter_mm": 60.3, "assumption": "<key or existing ID>",
  "route": {"mode": "none | guided | points", "points_m": [], "assumption": ""}}}
```

- Ports are joined only by the named IDs. Translation pre-checks direction, DN, service, free capacity (no branches) and symbol ownership; the core checks again.
- The planner also requires the two items to differ. That is stricter than the core, which refuses only a port connected to itself; a line between two different ports of one item is valid in the core but is not offered by plans.
- The P&ID edge `edge-<stem>` always uses the core's guided path, or is stored `unresolved` if an anchor cannot be resolved.
- `route.mode`: `points` is an explicit manual route (≥ 2 points plus an assumption, checked by the core); `none` is explicitly unresolved; `guided` is computed after translation by the same bounded helper as the Reroute button (`skid.interactive.rerouting.plan_reroute`) on the final dry-run state, with a service-written assumption. It stays unresolved, with a reason and never invented points, when an endpoint is unplaced, the router refuses, a later operation changes an endpoint or the line, or the second dry run rejects it.

### Research citations

Plan assumptions carry `source_ids` (`[]` without research, at most 8). With a verified research record, unknown or duplicate IDs, or citations without research, reject the plan (`plan_unknown_source`). Each recorded assumption's `source` is suffixed with the research identity (`Research <res-id> (skid.equipment.research/1, brief sha256 …, n web source(s), text search only)`) and the cited URLs, or states that no research source is cited for that value. These strings live in the model's assumption records and survive save, reopen, undo/redo and rebuilt descriptions.

Only the plan's own recorded assumptions can carry references. A researched plan that records none (for example a rename alone) applies normally, but nothing saved cites the research; the brief exists only in the proposal, which expires after one hour. A ready researched proposal states this before Apply in `research.persistence`: `saved_with_assumptions` with the assumption IDs, or `proposal_only` with a note.

## Planner context additions

- `creation.builtin`: each addable kind's parameters and bounds, reference values read from `examples/pump_skid.json`, local body extent, and per port its direction, required flag, DN, service, ID suffix, P&ID anchor, outward direction and position formula. All are derived from the template code and re-checked by a test. Present even for an empty model.
- `creation.line_conventions` (DN50, 60.3 mm outside-diameter basis), route modes and short arrangement notes; `composition.example_port_frames` for each example definition; existing ports' world outward direction and `connection_status`.
- `capabilities` lists unsupported requests (branches, proximity joining, adding instruments, design or sizing claims) and states that this is an arrangement tool, not a process, mechanical, safety or fabrication design tool.
- `research`: `null`, or the verified brief marked untrusted.

## Research in the service

- `POST /api/propose` accepts an optional strict boolean `research` (default false), allowed only with `mode: "engineering"` (otherwise 400 `research_mode`). Ask refuses the field.
- `InteractiveService(..., researcher=None)` builds the default researcher; `False` disables it. The launcher passes `--planner-cli` to the researcher and offers `--research-timeout S` and `--no-research`.
- Stages share one plan slot, on one frozen snapshot, with no lock held: **researching** (only the cleaned request text is sent) → re-validation of the reply (no exemption for injected researchers) → source check (a changed document makes the proposal stale and planning is not started) → **planning** with the brief as untrusted context → the usual final stale check.
- `state.planning.active[]` reports `stage`, `stage_elapsed_s` and completed `stages`; `state.planning.research` reports availability; `state.capabilities` adds `equipment_creation` and `equipment_research`.
- One cancel serves both stages; shutdown stops both. A research error, cancellation, missing researcher or rejected brief returns a refusal with `commands: []` and no Apply. There is no source-free fallback.

## Change / Engineering plan UI

- **Research equipment** is one checkbox in the Engineering plan composer, shown only when the running service advertises `capabilities.equipment_research` and research is available. The page sends `research: true` only when ticked and never sends `research: false`, so older services that reject unknown fields keep working.
- Progress shows *Researching* (request text only) then *Planning*, with elapsed time and Cancel plan.
- The review card shows equipment type, summary, numbered sources with observations, sourced features, research assumptions (illustrative, not from sources), unknowns, the text-evidence warning, the research identity, where references will be saved, and a closed *Research run* section with the recorded queries (clipping labelled). The local CLI path is not shown. Failed, forged, cancelled or stale research shows no Apply.
- Links are rendered only for public absolute http(s) URLs (`web/js/research-model.js`) and open with `rel="noopener noreferrer nofollow"` and no referrer; anything else is plain text. All brief text is inserted as text, never markup.
- Blank projects: Quick edit explains that it changes existing items; Engineering plan offers model-derived example requests (only from services advertising `equipment_creation`). The 3D view frames the camera once on the first scene with visible components.

## Limits

- Research is textual public web evidence; dimensions remain illustrative assumptions.
- Guided routing is per line and bounded (DN50, axis-aligned candidates, conservative body boxes); not a plant router.
- DN50 liquid only; branches need a junction entity that does not exist.
- Descriptions of drafts keep declared circuits separate and list unconnected items as not connected; ambiguous or multi-flow titles use `PROCESS SCHEMATIC` ([drafting](DRAFTING.md)).
- Tests use labelled fake planners and researchers (`tests/test_planning_creation.py`, `tests/test_interactive_research.py`, `tests/test_research_review_notes.py`, `tests/browser/creation.e2e.mjs`); they prove mechanics only, not real model behaviour.
