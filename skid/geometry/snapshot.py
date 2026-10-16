"""Generated recipe snapshots (``geometry.recipe.json``): content digest and replay checks.

Every build writes a normalized snapshot of the units it compiled. Its
``recipe_sha256`` is the SHA-256 of the snapshot's canonical JSON (sorted
keys, compact separators, UTF-8, no NaN) with only the ``recipe_sha256``
field removed; every other field, including the claimed source identity and
the usage hint, is covered.

``load_snapshot`` is the only way a standalone rebuild obtains units. Before
any solid is generated it parses strictly, rejects legacy and unknown
schemas, recomputes the digest and rejects a missing or mismatching one, then
checks that the generator version, units, world and tessellation conventions,
materials, assumptions and unit structure are exactly those this generator
produces. An edited snapshot is therefore refused rather than rebuilt under
the original identity. Design changes belong in validated project commands
(then ``build``) or in an authored recipe (``recipe``).

A ``composed_equipment`` unit also records ``definition`` (the definition
document, its SHA-256 and the exact metre parameters it was resolved with).
Replay re-resolves that definition and requires exactly the recorded part
calls, materials and local port frames. Its local assumptions appear under
entity-namespaced IDs with scope ``definition``.

The digest detects accidental or unintended edits. It is not a signature:
anyone can recompute it, so it proves neither authorship nor authenticity.
It is also not the project's ``input_digest``, which is the canonical digest
of the engineering project and is recorded separately as source identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from skid.equipment import COMPOSED_KIND

from .composed import check_unit_definition
from .kernel import KernelError, placement_matrix, transform_point, transform_vector
from .materials import MATERIALS
from .recipes import RecipeError, check_component

__all__ = [
    "DIGEST_FIELD",
    "LEGACY_SCHEMAS",
    "SNAPSHOT_FIELDS",
    "SNAPSHOT_SCHEMA",
    "SnapshotError",
    "content_digest",
    "describe",
    "load_snapshot",
    "with_digest",
]

SNAPSHOT_SCHEMA = "skid.geometry.snapshot-recipe/2"
LEGACY_SCHEMAS = frozenset({"skid.geometry.snapshot-recipe/1"})
DIGEST_FIELD = "recipe_sha256"
SNAPSHOT_FIELDS = frozenset({
    "schema", "generator", "project_id", "source_revision", "input_digest", "length_unit", "world", "tessellation",
    "units_built", "representation_ids", "assumptions", "materials", "diagnostics", "rebuild", "source", DIGEST_FIELD,
})
SOURCE_KINDS = ("project", "authored_recipe")
UNIT_FIELDS = {
    "entity": frozenset({"id", "type", "kind", "tag", "template", "generator", "status", "parameters_mm", "placement",
                         "matrix_mm", "ports", "components", "envelope_checks"}),
    "connection": frozenset({"id", "type", "kind", "tag", "template", "generator", "status", "points_m", "route",
                             "from_port_id", "to_port_id", "nominal_diameter_mm", "outside_diameter_m", "matrix_mm",
                             "ports", "components", "envelope_checks"}),
}
COMPOSED_UNIT_FIELDS = frozenset({"definition"})
ASSUMPTION_SCOPES = ("project", "recipe", "definition")
PORT_FIELDS = frozenset({"id", "entity_id", "name", "position_m", "direction", "local_position_mm", "local_direction",
                         "nominal_diameter_mm", "component_key"})
ENVELOPE_FIELDS = frozenset({"name", "components", "mode", "bounds_mm"})
ENVELOPE_KEYS = frozenset({"xmin", "xmax", "ymin", "ymax", "zmin", "zmax"})
PORT_POSITION_TOLERANCE_M = 1e-6
DIRECTION_TOLERANCE = 1e-9
MATRIX_TOLERANCE = 1e-9
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
REGENERATE = ("Regenerate it from the project (python -m skid.geometry build PROJECT.json OUT_DIR). Make design "
              "changes with validated project commands or an authored recipe (python -m skid.geometry recipe), "
              "not by editing a generated snapshot.")


class SnapshotError(ValueError):
    """A generated snapshot cannot be replayed; the message says why and what to do."""


def _canonical(document) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def content_digest(document: dict) -> str:
    """SHA-256 of the canonical snapshot JSON without its ``recipe_sha256`` field."""
    return hashlib.sha256(_canonical({k: v for k, v in document.items() if k != DIGEST_FIELD})).hexdigest()


def with_digest(document: dict) -> dict:
    """Return a JSON-normalized copy of ``document`` carrying its content digest."""
    body = json.loads(_canonical({k: v for k, v in document.items() if k != DIGEST_FIELD}))
    body[DIGEST_FIELD] = content_digest(body)
    return body


def _strict_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise SnapshotError(f"snapshot JSON repeats the key {key!r}")
        out[key] = value
    return out


def _reject_constant(name):
    raise SnapshotError(f"snapshot JSON contains the non-finite number {name}")


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _vector(value, size=3):
    return isinstance(value, list) and len(value) == size and all(_finite(v) for v in value)


def load_snapshot(path, conventions: dict) -> dict:
    """Read, digest-check and compatibility-check a generated snapshot; raise ``SnapshotError``.

    ``conventions`` holds the current ``generator``, ``length_unit``,
    ``world`` and ``tessellation`` records. Returns the parsed document.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot {path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SnapshotError(f"snapshot {path} must be a JSON object")
    schema = document.get("schema")
    if schema in LEGACY_SCHEMAS:
        raise SnapshotError(f"{path} uses legacy schema {schema!r}, which records no {DIGEST_FIELD} content digest, "
                            f"so its content cannot be checked and it is not replayed. {REGENERATE}")
    if schema != SNAPSHOT_SCHEMA:
        raise SnapshotError(f"{path} is not a generated geometry snapshot (schema {schema!r}, expected "
                            f"{SNAPSHOT_SCHEMA!r}). Authored recipes are built with python -m skid.geometry recipe.")
    recorded = document.get(DIGEST_FIELD)
    if recorded is None:
        raise SnapshotError(f"{path} has no {DIGEST_FIELD}; its content cannot be checked. {REGENERATE}")
    if not isinstance(recorded, str) or not _HEX64.match(recorded):
        raise SnapshotError(f"{path}: {DIGEST_FIELD} must be 64 lowercase hexadecimal characters, not {recorded!r}")
    try:
        computed = content_digest(document)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{path}: content cannot be canonicalized: {exc}") from exc
    if computed != recorded:
        raise SnapshotError(f"{path}: content does not match its recorded {DIGEST_FIELD} (recorded {recorded}, "
                            f"computed {computed}); the snapshot was edited or corrupted after generation and is "
                            f"not rebuilt under its original source identity. {REGENERATE}")
    _check_replayable(document, conventions, path)
    return document


def _check_replayable(document, conventions, path):
    def fail(message):
        raise SnapshotError(f"{path}: {message}")

    missing, extra = SNAPSHOT_FIELDS - set(document), set(document) - SNAPSHOT_FIELDS
    if missing or extra:
        fail(f"snapshot fields missing {sorted(missing)} or unsupported {sorted(extra)}")
    if document["generator"] != conventions["generator"]:
        fail(f"generated by {document['generator']!r}, but this is {conventions['generator']!r}; a snapshot is only "
             f"replayed by the generator version that wrote it. {REGENERATE}")
    for key in ("length_unit", "world", "tessellation"):
        if document[key] != conventions[key]:
            fail(f"{key} {document[key]!r} differs from this generator's {conventions[key]!r}; rebuilds do not "
                 f"reinterpret recorded conventions. {REGENERATE}")
    if not isinstance(document["project_id"], str) or not document["project_id"]:
        fail("project_id must be a nonempty string")
    revision = document["source_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        fail("source_revision must be a nonnegative integer")
    if not isinstance(document["input_digest"], str) or not _HEX64.match(document["input_digest"]):
        fail("input_digest must be a 64-character lowercase hexadecimal digest")
    source = document["source"]
    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        fail(f"source.kind must be one of {SOURCE_KINDS}")
    if not isinstance(document["rebuild"], str):
        fail("rebuild must be a usage string")
    for index, item in enumerate(document["diagnostics"] if isinstance(document["diagnostics"], list) else [None]):
        if not (isinstance(item, dict) and set(item) == {"code", "severity", "entity_ids", "message"}
                and item["severity"] in ("error", "warning") and isinstance(item["entity_ids"], list)
                and isinstance(item["message"], str) and isinstance(item["code"], str)):
            fail(f"diagnostics[{index}] is not a contract diagnostic record")
    if any(d["severity"] == "error" for d in document["diagnostics"]):
        fail("a snapshot of a failed build cannot be replayed")

    units = document["units_built"]
    if not isinstance(units, list):
        fail("units_built must be a list")
    unit_ids, port_ids, materials_used, cited = set(), set(), set(), set()
    for index, unit in enumerate(units):
        where = f"units_built[{index}]"
        try:
            _check_unit(unit, where)
        except (RecipeError, KernelError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SnapshotError):
                raise SnapshotError(f"{path}: {exc}") from exc
            fail(f"{where}: {exc}")
        if unit["id"] in unit_ids:
            fail(f"{where}: duplicate unit id {unit['id']!r}")
        unit_ids.add(unit["id"])
        for port in unit["ports"]:
            if port["id"] in port_ids:
                fail(f"{where}: duplicate port id {port['id']!r}")
            port_ids.add(port["id"])
        for component in unit["components"]:
            materials_used.add(component["material"])
            cited.update(component["assumptions"])
    ids = document["representation_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        fail("representation_ids must be a list of unique IDs")
    if not (unit_ids | port_ids) <= set(ids):
        fail(f"representation_ids omit built IDs {sorted((unit_ids | port_ids) - set(ids))[:5]}")
    materials = document["materials"]
    if not isinstance(materials, dict) or set(materials) != materials_used:
        fail(f"materials {sorted(materials) if isinstance(materials, dict) else materials!r} must list exactly the "
             f"materials the components use {sorted(materials_used)}")
    for name, record in materials.items():
        if record != MATERIALS.get(name):
            fail(f"material {name!r} {record!r} differs from this generator's palette {MATERIALS.get(name)!r}; "
                 f"rebuilds do not apply edited appearance. {REGENERATE}")
    assumptions = document["assumptions"]
    if not isinstance(assumptions, dict) or set(assumptions) != cited:
        fail("assumptions must record exactly the assumption IDs the components cite")
    for aid, record in assumptions.items():
        if not isinstance(record, dict) or not isinstance(record.get("description"), str) \
                or record.get("scope") not in ASSUMPTION_SCOPES:
            fail(f"assumption {aid!r} needs a description and a project, recipe or definition scope")


def _check_unit(unit, where):
    if not isinstance(unit, dict) or unit.get("type") not in UNIT_FIELDS:
        raise SnapshotError(f"{where}: type must be entity or connection")
    fields = UNIT_FIELDS[unit["type"]]
    if unit["type"] == "entity" and unit.get("kind") == COMPOSED_KIND:
        fields = fields | COMPOSED_UNIT_FIELDS
    missing, extra = fields - set(unit), set(unit) - fields
    if missing or extra:
        raise SnapshotError(f"{where}: fields missing {sorted(missing)} or unsupported {sorted(extra)}")
    for key in ("id", "kind", "tag", "generator"):
        if not isinstance(unit[key], str) or not unit[key]:
            raise SnapshotError(f"{where}: {key} must be a nonempty string")
    if unit["status"] not in (("current",) if unit["type"] == "entity" else ("current", "pending")):
        raise SnapshotError(f"{where}: status {unit['status']!r} is not valid for a {unit['type']}")
    if not isinstance(unit["components"], list) or not unit["components"]:
        raise SnapshotError(f"{where}: components must be a nonempty list")
    keys = []
    for index, component in enumerate(unit["components"]):
        check_component(component, f"{where}.components[{index}]")
        keys.append(component["key"])
    if len(set(keys)) != len(keys):
        raise SnapshotError(f"{where}: duplicate component keys")
    cad_keys = {c["key"] for c in unit["components"] if not c["display_only"]}
    matrix = unit["matrix_mm"]
    if unit["type"] == "connection":
        if matrix is not None or unit["ports"] or unit["envelope_checks"]:
            raise SnapshotError(f"{where}: a route unit has no placement matrix, ports or envelope checks")
        points = unit["points_m"]
        if not isinstance(points, list) or len(points) < 2 or not all(_vector(p) for p in points):
            raise SnapshotError(f"{where}: points_m must hold at least two finite points")
        return
    if not (isinstance(matrix, list) and len(matrix) == 3 and all(_vector(row, 4) for row in matrix)):
        raise SnapshotError(f"{where}: matrix_mm must be a finite 3x4 matrix")
    rotation = [row[:3] for row in matrix]
    for i in range(3):
        for j in range(3):
            dot = sum(rotation[k][i] * rotation[k][j] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > MATRIX_TOLERANCE:
                raise SnapshotError(f"{where}: matrix_mm rotation is not orthonormal")
    placement = unit["placement"]
    if not (isinstance(placement, dict) and set(placement) == {"position_m", "rotation_deg", "assumption_id"}
            and _vector(placement["position_m"]) and _vector(placement["rotation_deg"])):
        raise SnapshotError(f"{where}: placement must be {{position_m, rotation_deg, assumption_id}}")
    expected = placement_matrix(placement["position_m"], placement["rotation_deg"], 1000.0)
    if max(abs(expected[i][j] - matrix[i][j]) for i in range(3) for j in range(4)) > MATRIX_TOLERANCE:
        raise SnapshotError(f"{where}: matrix_mm does not equal its recorded placement")
    for index, port in enumerate(unit["ports"] if isinstance(unit["ports"], list) else [None]):
        here = f"{where}.ports[{index}]"
        if not isinstance(port, dict) or set(port) != PORT_FIELDS:
            raise SnapshotError(f"{here}: port fields must be {sorted(PORT_FIELDS)}")
        if port["entity_id"] != unit["id"] or port["component_key"] not in cad_keys:
            raise SnapshotError(f"{here}: port must belong to this unit and name one of its CAD components")
        for key in ("position_m", "direction", "local_position_mm", "local_direction"):
            if not _vector(port[key]):
                raise SnapshotError(f"{here}: {key} must be three finite numbers")
        world = [c / 1000.0 for c in transform_point(matrix, port["local_position_mm"])]
        if math.dist(world, port["position_m"]) > PORT_POSITION_TOLERANCE_M:
            raise SnapshotError(f"{here}: position_m does not follow from its local frame and the placement")
        direction = transform_vector(matrix, port["local_direction"])
        if math.dist(direction, port["direction"]) > DIRECTION_TOLERANCE \
                or abs(math.hypot(*port["direction"]) - 1.0) > DIRECTION_TOLERANCE:
            raise SnapshotError(f"{here}: direction does not follow from its local frame and the placement")
    if unit["kind"] == COMPOSED_KIND:
        try:
            check_unit_definition(unit)
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError(f"{where}: {exc}") from None
    checks = unit["envelope_checks"]
    if not isinstance(checks, list):
        raise SnapshotError(f"{where}: envelope_checks must be a list")
    for index, check in enumerate(checks):
        here = f"{where}.envelope_checks[{index}]"
        if not (isinstance(check, dict) and set(check) == ENVELOPE_FIELDS and check["mode"] in ("equal", "within")
                and isinstance(check["components"], list) and set(check["components"]) <= cad_keys
                and isinstance(check["bounds_mm"], dict) and set(check["bounds_mm"]) <= ENVELOPE_KEYS
                and all(_finite(v) for v in check["bounds_mm"].values())):
            raise SnapshotError(f"{here}: malformed envelope check")


def describe(document) -> dict:
    """Small summary used in provenance records."""
    return {"snapshot_schema": document["schema"], "snapshot_generator": copy.deepcopy(document["generator"]),
            "snapshot_source_kind": document["source"]["kind"]}
