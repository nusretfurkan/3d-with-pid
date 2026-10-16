"""Appearance-only material palette for display meshes and STEP colours.

Colours, metalness and roughness are presentation choices. They assert no
material of construction, coating or finish; the recipe assumption
``rcp-appearance`` records this for every component.
"""

from __future__ import annotations

__all__ = ["MATERIALS", "material"]

MATERIALS = {
    "pump_paint": {"color": "#1f4f8f", "metalness": 0.15, "roughness": 0.38},
    "motor_paint": {"color": "#6b7378", "metalness": 0.2, "roughness": 0.45},
    "guard_paint": {"color": "#f0b020", "metalness": 0.1, "roughness": 0.4},
    "base_paint": {"color": "#2e3338", "metalness": 0.2, "roughness": 0.55},
    "machined_steel": {"color": "#b4b9bd", "metalness": 0.9, "roughness": 0.28},
    "fastener_zinc": {"color": "#9aa1a7", "metalness": 0.85, "roughness": 0.38},
    "pipe_steel": {"color": "#8d969e", "metalness": 0.6, "roughness": 0.42},
    "vessel_paint": {"color": "#d3d7d5", "metalness": 0.1, "roughness": 0.5},
    "valve_body": {"color": "#4b545c", "metalness": 0.35, "roughness": 0.45},
    "handwheel": {"color": "#2b3035", "metalness": 0.3, "roughness": 0.45},
    "gauge_case": {"color": "#23272b", "metalness": 0.4, "roughness": 0.35},
    "gauge_dial": {"color": "#f3f2ec", "metalness": 0.0, "roughness": 0.6},
    "boundary_marker": {"color": "#7d8a97", "metalness": 0.0, "roughness": 0.7},
    "pending_route": {"color": "#c92a2a", "metalness": 0.0, "roughness": 0.6},
}


def material(name: str) -> dict:
    """Return a copy of a palette entry; unknown names raise ``KeyError``."""
    if name not in MATERIALS:
        raise KeyError(f"unknown material {name!r}; known: {', '.join(sorted(MATERIALS))}")
    return dict(MATERIALS[name])


def rgb(name: str) -> tuple[float, float, float]:
    """sRGB components in [0, 1] for STEP colour records."""
    color = MATERIALS[name]["color"]
    return tuple(int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
