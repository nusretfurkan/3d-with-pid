"""Pure-stdlib equipment definitions: validation, template dispatch and recipe resolution.

``composed_equipment`` entities carry a self-contained JSON definition whose
embedded authored recipe is resolved with the entity's current parameters.
This is the public interface; the implementation lives in ``skid.templates``
(``composed``, ``recipe``, ``expressions``, ``vocabulary``) so that the
template package, which every view and the core depend on, keeps importing
nothing outside itself. Nothing here imports CAD, Blender or core code, and
it runs under any Python 3.9+ interpreter. See ``docs/EQUIPMENT.md``.

- ``validate_definition(definition) -> list[diagnostic]`` (including the optional
  ``flow_paths``: at most ``LIMITS["flow_paths"]`` disjoint ``[inlet, outlet]`` pairs)
- ``entity_template(entity) -> dict`` (built-ins delegate unchanged to ``get_template``;
  a composed record carries ``flow_paths`` only when its definition declares them)
- ``resolve_definition(entity) -> dict`` (resolved recipe at current values)
- ``DefinitionError(ValueError)`` for lookup and resolution faults
"""

from ..templates import entity_template
from ..templates.composed import (
    COMPOSED_KIND,
    COMPOSED_VERSION,
    DEFINITION_SCHEMA,
    LIMITS,
    definition_digest,
    inline_ports,
    is_composed,
    resolve_definition,
    validate_definition,
)
from ..templates.errors import DefinitionError
from ..templates.expressions import ExpressionError, evaluate
from ..templates.recipe import RECIPE_SCHEMA, resolve_recipe
from ..templates.vocabulary import MATERIAL_NAMES, PART_SIGNATURES, SYMBOL_SHAPES

__all__ = [
    "COMPOSED_KIND",
    "COMPOSED_VERSION",
    "DEFINITION_SCHEMA",
    "LIMITS",
    "MATERIAL_NAMES",
    "PART_SIGNATURES",
    "RECIPE_SCHEMA",
    "SYMBOL_SHAPES",
    "DefinitionError",
    "ExpressionError",
    "definition_digest",
    "entity_template",
    "evaluate",
    "inline_ports",
    "is_composed",
    "resolve_definition",
    "resolve_recipe",
    "validate_definition",
]
