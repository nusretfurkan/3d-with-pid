"""Offline build orchestration: one frozen snapshot, both views, one published bundle.

``build_project`` loads the authoritative document and freezes its current
project into ``snapshot.json`` in a private staging directory. Everything is
derived from that one file: the P&ID SVG (``render_pid`` in this process), the
Blender model (an isolated Blender subprocess started with an argument array and
the static bootstrap ``skid/adapters/blender_runner.py``), and the entity and
line lists. Each adapter manifest is checked against the snapshot: adapter name
and supported version, project ID, source revision, canonical digest, an explicit
``complete`` or ``pending`` status, a mapping for every entity, port, and
connection, and a nonempty artifact. A zero exit status alone is never success.

Publishing happens only after both adapters pass. While holding the document's
storage lock (``<project>.lock``, the lock ``save_document`` takes) the source is
reread; only if it still has the snapshot's digest does the staging directory
become ``bundles/<name>`` and the single pointer ``current.json`` get replaced
atomically. Otherwise the build is ``superseded`` and nothing is published.

Output directory (default ``<project dir>/<project stem>.skid``)::

    current.json       publish pointer: bundle name, identity, status, digest of bundle.json
    last_build.json    the latest attempt: published, unchanged, failed, superseded, or interrupted
    bundles/<name>/    immutable bundles: snapshot.json, pid.svg, pid.manifest.json, model.blend,
                       blender.manifest.json, lists.json, optional preview.png, and bundle.json
    staging/<random>/  one build's work directory, removed by that build
    .build.lock        serializes builds into this directory

``<name>`` is ``r<revision>-<key>``; the key digests the snapshot, the selected
IDs, the preview option, and the adapter versions. An unchanged rebuild reuses
the verified bundle instead of adding a duplicate, and a selection, which
changes presentation bytes but not the revision, gets its own bundle. Failures,
interruptions, and superseded sources leave ``current.json`` and every published
bundle untouched. ``project_status`` compares the current source with the
pointer, re-verifies the published manifests, and reports the latest attempt.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import json
import os
import reprlib
import shutil
import subprocess
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from .adapters import blender as blender_adapter
from .adapters import pid as pid_adapter
from .core import DocumentError, canonical_digest, load_document, validate_project
from .core.storage import LOCK_SUFFIX
from .core.validation import SCHEMA_VERSION, normalize_tag

__all__ = [
    "BOOTSTRAP",
    "blender_command",
    "build_lists",
    "build_project",
    "check_manifest",
    "default_out_dir",
    "find_blender",
    "parse_json",
    "project_status",
    "run_subprocess",
    "verify_bundle",
]

BUNDLE_FORMAT = 1
POINTER, LAST_BUILD, BUNDLES, STAGING, BUILD_LOCK = "current.json", "last_build.json", "bundles", "staging", ".build.lock"
SNAPSHOT, LISTS, BUNDLE, PREVIEW = "snapshot.json", "lists.json", "bundle.json", "preview.png"
ARTIFACTS = {"pid": ("pid.svg", "pid.manifest.json"), "blender": ("model.blend", "blender.manifest.json")}
SUPPORTED_ADAPTERS = {
    pid_adapter.ADAPTER: (pid_adapter.ADAPTER_VERSION,),
    blender_adapter.ADAPTER: (blender_adapter.ADAPTER_VERSION,),
}
MANIFEST_FIELDS = ("adapter", "adapter_version", "project_id", "source_revision", "input_digest", "status",
                   "diagnostics", "representations")
DIAGNOSTIC_FIELDS = ("code", "severity", "entity_ids", "message")
BOOTSTRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters", "blender_runner.py")
DEFAULT_TIMEOUT_S = 900.0
LOCK_TIMEOUT_S = 10.0
_LOG_TAIL_LINES = 25


class _Failure(Exception):
    def __init__(self, diagnostics, outcome="failed"):
        self.diagnostics = list(diagnostics)
        self.outcome = outcome
        super().__init__(self.diagnostics)


class _Busy(Exception):
    pass


def _diag(code, message, ids=(), severity="error"):
    return {"code": code, "severity": severity, "entity_ids": list(ids), "message": message}


def _unique(items):
    """``items`` without exact repeats, keeping the first occurrence."""
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


# --- Locations and Blender invocation -----------------------------------------


def default_out_dir(project_path) -> str:
    path = os.path.abspath(os.fspath(project_path))
    return os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] + ".skid")


def find_blender(explicit=None):
    """Return an executable from ``explicit``, ``$SKID_BLENDER``, or ``blender`` on ``PATH``; else ``None``."""
    candidate = explicit or os.environ.get("SKID_BLENDER") or shutil.which("blender")
    if not candidate:
        return None
    candidate = os.path.abspath(os.path.expanduser(candidate))
    if candidate.endswith(".app") and os.path.isdir(candidate):  # a macOS application bundle
        candidate = os.path.join(candidate, "Contents", "MacOS", "Blender")
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


def blender_command(executable, document, blend, manifest, selected_ids=(), preview=None) -> list[str]:
    """Argument array for one isolated Blender build; no shell and no user-supplied code."""
    argv = [executable, "--background", "--factory-startup", "--python-exit-code", "1", "--python", BOOTSTRAP,
            "--", "build", "--document", document, "--blend", blend, "--manifest", manifest]
    if preview:
        argv += ["--preview", preview]
    if selected_ids:
        argv += ["--select", *selected_ids]
    return argv


def run_subprocess(argv, timeout, cwd) -> tuple[int, str]:
    """Default runner: execute ``argv`` without a shell; return ``(exit status, combined output)``."""
    completed = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return completed.returncode, completed.stdout.decode("utf-8", "replace")


# --- Files ---------------------------------------------------------------------


def _json_bytes(data) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _write_atomic(path, data: bytes) -> None:
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


def parse_json(text: str):
    """Strict JSON: duplicate keys and nonfinite numbers raise ``ValueError``."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise ValueError(f"nonfinite number {name}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _read_json(path):
    with open(path, "rb") as handle:
        return parse_json(handle.read().decode("utf-8"))


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty(path) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


