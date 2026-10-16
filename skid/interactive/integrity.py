"""Integrity checks for five-artifact interactive builds (workflow contract 0.2).

The generation worker runs these checks before it reports success, and the
service reruns them from disk after the worker exits, before publication, and
whenever a published build is re-read. A stored success flag is never trusted:
identities, statuses, diagnostics, mappings, file sets, digests and active
content (scripts or external references in SVG/HTML) are rechecked.

A build directory ``builds/<build_id>/`` holds the frozen ``snapshot.json``,
``request.json``, every generator output, ``preview/`` snapshots, the worker's
``result.json``, its ``worker.log``, the service's ``events.jsonl`` and, once
the build is over, the service's seal ``build.json``. The seal records the
SHA-256 of every file except the two logs, the seal itself and ``result.json``.
The publish pointer ``current.json`` names one sealed build and the digest of
its seal.
"""

from __future__ import annotations

import contextlib
import hashlib
import html.parser
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from ..core import DocumentError, canonical_digest, load_document, validate_project

REQUIRED_ARTIFACTS = ("display", "cad", "pid", "legend", "description")
GENERATORS = {"geometry": ("display", "cad"), "drafting": ("pid", "legend", "description")}
ADAPTER_VERSION = "0.2.0"
LABELS = {"display": "display", "cad": "CAD", "pid": "P&ID", "legend": "legend", "description": "description"}
MANIFEST_FIELDS = ("adapter", "adapter_version", "project_id", "source_revision", "input_digest", "status",
                   "diagnostics", "representations", "files")
DIAGNOSTIC_FIELDS = ("code", "severity", "entity_ids", "message")
# Files each adapter must list in its manifest, and files each generator must list across its manifests.
PRIMARY_FILES = {
    "display": ("scene.json",),
    "cad": ("model.step",),
    "pid": ("pid.svg",),
    "legend": ("legend.svg",),
    "description": ("description.json", "description.html"),
}
GROUP_FILES = {
    "cad": ("geometry", ("geometry.recipe.json", "cad.verification.json")),
    "pid": ("drafting", ("drawing.inventory.json", "item-index.json")),
}
# Published URL keys -> file names inside a build directory.
PUBLISHED_FILES = {
    "display": "scene.json",
    "cad": "model.step",
    "pid": "pid.svg",
    "legend": "legend.svg",
    "description": "description.html",
    "index": "item-index.json",
    "inventory": "drawing.inventory.json",
    "recipe": "geometry.recipe.json",
    "verification": "cad.verification.json",
}
DESCRIPTION_FIELDS = ("project_id", "source_revision", "input_digest", "purpose", "flow_sequence", "items",
                      "assumptions", "unknowns", "manual_notes", "conflicts")

SNAPSHOT, REQUEST, RESULT, SEAL, EVENTS, WORKER_LOG = (
    "snapshot.json", "request.json", "result.json", "build.json", "events.jsonl", "worker.log")
NOTES_COPY = "manual-notes.json"
POINTER = "current.json"
UNSEALED_FILES = frozenset({RESULT, SEAL, EVENTS, WORKER_LOG})
SEAL_FORMAT = POINTER_FORMAT = 2
BUILD_ID_PATTERN = re.compile(r"^b\d{8}T\d{6}-r\d{1,12}-[0-9a-f]{8}$")
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_STATUSES = ("complete", "pending")


def diagnostic(code, message, ids=(), severity="error"):
    return {"code": code, "severity": severity, "entity_ids": [str(i) for i in ids if i], "message": message}


def manifest_name(artifact):
    return f"{artifact}.manifest.json"


# --- Files ---------------------------------------------------------------------


