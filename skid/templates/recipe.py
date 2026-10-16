"""Pure resolution of authored geometry recipes (``skid.geometry.recipe/1``).

This is the one implementation of recipe resolution. ``skid.geometry.recipes
.resolve_recipe`` (standalone authored recipes, CAD) and
``skid.equipment.resolve_definition`` (equipment embedded in a project, core)
both call ``resolve_recipe`` here, so both resolve the same document to the
same numbers. It needs only the standard library.

A recipe declares parameters (metres, with bounds and an assumption), ordered
derived values, ``at_least_zero`` constraints, named ports and components of
part calls in a local millimetre frame. String values beginning with ``=``
are expressions (``skid.templates.expressions``). Length parameters appear in
expressions in millimetres.
"""

from __future__ import annotations

import copy
import math

from .errors import DefinitionError
from .expressions import evaluate, usable_name
from .vocabulary import MATERIAL_NAMES

__all__ = [
    "ASSUMPTION_CATEGORIES",
    "COMPONENT_FIELDS",
    "RECIPE_FIELDS",
    "RECIPE_SCHEMA",
    "SUPPORTED_PORT_DN",
    "check_component",
    "resolve_recipe",
    "substitute",
]

RECIPE_SCHEMA = "skid.geometry.recipe/1"
RECIPE_FIELDS = frozenset({"schema", "id", "title", "description", "length_unit", "frame", "parameters", "derived",
                           "constraints", "ports", "components", "assumptions"})
COMPONENT_FIELDS = frozenset({"key", "material", "display_only", "assumptions", "parts"})
OPTIONAL_COMPONENT_FIELDS = frozenset({"combine"})
CONSTRAINT_FIELDS = frozenset({"at_least_zero", "message"})
PARAMETER_FIELDS = frozenset({"value", "unit", "minimum", "maximum", "assumption_id"})
PORT_FIELDS = frozenset({"position", "direction", "nominal_diameter_mm", "component", "assumption_id"})
ASSUMPTION_FIELDS = frozenset({"description", "category"})
ASSUMPTION_CATEGORIES = ("illustrative_dimension", "illustrative_detail", "appearance", "approximation")
SUPPORTED_PORT_DN = 50


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DefinitionError(f"{label} must be a finite number, not {value!r}")
    return float(value)


def _vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise DefinitionError(f"{label} must be three numbers, not {value!r}")
    return tuple(_number(c, label) for c in value)


def _unit_vector(value, label):
    x, y, z = _vector(value, label)
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        raise DefinitionError(f"{label} must be a nonzero vector")
    return (x / length, y / length, z / length)


def _fields(record, required, path, optional=()):
    if not isinstance(record, dict):
        raise DefinitionError(f"{path} must be an object")
    missing = set(required) - set(record)
    extra = set(record) - set(required) - set(optional)
    if missing or extra:
        raise DefinitionError(f"{path}: missing {sorted(missing)} or unsupported {sorted(map(str, extra))}")


def _identifier(name, path):
    if not usable_name(name):
        raise DefinitionError(f"{path}: {name!r} is not a usable expression name")


def check_component(component, path, materials=MATERIAL_NAMES) -> None:
    """Check one component record's fields; ``materials`` names the palette."""
    if not isinstance(component, dict):
        raise DefinitionError(f"{path}: component must be an object")
    missing = COMPONENT_FIELDS - set(component)
    extra = set(component) - COMPONENT_FIELDS - OPTIONAL_COMPONENT_FIELDS
    if missing or extra:
        raise DefinitionError(f"{path}: component missing {sorted(missing)} or with unsupported "
                              f"{sorted(map(str, extra))}")
    key = component["key"]
    if not isinstance(key, str) or not key or "#" in key or key != key.strip():
        raise DefinitionError(f"{path}: component key must be a nonempty string without '#', not {key!r}")
    if not isinstance(component["material"], str) or component["material"] not in materials:
        raise DefinitionError(f"{path}: unknown material {component['material']!r}")
    if not isinstance(component["display_only"], bool):
        raise DefinitionError(f"{path}: display_only must be true or false")
    assumptions = component["assumptions"]
    if not isinstance(assumptions, list) or not assumptions or not all(isinstance(a, str) and a for a in assumptions):
        raise DefinitionError(f"{path}: assumptions must be a nonempty list of assumption IDs")
    if not isinstance(component["parts"], list) or not component["parts"]:
        raise DefinitionError(f"{path}: parts must be a nonempty list of part calls")
    if component.get("combine", "compound") not in ("compound", "fuse"):
        raise DefinitionError(f"{path}: combine must be 'compound' or 'fuse'")


