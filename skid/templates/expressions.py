"""Bounded arithmetic expressions shared by authored recipes and project definitions.

An expression is the text after a leading ``=`` in a recipe value. It may use
numeric literals, declared names, ``pi``, ``+ - * / % **``, unary signs and
the functions ``min max abs sqrt sin cos tan atan2`` (trigonometry in
degrees). Nothing else parses: no attributes, subscripts, comparisons,
conditionals, keywords, strings or other calls. This module is the only
expression evaluator; ``skid.geometry.recipes.evaluate`` wraps it, so the CAD
compiler and the engineering core always resolve the same text identically.

Work is bounded before evaluation (see ``LIMITS``): text length, syntax-tree
node count and nesting depth, function arity, and powers, whose exponent must
be a numeric literal of magnitude at most ``MAX_POWER_EXPONENT``. Parsed and
checked trees are cached by text.
"""

from __future__ import annotations

import ast
import math
import operator

from .errors import DefinitionError

__all__ = [
    "CONSTANTS",
    "FUNCTIONS",
    "LIMITS",
    "ExpressionError",
    "check_expression",
    "evaluate",
    "is_expression",
    "usable_name",
]

MAX_EXPRESSION_CHARS = 512
MAX_EXPRESSION_NODES = 160
MAX_EXPRESSION_DEPTH = 24
MAX_POWER_EXPONENT = 8
MAX_FUNCTION_ARGUMENTS = 8
LIMITS = {
    "expression_chars": MAX_EXPRESSION_CHARS,
    "expression_nodes": MAX_EXPRESSION_NODES,
    "expression_depth": MAX_EXPRESSION_DEPTH,
    "power_exponent_literal_abs": MAX_POWER_EXPONENT,
    "function_arguments": MAX_FUNCTION_ARGUMENTS,
}

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
FUNCTIONS = {"min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "sin": lambda d: math.sin(math.radians(d)),
             "cos": lambda d: math.cos(math.radians(d)), "tan": lambda d: math.tan(math.radians(d)),
             "atan2": lambda y, x: math.degrees(math.atan2(y, x))}
_ARITY = {"min": (1, MAX_FUNCTION_ARGUMENTS), "max": (1, MAX_FUNCTION_ARGUMENTS), "abs": (1, 1), "sqrt": (1, 1),
          "sin": (1, 1), "cos": (1, 1), "tan": (1, 1), "atan2": (2, 2)}
CONSTANTS = {"pi": math.pi}
_CACHE_SIZE = 4096
_CACHE: dict = {}


class ExpressionError(DefinitionError):
    """An expression is not supported arithmetic, exceeds a bound, or cannot be evaluated."""


def is_expression(value) -> bool:
    """Whether a recipe value is an expression string (``"=..."``)."""
    return isinstance(value, str) and value.startswith("=")


def usable_name(name) -> bool:
    """Whether ``name`` may be declared as a parameter or derived value."""
    return isinstance(name, str) and name.isidentifier() and name not in FUNCTIONS and name not in CONSTANTS


def _literal(node):
    """The value of a numeric literal, optionally signed, else None."""
    sign = 1.0
    while isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        if isinstance(node.op, ast.USub):
            sign = -sign
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        try:
            return sign * float(node.value)
        except OverflowError:
            return math.inf
    return None


def _parse(text):
    """Return ``(tree, names)`` for checked text; raise ``ExpressionError`` otherwise."""
    cached = _CACHE.get(text)
    if cached is not None:
        return cached
    if not isinstance(text, str):
        raise ExpressionError(f"expression must be text, not {type(text).__name__}")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ExpressionError(f"expression is longer than {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"expression {text!r} is not valid: {exc.msg}") from None
    except (RecursionError, MemoryError, ValueError):
        raise ExpressionError(f"expression {text[:40]!r}... is too deeply nested or not valid") from None
    names = set()
    count = 0
    stack = [(tree.body, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_EXPRESSION_NODES:
            raise ExpressionError(f"expression {text[:40]!r} has more than {MAX_EXPRESSION_NODES} syntax nodes")
        if depth > MAX_EXPRESSION_DEPTH:
            raise ExpressionError(f"expression {text[:40]!r} nests deeper than {MAX_EXPRESSION_DEPTH} levels")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            continue
        if isinstance(node, ast.Name):
            names.add(node.id)
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            if isinstance(node.op, ast.Pow):
                exponent = _literal(node.right)
                if exponent is None or not abs(exponent) <= MAX_POWER_EXPONENT:
                    raise ExpressionError(f"expression {text!r}: a power's exponent must be a numeric literal of "
                                          f"magnitude at most {MAX_POWER_EXPONENT}")
            stack.append((node.left, depth + 1))
            stack.append((node.right, depth + 1))
            continue
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            stack.append((node.operand, depth + 1))
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS \
                and not node.keywords:
            low, high = _ARITY[node.func.id]
            if not low <= len(node.args) <= high or any(isinstance(a, ast.Starred) for a in node.args):
                raise ExpressionError(f"expression {text!r}: {node.func.id}() takes {low}"
                                      f"{'' if low == high else f' to {high}'} argument(s)")
            stack.extend((arg, depth + 1) for arg in node.args)
            continue
        raise ExpressionError(f"expression {text!r} contains unsupported syntax {type(node).__name__}")
    result = (tree, frozenset(names))
    if len(_CACHE) >= _CACHE_SIZE:
        _CACHE.clear()
    _CACHE[text] = result
    return result


def check_expression(text: str, known) -> frozenset:
    """Statically check ``text``; every name must be in ``known`` or a constant. Return the names used."""
    _, names = _parse(text)
    unknown = sorted(n for n in names if n not in known and n not in CONSTANTS)
    if unknown:
        raise ExpressionError(f"expression {text!r} uses undeclared name {unknown[0]!r}")
    return names


def evaluate(text: str, names: dict) -> float:
    """Evaluate an arithmetic expression over ``names``; trigonometric functions use degrees."""
    tree, _ = _parse(text)

    def walk(node):
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            raise ExpressionError(f"expression {text!r} uses unknown name {node.id!r}")
        if isinstance(node, ast.BinOp):
            value = _BINARY[type(node.op)](walk(node.left), walk(node.right))
            if isinstance(value, complex):  # a negative base with a fractional exponent
                raise ExpressionError(f"expression {text!r} has no real value")
            return value
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](walk(node.operand))
        return FUNCTIONS[node.func.id](*[walk(arg) for arg in node.args])

    try:
        value = walk(tree.body)
    except ExpressionError:
        raise
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
        raise ExpressionError(f"expression {text!r} cannot be evaluated: {exc}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ExpressionError(f"expression {text!r} is not a finite number")
    return float(value)