def parse_json(data):
    """Strict JSON from bytes or text: duplicate keys and nonfinite numbers raise ``ValueError``."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise ValueError(f"nonfinite number {name}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def read_json(path):
    with open(path, "rb") as handle:
        return parse_json(handle.read())


def json_bytes(data) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def write_atomic(path, data: bytes) -> None:
    directory, name = os.path.split(path)
    temporary = os.path.join(directory, f".{name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relpath(value):
    """``value`` if it is a plain relative path of visible, simple names joined by '/'; else ``None``."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    parts = value.split("/")
    if len(parts) > 8 or not all(_SAFE_PART.match(part) for part in parts):
        return None
    return value


def resolve_inside(root, relpath):
    """Absolute real path of the regular file ``relpath`` inside ``root``; ``None`` if unsafe or absent."""
    relpath = safe_relpath(relpath)
    if relpath is None:
        return None
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, *relpath.split("/")))
    if os.path.commonpath([base, target]) != base or target == base:
        return None
    return target if os.path.isfile(target) else None


def hash_tree(build_dir, exclude=UNSEALED_FILES):
    """``{relative path: sha256}`` for every regular file under ``build_dir`` except top-level ``exclude``."""
    files = {}
    base = os.path.realpath(build_dir)
    for directory, subdirs, names in os.walk(base):
        subdirs.sort()
        for name in sorted(names):
            full = os.path.join(directory, name)
            relpath = os.path.relpath(full, base).replace(os.sep, "/")
            if relpath in exclude or name.startswith(".") and name.endswith(".tmp"):
                continue
            if os.path.isfile(full) and not os.path.islink(full):
                files[relpath] = sha256_file(full)
            else:
                files[relpath] = None  # a symlink or special file: never sealed as valid content
    return files


@contextlib.contextmanager
def file_lock(path, timeout):
    """Exclusive ``flock`` on ``path`` (the same lock ``save_document`` takes on ``<project>.lock``)."""
    if fcntl is None:
        raise OSError("file locking (fcntl.flock) is unavailable on this platform")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out after {timeout:g} s waiting for {path}") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- Active content ------------------------------------------------------------

_SVG_FORBIDDEN = {"script", "foreignobject", "iframe", "object", "embed", "handler", "listener", "audio", "video"}
_ANIMATION = {"set", "animate", "animatemotion", "animatetransform"}
_HTML_FORBIDDEN = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "form", "input",
                   "button", "textarea", "select", "link", "portal", "noscript"}
