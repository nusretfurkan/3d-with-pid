# Empty projects, adding equipment and connecting ports

How to start from a genuinely empty document and build a model one item and one line at a time. A complete runnable walkthrough is in [input contracts](INPUT_CONTRACTS.md#worked-example-blank-project-to-complete-model). Planner and UI integration: [creation and research](CREATION_AND_RESEARCH_UI.md). Source is authoritative.

## New documents

- `skid.core.new_document(project_id, name=None)` returns a schema-1 document at revision 0 with empty model, P&ID and physical maps and empty history. Nothing is seeded. The ID must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; `name` follows the [project-name rule](PROJECT_NAMING.md). Bad arguments raise `DocumentError`.
- `python -m skid.cli new PATH [--name NAME] [--project-id ID]` writes one. Without `--project-id` the ID is `project-<UUID4>`. An existing target is refused (exit 4, nothing written); bad arguments exit 2. `sample` still copies the reference fixture.
- An empty document validates with no diagnostics.

## Deliberately open ports

A required port with no connection is normally a structural `required_port_unconnected` **error**. A port record may instead carry the draft marker `connection_status: "unresolved"`; the same code is then a **warning**, the document commits, and every output stays pending until the port is connected.

| Port record | Result |
| --- | --- |
| required, unconnected, unmarked | error (cannot commit) |
| required, unconnected, marked | warning (open item, outputs pending) |
| connected and marked | `contradictory_connection_status` error |
| any other marker value, or marker on an optional port | `invalid_connection_status` error |

`add_equipment` marks every required port it adds. `connect_ports`, `insert_valve` and `insert_equipment` clear or refuse the marker as appropriate; no handler leaves a connected port marked. Undo and redo restore the marker with the rest of the snapshot.

## `add_equipment`

Payload `{entity, ports, symbol, placement?}`; a single command or a batch operation.

- `entity`: any built-in template (`tank`, `centrifugal_pump`, `isolation_valve`, `check_valve`, `indicator`, `boundary`, version 1) or `composed_equipment` with a valid definition. `parameters` must be exactly the declared names in metres with provenance and within bounds; a composed entity must resolve at those values.
- `ports`: exactly one full record per declared port, repeating its direction, required flag, DN and service with exact types. Up to 8 ports for composed items.
- `symbol`: the P&ID occurrence. `placement`: optional; omitted means an `unresolved` placement with a reason and a `placement_unresolved` warning.
- All new IDs must be unused (`duplicate_id`); tags must be unique (`duplicate_tag`).

## `connect_ports`

Payload `{connection, edge, route?}`.

- Joins two **existing, unused** ports by stable ID, never by proximity: from `out|both`, to `in|both`, equal DN and service. Codes include `unknown_reference`, `self_connection`, `wrong_port_direction`, `dn_mismatch`, `service_mismatch`, `duplicate_port_use`.
- `edge`: its symbols must show the two port owners. `status: "current"`, `manual: false`, `points: []` requests the deterministic guided path (straight when aligned, else a Z through mid-x); supplied points must end on the anchors.
- `route`: optional. A current route must be orthogonal, end on both port frames and pass the [equipment-body screen](PIPE_SEGMENTS.md#equipment-body-screen). Omitted means an `unresolved` route with a `route_invalidated` warning; `set_route` or the workspace Reroute can supply it later.

Add and connect operations combine in one `batch`: later operations may reference records created earlier; any failure rejects all; a current route is screened again in the final batch result.

## Empty generation

Generating an empty project completes truthfully: `scene.json` with no components; a valid STEP file whose only product is the empty project root (no parts, solids or dummy geometry; `cad.verification.json` `summary.empty_root_product: true`); P&ID, legend, index and description with no occurrences and the narrative "The model contains no process items with ports, so no flow can be described." Any open port, unresolved placement or route keeps all five outputs pending.

## Shell-and-tube example

`examples/recipes/shell_and_tube_exchanger.definition.json` composes only existing parts (shell, front channel with bolted cover, tubesheets, return bonnet, saddles, four DN50 flanged nozzles). Ports `tube_inlet`, `tube_outlet`, `shell_inlet`, `shell_outlet`; `flow_paths: [["tube_inlet","tube_outlet"],["shell_inlet","shell_outlet"]]`. Parameters (metres, illustrative): `shell_length` 1.6 (1.0–3.0), `shell_diameter` 0.32 (0.22–0.6), `centreline_height` 0.62 (0.5–1.2), `channel_length` 0.3 (0.22–0.5), `shell_nozzle_offset` 0.22 (0.15–1.2), with clearance constraints. It uses the original `heat_exchanger` glyph. Duty, sizing and thermal and mechanical design are unknown. The arrangement assumption cites a public manufacturer overview as a general product-type reference only; no vendor geometry, drawing or dimension is copied, and the link grants no rights to its content.

Because it has four ports, it cannot be inserted inline; add it with `add_equipment` and wire it with `connect_ports`. Drafting and Ask keep its two circuits separate ([equipment](EQUIPMENT.md#definition-document), [Ask](ASK.md#declared-flow-paths)).

## Limits

DN50 liquid service only. Built-in templates have no optional ports. The guided P&ID edge is a simple Z rule; layouts with ports on the far side of a symbol need manual edge points. No thermal, hydraulic, mechanical, code or vendor verification of any example.

Tests: `tests/test_empty_start.py`, `tests/test_flow_paths.py`, `tests/test_cad_empty_exchanger.py` (CAD environment).
