"""Entity templates and declared equipment definitions, as the drafting adapter reads them.

Port order, parameter order and diagram anchors come from one template contract
for every entity: ``skid.equipment.entity_template(entity)``, which delegates
built-in kinds to ``skid.templates.get_template`` and derives a composed
entity's template from its embedded definition. Drafting never resolves
geometry or reads a definition's recipe expressions; it uses the declared
identity, function, unknowns, symbol and local assumption records as authored
text, and the entity's own parameters as the authoritative quantities.
"""

from __future__ import annotations

from ..equipment import definition_digest as _definition_digest
from ..equipment import entity_template
from .symbols import COMPOSED_KIND


def is_composed(entity) -> bool:
    return entity.get("kind") == COMPOSED_KIND and isinstance(entity.get("definition"), dict)


def definition_of(entity) -> dict | None:
    return entity["definition"] if is_composed(entity) else None


def _text(value, fallback):
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def definition_name(entity) -> str:
    definition = definition_of(entity) or {}
    return _text(definition.get("name"), _text(definition.get("id"), "unnamed definition"))


def kind_label(entity) -> str:
    """Readable item type: the kind in words, or the declared definition name for composed equipment."""
    if is_composed(entity):
        return f"{definition_name(entity)} (composed equipment)"
    return str(entity.get("kind", "")).replace("_", " ")


def definition_digest(definition) -> str:
    """SHA-256 of the definition's canonical JSON (the foundation's digest): identifies content, not its author."""
    return _definition_digest(definition)


def parameter_order(entity, template) -> list[str]:
    """Display order of parameters: the definition's authored order for composed equipment, else the template's."""
    authored = list(recipe_ranges(entity)) if is_composed(entity) else []
    return authored or list(template.get("parameters", {}))


def definition_identity(entity) -> dict:
    """``{id, name, schema, symbol_shape, digest}`` of a composed entity's embedded definition."""
    definition = definition_of(entity) or {}
    symbol = definition.get("symbol") if isinstance(definition.get("symbol"), dict) else {}
    return {"id": definition.get("id"), "name": definition_name(entity), "schema": definition.get("schema"),
            "symbol_shape": symbol.get("shape"), "digest": definition_digest(definition)}


def declared_unknowns(entity) -> list[str]:
    definition = definition_of(entity) or {}
    unknowns = definition.get("unknowns")
    return [u.strip() for u in unknowns if isinstance(u, str) and u.strip()] if isinstance(unknowns, list) else []


def local_assumptions(entity) -> list[dict]:
    """The definition recipe's own assumption records, in ID order (``{id, category, description}``).

    They are local to this entity's definition: the same ID in another definition is a
    different record, and none of them is a project model assumption.
    """
    definition = definition_of(entity) or {}
    geometry = definition.get("geometry")
    records = geometry.get("assumptions") if isinstance(geometry, dict) else None
    if not isinstance(records, dict):
        return []
    out = []
    for local_id in sorted(records):
        record = records[local_id]
        if not isinstance(record, dict):
            continue
        out.append({"id": str(local_id), "category": _text(record.get("category"), "unspecified"),
                    "description": _text(record.get("description"), "")})
    return out


def recipe_ranges(entity) -> dict:
    """Parameter name -> {minimum?, maximum?, unit} declared by the definition recipe (authoring ranges)."""
    definition = definition_of(entity) or {}
    geometry = definition.get("geometry")
    parameters = geometry.get("parameters") if isinstance(geometry, dict) else None
    if not isinstance(parameters, dict):
        return {}
    ranges = {}
    for name, record in parameters.items():
        if isinstance(record, dict):
            ranges[name] = {k: record[k] for k in ("minimum", "maximum", "unit") if k in record}
    return ranges