def substitute(value, names, path):
    """Copy ``value`` with every ``"=expression"`` string replaced by its number."""
    if isinstance(value, str) and value.startswith("="):
        try:
            return evaluate(value[1:], names)
        except DefinitionError as exc:
            raise DefinitionError(f"{path}: {exc}") from None
    if isinstance(value, list):
        return [substitute(item, names, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: substitute(item, names, f"{path}.{key}") for key, item in value.items()}
    return value


def resolve_recipe(recipe: dict, overrides: dict | None = None, *, materials=MATERIAL_NAMES,
                   override_source: str = "override") -> dict:
    """Validate an authored recipe and evaluate every expression.

    ``overrides`` maps parameter names to values in the parameter's declared
    unit. Returns a new document whose parameters record their resolved value
    and whether it came from the recipe default or an override (labelled
    ``override_source``). Raises ``DefinitionError`` at the first problem.
    """
    _fields(recipe, RECIPE_FIELDS, "recipe")
    if recipe["schema"] != RECIPE_SCHEMA:
        raise DefinitionError(f"recipe schema must be {RECIPE_SCHEMA!r}, not {recipe['schema']!r}")
    if recipe["length_unit"] != "mm":
        raise DefinitionError("recipe length_unit must be 'mm'")
    for key in ("id", "title", "description", "frame"):
        if not isinstance(recipe[key], str) or not recipe[key].strip():
            raise DefinitionError(f"recipe {key} must be a nonempty string")
    assumptions = recipe["assumptions"]
    if not isinstance(assumptions, dict) or not assumptions:
        raise DefinitionError("recipe assumptions must be a nonempty map")
    for aid, record in assumptions.items():
        _fields(record, ASSUMPTION_FIELDS, f"assumption {aid!r}")
        if not isinstance(record["description"], str) or not record["description"].strip():
            raise DefinitionError(f"assumption {aid!r} needs a description")
        if record["category"] not in ASSUMPTION_CATEGORIES:
            raise DefinitionError(f"assumption {aid!r} category must be one of {ASSUMPTION_CATEGORIES}")

    def known(aid, path):
        if not isinstance(aid, str) or aid not in assumptions:
            raise DefinitionError(f"{path} references unknown assumption {aid!r}")

    overrides = dict(overrides or {})
    names, resolved_parameters = {}, {}
    if not isinstance(recipe["parameters"], dict) or not recipe["parameters"]:
        raise DefinitionError("recipe parameters must be a nonempty map")
    for name, record in recipe["parameters"].items():
        path = f"parameter {name!r}"
        _identifier(name, path)
        _fields(record, PARAMETER_FIELDS, path)
        if record["unit"] != "m":
            raise DefinitionError(f"{path}: unit must be 'm'")
        known(record["assumption_id"], path)
        low, high = (_number(record[b], f"{path} {b}") for b in ("minimum", "maximum"))
        source = "recipe default"
        value = record["value"]
        if name in overrides:
            value, source = overrides.pop(name), override_source
        value = _number(value, f"{path} value")
        if not 0 < low <= value <= high:
            raise DefinitionError(f"{path}: value {value} m is outside [{low}, {high}] m")
        names[name] = value * 1000.0
        resolved_parameters[name] = {"value": value, "unit": "m", "assumption_id": record["assumption_id"],
                                     "source": source}
    if overrides:
        raise DefinitionError(f"overrides name unknown parameters: {sorted(map(str, overrides))}")
    derived = recipe["derived"]
    if not isinstance(derived, list):
        raise DefinitionError("recipe derived must be a list of [name, expression]")
    resolved_derived = {}
    for index, item in enumerate(derived):
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], str):
            raise DefinitionError(f"derived[{index}] must be [name, '=expression']")
        name, text = item
        _identifier(name, f"derived[{index}]")
        if name in names:
            raise DefinitionError(f"derived[{index}] redefines {name!r}")
        names[name] = evaluate(text.lstrip("="), names)
        resolved_derived[name] = round(names[name], 9)
    if not isinstance(recipe["constraints"], list):
        raise DefinitionError("recipe constraints must be a list of {at_least_zero, message}")
    checked = []
    for index, constraint in enumerate(recipe["constraints"]):
        _fields(constraint, CONSTRAINT_FIELDS, f"constraints[{index}]")
        if not isinstance(constraint["at_least_zero"], str) or not isinstance(constraint["message"], str):
            raise DefinitionError(f"constraints[{index}] needs an expression string and a message")
        value = evaluate(constraint["at_least_zero"].lstrip("="), names)
        if value < 0:
            raise DefinitionError(f"constraint violated: {constraint['message']} ({constraint['at_least_zero']} = "
                                  f"{value:.6g} mm)")
        checked.append({"message": constraint["message"], "value": round(value, 9)})

    ports = recipe["ports"]
    if not isinstance(ports, dict) or not ports:
        raise DefinitionError("recipe ports must be a nonempty map")
    keys = []
    if not isinstance(recipe["components"], list) or not recipe["components"]:
        raise DefinitionError("recipe components must be a nonempty list")
    components = []
    for index, component in enumerate(recipe["components"]):
        path = f"components[{index}]"
        check_component(component, path, materials)
        if component["key"] in keys:
            raise DefinitionError(f"{path}: duplicate component key {component['key']!r}")
        keys.append(component["key"])
        for aid in component["assumptions"]:
            known(aid, path)
        components.append(substitute(copy.deepcopy(component), names, path))
    resolved_ports = {}
    for name, port in ports.items():
        path = f"port {name!r}"
        _fields(port, PORT_FIELDS, path)
        known(port["assumption_id"], path)
        if port["component"] not in keys:
            raise DefinitionError(f"{path} names unknown component {port['component']!r}")
        if port["nominal_diameter_mm"] != SUPPORTED_PORT_DN or isinstance(port["nominal_diameter_mm"], bool):
            raise DefinitionError(f"{path}: only DN50 ports are supported by this recipe version")
        position = substitute(port["position"], names, f"{path}.position")
        direction = substitute(port["direction"], names, f"{path}.direction")
        resolved_ports[name] = {
            "position_mm": [round(c, 9) for c in _vector(position, f"{path}.position")],
            "direction": [round(c, 12) for c in _unit_vector(direction, f"{path}.direction")],
            "nominal_diameter_mm": port["nominal_diameter_mm"],
            "component": port["component"],
            "assumption_id": port["assumption_id"],
        }
    return {
        "schema": RECIPE_SCHEMA,
        "id": recipe["id"],
        "title": recipe["title"],
        "description": recipe["description"],
        "frame": recipe["frame"],
        "length_unit": "mm",
        "parameters": resolved_parameters,
        "derived_mm": resolved_derived,
        "constraints_checked": checked,
        "ports": resolved_ports,
        "components": components,
        "assumptions": copy.deepcopy(assumptions),
    }
