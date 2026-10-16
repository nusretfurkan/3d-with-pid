"""Build units for ``composed_equipment`` entities from their embedded definitions.

There is no handler per equipment type: every composed entity goes through
``skid.equipment.resolve_definition`` (the same pure resolver the core uses
for port positions) at its current parameter values, and the resolved
components pass through the shared component compiler like any other unit.

Recipe-local assumption IDs are namespaced by entity ID in every generated
sidecar (``<entity id>::<local id>``, local IDs never contain ``:``), so two
definitions that both say ``rcp-appearance`` cannot overwrite each other's
provenance or the global recipe assumptions. Project (core model) assumption
IDs are cited unchanged: the entity's parameter provenance and its placement
assumption are added to every component, as for built-in kinds. A unit
records its definition document, digest and exact metre parameters, so a
generated snapshot can be checked against the definition before replay.
"""

from __future__ import annotations

import copy

from skid.equipment import COMPOSED_KIND, COMPOSED_VERSION, DefinitionError, definition_digest, resolve_definition

from .recipes import RecipeError

__all__ = [
    "GENERATOR",
    "NAMESPACE_SEPARATOR",
    "check_unit_definition",
    "composed_recipe",
    "definition_assumption_records",
    "namespaced",
]

GENERATOR = "skid.equipment.definition"
NAMESPACE_SEPARATOR = "::"


def namespaced(entity_id: str, local_id: str) -> str:
    """Sidecar ID of a definition-local assumption of one entity."""
    return f"{entity_id}{NAMESPACE_SEPARATOR}{local_id}"


def definition_assumption_records(entity) -> dict:
    """``{namespaced id: record}`` for the local assumptions of a composed entity's definition."""
    definition = entity["definition"]
    records = {}
    for local_id, record in sorted(definition["geometry"]["assumptions"].items()):
        records[namespaced(entity["id"], local_id)] = {
            "scope": "definition", "category": record["category"], "description": record["description"],
            "source": f"definition {definition['id']!r} of entity {entity['id']!r} (local assumption {local_id!r})",
        }
    return records


def composed_recipe(entity, reserved_ids=()) -> dict:
    """``{generator, parameters_mm, ports, components, definition, assumption_records}`` for one entity.

    ``reserved_ids`` are the project and global recipe assumption IDs; a
    namespaced ID that collides with one fails explicitly (``RecipeError``)
    rather than replacing it. Definition faults also raise ``RecipeError``.
    """
    try:
        resolved = resolve_definition(entity)
    except DefinitionError as exc:
        raise RecipeError(str(exc)) from None
    entity_id = entity["id"]
    records = definition_assumption_records(entity)
    clashes = sorted(set(records) & set(reserved_ids))
    if clashes:
        raise RecipeError(f"namespaced definition assumption IDs {clashes} collide with existing assumption IDs")
    cited = sorted({q["assumption_id"] for q in entity["parameters"].values() if q.get("assumption_id")})
    components = []
    for component in resolved["components"]:
        component = copy.deepcopy(component)
        component["assumptions"] = sorted({namespaced(entity_id, a) for a in component["assumptions"]} | set(cited))
        components.append(component)
    ports = {name: {"position_mm": list(port["position_mm"]), "direction": list(port["direction"]),
                    "component": port["component"]}
             for name, port in resolved["ports"].items()}
    parameters_m = {name: record["value"] for name, record in sorted(resolved["parameters"].items())}
    return {
        "generator": f"{GENERATOR}/{resolved['definition_id']}@{COMPOSED_VERSION}",
        "parameters_mm": {name: round(value * 1000.0, 9) for name, value in parameters_m.items()},
        "ports": ports,
        "components": components,
        "definition": {"sha256": resolved["definition_sha256"], "parameters_m": parameters_m,
                       "document": copy.deepcopy(entity["definition"])},
        "assumption_records": records,
    }


def check_unit_definition(unit) -> None:
    """Check a snapshot unit's recorded definition against its components and ports; raise ``ValueError``.

    The recorded definition must validate, match its digest, and resolve at
    the recorded parameters to exactly the unit's part calls, materials and
    local port frames, citing every namespaced local assumption.
    """
    record = unit["definition"]
    if not isinstance(record, dict) or set(record) != {"sha256", "parameters_m", "document"}:
        raise ValueError("definition must be {sha256, parameters_m, document}")
    document = record["document"]
    if definition_digest(document) != record["sha256"]:
        raise ValueError("definition document does not match its recorded sha256")
    parameters = record["parameters_m"]
    if not isinstance(parameters, dict):
        raise ValueError("definition parameters_m must be an object")
    entity = {"id": unit["id"], "kind": COMPOSED_KIND, "template_version": COMPOSED_VERSION, "definition": document,
              "parameters": {name: {"value": value, "unit": "m", "source": "snapshot"}
                             for name, value in parameters.items()}}
    try:
        expected = composed_recipe(entity)
    except RecipeError as exc:
        raise ValueError(f"recorded definition does not resolve: {exc}") from None
    if expected["parameters_mm"] != unit["parameters_mm"]:
        raise ValueError("parameters_mm differ from the recorded definition parameters")
    if expected["generator"] != unit["generator"]:
        raise ValueError(f"generator {unit['generator']!r} does not name the recorded definition")
    if len(expected["components"]) != len(unit["components"]):
        raise ValueError("components differ from the recorded definition")
    for want, got in zip(expected["components"], unit["components"]):
        if {k: v for k, v in want.items() if k != "assumptions"} != {k: v for k, v in got.items() if k != "assumptions"}:
            raise ValueError(f"component {got.get('key')!r} differs from the recorded definition")
        if not set(want["assumptions"]) <= set(got["assumptions"]):  # the namespaced local assumptions
            raise ValueError(f"component {got.get('key')!r} omits its definition assumptions")
    by_name = {port["name"]: port for port in unit["ports"]}
    if set(by_name) != set(expected["ports"]):
        raise ValueError("ports differ from the recorded definition")
    for name, port in expected["ports"].items():
        got = by_name[name]
        if got["local_position_mm"] != port["position_mm"] or got["local_direction"] != port["direction"] \
                or got["component_key"] != port["component"]:
            raise ValueError(f"port {name!r} frame differs from the recorded definition")
