"""Generate the display scene and STEP assembly of one project snapshot.

``build_geometry(project, output_dir, emit=None, cancelled=None)`` follows the
version 0.2 workflow contract (``docs/WORKFLOW_CONTRACTS.md``):

1. Validate the read-only snapshot with the core validator and digest it.
2. Plan one *unit* per placed entity (flow order first, then the rest) and
   per connection: a recipe of components in a local millimetre frame.
3. For each unit, compile the components into OCCT solids, place them, and
   tessellate exactly those shapes into world-metre Z-up meshes. After each
   unit an immutable ``preview/display-NNNN.json`` snapshot is written and a
   ``preview`` event emitted. ``cancelled()`` is checked between units.
4. Write ``scene.json`` and ``display.manifest.json`` once the scene is final.
5. Write ``geometry.recipe.json`` (the rebuildable source), export
   ``model.step`` from the same shapes, reimport it and verify names, counts,
   validity, volumes, bounds, placements, port faces, envelope dimensions and
   mesh bounds; then write ``cad.verification.json`` and ``cad.manifest.json``.

``rebuild_from_recipe`` runs steps 3-5 from a saved ``geometry.recipe.json``
without the project, after ``snapshot.load_snapshot`` has verified the
snapshot's ``recipe_sha256`` content digest and its compatibility with this
generator. ``build_units`` is the shared engine.

Every output carries a ``provenance`` record (scene, previews, both
manifests, ``cad.verification.json``) and the STEP header repeats it:
``kind`` is ``project_build`` (authoritative build of a validated project),
``snapshot_rebuild`` (replay of an unchanged generated snapshot, not
regenerated from the project) or ``authored_recipe``; ``recipe_sha256`` is
the content digest of the ``geometry.recipe.json`` beside the outputs;
``source`` is the identity the snapshot claims (for projects, the canonical
project ``input_digest``). The manifest identity fields keep their contract
meaning, so service builds are unchanged.
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import time

from skid.core.validation import canonical_digest, validate_project
from skid.equipment import is_composed
from skid.templates import world_port_position

from . import kernel
from .composed import composed_recipe, definition_assumption_records
from .equipment import RECIPE_ASSUMPTIONS, entity_recipe
from .materials import MATERIALS, material, rgb
from .parts import PartError
from .recipes import RecipeError, compile_component, port_face_report
from .routes import pending_recipe, route_recipe
from .snapshot import DIGEST_FIELD, SNAPSHOT_SCHEMA, SnapshotError, describe, load_snapshot, with_digest
from .step import StepError, header_facts, read_step, write_step

__all__ = [
    "ADAPTER_VERSION",
    "ANGULAR_DEFLECTION_RAD",
    "LINEAR_DEFLECTION_MM",
    "SnapshotError",
    "build_geometry",
    "build_units",
    "conventions",
    "plan_units",
    "rebuild_from_recipe",
]

ADAPTER_VERSION = "0.2.0"
GENERATOR = "skid.geometry"
SCENE_SCHEMA_VERSION = 1
LINEAR_DEFLECTION_MM = 0.5
ANGULAR_DEFLECTION_RAD = 0.35
POSITION_DIGITS = 5
NORMAL_DIGITS = 3
WORLD_CONVENTIONS = {"units": "m", "up_axis": "Z", "handedness": "right",
                     "rotation": "XYZ fixed axes, R = Rz @ Ry @ Rx", "kernel_unit": "mm",
                     "scene_conversion": "metres = millimetres / 1000"}
TESSELLATION = {"linear_deflection_mm": LINEAR_DEFLECTION_MM, "angular_deflection_rad": ANGULAR_DEFLECTION_RAD,
                "position_digits_m": POSITION_DIGITS, "normal_digits": NORMAL_DIGITS}
PROVENANCE_STATEMENTS = {
    "project_build": "Generated from the validated project identified by source (input_digest is the canonical "
                     "project digest). recipe_sha256 is the content digest of the geometry.recipe.json written with "
                     "these outputs, not a project digest.",
    "snapshot_rebuild": "Replayed from an unchanged generated recipe snapshot whose recipe_sha256 was verified "
                        "before solid generation; not regenerated from the project. source is the identity the "
                        "snapshot records. The digest detects edits; it is not a signature and proves neither "
                        "authorship nor authenticity.",
    "authored_recipe": "Generated from an authored recipe; source.input_digest is the SHA-256 of the resolved "
                       "authored recipe, not a project digest. recipe_sha256 is the content digest of the generated "
                       "geometry.recipe.json.",
}
STEP_BOUNDS_TOLERANCE_MM = 1e-3
STEP_VOLUME_RELATIVE_TOLERANCE = 1e-6
PLACEMENT_TOLERANCE_MM = 1e-6
ENVELOPE_TOLERANCE_MM = 1e-3
MESH_INSIDE_TOLERANCE_MM = 0.01
MESH_REACH_TOLERANCE_MM = 2 * LINEAR_DEFLECTION_MM + 0.01
PORT_TOLERANCE_M = 1e-9

# Validator warnings about P&ID presentation do not concern 3D geometry; the
# geometry builder reports placement and route warnings itself.
PID_WARNINGS = frozenset({"edge_not_current", "symbol_missing", "edge_missing"})
OWN_WARNINGS = frozenset({"placement_unresolved", "placement_missing", "route_not_current", "route_missing"})

DISPLAY_FILES = ("scene.json",)
CAD_FILES = ("model.step", "geometry.recipe.json", "cad.verification.json")


class _Failed(Exception):
    def __init__(self, diagnostics):
        super().__init__(diagnostics)
        self.diagnostics = diagnostics


def _diag(code, severity, ids, message):
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]
    return {"code": code, "severity": severity, "entity_ids": ids, "message": message}


def component_id(semantic_id, key):
    return f"{semantic_id}#{key}"


def conventions() -> dict:
    """The generator, unit, frame and tessellation conventions a replayed snapshot must match exactly."""
    return {"generator": {"name": GENERATOR, "version": ADAPTER_VERSION}, "length_unit": "mm",
            "world": dict(WORLD_CONVENTIONS), "tessellation": dict(TESSELLATION)}


def _provenance(kind, identity, recipe_sha256, snapshot=None):
    record = {
        "kind": kind,
        "recipe_sha256": recipe_sha256,
        "recipe_file": "geometry.recipe.json" if recipe_sha256 else None,
        "source": {key: identity[key] for key in ("project_id", "source_revision", "input_digest")},
        "statement": PROVENANCE_STATEMENTS[kind],
    }
    if snapshot is not None:
        record.update(describe(snapshot))
    return record


def _step_header(identity, provenance):
    rebuild = provenance["kind"] == "snapshot_rebuild"
    return {
        "name": f"{identity['project_id']} revision {identity['source_revision']}"
                + (" (snapshot rebuild)" if rebuild else ""),
        "description": f"{GENERATOR} {ADAPTER_VERSION}; provenance={provenance['kind']}; "
                       f"recipe_sha256={provenance['recipe_sha256']}; source_project={identity['project_id']}; "
                       f"source_revision={identity['source_revision']}; "
                       f"source_input_digest={identity['input_digest']}; length_unit=mm; up_axis=+Z; "
                       f"illustrative prototype geometry",
        "author": GENERATOR, "organization": "Equipment 3D and PID prototype",
        "originating_system": f"{GENERATOR} {ADAPTER_VERSION} (OCCT via OCP)",
    }


# --- Files and events ---------------------------------------------------------


def _dumps(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _write_atomic(path, text: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _write_immutable(path, text: str) -> None:
    """Publish a new file atomically; refuse to replace an existing one."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