@contextlib.contextmanager
def _flock(path, timeout):
    """Hold an exclusive advisory lock on ``path``; raise ``_Busy`` after ``timeout`` seconds."""
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
                    raise _Busy(path) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- Lists ---------------------------------------------------------------------


def build_lists(project: dict) -> dict:
    """Entity and line lists keyed by permanent IDs, sorted by tag; quantities keep their provenance."""
    model, pid, physical = project["model"], project["pid"], project["physical"]
    attached = {}
    for connection_id, connection in model["connections"].items():
        attached[connection["from_port_id"]] = connection_id
        attached[connection["to_port_id"]] = connection_id

    def end(port_id):
        port = model["ports"].get(port_id, {})
        owner = model["entities"].get(port.get("entity_id"), {})
        return {"port_id": port_id, "port_name": port.get("name"), "entity_id": port.get("entity_id"),
                "entity_tag": owner.get("tag")}

    entities = []
    for entity_id, entity in model["entities"].items():
        ports = sorted((p for p in model["ports"].values() if p["entity_id"] == entity_id),
                       key=lambda p: (p["name"], p["id"]))
        placement = physical["placements"].get(entity_id)
        entities.append({
            "id": entity_id,
            "tag": entity["tag"],
            "kind": entity["kind"],
            "template_version": entity["template_version"],
            "parameters": copy.deepcopy(entity["parameters"]),
            "observes": copy.deepcopy(entity.get("observes")),
            "ports": [{"id": p["id"], "name": p["name"], "direction": p["direction"],
                       "nominal_diameter_mm": p["nominal_diameter_mm"], "assumption_id": p["assumption_id"],
                       "connection_id": attached.get(p["id"])} for p in ports],
            "placement": copy.deepcopy(placement),
            "pid_symbol_ids": sorted(s["id"] for s in pid["symbols"].values() if s["entity_id"] == entity_id),
        })
    lines = []
    for connection_id, connection in model["connections"].items():
        route = physical["routes"].get(connection_id)
        lines.append({
            "id": connection_id,
            "tag": connection["tag"],
            "from": end(connection["from_port_id"]),
            "to": end(connection["to_port_id"]),
            "nominal_diameter_mm": connection["nominal_diameter_mm"],
            "outside_diameter_m": connection["outside_diameter_m"],
            "assumption_id": connection["assumption_id"],
            "route": None if route is None else {key: copy.deepcopy(route[key]) for key in
                                                 ("status", "mode", "assumption_id", "reason", "points_m")
                                                 if key in route},
            "pid_edges": sorted(({"id": e["id"], "status": e["status"]} for e in pid["edges"].values()
                                 if e["connection_id"] == connection_id), key=lambda e: e["id"]),
        })
    by_tag = lambda row: (normalize_tag(row["tag"]), row["id"])  # noqa: E731
    return {
        "project_id": project["id"],
        "source_revision": project["revision"],
        "input_digest": canonical_digest(project),
        "entities": sorted(entities, key=by_tag),
        "lines": sorted(lines, key=by_tag),
        "assumptions": copy.deepcopy(model["assumptions"]),
    }


