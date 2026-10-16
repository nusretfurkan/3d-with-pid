"""Recipe components and authored equipment recipes.

A *component* is ``{key, material, display_only, assumptions, parts[, combine]}``
where ``parts`` are calls into ``skid.geometry.parts``. ``compile_component``
turns one into a single OCCT shape (a compound of solids, or one fused solid
when ``combine`` is ``"fuse"``). Both the project generators in
``equipment``/``routes`` and authored JSON recipes use it.

An *authored recipe* (see ``examples/recipes``) declares parameters with
provenance, derived values, explicit ports and components in a local
millimetre frame. String values beginning with ``=`` are arithmetic
expressions over parameter and derived names. Length parameters are declared
in metres and appear in expressions in millimetres. ``resolve_recipe`` checks
the document and evaluates expressions; ``compile_recipe`` builds the solids
and proves every port lies on an outward planar face of its component.

Expression evaluation and recipe resolution are the pure implementations in
``skid.templates.expressions`` and ``skid.templates.recipe``, shared with
the engineering core's ``composed_equipment`` definitions, so CAD and core
never resolve the same text differently. This module wraps their
``DefinitionError`` as ``RecipeError`` and supplies the CAD palette.
"""

from __future__ import annotations

from skid.templates import recipe as _shared
from skid.templates.errors import DefinitionError
from skid.templates.expressions import evaluate as _evaluate

from . import kernel as k
from . import parts
from .materials import MATERIALS

__all__ = [
    "COMPONENT_FIELDS",
    "RECIPE_SCHEMA",
    "RecipeError",
    "compile_component",
    "compile_recipe",
    "evaluate",
    "port_face_report",
    "resolve_recipe",
]

RECIPE_SCHEMA = _shared.RECIPE_SCHEMA
COMPONENT_FIELDS = set(_shared.COMPONENT_FIELDS)
PORT_FACE_TOLERANCE_MM = 1e-4
ASSUMPTION_CATEGORIES = _shared.ASSUMPTION_CATEGORIES


class RecipeError(ValueError):
    """A recipe or component is malformed, inconsistent or cannot be built."""


# --- Components --------------------------------------------------------------


def check_component(component, path) -> None:
    try:
        _shared.check_component(component, path, MATERIALS)
    except DefinitionError as exc:
        raise RecipeError(str(exc)) from None


def compile_component(component, path="component"):
    """Return ``(shape, solid_count)`` for a checked component (local millimetres)."""
    check_component(component, path)
    solids = []
    for index, call in enumerate(component["parts"]):
        try:
            solids += parts.build(call, f"{path}/parts[{index}]")
        except (parts.PartError, k.KernelError) as exc:
            raise RecipeError(str(exc)) from exc
    if not solids:
        raise RecipeError(f"{path}: produced no solids")
    if component.get("combine") == "fuse":
        try:
            shape = k.fuse(solids)
        except k.KernelError as exc:
            raise RecipeError(f"{path}: {exc}") from exc
    else:
        shape = k.compound(solids)
    count = k.solid_count(shape)
    if count == 0:
        raise RecipeError(f"{path}: produced no solids")
    return shape, count


def port_face_report(shape, position, direction) -> dict:
    """Check that a port lies on an outward planar face of ``shape``.

    ``shape`` is moved into the port frame (port at the origin, outward
    direction +z). The shape must not extend beyond the port plane, and some
    planar face must lie in that plane and cover the port's axis.
    """
    u, v, n = k.frame_axes(direction)
    p = position
    matrix = (
        (u[0], u[1], u[2], -(u[0] * p[0] + u[1] * p[1] + u[2] * p[2])),
        (v[0], v[1], v[2], -(v[0] * p[0] + v[1] * p[1] + v[2] * p[2])),
        (n[0], n[1], n[2], -(n[0] * p[0] + n[1] * p[1] + n[2] * p[2])),
    )
    framed = k.located(shape, matrix)
    (xmin, ymin, zmin), (xmax, ymax, zmax) = k.bounds(framed)
    face_found = False
    for face in k.faces(framed):
        (fx0, fy0, fz0), (fx1, fy1, fz1) = k.bounds(face)
        if abs(fz0) <= PORT_FACE_TOLERANCE_MM and abs(fz1) <= PORT_FACE_TOLERANCE_MM \
                and fx0 < 0 < fx1 and fy0 < 0 < fy1:
            face_found = True
            break
    beyond = zmax
    return {
        "beyond_port_plane_mm": round(beyond, 9),
        "port_face_found": face_found,
        "lateral_extent_mm": [round(xmin, 6), round(xmax, 6), round(ymin, 6), round(ymax, 6)],
        "ok": face_found and abs(beyond) <= PORT_FACE_TOLERANCE_MM,
    }


# --- Expressions and authored recipes (shared pure implementation) ----------


def evaluate(text: str, names: dict) -> float:
    """Evaluate a bounded arithmetic expression; trigonometric functions use degrees."""
    try:
        return _evaluate(text, names)
    except DefinitionError as exc:
        raise RecipeError(str(exc)) from None


def resolve_recipe(recipe: dict, overrides: dict | None = None) -> dict:
    """Validate an authored recipe and evaluate every expression.

    ``overrides`` maps parameter names to values in the parameter's declared
    unit. Returns a new document whose parameters record their resolved value
    and whether it came from the recipe default or an override.
    """
    try:
        return _shared.resolve_recipe(recipe, overrides, materials=MATERIALS)
    except DefinitionError as exc:
        raise RecipeError(str(exc)) from None


def compile_recipe(resolved: dict) -> dict:
    """Build every component of a resolved recipe and check its ports."""
    compiled = []
    for index, component in enumerate(resolved["components"]):
        shape, count = compile_component(component, f"{resolved['id']}/{component['key']}")
        compiled.append({"component": component, "shape": shape, "solid_count": count})
    by_key = {item["component"]["key"]: item for item in compiled}
    port_checks = {}
    for name, port in resolved["ports"].items():
        item = by_key[port["component"]]
        if item["component"]["display_only"]:
            raise RecipeError(f"port {name!r} is on display-only component {port['component']!r}")
        report = port_face_report(item["shape"], port["position_mm"], port["direction"])
        if not report["ok"]:
            raise RecipeError(f"port {name!r} does not lie on an outward planar face of {port['component']!r}: "
                              f"{report}")
        port_checks[name] = report
    return {"components": compiled, "port_checks": port_checks}
