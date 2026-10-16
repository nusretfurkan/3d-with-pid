"""Build an authored equipment recipe (``examples/recipes``) through the shared engine.

The recipe is resolved and its ports are proven to lie on outward flange
faces before anything is written. Its components then pass through the same
compiler, tessellation, preview, STEP export and reimport verification as the
project skid, as one item at the origin. The identity is
``project_id = "recipe:<id>"``, revision 0 and the SHA-256 of the resolved
recipe. This demonstrates bounded composition from the existing part
vocabulary; it is not general equipment generation.
"""

from __future__ import annotations

import hashlib
import json
import os

from .build import _Events, build_units
from .recipes import compile_recipe, resolve_recipe

__all__ = ["build_authored", "load_and_resolve"]

_IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))


def load_and_resolve(path, overrides=None) -> dict:
    with open(path, encoding="utf-8") as handle:
        recipe = json.load(handle)
    return resolve_recipe(recipe, overrides)


def build_authored(path, output_dir, overrides=None, emit=None, cancelled=None) -> dict:
    """Resolve, check and build an authored recipe into ``output_dir``."""
    if not os.path.isdir(output_dir):
        raise ValueError(f"output_dir {output_dir!r} must be an existing directory")
    resolved = load_and_resolve(path, overrides)
    checks = compile_recipe(resolved)["port_checks"]
    digest = hashlib.sha256(json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    unit_id = f"recipe:{resolved['id']}"
    identity = {"project_id": unit_id, "source_revision": 0, "input_digest": digest}
    ports = []
    for name, port in sorted(resolved["ports"].items()):
        ports.append({
            "id": f"{unit_id}/{name}", "entity_id": unit_id, "name": name,
            "position_m": [round(c / 1000.0, 12) + 0.0 for c in port["position_mm"]],
            "direction": port["direction"], "local_position_mm": port["position_mm"],
            "local_direction": port["direction"], "nominal_diameter_mm": port["nominal_diameter_mm"],
            "component_key": port["component"],
        })
    unit = {
        "id": unit_id, "type": "entity", "kind": resolved["id"], "tag": resolved["title"],
        "template": None, "generator": f"skid.geometry.authored/{resolved['id']}", "status": "current",
        "parameters_mm": {name: p["value"] * 1000.0 for name, p in resolved["parameters"].items()},
        "placement": {"position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0], "assumption_id": None},
        "matrix_mm": [list(row) for row in _IDENTITY], "ports": ports, "components": resolved["components"],
        "envelope_checks": [],
    }
    records = {aid: dict(record, scope="recipe") for aid, record in resolved["assumptions"].items()}
    events = _Events(emit, cancelled, output_dir)
    display, cad = build_units([unit], identity, output_dir, events, assumption_records=records, diagnostics=[],
                               failures=[], representation_ids=[unit_id] + [p["id"] for p in ports],
                               provenance_kind="authored_recipe",
                               snapshot_source={"kind": "authored_recipe", "recipe": resolved, "port_checks": checks})
    return {"display": display, "cad": cad}
