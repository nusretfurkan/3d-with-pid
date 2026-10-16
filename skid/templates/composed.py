"""Self-contained equipment definitions carried by ``composed_equipment`` entities.

A definition (``skid.equipment.definition/1``) is JSON data: identity, a
declared function, an embedded authored geometry recipe
(``skid.geometry.recipe/1``), semantic ports, a symbol from a small reusable
vocabulary with port anchors, and explicit unknowns. It contains no code,
paths or SVG. An optional ``flow_paths`` list of disjoint ``[inlet, outlet]``
port-name pairs declares which ports carry flow through the item (for
example separate tube and shell circuits); it is copied into the template
record as ``flow_paths`` only when declared. ``validate_definition`` checks it
statically and within fixed bounds (``LIMITS``), then resolves its own
defaults once.
``composed_template`` gives a composed entity its template record and
``resolve_definition`` evaluates the embedded recipe with the entity's
current, authoritative parameter values, never the recipe defaults, checking
bounds and constraints.

The public interface is ``skid.equipment`` (which adds built-in delegation in
``entity_template``). The implementation lives in ``skid.templates`` so that
the template package stays a dependency leaf: it imports only its own
modules and the standard library, never CAD, Blender or core code. Results
are cached by content (SHA-256 of canonical JSON), never by object identity,
and returned as fresh copies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
from collections import OrderedDict

from .errors import DefinitionError
from .expressions import LIMITS as EXPRESSION_LIMITS
from .expressions import ExpressionError, check_expression, is_expression, usable_name
from .recipe import (
    ASSUMPTION_CATEGORIES,
    ASSUMPTION_FIELDS,
    COMPONENT_FIELDS,
    CONSTRAINT_FIELDS,
    OPTIONAL_COMPONENT_FIELDS,
    PARAMETER_FIELDS,
    PORT_FIELDS,
    RECIPE_FIELDS,
    RECIPE_SCHEMA,
    SUPPORTED_PORT_DN,
    resolve_recipe,
)
from .vocabulary import (
    BOOLEAN_ARGUMENTS,
    FLANGE_FIELDS,
    INTEGER_ARGUMENTS,
    ITEM_PARTS,
    MATERIAL_NAMES,
    NULLABLE_ARGUMENTS,
    PART_SIGNATURES,
    SYMBOL_SHAPES,
    estimated_solids,
)

__all__ = [
    "COMPOSED_KIND",
    "COMPOSED_VERSION",
    "DEFINITION_FIELDS",
    "DEFINITION_SCHEMA",
    "LIMITS",
    "OPTIONAL_DEFINITION_FIELDS",
    "composed_template",
    "definition_digest",
    "inline_ports",
    "is_composed",
    "resolve_definition",
    "validate_definition",
]

DEFINITION_SCHEMA = "skid.equipment.definition/1"
COMPOSED_KIND = "composed_equipment"
COMPOSED_VERSION = 1
DEFINITION_FIELDS = ("schema", "id", "name", "function", "geometry", "ports", "symbol", "unknowns")
# Optional: ``flow_paths``, disjoint [inlet, outlet] port-name pairs that carry flow through the item.
# Absent means the legacy reading (every incident line belongs to one item); see ``docs/EMPTY_START.md``.
OPTIONAL_DEFINITION_FIELDS = ("flow_paths",)
DEFINITION_PORT_FIELDS = ("direction", "required", "service")
FLOW_PATH_INLET_DIRECTIONS = ("in", "both")
FLOW_PATH_OUTLET_DIRECTIONS = ("out", "both")
SYMBOL_FIELDS = ("shape", "anchors")
PORT_DIRECTIONS = ("in", "out", "both")
SERVICES = ("liquid",)

LIMITS = {
    "definition_json_bytes": 65536,
    "text_id_chars": 64,
    "text_name_chars": 120,
    "text_function_chars": 600,
    "text_description_chars": 2000,
    "text_frame_chars": 600,
    "text_assumption_chars": 1000,
    "text_constraint_message_chars": 300,
    "text_unknown_chars": 400,
    "unknowns": 16,
    "ports": 8,
    "flow_paths": 4,
    "parameters": 24,
    "parameter_maximum_m": 100.0,
    "derived": 128,
    "constraints": 48,
    "assumptions": 32,
    "components": 48,
    "component_assumptions": 16,
    "parts_per_component": 64,
    "part_calls_total": 256,
    "part_nesting_depth": 4,
    "estimated_solids_total": 1024,
    "argument_list_items": 256,
    "argument_nesting_depth": 6,
    "expressions_total": 2048,
    "anchor_abs_max": 400.0,
    **EXPRESSION_LIMITS,
}

_DEFINITION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_COMPONENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_ARGUMENT_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_CACHE_LIMIT = 256
_lock = threading.Lock()
_validated = OrderedDict()  # definition digest -> diagnostics
_templates = OrderedDict()  # definition digest -> template record
_resolved = OrderedDict()  # (definition digest, parameter values) -> resolved recipe


def _remember(cache, key, value):
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)


def _recall(cache, key):
    with _lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def definition_digest(definition) -> str:
    """SHA-256 of the definition's canonical JSON (raises ``DefinitionError`` if it is not finite JSON)."""
    try:
        return hashlib.sha256(_canonical(definition)).hexdigest()
    except (TypeError, ValueError, RecursionError) as exc:
        raise DefinitionError(f"definition is not finite JSON data: {exc}") from None