_BAD_SCHEMES = ("javascript:", "vbscript:", "data:text/html", "data:application", "data:image/svg")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
_CSS_BAD = re.compile(r"@import|expression\s*\(|url\s*\(\s*['\"]?\s*(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
_REF_ATTRIBUTES = {"href", "src", "xlink:href", "action", "formaction", "poster", "background", "srcset"}


def _local(name):
    return name.rsplit("}", 1)[-1].lower()


def _reference_problem(element, attribute, value, allow_links):
    text = "".join(str(value).split()).lower()
    if text.startswith(_BAD_SCHEMES):
        return f"<{element}> {attribute} uses an active URL scheme"
    if _EXTERNAL.match(text) and not text.startswith(("data:image/png", "data:image/jpeg", "data:image/gif",
                                                      "data:image/webp")):
        if allow_links and element == "a" and attribute in ("href", "xlink:href") and text.startswith(("http:", "https:")):
            return None
        return f"<{element}> {attribute} references external content {str(value)[:80]!r}"
    return None


def svg_problems(data: bytes) -> list[str]:
    """Reasons ``data`` is not a self-contained, script-free SVG document (empty if acceptable)."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        return ["SVG declares a DOCTYPE or entities"]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        return [f"SVG is not well-formed XML ({exc})"]
    if _local(root.tag) != "svg":
        return [f"root element is <{_local(root.tag)}>, not <svg>"]
    problems = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _local(element.tag)
        if name in _SVG_FORBIDDEN:
            problems.append(f"SVG contains a <{name}> element")
        if name == "style" and _CSS_BAD.search(element.text or ""):
            problems.append("SVG <style> imports or references external content")
        for key, value in element.attrib.items():
            attribute = _local(key) if not key.startswith("{http://www.w3.org/1999/xlink}") else "xlink:href"
            if attribute.startswith("on"):
                problems.append(f"<{name}> has an event handler attribute {attribute}")
            elif attribute in _REF_ATTRIBUTES:
                problem = _reference_problem(name, attribute, value, allow_links=False)
                if problem:
                    problems.append(problem)
            elif attribute == "style" and _CSS_BAD.search(value):
                problems.append(f"<{name}> style references external content")
            if name in _ANIMATION and attribute == "attributename" and ("href" in value.lower()
                                                                        or value.lower().startswith("on")):
                problems.append(f"<{name}> animates {value}")
        if len(problems) >= 5:
            break
    return problems


class _HTMLScanner(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self._style = False

    def handle_starttag(self, tag, attrs):
        self._check(tag, attrs)
        self._style = tag == "style"

    def handle_startendtag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "style":
            self._style = False

    def handle_data(self, data):
        if self._style and _CSS_BAD.search(data):
            self.problems.append("<style> imports or references external content")

    def _check(self, tag, attrs):
        if tag in _HTML_FORBIDDEN:
            self.problems.append(f"HTML contains a <{tag}> element")
        if tag == "meta" and any(name == "http-equiv" for name, _ in attrs):
            self.problems.append("HTML contains <meta http-equiv>")
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append(f"<{tag}> has an event handler attribute {name}")
            elif name in _REF_ATTRIBUTES and value is not None:
                problem = _reference_problem(tag, name, value, allow_links=True)
                if problem:
                    self.problems.append(problem)
            elif name == "style" and value and _CSS_BAD.search(value):
                self.problems.append(f"<{tag}> style references external content")


def html_problems(data: bytes) -> list[str]:
    """Reasons ``data`` is not script-free, self-contained HTML (outbound <a> links are allowed)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"HTML is not UTF-8 ({exc})"]
    scanner = _HTMLScanner()
    try:
        scanner.feed(text)
        scanner.close()
    except Exception as exc:  # html.parser is lenient; any fault fails closed
        return [f"HTML cannot be scanned ({type(exc).__name__}: {exc})"]
    return scanner.problems[:5]


def content_problems(path, relpath, project=None, digest=None, required_identity=False) -> list[str]:
    """Format and safety problems of one output file judged by its extension."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return [f"{relpath} cannot be read ({exc})"]
    lower = relpath.lower()
    if lower.endswith(".svg"):
        return [f"{relpath}: {p}" for p in svg_problems(data)]
    if lower.endswith((".html", ".htm")):
        return [f"{relpath}: {p}" for p in html_problems(data)]
    if lower.endswith(".json"):
        try:
            value = parse_json(data)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            return [f"{relpath} is not strict JSON ({exc})"]
        if project is None:
            return []
        return [f"{relpath}: {p}" for p in identity_problems(value, project, digest, required_identity)]
    if lower.endswith((".step", ".stp")):
        if not data.lstrip()[:13] == b"ISO-10303-21;":
            return [f"{relpath} does not begin with an ISO-10303-21 header"]
    return []


def identity_problems(value, project, digest, required) -> list[str]:
    """Source identity mismatches of a JSON artifact; fields may be absent unless ``required``."""
    if not isinstance(value, dict):
        return ["is not a JSON object"] if required else []
    problems = []
    expected = {"project_id": project["id"], "source_revision": project["revision"], "input_digest": digest}
    for key, wanted in expected.items():
        if key not in value:
            if required:
                problems.append(f"has no {key}")
        elif value[key] != wanted or (key == "source_revision" and type(value[key]) is not int):
            problems.append(f"{key} is {str(value[key])[:80]!r}, not {wanted!r}")
    return problems


# --- Manifests -----------------------------------------------------------------


def diagnostic_problem(item):
    """Why ``item`` is not a ``{code, severity, entity_ids, message}`` record; ``None`` if it is."""
    if not isinstance(item, dict):
        return "is not an object"
    if set(item) != set(DIAGNOSTIC_FIELDS):
        return f"has fields {sorted(map(str, item))}, not {sorted(DIAGNOSTIC_FIELDS)}"
    if not isinstance(item["code"], str) or not item["code"]:
        return "code is not a nonempty string"
    if item["severity"] not in ("error", "warning"):
        return f"severity {str(item['severity'])[:20]!r} is not error or warning"
    if not isinstance(item["entity_ids"], list) or not all(isinstance(i, str) and i for i in item["entity_ids"]):
        return "entity_ids is not a list of ID strings"
    if not isinstance(item["message"], str):
        return "message is not a string"
    return None


def known_ids(project):
    model = project["model"]
    return (set(model["entities"]) | set(model["ports"]) | set(model["connections"]) | set(model["assumptions"])
            | {project["id"]})


def required_ids(project, artifact):
    """Semantic IDs ``artifact``'s manifest must map (possibly to an explicit empty list)."""
    model = project["model"]
    if artifact in ("display", "cad", "pid"):
        return set(model["entities"]) | set(model["ports"]) | set(model["connections"])
    if artifact == "description":
        return set(model["entities"]) | set(model["connections"])
    return set()


