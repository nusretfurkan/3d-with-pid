"""The structured engineering-plan record: one schema, two uses.

``PLAN_SCHEMA`` is the complete internal schema, including length, count,
pattern and range limits. ``cli_schema()`` strips those limit keywords and
gives the conservative subset (types, properties, required, closed objects,
enums, items, anyOf) passed to the CLI's ``--json-schema``; the CLI's own
enforcement is advisory. Because the CLI never sees the limits,
``limit_lines()`` renders every one of them from the same schema for the
prompt. ``validate_plan`` applies the complete schema to untrusted planner
output with this module's own checker and returns path-qualified problems;
an output over any limit is rejected whole, never truncated. Free-form JSON
(a composed-equipment definition) travels as a string and is parsed strictly
later.
"""

from __future__ import annotations

import copy
import json
import math
import re

__all__ = ["ADDABLE_KINDS", "MAX_OPERATIONS", "OPERATION_TYPES", "PLAN_SCHEMA", "ROUTE_MODES", "cli_schema",
           "limit_lines", "schema_node", "strict_json", "validate_plan"]

MAX_OPERATIONS = 12
MAX_DEFINITION_CHARS = 65536
MAX_DEPTH = 40
_LIMIT_KEYWORDS = ("minLength", "maxLength", "pattern", "minItems", "maxItems", "minimum", "maximum")

ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}"
NEW_ENTITY_PATTERN = r"ent-[a-z0-9]+(?:-[a-z0-9]+)*"
NEW_CONNECTION_PATTERN = r"conn-[a-z0-9]+(?:-[a-z0-9]+)*"
TAG_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 _./#-]{0,39}"
KEY_PATTERN = r"[a-z][a-z0-9-]{0,23}"
SOURCE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}"
VALVE_KINDS = ("isolation_valve", "check_valve")
# Built-in kinds add_equipment may create (instruments are deliberately absent); plus composed_equipment.
ADDABLE_KINDS = ("tank", "centrifugal_pump", "isolation_valve", "check_valve", "boundary")
COMPOSED = "composed_equipment"
ROUTE_MODES = ("none", "guided", "points")


def _text(maximum, minimum=0):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _number(limit):
    return {"type": "number", "minimum": -limit, "maximum": limit}


def _vector(size, limit):
    return {"type": "array", "items": _number(limit), "minItems": size, "maxItems": size}


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_ID = {"type": "string", "pattern": ID_PATTERN}
_NEW_ENTITY = {"type": "string", "pattern": NEW_ENTITY_PATTERN, "maxLength": 40}
_TAG = {"type": "string", "pattern": TAG_PATTERN}
_ASSUMPTION = {"type": "string", "minLength": 1, "maxLength": 80}
_UNIT = {"type": "string", "enum": ["m", "mm"]}
_POSITION = _vector(3, 1000.0)
_ROTATION = _vector(3, 360.0)
_SYMBOL = _obj({"x": _number(10000.0), "y": _number(10000.0), "rotation_deg": _number(360.0)})
_QUANTITY = _obj({"name": {"type": "string", "pattern": r"[a-z][a-z0-9_]{0,47}"}, "value": _number(1000.0),
                  "unit": _UNIT, "assumption": _ASSUMPTION})
_PLACEMENT = _obj({"status": {"type": "string", "enum": ["placed", "unresolved"]},
                   "position_m": {"type": "array", "items": _number(1000.0), "maxItems": 3},
                   "rotation_deg": {"type": "array", "items": _number(360.0), "maxItems": 3},
                   "assumption": {"type": "string", "maxLength": 80}})
_INSERT_COMMON = {
    "connection_id": _ID, "entity_id": _NEW_ENTITY, "tag": _TAG, "downstream_tag": _TAG,
    "parameters": {"type": "array", "items": _QUANTITY, "maxItems": 24}, "symbol": _SYMBOL, "placement": _PLACEMENT,
}
_DEFINITION_ARGS = {"definition_ref": {"type": "string", "maxLength": 120},
                    "definition_json": {"type": "string", "maxLength": MAX_DEFINITION_CHARS}}
_ROUTE_REQUEST = _obj({"mode": {"type": "string", "enum": list(ROUTE_MODES)},
                       "points_m": {"type": "array", "items": _POSITION, "maxItems": 64},
                       "assumption": {"type": "string", "maxLength": 80}})

