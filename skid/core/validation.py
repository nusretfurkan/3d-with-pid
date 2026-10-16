"""Validation of persisted pump-skid documents and projects.

Validation never mutates its input and never raises for malformed data: every
finding is a diagnostic ``{code, severity, entity_ids, message}``. Errors are
structural and block commits. Warnings mark open items: unresolved or missing
presentation (physical placements and routes, P&ID occurrences and edges) and
required ports explicitly marked ``connection_status: "unresolved"`` that are
not yet connected, so a document can be built up from empty one validated
command at a time. Such a project may be committed, but it cannot produce a
complete generation result. An unmarked unconnected required port stays an
error (a broken topology), as do markers with another value, on an optional
port or on a connected port. Validation does not establish hydraulic,
process-safety, or fabrication adequacy.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata

from ..clearance import ClearanceError, route_report
from ..equipment import (COMPOSED_KIND, COMPOSED_VERSION, DefinitionError, entity_template, resolve_definition,
                         validate_definition)
from ..templates import diagram_port_position, get_template, world_port_position

SCHEMA_VERSION = 1
ROUTE_TOLERANCE_M = 1e-6
DIAGRAM_TOLERANCE = 1e-6

DOCUMENT_FIELDS = ("schema_version", "current", "undo", "redo")
PROJECT_FIELDS = ("id", "revision", "model", "pid", "physical")
SNAPSHOT_FIELDS = ("model", "pid", "physical")
# Optional on the current project and on history snapshots; absent means "unnamed" (display falls back to id).
PROJECT_OPTIONAL_FIELDS = ("name",)
PROJECT_NAME_MAX_LENGTH = 120
# Characters a display name may not contain beyond controls, surrogates and line/paragraph separators:
# bidirectional embeddings/overrides/isolates and marks, which can make a title misrepresent its text.
_NAME_FORBIDDEN = frozenset((
    "\u061c",  # ARABIC LETTER MARK
    "\u200e", "\u200f",  # LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # LRE, RLE, PDF, LRO, RLO
    "\u2066", "\u2067", "\u2068", "\u2069",  # LRI, RLI, FSI, PDI
))
_NAME_FORBIDDEN_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")
# A name needs at least one base character: a code point whose general category is not
# Other (C*), Separator (Z*) or Mark (M*). Format characters such as ZWJ stay allowed beside one.
_NAME_NON_BASE_MAJOR_CATEGORIES = ("C", "Z", "M")
SECTION_FIELDS = {
    "model": ("entities", "ports", "connections", "assumptions"),
    "pid": ("symbols", "edges"),
    "physical": ("placements", "routes"),
}
ASSUMPTION_FIELDS = ("description", "source")
ENTITY_FIELDS = ("id", "kind", "tag", "template_version", "parameters")
OBSERVES_FIELDS = ("entity_id", "variable")
QUANTITY_FIELDS = ("value", "unit", "assumption_id", "source")
PORT_FIELDS = (
    "id",
    "entity_id",
    "name",
    "direction",
    "required",
    "nominal_diameter_mm",
    "service",
    "assumption_id",
)
# Optional explicit draft marker of a required port that is intentionally not connected yet (docs/EMPTY_START.md).
PORT_OPTIONAL_FIELDS = ("connection_status",)
CONNECTION_STATUS_UNRESOLVED = "unresolved"
CONNECTION_FIELDS = (
    "id",
    "tag",
    "from_port_id",
    "to_port_id",
    "nominal_diameter_mm",
    "outside_diameter_m",
    "assumption_id",
)
SYMBOL_FIELDS = ("id", "entity_id", "x", "y", "rotation_deg", "manual")
EDGE_FIELDS = ("id", "connection_id", "from_symbol_id", "to_symbol_id", "points", "manual", "status")
PLACEMENT_FIELDS = ("position_m", "rotation_deg", "status", "manual", "assumption_id")
ROUTE_FIELDS = ("points_m", "mode", "status", "assumption_id")
REASON_FIELD = ("reason",)

PORT_DIRECTIONS = ("in", "out", "both")
SERVICES = ("liquid",)
PATH_STATUSES = ("current", "needs_reroute", "unresolved")
PLACEMENT_STATUSES = ("placed", "unresolved")
ROUTE_MODES = ("guided", "manual")

# Template parameter pairs whose values must be strictly ordered (lower, upper).
_ORDERED_PARAMETERS = {"tank": (("outlet_height", "height"),)}
_BOUNDS = (
    ("min_exclusive", "greater than", lambda value, bound: value > bound),
    ("min_inclusive", "at least", lambda value, bound: value >= bound),
    ("max_exclusive", "less than", lambda value, bound: value < bound),
    ("max_inclusive", "at most", lambda value, bound: value <= bound),
)
_ALLOWED_DIRECTIONS = {"from": ("out", "both"), "to": ("in", "both")}


def validate_document(document: dict) -> list[dict]:
    """Validate the envelope, the current project, and every undo/redo snapshot.

    Snapshot findings are reported as errors only, with messages prefixed by
    their stack position (for example ``undo[0]:``). Unresolved presentation is
    legitimate inside history and is not reported from snapshots.
    """
    checker = _Checker()
    try:
        _check_document(checker, document)
    except Exception as exc:  # fail closed: an unexpected fault must still reject the input
        checker.fault(exc)
    return checker.diagnostics


def validate_project(project: dict) -> list[dict]:
    """Validate one project: schema, references, topology, and presentation state.

    A project without errors is also screened for current physical routes
    entering placed equipment bodies (``skid.clearance``); each such route is a
    ``route_body_intrusion`` warning (an open item), never an error, so an
    existing document stays loadable while its outputs report pending.
    """
    checker = _Checker()
    try:
        checker.project(project)
    except Exception as exc:  # fail closed: an unexpected fault must still reject the input
        checker.fault(exc)
    if not any(d["severity"] == "error" for d in checker.diagnostics):
        checker.diagnostics.extend(_route_intrusion_warnings(project))
    return checker.diagnostics


def _route_intrusion_warnings(project):
    """Warnings for current routes whose conservative envelope enters a placed body.

    A body that cannot be bounded yields no claim either way: the screen is
    skipped for that route (``docs/PIPE_SEGMENTS.md``).
    """
    found = []
    for cid, route in sorted(project["physical"]["routes"].items()):
        if route.get("status") != "current":
            continue
        try:
            report = route_report(project, cid, route["points_m"])
        except (ClearanceError, DefinitionError, ArithmeticError, LookupError, TypeError, ValueError):
            continue
        if report["clear"]:
            continue
        bodies = sorted({f"{f['tag']} {f['component']}" for f in report["intrusions"]})
        entities = sorted({f["entity_id"] for f in report["intrusions"]})
        found.append(_diagnostic("route_body_intrusion", "warning", [cid, *entities],
                                 f"physical route {cid} is current but enters equipment bodies ({report['model']}): "
                                 f"{', '.join(bodies)}; move a section, reroute or submit a manual route"))
    return found


def canonical_digest(project: dict) -> str:
    """Return the SHA-256 hex digest of the project's canonical JSON.

    Canonical JSON uses sorted keys, compact separators, ``ensure_ascii=False``
    and UTF-8. Nonfinite numbers raise ``ValueError``; non-JSON values raise
    ``TypeError``.
    """
    text = json.dumps(
        project,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_tag(tag: str) -> str:
    """Return the comparison key of a tag: whitespace collapsed, case folded."""
    return " ".join(tag.split()).casefold()


def project_name_problem(value) -> str | None:
    """Explain why ``value`` is not a valid stored project name, or return None.

    A name is a string of 1 to ``PROJECT_NAME_MAX_LENGTH`` code points with no
    leading or trailing whitespace, no control characters (including tabs and
    newlines), line/paragraph separators, surrogates, Unicode noncharacters or
    bidirectional formatting characters, and at least one base character (a
    letter, number, punctuation mark or symbol: category outside C, Z and M),
    so invisible format/mark/space-only text is refused. Other Unicode,
    including letters of any script, symbols, combining marks, emoji sequences
    and inner spaces, is allowed.
    """
    if not isinstance(value, str):
        return f"project name must be a string, not {_type(value)}"
    if value.strip() == "":
        return "project name must not be empty or only whitespace"
    if value != value.strip():
        return "project name must not have leading or trailing whitespace"
    if len(value) > PROJECT_NAME_MAX_LENGTH:
        return f"project name has {len(value)} characters; at most {PROJECT_NAME_MAX_LENGTH} are allowed"
    for char in value:
        code = ord(char)
        if (unicodedata.category(char) in _NAME_FORBIDDEN_CATEGORIES or char in _NAME_FORBIDDEN
                or 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE):
            return f"project name contains the disallowed character U+{code:04X}"
    if all(unicodedata.category(char)[0] in _NAME_NON_BASE_MAJOR_CATEGORIES for char in value):
        return "project name must contain a visible letter, number, punctuation mark or symbol"
    return None


def project_display_name(project) -> str | None:
    """Human-facing project title: the valid stored name, else the stable project id (None if neither)."""
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    if name is not None and project_name_problem(name) is None:
        return name
    project_id = project.get("id")
    return project_id if _is_id(project_id) else None


def _check_document(checker, document):
    if not isinstance(document, dict):
        checker.error("invalid_type", [], f"document must be an object, not {_type(document)}")
        return
    checker.fields(document, DOCUMENT_FIELDS, (), [], "document")
    if "schema_version" not in document:
        return
    version = document["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        checker.error(
            "unsupported_schema_version",
            [],
            f"schema_version {_show(version)} is not supported; expected {SCHEMA_VERSION}",
        )
        return
    current = document.get("current")
    if "current" in document:
        checker.project(current)
    project_id = current.get("id") if isinstance(current, dict) else None
    revision = current.get("revision") if isinstance(current, dict) else None
    project_id = project_id if _is_id(project_id) else None
    revision = revision if _is_revision(revision) else None
    history = 0
    for stack in ("undo", "redo"):
        if stack not in document:
            continue
        entries = document[stack]
        if not isinstance(entries, list):
            checker.error("invalid_type", [], f"{stack} must be a list of snapshots, not {_type(entries)}")
            continue
        history += len(entries)
        for index, snapshot in enumerate(entries):
            for diagnostic in _snapshot_errors(snapshot, project_id, revision):
                diagnostic["message"] = f"{stack}[{index}]: {diagnostic['message']}"
                checker.diagnostics.append(diagnostic)
    if revision is not None and history > revision:
        checker.error(
            "history_revision_inconsistent",
            [project_id],
            f"{history} history snapshot(s) cannot arise from {revision} accepted revision(s)",
        )


def _snapshot_errors(snapshot, project_id, revision):
    checker = _Checker()
    try:
        checker.snapshot(snapshot, project_id, revision)
    except Exception as exc:  # fail closed, as for the current project
        checker.fault(exc)
    return [d for d in checker.diagnostics if d["severity"] == "error"]


class _Checker:
    """Collects diagnostics for one project or one history snapshot."""

    def __init__(self):
        self.diagnostics = []

    def error(self, code, ids, message):
        self.diagnostics.append(_diagnostic(code, "error", ids, message))

    def warning(self, code, ids, message):
        self.diagnostics.append(_diagnostic(code, "warning", ids, message))

    def fault(self, exc):
        self.error("validation_failed", [], f"validation could not complete ({_describe_exception(exc)})")

    def fields(self, record, required, optional, ids, where):
        """Report a non-object record, unknown fields, and missing fields; return whether it is an object."""
        if not isinstance(record, dict):
            self.error("invalid_type", ids, f"{where} must be an object, not {_type(record)}")
            return False
        for key in record:
            if key not in required and key not in optional:
                self.error("unknown_field", ids, f"{where} has unsupported field {_show(key)}")
        for key in required:
            if key not in record:
                self.error("missing_field", ids, f"{where} is missing required field {key!r}")
        return True

    def project(self, project):
        if not isinstance(project, dict):
            self.error("invalid_type", [], f"project must be an object, not {_type(project)}")
            return
        self.fields(project, PROJECT_FIELDS, PROJECT_OPTIONAL_FIELDS, [], "project")
        self.name(project, "project")
        if "id" in project and not _is_id(project["id"]):
            self.error("invalid_id", [], f"project id must be a nonempty string, not {_show(project['id'])}")
        if "revision" in project and not _is_revision(project["revision"]):
            self.error(
                "invalid_revision",
                [],
                f"project revision must be a nonnegative integer, not {_show(project['revision'])}",
            )
        _Content(self, project, project).run()

    def snapshot(self, snapshot, project_id, revision):
        if not isinstance(snapshot, dict):
            self.error("invalid_type", [], f"snapshot must be an object, not {_type(snapshot)}")
            return
        self.fields(snapshot, SNAPSHOT_FIELDS, PROJECT_OPTIONAL_FIELDS, [], "snapshot")
        self.name(snapshot, "snapshot")
        view = {"id": project_id, "revision": revision}
        view.update((key, snapshot[key]) for key in SNAPSHOT_FIELDS if key in snapshot)
        _Content(self, snapshot, view).run()

    def name(self, record, where):
        if "name" in record:
            problem = project_name_problem(record["name"])
            if problem is not None:
                self.error("invalid_project_name", [], f"{where} name is invalid: {problem}")


class _Content:
    """Checks the model, P&ID, and physical sections of one project or snapshot.

    ``view`` is the project-shaped mapping handed to the template position
    functions; it shares (and never modifies) the sections of ``container``.
    """

    def __init__(self, report, container, view):
        self.report = report
        self.error = report.error
        self.warning = report.warning
        self.view = view
        self.templates = {}
        self.entity_template = {}
        self.geometry_ok = set()
        self.port_info = {}
        self.ports_by_entity = {}
        self.port_uses = {}
        self.connection_info = {}
        self.symbol_info = {}
        self.symbols_by_entity = {}
        self.edges_by_connection = {}
        self.placement_state = {}
        self.tags = {}
        maps = self.read_sections(container)
        self.entities = maps["entities"]
        self.ports = maps["ports"]
        self.connections = maps["connections"]
        self.assumptions = maps["assumptions"]
        self.symbols = maps["symbols"]
        self.edges = maps["edges"]
        self.placements = maps["placements"]
        self.routes = maps["routes"]

    def run(self):
        for key, record in self.assumptions.items():
            self.check_assumption(key, record)
        for key, record in self.entities.items():
            self.check_entity(key, record)
        for key, record in self.ports.items():
            self.check_port(key, record)
        self.check_entity_ports()
        for key, record in self.connections.items():
            self.check_connection(key, record)
        self.check_port_uses()
        for key, record in self.symbols.items():
            self.check_symbol(key, record)
        for key, record in self.edges.items():
            self.check_edge(key, record)
        for key, record in self.placements.items():
            self.check_placement(key, record)
        for key, record in self.routes.items():
            self.check_route(key, record)
        self.check_completeness()
        self.check_tags()
        self.check_unique_ids()

    # Sections and shared field checks

    def read_sections(self, container):
        maps = {}
        for section, names in SECTION_FIELDS.items():
            body = container.get(section)
            if section in container and not isinstance(body, dict):
                self.error("invalid_type", [], f"{section} must be an object, not {_type(body)}")
            if isinstance(body, dict):
                self.report.fields(body, names, (), [], section)
            for name in names:
                maps[name] = self.read_map(body, f"{section}.{name}", name)
        return maps

    def read_map(self, body, where, name):
        if not isinstance(body, dict) or name not in body:
            return {}
        value = body[name]
        if not isinstance(value, dict):
            self.error("invalid_type", [], f"{where} must be an object keyed by ID, not {_type(value)}")
            return {}
        records = {}
        for key, record in value.items():
            if _is_id(key):
                records[key] = record
            else:
                self.error("invalid_id", [], f"{where} key {_show(key)} is not a nonempty string ID")
        return records

    def check_id(self, key, record, ids, where):
        if "id" in record and not (isinstance(record["id"], str) and record["id"] == key):
            self.error("id_mismatch", ids, f"{where} id {_show(record['id'])} does not match its map key")

    def check_flag(self, record, key, ids, where):
        if key in record and not isinstance(record[key], bool):
            self.error("invalid_type", ids, f"{where} {key} must be true or false, not {_show(record[key])}")

    def check_status(self, record, options, ids, where):
        """Return the record's status when valid, else None (reporting invalid values)."""
        if "status" not in record:
            return None
        status = record["status"]
        if _one_of(status, options):
            return status
        self.error("invalid_value", ids, f"{where} status must be one of {', '.join(options)}, not {_show(status)}")
        return None

    def check_reason(self, record, required, ids, where):
        reason = record.get("reason")
        if required:
            if not _is_text(reason):
                self.error(
                    "missing_reason",
                    ids,
                    f"{where} is {_show(record.get('status'))} and needs a nonempty reason",
                )
        elif "reason" in record and reason is not None and not isinstance(reason, str):
            self.error("invalid_type", ids, f"{where} reason must be a string, not {_show(reason)}")

    def check_points(self, points, size, ids, where):
        if not isinstance(points, (list, tuple)):
            self.error("invalid_type", ids, f"{where} must be a list of points, not {_type(points)}")
            return False
        for index, point in enumerate(points):
            if not _is_vector(point, size):
                self.error("invalid_number", ids, f"{where}[{index}] must be {size} finite numbers, not {_show(point)}")
                return False
        return True

    def enough_points(self, points, ids, where):
        if len(points) >= 2:
            return True
        self.error("too_few_points", ids, f"{where} is current but has {len(points)} point(s); at least two are required")
        return False

    def ref(self, value, targets, noun, ids, where):
        """Return a resolved reference, or None after reporting an invalid or dangling one."""
        if not _is_id(value):
            self.error("invalid_reference", ids, f"{where} must be a {noun} ID, not {_show(value)}")
            return None
        if value not in targets:
            self.error("dangling_reference", ids, f"{where} references unknown {noun} {value!r}")
            return None
        return value

    def assumption_ref(self, value, ids, where, allow_null=False):
        if value is None and allow_null:
            return True
        return self.ref(value, self.assumptions, "assumption", ids, f"{where} assumption_id") is not None

    def collect_tag(self, owner, record, where):
        if "tag" not in record:
            return
        tag = record["tag"]
        if not _is_text(tag):
            self.error("invalid_tag", [owner], f"{where} tag must be a nonempty string, not {_show(tag)}")
            return
        self.tags.setdefault(normalize_tag(tag), []).append((owner, tag))

    def template(self, kind, version):
        """Return ``(template, detail)``; template is None when unavailable or malformed."""
        key = (kind, version)
        if key not in self.templates:
            detail = ""
            try:
                template = get_template(kind, version)
            except Exception as exc:  # any lookup failure means the template is missing
                template, detail = None, f" ({_describe_exception(exc)})"
            if template is not None and not (
                isinstance(template, dict)
                and isinstance(template.get("parameters"), dict)
                and isinstance(template.get("ports"), dict)
            ):
                template, detail = None, " (template record is malformed)"
            self.templates[key] = (template, detail)
        return self.templates[key]

    # Model records

    def check_assumption(self, aid, record):
        ids = [aid]
        where = f"assumption {aid!r}"
        if not self.report.fields(record, ASSUMPTION_FIELDS, (), ids, where):
            return
        for key in ASSUMPTION_FIELDS:
            if key in record and not _is_text(record[key]):
                self.error("invalid_value", ids, f"{where} {key} must be a nonempty string, not {_show(record[key])}")

    def check_entity(self, eid, record):
        ids = [eid]
        where = f"entity {eid!r}"
        if not self.report.fields(record, ENTITY_FIELDS, ("observes", "definition"), ids, where):
            return
        self.check_id(eid, record, ids, where)
        self.collect_tag(eid, record, where)
        kind = record.get("kind")
        version = record.get("template_version")
        if "kind" in record and not _is_text(kind):
            self.error("invalid_value", ids, f"{where} kind must be a nonempty string, not {_show(kind)}")
        version_ok = isinstance(version, int) and not isinstance(version, bool) and version >= 1
        if "template_version" in record and not version_ok:
            self.error(
                "invalid_template_version",
                ids,
                f"{where} template_version must be a positive integer, not {_show(version)}",
            )
        composed = kind == COMPOSED_KIND
        if "definition" in record and not composed:
            self.error("invalid_definition", ids, f"{where} carries a definition, but only {COMPOSED_KIND} entities may")
        template = None
        if _is_text(kind) and version_ok:
            if composed:
                template = self.composed_template(record, version, ids, where)
            else:
                template, detail = self.template(kind, version)
                if template is None:
                    self.error("missing_template", ids, f"{where} has no template for kind {kind!r} version {version}{detail}")
        values = self.check_parameters(record, template, ids, where)
        if template is not None:
            self.entity_template[eid] = template
            if values is not None and self.check_ordered(kind, values, ids, where) and self.check_resolution(
                record, composed, ids, where
            ):
                self.geometry_ok.add(eid)
        if "observes" in record:
            self.check_observes(eid, record, ids, where)

    def composed_template(self, record, version, ids, where):
        """Template record of a composed entity from its definition, or None after reporting why not."""
        if version != COMPOSED_VERSION:
            self.error(
                "missing_template",
                ids,
                f"{where} has no template for kind {COMPOSED_KIND!r} version {version}; only version "
                f"{COMPOSED_VERSION} exists",
            )
            return None
        if "definition" not in record:
            self.error("missing_definition", ids, f"{where} is {COMPOSED_KIND} but carries no definition")
            return None
        problems = validate_definition(record["definition"])
        for problem in problems:
            self.error(problem["code"], ids, f"{where} definition: {problem['message']}")
        if problems:
            return None
        try:
            return entity_template(record)
        except Exception as exc:  # report any lookup fault as a missing template
            self.error("missing_template", ids, f"{where} definition gives no template ({_describe_exception(exc)})")
            return None

    def check_resolution(self, record, composed, ids, where):
        """A composed entity's geometry must resolve at its current values (bounds and constraints)."""
        if not composed:
            return True
        try:
            resolve_definition(record)
        except Exception as exc:  # any resolution fault blocks geometry for this entity
            self.error(
                "definition_resolution_failed",
                ids,
                f"{where} definition does not resolve at the current parameter values ({_describe_exception(exc)})",
            )
            return False
        return True

    def check_parameters(self, record, template, ids, where):
        """Return valid parameter values by name, or None unless every declared parameter is valid."""
        if "parameters" not in record:
            return None
        parameters = record["parameters"]
        if not isinstance(parameters, dict):
            self.error("invalid_type", ids, f"{where} parameters must be an object, not {_type(parameters)}")
            return None
        declarations = template["parameters"] if template is not None else None
        values = {}
        complete = True
        for name, quantity in parameters.items():
            label = f"{where} parameter {_show(name)}"
            if declarations is not None and (not isinstance(name, str) or name not in declarations):
                self.error(
                    "unknown_parameter",
                    ids,
                    f"{label} is not declared by the {_show(record.get('kind'))} template",
                )
                complete = False
                continue
            declaration = declarations[name] if declarations is not None else None
            value = self.check_quantity(quantity, declaration, ids, label)
            if value is None:
                complete = False
            else:
                values[name] = value
        if declarations is None:
            return None
        for name in declarations:
            if name not in parameters:
                self.error("missing_parameter", ids, f"{where} is missing parameter {name!r}")
                complete = False
        return values if complete else None

    def check_quantity(self, quantity, declaration, ids, where):
        """Return the quantity value when the quantity is fully valid, else None."""
        if not isinstance(quantity, dict):
            self.error(
                "invalid_quantity",
                ids,
                f"{where} must be a quantity {{value, unit, assumption_id | source}}, not {_type(quantity)}",
            )
            return None
        valid = True
        for key in quantity:
            if key not in QUANTITY_FIELDS:
                self.error("unknown_field", ids, f"{where} has unsupported field {_show(key)}")
                valid = False
        for key in ("value", "unit"):
            if key not in quantity:
                self.error("missing_field", ids, f"{where} is missing required field {key!r}")
                valid = False
        has_assumption = "assumption_id" in quantity
        has_source = "source" in quantity
        if has_assumption and has_source:
            self.error("invalid_quantity", ids, f"{where} must cite either assumption_id or source, not both")
            valid = False
        elif has_assumption:
            valid = self.assumption_ref(quantity["assumption_id"], ids, where) and valid
        elif has_source:
            if not _is_text(quantity["source"]):
                self.error("missing_provenance", ids, f"{where} source must be a nonempty string")
                valid = False
        else:
            self.error("missing_provenance", ids, f"{where} has neither assumption_id nor source")
            valid = False
        value = quantity.get("value")
        if "value" in quantity and not _is_number(value):
            self.error("invalid_number", ids, f"{where} value must be a finite number, not {_show(value)}")
            valid = False
        expected_unit = declaration.get("unit") if isinstance(declaration, dict) else None
        if "unit" in quantity:
            unit = quantity["unit"]
            if not _is_text(unit) or (expected_unit is not None and unit != expected_unit):
                expected = repr(expected_unit) if expected_unit is not None else "a nonempty unit string"
                self.error("invalid_unit", ids, f"{where} unit must be {expected}, not {_show(unit)}")
                valid = False
        if valid and isinstance(declaration, dict):
            for key, phrase, accepts in _BOUNDS:
                bound = declaration.get(key)
                if _is_number(bound) and not accepts(value, bound):
                    self.error("out_of_bounds", ids, f"{where} value {value!r} must be {phrase} {bound!r}")
                    valid = False
        return value if valid else None

    def check_ordered(self, kind, values, ids, where):
        ordered = True
        for lower, upper in _ORDERED_PARAMETERS.get(kind, ()):
            if lower in values and upper in values and not values[lower] < values[upper]:
                self.error(
                    "out_of_bounds",
                    ids,
                    f"{where} {lower} ({values[lower]!r}) must lie within {upper} ({values[upper]!r})",
                )
                ordered = False
        return ordered

    def check_observes(self, eid, record, ids, where):
        observes = record["observes"]
        label = f"{where} observes"
        if not self.report.fields(observes, OBSERVES_FIELDS, (), ids, label):
            return
        if record.get("kind") != "indicator":
            self.error("invalid_observes", ids, f"{where} is not an indicator; only indicators observe other entities")
        if "entity_id" in observes:
            target = self.ref(observes["entity_id"], self.entities, "entity", ids, f"{label}.entity_id")
            if target == eid:
                self.error("invalid_observes", ids, f"{where} cannot observe itself")
        if "variable" in observes and not _is_text(observes["variable"]):
            self.error(
                "invalid_observes",
                ids,
                f"{label}.variable must be a nonempty string, not {_show(observes['variable'])}",
            )

    def check_port(self, pid, record):
        ids = [pid]
        where = f"port {pid!r}"
        if not self.report.fields(record, PORT_FIELDS, PORT_OPTIONAL_FIELDS, ids, where):
            return
        unresolved = "connection_status" in record
        if unresolved and record["connection_status"] != CONNECTION_STATUS_UNRESOLVED:
            self.error(
                "invalid_connection_status",
                ids,
                f"{where} connection_status must be {CONNECTION_STATUS_UNRESOLVED!r} (an explicitly open draft port) or "
                f"absent, not {_show(record['connection_status'])}",
            )
            unresolved = False
        self.check_id(pid, record, ids, where)
        owner = None
        if "entity_id" in record:
            owner = self.ref(record["entity_id"], self.entities, "entity", ids, f"{where} entity_id")
            ids = [pid, owner]
        name = record.get("name")
        if "name" in record and not _is_text(name):
            self.error("invalid_value", ids, f"{where} name must be a nonempty string, not {_show(name)}")
        direction = record.get("direction")
        if "direction" in record and not _one_of(direction, PORT_DIRECTIONS):
            self.error("invalid_value", ids, f"{where} direction must be in, out or both, not {_show(direction)}")
        required = record.get("required")
        self.check_flag(record, "required", ids, where)
        dn = self.check_dn(record["nominal_diameter_mm"], ids, where) if "nominal_diameter_mm" in record else None
        service = record.get("service")
        if "service" in record and not _one_of(service, SERVICES):
            self.error("invalid_service", ids, f"{where} service must be one of {', '.join(SERVICES)}, not {_show(service)}")
        if "assumption_id" in record:
            self.assumption_ref(record["assumption_id"], ids, where)
        info = {
            "entity": owner,
            "name": name if _is_text(name) else None,
            "direction": direction if _one_of(direction, PORT_DIRECTIONS) else None,
            "required": required if isinstance(required, bool) else None,
            "dn": dn,
            "service": service if _one_of(service, SERVICES) else None,
            "declared": False,
            "template_required": False,
            "unresolved": unresolved,
        }
        self.port_info[pid] = info
        if owner is not None and info["name"] is not None:
            self.ports_by_entity.setdefault(owner, []).append(pid)

    def check_dn(self, value, ids, where):
        if _is_number(value) and value > 0 and float(value).is_integer():
            return value
        self.error(
            "invalid_nominal_diameter",
            ids,
            f"{where} nominal_diameter_mm must be a positive whole-number DN designation, not {_show(value)}",
        )
        return None

    def check_entity_ports(self):
        for eid, template in self.entity_template.items():
            declared = template["ports"]
            claimed = {}
            for pid in self.ports_by_entity.get(eid, ()):
                info = self.port_info[pid]
                name = info["name"]
                ids = [pid, eid]
                where = f"port {pid!r}"
                if name not in declared:
                    self.error(
                        "unknown_port",
                        ids,
                        f"{where} name {name!r} is not declared by the {_show(self.entities[eid].get('kind'))} template",
                    )
                    continue
                if name in claimed:
                    self.error("duplicate_port_name", [eid, claimed[name], pid], f"entity {eid!r} has more than one port named {name!r}")
                    continue
                claimed[name] = pid
                spec = declared[name] if isinstance(declared[name], dict) else {}
                info["declared"] = True
                info["template_required"] = spec.get("required") is True
                expected = spec.get("direction")
                if info["direction"] is not None and expected is not None and info["direction"] != expected:
                    self.error(
                        "wrong_port_direction",
                        ids,
                        f"{where} direction {info['direction']!r} contradicts template direction {expected!r}",
                    )
                expected = spec.get("required")
                if info["required"] is not None and isinstance(expected, bool) and info["required"] != expected:
                    self.error(
                        "port_template_mismatch",
                        ids,
                        f"{where} required={info['required']} contradicts template required={expected}",
                    )
                # The declared DN is the template's only supported size, not a default: version 1
                # nozzle geometry does not scale with DN, and DN is not an outside diameter.
                expected = spec.get("nominal_diameter_mm")
                if info["dn"] is not None and _is_number(expected) and info["dn"] != expected:
                    self.error(
                        "unsupported_nominal_diameter",
                        ids,
                        f"{where} is DN{_dn(info['dn'])}, but the {_show(self.entities[eid].get('kind'))} template "
                        f"version {_show(template.get('version'))} supports only DN{_dn(expected)}",
                    )
                expected = spec.get("service")
                if info["service"] is not None and expected is not None and info["service"] != expected:
                    self.error(
                        "port_template_mismatch",
                        ids,
                        f"{where} service {info['service']!r} contradicts template service {expected!r}",
                    )
            for name, spec in declared.items():
                if isinstance(spec, dict) and spec.get("required") is True and name not in claimed:
                    self.error("missing_required_port", [eid], f"entity {eid!r} is missing required port {name!r}")

    def check_connection(self, cid, record):
        ids = [cid]
        where = f"connection {cid!r}"
        if not self.report.fields(record, CONNECTION_FIELDS, (), ids, where):
            return
        self.check_id(cid, record, ids, where)
        self.collect_tag(cid, record, where)
        ends = {}
        for role in ("from", "to"):
            key = f"{role}_port_id"
            ends[role] = self.ref(record[key], self.ports, "port", ids, f"{where} {key}") if key in record else None
        if ends["from"] is not None and ends["from"] == ends["to"]:
            self.error("self_connection", [cid, ends["from"]], f"{where} connects port {ends['from']!r} to itself")
        dn = self.check_dn(record["nominal_diameter_mm"], ids, where) if "nominal_diameter_mm" in record else None
        if "outside_diameter_m" in record:
            outside = record["outside_diameter_m"]
            if not _is_number(outside):
                self.error("invalid_number", ids, f"{where} outside_diameter_m must be a finite number, not {_show(outside)}")
            elif outside <= 0:
                self.error("out_of_bounds", ids, f"{where} outside_diameter_m must be positive, not {outside!r}")
        if "assumption_id" in record:
            self.assumption_ref(record["assumption_id"], ids, where)
        self.connection_info[cid] = ends
        for port_id in dict.fromkeys(p for p in (ends["from"], ends["to"]) if p is not None):
            self.port_uses.setdefault(port_id, []).append(cid)
        services = []
        for role in ("from", "to"):
            info = self.port_info.get(ends[role])
            if info is None:
                continue
            port_ids = [cid, ends[role], info["entity"]]
            allowed = _ALLOWED_DIRECTIONS[role]
            if info["direction"] is not None and info["direction"] not in allowed:
                self.error(
                    "wrong_port_direction",
                    port_ids,
                    f"{where} {role} port {ends[role]!r} has direction {info['direction']!r}; "
                    f"expected {' or '.join(allowed)}",
                )
            if dn is not None and info["dn"] is not None and info["dn"] != dn:
                self.error(
                    "dn_mismatch",
                    port_ids,
                    f"{where} is DN{_dn(dn)} but its {role} port {ends[role]!r} is DN{_dn(info['dn'])}",
                )
            if info["service"] is not None:
                services.append(info["service"])
        if len(services) == 2 and services[0] != services[1]:
            self.error(
                "service_mismatch",
                [cid, ends["from"], ends["to"]],
                f"{where} joins {services[0]!r} and {services[1]!r} service ports",
            )

    def check_port_uses(self):
        for pid, info in self.port_info.items():
            uses = self.port_uses.get(pid, [])
            if len(uses) > 1:
                self.error(
                    "duplicate_port_use",
                    [pid, info["entity"], *uses],
                    f"port {pid!r} is used by {len(uses)} connections ({', '.join(uses)}); "
                    "branches require an explicit junction entity",
                )
            elif uses and info["unresolved"]:
                self.error(
                    "contradictory_connection_status",
                    [pid, info["entity"], *uses],
                    f"port {pid!r} is connected by {uses[0]!r} but still marked connection_status 'unresolved'; "
                    "connecting a port must clear its open-draft marker",
                )
            elif not uses and (info["required"] is True or info["template_required"]):
                if info["unresolved"]:
                    # An explicitly open draft port: an authored open item, never corruption and never complete.
                    self.warning(
                        "required_port_unconnected",
                        [pid, info["entity"]],
                        f"required port {pid!r} ({_show(info['name'])} of entity {_show(info['entity'])}) is "
                        "explicitly unresolved (connection_status 'unresolved'); outputs stay pending until it is "
                        "connected",
                    )
                else:
                    self.error(
                        "required_port_unconnected",
                        [pid, info["entity"]],
                        f"required port {pid!r} ({_show(info['name'])} of entity {_show(info['entity'])}) "
                        "is not connected",
                    )
            elif not uses and info["unresolved"]:
                self.error(
                    "invalid_connection_status",
                    [pid, info["entity"]],
                    f"port {pid!r} is not required, so it cannot be marked connection_status 'unresolved'; the "
                    "marker is only for intentionally open required ports",
                )

    # P&ID presentation

    def check_symbol(self, sid, record):
        ids = [sid]
        where = f"symbol {sid!r}"
        if not self.report.fields(record, SYMBOL_FIELDS, (), ids, where):
            return
        self.check_id(sid, record, ids, where)
        owner = None
        if "entity_id" in record:
            owner = self.ref(record["entity_id"], self.entities, "entity", ids, f"{where} entity_id")
            ids = [sid, owner]
        positioned = True
        for key in ("x", "y", "rotation_deg"):
            if key not in record:
                positioned = False
            elif not _is_number(record[key]):
                self.error("invalid_number", ids, f"{where} {key} must be a finite number, not {_show(record[key])}")
                positioned = False
        self.check_flag(record, "manual", ids, where)
        self.symbol_info[sid] = {"entity": owner, "positioned": positioned}
        if owner is not None:
            self.symbols_by_entity.setdefault(owner, []).append(sid)

    def check_edge(self, edge_id, record):
        ids = [edge_id]
        where = f"edge {edge_id!r}"
        if not self.report.fields(record, EDGE_FIELDS, REASON_FIELD, ids, where):
            return
        self.check_id(edge_id, record, ids, where)
        cid = None
        if "connection_id" in record:
            cid = self.ref(record["connection_id"], self.connections, "connection", ids, f"{where} connection_id")
        if cid is not None:
            ids = [edge_id, cid]
            self.edges_by_connection.setdefault(cid, []).append(edge_id)
        symbols = {}
        for role in ("from", "to"):
            key = f"{role}_symbol_id"
            symbols[role] = self.ref(record[key], self.symbols, "symbol", ids, f"{where} {key}") if key in record else None
        points_ok = "points" in record and self.check_points(record["points"], 2, ids, f"{where} points")
        self.check_flag(record, "manual", ids, where)
        status = self.check_status(record, PATH_STATUSES, ids, where)
        self.check_reason(record, status is not None and status != "current", ids, where)
        consistent = self.check_occurrences(edge_id, cid, symbols, where)
        if status == "current":
            if points_ok and self.enough_points(record["points"], ids, where) and consistent:
                self.check_edge_ends(cid, symbols, record["points"], ids, where)
        elif status is not None:
            self.warning("edge_not_current", ids, f"{where} is {status}{_because(record)}")

    def check_occurrences(self, edge_id, cid, symbols, where):
        """Check that the edge's symbols show the entities owning its connection's ports."""
        ends = self.connection_info.get(cid)
        if ends is None:
            return False
        consistent = True
        for role in ("from", "to"):
            port = self.port_info.get(ends[role])
            symbol = self.symbol_info.get(symbols[role])
            if port is None or symbol is None or port["entity"] is None or symbol["entity"] is None:
                consistent = False
                continue
            if symbol["entity"] != port["entity"]:
                self.error(
                    "occurrence_mismatch",
                    [edge_id, symbols[role], symbol["entity"], port["entity"]],
                    f"{where} {role}_symbol_id {symbols[role]!r} shows entity {symbol['entity']!r}, "
                    f"but the {role} port of connection {cid!r} belongs to entity {port['entity']!r}",
                )
                consistent = False
        return consistent

    def check_edge_ends(self, cid, symbols, points, ids, where):
        ends = self.connection_info[cid]
        for role, index, verb in (("from", 0, "starts"), ("to", -1, "ends")):
            anchor = self.diagram_anchor(ends[role], symbols[role], ids, where)
            if anchor is None:
                continue
            if math.dist(points[index], anchor) > DIAGRAM_TOLERANCE:
                self.error(
                    "edge_endpoint_mismatch",
                    ids + [symbols[role], ends[role]],
                    f"{where} is current but {verb} at {_point(points[index])}, not at the anchor "
                    f"{_point(anchor)} of port {ends[role]!r} on symbol {symbols[role]!r}",
                )

    def diagram_anchor(self, port_id, symbol_id, ids, where):
        if not self.port_info[port_id]["declared"] or not self.symbol_info[symbol_id]["positioned"]:
            return None  # the underlying record errors are already reported
        try:
            anchor = diagram_port_position(self.view, port_id, symbol_id)
        except Exception as exc:  # report template faults as diagnostics
            self.error(
                "geometry_unavailable",
                ids,
                f"{where}: anchor of port {port_id!r} on symbol {symbol_id!r} is unavailable "
                f"({_describe_exception(exc)})",
            )
            return None
        if not _is_vector(anchor, 2):
            self.error("geometry_unavailable", ids, f"{where}: template returned an invalid anchor {_show(anchor)}")
            return None
        return anchor

    # Physical presentation

    def check_placement(self, eid, record):
        ids = [eid]
        where = f"placement of {eid!r}"
        if eid not in self.entities:
            self.error("dangling_reference", ids, f"{where} refers to an unknown entity")
        self.placement_state[eid] = "invalid"
        if not self.report.fields(record, PLACEMENT_FIELDS, REASON_FIELD, ids, where):
            return
        status = self.check_status(record, PLACEMENT_STATUSES, ids, where)
        self.check_flag(record, "manual", ids, where)
        self.check_reason(record, status == "unresolved", ids, where)
        if status == "placed":
            posed = True
            for key in ("position_m", "rotation_deg"):
                if key not in record:
                    posed = False
                elif not _is_vector(record[key], 3):
                    self.error("invalid_number", ids, f"{where} {key} must be three finite numbers, not {_show(record[key])}")
                    posed = False
            if "assumption_id" in record:
                self.assumption_ref(record["assumption_id"], ids, where)
            if posed:
                self.placement_state[eid] = "placed"
        elif status == "unresolved":
            for key in ("position_m", "rotation_deg"):
                if record.get(key) is not None:
                    self.error("invalid_value", ids, f"{where} is unresolved, so {key} must be null, not {_show(record[key])}")
            if "assumption_id" in record:
                self.assumption_ref(record["assumption_id"], ids, where, allow_null=True)
            self.placement_state[eid] = "unresolved"
            self.warning("placement_unresolved", ids, f"{where} is unresolved{_because(record)}")

    def check_route(self, cid, record):
        ids = [cid]
        where = f"route of {cid!r}"
        if cid not in self.connections:
            self.error("dangling_reference", ids, f"{where} refers to an unknown connection")
        if not self.report.fields(record, ROUTE_FIELDS, REASON_FIELD, ids, where):
            return
        points_ok = "points_m" in record and self.check_points(record["points_m"], 3, ids, f"{where} points_m")
        if "mode" in record and not _one_of(record["mode"], ROUTE_MODES):
            self.error("invalid_value", ids, f"{where} mode must be guided or manual, not {_show(record['mode'])}")
        status = self.check_status(record, PATH_STATUSES, ids, where)
        stale = status is not None and status != "current"
        self.check_reason(record, stale, ids, where)
        if "assumption_id" in record:
            self.assumption_ref(record["assumption_id"], ids, where, allow_null=stale)
        if status == "current":
            if points_ok and self.enough_points(record["points_m"], ids, where):
                points = record["points_m"]
                self.check_orthogonal(points, ids, where)
                ends = self.connection_info.get(cid)
                if ends is not None:
                    self.check_route_end(ends["from"], points[0], "starts", ids, where)
                    self.check_route_end(ends["to"], points[-1], "ends", ids, where)
        elif stale:
            self.warning("route_not_current", ids, f"{where} is {status}{_because(record)}")

    def check_orthogonal(self, points, ids, where):
        for index in range(1, len(points)):
            start, end = points[index - 1], points[index]
            moving = sum(1 for axis in range(3) if abs(end[axis] - start[axis]) > ROUTE_TOLERANCE_M)
            if moving != 1:
                problem = "has zero length" if moving == 0 else "is not parallel to an axis"
                self.error("route_not_orthogonal", ids, f"{where} segment {index - 1}->{index} {problem}")
                return

    def check_route_end(self, port_id, point, verb, ids, where):
        port = self.port_info.get(port_id)
        if port is None or port["entity"] is None:
            return
        entity = port["entity"]
        end_ids = ids + [port_id, entity]
        state = self.placement_state.get(entity)
        if state is None or state == "unresolved":
            self.error(
                "route_endpoint_unplaced",
                end_ids,
                f"{where} is current but {verb} at port {port_id!r} of entity {entity!r}, which has no resolved placement",
            )
            return
        if state != "placed" or entity not in self.geometry_ok or not port["declared"]:
            return  # the underlying record errors are already reported
        try:
            expected = world_port_position(self.view, port_id)
        except Exception as exc:  # report template faults as diagnostics
            self.error(
                "geometry_unavailable",
                end_ids,
                f"{where}: position of port {port_id!r} is unavailable ({_describe_exception(exc)})",
            )
            return
        if expected is None:
            self.error("route_endpoint_unplaced", end_ids, f"{where} is current but port {port_id!r} has no world position")
            return
        if not _is_vector(expected, 3):
            self.error("geometry_unavailable", end_ids, f"{where}: template returned an invalid position {_show(expected)}")
            return
        gap = math.dist(point, expected)
        if gap > ROUTE_TOLERANCE_M:
            self.error(
                "route_endpoint_mismatch",
                end_ids,
                f"{where} {verb} at {_point(point)}, {gap:.6g} m from port {port_id!r} at {_point(expected)}",
            )

    # Whole-project checks

    def check_completeness(self):
        for eid in self.entities:
            if eid not in self.placements:
                self.warning("placement_missing", [eid], f"entity {eid!r} has no physical placement record")
            if eid not in self.symbols_by_entity:
                self.warning("symbol_missing", [eid], f"entity {eid!r} has no P&ID symbol occurrence")
        for cid in self.connections:
            if cid not in self.routes:
                self.warning("route_missing", [cid], f"connection {cid!r} has no physical route record")
            if cid not in self.edges_by_connection:
                self.warning("edge_missing", [cid], f"connection {cid!r} has no P&ID edge")

    def check_tags(self):
        for owners in self.tags.values():
            if len(owners) > 1:
                listed = ", ".join(f"{owner!r} ({tag!r})" for owner, tag in owners)
                self.error(
                    "duplicate_tag",
                    [owner for owner, _ in owners],
                    f"tag {owners[0][1]!r} is not unique (case and whitespace are ignored): {listed}",
                )

    def check_unique_ids(self):
        owners = {}
        for name in ("entities", "ports", "connections", "symbols", "edges"):
            for key in getattr(self, name):
                owners.setdefault(key, []).append(name)
        for key, names in owners.items():
            if len(names) > 1:
                self.error("duplicate_id", [key], f"ID {key!r} is used by more than one record ({', '.join(names)})")


def _diagnostic(code, severity, ids, message):
    entity_ids = []
    for item in ids:
        if isinstance(item, str) and item and item not in entity_ids:
            entity_ids.append(item)
    return {"code": code, "severity": severity, "entity_ids": entity_ids, "message": message}


def _is_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # integers beyond float range are not usable quantities
        return False


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _is_id(value):
    return _is_text(value)


def _is_revision(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_vector(value, size):
    return isinstance(value, (list, tuple)) and len(value) == size and all(_is_number(c) for c in value)


def _one_of(value, options):
    return isinstance(value, str) and value in options


def _type(value):
    return type(value).__name__


def _show(value, limit=80):
    try:
        text = repr(value)
    except Exception:  # unrepresentable values (for example, very deep nesting)
        text = f"<{_type(value)}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_exception(exc):
    try:
        text = str(exc)
    except Exception:
        text = ""
    text = text if len(text) <= 120 else text[:117] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _point(point):
    return "[" + ", ".join(f"{coordinate:.6g}" for coordinate in point) + "]"


def _dn(value):
    return int(value)


def _because(record):
    reason = record.get("reason")
    return f": {reason}" if _is_text(reason) else ""