def check_manifest(manifest, artifact, project, digest, build_dir) -> list[dict]:
    """Error diagnostics unless ``manifest`` is a usable 0.2 manifest of ``project`` by ``artifact``'s adapter.

    An error diagnostic fails it whatever its status, ``failed`` fails it, and a
    ``complete`` manifest may list no warnings. Every listed file must be a safe
    relative path to a nonempty regular file in ``build_dir``.
    """
    ids = [project["id"]]
    label = LABELS[artifact]
    if not isinstance(manifest, dict):
        return [diagnostic("manifest_missing", f"the {label} generator produced no readable manifest", ids)]
    problems = []

    def bad(code, message, entity_ids=None):
        problems.append(diagnostic(code, f"{label} manifest: {message}", entity_ids or ids))

    missing = [name for name in MANIFEST_FIELDS if name not in manifest]
    if missing:
        bad("manifest_fields", f"missing {', '.join(missing)}")
    if manifest.get("adapter") != artifact:
        bad("manifest_mismatch", f"adapter is {str(manifest.get('adapter'))[:40]!r}, expected {artifact!r}")
    if manifest.get("adapter_version") != ADAPTER_VERSION:
        bad("unsupported_adapter_version", f"adapter_version {str(manifest.get('adapter_version'))[:40]!r} "
            f"is not {ADAPTER_VERSION!r}")
    if manifest.get("project_id") != project["id"]:
        bad("manifest_mismatch", f"project_id {str(manifest.get('project_id'))[:80]!r} is not {project['id']!r}")
    revision = manifest.get("source_revision")
    if type(revision) is not int or revision != project["revision"]:
        bad("manifest_mismatch", f"source_revision {str(revision)[:20]!r} is not {project['revision']}")
    if manifest.get("input_digest") != digest:
        bad("manifest_mismatch", f"input_digest {str(manifest.get('input_digest'))[:80]!r} is not the snapshot "
            f"digest {digest}")

    records = []
    if not isinstance(manifest.get("diagnostics"), list):
        bad("diagnostics_invalid", "diagnostics is not a list")
    else:
        malformed = []
        for index, item in enumerate(manifest["diagnostics"]):
            reason = diagnostic_problem(item)
            if reason is None:
                records.append(item)
            else:
                malformed.append(f"diagnostics[{index}] {reason}")
        if malformed:
            bad("diagnostics_invalid", f"{len(malformed)} malformed diagnostic(s): {'; '.join(malformed[:3])}")
    errors = [d for d in records if d["severity"] == "error"]
    warnings = [d for d in records if d["severity"] == "warning"]
    status = manifest.get("status")
    if status == "failed":
        bad("adapter_failed", "the generator reported status 'failed'")
    elif status not in _REQUIRED_STATUSES:
        bad("manifest_status", f"status {str(status)[:20]!r} is not complete, pending, or failed")
    else:
        if errors:
            bad("adapter_reported_error", f"status is {status!r} but {len(errors)} error diagnostic(s) were reported")
        if status == "complete" and warnings:
            listed = "; ".join(f"[{d['code']}] {d['message'][:120]}" for d in warnings[:3])
            bad("manifest_contradiction", f"status is 'complete' but {len(warnings)} warning(s) remain open: {listed}")
    for item in errors:
        problems.append(diagnostic(item["code"], f"{label}: {item['message']}", item["entity_ids"] or ids))

    representations = manifest.get("representations")
    if not isinstance(representations, dict) or not all(
            isinstance(v, list) and all(isinstance(i, str) and i for i in v) for v in representations.values()):
        bad("mapping_invalid", "representations is not a map of semantic IDs to lists of representation IDs")
    else:
        unknown = sorted(set(representations) - known_ids(project))
        if unknown:
            bad("mapping_unknown_id", f"maps {len(unknown)} ID(s) absent from this revision: {', '.join(unknown[:6])}",
                ids + unknown[:6])
        absent = sorted(required_ids(project, artifact) - set(representations))
        if absent:
            bad("mapping_incomplete", f"no representation entry for {len(absent)} semantic ID(s): "
                f"{', '.join(absent[:6])}", ids + absent[:6])

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        bad("files_invalid", "files is not a nonempty list of relative paths")
    else:
        seen = set()
        for item in files:
            relpath = safe_relpath(item)
            if relpath is None:
                bad("unsafe_path", f"file entry {str(item)[:80]!r} is not a plain relative path")
                continue
            if relpath in seen:
                bad("files_invalid", f"{relpath} is listed twice")
            seen.add(relpath)
            full = resolve_inside(build_dir, relpath)
            if full is None:
                bad("artifact_missing", f"{relpath} is missing, outside the build, or not a regular file")
            elif os.path.getsize(full) == 0:
                bad("artifact_missing", f"{relpath} is empty")
        for name in PRIMARY_FILES[artifact]:
            if name not in seen:
                bad("artifact_missing", f"required file {name} is not listed")
    return problems