class _Events:
    def __init__(self, emit, cancelled, output_dir):
        if emit is not None and not callable(emit):
            raise TypeError("emit must be callable or None")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        self.emit_fn, self.cancelled_fn, self.output_dir = emit, cancelled, output_dir

    def emit(self, kind, artifact, message, path=None, completed=None, total=None):
        if self.emit_fn is None:
            return
        event = {"kind": kind, "artifact": artifact, "message": message}
        if path is not None:
            if not os.path.exists(os.path.join(self.output_dir, path)):
                raise RuntimeError(f"refusing to emit {path!r} before it exists")
            event["path"] = path
        if completed is not None:
            event["completed"], event["total"] = completed, total
        self.emit_fn(event)

    def check(self, before):
        if self.cancelled_fn is not None and self.cancelled_fn():
            raise InterruptedError(f"geometry build cancelled before {before}")


# --- Planning -----------------------------------------------------------------


def _flow_order(project):
    """Entity IDs in process-flow order (topological over connections), then the rest by ID."""
    model = project["model"]
    owner = {pid: port["entity_id"] for pid, port in model["ports"].items()}
    successors = {eid: set() for eid in model["entities"]}
    indegree = {eid: 0 for eid in model["entities"]}
    for connection in model["connections"].values():
        a, b = owner.get(connection["from_port_id"]), owner.get(connection["to_port_id"])
        if a in successors and b in successors and b not in successors[a] and a != b:
            successors[a].add(b)
            indegree[b] += 1
    connected = {eid for eid in successors if successors[eid] or indegree[eid]}
    ready = sorted(eid for eid in connected if indegree[eid] == 0)
    order = []
    while ready:
        eid = ready.pop(0)
        order.append(eid)
        for nxt in sorted(successors[eid]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    order += sorted(eid for eid in connected if eid not in order)
    order += sorted(eid for eid in model["entities"] if eid not in connected)
    return order


def _port_frames_world(project, entity_id, recipe, matrix_mm):
    """World port frames, checked against ``skid.templates.world_port_position``."""
    frames = {}
    model = project["model"]
    for pid in sorted(model["ports"]):
        port = model["ports"][pid]
        if port["entity_id"] != entity_id:
            continue
        local = recipe["ports"].get(port["name"])
        if local is None:
            raise RecipeError(f"recipe has no frame for port {pid!r} ({port['name']})")
        position_mm = kernel.transform_point(matrix_mm, local["position_mm"])
        direction = kernel.transform_vector(matrix_mm, local["direction"])
        position_m = [round(c / 1000.0, 12) + 0.0 for c in position_mm]
        expected = world_port_position(project, pid)
        if expected is None or math.dist(expected, position_m) > PORT_TOLERANCE_M * 1000:
            raise RecipeError(f"port {pid!r} frame {position_m} disagrees with the template position {expected}")
        frames[pid] = {
            "id": pid, "entity_id": entity_id, "name": port["name"], "position_m": list(expected),
            "direction": [round(c, 12) + 0.0 for c in direction], "local_position_mm": local["position_mm"],
            "local_direction": local["direction"], "nominal_diameter_mm": port["nominal_diameter_mm"],
            "component_key": local.get("component", f"nozzle/{port['name']}"),
        }
    return frames


def plan_units(project) -> tuple[list, list, list]:
    """Return ``(units, diagnostics, failures)`` for a validated project copy.

    Units are entities in flow order then connections; each carries its
    components. ``failures`` are error diagnostics of units whose recipes
    could not be produced.
    """
    model, physical = project["model"], project["physical"]
    units, diagnostics, failures = [], [], []
    port_frames = {}
    reserved = set(model["assumptions"]) | set(RECIPE_ASSUMPTIONS)
    for eid in _flow_order(project):
        entity = model["entities"][eid]
        placement = physical["placements"].get(eid)
        if placement is None or placement.get("status") != "placed":
            reason = "no physical placement record" if placement is None else (
                placement.get("reason") or "placement unresolved")
            diagnostics.append(_diag("geometry_omitted", "warning", [eid],
                                     f"{entity['tag']} ({eid}) has no resolved placement, so no 3D or CAD geometry "
                                     f"was generated: {reason}"))
            continue
        composed = is_composed(entity)
        try:
            recipe = composed_recipe(entity, reserved) if composed else entity_recipe(entity)
            matrix = kernel.placement_matrix(placement["position_m"], placement["rotation_deg"], 1000.0)
            frames = _port_frames_world(project, eid, recipe, matrix)
        except (RecipeError, PartError, kernel.KernelError, KeyError, ValueError) as exc:
            failures.append(_diag("geometry_recipe_failed", "error", [eid],
                                  f"{entity['tag']} ({eid}): no geometry recipe ({exc})"))
            continue
        port_frames.update(frames)
        cited = [placement["assumption_id"]] if placement.get("assumption_id") else []
        for component in recipe["components"]:
            component["assumptions"] = sorted(set(component["assumptions"]) | set(cited))
        unit = {
            "id": eid, "type": "entity", "kind": entity["kind"], "tag": entity["tag"],
            "template": {"kind": entity["kind"], "version": entity["template_version"]},
            "generator": recipe["generator"], "status": "current",
            "parameters_mm": recipe["parameters_mm"],
            "placement": {"position_m": list(placement["position_m"]), "rotation_deg": list(placement["rotation_deg"]),
                          "assumption_id": placement.get("assumption_id")},
            "matrix_mm": [list(row) for row in matrix],
            "ports": list(frames.values()),
            "components": recipe["components"],
            "envelope_checks": _envelope_checks(entity["kind"], recipe),
        }
        if composed:
            unit["definition"] = recipe["definition"]  # what a snapshot needs to check its resolved components
        units.append(unit)
    order = {eid: i for i, eid in enumerate(_flow_order(project))}
    owner = {pid: port["entity_id"] for pid, port in model["ports"].items()}

    def rank(cid):
        return (order.get(owner.get(model["connections"][cid]["from_port_id"]), len(order)), cid)

    for cid in sorted(model["connections"], key=rank):
        connection = model["connections"][cid]
        route = physical["routes"].get(cid)
        tag = connection["tag"]
        if route is None:
            diagnostics.append(_diag("route_omitted", "warning", [cid],
                                     f"{tag} ({cid}) has no physical route record; no piping was generated"))
            continue
        ends = {"from": port_frames.get(connection["from_port_id"]), "to": port_frames.get(connection["to_port_id"])}
        if route["status"] == "current" and all(ends.values()):
            try:
                recipe = route_recipe(connection, route, ends)
            except (RecipeError, KeyError, ValueError) as exc:
                failures.append(_diag("geometry_recipe_failed", "error", [cid], f"{tag} ({cid}): {exc}"))
                continue
            status = "current"
        else:
            reason = route.get("reason") or ("an endpoint has no generated geometry" if route["status"] == "current"
                                             else route["status"])
            recipe = pending_recipe(connection, route)
            what = "shown as a display-only pending path" if recipe else "omitted"
            diagnostics.append(_diag("route_pending", "warning", [cid],
                                     f"{tag} ({cid}) route is {route['status']}; it is {what}, not current piping "
                                     f"and not in CAD: {reason}"))
            if recipe is None:
                continue
            status = "pending"
        units.append({
            "id": cid, "type": "connection", "kind": "connection", "tag": tag, "template": None,
            "generator": recipe["generator"], "status": status,
            "points_m": [list(p) for p in route["points_m"]],
            "route": {"mode": route["mode"], "status": route["status"], "assumption_id": route.get("assumption_id")},
            "from_port_id": connection["from_port_id"], "to_port_id": connection["to_port_id"],
            "nominal_diameter_mm": connection["nominal_diameter_mm"],
            "outside_diameter_m": connection["outside_diameter_m"],
            "matrix_mm": None, "ports": [], "components": recipe["components"], "envelope_checks": [],
        })
    return units, diagnostics, failures


def _envelope_checks(kind, recipe):
    """Dimension checks in the entity frame that both source and reimported CAD must pass."""
    p = recipe["parameters_mm"]
    if kind == "centrifugal_pump":
        L, W, H, ML, R = p["body_length"], p["body_width"], p["body_height"], p["motor_length"], p["motor_diameter"] / 2
        zc = H / 2
        inside = ["body", "bearing_frame", "support", "shaft", "coupling", "coupling_guard"]
        return [
            {"name": "motor frame spans motor_length and reaches motor_diameter", "components": ["motor"],
             "mode": "equal", "bounds_mm": {"xmin": L / 2, "xmax": L / 2 + ML, "ymin": -R, "ymax": R, "zmax": zc + R}},
            {"name": "casing reaches the suction face and body top", "components": ["body"], "mode": "equal",
             "bounds_mm": {"xmin": -L / 2, "zmax": H}},
            {"name": "pump end stays inside the body envelope", "components": inside, "mode": "within",
             "bounds_mm": {"xmin": -L / 2, "xmax": L / 2 + 2.0, "ymin": -W / 2, "ymax": W / 2, "zmin": 0.0,
                           "zmax": H}},
        ]
    if kind == "tank":
        return [{"name": "tank body spans its height", "components": ["body"], "mode": "equal",
                 "bounds_mm": {"zmin": 0.0, "zmax": p["height"]}}]
    if kind in ("isolation_valve", "check_valve"):
        r = p["body_diameter"] / 2
        return [{"name": "valve body within body_diameter", "components": ["body"], "mode": "equal",
                 "bounds_mm": {"ymin": -r, "ymax": r, "zmin": -r, "zmax": r}},
                {"name": "end flanges at the face-to-face length", "components": ["nozzle/inlet", "nozzle/outlet"],
                 "mode": "equal", "bounds_mm": {"xmin": -p["length"] / 2, "xmax": p["length"] / 2}}]
    if kind == "indicator":
        d = p["diameter"]
        return [{"name": "dial case diameter", "components": ["body"], "mode": "equal",
                 "bounds_mm": {"xmin": -d / 2, "xmax": d / 2, "zmin": d / 2, "zmax": 1.5 * d}}]
    return []


def _check_envelope(check, local_bounds):
    lows, highs = [], []
    for key in check["components"]:
        lo, hi = local_bounds[key]
        lows.append(lo)
        highs.append(hi)
    lo = [min(v[i] for v in lows) for i in range(3)]
    hi = [max(v[i] for v in highs) for i in range(3)]
    actual = {"xmin": lo[0], "ymin": lo[1], "zmin": lo[2], "xmax": hi[0], "ymax": hi[1], "zmax": hi[2]}
    failures = []
    for name, expected in check["bounds_mm"].items():
        value = actual[name]
        if check["mode"] == "equal":
            bad = abs(value - expected) > ENVELOPE_TOLERANCE_MM
        elif name.endswith("min"):
            bad = value < expected - ENVELOPE_TOLERANCE_MM
        else:
            bad = value > expected + ENVELOPE_TOLERANCE_MM
        if bad:
            failures.append(f"{name} {value:.6f} mm vs {check['mode']} {expected:.6f} mm")
    return {"name": check["name"], "ok": not failures, "failures": failures,
            "actual_mm": {k: round(v, 6) for k, v in actual.items() if k in check["bounds_mm"]}}


# --- Compilation and tessellation --------------------------------------------


def _round_flat(values, scale, digits):
    return [round(v * scale, digits) + 0.0 for v in values]


def _compile_unit(unit):
    compiled = []
    matrix = unit["matrix_mm"]
    for component in unit["components"]:
        cid = component_id(unit["id"], component["key"])
        local, solids = compile_component(component, cid)
        world = kernel.located(local, matrix) if matrix else local
        positions, normals, indices = kernel.mesh(world, LINEAR_DEFLECTION_MM, ANGULAR_DEFLECTION_RAD)
        if not indices:
            raise RecipeError(f"{cid}: tessellation produced no triangles")
        pos_m = _round_flat(positions, 0.001, POSITION_DIGITS)
        nrm = _round_flat(normals, 1.0, NORMAL_DIGITS)
        lo = [min(pos_m[i::3]) for i in range(3)]
        hi = [max(pos_m[i::3]) for i in range(3)]
        record = {
            "id": cid, "semantic_id": unit["id"], "component_key": component["key"], "tag": unit["tag"],
            "kind": unit["kind"], "positions": pos_m, "normals": nrm, "indices": indices,
            "material": material(component["material"]), "material_key": component["material"],
            "bounds_m": {"min": lo, "max": hi}, "display_only": component["display_only"],
            "status": unit["status"], "cad_part": None if component["display_only"] else cid,
            "assumptions": component["assumptions"],
        }
        compiled.append({"component": component, "id": cid, "local": local, "world": world, "solids": solids,
                         "record": record, "json": _dumps(record), "triangles": len(indices) // 3})
    return compiled


def _union_bounds(records):
    if not records:
        return None
    lo = [min(r["bounds_m"]["min"][i] for r in records) for i in range(3)]
    hi = [max(r["bounds_m"]["max"][i] for r in records) for i in range(3)]
    return {"min": lo, "max": hi}


def _label(unit, compiled):
    records = [c["record"] for c in compiled if not c["record"]["display_only"]] or [c["record"] for c in compiled]
    bounds = _union_bounds(records)
    if unit["type"] == "entity":
        anchor = [(bounds["min"][0] + bounds["max"][0]) / 2, (bounds["min"][1] + bounds["max"][1]) / 2,
                  bounds["max"][2] + 0.06]
    else:
        points = unit["points_m"]
        best = max(range(len(points) - 1), key=lambda i: (round(math.dist(points[i], points[i + 1]), 9), -i))
        a, b = points[best], points[best + 1]
        anchor = [(a[i] + b[i]) / 2 for i in range(3)]
        anchor[2] += unit["outside_diameter_m"] / 2 + 0.05
    return {"semantic_id": unit["id"], "text": unit["tag"], "kind": unit["type"],
            "status": unit["status"], "anchor_m": [round(c, POSITION_DIGITS) + 0.0 for c in anchor]}


# --- The shared engine --------------------------------------------------------


class _Scene:
    """Accumulates compiled units and serializes scene snapshots cheaply."""

    def __init__(self, identity, assumption_records):
        self.identity = identity
        self.assumption_records = assumption_records
        self.units, self.labels, self.ports = [], [], []

    def add(self, unit, compiled):
        self.units.append((unit, compiled))
        self.labels.append(_label(unit, compiled))
        self.ports += [{k: p[k] for k in ("id", "entity_id", "name", "position_m", "direction",
                                          "nominal_diameter_mm")} | {"component_id": component_id(unit["id"],
                                                                                                  p["component_key"])}
                       for p in unit["ports"]]

    def records(self):
        return [c["record"] for _, compiled in self.units for c in compiled]

    def assumptions(self):
        applies = {}
        for _, compiled in self.units:
            for c in compiled:
                for aid in c["record"]["assumptions"]:
                    applies.setdefault(aid, []).append(c["id"])
        out = []
        for aid in sorted(applies):
            record = self.assumption_records.get(aid, {})
            out.append({"id": aid, "scope": record.get("scope", "unknown"), "category": record.get("category"),
                        "description": record.get("description"), "source": record.get("source"),
                        "applies_to": applies[aid]})
        return out

    def text(self, status, diagnostics, extra=None):
        records = self.records()
        bounds = _union_bounds(records) or {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
        head = dict(self.identity)
        head.update({
            "schema_version": SCENE_SCHEMA_VERSION, "units": "m", "up_axis": "Z", "handedness": "right",
            "generator": {"name": GENERATOR, "version": ADAPTER_VERSION},
            "status": status, "components": "\u0000COMPONENTS\u0000", "ports": self.ports,
            "assumptions": self.assumptions(), "bounds_m": bounds, "labels": self.labels,
            "diagnostics": diagnostics,
            "triangle_count": sum(c["triangles"] for _, compiled in self.units for c in compiled),
        })
        head.update(extra or {})
        text = _dumps(head)
        joined = ",".join(c["json"] for _, compiled in self.units for c in compiled)
        # Quotes inside JSON strings are escaped, so this key-value text occurs exactly once.
        placeholder = '"components":"\\u0000COMPONENTS\\u0000"'
        if text.count(placeholder) != 1:
            raise RuntimeError("scene serialization placeholder not unique")
        return text.replace(placeholder, '"components":[' + joined + "]", 1)


def build_units(units, identity, output_dir, events, *, assumption_records, diagnostics, failures,
                representation_ids, provenance_kind="project_build", snapshot_source=None, snapshot=None):
    """Compile units progressively, then publish the scene, recipe snapshot and verified STEP.

    Returns ``(display_manifest, cad_manifest)``. ``identity`` holds
    ``project_id``, ``source_revision`` and ``input_digest``. Fresh builds
    pass ``snapshot_source`` and write a new snapshot with its content
    digest; a rebuild passes the already verified ``snapshot``, which is
    written back unchanged so its ``recipe_sha256`` labels the outputs.
    """
    if (snapshot is None) == (snapshot_source is None):
        raise ValueError("give exactly one of snapshot_source (fresh build) or snapshot (rebuild)")
    started = time.monotonic()
    timings = {}
    total = len(units)
    scene = _Scene(identity, assumption_records)
    preview_dir = os.path.join(output_dir, "preview")
    os.makedirs(preview_dir, exist_ok=True)
    previews = []
    failures = list(failures)
    # A fresh build's digest exists only once every unit has compiled; a rebuild's is known up front.
    provenance = _provenance(provenance_kind, identity, snapshot[DIGEST_FIELD] if snapshot else None, snapshot)
    label = "Rebuilding snapshot" if provenance_kind == "snapshot_rebuild" else "Building geometry"
    events.emit("activity", "display", f"{label} for revision {identity['source_revision']}: "
                f"{sum(u['type'] == 'entity' for u in units)} equipment items, "
                f"{sum(u['type'] == 'connection' for u in units)} routes", completed=0, total=total)
    for index, unit in enumerate(units, 1):
        noun = "route" if unit["type"] == "connection" else unit["kind"].replace("_", " ")
        events.check(f"{unit['tag']} ({unit['id']})")
        events.emit("activity", "display", f"Building solids for {unit['tag']} ({noun})", completed=index - 1,
                    total=total)
        t0 = time.monotonic()
        try:
            compiled = _compile_unit(unit)
        except (RecipeError, PartError, kernel.KernelError) as exc:
            failures.append(_diag("geometry_build_failed", "error", [unit["id"]],
                                  f"{unit['tag']} ({unit['id']}): {exc}"))
            continue
        timings[unit["id"]] = round(time.monotonic() - t0, 4)
        scene.add(unit, compiled)
        name = f"preview/display-{len(previews) + 1:04d}.json"
        extra = {"preview": {"sequence": len(previews) + 1, "completed": index, "total": total,
                             "unit_id": unit["id"], "final": False}, "provenance": provenance}
        _write_immutable(os.path.join(output_dir, name), scene.text("working", diagnostics + failures, extra))
        previews.append(name)
        triangles = sum(c["triangles"] for c in compiled)
        state = "pending path" if unit["status"] == "pending" else f"{len(compiled)} components"
        events.emit("preview", "display", f"{unit['tag']} ready: {state}, {triangles:,} triangles", path=name,
                    completed=index, total=total)
    timings["display_s"] = round(time.monotonic() - started, 3)

    warnings = [d for d in diagnostics if d["severity"] == "warning"]
    failed = bool(failures)
    status = "failed" if failed else ("pending" if warnings else "complete")
    all_diagnostics = diagnostics + failures
    representations = {sid: [] for sid in representation_ids}
    cad_representations = {sid: [] for sid in representation_ids}
    for unit, compiled in scene.units:
        for c in compiled:
            representations.setdefault(unit["id"], []).append(c["id"])
            if not c["record"]["display_only"]:
                cad_representations.setdefault(unit["id"], []).append(c["id"])
        for port in unit["ports"]:
            cid = component_id(unit["id"], port["component_key"])
            representations.setdefault(port["id"], []).append(cid)
            cad_representations.setdefault(port["id"], []).append(cid)
    triangle_total = sum(c["triangles"] for _, compiled in scene.units for c in compiled)
    display_files = list(previews)
    document = None
    if failed:
        provenance = dict(provenance, recipe_sha256=None, recipe_file=None)
    else:
        document = snapshot if snapshot is not None else _snapshot_recipe(
            scene, identity, assumption_records, all_diagnostics, representation_ids, snapshot_source)
        provenance = _provenance(provenance_kind, identity, document[DIGEST_FIELD], snapshot)
        _write_atomic(os.path.join(output_dir, "scene.json"),
                      scene.text(status, all_diagnostics, {"provenance": provenance}))
        display_files = ["scene.json"] + previews
    display = _manifest("display", identity, status, all_diagnostics, representations, display_files, {
        "component_count": len(scene.records()), "triangle_count": triangle_total, "preview_count": len(previews),
        "tessellation": dict(TESSELLATION), "units": "m", "up_axis": "Z", "timings_s": dict(timings),
        "provenance": provenance,
    })
    _write_atomic(os.path.join(output_dir, "display.manifest.json"), _dumps(display))
    if failed:
        events.emit("activity", "display", f"Geometry failed for {len(failures)} item(s); no final scene written")
        cad = _manifest("cad", identity, "failed", all_diagnostics + [_diag(
            "cad_not_exported", "error", [identity["project_id"]], "CAD was not exported because geometry failed")],
            {}, [], {"provenance": provenance})
        _write_atomic(os.path.join(output_dir, "cad.manifest.json"), _dumps(cad))
        return display, cad
    events.emit("artifact", "display", f"3D scene ready: {len(scene.records())} components, "
                f"{triangle_total:,} triangles ({status})", path="scene.json", completed=total, total=total)

    # CAD: the rebuildable snapshot, then STEP from the same shapes, then reimport.
    cad_started = time.monotonic()
    _write_atomic(os.path.join(output_dir, "geometry.recipe.json"), json.dumps(document, indent=1, sort_keys=True))
    groups, part_total, solid_total = [], 0, 0
    for unit, compiled in scene.units:
        parts_ = [{"name": c["id"], "shape": c["local"], "rgb": rgb(c["component"]["material"])}
                  for c in compiled if not c["record"]["display_only"]]
        if parts_:
            groups.append({"name": unit["id"], "matrix": unit["matrix_mm"], "parts": parts_})
            part_total += len(parts_)
            solid_total += sum(c["solids"] for c in compiled if not c["record"]["display_only"])
    events.check("STEP export")
    events.emit("activity", "cad", f"Exporting STEP assembly: {len(groups)} items, {part_total} parts, "
                f"{solid_total} solids (mm)")
    step_path = os.path.join(output_dir, "model.step")
    tmp_step = os.path.join(output_dir, ".model.step.tmp")
    header = _step_header(identity, provenance)
    try:
        write_step(tmp_step, header, identity["project_id"], groups)
        os.replace(tmp_step, step_path)
    except (StepError, OSError) as exc:
        if os.path.exists(tmp_step):
            os.unlink(tmp_step)
        return display, _cad_failed(output_dir, identity, all_diagnostics, f"STEP export failed: {exc}",
                                    provenance=provenance)
    timings["step_export_s"] = round(time.monotonic() - cad_started, 3)
    events.check("STEP verification")
    events.emit("activity", "cad", "Re-importing model.step to verify solids, placements, dimensions and ports")
    verify_started = time.monotonic()
    try:
        verification = verify_step(step_path, identity, scene, groups, header)
    except (StepError, kernel.KernelError, RecipeError, KeyError, ValueError, OSError) as exc:
        verification = {"ok": False, "problems": [f"STEP reimport failed: {type(exc).__name__}: {exc}"]}
    timings["step_verify_s"] = round(time.monotonic() - verify_started, 3)
    verification["timings_s"] = {k: v for k, v in timings.items() if k.startswith("step")}
    verification["provenance"] = provenance
    _write_atomic(os.path.join(output_dir, "cad.verification.json"), json.dumps(verification, indent=1, sort_keys=True))
    events.check("CAD publication")
    if not verification["ok"]:
        problems = verification["problems"]
        return display, _cad_failed(output_dir, identity, all_diagnostics,
                                    f"STEP reimport verification failed ({len(problems)} problem(s)): "
                                    f"{'; '.join(problems[:3])}", files=list(CAD_FILES), provenance=provenance)
    cad = _manifest("cad", identity, status, all_diagnostics, cad_representations, list(CAD_FILES), {
        "format": "STEP AP214 (OCCT XCAF assembly)", "length_unit": "mm", "up_axis": "Z",
        "group_count": len(groups), "part_count": part_total, "solid_count": solid_total,
        "verification": {"ok": True, "file": "cad.verification.json", "checks": verification["summary"]},
        "excluded_display_only": sorted(c["id"] for _, compiled in scene.units for c in compiled
                                        if c["record"]["display_only"]),
        "recipe": "geometry.recipe.json", "timings_s": dict(timings), "provenance": provenance,
        "step_header": {"name": header["name"], "description": header["description"]},
    })
    _write_atomic(os.path.join(output_dir, "cad.manifest.json"), _dumps(cad))
    rebuilt = " (snapshot rebuild)" if provenance_kind == "snapshot_rebuild" else ""
    contents = (f"{part_total} parts, {solid_total} solids" if groups else
                "no geometry to export, one named root product without parts or solids")
    events.emit("artifact", "cad", f"STEP verified on reimport{rebuilt}: {contents} ({status})", path="model.step")
    return display, cad


def _cad_failed(output_dir, identity, diagnostics, message, files=(), provenance=None):
    files = [f for f in files if os.path.exists(os.path.join(output_dir, f))]
    cad = _manifest("cad", identity, "failed",
                    diagnostics + [_diag("cad_failed", "error", [identity["project_id"]], message)], {}, files,
                    {"provenance": provenance})
    _write_atomic(os.path.join(output_dir, "cad.manifest.json"), _dumps(cad))
    return cad


def _manifest(adapter, identity, status, diagnostics, representations, files, extra):
    manifest = {
        "adapter": adapter, "adapter_version": ADAPTER_VERSION, "project_id": identity["project_id"],
        "source_revision": identity["source_revision"], "input_digest": identity["input_digest"],
        "status": status, "diagnostics": copy.deepcopy(diagnostics),
        "representations": {} if status == "failed" else representations, "files": list(files),
    }
    manifest.update(extra)
    return manifest


def _snapshot_recipe(scene, identity, assumption_records, diagnostics, representation_ids, source):
    """The normalized, rebuildable snapshot of a fresh build, carrying its content digest."""
    cited = {a["id"] for a in scene.assumptions()}
    return with_digest({
        "schema": SNAPSHOT_SCHEMA,
        "generator": {"name": GENERATOR, "version": ADAPTER_VERSION},
        **{key: identity[key] for key in ("project_id", "source_revision", "input_digest")},
        "length_unit": "mm",
        "world": dict(WORLD_CONVENTIONS),
        "tessellation": dict(TESSELLATION),
        "units_built": [dict(unit) for unit, _ in scene.units],
        "representation_ids": list(representation_ids),
        "assumptions": {aid: assumption_records[aid] for aid in sorted(cited) if aid in assumption_records},
        "materials": {name: MATERIALS[name] for name in sorted({c["component"]["material"]
                                                                for _, compiled in scene.units for c in compiled})},
        "diagnostics": copy.deepcopy(diagnostics),
        "rebuild": "python -m skid.geometry rebuild <geometry.recipe.json> <empty output directory>; replays "
                   "this unchanged snapshot only (recipe_sha256 is checked first)",
        "source": copy.deepcopy(source),
    })


# --- STEP verification ---------------------------------------------------------


def _bounds_delta(a, b):
    return max(max(abs(x - y) for x, y in zip(a[0], b[0])), max(abs(x - y) for x, y in zip(a[1], b[1])))


def _matrix_delta(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(3) for j in range(4))


def verify_step(step_path, identity, scene, groups, header) -> dict:
    """Reimport ``model.step`` and compare it with the source shapes, meshes and intended header."""
    with open(step_path, encoding="ascii", errors="replace") as handle:
        facts = header_facts(handle.read())
    data = read_step(step_path)
    problems = []
    if not facts["length_unit_millimetre"] or facts["length_unit_metre_plain"]:
        problems.append("STEP length unit is not declared as millimetre")
    if facts["description"] != header["description"]:
        problems.append(f"STEP header description {facts['description']!r} does not carry the intended provenance "
                        f"and source identity {header['description']!r}")
    if data["root"] != identity["project_id"]:
        problems.append(f"root assembly is named {data['root']!r}, not {identity['project_id']!r}")
    if data["loose_shapes"]:
        problems.append(f"{data['loose_shapes']} group(s) were not reimported as assemblies")
    # A project with nothing to export writes only its named root product; it must reimport as exactly that
    # (no components, no geometry), and a source with groups must never reimport as an empty root.
    if not groups and not data.get("empty"):
        problems.append("the source has no groups, but the STEP root is not an empty product")
    if groups and data.get("empty"):
        problems.append(f"the STEP root is an empty product, but the source has {len(groups)} group(s)")
    expected = {g["name"]: g for g in groups}
    found = {g["name"]: g for g in data["groups"]}
    if set(expected) != set(found) or len(found) != len(data["groups"]):
        problems.append(f"STEP groups {sorted(found)} differ from source {sorted(expected)}")
    compiled_by_id = {c["id"]: (unit, c) for unit, compiled in scene.units for c in compiled}
    units_by_id = {unit["id"]: unit for unit, _ in scene.units}
    part_reports, group_reports, port_reports, envelope_reports = [], [], [], []
    max_bounds, max_volume, max_mesh_out, max_mesh_reach, max_place = 0.0, 0.0, 0.0, 0.0, 0.0
    for name in sorted(set(expected) & set(found)):
        source, step_group = expected[name], found[name]
        identity_matrix = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))
        want = tuple(tuple(r) for r in source["matrix"]) if source["matrix"] else identity_matrix
        delta = _matrix_delta(want, step_group["location"])
        max_place = max(max_place, delta)
        group_reports.append({"name": name, "placement_delta_mm": delta, "ok": delta <= PLACEMENT_TOLERANCE_MM,
                              "product": step_group["product"]})
        if delta > PLACEMENT_TOLERANCE_MM or step_group["product"] != name:
            problems.append(f"{name}: placement differs by {delta:.3g} mm or product name {step_group['product']!r}")
        want_parts = {p["name"] for p in source["parts"]}
        got_parts = {p["name"]: p for p in step_group["parts"]}
        if want_parts != set(got_parts) or len(got_parts) != len(step_group["parts"]):
            problems.append(f"{name}: STEP parts {sorted(got_parts)} differ from source {sorted(want_parts)}")
        local_bounds = {}
        for pname in sorted(want_parts & set(got_parts)):
            unit, c = compiled_by_id[pname]
            part = got_parts[pname]
            shape = part["shape"]
            solids = kernel.solids(shape)
            valid = all(kernel.is_valid(s) for s in solids) and kernel.is_valid(shape)
            vol_src, vol_step = kernel.volume(c["world"]), kernel.volume(shape)
            b_src, b_step = kernel.bounds(c["world"]), kernel.bounds(shape)
            dv = abs(vol_step - vol_src) / max(abs(vol_src), 1e-9)
            db = _bounds_delta(b_src, b_step)
            mesh = c["record"]["bounds_m"]
            mesh_mm = ([v * 1000 for v in mesh["min"]], [v * 1000 for v in mesh["max"]])
            outside = max(max(b_step[0][i] - mesh_mm[0][i] for i in range(3)),
                          max(mesh_mm[1][i] - b_step[1][i] for i in range(3)))
            reach = max(max(mesh_mm[0][i] - b_step[0][i] for i in range(3)),
                        max(b_step[1][i] - mesh_mm[1][i] for i in range(3)))
            max_bounds, max_volume = max(max_bounds, db), max(max_volume, dv)
            max_mesh_out, max_mesh_reach = max(max_mesh_out, outside), max(max_mesh_reach, reach)
            ok = (valid and len(solids) == c["solids"] and dv <= STEP_VOLUME_RELATIVE_TOLERANCE
                  and db <= STEP_BOUNDS_TOLERANCE_MM and outside <= MESH_INSIDE_TOLERANCE_MM
                  and reach <= MESH_REACH_TOLERANCE_MM and part["product"] == pname)
            if not ok:
                problems.append(f"{pname}: valid={valid}, solids {len(solids)}/{c['solids']}, volume delta {dv:.3g}, "
                                f"bounds delta {db:.3g} mm, mesh outside {outside:.3g} mm, mesh reach {reach:.3g} mm, "
                                f"product {part['product']!r}")
            part_reports.append({
                "name": pname, "ok": ok, "valid": valid, "solids": len(solids), "source_solids": c["solids"],
                "volume_mm3": round(vol_step, 3), "volume_relative_delta": dv, "bounds_mm": [
                    [round(v, 6) for v in b_step[0]], [round(v, 6) for v in b_step[1]]],
                "bounds_delta_mm": db, "mesh_outside_mm": round(outside, 6), "mesh_reach_mm": round(reach, 6),
            })
            local_bounds[c["component"]["key"]] = _local_bounds(shape, step_group["location"])
        unit = units_by_id[name]
        for port in unit["ports"]:
            pname = component_id(name, port["component_key"])
            if pname not in got_parts:
                problems.append(f"port {port['id']}: nozzle part {pname} missing from STEP")
                continue
            position_mm = [c * 1000.0 for c in port["position_m"]]
            report = port_face_report(got_parts[pname]["shape"], position_mm, port["direction"])
            report.update(port_id=port["id"], part=pname, position_m=port["position_m"], direction=port["direction"])
            port_reports.append(report)
            if not report["ok"]:
                problems.append(f"port {port['id']}: not on an outward planar face of {pname} in STEP: {report}")
        for check in unit["envelope_checks"]:
            missing = [k for k in check["components"] if k not in local_bounds]
            if missing:
                problems.append(f"{name}: envelope check {check['name']!r} lacks parts {missing}")
                continue
            report = _check_envelope(check, local_bounds)
            report["group"] = name
            envelope_reports.append(report)
            if not report["ok"]:
                problems.append(f"{name}: envelope check {check['name']!r} failed: {report['failures']}")
    summary = {
        "groups": len(group_reports), "parts": len(part_reports),
        "solids": sum(p["solids"] for p in part_reports), "ports_on_flange_faces": sum(r["ok"] for r in port_reports),
        "ports_checked": len(port_reports), "envelope_checks_passed": sum(r["ok"] for r in envelope_reports),
        "envelope_checks": len(envelope_reports), "max_bounds_delta_mm": max_bounds,
        "max_volume_relative_delta": max_volume, "max_placement_delta_mm": max_place,
        "max_mesh_outside_mm": round(max_mesh_out, 6), "max_mesh_reach_mm": round(max_mesh_reach, 6),
        "length_unit": "mm" if facts["length_unit_millimetre"] else None, "schema": facts["schema"],
        "empty_root_product": bool(data.get("empty")),
    }
    return {
        "ok": not problems, "problems": problems, "step_file": "model.step", "header": facts,
        "identity": identity, "summary": summary,
        "tolerances": {"bounds_mm": STEP_BOUNDS_TOLERANCE_MM, "volume_relative": STEP_VOLUME_RELATIVE_TOLERANCE,
                       "placement_mm": PLACEMENT_TOLERANCE_MM, "envelope_mm": ENVELOPE_TOLERANCE_MM,
                       "mesh_inside_mm": MESH_INSIDE_TOLERANCE_MM, "mesh_reach_mm": MESH_REACH_TOLERANCE_MM},
        "groups": group_reports, "parts": part_reports, "ports": port_reports, "envelopes": envelope_reports,
    }


def _local_bounds(world_shape, group_matrix):
    """Bounds of a reimported part in its group's (entity's) frame."""
    r = [row[:3] for row in group_matrix]
    t = [row[3] for row in group_matrix]
    inverse = tuple(tuple(r[j][i] for j in range(3)) + (-sum(r[j][i] * t[j] for j in range(3)),) for i in range(3))
    return kernel.bounds(kernel.located(world_shape, inverse))


# --- Entry points --------------------------------------------------------------


def _identity(project):
    project_id = project.get("id") if isinstance(project.get("id"), str) else None
    revision = project.get("revision")
    revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    return {"project_id": project_id, "source_revision": revision, "input_digest": None}


def _fail_early(output_dir, identity, diagnostics, events):
    extra = {"provenance": _provenance("project_build", identity, None)}
    display = _manifest("display", identity, "failed", diagnostics, {}, [], extra)
    cad = _manifest("cad", identity, "failed", diagnostics, {}, [], extra)
    _write_atomic(os.path.join(output_dir, "display.manifest.json"), _dumps(display))
    _write_atomic(os.path.join(output_dir, "cad.manifest.json"), _dumps(cad))
    events.emit("activity", "display", f"Geometry not generated: {diagnostics[0]['message']}")
    return {"display": display, "cad": cad}


def _assumption_records(project):
    records = {aid: dict(record, scope="recipe") for aid, record in RECIPE_ASSUMPTIONS.items()}
    for aid, record in project["model"]["assumptions"].items():
        records[aid] = {"scope": "project", "category": "project_assumption",
                        "description": record.get("description"), "source": record.get("source")}
    # Definition-local assumptions are namespaced by entity ID; a clash fails that unit in plan_units,
    # and is never resolved here by overwriting a project or global record.
    for entity in project["model"]["entities"].values():
        if is_composed(entity):
            try:
                local = definition_assumption_records(entity)
            except (KeyError, TypeError, AttributeError):
                continue  # an unusable definition fails its unit with an explicit diagnostic
            for aid, record in local.items():
                records.setdefault(aid, record)
    return records


def build_geometry(project, output_dir, emit=None, cancelled=None) -> dict:
    """Generate the display and CAD artifacts of ``project`` into ``output_dir``.

    Returns ``{"display": manifest, "cad": manifest}``. Invalid projects and
    geometry faults produce ``failed`` manifests with error diagnostics;
    cancellation raises ``InterruptedError`` after leaving only immutable
    previews; invalid arguments raise ``TypeError``/``ValueError``.
    """
    if not isinstance(output_dir, (str, os.PathLike)) or not os.path.isdir(output_dir):
        raise ValueError(f"output_dir {output_dir!r} must be an existing directory")
    output_dir = os.fspath(output_dir)
    events = _Events(emit, cancelled, output_dir)
    if not isinstance(project, dict):
        return _fail_early(output_dir, {"project_id": None, "source_revision": None, "input_digest": None},
                           [_diag("invalid_input", "error", [], "project must be a JSON object")], events)
    identity = _identity(project)
    try:
        identity["input_digest"] = canonical_digest(project)
    except (TypeError, ValueError) as exc:
        return _fail_early(output_dir, identity, [_diag("invalid_input", "error", [],
                                                        f"project is not canonical JSON: {exc}")], events)
    events.check("validation")
    found = validate_project(project)
    errors = [d for d in found if d["severity"] == "error"]
    if errors or identity["project_id"] is None or identity["source_revision"] is None:
        if not errors:
            errors = [_diag("invalid_input", "error", [], "project id or revision is missing")]
        return _fail_early(output_dir, identity, errors, events)
    snapshot = copy.deepcopy(project)
    other = [d for d in found if d["severity"] == "warning" and d["code"] not in PID_WARNINGS | OWN_WARNINGS]
    units, diagnostics, failures = plan_units(snapshot)
    model = snapshot["model"]
    ids = sorted(model["entities"]) + sorted(model["ports"]) + sorted(model["connections"])
    display, cad = build_units(units, identity, output_dir, events,
                               assumption_records=_assumption_records(snapshot),
                               diagnostics=other + diagnostics, failures=failures, representation_ids=ids,
                               provenance_kind="project_build",
                               snapshot_source={"kind": "project", "entity_count": len(model["entities"]),
                                                "connection_count": len(model["connections"])})
    return {"display": display, "cad": cad}


def rebuild_from_recipe(recipe_path, output_dir, emit=None, cancelled=None) -> dict:
    """Replay an unchanged generated ``geometry.recipe.json`` without the project.

    ``snapshot.load_snapshot`` first verifies the recorded ``recipe_sha256``
    and that the generator version and conventions match this generator; any
    mismatch raises ``SnapshotError`` before a solid is generated or a file is
    written. Outputs are labelled ``provenance.kind = "snapshot_rebuild"``
    with the snapshot's digest and the source identity it records, and the
    snapshot is written back unchanged beside them.
    """
    if not isinstance(output_dir, (str, os.PathLike)) or not os.path.isdir(output_dir):
        raise ValueError(f"output_dir {output_dir!r} must be an existing directory")
    output_dir = os.fspath(output_dir)
    events = _Events(emit, cancelled, output_dir)
    document = load_snapshot(recipe_path, conventions())
    identity = {key: document[key] for key in ("project_id", "source_revision", "input_digest")}
    display, cad = build_units(document["units_built"], identity, output_dir, events,
                               assumption_records=document["assumptions"],
                               diagnostics=copy.deepcopy(document["diagnostics"]), failures=[],
                               representation_ids=document["representation_ids"],
                               provenance_kind="snapshot_rebuild", snapshot=document)
    return {"display": display, "cad": cad}