def _show(value, limit=60):
    try:
        text = repr(value)
    except Exception:  # pragma: no cover - only pathological objects
        text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


# --- Static validation ---------------------------------------------------------


class _Checker:
    """Collects diagnostics for one definition; every loop is bounded by ``LIMITS``."""

    def __init__(self):
        self.diagnostics = []
        self.expressions = 0
        self.part_calls = 0
        self.solids = 0

    def error(self, code, message):
        self.diagnostics.append({"code": code, "severity": "error", "entity_ids": [], "message": message})

    def fields(self, record, required, optional, where):
        if not isinstance(record, dict):
            self.error("invalid_type", f"{where} must be an object, not {type(record).__name__}")
            return False
        for key in record:
            if key not in required and key not in optional:
                self.error("unknown_field", f"{where} has unsupported field {_show(key)}")
        for key in required:
            if key not in record:
                self.error("missing_field", f"{where} is missing required field {key!r}")
        return True

    def text(self, value, limit_key, where, pattern=None):
        limit = LIMITS[limit_key]
        if not isinstance(value, str) or not value.strip():
            self.error("invalid_value", f"{where} must be a nonempty string, not {_show(value)}")
            return False
        if len(value) > limit:
            self.error("limit_exceeded", f"{where} is longer than {limit} characters")
            return False
        if pattern is not None and not pattern.match(value):
            self.error("invalid_value", f"{where} {_show(value)} must match {pattern.pattern}")
            return False
        return True

    def bounded(self, collection, limit_key, where, minimum=0):
        limit = LIMITS[limit_key]
        if len(collection) > limit:
            self.error("limit_exceeded", f"{where} has {len(collection)} entries; at most {limit} are allowed")
            return False
        if len(collection) < minimum:
            self.error("missing_field", f"{where} needs at least {minimum} entr{'y' if minimum == 1 else 'ies'}")
            return False
        return True

    def expression(self, text, known, where):
        self.expressions += 1
        if self.expressions > LIMITS["expressions_total"]:
            if self.expressions == LIMITS["expressions_total"] + 1:
                self.error("limit_exceeded", f"definition has more than {LIMITS['expressions_total']} expressions")
            return False
        try:
            check_expression(text, known)
        except ExpressionError as exc:
            code = "undeclared_name" if "undeclared name" in str(exc) else "unsafe_expression"
            self.error(code, f"{where}: {exc}")
            return False
        return True

    # Definition envelope

    def definition(self, definition):
        if not self.fields(definition, DEFINITION_FIELDS, OPTIONAL_DEFINITION_FIELDS, "definition"):
            return
        if "schema" in definition and definition["schema"] != DEFINITION_SCHEMA:
            self.error("unsupported_schema", f"definition schema must be {DEFINITION_SCHEMA!r}, not "
                                             f"{_show(definition['schema'])}")
        if "id" in definition:
            self.text(definition["id"], "text_id_chars", "definition id", _DEFINITION_ID)
        if "name" in definition:
            self.text(definition["name"], "text_name_chars", "definition name")
        if "function" in definition:
            self.text(definition["function"], "text_function_chars", "definition function")
        unknowns = definition.get("unknowns")
        if "unknowns" in definition:
            if not isinstance(unknowns, list):
                self.error("invalid_type", "definition unknowns must be a list of statements")
            elif self.bounded(unknowns, "unknowns", "definition unknowns"):
                for index, item in enumerate(unknowns):
                    self.text(item, "text_unknown_chars", f"definition unknowns[{index}]")
        names = self.ports(definition.get("ports")) if "ports" in definition else None
        if "flow_paths" in definition:
            self.flow_paths(definition["flow_paths"], definition.get("ports") if names is not None else None)
        if "symbol" in definition:
            self.symbol(definition["symbol"], names)
        if "geometry" in definition:
            recipe_ports = Recipe(self).run(definition["geometry"])
            if names is not None and recipe_ports is not None and set(recipe_ports) != set(names):
                self.error("port_mismatch", f"geometry ports {sorted(recipe_ports)} must equal the definition "
                                            f"ports {sorted(names)}")

    def ports(self, ports):
        if not isinstance(ports, dict):
            self.error("invalid_type", "definition ports must be an object keyed by port name")
            return None
        if not self.bounded(ports, "ports", "definition ports", minimum=1):
            return None
        names = []
        for name, record in ports.items():
            where = f"definition port {_show(name)}"
            if not isinstance(name, str) or len(name) > 32 or not _PORT_NAME.match(name):
                self.error("invalid_value", f"{where}: port names must match {_PORT_NAME.pattern} (32 characters "
                                            f"at most)")
                continue
            names.append(name)
            if not self.fields(record, DEFINITION_PORT_FIELDS, (), where):
                continue
            if "direction" in record and record["direction"] not in PORT_DIRECTIONS:
                self.error("invalid_value", f"{where} direction must be in, out or both, not "
                                            f"{_show(record['direction'])}")
            if "required" in record and not isinstance(record["required"], bool):
                self.error("invalid_type", f"{where} required must be true or false")
            if "service" in record and record["service"] not in SERVICES:
                self.error("invalid_service", f"{where} service must be one of {', '.join(SERVICES)}, not "
                                              f"{_show(record['service'])}")
        return names

    def flow_paths(self, paths, ports):
        """Disjoint ``[inlet, outlet]`` pairs of declared ports: inlet ``in``/``both``, outlet ``out``/``both``."""
        if not isinstance(paths, list):
            self.error("invalid_type", "definition flow_paths must be a list of [inlet, outlet] port-name pairs")
            return
        if not self.bounded(paths, "flow_paths", "definition flow_paths", minimum=1):
            return
        claimed = {}
        for index, pair in enumerate(paths):
            where = f"definition flow_paths[{index}]"
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(name, str) for name in pair)):
                self.error("invalid_flow_path", f"{where} must be [inlet port name, outlet port name], not "
                                                f"{_show(pair)}")
                continue
            if pair[0] == pair[1]:
                self.error("invalid_flow_path", f"{where} names port {pair[0]!r} as both its inlet and its outlet")
                continue
            for role, name, allowed in (("inlet", pair[0], FLOW_PATH_INLET_DIRECTIONS),
                                        ("outlet", pair[1], FLOW_PATH_OUTLET_DIRECTIONS)):
                record = ports.get(name) if isinstance(ports, dict) else None
                if not isinstance(record, dict):
                    self.error("invalid_flow_path", f"{where} {role} {_show(name)} is not a declared port")
                    continue
                if record.get("direction") not in allowed:
                    self.error("invalid_flow_path", f"{where} {role} {name!r} has direction "
                                                    f"{_show(record.get('direction'))}; a path {role} must be "
                                                    f"{' or '.join(allowed)}")
                if name in claimed:
                    self.error("invalid_flow_path", f"{where} reuses port {name!r} of flow_paths[{claimed[name]}]; "
                                                    "flow paths must not share ports")
                else:
                    claimed[name] = index

    def symbol(self, symbol, names):
        if not self.fields(symbol, SYMBOL_FIELDS, (), "definition symbol"):
            return
        if "shape" in symbol and (not isinstance(symbol["shape"], str) or symbol["shape"] not in SYMBOL_SHAPES):
            self.error("unsupported_symbol", f"definition symbol shape {_show(symbol['shape'])} is not one of "
                                             f"{', '.join(SYMBOL_SHAPES)}")
        anchors = symbol.get("anchors")
        if "anchors" not in symbol:
            return
        if not isinstance(anchors, dict):
            self.error("invalid_type", "definition symbol anchors must be an object keyed by port name")
            return
        if names is not None and set(anchors) != set(names):
            self.error("port_mismatch", f"symbol anchors {sorted(map(str, anchors))} must equal the definition "
                                        f"ports {sorted(names)}")
        bound = LIMITS["anchor_abs_max"]
        for name, anchor in anchors.items():
            if not (isinstance(anchor, list) and len(anchor) == 2 and all(_number(v) for v in anchor)):
                self.error("invalid_number", f"symbol anchor {_show(name)} must be [x, y] finite numbers, not "
                                             f"{_show(anchor)}")
            elif any(abs(v) > bound for v in anchor):
                self.error("limit_exceeded", f"symbol anchor {_show(name)} lies beyond {bound:g} drawing units")