def _content_checks(artifact, manifest, project, digest, build_dir) -> list[dict]:
    ids = [project["id"]]
    problems = []
    required = {"scene.json", "description.json"}
    for relpath in manifest["files"]:
        full = resolve_inside(build_dir, relpath)
        for reason in content_problems(full, relpath, project, digest, required_identity=relpath in required):
            problems.append(diagnostic("artifact_content", f"{LABELS[artifact]}: {reason}", ids))
    if artifact == "display":
        try:
            scene = read_json(os.path.join(build_dir, "scene.json"))
        except (OSError, ValueError):
            scene = None
        if isinstance(scene, dict):
            expected = {"schema_version": 1, "units": "m", "up_axis": "Z"}
            for key, wanted in expected.items():
                if scene.get(key) != wanted:
                    problems.append(diagnostic("artifact_content", f"display: scene.json {key} is "
                                               f"{str(scene.get(key))[:20]!r}, not {wanted!r}", ids))
            if not isinstance(scene.get("components"), list) or not isinstance(scene.get("ports"), list):
                problems.append(diagnostic("artifact_content", "display: scene.json has no components and ports "
                                           "lists", ids))
    if artifact == "description":
        try:
            description = read_json(os.path.join(build_dir, "description.json"))
        except (OSError, ValueError):
            description = None
        if isinstance(description, dict):
            missing = [key for key in DESCRIPTION_FIELDS if key not in description]
            if missing:
                problems.append(diagnostic("artifact_content", f"description: description.json is missing "
                                           f"{', '.join(missing)}", ids))
    return problems


def _same_contract_fields(returned, on_disk):
    """Returned and written manifests agree on every contracted field (extra evidence fields may differ)."""
    if not isinstance(returned, dict) or not isinstance(on_disk, dict):
        return False
    return all(returned.get(name) == on_disk.get(name) for name in MANIFEST_FIELDS)


