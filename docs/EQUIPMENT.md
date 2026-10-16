# Composed equipment definitions (`skid.equipment`)

How project-defined equipment is described, validated and resolved. Contract context: [extension contracts](EXTENSION_CONTRACTS.md). Field summary and worked examples: [input contracts](INPUT_CONTRACTS.md#composed-equipment-definitions). Source is authoritative: `skid/equipment/__init__.py` (public facade) and `skid/templates/{composed,recipe,expressions,vocabulary,errors}.py`.

## Definition document

```json
{"schema": "skid.equipment.definition/1", "id": "horizontal-drum", "name": "Horizontal drum",
 "function": "Illustrative liquid hold-up vessel; operating duty unknown.",
 "geometry": {"schema": "skid.geometry.recipe/1", "...": "authored recipe, embedded"},
 "ports": {"inlet": {"direction": "in", "required": true, "service": "liquid"},
           "outlet": {"direction": "out", "required": true, "service": "liquid"}},
 "symbol": {"shape": "horizontal_vessel", "anchors": {"inlet": [-60, 0], "outlet": [60, 0]}},
 "unknowns": ["Pressure, temperature, capacity, material and ratings are unspecified."]}
```

- Eight required fields plus the optional `flow_paths`; anything else is rejected.
- Semantic port names, geometry port names and symbol anchor names must be identical sets. Geometry ports are DN50 and lie on a CAD (not display-only) component. Semantic ports declare `direction` (`in|out|both`), boolean `required` and `service: "liquid"`.
- `symbol.shape` is one of `skid.equipment.SYMBOL_SHAPES`: `horizontal_vessel`, `vertical_vessel`, `box_equipment`, `heat_exchanger`.
- ID patterns: definition IDs `^[a-z0-9][a-z0-9._-]*$`; local assumption IDs `^[A-Za-z0-9][A-Za-z0-9._-]*$`; component keys `^[A-Za-z0-9][A-Za-z0-9_./-]*$` (never `#`); port names `^[a-z][a-z0-9_]*$`.

**`flow_paths`** declares which ports carry flow *through* the item: 1–4 disjoint `[inlet, outlet]` pairs of declared port names (inlet `in|both`, outlet `out|both`, no port in two pairs). Separate pairs are separate circuits; a port outside every pair passes no flow; only a recorded connection joins circuits. Drafting and Ask read circuits this way. Without the field, the item is one flow unit (every line in may continue through every line out). A declaration is model topology only; it implies no hydraulic or heat-transfer behaviour.

## Pure interface

| Function | Behaviour |
| --- | --- |
| `validate_definition(definition)` | Error diagnostics, empty when valid; never raises. Static bounded checks first, then one resolution with the recipe's own defaults. |
| `entity_template(entity)` | Built-ins return `get_template(kind, version)` unchanged. Composed: `{kind, version, parameters, ports, diagram_anchors, definition}` plus `flow_paths` when declared. `DefinitionError` for a missing or invalid definition, an unsupported version, or a definition on a built-in kind. |
| `resolve_definition(entity)` | Resolved recipe (local port `position_mm`, direction, component; resolved components; derived values; checked constraints; local assumptions) at the entity's **current** parameters. `DefinitionError` for missing/extra/non-metre parameters, out-of-range values or violated constraints. |
| `definition_digest(definition)` | SHA-256 of canonical JSON; identifies content, not authorship. |
| `inline_ports(template)` | `("inlet", "outlet")` for a two-port inline item, else `DefinitionError`. |

Nothing in this path imports CAD, Blender or core code. Results are cached by content digest and returned as fresh copies.

## Expressions and limits

Values starting with `=` are expressions: numbers, declared names, `pi`, `+ - * / % **`, unary signs and `min max abs sqrt sin cos tan atan2` (degrees). Attributes, subscripts, comparisons, conditionals, strings, lambdas and other calls are rejected before evaluation. Exponents must be numeric literals with magnitude ≤ 8. Results must be finite.

Selected bounds from `skid.equipment.LIMITS`: canonical definition JSON 65,536 bytes; 8 ports; 4 flow paths; 24 parameters (0 < min ≤ value ≤ max ≤ 100 m); 128 derived values; 48 constraints; 32 local assumptions; 48 components; 64 part calls per component and 256 in total; nesting 4 levels; 1,024 estimated solids; expression text 512 characters, 160 syntax nodes, depth 24, 8 function arguments; 2,048 expressions; anchor coordinates ±400 drawing units; 16 unknowns of ≤ 400 characters. Read the dictionary for the full list.

## Core validation and commands

Validation adds `invalid_definition`, `missing_definition`, `missing_template`, the definition's own codes prefixed with the entity (for example `unknown_field`, `unsafe_expression`, `undeclared_name`, `port_mismatch`, `limit_exceeded`, `invalid_flow_path`), `out_of_bounds` and `definition_resolution_failed`. Port direction, required flag, DN50 and liquid service are checked exactly as for built-ins.

- `insert_equipment`: two-port inline items only; other port sets are refused with `unsupported_property`.
- `add_equipment` accepts any built-in or valid definition with its full declared port set ([empty start](EMPTY_START.md)).
- A later `set_parameter` invalidates an incident current route only if that route's endpoint actually moved; manual points are kept.
- `assumption_id` keys inside a definition are local to it and are namespaced `<entity id>::<local id>` (scope `definition`) in generated sidecars.

## CAD and display

`skid.geometry.build_geometry` builds composed items from `resolve_definition` with the shared component compiler. STEP export, reimport, solid validity, bounds, placement and port-face checks are the same as for built-ins. Composed items have no declared-envelope checks. Snapshots record the definition, its digest and the exact metre values, and replay rejects inconsistent snapshots ([CAD geometry](CAD_SPIKE.md)).

## Examples

`examples/recipes/horizontal_drum.definition.json`, `inline_box_housing.definition.json` and `shell_and_tube_exchanger.definition.json` (four ports, two `flow_paths`). All dimensions are illustrative; none carries duty, rating, material or vendor data.

## Limits

DN50 liquid service only; definition schema `/1` only, with no migration; no generic envelope checks; no equipment-to-equipment overlap check, and routes are screened only by the conservative equipment-body boxes ([pipe sections](PIPE_SEGMENTS.md)). This is bounded composition from a fixed vocabulary, not general equipment design.