class Recipe:
    """Static, bounded checks of an embedded ``skid.geometry.recipe/1`` document."""

    def __init__(self, checker):
        self.c = checker
        self.assumptions = set()
        self.names = set()

    def run(self, recipe):
        """Check ``recipe``; return its port names (None when the port map is unusable)."""
        c = self.c
        if not c.fields(recipe, RECIPE_FIELDS, (), "geometry"):
            return None
        if "schema" in recipe and recipe["schema"] != RECIPE_SCHEMA:
            c.error("unsupported_schema", f"geometry schema must be {RECIPE_SCHEMA!r}, not {_show(recipe['schema'])}")
        if "length_unit" in recipe and recipe["length_unit"] != "mm":
            c.error("invalid_unit", "geometry length_unit must be 'mm'")
        for key, limit in (("id", "text_id_chars"), ("title", "text_name_chars"),
                           ("description", "text_description_chars"), ("frame", "text_frame_chars")):
            if key in recipe:
                c.text(recipe[key], limit, f"geometry {key}", _DEFINITION_ID if key == "id" else None)
        self.check_assumptions(recipe.get("assumptions"))
        self.check_parameters(recipe.get("parameters"))
        self.check_derived(recipe.get("derived"))
        self.check_constraints(recipe.get("constraints"))
        components = self.check_components(recipe.get("components"))
        return self.check_ports(recipe.get("ports"), components)

    def assumption(self, aid, where):
        if not isinstance(aid, str) or aid not in self.assumptions:
            self.c.error("undeclared_assumption", f"{where} references undeclared assumption {_show(aid)}")

    def check_assumptions(self, assumptions):
        c = self.c
        if not isinstance(assumptions, dict):
            c.error("invalid_type", "geometry assumptions must be an object keyed by assumption ID")
            return
        if not c.bounded(assumptions, "assumptions", "geometry assumptions", minimum=1):
            return
        for aid, record in assumptions.items():
            where = f"geometry assumption {_show(aid)}"
            if not isinstance(aid, str) or len(aid) > LIMITS["text_id_chars"] or not _LOCAL_ID.match(aid):
                c.error("invalid_value", f"{where}: local assumption IDs must match {_LOCAL_ID.pattern} "
                                         f"({LIMITS['text_id_chars']} characters at most)")
                continue
            self.assumptions.add(aid)
            if not c.fields(record, ASSUMPTION_FIELDS, (), where):
                continue
            if "description" in record:
                c.text(record["description"], "text_assumption_chars", f"{where} description")
            if "category" in record and record["category"] not in ASSUMPTION_CATEGORIES:
                c.error("invalid_value", f"{where} category must be one of {', '.join(ASSUMPTION_CATEGORIES)}")

    def name(self, name, where):
        c = self.c
        if not usable_name(name) or len(name) > 32:
            c.error("invalid_value", f"{where}: {_show(name)} is not a usable expression name")
            return False
        if name in self.names:
            c.error("duplicate_name", f"{where}: {name!r} is declared more than once")
            return False
        return True

    def check_parameters(self, parameters):
        c = self.c
        if not isinstance(parameters, dict):
            c.error("invalid_type", "geometry parameters must be an object keyed by name")
            return
        if not c.bounded(parameters, "parameters", "geometry parameters", minimum=1):
            return
        ceiling = LIMITS["parameter_maximum_m"]
        for name, record in parameters.items():
            where = f"geometry parameter {_show(name)}"
            if not self.name(name, where):
                continue
            self.names.add(name)
            if not c.fields(record, PARAMETER_FIELDS, (), where):
                continue
            if "unit" in record and record["unit"] != "m":
                c.error("invalid_unit", f"{where} unit must be 'm'")
            if "assumption_id" in record:
                self.assumption(record["assumption_id"], where)
            values = {}
            for key in ("value", "minimum", "maximum"):
                if key in record:
                    if _number(record[key]):
                        values[key] = record[key]
                    else:
                        c.error("invalid_number", f"{where} {key} must be a finite number, not {_show(record[key])}")
            if len(values) == 3 and not 0 < values["minimum"] <= values["value"] <= values["maximum"] <= ceiling:
                c.error("out_of_bounds", f"{where} needs 0 < minimum <= value <= maximum <= {ceiling:g} m")

    def check_derived(self, derived):
        c = self.c
        if not isinstance(derived, list):
            c.error("invalid_type", "geometry derived must be a list of [name, '=expression']")
            return
        if not c.bounded(derived, "derived", "geometry derived"):
            return
        for index, item in enumerate(derived):
            where = f"geometry derived[{index}]"
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], str)):
                c.error("invalid_value", f"{where} must be [name, '=expression']")
                continue
            if not self.name(item[0], where):
                continue
            c.expression(item[1].lstrip("="), self.names, where)
            self.names.add(item[0])

    def check_constraints(self, constraints):
        c = self.c
        if not isinstance(constraints, list):
            c.error("invalid_type", "geometry constraints must be a list of {at_least_zero, message}")
            return
        if not c.bounded(constraints, "constraints", "geometry constraints"):
            return
        for index, record in enumerate(constraints):
            where = f"geometry constraints[{index}]"
            if not c.fields(record, CONSTRAINT_FIELDS, (), where):
                continue
            if "message" in record:
                c.text(record["message"], "text_constraint_message_chars", f"{where} message")
            if "at_least_zero" in record:
                if isinstance(record["at_least_zero"], str):
                    c.expression(record["at_least_zero"].lstrip("="), self.names, where)
                else:
                    c.error("invalid_value", f"{where} at_least_zero must be an expression string")

    def check_components(self, components):
        """Return ``{key: display_only}`` for the usable components."""
        c = self.c
        found = {}
        if not isinstance(components, list):
            c.error("invalid_type", "geometry components must be a list")
            return found
        if not c.bounded(components, "components", "geometry components", minimum=1):
            return found
        for index, component in enumerate(components):
            where = f"geometry components[{index}]"
            if not c.fields(component, COMPONENT_FIELDS, OPTIONAL_COMPONENT_FIELDS, where):
                continue
            key = component.get("key")
            if "key" in component:
                if not isinstance(key, str) or len(key) > LIMITS["text_id_chars"] or not _COMPONENT_KEY.match(key):
                    c.error("invalid_value", f"{where} key {_show(key)} must match {_COMPONENT_KEY.pattern} (no '#')")
                    key = None
                elif key in found:
                    c.error("duplicate_component", f"{where} repeats component key {key!r}")
                    key = None
            if "material" in component and (not isinstance(component["material"], str)
                                            or component["material"] not in MATERIAL_NAMES):
                c.error("unknown_material", f"{where} material {_show(component['material'])} is not in the palette")
            if "display_only" in component and not isinstance(component["display_only"], bool):
                c.error("invalid_type", f"{where} display_only must be true or false")
            if component.get("combine", "compound") not in ("compound", "fuse"):
                c.error("invalid_value", f"{where} combine must be 'compound' or 'fuse'")
            cited = component.get("assumptions")
            if "assumptions" in component:
                if not isinstance(cited, list) or not cited:
                    c.error("invalid_value", f"{where} assumptions must be a nonempty list of assumption IDs")
                elif c.bounded(cited, "component_assumptions", f"{where} assumptions"):
                    for aid in cited:
                        self.assumption(aid, where)
            calls = component.get("parts")
            if "parts" in component:
                if not isinstance(calls, list) or not calls:
                    c.error("invalid_value", f"{where} parts must be a nonempty list of part calls")
                elif c.bounded(calls, "parts_per_component", f"{where} parts"):
                    for number, call in enumerate(calls):
                        self.part(call, f"{where}.parts[{number}]", 1)
            if key is not None:
                found[key] = component.get("display_only") is True
        if c.solids > LIMITS["estimated_solids_total"]:
            c.error("limit_exceeded", f"geometry expands to about {c.solids} solids; at most "
                                      f"{LIMITS['estimated_solids_total']} are allowed")
        return found

    def part(self, call, where, depth):
        c = self.c
        c.part_calls += 1
        if c.part_calls > LIMITS["part_calls_total"]:
            if c.part_calls == LIMITS["part_calls_total"] + 1:
                c.error("limit_exceeded", f"geometry has more than {LIMITS['part_calls_total']} part calls")
            return False
        if depth > LIMITS["part_nesting_depth"]:
            c.error("limit_exceeded", f"{where} nests part calls deeper than {LIMITS['part_nesting_depth']} levels")
            return False
        if not isinstance(call, dict) or not isinstance(call.get("part"), str):
            c.error("invalid_type", f"{where} must be a part call object with a 'part' name")
            return False
        name = call["part"]
        if name not in PART_SIGNATURES:
            c.error("unknown_part", f"{where} names unknown part {_show(name)}")
            return False
        required, optional = PART_SIGNATURES[name]
        ok = True
        for key in call:
            if key != "part" and key not in required and key not in optional:
                c.error("unknown_field", f"{where} ({name}) has unsupported argument {_show(key)}")
                ok = False
        for key in required:
            if key not in call:
                c.error("missing_field", f"{where} ({name}) is missing argument {key!r}")
                ok = False
        for key, value in call.items():
            if key == "part" or (key not in required and key not in optional):
                continue
            here = f"{where}.{key}"
            if key == "item" and name in ITEM_PARTS:
                ok = self.part(value, here, depth + 1) and ok
            elif (name, key) in INTEGER_ARGUMENTS:
                ok = self.integer(value, INTEGER_ARGUMENTS[(name, key)], here,
                                  nullable=(name, key) in NULLABLE_ARGUMENTS) and ok
            elif (name, key) in BOOLEAN_ARGUMENTS:
                if not isinstance(value, bool):
                    c.error("invalid_type", f"{here} must be true or false")
                    ok = False
            elif value is None and (name, key) in NULLABLE_ARGUMENTS:
                continue
            elif name == "flanged_nozzle" and key == "flange":
                ok = self.flange(value, here) and ok
            else:
                ok = self.value(value, here, 1) and ok
        if name == "polar_array" and (call.get("count") is None) == (call.get("angles_deg") is None):
            c.error("invalid_value", f"{where} (polar_array) needs exactly one of count or angles_deg")
            ok = False
        if ok and depth == 1:
            c.solids += estimated_solids(call)
        return ok

    def integer(self, value, bounds, where, nullable=False):
        if value is None and nullable:
            return True
        low, high = bounds
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            self.c.error("invalid_value", f"{where} must be an integer literal in [{low}, {high}], not {_show(value)}")
            return False
        return True

    def flange(self, value, where):
        c = self.c
        required, optional = FLANGE_FIELDS
        if not c.fields(value, required, optional, where):
            return False
        ok = True
        for key, item in value.items():
            if key == "bolt_count":
                ok = self.integer(item, (0, 64), f"{where}.bolt_count") and ok
            elif key in required or key in optional:
                ok = self.value(item, f"{where}.{key}", 2) and ok
            else:
                ok = False
        return ok

    def value(self, value, where, depth):
        """Numbers, expressions, and bounded lists/objects of them; nothing else."""
        c = self.c
        if depth > LIMITS["argument_nesting_depth"]:
            c.error("limit_exceeded", f"{where} nests deeper than {LIMITS['argument_nesting_depth']} levels")
            return False
        if isinstance(value, bool):
            c.error("invalid_number", f"{where}: true/false is not a dimension")
            return False
        if isinstance(value, (int, float)):
            if not _number(value):
                c.error("invalid_number", f"{where} must be a finite number, not {_show(value)}")
                return False
            return True
        if is_expression(value):
            return c.expression(value[1:], self.names, where)
        if isinstance(value, list):
            if len(value) > LIMITS["argument_list_items"]:
                c.error("limit_exceeded", f"{where} has more than {LIMITS['argument_list_items']} items")
                return False
            return all([self.value(item, f"{where}[{i}]", depth + 1) for i, item in enumerate(value)])
        if isinstance(value, dict):
            ok = True
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > 32 or not _ARGUMENT_KEY.match(key):
                    c.error("invalid_value", f"{where} has unsupported key {_show(key)}")
                    ok = False
                else:
                    ok = self.value(item, f"{where}.{key}", depth + 1) and ok
            return ok
        c.error("invalid_value", f"{where} must be a number or '=expression', not {_show(value)}")
        return False

    def check_ports(self, ports, components):
        c = self.c
        if not isinstance(ports, dict):
            c.error("invalid_type", "geometry ports must be an object keyed by port name")
            return None
        if not c.bounded(ports, "ports", "geometry ports", minimum=1):
            return None
        for name, record in ports.items():
            where = f"geometry port {_show(name)}"
            if not c.fields(record, PORT_FIELDS, (), where):
                continue
            if "assumption_id" in record:
                self.assumption(record["assumption_id"], where)
            component = record.get("component")
            if "component" in record:
                if not isinstance(component, str) or component not in components:
                    c.error("port_mismatch", f"{where} names unknown component {_show(component)}")
                elif components[component]:
                    c.error("port_mismatch", f"{where} lies on display-only component {component!r}")
            dn = record.get("nominal_diameter_mm")
            if "nominal_diameter_mm" in record and (isinstance(dn, bool) or dn != SUPPORTED_PORT_DN):
                c.error("unsupported_nominal_diameter", f"{where} must be DN{SUPPORTED_PORT_DN}; this definition "
                                                        f"version supports no other size")
            for key in ("position", "direction"):
                if key not in record:
                    continue
                vector = record[key]
                if not (isinstance(vector, list) and len(vector) == 3):
                    c.error("invalid_value", f"{where} {key} must be three numbers or expressions")
                else:
                    self.value(vector, f"{where}.{key}", 1)
        return list(ports)


