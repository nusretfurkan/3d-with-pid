# Input contracts

What goes into a project document, how edits are expressed, and how open work is represented. The implementation is authoritative. Field lists below come from `skid/core/commands.py` and `skid/core/validation.py`; when in doubt, run `python -m skid.cli validate` and read its diagnostics.

All examples on this page run with the standard library only, from the repository root ([getting started](GETTING_STARTED.md)). They were run against the current source when this page was written ([checking these examples](#checking-these-examples)).

## Principles

- **One authoritative model.** Every output derives from one JSON document. Generated files are never edited back into it.
- **Three separate concerns.** `model` holds engineering semantics: equipment, ports, lines and assumptions. `pid` holds diagram presentation: symbol occurrences and edge paths. `physical` holds placements and routes. Moving a P&ID symbol never moves equipment, and moving equipment never moves a symbol.
- **Permanent IDs, editable tags.** IDs are opaque strings chosen once and never derived from tags. Renaming `P-101` to `P-201` changes `tag` only. Tags must be unique across equipment and lines. For that comparison, runs of whitespace are collapsed to one space, leading and trailing whitespace is ignored, and case is ignored. The stored tag is kept as written.
- **Connectivity by identity.** A line joins two port IDs. Touching meshes or crossing diagram lines mean nothing.
- **Every assumed value carries provenance.** A quantity cites an `assumption_id` from the assumption map, or a nonempty `source` string.
- **Open work is explicit.** Unplaced equipment, invalidated routes and deliberately unconnected ports are recorded states that keep outputs *pending*. They are never silently "fixed".

## Document

```json
{"schema_version": 1, "current": { "...project..." }, "undo": [ "...snapshots..." ], "redo": [ "...snapshots..." ]}
```

A snapshot holds `model`, `pid`, `physical` and optional `name`. It holds no revision and no generated results. Loading is strict: unknown schema versions, unexpected fields, duplicate JSON keys, non-finite numbers and broken references are rejected with diagnostics. History snapshots are validated too.

| Project field | Contract |
| --- | --- |
| `id` | Permanent project ID. New documents created by `skid.cli new` match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. |
| `name` | Optional human-facing name, separate from `id`; set by `rename_project`. |
| `revision` | Non-negative integer. Every accepted change, undo and redo increases it by one. Rejected commands, no-ops and builds do not change it. It never decreases. |
| `model.entities` / `model.ports` / `model.connections` | Maps from permanent ID to record. |
| `model.assumptions` | Map from assumption ID to `{description, source}`, both nonempty strings. Existing assumptions cannot be overwritten. |
| `pid.symbols` / `pid.edges` | Maps from occurrence ID to diagram record. |
| `physical.placements` | Map from entity ID to placement. |
| `physical.routes` | Map from connection ID to route. |

## Records

Required fields are listed first; optional fields follow "+".

| Record | Fields |
| --- | --- |
| entity | `id, kind, tag, template_version, parameters` + `definition` (required for `composed_equipment`, refused otherwise), `observes: {entity_id, variable}` (indicators only) |
| quantity | `{value, unit}` plus exactly one of `assumption_id` or `source` |
| port | `id, entity_id, name, direction (in\|out\|both), required, nominal_diameter_mm, service, assumption_id` + `connection_status: "unresolved"` (required ports only, while deliberately unconnected) |
| connection | `id, tag, from_port_id, to_port_id, nominal_diameter_mm, outside_diameter_m, assumption_id` |
| symbol | `id, entity_id, x, y, rotation_deg, manual` |
| edge | `id, connection_id, from_symbol_id, to_symbol_id, points, manual, status (current\|needs_reroute\|unresolved)` + `reason` (required when not current, forbidden when current) |
| placement | `position_m, rotation_deg, status (placed\|unresolved), manual, assumption_id` + `reason` (required when unresolved; position and rotation are then `null`) |
| route | `points_m, mode (guided\|manual), status (current\|needs_reroute\|unresolved), assumption_id` + `reason` (when not current) |

### Units and frames

| Quantity | Unit and convention |
| --- | --- |
| Geometry parameters | Metres, positive and finite (`unit: "m"`). Template-declared bounds apply. |
| Angles | Degrees. Physical rotation order is X, then Y, then Z. |
| Nominal size | `nominal_diameter_mm` is a DN designation, not a measured diameter. **Only DN50** is supported by the current templates and definitions. |
| Pipe outside diameter | `outside_diameter_m`, a separate assumed value (the example uses 0.0603 m). |
| Physical space | Right-handed, metres, +z up. A current route must start and end on the transformed port positions within 1e-6 m. Routes are orthogonal polylines. |
| Diagram space | Dimensionless drawing units, +x right, +y down. The published sheet is 1200 × 840 units, with stored coordinates mapped by x + 100, y + 100. Zones are columns 1–10 and rows A–I (A at the bottom). |
| Service | `liquid` only. |

## Commands

Every edit is one command envelope:

```json
{"id": "cmd-…", "expected_revision": 4, "type": "set_route", "payload": { }, "assumptions": { }}
```

- `id`, `expected_revision`, `type` and `payload` are required. No other field is accepted except optional `assumptions`, a map of **new** assumption IDs that the same command must reference. The CLI's `apply` fills in a missing `id`. The HTTP API (`POST /api/command`) requires all four.
- `expected_revision` is a compare-and-swap. If the document is at any other revision, the command is rejected (`stale_revision`, CLI exit 4, HTTP 409) and nothing is written. The save also rereads the file under its lock, so a concurrent writer causes `revision_conflict` instead of lost work. Re-read the current revision and resubmit; do not retry blindly.
- A command is applied to a copy, the whole result is validated, and only then is it saved atomically. A rejection leaves the document, history and outputs untouched.
- A command that changes nothing returns `no_op`: no save, no new revision, no history entry.

| Type | Payload fields | Effect on other records |
| --- | --- | --- |
| `rename_entity` | `entity_id, tag` | Tag only. |
| `rename_project` | `name` | Name only; the project `id` never changes. |
| `set_parameter` | `entity_id, name, quantity` | Incident current routes whose endpoints no longer fit become `needs_reroute`. |
| `set_port_size` | `port_id, nominal_diameter_mm, assumption_id` | Rejected unless the size is supported and the whole connection agrees (currently DN50 only). |
| `insert_valve` | `connection_id, entity, ports, downstream_connection, symbol, downstream_edge` + `placement, upstream_route, downstream_route` | Rewires `A → B` into `A → valve.inlet`, `valve.outlet → B`. The upstream connection and edge keep their IDs. A missing placement or route split is recorded `unresolved`. |
| `insert_equipment` | as `insert_valve` | Same, for a two-port `composed_equipment` with a definition. |
| `add_equipment` | `entity, ports, symbol` + `placement` | Adds one entity with exactly its declared ports. Every required port is marked `connection_status: "unresolved"` until connected. No placement means an `unresolved` placement. |
| `connect_ports` | `connection, edge` + `route` | Joins two unused ports by ID (`from` is `out`/`both`, `to` is `in`/`both`, same DN and service). An edge with `points: []`, `manual: false`, `status: "current"` gets the deterministic guided path. No route means an `unresolved` route. |
| `move_equipment` / `place_equipment` | `entity_id, placement` (status `placed`) | Every incident physical route becomes `needs_reroute`; its old points are kept. Diagram symbols do not move. |
| `move_symbol` | `symbol_id, x, y, rotation_deg` | Marks the symbol manual. Guided incident edges are recomputed. **Manual incident edges are never rewritten**: they become `needs_reroute` with their points preserved. |
| `set_route` | `connection_id, points_m, mode, assumption_id` | Sets a current route after endpoint and equipment-body screening (`route_intrusion` refuses). |
| `move_route_segment` | `connection_id, segment_index, offset_m, assumption_id` | Moves one straight section perpendicular to itself; the assumption must be added by the same command. |
| `set_diagram_route` | `edge_id, points` | Sets a manual, current diagram path ending on the symbol anchors. |
| `batch` | `operations: [{type, payload, assumptions?}, …]` (1–12) | Runs the operations in order on one working copy, then validates the final result. One revision, one undo step. Any failure rejects all operations. No nested `batch`, `undo` or `redo`, and no envelope-level `assumptions`. |
| `undo` / `redo` | `{}` | Restores a snapshot (content and manual presentation, same IDs) **at a new, higher revision**. A new edit clears redo. |

### Placement and routing behaviour

| Situation | Result | How to resolve |
| --- | --- | --- |
| Equipment added or inserted without `placement` | placement `unresolved`; incident routes `unresolved` | `place_equipment`, then `set_route` |
| Equipment moved | incident routes `needs_reroute`, old points kept | `set_route` with new points, or the workspace **Reroute** action (one guided DN50 route) |
| Dimension changed and a port moved | affected incident routes `needs_reroute` | as above |
| Symbol moved | guided edges recomputed; manual edges `needs_reroute`, old points kept | `set_diagram_route` |
| Required port deliberately left open | `required_port_unconnected` **warning**, outputs pending | `connect_ports` |
| Required port unconnected **without** the marker | `required_port_unconnected` **error** (structural; cannot commit) | connect it, or add it through `add_equipment` |
| Stored route enters an equipment body | `route_body_intrusion` warning, outputs pending | `set_route` or reroute |

Regeneration never invents placements or route points, and never replaces a manual choice. Inside a `batch`, a path invalidated by one operation and valid again at the end, with its record unchanged, is restored. A batch that moves a pump and then supplies new routes therefore reports no stale warnings.

### Diagnostics and exit codes

Diagnostics are `{code, severity: "error" | "warning", entity_ids: [...], message}`. Errors block a commit; warnings are open items that keep outputs pending.

`python -m skid.cli` exit statuses: `0` success; `1` failure (structural error, rejected command, failed build); `2` usage; `3` valid but incomplete (open items, pending bundle); `4` conflict or stale (stale revision, existing target, source changed during a build, nothing current published).

## Composed equipment definitions

A `composed_equipment` entity carries a self-contained `definition` (`schema: "skid.equipment.definition/1"`) with exactly these fields: `schema, id, name, function, geometry, ports, symbol, unknowns`, plus optional `flow_paths`. The `geometry` is an authored recipe built only from a fixed part vocabulary (`box`, `cylinder`, `cone`, `sphere`, `torus`, `revolve`, `prism`, `hex_prism`, `loft`, `pipe`, `tube`, `flange`, `flanged_nozzle`, `bolting`, `bolt`, `volute_casing`, `finned_frame`, `handwheel`, `arrow`, and the arrays `polar_array`, `linear_array`, `instances`). Expressions are a small arithmetic language with no names beyond declared parameters. The P&ID symbol is one of `horizontal_vessel`, `vertical_vessel`, `box_equipment` or `heat_exchanger`, with declared port anchors.

Entity `parameters` are the authoritative current metre values. Recipe defaults are authoring defaults only. Definitions are validated before use (`skid.equipment.validate_definition`); size limits are in `skid.equipment.LIMITS` (for example 8 ports, 48 components, 64 KiB of canonical JSON). A definition that validates can still fail CAD. That is an explicit failed build, never silent success.

Worked examples: `examples/recipes/horizontal_drum.definition.json`, `inline_box_housing.definition.json` and `shell_and_tube_exchanger.definition.json` (four ports, two declared `flow_paths`). These are illustrative shapes. They carry no duty, rating or vendor data.

## Worked example: blank project to complete model

Create a genuinely empty project, then add a tank and a pump and connect them in one reviewed batch. `new` refuses to overwrite an existing file.

<!-- check:blank:start -->
```sh
mkdir -p work/contract-example/contract-example
python -m skid.cli new work/contract-example/blank.json --name "Transfer study" --project-id project-transfer-study
python -m skid.cli validate work/contract-example/blank.json; echo "exit $?"          # VALID, exit 0: nothing to describe
```

```sh
cat > work/contract-example/add-tank-pump.json <<'EOF'
{"id": "cmd-add-tank-pump-1", "expected_revision": 0, "type": "batch", "payload": {"operations": [
  {"type": "add_equipment",
   "assumptions": {
     "asm-study-tank": {"description": "Illustrative vertical tank, 0.8 m diameter by 1.2 m high, outlet 0.2 m above the base; not a vessel design.", "source": "user study input"},
     "asm-study-ports": {"description": "DN50 liquid-service ports; duty, pressure, temperature and material unknown.", "source": "user study input"},
     "asm-study-layout": {"description": "Illustrative placement on a z = 0 deck; not a plant layout.", "source": "user study input"}},
   "payload": {
     "entity": {"id": "ent-study-tank", "kind": "tank", "tag": "TK-201", "template_version": 1,
                "parameters": {"diameter": {"value": 0.8, "unit": "m", "assumption_id": "asm-study-tank"},
                               "height": {"value": 1.2, "unit": "m", "assumption_id": "asm-study-tank"},
                               "outlet_height": {"value": 0.2, "unit": "m", "assumption_id": "asm-study-tank"}}},
     "ports": [{"id": "port-study-tank-outlet", "entity_id": "ent-study-tank", "name": "outlet", "direction": "out",
                "required": true, "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-study-ports"}],
     "symbol": {"id": "sym-study-tank", "entity_id": "ent-study-tank", "x": 120.0, "y": 280.0, "rotation_deg": 0.0, "manual": false},
     "placement": {"position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0], "status": "placed", "manual": true,
                   "assumption_id": "asm-study-layout"}}},
  {"type": "add_equipment",
   "assumptions": {
     "asm-study-pump": {"description": "Illustrative centrifugal pump envelopes (body 0.45 x 0.30 x 0.40 m, motor 0.35 m x 0.22 m); not a vendor selection.", "source": "user study input"}},
   "payload": {
     "entity": {"id": "ent-study-pump", "kind": "centrifugal_pump", "tag": "P-201", "template_version": 1,
                "parameters": {"body_length": {"value": 0.45, "unit": "m", "assumption_id": "asm-study-pump"},
                               "body_width": {"value": 0.3, "unit": "m", "assumption_id": "asm-study-pump"},
                               "body_height": {"value": 0.4, "unit": "m", "assumption_id": "asm-study-pump"},
                               "motor_length": {"value": 0.35, "unit": "m", "assumption_id": "asm-study-pump"},
                               "motor_diameter": {"value": 0.22, "unit": "m", "assumption_id": "asm-study-pump"}}},
     "ports": [{"id": "port-study-pump-inlet", "entity_id": "ent-study-pump", "name": "inlet", "direction": "in",
                "required": true, "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-study-ports"},
               {"id": "port-study-pump-outlet", "entity_id": "ent-study-pump", "name": "outlet", "direction": "out",
                "required": true, "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-study-ports"}],
     "symbol": {"id": "sym-study-pump", "entity_id": "ent-study-pump", "x": 360.0, "y": 300.0, "rotation_deg": 0.0, "manual": false},
     "placement": {"position_m": [1.5, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0], "status": "placed", "manual": true,
                   "assumption_id": "asm-study-layout"}}},
  {"type": "connect_ports",
   "assumptions": {
     "asm-study-line": {"description": "DN50 line with an assumed 0.0603 m outside diameter; not a pipe specification.", "source": "user study input"}},
   "payload": {
     "connection": {"id": "conn-study-suction", "tag": "L-201", "from_port_id": "port-study-tank-outlet",
                    "to_port_id": "port-study-pump-inlet", "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603,
                    "assumption_id": "asm-study-line"},
     "edge": {"id": "edge-study-suction", "connection_id": "conn-study-suction", "from_symbol_id": "sym-study-tank",
              "to_symbol_id": "sym-study-pump", "points": [], "manual": false, "status": "current"},
     "route": {"points_m": [[0.5, 0.0, 0.2], [1.175, 0.0, 0.2]], "mode": "manual", "status": "current",
               "assumption_id": "asm-study-line"}}}
]}}
EOF
python -m skid.cli apply work/contract-example/blank.json work/contract-example/add-tank-pump.json      # revision 0 -> 1, one undo step
python -m skid.cli validate work/contract-example/blank.json; echo "exit $?"            # exit 3: P-201 outlet is deliberately open
```

The pump outlet is now an explicit open item (`required_port_unconnected`, warning). Any build of this revision is **pending**, not complete. Undo and redo each create a new revision:

```sh
python -m skid.cli undo work/contract-example/blank.json --expected-revision 1         # revision 2: empty content again, redo 1
python -m skid.cli redo work/contract-example/blank.json --expected-revision 2         # revision 3: tank, pump and L-201 back
```

Close the scope with a boundary. This batch deliberately omits the physical route, so the new line is `unresolved`. A separate `set_route` then supplies it:

```sh
cat > work/contract-example/add-boundary.json <<'EOF'
{"id": "cmd-add-boundary-1", "expected_revision": 3, "type": "batch", "payload": {"operations": [
  {"type": "add_equipment",
   "assumptions": {
     "asm-study-boundary": {"description": "Scope boundary marker, illustrative 0.2 m size; downstream plant is outside the model.", "source": "user study input"}},
   "payload": {
     "entity": {"id": "ent-study-boundary", "kind": "boundary", "tag": "OUT-201", "template_version": 1,
                "parameters": {"size": {"value": 0.2, "unit": "m", "assumption_id": "asm-study-boundary"}}},
     "ports": [{"id": "port-study-boundary-inlet", "entity_id": "ent-study-boundary", "name": "inlet", "direction": "in",
                "required": true, "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-study-ports"}],
     "symbol": {"id": "sym-study-boundary", "entity_id": "ent-study-boundary", "x": 560.0, "y": 220.0, "rotation_deg": 0.0, "manual": false},
     "placement": {"position_m": [2.2, 0.0, 0.7], "rotation_deg": [0.0, 0.0, 0.0], "status": "placed", "manual": true,
                   "assumption_id": "asm-study-layout"}}},
  {"type": "connect_ports",
   "payload": {
     "connection": {"id": "conn-study-discharge", "tag": "L-202", "from_port_id": "port-study-pump-outlet",
                    "to_port_id": "port-study-boundary-inlet", "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603,
                    "assumption_id": "asm-study-line"},
     "edge": {"id": "edge-study-discharge", "connection_id": "conn-study-discharge", "from_symbol_id": "sym-study-pump",
              "to_symbol_id": "sym-study-boundary", "points": [[360.0, 272.0], [360.0, 220.0], [542.0, 220.0]],
              "manual": true, "status": "current"}}}
]}}
EOF
python -m skid.cli apply work/contract-example/blank.json work/contract-example/add-boundary.json       # revision 4; route of L-202 unresolved
python -m skid.cli validate work/contract-example/blank.json; echo "exit $?"            # exit 3: one open route

cat > work/contract-example/route-l202.json <<'EOF'
{"id": "cmd-route-l202-1", "expected_revision": 4, "type": "set_route",
 "payload": {"connection_id": "conn-study-discharge",
             "points_m": [[1.5, 0.0, 0.5], [1.5, 0.0, 0.7], [2.1, 0.0, 0.7]],
             "mode": "manual", "assumption_id": "asm-study-route"},
 "assumptions": {"asm-study-route": {
   "description": "Discharge riser then horizontal run above the motor; no support, stress or fabrication check.",
   "source": "user study input"}}}
EOF
python -m skid.cli apply work/contract-example/blank.json work/contract-example/route-l202.json         # revision 5
python -m skid.cli validate work/contract-example/blank.json; echo "exit $?"            # VALID, exit 0
```
<!-- check:blank:end -->

`VALID` here means the model is structurally consistent with no open items. It is not a process-design check. A real CAD build ([getting started](GETTING_STARTED.md#4-interactive-five-output-workspace)) adds geometric checks, such as bend room and STEP reimport, that the core does not perform.

## Worked example: inserting a valve

Insert an isolation valve into line L-104 of the sample skid without choosing its physical position. The diagram split validates; the physical side is honestly unresolved.

<!-- check:insert:start -->
```sh
mkdir -p work/contract-example/contract-example
python -m skid.cli sample work/contract-example/insert.json
cat > work/contract-example/insert-valve.json <<'EOF'
{"id": "cmd-insert-hv103-1", "expected_revision": 0, "type": "insert_valve",
 "assumptions": {"asm-user-hv103": {
   "description": "Additional illustrative DN50 isolation valve, 0.16 m face to face; operator type unspecified.",
   "source": "reference walkthrough"}},
 "payload": {
   "connection_id": "conn-discharge-2",
   "entity": {"id": "ent-hv103", "kind": "isolation_valve", "tag": "HV-103", "template_version": 1,
              "parameters": {"length": {"value": 0.16, "unit": "m", "assumption_id": "asm-user-hv103"},
                             "body_diameter": {"value": 0.1, "unit": "m", "assumption_id": "asm-user-hv103"}}},
   "ports": [{"id": "port-hv103-inlet", "entity_id": "ent-hv103", "name": "inlet", "direction": "in", "required": true,
              "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"},
             {"id": "port-hv103-outlet", "entity_id": "ent-hv103", "name": "outlet", "direction": "out", "required": true,
              "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"}],
   "downstream_connection": {"id": "conn-hv103-down", "tag": "L-106", "from_port_id": "port-hv103-outlet",
                             "to_port_id": "port-discharge-valve-inlet", "nominal_diameter_mm": 50,
                             "outside_diameter_m": 0.0603, "assumption_id": "asm-dn50-pipe"},
   "symbol": {"id": "sym-hv103", "entity_id": "ent-hv103", "x": 540.0, "y": 220.0, "rotation_deg": 0.0, "manual": false},
   "downstream_edge": {"id": "edge-hv103-down", "connection_id": "conn-hv103-down", "from_symbol_id": "sym-hv103",
                       "to_symbol_id": "sym-discharge-valve", "points": [[556.0, 220.0], [584.0, 220.0]],
                       "manual": false, "status": "current"}}}
EOF
python -m skid.cli apply work/contract-example/insert.json work/contract-example/insert-valve.json      # revision 1
python -m skid.cli validate work/contract-example/insert.json; echo "exit $?"           # exit 3: HV-103 unplaced, L-104/L-106 unresolved
python -m skid.cli lists work/contract-example/insert.json
```
<!-- check:insert:end -->

L-104 keeps `conn-discharge-2` and now ends at HV-103. L-106 is the new downstream line. Resolve the physical side with `place_equipment` and two `set_route` commands, ideally in one `batch` so the change is a single undo step.

## Interactive service inputs

The local service (`python -m skid.interactive`) accepts the same envelopes at `POST /api/command`. It returns `400` for a rejected command and `409` for a stale revision or conflict. It also accepts a few UI-oriented requests, such as `/api/propose`, `/api/apply-proposal` and `/api/reroute`. Every proposal is converted into ordinary validated commands before anything is saved; see `skid/interactive/server.py` for the exact endpoint list. A manual-notes sidecar (`<project stem>.notes.json`) can attach reviewed prose to stable IDs. The service passes it unchanged to drafting and never edits it.

## Optional research data

Research and engineering planning are opt-in and off unless you request them. Planning sends the request **and a project snapshot**; research sends **only the request text**. Both go to an external model service through your own Claude Code CLI ([capabilities](CAPABILITIES.md#optional-ai-planning-and-research)). Source URLs from research are stored only in the assumption records a reviewed plan adds. They identify where a statement came from; they grant no rights to that content and are not a design basis.

## Checking these examples

The `sh` blocks between the hidden `check:` comments are meant to run verbatim, in order, from the repository root inside the activated environment. They write only under `work/contract-example/` (ignored by Git), so they do not touch `work/blank.json` or `work/skid.json` from [getting started](GETTING_STARTED.md#3-core-standard-library-only). Because `new` and `sample` refuse an existing target (exit 4), remove `work/contract-example/` (only that subdirectory) before running them again. If a block's printed revision or exit status differs from its comment, please report it ([contributing](../CONTRIBUTING.md)).