# --- Manifest and bundle checks ------------------------------------------------


def _diagnostic_problem(item):
    """Why ``item`` is not a contracted ``{code, severity, entity_ids, message}`` record; ``None`` if it is."""
    if not isinstance(item, dict):
        return f"is {reprlib.repr(item)}, not an object"
    if set(item) != set(DIAGNOSTIC_FIELDS):
        return f"has fields {sorted(map(str, item))}, not {sorted(DIAGNOSTIC_FIELDS)}"
    if not isinstance(item["code"], str) or not item["code"]:
        return f"code {reprlib.repr(item['code'])} is not a nonempty string"
    if item["severity"] not in ("error", "warning"):
        return f"severity {reprlib.repr(item['severity'])} is not error or warning"
    if not isinstance(item["entity_ids"], list) or not all(isinstance(i, str) and i for i in item["entity_ids"]):
        return f"entity_ids {reprlib.repr(item['entity_ids'])} is not a list of ID strings"
    if not isinstance(item["message"], str):
        return f"message {reprlib.repr(item['message'])} is not a string"
    return None


def check_manifest(manifest, adapter, project, digest, artifact) -> list[dict]:
    """Return error diagnostics unless ``manifest`` is a successful manifest of ``project`` by ``adapter``.

    Every diagnostic must be a contracted record. An error diagnostic fails the
    manifest whatever its status, and a ``complete`` manifest lists no warnings.
    """
    ids = [project["id"]]
    label = "P&ID" if adapter == "pid" else adapter.capitalize()
    if not isinstance(manifest, dict):
        return [_diag("manifest_missing", f"the {label} adapter produced no readable manifest", ids)]
    problems = []

    def bad(code, message):
        problems.append(_diag(code, f"{label} manifest: {message}", ids))

    if set(manifest) != set(MANIFEST_FIELDS):
        bad("manifest_fields", f"fields {sorted(manifest)} differ from the contract {sorted(MANIFEST_FIELDS)}")
    if manifest.get("adapter") != adapter:
        bad("manifest_mismatch", f"adapter is {manifest.get('adapter')!r}, expected {adapter!r}")
    if manifest.get("adapter_version") not in SUPPORTED_ADAPTERS[adapter]:
        bad("unsupported_adapter_version", f"adapter_version {manifest.get('adapter_version')!r} is not one of "
            f"{list(SUPPORTED_ADAPTERS[adapter])}")
    if manifest.get("project_id") != project["id"]:
        bad("manifest_mismatch", f"project_id {manifest.get('project_id')!r} is not {project['id']!r}")
    revision = manifest.get("source_revision")
    if type(revision) is not int or revision != project["revision"]:
        bad("manifest_mismatch", f"source_revision {revision!r} is not {project['revision']}")
    if manifest.get("input_digest") != digest:
        bad("manifest_mismatch", f"input_digest {manifest.get('input_digest')!r} is not the snapshot digest {digest}")
    records = []
    if not isinstance(manifest.get("diagnostics"), list):
        bad("manifest_fields", "diagnostics is not a list")
    else:
        malformed = []
        for index, item in enumerate(manifest["diagnostics"]):
            reason = _diagnostic_problem(item)
            if reason is None:
                records.append(item)
            else:
                malformed.append(f"diagnostics[{index}] {reason}")
        if malformed:
            bad("diagnostics_invalid", f"{len(malformed)} diagnostic(s) are not {{code, severity: error|warning, "
                f"entity_ids: [ID, ...], message}} records: {'; '.join(malformed[:3])}")
    errors = [d for d in records if d["severity"] == "error"]
    warnings = [d for d in records if d["severity"] == "warning"]
    status = manifest.get("status")
    if status == "failed":
        bad("adapter_failed", "the adapter reported a failed build")
    elif status not in ("complete", "pending"):
        bad("manifest_status", f"status {status!r} is not complete, pending, or failed")
    else:
        if errors:
            bad("adapter_reported_error", f"status is {status!r} but the adapter reported {len(errors)} error "
                "diagnostic(s); an error prevents publication")
        if status == "complete" and warnings:
            listed = "; ".join(f"[{d['code']}] {d['message']}" for d in warnings[:3])
            bad("manifest_contradiction", f"status is 'complete' but the adapter reported {len(warnings)} warning "
                f"diagnostic(s), so open items remain: {listed}")
        representations = manifest.get("representations")
        model = project["model"]
        expected = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
        if not isinstance(representations, dict) or not all(isinstance(v, list) for v in representations.values()):
            bad("mapping_invalid", "representations is not a map of ID lists")
        elif expected - set(representations):
            missing = sorted(expected - set(representations))
            bad("mapping_incomplete", f"no representation entry for {len(missing)} semantic ID(s): "
                f"{', '.join(missing[:8])}")
    for item in errors:
        problems.append(_diag(item["code"], f"{label}: {item['message']}", item["entity_ids"] or ids))
    if not _nonempty(artifact):
        bad("artifact_missing", f"{os.path.basename(artifact)} is missing or empty")
    return problems