def validate_definition(definition) -> list[dict]:
    """Return error diagnostics ``{code, severity, entity_ids, message}``; empty when valid.

    Static and bounded checks run first. Only a statically valid definition
    is then resolved once with its own defaults (``definition_resolution_
    failed`` if that fails). Never raises for malformed input.
    """
    try:
        digest = definition_digest(definition)
    except DefinitionError as exc:
        return [{"code": "invalid_definition", "severity": "error", "entity_ids": [], "message": str(exc)}]
    cached = _recall(_validated, digest)
    if cached is not None:
        return copy.deepcopy(cached)
    checker = _Checker()
    size = len(_canonical(definition))
    if size > LIMITS["definition_json_bytes"]:
        checker.error("limit_exceeded", f"definition is {size} bytes of canonical JSON; at most "
                                        f"{LIMITS['definition_json_bytes']} are allowed")
    else:
        try:
            checker.definition(definition)
        except Exception as exc:  # fail closed: an unexpected fault still rejects the definition
            checker.error("validation_failed", f"definition check could not complete ({type(exc).__name__}: {exc})")
        if not checker.diagnostics:
            try:
                resolve_recipe(definition["geometry"], materials=MATERIAL_NAMES)
            except Exception as exc:  # fail closed: any resolution fault rejects the definition
                detail = str(exc) if isinstance(exc, DefinitionError) else f"{type(exc).__name__}: {exc}"
                checker.error("definition_resolution_failed", f"geometry does not resolve with its own defaults: "
                                                              f"{detail}")
    _remember(_validated, digest, checker.diagnostics)
    return copy.deepcopy(checker.diagnostics)