def verify_outputs(build_dir, project, digest, returned=None, skip=()) -> dict:
    """Recheck all five artifacts in ``build_dir``; return ``{artifact: {status, diagnostics, files}}``.

    ``returned`` maps artifacts to the manifests the generators returned; each
    must agree with its manifest file on the contracted fields (identity,
    status, diagnostics, representations, files). ``skip`` names artifacts whose generator did
    not finish (their status is decided by the caller). ``status`` is
    ``complete``, ``pending`` or ``failed``; ``diagnostics`` holds this check's
    errors followed by the manifest's own warnings.
    """
    results = {}
    manifests = {}
    for artifact in REQUIRED_ARTIFACTS:
        if artifact in skip:
            continue
        path = os.path.join(build_dir, manifest_name(artifact))
        problems = []
        try:
            manifest = read_json(path)
        except FileNotFoundError:
            manifest = None
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            manifest = None
            problems.append(diagnostic("manifest_unreadable", f"{LABELS[artifact]}: {manifest_name(artifact)} is "
                                       f"not strict JSON ({exc})", [project["id"]]))
        if not problems:
            problems += check_manifest(manifest, artifact, project, digest, build_dir)
        if returned is not None and artifact in returned and not _same_contract_fields(returned[artifact], manifest):
            problems.append(diagnostic("manifest_mismatch", f"{LABELS[artifact]}: the returned manifest's contract "
                                       f"fields differ from {manifest_name(artifact)}", [project["id"]]))
        if not problems:
            problems += _content_checks(artifact, manifest, project, digest, build_dir)
        manifests[artifact] = manifest
        warnings = []
        if isinstance(manifest, dict) and isinstance(manifest.get("diagnostics"), list):
            warnings = [d for d in manifest["diagnostics"]
                        if diagnostic_problem(d) is None and d["severity"] == "warning"]
        results[artifact] = {
            "status": "failed" if problems else manifest["status"],
            "diagnostics": problems + warnings,
            "files": list(manifest["files"]) if not problems else [],
        }
    for artifact, (generator, names) in GROUP_FILES.items():
        group = [a for a in GENERATORS[generator] if a in results]
        if len(group) != len(GENERATORS[generator]):
            continue
        listed = set()
        for member in group:
            manifest = manifests.get(member)
            if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
                listed.update(f for f in manifest["files"] if isinstance(f, str))
        missing = [name for name in names if name not in listed]
        if missing:
            results[artifact]["status"] = "failed"
            results[artifact]["diagnostics"].insert(0, diagnostic(
                "artifact_missing", f"the {generator} generator's manifests list none of {', '.join(missing)}",
                [project["id"]]))
            results[artifact]["files"] = []
    return results


def overall_status(project, results) -> str:
    """``complete`` only if all five verified complete and the model has no open items; else pending/failed/cancelled."""
    statuses = [results.get(a, {}).get("status", "failed") for a in REQUIRED_ARTIFACTS]
    if any(s == "failed" for s in statuses):
        return "failed"
    if any(s == "cancelled" for s in statuses):
        return "cancelled"
    if any(s not in ("complete", "pending") for s in statuses):
        return "failed"
    if any(s == "pending" for s in statuses):
        return "pending"
    if any(d["severity"] == "warning" for d in validate_project(project)):
        return "pending"
    return "complete"


# --- Seals and the publish pointer ----------------------------------------------


