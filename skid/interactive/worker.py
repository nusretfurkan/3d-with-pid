"""Generation worker: one separate process per build, started only by the service.

``python -I skid/interactive/_worker_entry.py --build-dir DIR --suite MODULE:FACTORY``

The service has already written the frozen ``snapshot.json`` and
``request.json`` (build ID, source identity, suite options, whether a manual
notes copy exists) into ``DIR``. The worker reloads the snapshot strictly,
checks its digest, and runs the geometry generator (display, CAD) and the
drafting generator (P&ID, legend, description) concurrently in two threads,
each on its own deep copy of the project, writing into ``DIR``.

Events travel to the service as one JSON object per line on the original
standard output; anything a library prints goes to standard error
(``worker.log``). Each generator event is validated (kind, artifact owned by
that generator, existing safe path, preview content and immutability) and then
wrapped with the build ID, source revision, input digest, a monotonically
increasing ``sequence`` and ``elapsed_s``. An invalid event fails the emitting
artifact.

SIGTERM sets the ``cancelled()`` flag the generators poll; they raise
``InterruptedError`` and the build ends ``cancelled``. When both generators
have returned, every manifest, listed file, preview and the snapshot are
rechecked (``integrity.verify_outputs``), and ``result.json`` records the
per-artifact and overall status. Exit status 0 means a result was written, 3
that it records a cancellation, 1 an internal fault, 2 a usage error.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import signal
import sys
import threading
import time
import traceback

from ..core import DocumentError, canonical_digest, load_document
from . import integrity
from .generators import REAL_SUITE, GeneratorUnavailable
from .integrity import GENERATORS, LABELS, REQUIRED_ARTIFACTS, diagnostic

EXIT_DONE, EXIT_FAULT, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 3
EVENT_KINDS = ("activity", "preview", "artifact")
EVENT_FIELDS = ("kind", "artifact", "message", "path", "completed", "total")
MAX_MESSAGE = 2000
RESULT_FORMAT = 2


class ProtocolViolation(ValueError):
    """A generator emitted an event that breaks the event contract."""


def load_suite(spec, options):
    """Instantiate ``MODULE:FACTORY``; only the real suite or test modules (``tests.*``) may be named."""
    module_name, _, attribute = str(spec).partition(":")
    if spec != REAL_SUITE and not (module_name.startswith("tests.") and attribute.isidentifier()):
        raise ValueError(f"generator suite {spec!r} is not allowed")
    factory = getattr(importlib.import_module(module_name), attribute)
    return factory(options)


class Emitter:
    """Validates generator events, wraps them with the build identity, and writes them in sequence order."""

    def __init__(self, stream, identity, build_dir, project, digest, cancel):
        self._stream = stream
        self._identity = identity
        self._build_dir = build_dir
        self._project = project
        self._digest = digest
        self._cancel = cancel
        self._lock = threading.Lock()
        self._sequence = 0
        self._start = time.monotonic()
        self.previews = {}  # relative path -> (artifact, sha256) at emission
        self.violations = {}  # artifact -> [diagnostic]

    @property
    def count(self):
        return self._sequence

    def elapsed(self):
        return round(time.monotonic() - self._start, 3)

    def callback(self, generator):
        allowed = GENERATORS[generator]

        def emit(event):
            try:
                record = self._validate(event, allowed)
            except ProtocolViolation as exc:
                artifact = event.get("artifact") if isinstance(event, dict) else None
                self._violation(artifact if artifact in allowed else allowed[0], str(exc))
                raise
            self.send(record)

        return emit

    def send(self, event):
        with self._lock:
            self._sequence += 1
            wrapped = dict(self._identity, sequence=self._sequence, elapsed_s=self.elapsed(), **event)
            line = json.dumps(wrapped, ensure_ascii=False, allow_nan=False, sort_keys=True)
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                self._cancel.set()  # the service is gone; nobody can publish this build

    def _violation(self, artifact, message):
        with self._lock:
            self.violations.setdefault(artifact, []).append(
                diagnostic("event_invalid", f"{LABELS[artifact]} event rejected: {message}", [self._project["id"]]))

    def _validate(self, event, allowed):
        if not isinstance(event, dict):
            raise ProtocolViolation("an event must be a dict")
        extra = sorted(str(k) for k in event if k not in EVENT_FIELDS)
        if extra:
            raise ProtocolViolation(f"unsupported event fields {extra}")
        kind, artifact, message = event.get("kind"), event.get("artifact"), event.get("message")
        if kind not in EVENT_KINDS:
            raise ProtocolViolation(f"kind {str(kind)[:20]!r} is not one of {EVENT_KINDS}")
        if artifact not in allowed:
            raise ProtocolViolation(f"artifact {str(artifact)[:20]!r} is not produced by this generator {allowed}")
        if not isinstance(message, str) or not message.strip():
            raise ProtocolViolation("message must be a nonempty string")
        record = {"kind": kind, "artifact": artifact,
                  "message": message if len(message) <= MAX_MESSAGE else message[:MAX_MESSAGE - 1] + "…"}
        for key in ("completed", "total"):
            if key in event:
                value = event[key]
                if type(value) is not int or value < 0:
                    raise ProtocolViolation(f"{key} must be a nonnegative integer")
                record[key] = value
        if "completed" in record and "total" in record and record["completed"] > record["total"]:
            raise ProtocolViolation("completed exceeds total")
        path = event.get("path")
        if path is None:
            if kind != "activity":
                raise ProtocolViolation(f"a {kind} event needs a path")
            return record
        relpath = integrity.safe_relpath(path)
        if relpath is None:
            raise ProtocolViolation(f"path {str(path)[:80]!r} is not a plain relative path")
        full = integrity.resolve_inside(self._build_dir, relpath)
        if full is None:
            raise ProtocolViolation(f"{relpath} does not exist in the build directory when emitted")
        if kind == "preview" and not relpath.startswith("preview/"):
            raise ProtocolViolation(f"preview {relpath} is not under preview/")
        identity = kind == "preview" and artifact == "display" and relpath.endswith(".json")
        problems = integrity.content_problems(full, relpath, self._project, self._digest, required_identity=identity)
        if problems:
            raise ProtocolViolation("; ".join(problems[:3]))
        if kind == "preview":
            digest = integrity.sha256_file(full)
            with self._lock:
                known = self.previews.get(relpath)
                if known is not None and known[1] != digest:
                    raise ProtocolViolation(f"preview {relpath} was rewritten; previews are immutable")
                self.previews[relpath] = (artifact, digest)
        record["path"] = relpath
        return record


def _call(target, outcome):
    try:
        outcome["value"] = target()
        outcome["state"] = "returned"
    except InterruptedError as exc:
        outcome.update(state="cancelled", error=exc)
    except GeneratorUnavailable as exc:
        outcome.update(state="unavailable", error=exc)
    except BaseException as exc:  # any generator fault, including SystemExit, fails its artifacts
        outcome.update(state="fault", error=exc, trace=traceback.format_exc())


def _describe(exc):
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text[:400]}" if text else type(exc).__name__


def run_build(build_dir, suite_spec, stream):
    """Run one build in ``build_dir``; write ``result.json``; return the overall status."""
    build_dir = os.path.realpath(build_dir)
    request = integrity.read_json(os.path.join(build_dir, integrity.REQUEST))
    identity = {key: request[key] for key in ("build_id", "source_revision", "input_digest")}
    project_id = request["project_id"]
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: cancel.set())
    started = time.monotonic()
    result = {"format": RESULT_FORMAT, "build_id": identity["build_id"], "project_id": project_id,
              "source_revision": identity["source_revision"], "input_digest": identity["input_digest"],
              "suite": suite_spec, "status": "failed", "artifacts": {}, "diagnostics": [], "previews": {}}

    def finish(status):
        result["status"] = status
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        integrity.write_atomic(os.path.join(build_dir, integrity.RESULT), integrity.json_bytes(result))
        return status

    def fail_all(code, message):
        failure = diagnostic(code, message, [project_id])
        result["diagnostics"].append(failure)
        result["artifacts"] = {a: {"status": "failed", "diagnostics": [failure]} for a in REQUIRED_ARTIFACTS}
        return finish("failed")

    snapshot_path = os.path.join(build_dir, integrity.SNAPSHOT)
    snapshot_sha = integrity.sha256_file(snapshot_path)
    try:
        project = load_document(snapshot_path)["current"]
    except DocumentError as exc:
        return fail_all("snapshot_invalid", f"the frozen snapshot does not load ({exc})")
    digest = canonical_digest(project)
    if (project["id"], project["revision"], digest) != (project_id, identity["source_revision"],
                                                       identity["input_digest"]):
        return fail_all("snapshot_mismatch", "the frozen snapshot does not match the requested build identity")
    try:
        suite = load_suite(suite_spec, request.get("suite_options") or {})
    except Exception as exc:  # an unloadable suite fails every artifact
        return fail_all("generator_unavailable", f"generator suite {suite_spec} cannot be loaded ({_describe(exc)})")

    emitter = Emitter(stream, identity, build_dir, project, digest, cancel)
    os.makedirs(os.path.join(build_dir, "preview"), exist_ok=True)
    copies = {generator: copy.deepcopy(project) for generator in GENERATORS}
    outcomes = {generator: {"state": "running"} for generator in GENERATORS}
    manual_notes = None
    if request.get("manual_notes_error"):
        # The sidecar exists but the service could not read it; drafting must not run as if it were absent.
        outcomes["drafting"] = {"state": "notes_unreadable", "error": str(request["manual_notes_error"])}
    elif request.get("manual_notes"):
        try:
            manual_notes = integrity.read_json(os.path.join(build_dir, integrity.NOTES_COPY))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            # Drafting would silently drop notes it cannot read; it does not run instead.
            outcomes["drafting"] = {"state": "notes", "error": exc}
    calls = {
        "geometry": lambda: suite.geometry(copies["geometry"], build_dir, emitter.callback("geometry"), cancel.is_set),
        "drafting": lambda: suite.drafting(copies["drafting"], build_dir, emitter.callback("drafting"), cancel.is_set,
                                           copy.deepcopy(manual_notes)),
    }
    threads = [threading.Thread(target=_call, args=(calls[g], outcomes[g]), name=f"generator-{g}", daemon=True)
               for g in GENERATORS if outcomes[g]["state"] == "running"]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(0.05)  # short joins let the SIGTERM handler run in this thread

    returned, skip, artifacts = {}, [], {}
    for generator, members in GENERATORS.items():
        outcome = outcomes[generator]
        state = outcome["state"]
        failure = None
        if state == "returned":
            value = outcome["value"]
            if not isinstance(value, dict) or set(value) != set(members):
                failure = diagnostic("generator_result_invalid", f"the {generator} generator returned "
                                     f"{sorted(value) if isinstance(value, dict) else type(value).__name__}, "
                                     f"not manifests for {', '.join(members)}", [project_id])
            else:
                returned.update(value)
        elif state == "cancelled" and cancel.is_set():
            for artifact in members:
                artifacts[artifact] = {"status": "cancelled", "diagnostics": [diagnostic(
                    "generation_cancelled", f"{LABELS[artifact]} generation stopped on cancellation", [project_id])]}
            skip += members
            continue
        elif state == "cancelled":
            failure = diagnostic("generator_fault", f"the {generator} generator raised InterruptedError without a "
                                 "cancellation request", [project_id])
        elif state == "notes_unreadable":
            failure = diagnostic("manual_notes_unreadable", f"the manual notes sidecar exists but could not be read "
                                 f"({outcome['error'][:300]}); drafting did not run", [project_id])
        elif state == "notes":
            failure = diagnostic("manual_notes_invalid", f"the manual notes sidecar is not strict JSON "
                                 f"({_describe(outcome['error'])}); drafting did not run", [project_id])
        elif state == "unavailable":
            failure = diagnostic("generator_unavailable", str(outcome["error"]), [project_id])
        else:
            failure = diagnostic("generator_fault", f"the {generator} generator raised {_describe(outcome['error'])}",
                                 [project_id])
            print(outcome.get("trace", ""), file=sys.stderr)
        try:
            mutated = canonical_digest(copies[generator]) != digest
        except (TypeError, ValueError):
            mutated = True
        if mutated:
            failure = failure or diagnostic("input_mutated", f"the {generator} generator modified its read-only "
                                            "project snapshot", [project_id])
        if failure is not None:
            for artifact in members:
                artifacts[artifact] = {"status": "failed", "diagnostics": [failure]}
            skip += members

    for artifact, verified in integrity.verify_outputs(build_dir, project, digest, returned, skip).items():
        artifacts[artifact] = {"status": verified["status"], "diagnostics": verified["diagnostics"]}
    for artifact, problems in emitter.violations.items():
        artifacts[artifact]["status"] = "failed"
        artifacts[artifact]["diagnostics"] = problems + artifacts[artifact]["diagnostics"]
    for relpath, (artifact, sha) in sorted(emitter.previews.items()):
        full = integrity.resolve_inside(build_dir, relpath)
        if full is None or integrity.sha256_file(full) != sha:
            artifacts[artifact]["status"] = "failed"
            artifacts[artifact]["diagnostics"].insert(0, diagnostic(
                "preview_changed", f"{LABELS[artifact]} preview {relpath} changed or vanished after emission",
                [project_id]))
        result["previews"][relpath] = sha
    if integrity.sha256_file(snapshot_path) != snapshot_sha:
        return fail_all("snapshot_changed", "snapshot.json changed during generation")

    result["artifacts"] = artifacts
    status = "cancelled" if cancel.is_set() else integrity.overall_status(project, artifacts)
    for artifact in REQUIRED_ARTIFACTS:
        state = artifacts[artifact]
        errors = [d for d in state["diagnostics"] if d["severity"] == "error"]
        detail = f": [{errors[0]['code']}] {errors[0]['message'][:300]}" if errors else ""
        emitter.send({"kind": "activity", "artifact": artifact,
                      "message": f"{LABELS[artifact]} output verified as {state['status']}{detail}"})
    result["events"] = emitter.count
    return finish(status)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="skid.interactive.worker")
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--suite", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)  # library output goes to the log, never into the event stream
    sys.stdout = sys.stderr
    try:
        status = run_build(args.build_dir, args.suite, protocol)
    except Exception:
        traceback.print_exc()
        return EXIT_FAULT
    finally:
        try:
            protocol.close()
        except OSError:
            pass
    return EXIT_CANCELLED if status == "cancelled" else EXIT_DONE