def _bundle_status(project, manifests):
    """Open items are the model's warnings, then each adapter's, without exact repeats; any one makes it pending."""
    warnings = [d for d in validate_project(project) if d["severity"] == "warning"]
    for adapter in ARTIFACTS:
        warnings += [d for d in manifests[adapter]["diagnostics"] if d["severity"] == "warning"]
    open_items = copy.deepcopy(_unique(warnings))
    complete = not open_items and all(m["status"] == "complete" for m in manifests.values())
    return ("complete" if complete else "pending"), open_items


def verify_bundle(bundle_dir) -> tuple[dict | None, list[dict]]:
    """Recheck a bundle on disk: file digests, the snapshot, both manifests, and the derived status."""
    try:
        record = _read_json(os.path.join(bundle_dir, BUNDLE))
        files = dict(record["files"])
        identity = (record["project_id"], record["source_revision"], record["input_digest"])
        preview = record["preview"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, [_diag("bundle_unreadable", f"{bundle_dir}: {BUNDLE} is unreadable ({exc})")]
    required = {SNAPSHOT, LISTS, *(name for pair in ARTIFACTS.values() for name in pair)} | ({PREVIEW} if preview else set())
    problems = [_diag("bundle_incomplete", f"{bundle_dir}: {name} is not recorded") for name in sorted(required - set(files))]
    for name, expected in sorted(files.items()):
        path = os.path.join(bundle_dir, name)
        if name != os.path.basename(name) or name in (os.curdir, os.pardir):
            problems.append(_diag("bundle_unreadable", f"{bundle_dir}: invalid file name {name!r}"))
        elif not os.path.isfile(path):
            problems.append(_diag("bundle_file_missing", f"{path} is missing"))
        elif _sha256(path) != expected:
            problems.append(_diag("bundle_file_changed", f"{path} differs from the published digest"))
    if problems:
        return record, problems
    try:
        project = load_document(os.path.join(bundle_dir, SNAPSHOT))["current"]
    except DocumentError as exc:
        return record, [_diag("bundle_snapshot_invalid", f"{bundle_dir}: {exc}")]
    digest = canonical_digest(project)
    if (project["id"], project["revision"], digest) != identity:
        return record, [_diag("bundle_mismatch", f"{bundle_dir}: the snapshot does not match {BUNDLE}")]
    manifests = {}
    for adapter, (artifact, manifest_name) in ARTIFACTS.items():
        try:
            manifests[adapter] = _read_json(os.path.join(bundle_dir, manifest_name))
        except (OSError, ValueError):
            manifests[adapter] = None
        problems += check_manifest(manifests[adapter], adapter, project, digest, os.path.join(bundle_dir, artifact))
    if not problems and _bundle_status(project, manifests)[0] != record.get("status"):
        problems.append(_diag("bundle_mismatch", f"{bundle_dir}: recorded status {record.get('status')!r} does not "
                              "follow from the manifests and open items"))
    return record, problems


def _read_pointer(out_dir):
    path = os.path.join(out_dir, POINTER)
    try:
        pointer = _read_json(path)
        name = pointer["bundle"]
        if not isinstance(name, str) or name != os.path.basename(name) or name in ("", os.curdir, os.pardir):
            raise ValueError(f"invalid bundle name {name!r}")
        return pointer, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, _diag("pointer_unreadable", f"{path} is unreadable ({exc})")


def _verify_published(out_dir, pointer):
    """Verify the bundle a pointer names, including the pointer's own copy of its identity."""
    bundle_dir = os.path.join(out_dir, BUNDLES, pointer["bundle"])
    record, problems = verify_bundle(bundle_dir)
    if problems:
        return problems
    if _sha256(os.path.join(bundle_dir, BUNDLE)) != pointer.get("bundle_sha256") or any(
            pointer.get(key) != record[key] for key in ("project_id", "source_revision", "input_digest", "status")):
        return [_diag("pointer_mismatch", f"{POINTER} does not match {bundle_dir}")]
    return []


# --- Build ---------------------------------------------------------------------


def _selection(project, selected_ids):
    model = project["model"]
    known = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
    for item in selected_ids:
        if not isinstance(item, str) or item not in known:
            raise _Failure([_diag("selection_unknown_id", f"selected ID {item!r} is not an entity, port, or "
                                  f"connection ID of project {project['id']!r}", [project["id"]])])
        if item.startswith("-"):
            raise _Failure([_diag("selection_unsupported_id", f"selected ID {item!r} starts with '-' and cannot be "
                                  "passed to the Blender entry script", [item])])
    return tuple(sorted(set(selected_ids)))


def _bundle_name(project, digest, selection, preview):
    options = {"format": BUNDLE_FORMAT, "input_digest": digest, "selected_ids": list(selection), "preview": preview,
               "adapters": {name: list(versions) for name, versions in sorted(SUPPORTED_ADAPTERS.items())}}
    key = hashlib.sha256(_json_bytes(options)).hexdigest()[:16]
    return f"r{project['revision']}-{key}"


def _generate(staging, project, digest, selection, preview, blender, runner, timeout):
    """Produce every bundle file in ``staging`` from one frozen snapshot; raise ``_Failure`` on any problem."""
    def path(name):
        return os.path.join(staging, name)

    _write_atomic(path(SNAPSHOT), _json_bytes({"schema_version": SCHEMA_VERSION, "current": project,
                                               "undo": [], "redo": []}))
    try:
        frozen = load_document(path(SNAPSHOT))["current"]
    except DocumentError as exc:
        raise _Failure([_diag("snapshot_invalid", f"the staged snapshot does not reload ({exc})")]) from None
    if canonical_digest(frozen) != digest:
        raise _Failure([_diag("snapshot_invalid", "the staged snapshot does not reproduce the source digest")])
    ids = [frozen["id"]]

    svg_name, pid_manifest_name = ARTIFACTS["pid"]
    try:
        svg, pid_manifest = pid_adapter.render_pid(frozen, selection)
        _write_atomic(path(svg_name), svg.encode("utf-8"))
        _write_atomic(path(pid_manifest_name), _json_bytes(pid_manifest))
    except Exception as exc:  # any adapter fault fails this build; nothing is published
        raise _Failure([_diag("adapter_fault", f"P&ID adapter fault ({type(exc).__name__}: {exc})", ids)]) from None
    problems = check_manifest(pid_manifest, "pid", frozen, digest, path(svg_name))
    if problems:
        raise _Failure(problems)

    blend_name, blender_manifest_name = ARTIFACTS["blender"]
    if runner is None and blender is None:
        raise _Failure([_diag("blender_not_found", "no Blender executable: pass --blender or set SKID_BLENDER", ids)])
    argv = blender_command(blender or "blender", path(SNAPSHOT), path(blend_name), path(blender_manifest_name),
                           selection, path(PREVIEW) if preview else None)
    try:
        code, log = (runner or run_subprocess)(argv, timeout, staging)
    except subprocess.TimeoutExpired:
        raise _Failure([_diag("blender_timeout", f"Blender did not finish within {timeout:g} s", ids)]) from None
    except OSError as exc:
        raise _Failure([_diag("blender_unavailable", f"cannot run {argv[0]} ({exc})", ids)]) from None
    try:
        blender_manifest = _read_json(path(blender_manifest_name))
    except (OSError, ValueError):
        blender_manifest = None
    problems = check_manifest(blender_manifest, "blender", frozen, digest, path(blend_name))
    if code != 0:
        tail = "\n".join((log or "").splitlines()[-_LOG_TAIL_LINES:])
        problems.insert(0, _diag("blender_exit_status", f"Blender exited with status {code}; output tail:\n{tail}", ids))
    if preview and not _nonempty(path(PREVIEW)):
        problems.append(_diag("artifact_missing", f"{PREVIEW} was requested but not rendered", ids))
    if problems:
        raise _Failure(problems)

    _write_atomic(path(LISTS), _json_bytes(build_lists(frozen)))
    manifests = {"pid": pid_manifest, "blender": blender_manifest}
    status, open_items = _bundle_status(frozen, manifests)
    names = [SNAPSHOT, LISTS, *(name for pair in ARTIFACTS.values() for name in pair)] + ([PREVIEW] if preview else [])
    record = {
        "format": BUNDLE_FORMAT,
        "project_id": frozen["id"],
        "source_revision": frozen["revision"],
        "input_digest": digest,
        "status": status,
        "selected_ids": list(selection),
        "preview": preview,
        "adapters": {adapter: {"adapter_version": manifests[adapter]["adapter_version"],
                               "status": manifests[adapter]["status"], "artifact": artifact, "manifest": manifest_name}
                     for adapter, (artifact, manifest_name) in ARTIFACTS.items()},
        "open_items": open_items,
        "files": {name: _sha256(path(name)) for name in sorted(names)},
    }
    _write_atomic(path(BUNDLE), _json_bytes(record))
    return record


def _publish(project_path, out_dir, name, record, staging):
    """Under the document's storage lock, recheck the source, then move the bundle in and swing the pointer."""
    try:
        with _flock(project_path + LOCK_SUFFIX, LOCK_TIMEOUT_S):
            try:
                latest = load_document(project_path)["current"]
            except DocumentError as exc:
                raise _Failure([_diag("source_unreadable", f"the source became unreadable during the build ({exc})")]
                               + exc.diagnostics, "superseded") from None
            if latest["id"] != record["project_id"] or canonical_digest(latest) != record["input_digest"]:
                raise _Failure([_diag("source_changed", f"the source changed during the build (now project "
                                      f"{latest['id']!r} revision {latest['revision']}); revision "
                                      f"{record['source_revision']} was not published", [latest["id"]])],
                               "superseded")
            bundle_dir = os.path.join(out_dir, BUNDLES, name)
            if staging is not None:
                os.rename(staging, bundle_dir)
            current, _ = _read_pointer(out_dir)
            if current is not None and current.get("bundle") == name and not _verify_published(out_dir, current):
                return "unchanged"
            pointer = {key: record[key] for key in ("project_id", "source_revision", "input_digest", "status",
                                                    "selected_ids")}
            pointer.update(format=BUNDLE_FORMAT, bundle=name, bundle_sha256=_sha256(os.path.join(bundle_dir, BUNDLE)),
                           published_at=_now())
            _write_atomic(os.path.join(out_dir, POINTER), _json_bytes(pointer))
            return "published"
    except _Busy:
        raise _Failure([_diag("source_locked", f"timed out waiting for {project_path}{LOCK_SUFFIX}")]) from None


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def build_project(project_path, out_dir=None, *, blender=None, selected_ids=(), preview=False,
                  timeout=DEFAULT_TIMEOUT_S, runner=None) -> dict:
    """Build both views of the document's current project and publish one bundle; return a report.

    ``outcome`` is ``published`` or ``unchanged`` (the pointer names a bundle of
    this exact snapshot and options; ``bundle_status`` is ``complete`` or
    ``pending``), ``failed`` (nothing published), or ``superseded`` (the source
    changed during the build; nothing published). ``runner(argv, timeout, cwd)``
    replaces the Blender subprocess, for tests only.
    """
    project_path = os.path.abspath(os.fspath(project_path))
    out_dir = os.path.abspath(os.fspath(out_dir or default_out_dir(project_path)))
    report = {"outcome": "failed", "project_path": project_path, "out_dir": out_dir, "project_id": None,
              "requested_revision": None, "input_digest": None, "selected_ids": list(selected_ids),
              "bundle": None, "bundle_status": None, "reused": False, "open_items": [], "diagnostics": [],
              "published": None}
    try:
        if os.path.isdir(out_dir) and os.listdir(out_dir) and not os.path.exists(os.path.join(out_dir, BUILD_LOCK)):
            report["diagnostics"] = [_diag("output_not_owned", f"{out_dir} is not empty and is not a skid build "
                                           "directory; nothing was written there")]
            return report
        os.makedirs(os.path.join(out_dir, BUNDLES), exist_ok=True)
        os.makedirs(os.path.join(out_dir, STAGING), exist_ok=True)
        with _flock(os.path.join(out_dir, BUILD_LOCK), 0):
            _build_locked(report, project_path, out_dir, blender, selected_ids, bool(preview), timeout, runner)
    except _Busy:
        report["diagnostics"] = [_diag("build_busy", f"another build is running in {out_dir}")]
    except OSError as exc:
        report["diagnostics"] = [_diag("output_unavailable", f"cannot prepare {out_dir} ({exc})")]
    return report


def _build_locked(report, project_path, out_dir, blender, selected_ids, preview, timeout, runner):
    staging = None
    try:
        try:
            document = load_document(project_path)
        except DocumentError as exc:
            raise _Failure([_diag("source_invalid", str(exc))] + exc.diagnostics) from None
        project = copy.deepcopy(document["current"])
        digest = canonical_digest(project)
        report.update(project_id=project["id"], requested_revision=project["revision"], input_digest=digest)
        selection = _selection(project, selected_ids)
        report["selected_ids"] = list(selection)
        name = _bundle_name(project, digest, selection, preview)
        bundle_dir = os.path.join(out_dir, BUNDLES, name)
        if os.path.lexists(bundle_dir):
            record, problems = verify_bundle(bundle_dir)
            if problems:
                raise _Failure(problems + [_diag("bundle_unverifiable", f"{bundle_dir} already exists but does not "
                                                 "verify; it was left in place for inspection")])
            report["reused"] = True
        else:
            staging = os.path.join(out_dir, STAGING, uuid.uuid4().hex)
            os.mkdir(staging)
            record = _generate(staging, project, digest, selection, preview, blender, runner, timeout)
        report.update(bundle=name, bundle_status=record["status"], open_items=record["open_items"])
        report["outcome"] = _publish(project_path, out_dir, name, record, staging)
        staging = None  # now the published bundle
    except _Failure as failure:
        report.update(outcome=failure.outcome, diagnostics=failure.diagnostics, bundle=None, bundle_status=None)
    except OSError as exc:
        report.update(outcome="failed", bundle=None, bundle_status=None,
                      diagnostics=[_diag("io_error", f"{type(exc).__name__}: {exc}")])
    except BaseException as exc:  # record an interruption, then let it propagate
        report.update(outcome="interrupted", bundle=None, bundle_status=None,
                      diagnostics=[_diag("build_interrupted", f"build interrupted ({type(exc).__name__})")])
        raise
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        report["published"], _ = _read_pointer(out_dir)
        with contextlib.suppress(OSError, TypeError, ValueError):
            _write_atomic(os.path.join(out_dir, LAST_BUILD), _json_bytes(dict(report, finished_at=_now())))


# --- Status --------------------------------------------------------------------


def project_status(project_path, out_dir=None) -> dict:
    """Compare the current source with the published bundle; never trusts a stored success flag alone.

    ``state`` is ``synchronized`` (the published bundle verifies and matches the
    source, status complete), ``pending`` (same, but presentation work is open),
    ``stale`` (the published bundle is of another revision or project),
    ``unpublished``, ``bundle_invalid``, or ``invalid_source``. ``open_items``
    lists the source's warnings and, when ``pending``, the bundle's adapter warnings.
    """
    project_path = os.path.abspath(os.fspath(project_path))
    out_dir = os.path.abspath(os.fspath(out_dir or default_out_dir(project_path)))
    report = {"project_path": project_path, "out_dir": out_dir, "state": None, "source": None, "published": None,
              "open_items": [], "problems": [], "last_build": None, "staging": []}
    source = None
    try:
        source = load_document(project_path)["current"]
        report["source"] = {"project_id": source["id"], "revision": source["revision"],
                            "input_digest": canonical_digest(source)}
        report["open_items"] = [d for d in validate_project(source) if d["severity"] == "warning"]
    except DocumentError as exc:
        report["problems"] = [_diag("source_invalid", str(exc))] + exc.diagnostics
    pointer, problem = _read_pointer(out_dir)
    report["published"] = pointer
    if problem:
        report["problems"].append(problem)
    elif pointer is not None:
        report["problems"] += _verify_published(out_dir, pointer)
    with contextlib.suppress(OSError, ValueError):
        report["last_build"] = _read_json(os.path.join(out_dir, LAST_BUILD))
    with contextlib.suppress(OSError):
        report["staging"] = sorted(os.listdir(os.path.join(out_dir, STAGING)))
    if source is None:
        report["state"] = "invalid_source"
    elif problem or (pointer is not None and report["problems"]):
        report["state"] = "bundle_invalid"
    elif pointer is None:
        report["state"] = "unpublished"
    elif (pointer["project_id"], pointer["input_digest"]) != (source["id"], report["source"]["input_digest"]):
        report["state"] = "stale"
    else:
        report["state"] = "synchronized" if pointer["status"] == "complete" else "pending"
        if report["state"] == "pending":  # the verified bundle records its adapters' open items too
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                recorded = _read_json(os.path.join(out_dir, BUNDLES, pointer["bundle"], BUNDLE))["open_items"]
                if isinstance(recorded, list):
                    report["open_items"] = _unique(report["open_items"] + recorded)
    return report