def verify_sealed(builds_root, build_id, require_complete=True):
    """Recheck a sealed build from disk; return ``(seal or None, project or None, problems)``."""
    if not isinstance(build_id, str) or not BUILD_ID_PATTERN.match(build_id):
        return None, None, [diagnostic("build_id_invalid", f"{str(build_id)[:80]!r} is not a build ID")]
    build_dir = os.path.join(builds_root, build_id)
    try:
        seal = read_json(os.path.join(build_dir, SEAL))
        files = dict(seal["files"])
        identity = (seal["project_id"], seal["source_revision"], seal["input_digest"])
        status = seal["status"]
    except FileNotFoundError:
        return None, None, [diagnostic("build_unsealed", f"build {build_id} has no {SEAL}")]
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        return None, None, [diagnostic("seal_unreadable", f"build {build_id}: {SEAL} is unreadable ({exc})")]
    problems = []
    if seal.get("build_id") != build_id or seal.get("format") != SEAL_FORMAT:
        problems.append(diagnostic("seal_mismatch", f"build {build_id}: {SEAL} names another build or format"))
    if require_complete and status != "complete":
        problems.append(diagnostic("seal_incomplete", f"build {build_id} is sealed {status!r}, not 'complete'"))
    required = {SNAPSHOT} | {manifest_name(a) for a in REQUIRED_ARTIFACTS}
    for name in sorted(required - set(files)):
        problems.append(diagnostic("seal_incomplete", f"build {build_id}: {name} is not sealed"))
    actual = hash_tree(build_dir)
    for relpath in sorted(set(actual) | set(files)):
        if relpath not in files:
            problems.append(diagnostic("build_file_unexpected", f"build {build_id}: {relpath} appeared after sealing"))
        elif relpath not in actual:
            problems.append(diagnostic("build_file_missing", f"build {build_id}: {relpath} is missing"))
        elif actual[relpath] != files[relpath]:
            problems.append(diagnostic("build_file_changed", f"build {build_id}: {relpath} differs from its sealed "
                                       "digest"))
        if len(problems) >= 12:
            break
    if problems:
        return seal, None, problems
    try:
        project = load_document(os.path.join(build_dir, SNAPSHOT))["current"]
    except DocumentError as exc:
        return seal, None, [diagnostic("snapshot_invalid", f"build {build_id}: {exc}")]
    digest = canonical_digest(project)
    if (project["id"], project["revision"], digest) != identity:
        return seal, None, [diagnostic("seal_mismatch", f"build {build_id}: the snapshot does not match {SEAL}")]
    if require_complete:
        results = verify_outputs(build_dir, project, digest)
        for artifact, result in results.items():
            problems += [d for d in result["diagnostics"] if d["severity"] == "error"]
        derived = overall_status(project, results)
        if derived != status:
            problems.append(diagnostic("seal_mismatch", f"build {build_id}: sealed status {status!r} does not "
                                       f"follow from its outputs ({derived!r})"))
    return seal, project, problems


def read_pointer(root):
    """``(pointer or None, problem or None)`` for ``<root>/current.json``."""
    path = os.path.join(root, POINTER)
    try:
        pointer = read_json(path)
        if not isinstance(pointer, dict) or not BUILD_ID_PATTERN.match(str(pointer.get("build_id"))):
            raise ValueError("no valid build_id")
        return pointer, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        return None, diagnostic("pointer_unreadable", f"{path} is unreadable ({exc})")


def verify_pointer(root, pointer):
    """Verify the build ``pointer`` names, including the pointer's copy of its identity; return problems."""
    builds_root = os.path.join(root, "builds")
    seal, _, problems = verify_sealed(builds_root, pointer.get("build_id"))
    if problems:
        return problems
    seal_path = os.path.join(builds_root, pointer["build_id"], SEAL)
    if sha256_file(seal_path) != pointer.get("build_sha256") or pointer.get("format") != POINTER_FORMAT or any(
            pointer.get(key) != seal.get(key) for key in ("project_id", "source_revision", "input_digest", "status")):
        return [diagnostic("pointer_mismatch", f"{POINTER} does not match build {pointer['build_id']}")]
    return []


def stat_signature(paths):
    """Cheap change detector: ``(inode, size, mtime_ns)`` of each path (``None`` when absent)."""
    signature = []
    for path in paths:
        try:
            st = os.stat(path)
            signature.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            signature.append(None)
    return tuple(signature)