# --- Entities ------------------------------------------------------------------


def is_composed(entity) -> bool:
    """Whether ``entity`` claims the composed-equipment kind (its definition may still be invalid)."""
    return isinstance(entity, dict) and entity.get("kind") == COMPOSED_KIND


def _checked_definition(entity):
    if not isinstance(entity, dict):
        raise DefinitionError("entity must be an object")
    version = entity.get("template_version")
    if type(version) is not int or version != COMPOSED_VERSION:
        raise DefinitionError(f"{COMPOSED_KIND} supports template_version {COMPOSED_VERSION} only, not {_show(version)}")
    if "definition" not in entity:
        raise DefinitionError(f"{COMPOSED_KIND} entity {_show(entity.get('id'))} has no definition")
    definition = entity["definition"]
    errors = validate_definition(definition)
    if errors:
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        raise DefinitionError(f"definition of {_show(entity.get('id'))} is invalid: [{errors[0]['code']}] "
                              f"{errors[0]['message']}{more}")
    return definition


def _template_record(definition, digest):
    recipe = definition["geometry"]
    parameters = {}
    for name in sorted(recipe["parameters"]):
        record = recipe["parameters"][name]
        parameters[name] = {"unit": "m", "min_inclusive": record["minimum"], "max_inclusive": record["maximum"]}
    ports = {}
    for name in sorted(definition["ports"]):
        port = definition["ports"][name]
        ports[name] = {"direction": port["direction"], "required": port["required"],
                       "nominal_diameter_mm": recipe["ports"][name]["nominal_diameter_mm"], "service": port["service"]}
    anchors = {name: [float(v) for v in definition["symbol"]["anchors"][name]] for name in sorted(ports)}
    record = {
        "kind": COMPOSED_KIND,
        "version": COMPOSED_VERSION,
        "parameters": parameters,
        "ports": ports,
        "diagram_anchors": anchors,
        "definition": {"schema": definition["schema"], "id": definition["id"], "name": definition["name"],
                       "symbol": definition["symbol"]["shape"], "sha256": digest},
    }
    if "flow_paths" in definition:  # declared order kept; absent for every legacy definition
        record["flow_paths"] = [list(pair) for pair in definition["flow_paths"]]
    return record