OPERATION_ARGS = {
    "rename_entity": {"entity_id": _ID, "tag": _TAG},
    "set_parameter": {"entity_id": _ID, "name": {"type": "string", "pattern": r"[a-z][a-z0-9_]{0,47}"},
                      "value": _number(1000.0), "unit": _UNIT, "assumption": _ASSUMPTION},
    "move_equipment": {"entity_id": _ID, "position_m": _POSITION, "rotation_deg": _ROTATION,
                       "assumption": _ASSUMPTION},
    "move_symbol": {"symbol_id": _ID, "x": _number(10000.0), "y": _number(10000.0), "rotation_deg": _number(360.0)},
    "set_route": {"connection_id": _ID, "points_m": {"type": "array", "items": _POSITION, "minItems": 2,
                                                     "maxItems": 64}, "assumption": _ASSUMPTION},
    "set_diagram_route": {"edge_id": _ID, "points": {"type": "array", "items": _vector(2, 10000.0), "minItems": 2,
                                                     "maxItems": 64}},
    "insert_valve": dict(_INSERT_COMMON, kind={"type": "string", "enum": list(VALVE_KINDS)}),
    "insert_equipment": dict(_INSERT_COMMON, **_DEFINITION_ARGS),
    "add_equipment": {"entity_id": _NEW_ENTITY, "tag": _TAG, "kind": {"type": "string",
                                                                     "enum": [*ADDABLE_KINDS, COMPOSED]},
                      **_DEFINITION_ARGS, "parameters": {"type": "array", "items": _QUANTITY, "maxItems": 24},
                      "port_assumption": _ASSUMPTION, "symbol": _SYMBOL, "placement": _PLACEMENT},
    "connect_ports": {"connection_id": {"type": "string", "pattern": NEW_CONNECTION_PATTERN, "maxLength": 40},
                      "tag": _TAG, "from_port_id": _ID, "to_port_id": _ID, "from_symbol_id": _ID,
                      "to_symbol_id": _ID, "outside_diameter_mm": {"type": "number", "minimum": 1, "maximum": 199},
                      "assumption": _ASSUMPTION, "route": _ROUTE_REQUEST},
}
OPERATION_TYPES = tuple(OPERATION_ARGS)

PLAN_SCHEMA = _obj({
    "outcome": {"type": "string", "enum": ["proposal", "questions", "unsupported"]},
    "summary": _text(1000, 1),
    "reasoning": {"type": "array", "items": _text(600, 1), "maxItems": 12},
    "operations": {"type": "array", "maxItems": MAX_OPERATIONS, "items": {"anyOf": [
        _obj({"op": {"type": "string", "enum": [name]}, "purpose": _text(300, 1), "args": _obj(args)})
        for name, args in OPERATION_ARGS.items()]}},
    # source_ids: research source IDs (context.research) the assumption relies on; [] without research.
    "assumptions": {"type": "array", "maxItems": 16, "items": _obj({
        "key": {"type": "string", "pattern": KEY_PATTERN}, "description": _text(600, 1),
        "source_ids": {"type": "array", "items": {"type": "string", "pattern": SOURCE_ID_PATTERN}, "maxItems": 8}})},
    "unknowns": {"type": "array", "items": _text(400, 1), "maxItems": 24},
    "questions": {"type": "array", "maxItems": 8, "items": _obj({
        "id": {"type": "string", "pattern": KEY_PATTERN}, "question": _text(400, 1), "reason": _text(400, 1)})},
    "impacts": {"type": "array", "maxItems": 32, "items": _obj({
        "ids": {"type": "array", "items": _ID, "maxItems": 16}, "effect": _text(400, 1)})},
})


def schema_node(*path, schema=None):
    """The ``PLAN_SCHEMA`` node at ``path`` (property names; ``"items"`` steps into arrays), read at call time."""
    node = PLAN_SCHEMA if schema is None else schema
    for step in path:
        node = node["items"] if step == "items" else node["properties"][step]
    return node


def _count_rule(node):
    low, high = node.get("minItems", 0), node.get("maxItems")
    if high is not None and low == high:
        return f"exactly {high} items"
    if high is not None:
        return f"{low} to {high} items" if low else f"at most {high} items"
    return f"at least {low} items" if low else None


