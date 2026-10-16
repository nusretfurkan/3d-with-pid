"""Recipes for connection routes in world millimetres.

A current route becomes one swept pipe solid through its exact points, a
mating flange at each end whose joint face lies on the port, and stud bolting
through each flanged joint. A route that is not current never becomes piping:
its retained points (if any) yield one display-only ghost component that is
excluded from CAD.
"""

from __future__ import annotations

import math

from .equipment import BOLTING_DN50, FLANGE_DN50
from .recipes import RecipeError

__all__ = ["BEND_RADIUS_FACTOR", "pending_recipe", "route_recipe"]

GENERATOR = "skid.geometry.routes"
BEND_RADIUS_FACTOR = 1.5
"""Bend centreline radius as a multiple of the nominal diameter."""

_FLANGES = {50: FLANGE_DN50}
_BOLTING = {50: BOLTING_DN50}


def _mm(point):
    return [round(c * 1000.0, 9) + 0.0 for c in point]


def route_recipe(connection, route, ends) -> dict:
    """Recipe of a current route; ``ends`` maps ``from``/``to`` to world port frames (metres)."""
    dn = connection["nominal_diameter_mm"]
    if dn not in _FLANGES:
        raise RecipeError(f"no flange geometry for DN {dn}")
    flange, bolting = _FLANGES[dn], _BOLTING[dn]
    radius = connection["outside_diameter_m"] * 500.0
    points = [_mm(p) for p in route["points_m"]]
    cited = sorted({a for a in (connection.get("assumption_id"), route.get("assumption_id")) if a})
    components = [{
        "key": "pipe", "material": "pipe_steel", "display_only": False,
        "assumptions": cited + ["rcp-appearance", "rcp-route-pipe"],
        "parts": [{"part": "pipe", "points": points, "radius": round(radius, 9),
                   "bend_radius": BEND_RADIUS_FACTOR * dn}],
    }]
    for end in ("from", "to"):
        frame = ends[end]
        face = _mm(frame["position_m"])
        outward = list(frame["direction"])
        inward = [-c for c in outward]
        components.append({
            "key": f"flange/{end}", "material": "pipe_steel", "display_only": False,
            "assumptions": cited + ["rcp-appearance", "rcp-flange-dn50", "rcp-route-joints"],
            "parts": [{"part": "flange", "face_center": face, "normal": inward, "bore_radius": round(radius, 9),
                       "hub_radius": round(radius + 12.0, 9), "neck_radius": round(radius + 3.0, 9),
                       "hub_length": 30.0, **flange}],
        })
        components.append({
            "key": f"bolting/{end}", "material": "fastener_zinc", "display_only": False,
            "assumptions": cited + ["rcp-appearance", "rcp-route-joints"],
            "parts": [{"part": "bolting", "face_center": face, "normal": outward,
                       "thickness_back": flange["thickness"], "thickness_front": flange["thickness"],
                       "bolt_count": flange["bolt_count"], "bolt_circle_radius": flange["bolt_circle_radius"],
                       "phase_deg": flange["phase_deg"], **bolting}],
        })
    return {"generator": f"{GENERATOR}/flanged_pipe@1", "components": components,
            "recipe_assumptions": ["rcp-appearance", "rcp-flange-dn50", "rcp-route-joints", "rcp-route-pipe"]}


def pending_recipe(connection, route) -> dict | None:
    """Display-only ghost through retained points of a non-current route, or ``None``."""
    points = [_mm(p) for p in route.get("points_m") or []]
    if len(points) < 2:
        return None
    radius = max(4.0, connection["outside_diameter_m"] * 500.0 * 0.3)
    parts_ = []
    for a, b in zip(points, points[1:]):
        if math.dist(a, b) > 1e-6:
            parts_.append({"part": "cylinder", "start": a, "end": b, "radius": radius})
    for p in points[1:-1]:
        parts_.append({"part": "sphere", "center": p, "radius": radius * 1.6})
    if not parts_:
        return None
    cited = sorted({a for a in (connection.get("assumption_id"),) if a})
    return {"generator": f"{GENERATOR}/pending_path@1", "components": [{
        "key": "pending/path", "material": "pending_route", "display_only": True,
        "assumptions": cited + ["rcp-appearance", "rcp-pending-route"], "parts": parts_}],
        "recipe_assumptions": ["rcp-appearance", "rcp-pending-route"]}
