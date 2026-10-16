# Extension contracts: batches, composed equipment and engineering plans (version 0.3)

Current summary of the backward-compatible extension to the [core](CONTRACTS.md) and [workflow](WORKFLOW_CONTRACTS.md) contracts. Schema-1 documents, built-in templates, existing commands and the reference fixture are unchanged by it. Details: [equipment](EQUIPMENT.md), [planning](PLANNING.md), [drafting](DRAFTING.md). Source is authoritative.

## Atomic `batch`

```json
{"id": "cmd-plan", "expected_revision": 22, "type": "batch", "payload": {"operations": [
  {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-201"}},
  {"type": "set_parameter", "payload": {"entity_id": "ent-pump", "name": "motor_length",
     "quantity": {"value": 0.42, "unit": "m", "assumption_id": "asm-plan"}},
   "assumptions": {"asm-plan": {"description": "User-requested illustrative motor length.", "source": "reviewed engineering plan"}}}
]}}
```

- 1–12 operations, each exactly `type`, `payload` and optional `assumptions`. No nested `batch`, `undo` or `redo`; no envelope-level `assumptions`.
- Operations run in order on one detached working copy through the ordinary handlers; only the final result is validated. Any failure rejects everything with diagnostics prefixed `operations[i] (type):`.
- A changed batch is one revision, one undo snapshot, one redo clear and one build. A wholly unchanged batch, including changes reverted within it, is one `no_op`.
- A path invalidated and restored within the batch, with its record unchanged, is restored rather than left pending. Returned warnings describe the committed state.
- Dry run and commit are the same call; a caller simply does not persist a dry run.

## Composed equipment

An entity with `kind: "composed_equipment"`, `template_version: 1` carries a self-contained `definition` (`skid.equipment.definition/1`): `schema, id, name, function, geometry, ports, symbol, unknowns`, optional `flow_paths`. `geometry` is an authored recipe built only from a fixed part vocabulary with bounded arithmetic expressions; no scripts, imports, file paths or raw SVG. The definition is part of the entity, so it is carried in snapshots, history, digest and saved files.

Entity `parameters` are the authoritative current metre values with provenance; recipe defaults are authoring defaults only. Semantic port names, geometry port names and symbol anchors must match exactly; ports are DN50 liquid.

Pure interface, importable without CAD or Blender:

```python
skid.equipment.validate_definition(definition) -> list[diagnostic]   # never raises
skid.equipment.entity_template(entity) -> dict                       # built-ins delegate to get_template
skid.equipment.resolve_definition(entity) -> dict                    # resolved recipe at current values
skid.equipment.DefinitionError(ValueError)
```

The public `skid.equipment` facade delegates to helpers under `skid.templates`, so the template package stays a dependency leaf. CAD and core use the same resolver. A definition that validates can still fail CAD; that is an explicit failed build under the publication rules. The legacy offline adapters report composed items as pending/unsupported rather than drawing generic shapes.

`insert_equipment` has the `insert_valve` payload and rewiring semantics for a composed two-port inline item (`inlet` in, `outlet` out). The upstream connection and its P&ID edge keep their IDs; downstream IDs are supplied. Missing placement or routes stay explicitly unresolved; nothing is laid out automatically. Items with other port sets are added with `add_equipment` ([empty start](EMPTY_START.md)).

Example definitions in `examples/recipes/` are examples, not a registry: no code is keyed to any equipment type.

## Engineering planning

`POST /api/propose` keeps its deterministic quick mode and adds `mode: "engineering"`. Engineering mode sends the request and a frozen project context through the operator's own Claude Code CLI on a Claude Max login. It requests exact model `claude-opus-5-5` at effort `medium` (applied effort is not echoed by the CLI), tool-free, with `ANTHROPIC_API_KEY` removed from the child. Authentication, reported model and API-key source are checked; any mismatch refuses the plan, and no other model is substituted.

The model output is untrusted. It is validated against a strict schema, translated into one `batch`, dry-run through `apply_command` and stored as a proposal. Only an explicit Apply on the same revision and digest commits it: one revision, one undo step, one build. Questions, unsupported, rejected, unavailable, stale and expired proposals cannot apply. `validation.geometry_checked` stays false because CAD runs only in the build after Apply. No duty, material, operating condition, safety function or standards claim may be invented.

Deterministic workflows (core, CLI, generators, Quick edit, local Ask, rerouting) need no Claude at all.

## Presentation

Composed items are drawn with generic glyph families (`horizontal_vessel`, `vertical_vessel`, `box_equipment`, `heat_exchanger`) fitted to the definition's anchors, with no kind-specific handler. Authored names, functions and unknowns are quoted as authored, not verified. The inspector edits composed parameters generically from the entity's own data.