def composed_template(entity) -> dict:
    """Return the template record of a ``composed_equipment`` entity.

    ``{kind, version, parameters, ports, diagram_anchors, definition}`` from
    its validated definition; parameter bounds are the recipe's inclusive
    minimum/maximum in metres, ports and parameters are sorted by name.
    Raises ``DefinitionError`` for another kind, a missing or invalid
    definition, or an unsupported version.
    """
    if not is_composed(entity):
        raise DefinitionError(f"entity kind {_show(entity.get('kind') if isinstance(entity, dict) else entity)} "
                              f"is not {COMPOSED_KIND}")
    definition = _checked_definition(entity)
    digest = definition_digest(definition)
    record = _recall(_templates, digest)
    if record is None:
        record = _template_record(definition, digest)
        _remember(_templates, digest, record)
    return copy.deepcopy(record)


def inline_ports(template) -> tuple[str, str]:
    """``("inlet", "outlet")`` when a template is a two-port inline item, else ``DefinitionError``."""
    ports = template.get("ports") if isinstance(template, dict) else None
    if not isinstance(ports, dict) or set(ports) != {"inlet", "outlet"} \
            or ports["inlet"].get("direction") != "in" or ports["outlet"].get("direction") != "out":
        raise DefinitionError("an inline item needs exactly an 'inlet' (in) and an 'outlet' (out) port")
    return "inlet", "outlet"