def limit_lines(schema=None) -> list:
    """Every count, length, pattern, enum and range the local checker enforces, as ``path: rules`` lines.

    Generated from the schema itself (default ``PLAN_SCHEMA``, read at call time), so the planner is told
    exactly the limits ``validate_plan`` applies. ``[]`` marks array items; ``(op X)`` an operation variant.
    """
    lines = []

    def emit(path, rules):
        rules = [rule for rule in rules if rule]
        if rules:
            lines.append(f"{path}: {'; '.join(rules)}")

    def walk(node, path):
        if "anyOf" in node:
            for variant in node["anyOf"]:
                name = variant["properties"]["op"]["enum"][0]
                fields = {key: sub for key, sub in variant["properties"].items() if key != "op"}
                walk(dict(variant, properties=fields), f"{path} (op {name})")
            return
        kind = node.get("type")
        if kind == "object":
            for name, sub in node["properties"].items():
                walk(sub, f"{path}.{name}" if path else name)
        elif kind == "array":
            emit(path, [_count_rule(node)])
            walk(node.get("items", {}), f"{path}[]")
        elif kind == "string":
            emit(path, ["one of " + ", ".join(node["enum"]) if "enum" in node else None,
                        f"must fully match ^{node['pattern']}$" if "pattern" in node else None,
                        "nonempty" if node.get("minLength", 0) > 0 else None,
                        f"at most {node['maxLength']} characters" if "maxLength" in node else None])
        elif kind == "number":
            bounded = "minimum" in node and "maximum" in node
            emit(path, [f"finite number from {node['minimum']:g} to {node['maximum']:g}" if bounded
                        else "finite number"])

    walk(PLAN_SCHEMA if schema is None else schema, "")
    return lines


def cli_schema() -> dict:
    """The schema without limit keywords, for the CLI's ``--json-schema`` (limits are checked locally)."""

    def strip(node):
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key not in _LIMIT_KEYWORDS}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(copy.deepcopy(PLAN_SCHEMA))


# --- Checking ----------------------------------------------------------------------


def strict_json(text):
    """Parse JSON text strictly: duplicate keys, NaN/Infinity and excessive nesting raise ``ValueError``."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise ValueError(f"nonfinite number {name}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None
    if _depth(value) > MAX_DEPTH:
        raise ValueError(f"JSON nesting deeper than {MAX_DEPTH} levels")
    return value


def _depth(value, level=1):
    if level > MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level])
    return level


def _bad_text(value):
    return any(ord(ch) < 32 and ch not in "\n\t" for ch in value) or " " in value or " " in value


def _check(value, schema, path, problems):
    if len(problems) >= 25:
        return
    if "anyOf" in schema:
        variants = schema["anyOf"]
        op = value.get("op") if isinstance(value, dict) else None
        chosen = [v for v in variants if op in v["properties"]["op"]["enum"]]
        if not chosen:
            problems.append((path, f"operation type {str(op)[:40]!r} is not one of "
                                   f"{', '.join(OPERATION_TYPES)}"))
            return
        _check(value, chosen[0], path, problems)
        return
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            problems.append((path, "must be an object"))
            return
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", ()) if name not in value]
        extra = sorted(str(name)[:40] for name in value if name not in properties)
        if missing:
            problems.append((path, f"is missing {', '.join(missing)}"))
        if extra and schema.get("additionalProperties") is False:
            problems.append((path, f"has unsupported fields {', '.join(extra)}"))
        for name, sub in properties.items():
            if name in value:
                _check(value[name], sub, f"{path}.{name}", problems)
        return
    if kind == "array":
        if not isinstance(value, list):
            problems.append((path, "must be an array"))
            return
        if len(value) < schema.get("minItems", 0):
            problems.append((path, f"needs at least {schema['minItems']} items"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append((path, f"has more than {schema['maxItems']} items"))
            return
        for index, item in enumerate(value):
            _check(item, schema.get("items", {}), f"{path}[{index}]", problems)
        return
    if kind == "string":
        if not isinstance(value, str):
            problems.append((path, "must be a string"))
            return
        if len(value) < schema.get("minLength", 0) or (value.strip() == "" and schema.get("minLength", 0) > 0):
            problems.append((path, "must not be empty"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append((path, f"is longer than {schema['maxLength']} characters"))
        elif _bad_text(value):
            problems.append((path, "contains control characters"))
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            problems.append((path, f"{value[:40]!r} does not match {schema['pattern']}"))
        if "enum" in schema and value not in schema["enum"]:
            problems.append((path, f"{value[:40]!r} is not one of {', '.join(schema['enum'])}"))
        return
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append((path, "must be a number"))
            return
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            problems.append((path, "must be finite"))
        elif ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            problems.append((path, f"{value!r} is outside [{schema.get('minimum')}, {schema.get('maximum')}]"))
        return
    problems.append((path, f"unsupported schema node {kind!r}"))  # pragma: no cover - schema construction fault


def validate_plan(plan) -> list[tuple[str, str]]:
    """``[(path, problem), ...]`` for untrusted planner output against ``PLAN_SCHEMA``; empty when valid."""
    if _depth(plan) > MAX_DEPTH:
        return [("plan", f"nesting deeper than {MAX_DEPTH} levels")]
    problems = []
    _check(plan, PLAN_SCHEMA, "plan", problems)
    return problems