def resolve_definition(entity) -> dict:
    """Resolve a composed entity's geometry recipe with its current parameter values.

    Returns the resolved recipe record (``skid.geometry.recipe/1`` shape: local
    millimetre ``ports`` with ``position_mm``, unit ``direction`` and
    ``component``; resolved ``components``; ``derived_mm``;
    ``constraints_checked``; local ``assumptions``) plus ``definition_id``,
    ``definition_sha256`` and ``entity_id``. Each parameter records
    ``source: "entity parameter"`` and the entity quantity's provenance.
    Raises ``DefinitionError`` for an invalid definition, missing, extra or
    non-metre parameters, out-of-range values or violated constraints.
    """
    definition = _checked_definition(entity)
    declared = definition["geometry"]["parameters"]
    parameters = entity.get("parameters")
    if not isinstance(parameters, dict):
        raise DefinitionError(f"entity {_show(entity.get('id'))} parameters must be an object")
    extra = sorted(str(name) for name in parameters if name not in declared)
    missing = sorted(name for name in declared if name not in parameters)
    if extra or missing:
        raise DefinitionError(f"entity {_show(entity.get('id'))} parameters must be exactly {sorted(declared)} "
                              f"(missing {missing}, unsupported {extra})")
    values = {}
    for name in sorted(declared):
        quantity = parameters[name]
        if not isinstance(quantity, dict) or quantity.get("unit") != "m" or not _number(quantity.get("value")):
            raise DefinitionError(f"parameter {name!r} must be a finite quantity in metres")
        values[name] = float(quantity["value"])
    digest = definition_digest(definition)
    key = (digest, tuple((name, values[name].hex()) for name in sorted(values)))
    resolved = _recall(_resolved, key)
    if resolved is None:
        resolved = resolve_recipe(definition["geometry"], values, override_source="entity parameter")
        _remember(_resolved, key, resolved)
    result = copy.deepcopy(resolved)
    for name, record in result["parameters"].items():
        quantity = parameters[name]
        provenance = {k: quantity[k] for k in ("assumption_id", "source") if k in quantity}
        result["parameters"][name] = {"value": record["value"], "unit": "m", "source": "entity parameter",
                                      "provenance": provenance, "recipe_default_assumption_id": record["assumption_id"]}
    result["definition_id"] = definition["id"]
    result["definition_sha256"] = digest
    result["entity_id"] = entity.get("id")
    return result
