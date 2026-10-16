"""Single-project interactive service (workflow contract 0.2).

One ``InteractiveService`` owns one persisted engineering document. It is the
single command writer: every mutation is an existing command envelope applied
by ``apply_command`` and saved by ``save_document`` against the revision it was
applied to, so a competing writer produces a revision conflict instead of a
silent rebase. Undo and redo are ordinary commands. Selection, builds and
proposals never enter the document or its history.

Proposals have two modes. ``quick`` is the deterministic single-command parser
(``proposals.py``). ``engineering`` runs the tool-free exact-model CLI planner
(``skid.planning``) on a frozen document snapshot with no lock held, so state,
selection and commands stay responsive; the result is one checked ``batch``
envelope dry-run against that revision. Apply re-checks the source digest under
the write lock, so a plan made stale by a concurrent change is refused.

An engineering proposal may request equipment research (``research: true``,
strict boolean, engineering mode only). The researcher (``skid.research``, or
an injected object with the same ``run(request, cancel)`` interface) receives
only the cleaned request text; its reply is re-validated
(``skid.planning.evidence``) and the verified brief goes to the tool-free
planner as untrusted context. Both stages run on the same frozen snapshot in
the same plan slot with no lock held, share one cancel event (cancel-proposal
and shutdown stop whichever stage is running) and report their stage
(``researching``/``planning``) in ``planning.active``. A research failure is an
explicit refusal: the planner is not run without the requested sources. A
source change during either stage makes the whole proposal stale.

Ask (``POST /api/ask``, ``skid.assistant``, ``docs/ASK.md``) is read-only: it
reads the project file as atomically persisted now (never the in-memory copy,
and never through the adopting, building disk refresh), checks the request's
revision and digest against it, answers simple identity and directed-
connectivity questions locally from that frozen snapshot and broader ones
through the same tool-free planner bridge with an Ask-specific schema, with no
lock held while the model runs, then reads the file again to label the answer
current or stale. It never adopts, dispatches a command, saves, records
history, applies a proposal or requests a build. It is tracked from
acceptance, before the source read, so cancel-ask or shutdown stops it before
any model starts, and it is tracked and cancelled separately from engineering
plans.

Every accepted change queues generation of that exact revision. Generation runs
in a separate worker process (``worker.py``) on a frozen ``snapshot.json``; its
ordered events, progressive previews and outputs are visible while it runs.
There is at most one running build and one queued build: a newer committed
revision replaces the queued one, and a running build of an older revision is
stopped and marked ``superseded``. Cancellation terminates the owned worker
process group. A stopped, failed, pending or obsolete build is never
published.

Workflow root (default ``<project dir>/<project stem>.workflow``)::

    workflow.json      ownership marker naming the project ID
    .service.lock      held by the one service using this root
    builds/<build_id>/ one directory per started build (see ``integrity``)
    current.json       publish pointer to the last build whose five outputs verified complete

Publication happens only when all five artifacts (display, CAD, P&ID, legend,
description) verified ``complete`` for the same project ID, revision and
digest and the model has no open items. The service rechecks the outputs from
disk, takes the document's storage lock (``<project>.lock``, as
``save_document`` does), rereads the source, and only if its digest still
matches seals the build and atomically replaces ``current.json``. The 0.1
offline ``.skid`` bundles are never read or written.
"""

from __future__ import annotations

import collections
import contextlib
import copy
import datetime
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from .. import assistant
from ..core import (CommandError, DocumentError, RevisionConflict, apply_command, canonical_digest, load_document,
                    save_document, validate_project)
from ..core.storage import LOCK_SUFFIX
from ..planning import ClaudeCliPlanner, evidence
from ..planning.engineering import clean_request, failed_proposal, propose_engineering, research_failed_proposal
from . import integrity, proposals, rerouting
from .generators import REAL_SUITE, availability
from .integrity import BUILD_ID_PATTERN, LABELS, POINTER, PUBLISHED_FILES, REQUIRED_ARTIFACTS, diagnostic

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLES_DIR = os.path.realpath(os.path.join(ROOT, "examples"))
WORKER_ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_worker_entry.py")
SERVICE_VERSION = "0.2.0"
# Additive feature flags in state: a client offers an action only when its running service advertises it.
# ``equipment_research`` is added per service: true only when a researcher is configured.
CAPABILITIES = {"pipe_segment_move": True, "equipment_creation": True}
STAGES = ("researching", "planning")
_UNAVAILABLE_RESEARCH = ("unavailable", "auth", "timeout", "cancel", "shutdown", "exit", "busy", "not_found",
                         "subscription", "process")
MARKER, SERVICE_LOCK, BUILDS = "workflow.json", ".service.lock", "builds"
DEFAULT_TIMEOUT_S = 900.0
TERMINATE_GRACE_S = 2.0
LOCK_TIMEOUT_S = 10.0
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("complete", "pending", "failed", "cancelled", "superseded")
SELECTION_ORIGINS = ("pid", "display", "inspector")
PROPOSAL_MODES = ("quick", "engineering")
DEFAULT_MAX_PLANS = 1
ASK_TIMEOUT_S = 300.0  # one model Ask; the bridge's own (plan) time limit also applies
ASK_SOURCE_LOCK_TIMEOUT_S = LOCK_TIMEOUT_S  # longest wait for a writer's storage lock before an Ask fails closed
ASK_WAIT_SLICE_S = 0.05  # the source wait is taken in slices; cancel-ask or close() stops it within one
MAX_ASKS_IN_PROGRESS = 8  # Asks tracked at once (source check, preparation or model run); the model slot is one
MAX_SELECTION = 256
MAX_EVENTS = 20000
MAX_EVENT_LINE = 1 << 16
KEEP_BUILDS, KEEP_PROPOSALS, KEEP_COMMANDS = 48, 64, 50
ARTIFACT_OF_KEY = {"display": "display", "cad": "cad", "pid": "pid", "legend": "legend", "description": "description",
                   "index": "pid", "inventory": "pid", "recipe": "cad", "verification": "cad"}
_WORKER_EVENT_FIELDS = {"build_id", "source_revision", "input_digest", "sequence", "elapsed_s", "kind", "artifact",
                        "message", "path", "completed", "total"}


class ServiceError(Exception):
    """A rejected request: HTTP ``status``, a short ``error`` and contracted diagnostics."""

    def __init__(self, status, error, diagnostics=()):
        super().__init__(error)
        self.status = status
        self.error = error
        self.diagnostics = list(diagnostics)


class ServiceStartError(Exception):
    def __init__(self, message, diagnostics=()):
        super().__init__(message)
        self.diagnostics = list(diagnostics)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@contextlib.contextmanager
def _shared_lock(path, timeout):
    """Shared ``flock`` on an existing storage lock file for a reader (``save_document`` and publication hold it
    exclusively). Never creates the file: without one there is no locked writer yet, and the writers' atomic
    replacement alone keeps a read consistent. Raises ``TimeoutError`` after ``timeout`` seconds."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        fd = None
    if fd is None:
        yield
        return
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
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


def make_researcher(cli_path=None, timeout_s=None):
    """The default ``skid.research.ClaudeCliResearcher`` (same installed CLI and Max route as the planner).

    Only fields the research package's ``ResearchConfig`` declares are set; raises ``ImportError`` when the
    package is not installed.
    """
    import dataclasses

    from .. import research

    options = {}
    fields = {f.name for f in dataclasses.fields(research.ResearchConfig)} \
        if dataclasses.is_dataclass(research.ResearchConfig) else set()
    if cli_path is not None and "cli_path" in fields:
        options["cli_path"] = cli_path
    if timeout_s is not None and "timeout_s" in fields:
        options["timeout_s"] = float(timeout_s)
    return research.ClaudeCliResearcher(research.ResearchConfig(**options) if options else None)


def _default_researcher(researcher):
    """``(researcher, None)`` or ``(None, reason)``; ``False`` disables research explicitly."""
    if researcher is False:
        return None, "equipment research is disabled for this service"
    if researcher is not None:
        return researcher, None
    try:
        return make_researcher(), None
    except Exception as exc:  # noqa: BLE001 - research is optional; planning and every other function still work
        return None, f"equipment research is unavailable ({type(exc).__name__}: {str(exc)[:160]})"


def default_workflow_dir(project_path) -> str:
    path = os.path.abspath(os.fspath(project_path))
    return os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] + ".workflow")


def default_notes_path(project_path) -> str:
    path = os.path.abspath(os.fspath(project_path))
    return os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] + ".notes.json")


def artifact_url(build_id, relpath):
    return f"/artifacts/{build_id}/{relpath}"


class Notes(collections.namedtuple("Notes", "present data sha error")):
    """The manual-notes sidecar as read now: absent, readable (bytes and SHA-256), or present but unreadable."""

    @property
    def key(self):
        """Build identity of the notes; an unreadable sidecar never equals an absent or readable one."""
        return self.sha if self.error is None else f"unreadable: {self.error}"


class Build:
    """In-memory record of one requested build (queued, running or terminal)."""

    def __init__(self, build_id, project, digest, reason, notes):
        self.id = build_id
        self.project = project
        self.project_id = project["id"]
        self.source_revision = project["revision"]
        self.input_digest = digest
        self.reason = reason
        self.notes_bytes, self.notes_sha, self.notes_error, self.notes_key = notes.data, notes.sha, notes.error, notes.key
        self.status = "queued"
        self.message = f"Queued: {reason}."
        self.events = []
        self.events_dropped = 0
        self.last_sequence = 0
        self.worker_sequence = 0
        self.previews = {}  # artifact -> relative path of the latest emitted preview
        self.files = {}  # published-file key -> relative path of an emitted or verified output
        self.artifact_status = {artifact: "waiting" for artifact in REQUIRED_ARTIFACTS}
        self.diagnostics = []
        self.created_at = _now()
        self.created_monotonic = time.monotonic()
        self.started_at = self.finished_at = None
        self.dir = None
        self.process = None
        self.monitor = None
        self.watchdog = None
        self.event_log = None
        self.exit_status = None
        self.published = False


class InteractiveService:
    """One project document, one command writer, one generation worker at a time."""

    def __init__(self, project_path, workflow_dir=None, *, suite=REAL_SUITE, suite_options=None, notes_path=None,
                 python=None, timeout=DEFAULT_TIMEOUT_S, terminate_grace=TERMINATE_GRACE_S, initial_build=False,
                 keep_builds=0, planner=None, max_plans=DEFAULT_MAX_PLANS, ask_timeout=ASK_TIMEOUT_S,
                 researcher=None):
        path = os.path.abspath(os.fspath(project_path))
        if not os.path.isfile(path):
            raise ServiceStartError(f"{path} does not exist; the service never creates a project. Create one with "
                                    "`python -m skid.cli new PATH` (empty) or `python -m skid.cli sample PATH` and "
                                    "pass it with --project.")
        real = os.path.realpath(path)
        if os.path.commonpath([real, EXAMPLES_DIR]) == EXAMPLES_DIR:
            raise ServiceStartError(f"{path} is a repository example; the service would modify it. Copy it first with "
                                    "`python -m skid.cli sample demo/pump_skid.json`.")
        try:
            document = load_document(path)
        except DocumentError as exc:
            raise ServiceStartError(f"{path} is not a valid project document: {exc}", exc.diagnostics) from None
        self.path = path
        self.root = os.path.abspath(workflow_dir or default_workflow_dir(path))
        self.builds_root = os.path.join(self.root, BUILDS)
        self.notes_path = os.path.abspath(notes_path or default_notes_path(path))
        self.suite = suite
        self.suite_options = dict(suite_options or {})
        self.python = python or sys.executable
        self.timeout = float(timeout)
        self.grace = float(terminate_grace)
        if type(keep_builds) is not int or keep_builds < 0:
            raise ServiceStartError("keep_builds must be a nonnegative integer (0 keeps every build)")
        self.keep_builds = keep_builds
        if type(max_plans) is not int or max_plans < 1:
            raise ServiceStartError("max_plans must be a positive integer")
        # Engineering planner: fixed by the constructor (never selectable over HTTP); see docs/PLANNING.md.
        self.planner = planner if planner is not None else ClaudeCliPlanner()
        # Equipment researcher: fixed by the constructor too. None builds the default skid.research researcher;
        # False disables research (the capability is then not advertised). See docs/CREATION_AND_RESEARCH_UI.md.
        self.researcher, self.research_unavailable = _default_researcher(researcher)
        self.max_plans = max_plans
        self._plan_slots = threading.BoundedSemaphore(max_plans)
        self._plans = {}  # proposal ID -> running plan record (no lock is held while a planner runs)
        self._planning_closed = False
        if isinstance(ask_timeout, bool) or not isinstance(ask_timeout, (int, float)) or not ask_timeout > 0:
            raise ServiceStartError("ask_timeout must be a positive number of seconds")
        self.ask_timeout = float(ask_timeout)
        self._ask_slot = threading.BoundedSemaphore(1)  # one model Ask at a time; local answers need no slot
        # request ID -> Ask record from acceptance (before its source read) until its outcome is settled; tracked
        # apart from plans and the model slot, at most MAX_ASKS_IN_PROGRESS, no lock held while it waits or runs
        self._asks = {}
        self._asks_closed = False
        self.before_publish = None  # test hook: called with the build just before the publication source check
        self.before_reroute_commit = None  # test hook: called with a computed route plan just before its commit
        self._reroute_slot = threading.BoundedSemaphore(1)  # one route computation at a time; others get 409
        self.started_at = _now()

        self._lock = threading.RLock()  # in-memory state; held briefly, never while waiting for _write_lock
        self._write_lock = threading.RLock()  # document mutations, reopen, cancel and publication
        self._pub_lock = threading.Lock()  # published-build verification cache
        self._published_changed = threading.Condition(self._lock)
        self._publishing = None  # build ID while its pointer swap and status update are in progress
        self._pub_generation = 0  # completed publications; a state snapshot never straddles one
        self._closed = False
        self._lock_fd = None
        self._claim_root(document["current"]["id"])
        self.document = document
        self.digest = canonical_digest(document["current"])
        self._disk_sig = integrity.stat_signature([self.path])
        self._disk_problem = None
        self._notices = collections.deque(maxlen=5)
        self._open_items = (None, [])
        self._builds = collections.OrderedDict()
        self.active = self.queued = self.current = None
        self.selection = {"sequence": 0, "semantic_ids": [], "origin": None, "revision": document["current"]["revision"],
                          "updated_at": None}
        self._proposals = collections.OrderedDict()
        self._commands = collections.deque(maxlen=KEEP_COMMANDS)
        self._last_command = None
        self._saved = None
        self._pub_cache = None
        self._manifest_cache = {}
        self.interrupted = sorted(name for name in os.listdir(self.builds_root) if BUILD_ID_PATTERN.match(name)
                                  and not os.path.exists(os.path.join(self.builds_root, name, integrity.SEAL)))
        self._published_info(force=True)
        if initial_build:
            self.request_build("service started")

    # --- Lifecycle -----------------------------------------------------------------

    def _claim_root(self, project_id):
        os.makedirs(self.root, exist_ok=True)
        marker = os.path.join(self.root, MARKER)
        if os.path.exists(marker):
            try:
                owner = integrity.read_json(marker).get("project_id")
            except (OSError, ValueError, AttributeError) as exc:
                raise ServiceStartError(f"{marker} is unreadable ({exc}); refusing to use {self.root}") from None
            if owner != project_id:
                raise ServiceStartError(f"{self.root} belongs to project {owner!r}, not {project_id!r}")
        elif os.listdir(self.root):
            raise ServiceStartError(f"{self.root} is not empty and is not a workflow root; nothing was written there")
        else:
            integrity.write_atomic(marker, integrity.json_bytes({"format": 2, "project_id": project_id,
                                                                 "project_path": self.path, "created_at": _now()}))
        os.makedirs(self.builds_root, exist_ok=True)
        if fcntl is None:
            raise ServiceStartError("file locking (fcntl.flock) is unavailable on this platform")
        fd = os.open(os.path.join(self.root, SERVICE_LOCK), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ServiceStartError(f"another service is already using {self.root}") from None
        self._lock_fd = fd

    def close(self):
        """Stop running plans, every tracked Ask (still checking its source, preparing or running its model) and the
        queued and running builds (never published); release the root."""
        with self._lock:
            self._planning_closed = True
            self._asks_closed = True
            plans = list(self._plans.values())
            asks = list(self._asks.values())
            for record in asks:
                record["reason"] = record["reason"] or "shutdown"
        for plan in plans:
            plan["cancel"].set()
        for record in asks:
            record["cancel"].set()
        with contextlib.suppress(Exception):  # a planner fault must not keep builds or the lock alive
            if hasattr(self.planner, "shutdown"):
                self.planner.shutdown()
        with contextlib.suppress(Exception):  # nor may a researcher fault
            if self.researcher is not None and hasattr(self.researcher, "shutdown"):
                self.researcher.shutdown()
        with self._write_lock, self._lock:
            if self._closed:
                return
            self._closed = True
            if self.queued is not None:
                self._finish_unstarted_locked(self.queued, "cancelled", "Cancelled: the service is shutting down.")
                self.queued = None
            active = self.active
            if active is not None:
                self._stop_locked(active, "cancelled", "Cancelled: the service is shutting down.")
        if active is not None and active.monitor is not None:
            active.monitor.join(self.grace + 10)
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            self._lock_fd = None

    # --- Document ------------------------------------------------------------------

    def _adopt_locked(self, document):
        self.document = document
        self.digest = canonical_digest(document["current"])
        self._prune_selection_locked()

    def _read_back_signature(self, document):
        """Stat signature of the file if it holds exactly ``document``; ``None`` forces a later disk check."""
        signature = integrity.stat_signature([self.path])
        try:
            with open(self.path, "rb") as handle:
                on_disk = integrity.parse_json(handle.read())
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return signature if _canonical(on_disk) == _canonical(document) else None

    def _refresh_from_disk(self, blocking):
        """Adopt a valid external change to the project file; record an invalid one without adopting it."""
        if not self._write_lock.acquire(blocking=blocking):
            return
        try:
            signature = integrity.stat_signature([self.path])
            with self._lock:
                if signature == self._disk_sig:
                    return
            try:
                document = load_document(self.path)
            except DocumentError as exc:
                with self._lock:
                    self._disk_sig = signature
                    self._disk_problem = diagnostic(
                        "disk_invalid", f"the project file on disk no longer loads ({exc}); the service keeps "
                        f"revision {self.document['current']['revision']} in memory and rejects writes until it is "
                        "repaired and reopened", [self.document["current"]["id"]])
                return
            with self._lock:
                self._disk_sig = signature
                self._disk_problem = None
                if _canonical(document) == _canonical(self.document):
                    return
                before = self.document["current"]["revision"]
                self._adopt_locked(document)
                after = document["current"]["revision"]
                self._notices.append(diagnostic("external_change", f"the project file changed outside this service "
                                                f"(revision {before} -> {after}); the service adopted it",
                                                [document["current"]["id"]], "warning"))
                self._request_build_locked(f"external change to revision {after}")
        finally:
            self._write_lock.release()

    def _require_fields(self, body, required, optional=()):
        if not isinstance(body, dict):
            raise ServiceError(400, "the request body must be a JSON object")
        missing = [name for name in required if name not in body]
        extra = sorted(str(name) for name in body if name not in required and name not in optional)
        if missing or extra:
            raise ServiceError(400, f"request fields: missing {missing}, unsupported {extra}")
        return body

    @staticmethod
    def _revision_field(value, name="expected_revision"):
        if type(value) is not int or value < 0:
            raise ServiceError(400, f"{name} must be a nonnegative integer")
        return value

    def _commit(self, command, source, expected_digest=None, stale_what="proposal", details=None):
        """Apply and save one envelope; ``expected_digest`` (a checked proposal's or computed route's source) must
        still be current. ``details`` are added to the recorded command entry."""
        if not isinstance(command, dict):
            raise ServiceError(400, "the command must be a JSON object envelope {id, expected_revision, type, payload}")
        with self._write_lock:
            self._refresh_from_disk(blocking=True)
            with self._lock:
                if self._disk_problem is not None:
                    raise ServiceError(409, "the project file on disk is invalid; repair it and reopen",
                                       [self._disk_problem])
                document = self.document
                if expected_digest is not None and self.digest != expected_digest:
                    raise ServiceError(409, f"the project changed since this {stale_what} was checked (now revision "
                                       f"{document['current']['revision']}); nothing was applied, request a new "
                                       f"{stale_what}", [diagnostic(f"stale_{stale_what}", "the source digest "
                                                                    f"differs from the one the {stale_what} was "
                                                                    "checked against", [document["current"]["id"]])])
            revision = document["current"]["revision"]
            kind = command.get("type")
            try:
                new, warnings = apply_command(document, command)
            except CommandError as exc:
                stale = any(d.get("code") == "stale_revision" for d in exc.diagnostics)
                raise ServiceError(409 if stale else 400, f"{kind} rejected; revision {revision} is unchanged",
                                   exc.diagnostics) from None
            entry = {"command_id": command.get("id"), "type": kind, "source": source, "from_revision": revision,
                     "at": _now(), "warnings": [d for d in warnings if d.get("code") != "no_op"]}
            if kind == "batch":
                entry["operations"] = len(command["payload"]["operations"])
            if details:
                entry.update(details)
            if any(d.get("code") == "no_op" for d in warnings):
                with self._lock:
                    self._last_command = dict(entry, status="no_op", to_revision=revision)
                return "no_op"
            try:
                save_document(self.path, new, revision)
            except RevisionConflict as exc:
                with self._lock:
                    self._disk_sig = None
                self._refresh_from_disk(blocking=True)
                raise ServiceError(409, f"{kind} was not saved: the project file changed on disk; refresh and "
                                   "resubmit against the current revision", exc.diagnostics) from None
            except DocumentError as exc:
                raise ServiceError(500, f"{kind} was not saved: {exc.message}", exc.diagnostics) from None
            except OSError as exc:
                raise ServiceError(500, f"{kind} was not saved ({type(exc).__name__}: {exc})") from None
            with self._lock:
                self._adopt_locked(new)
                self._disk_sig = self._read_back_signature(new)
                self._saved = None
                to_revision = new["current"]["revision"]
                self._last_command = dict(entry, status="accepted", to_revision=to_revision)
                self._commands.append(self._last_command)
                self._request_build_locked(f"{kind} committed revision {to_revision}")
        return "accepted"

    # --- Builds --------------------------------------------------------------------

    def _read_notes(self) -> Notes:
        """Read the sidecar. Any failure other than a plainly absent path is ``unreadable``, never ``absent``."""
        try:
            with open(self.notes_path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError as exc:
            if os.path.lexists(self.notes_path):  # for example a dangling symlink
                return Notes(True, None, None, f"{type(exc).__name__}: {exc.strerror or exc}")
            return Notes(False, None, None, None)
        except OSError as exc:
            return Notes(True, None, None, f"{type(exc).__name__}: {exc.strerror or exc}")
        return Notes(True, data, hashlib.sha256(data).hexdigest(), None)

    def _notes_problem(self, notes):
        return diagnostic("manual_notes_unreadable", f"the manual notes sidecar {self.notes_path} exists but cannot be "
                          f"read ({notes.error}); drafting outputs fail and nothing is published until it is "
                          "readable or removed", [self.document["current"]["id"]])

    def request_build(self, reason, force=False):
        with self._write_lock, self._lock:
            return self._request_build_locked(reason, force=force)

    def _request_build_locked(self, reason, force=False):
        """Queue a build of the current document (coalescing); stop an obsolete running build."""
        if self._closed:
            return None
        project = self.document["current"]
        notes = self._read_notes()
        target = (self.digest, notes.key)
        for candidate in (self.queued, self.active):
            if candidate is not None and candidate.status in ACTIVE_STATUSES and (
                    candidate.input_digest, candidate.notes_key) == target and not force:
                return candidate
        record = Build(f"b{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-r{project['revision']}-"
                       f"{secrets.token_hex(4)}", copy.deepcopy(project), self.digest, reason, notes)
        self._remember_locked(record)
        self._service_event_locked(record, record.message)
        if self.queued is not None:
            self._finish_unstarted_locked(self.queued, "superseded", f"Superseded before it started by revision "
                                          f"{record.source_revision}.")
            self.queued = None
        self.current = record
        if self.active is not None:
            if self.active.input_digest != record.input_digest or self.active.notes_key != record.notes_key:
                self._stop_locked(self.active, "superseded", f"Superseded by revision {record.source_revision}; "
                                  "its outputs will not be published.")
            self.queued = record
        else:
            self._start_locked(record)
        return record

    def _remember_locked(self, record):
        self._builds[record.id] = record
        while len(self._builds) > KEEP_BUILDS:
            for build_id, old in self._builds.items():
                if old.status in TERMINAL_STATUSES and old is not self.current:
                    del self._builds[build_id]
                    self._manifest_cache.pop(build_id, None)
                    break
            else:
                break

    def _service_event_locked(self, record, message, **extra):
        event = {"sequence": record.last_sequence + 1, "source": "service", "kind": "status", "artifact": None,
                 "message": message, "status": record.status, "build_id": record.id,
                 "source_revision": record.source_revision, "input_digest": record.input_digest,
                 "elapsed_s": round(time.monotonic() - record.created_monotonic, 3)}
        event.update(extra)
        self._append_event_locked(record, event)

    def _append_event_locked(self, record, event):
        if len(record.events) >= MAX_EVENTS:
            record.events_dropped += 1
            record.last_sequence = event["sequence"]
            return
        record.events.append(event)
        record.last_sequence = event["sequence"]
        if record.event_log is not None:
            with contextlib.suppress(OSError, ValueError):
                record.event_log.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                record.event_log.flush()

    def _finish_unstarted_locked(self, record, status, message):
        record.status = status
        record.message = message
        record.finished_at = _now()
        record.artifact_status = {artifact: "not_run" for artifact in REQUIRED_ARTIFACTS}
        self._service_event_locked(record, message)

    def _start_locked(self, record):
        record.dir = os.path.join(self.builds_root, record.id)
        try:
            os.mkdir(record.dir)
            record.event_log = open(os.path.join(record.dir, integrity.EVENTS), "ab")
            for event in record.events:
                record.event_log.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            snapshot = os.path.join(record.dir, integrity.SNAPSHOT)
            integrity.write_atomic(snapshot, integrity.json_bytes({"schema_version": 1, "current": record.project,
                                                                   "undo": [], "redo": []}))
            frozen = load_document(snapshot)["current"]
            if canonical_digest(frozen) != record.input_digest:
                raise DocumentError("the frozen snapshot does not reproduce the source digest")
            if record.notes_bytes is not None:
                integrity.write_atomic(os.path.join(record.dir, integrity.NOTES_COPY), record.notes_bytes)
            integrity.write_atomic(os.path.join(record.dir, integrity.REQUEST), integrity.json_bytes({
                "build_id": record.id, "project_id": record.project_id, "source_revision": record.source_revision,
                "input_digest": record.input_digest, "suite_options": self.suite_options,
                "manual_notes": record.notes_bytes is not None, "manual_notes_error": record.notes_error,
                "created_at": record.created_at}))
            argv = [self.python, "-I", WORKER_ENTRY, "--build-dir", record.dir, "--suite", self.suite]
            with open(os.path.join(record.dir, integrity.WORKER_LOG), "wb") as log:
                record.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                                  cwd=record.dir, start_new_session=True, close_fds=True)
        except (OSError, DocumentError, ValueError) as exc:
            record.status = "failed"
            record.finished_at = _now()
            record.artifact_status = {artifact: "not_run" for artifact in REQUIRED_ARTIFACTS}
            record.diagnostics.append(diagnostic("build_start_failed", f"the build could not start "
                                                 f"({type(exc).__name__}: {exc})", [record.project_id]))
            record.message = f"Failed: the build could not start ({exc})."
            self._service_event_locked(record, record.message)
            if record.event_log is not None:
                with contextlib.suppress(OSError):
                    record.event_log.close()
                record.event_log = None
            return
        record.status = "running"
        record.started_at = _now()
        record.message = f"Running: generating revision {record.source_revision} in worker process {record.process.pid}."
        self.active = record
        self._service_event_locked(record, record.message, worker_pid=record.process.pid)
        record.watchdog = threading.Timer(self.timeout, self._on_timeout, args=(record,))
        record.watchdog.daemon = True
        record.watchdog.start()
        record.monitor = threading.Thread(target=self._monitor, args=(record,), name=f"monitor-{record.id}",
                                          daemon=True)
        record.monitor.start()

    def _stop_locked(self, record, status, message, problem=None):
        """Decide a running build's terminal status now and terminate its worker; it can never be published."""
        if record.status != "running":
            return
        record.status = status
        record.message = message
        if problem is not None:
            record.diagnostics.append(problem)
        self._service_event_locked(record, message)
        self._terminate(record)

    def _terminate(self, record):
        process = record.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            process.terminate()
        timer = threading.Timer(self.grace, self._kill, args=(record,))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _kill(record):
        process = record.process
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

    def _on_timeout(self, record):
        with self._lock:
            self._stop_locked(record, "failed", f"Failed: the worker exceeded the {self.timeout:g} s time limit and "
                              "was terminated; nothing was published.",
                              diagnostic("worker_timeout", f"generation exceeded {self.timeout:g} s",
                                         [record.project_id]))

    def _monitor(self, record):
        try:
            violation = None
            stream = record.process.stdout
            try:
                while True:
                    line = stream.readline(MAX_EVENT_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_EVENT_LINE or not line.endswith(b"\n"):
                        violation = "an event line is truncated or longer than 64 KiB"
                        break
                    try:
                        event = integrity.parse_json(line)
                    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                        violation = f"an event line is not strict JSON ({exc})"
                        break
                    with self._lock:
                        violation = self._accept_worker_event_locked(record, event)
                    if violation:
                        break
            except (OSError, ValueError) as exc:
                violation = f"the event stream failed ({exc})"
            if violation:
                with self._lock:
                    self._stop_locked(record, "failed", f"Failed: worker protocol violation ({violation}); nothing "
                                      "was published.", diagnostic("worker_protocol", violation, [record.project_id]))
            with contextlib.suppress(OSError):
                stream.close()
            record.process.wait()
        finally:
            self._finalize(record)

    def _accept_worker_event_locked(self, record, event):
        """Append one worker event; return a protocol violation message instead if it is not acceptable."""
        if not isinstance(event, dict):
            return "an event is not an object"
        extra = sorted(str(k) for k in event if k not in _WORKER_EVENT_FIELDS)
        if extra:
            return f"an event has unsupported fields {extra}"
        for key, wanted in (("build_id", record.id), ("source_revision", record.source_revision),
                            ("input_digest", record.input_digest)):
            if event.get(key) != wanted:
                return f"an event's {key} does not match the build"
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence != record.worker_sequence + 1:
            return f"event sequence {str(sequence)[:20]} does not follow {record.worker_sequence}"
        kind, artifact, message = event.get("kind"), event.get("artifact"), event.get("message")
        if kind not in ("activity", "preview", "artifact") or artifact not in REQUIRED_ARTIFACTS or not isinstance(
                message, str):
            return "an event has an invalid kind, artifact or message"
        elapsed = event.get("elapsed_s")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
            return "an event has an invalid elapsed_s"
        public = {"sequence": record.last_sequence + 1, "source": "worker", "worker_sequence": sequence,
                  "build_id": record.id, "source_revision": record.source_revision,
                  "input_digest": record.input_digest, "kind": kind, "artifact": artifact, "message": message,
                  "elapsed_s": elapsed}
        for key in ("completed", "total"):
            if key in event:
                if type(event[key]) is not int or event[key] < 0:
                    return f"an event has an invalid {key}"
                public[key] = event[key]
        relpath = None
        if event.get("path") is not None:
            relpath = integrity.safe_relpath(event["path"])
            if relpath is None or integrity.resolve_inside(record.dir, relpath) is None:
                return f"an event names a missing or unsafe path {str(event['path'])[:80]!r}"
            public["path"] = relpath
            public["url"] = artifact_url(record.id, relpath)
        elif kind != "activity":
            return f"a {kind} event has no path"
        record.worker_sequence = sequence
        self._append_event_locked(record, public)
        if record.artifact_status[artifact] == "waiting":
            record.artifact_status[artifact] = "running"
        if kind == "preview":
            record.previews[artifact] = relpath
        elif kind == "artifact":
            base = relpath.rsplit("/", 1)[-1]
            keys = [key for key, name in PUBLISHED_FILES.items() if name == base]
            if keys:
                record.files[keys[0]] = relpath
            else:
                record.files.setdefault(artifact, relpath)
        return None

    def _log_tail(self, record, lines=15):
        try:
            with open(os.path.join(record.dir, integrity.WORKER_LOG), "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 8192))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        tail = "\n".join(text.splitlines()[-lines:]).strip()
        return f"; worker log tail:\n{tail}" if tail else ""

    def _evaluate(self, record, code):
        """Decide a finished worker's outcome from its result and the service's own recheck of the outputs."""
        project, digest, ids = record.project, record.input_digest, [record.project_id]
        problems = []
        result = None
        try:
            result = integrity.read_json(os.path.join(record.dir, integrity.RESULT))
        except FileNotFoundError:
            problems.append(diagnostic("worker_crashed", f"the worker exited with status {code} without writing a "
                                       f"result{self._log_tail(record)}", ids))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            problems.append(diagnostic("result_unreadable", f"the worker result is unreadable ({exc})", ids))
        if result is not None and (not isinstance(result, dict) or any(result.get(key) != wanted for key, wanted in (
                ("build_id", record.id), ("project_id", record.project_id),
                ("source_revision", record.source_revision), ("input_digest", record.input_digest)))):
            problems.append(diagnostic("result_mismatch", "the worker result names another build or source", ids))
            result = None
        if result is not None and code not in (0, 3):
            problems.append(diagnostic("worker_exit_status", f"the worker exited with status {code}"
                                       f"{self._log_tail(record)}", ids))
        if result is not None and result.get("status") == "cancelled":
            problems.append(diagnostic("worker_terminated", "the worker stopped on a termination signal the service "
                                       "did not send", ids))
        claimed = result.get("artifacts") if isinstance(result, dict) else None
        claimed = claimed if isinstance(claimed, dict) else {}
        cancelled = [a for a in REQUIRED_ARTIFACTS if isinstance(claimed.get(a), dict)
                     and claimed[a].get("status") == "cancelled"]
        verified = integrity.verify_outputs(record.dir, project, digest, skip=cancelled)
        artifacts = {}
        for artifact in REQUIRED_ARTIFACTS:
            entry = claimed.get(artifact) if isinstance(claimed.get(artifact), dict) else {}
            notes = entry.get("diagnostics") if isinstance(entry.get("diagnostics"), list) else []
            notes = [d for d in notes if integrity.diagnostic_problem(d) is None]
            if artifact in cancelled:
                artifacts[artifact] = {"status": "cancelled", "diagnostics": notes}
                continue
            mine = verified[artifact]
            if result is None or entry.get("status") == "failed":
                errors = [d for d in mine["diagnostics"] if d["severity"] == "error"]
                artifacts[artifact] = {"status": "failed", "diagnostics": notes or errors or problems[:1]}
            elif entry.get("status") != mine["status"]:
                artifacts[artifact] = {"status": "failed", "diagnostics": [diagnostic(
                    "worker_disagreement", f"the worker reported {LABELS[artifact]} {str(entry.get('status'))[:20]!r} "
                    f"but the service's recheck found {mine['status']!r}", ids)] + mine["diagnostics"]}
            else:
                artifacts[artifact] = {"status": mine["status"], "diagnostics": mine["diagnostics"]}
        status = "failed" if problems else integrity.overall_status(project, artifacts)
        return status, artifacts, problems

    def _seal(self, record, status, artifacts, problems, files):
        return {"format": integrity.SEAL_FORMAT, "build_id": record.id, "project_id": record.project_id,
                "source_revision": record.source_revision, "input_digest": record.input_digest, "status": status,
                "artifacts": artifacts, "diagnostics": problems, "files": files, "suite": self.suite,
                "notes_sha256": record.notes_sha, "notes_error": record.notes_error, "created_at": record.created_at,
                "sealed_at": _now()}

    def _write_seal(self, record, seal):
        integrity.write_atomic(os.path.join(record.dir, integrity.SEAL), integrity.json_bytes(seal))

    def _publish_locked(self, record, seal):
        """Under the storage lock, recheck the source and swing the pointer; return ``(status, problems)``."""
        ids = [record.project_id]

        def refuse(status, problem):
            seal.update(status=status, diagnostics=seal["diagnostics"] + [problem])
            self._write_seal(record, seal)
            return status, [problem]

        if self.before_publish is not None:
            self.before_publish(record)
        try:
            with integrity.file_lock(self.path + LOCK_SUFFIX, LOCK_TIMEOUT_S):
                try:
                    latest = load_document(self.path)["current"]
                except DocumentError as exc:
                    return refuse("superseded", diagnostic("source_unreadable", f"the source became unreadable "
                                                           f"before publication ({exc})", ids))
                if latest["id"] != record.project_id or canonical_digest(latest) != record.input_digest:
                    return refuse("superseded", diagnostic(
                        "source_changed", f"the source changed before publication (now revision "
                        f"{latest['revision']}); revision {record.source_revision} was not published", ids))
                notes = self._read_notes()
                if notes.error is not None or record.notes_error is not None:
                    return refuse("failed", self._notes_problem(notes if notes.error else Notes(
                        True, None, None, record.notes_error)))
                if notes.key != record.notes_key:
                    return refuse("superseded", diagnostic("notes_changed", "the manual notes sidecar changed during "
                                                           "the build; it was not published", ids))
                self._write_seal(record, seal)
                pointer = {"format": integrity.POINTER_FORMAT, "build_id": record.id,
                           "project_id": record.project_id, "source_revision": record.source_revision,
                           "input_digest": record.input_digest, "status": "complete",
                           "build_sha256": integrity.sha256_file(os.path.join(record.dir, integrity.SEAL)),
                           "published_at": _now()}
                try:
                    integrity.write_atomic(os.path.join(self.root, POINTER), integrity.json_bytes(pointer))
                except OSError as exc:
                    return refuse("failed", diagnostic("publish_failed", f"the pointer could not be written ({exc})",
                                                       ids))
                return "complete", []
        except TimeoutError as exc:
            return refuse("failed", diagnostic("source_locked", str(exc), ids))
        except OSError as exc:
            return refuse("failed", diagnostic("publish_failed", f"{type(exc).__name__}: {exc}", ids))

    def _finalize(self, record):
        published = False
        try:
            if record.watchdog is not None:
                record.watchdog.cancel()
            code = record.process.returncode if record.process is not None else None
            with self._lock:
                stopped = record.status != "running"
            outcome = None
            if not stopped:
                try:
                    outcome = self._evaluate(record, code)
                except Exception as exc:  # fail closed: an evaluation fault never publishes
                    outcome = ("failed", {}, [diagnostic("evaluation_fault", f"{type(exc).__name__}: {exc}",
                                                         [record.project_id])])
            try:
                files = integrity.hash_tree(record.dir)
            except OSError:
                files = {}
            with self._write_lock:
                with self._lock:
                    stopped = record.status != "running"
                    if stopped:
                        status, problems = record.status, list(record.diagnostics)
                        artifacts = {a: {"status": status, "diagnostics": []} for a in REQUIRED_ARTIFACTS}
                    else:
                        status, artifacts, problems = outcome
                seal = self._seal(record, status, artifacts, problems, files)
                if status == "complete" and not stopped:
                    with self._lock:
                        self._publishing = record.id  # state() waits, so no snapshot straddles the pointer swap
                    status, extra = self._publish_locked(record, seal)
                    problems = problems + extra
                    published = status == "complete"
                else:
                    with contextlib.suppress(OSError):
                        self._write_seal(record, seal)
                with self._lock:
                    record.status = status
                    record.exit_status = code
                    record.finished_at = _now()
                    record.published = published
                    record.diagnostics = list(problems) if stopped else problems + [
                        d for a in REQUIRED_ARTIFACTS for d in artifacts.get(a, {}).get("diagnostics", [])]
                    for artifact in REQUIRED_ARTIFACTS:
                        record.artifact_status[artifact] = artifacts.get(artifact, {}).get("status", "failed")
                    self._settle_files_locked(record)
                    record.message = self._terminal_message(record)
                    self._service_event_locked(record, record.message)
        finally:
            with self._lock:
                if record.status in ACTIVE_STATUSES:  # an unexpected fault above: never leave it running
                    record.status = "failed"
                    record.message = "Failed: the service could not finish evaluating this build."
                    self._service_event_locked(record, record.message)
                if record.event_log is not None:
                    with contextlib.suppress(OSError):
                        record.event_log.close()
                    record.event_log = None
                if self.active is record:
                    self.active = None
                if self._publishing == record.id:
                    self._publishing = None
                    self._pub_generation += 1
                    self._published_changed.notify_all()
                following, self.queued = self.queued, None
                if following is not None and not self._closed:
                    self._start_locked(following)
        if published:
            self._published_info(force=True)
        if self.keep_builds:
            self._prune_builds()

    def _prune_builds(self):
        """Delete the oldest sealed build directories beyond ``keep_builds``; never the published, current,
        running or queued build, and never an unsealed (interrupted) directory."""
        pointer, problem = integrity.read_pointer(self.root)
        if problem is not None:
            return  # the published build cannot be identified, so nothing is safe to remove
        with self._lock:
            protected = {r.id for r in (self.current, self.active, self.queued) if r is not None}
        if pointer is not None:
            protected.add(pointer["build_id"])
        try:
            names = sorted(n for n in os.listdir(self.builds_root) if BUILD_ID_PATTERN.match(n))
        except OSError:
            return
        sealed = []
        for name in names:
            with contextlib.suppress(OSError):
                if name not in protected:
                    sealed.append((os.stat(os.path.join(self.builds_root, name, integrity.SEAL)).st_mtime_ns, name))
        sealed = [name for _, name in sorted(sealed)]  # oldest seal first
        for name in sealed[:max(0, len(sealed) - self.keep_builds)]:
            shutil.rmtree(os.path.join(self.builds_root, name), ignore_errors=True)
            with self._lock:
                self._manifest_cache.pop(name, None)
                record = self._builds.get(name)
                if record is not None and record.status in TERMINAL_STATUSES:
                    del self._builds[name]

    def _settle_files_locked(self, record):
        """Final outputs: verified files for complete/pending; drop outputs of failed artifacts."""
        if record.status in ("complete", "pending"):
            record.files = {key: name for key, name in PUBLISHED_FILES.items()
                            if integrity.resolve_inside(record.dir, name) is not None}
            return
        for key in list(record.files):
            if record.artifact_status.get(ARTIFACT_OF_KEY.get(key, key)) != "complete" and record.artifact_status.get(
                    ARTIFACT_OF_KEY.get(key, key)) != "pending":
                del record.files[key]

    def _terminal_message(self, record):
        revision = record.source_revision
        if record.status == "complete":
            return f"Published: all five outputs verified complete for revision {revision}."
        if record.status == "pending":
            open_items = sum(1 for d in record.diagnostics if d["severity"] == "warning")
            return (f"Pending: all five outputs were generated for revision {revision}, but {open_items} open item(s) "
                    "remain; not published, the last complete output is retained.")
        if record.status == "failed":
            failed = [LABELS[a] for a in REQUIRED_ARTIFACTS if record.artifact_status.get(a) == "failed"]
            errors = [d for d in record.diagnostics if d["severity"] == "error"]
            first = f" First error: [{errors[0]['code']}] {errors[0]['message'][:240]}" if errors else ""
            which = f" ({', '.join(failed)} failed)" if failed else ""
            return f"Failed{which}: nothing was published for revision {revision}.{first}"
        return record.message

    # --- Published output ----------------------------------------------------------

    def _published_info(self, force=False):
        """``(verified pointer or None, problems)``; full recheck when the pointer or any sealed file changes."""
        with self._pub_lock:
            pointer, problem = integrity.read_pointer(self.root)
            if problem is not None:
                self._pub_cache = None
                return None, [problem]
            if pointer is None:
                self._pub_cache = None
                return None, []
            build_dir = os.path.join(self.builds_root, pointer["build_id"])
            paths = [os.path.join(self.root, POINTER)]
            for directory, subdirs, names in os.walk(build_dir):
                subdirs.sort()
                paths += [os.path.join(directory, n) for n in sorted(names)
                          if n not in (integrity.EVENTS, integrity.WORKER_LOG)]
            signature = (tuple(paths), integrity.stat_signature(paths))
            if not force and self._pub_cache is not None and self._pub_cache[0] == signature:
                return self._pub_cache[1], self._pub_cache[2]
            problems = integrity.verify_pointer(self.root, pointer)
            info = None
            if not problems:
                seal = integrity.read_json(os.path.join(build_dir, integrity.SEAL))
                info = dict(pointer, notes_sha256=seal.get("notes_sha256"))
            self._pub_cache = (signature, info, problems)
            return info, problems

    # --- Requests ------------------------------------------------------------------

    def command(self, body):
        self._commit(body, "command")
        return self.state()

    def build(self, body):
        body = self._require_fields(body, (), ("force", "expected_revision"))
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise ServiceError(400, "force must be a boolean")
        with self._write_lock:
            self._refresh_from_disk(blocking=True)
            published, _ = self._published_info()
            with self._lock:
                revision = self.document["current"]["revision"]
                if "expected_revision" in body and self._revision_field(body["expected_revision"]) != revision:
                    raise ServiceError(409, f"the project is at revision {revision}, not {body['expected_revision']}",
                                       [diagnostic("stale_revision", "build request names another revision",
                                                   [self.document["current"]["id"]])])
                if not force and self._is_current(published):
                    self._notices.append(diagnostic("build_not_needed", f"revision {revision} is already published "
                                                    "complete and verified; no build started",
                                                    [self.document["current"]["id"]], "warning"))
                else:
                    self._request_build_locked("build requested", force=force)
        return self.state()

    def cancel(self, body):
        body = self._require_fields(body, ("build_id",))
        build_id = body["build_id"]
        if not isinstance(build_id, str):
            raise ServiceError(400, "build_id must be a string")
        with self._write_lock, self._lock:
            record = self._builds.get(build_id)
            if record is None:
                raise ServiceError(409, f"unknown or expired build {build_id[:80]!r}",
                                   [diagnostic("build_unknown", "no such active or recent build")])
            if record.status not in ACTIVE_STATUSES:
                raise ServiceError(409, f"build {build_id} is already {record.status}",
                                   [diagnostic("build_not_active", f"build {build_id} is {record.status}")])
            if record is self.queued:
                self._finish_unstarted_locked(record, "cancelled", "Cancelled before it started.")
                self.queued = None
            else:
                self._stop_locked(record, "cancelled", "Cancelled by request; its partial previews are kept as a "
                                  "cancelled draft and nothing is published.")
        return self.state()

    def select(self, body):
        body = self._require_fields(body, ("semantic_ids", "origin"))
        origin, ids = body["origin"], body["semantic_ids"]
        if origin not in SELECTION_ORIGINS:
            raise ServiceError(400, f"origin must be one of {', '.join(SELECTION_ORIGINS)}")
        if not isinstance(ids, list) or len(ids) > MAX_SELECTION or not all(isinstance(i, str) for i in ids):
            raise ServiceError(400, f"semantic_ids must be a list of at most {MAX_SELECTION} ID strings")
        unique = list(dict.fromkeys(ids))
        with self._lock:
            model = self.document["current"]["model"]
            known = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
            unknown = [i for i in unique if i not in known]
            if unknown:
                raise ServiceError(400, "selection names IDs that are not entities, ports or connections of the current "
                                   "revision", [diagnostic("unknown_semantic_id", f"unknown ID {i[:80]!r}") for i in
                                                unknown[:10]])
            if set(unique) != set(self.selection["semantic_ids"]):  # identical sets do not echo
                self.selection = {"sequence": self.selection["sequence"] + 1, "semantic_ids": unique, "origin": origin,
                                  "revision": self.document["current"]["revision"], "updated_at": _now()}
        return self.state()

    def _prune_selection_locked(self):
        model = self.document["current"]["model"]
        known = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
        kept = [i for i in self.selection["semantic_ids"] if i in known]
        if kept != self.selection["semantic_ids"]:
            self.selection = {"sequence": self.selection["sequence"] + 1, "semantic_ids": kept, "origin": "service",
                              "revision": self.document["current"]["revision"], "updated_at": _now()}

    def save(self, body):
        body = self._require_fields(body, ("expected_revision",))
        expected = self._revision_field(body["expected_revision"])
        with self._write_lock:
            self._refresh_from_disk(blocking=True)
            with self._lock:
                document = self.document
                if self._disk_problem is not None:
                    raise ServiceError(409, "the project file on disk is invalid; repair it and reopen",
                                       [self._disk_problem])
            revision = document["current"]["revision"]
            if expected != revision:
                raise ServiceError(409, f"the project is at revision {revision}, not {expected}",
                                   [diagnostic("stale_revision", f"save expected revision {expected}",
                                               [document["current"]["id"]])])
            try:
                with integrity.file_lock(self.path + LOCK_SUFFIX, LOCK_TIMEOUT_S):
                    with open(self.path, "rb") as handle:
                        on_disk = integrity.parse_json(handle.read())
                        if _canonical(on_disk) != _canonical(document):
                            raise ServiceError(409, "the file on disk differs from the service's revision; reopen",
                                               [diagnostic("disk_diverged", "disk and memory differ",
                                                           [document["current"]["id"]])])
                        os.fsync(handle.fileno())
                    directory = os.open(os.path.dirname(self.path), os.O_RDONLY)
                    try:
                        with contextlib.suppress(OSError):
                            os.fsync(directory)
                    finally:
                        os.close(directory)
            except (OSError, ValueError, TimeoutError) as exc:
                raise ServiceError(500, f"the saved file could not be verified ({type(exc).__name__}: {exc})") from None
            with self._lock:
                self._saved = {"revision": revision, "input_digest": self.digest, "verified_at": _now(),
                               "path": self.path, "undo": len(document["undo"]), "redo": len(document["redo"])}
        return self.state()

    def reopen(self, body):
        self._require_fields(body, ())
        with self._write_lock:
            try:
                document = load_document(self.path)
            except DocumentError as exc:
                raise ServiceError(409, f"the project file does not load; the service keeps revision "
                                   f"{self.document['current']['revision']} ({exc.message})", exc.diagnostics) from None
            with self._lock:
                changed = _canonical(document) != _canonical(self.document)
                self._adopt_locked(document)
                self._disk_sig = integrity.stat_signature([self.path])
                self._disk_problem = None
                self._saved = None
                self._notices.append(diagnostic("reopened", f"reopened {self.path} at revision "
                                                f"{document['current']['revision']} with {len(document['undo'])} undo "
                                                f"and {len(document['redo'])} redo step(s)",
                                                [document["current"]["id"]], "warning"))
            published, _ = self._published_info(force=True)
            with self._lock:
                if changed or (not self._is_current(published) and self.current is None):
                    self._request_build_locked("project reopened")
        return self.state()

    def propose(self, body):
        body = self._require_fields(body, ("text", "expected_revision"), ("mode", "research"))
        expected = self._revision_field(body["expected_revision"])
        if not isinstance(body["text"], str):
            raise ServiceError(400, "text must be a string")
        mode = body.get("mode", "quick")
        if mode not in PROPOSAL_MODES:
            raise ServiceError(400, f"mode must be one of {', '.join(PROPOSAL_MODES)}")
        research = body.get("research", False)
        if type(research) is not bool:
            raise ServiceError(400, "research must be true or false")
        if research and mode != "engineering":
            raise ServiceError(400, "equipment research is available only for engineering plans (mode engineering); "
                               "quick edits never research", [diagnostic("research_mode", "research requested "
                                                                         f"with mode {mode}")])
        self._refresh_from_disk(blocking=True)
        with self._lock:
            document, digest = self.document, self.digest
        revision = document["current"]["revision"]
        if expected != revision:
            raise ServiceError(409, f"the project is at revision {revision}, not {expected}; refresh first",
                               [diagnostic("stale_revision", "proposal requested against another revision",
                                           [document["current"]["id"]])])
        if mode == "engineering":
            return self._propose_engineering(document, digest, body["text"], research)
        proposal = proposals.propose(document["current"], body["text"], expected, f"prop-{secrets.token_hex(6)}")
        validation = {"accepted": False, "diagnostics": [], "warnings": []}
        if proposal["supported"]:
            try:
                _, warnings = apply_command(document, proposal["commands"][0])
                validation = {"accepted": True, "diagnostics": [], "warnings": warnings,
                              "no_op": any(d.get("code") == "no_op" for d in warnings)}
            except CommandError as exc:
                validation = {"accepted": False, "diagnostics": exc.diagnostics, "warnings": []}
        validation["geometry_checked"] = False
        proposal["validation"] = validation
        proposal["applied"] = False
        # Contract 0.3 metadata; the deterministic parser itself is unchanged and is not a language model.
        proposal.update(mode="quick", questions=[], impacts=[], command_count=len(proposal["commands"]),
                        operation_count=len(proposal["commands"]),
                        planner={"kind": "deterministic", "parser": proposal["parser"], "language_model": False})
        with self._lock:
            self._remember_proposal_locked(proposal)
        return copy.deepcopy(proposal)

    def _remember_proposal_locked(self, proposal):
        self._proposals[proposal["id"]] = proposal
        while len(self._proposals) > KEEP_PROPOSALS:
            self._proposals.popitem(last=False)

    def _set_stage_locked(self, proposal_id, stage):
        plan = self._plans.get(proposal_id)
        if plan is not None:
            now = time.monotonic()
            if plan["stage"] is not None:
                plan["stages"].append({"stage": plan["stage"], "elapsed_s": round(now - plan["stage_started"], 3)})
            plan["stage"], plan["stage_started"] = stage, now

    def _source_changed(self, digest, project_id):
        """Whether the authoritative source moved away from ``digest`` (adopting a valid external change first)."""
        self._refresh_from_disk(blocking=True)
        with self._lock:
            return self.digest != digest or self.document["current"]["id"] != project_id, \
                self.document["current"]["revision"]

    def _research(self, document, digest, text, proposal_id, cancel):
        """Run the research stage: ``(verified record, None)`` or ``(None, refused proposal)``. Never raises."""
        project = document["current"]
        cleaned, problem = clean_request(text)
        if problem:
            return None, None  # propose_engineering refuses the request text itself; nothing is researched
        research_id = f"res-{proposal_id[len('prop-'):]}"
        section = {"requested": True, "id": research_id, "brief": None, "metadata": {}, "error": None}

        def refuse(status, code, message, metadata=None):
            """Research did not yield a verified brief: an explicit refusal; the planner is not run."""
            record = dict(section, status=status, metadata=evidence.safe_metadata(metadata),
                          error={"code": code, "message": message[:600]})
            reason = (f"Equipment research {status}: {message}. Research was requested, so no plan is made without "
                      "verified sources.")
            return research_failed_proposal(document, cleaned, proposal_id, self.planner, record,
                                            "rejected" if status == "rejected" else "unavailable", reason,
                                            [diagnostic(code, message[:600], [project["id"]])])

        if self.researcher is None:
            return None, refuse("unavailable", "research_unavailable", self.research_unavailable or
                                "no researcher is configured")
        started = time.monotonic()
        try:
            reply = self.researcher.run(cleaned, cancel)
        except Exception as exc:  # noqa: BLE001 - every researcher fault is an explicit refusal, never a fallback
            code = str(getattr(exc, "code", "") or "research_failed")[:60]
            message = str(getattr(exc, "message", "") or exc or type(exc).__name__)
            metadata = getattr(exc, "metadata", None)
            if cancel.is_set():
                status = "cancelled"
            else:
                status = "unavailable" if any(w in code for w in _UNAVAILABLE_RESEARCH) else "rejected"
            return None, refuse(status, code, message, metadata)
        if cancel.is_set():
            return None, refuse("cancelled", "research_cancelled", "the proposal was cancelled")
        try:
            verified = evidence.verify_reply(reply, cleaned, research_id,
                                             self.researcher.describe() if hasattr(self.researcher, "describe")
                                             else None)
        except evidence.ResearchRejected as exc:
            metadata = reply.get("metadata") if isinstance(reply, dict) else None
            return None, refuse("rejected", "research_brief_invalid",
                                f"the research brief failed verification ({'; '.join(exc.problems[:3])})", metadata)
        verified["elapsed_s"] = round(time.monotonic() - started, 3)
        changed, revision = self._source_changed(digest, project["id"])
        if changed:
            message = (f"the project changed to revision {revision} while the equipment was researched (the request "
                       f"was made at revision {project['revision']}); planning was not started")
            return None, research_failed_proposal(
                document, cleaned, proposal_id, self.planner, evidence.public_record(verified), "unavailable",
                f"Stale: {message}. Request a new plan.", [diagnostic("stale_proposal", message, [project["id"]])],
                stale=True)
        return verified, None

    def _propose_engineering(self, document, digest, text, research=False):
        """Run the (researcher and) planner on a frozen snapshot with no lock held; re-check the source after."""
        project = document["current"]
        if not self._plan_slots.acquire(blocking=False):
            raise ServiceError(409, f"the engineering planner is busy ({self.max_plans} plan(s) running); wait "
                               "for it to finish or cancel it", [diagnostic("planner_busy", "concurrent plan limit "
                                                                            "reached", [project["id"]])])
        proposal_id = f"prop-{secrets.token_hex(6)}"
        cancel = threading.Event()
        try:
            with self._lock:
                if self._planning_closed:
                    raise ServiceError(409, "the service is shutting down; no plan was started")
                now = time.monotonic()
                self._plans[proposal_id] = {"id": proposal_id, "cancel": cancel, "started": now,
                                            "started_at": _now(), "expected_revision": project["revision"],
                                            "input_digest": digest, "research": research,
                                            "stage": "researching" if research else "planning",
                                            "stage_started": now, "stages": []}
            try:
                verified, refused = (self._research(document, digest, text, proposal_id, cancel) if research
                                     else (None, None))
                if refused is not None:
                    proposal = refused
                elif cancel.is_set():
                    proposal = failed_proposal(document, text, proposal_id, self.planner,
                                               "the proposal was cancelled before planning started")
                else:
                    with self._lock:
                        self._set_stage_locked(proposal_id, "planning")
                    proposal = propose_engineering(document, text, proposal_id, self.planner, cancel,
                                                   research=verified)
            except Exception as exc:  # fail closed: a planning fault is an unavailable proposal, never a command
                proposal = failed_proposal(document, text, proposal_id, self.planner,
                                           f"the planning step failed ({type(exc).__name__}: {str(exc)[:200]})")
            with self._lock:
                self._set_stage_locked(proposal_id, None)
                plan = self._plans.get(proposal_id)
                if plan is not None:
                    proposal["stages"] = plan["stages"]
        finally:
            with self._lock:
                self._plans.pop(proposal_id, None)
            self._plan_slots.release()
        self._refresh_from_disk(blocking=True)
        with self._lock:
            now = self.document["current"]
            if self.digest != digest or now["id"] != project["id"]:
                proposal["stale"] = True
                proposal["validation"]["warnings"].append(diagnostic(
                    "stale_proposal", f"the project changed to revision {now['revision']} while the planner ran; "
                    f"this plan was checked against revision {project['revision']} and cannot be applied. Request "
                    "a new plan.", [project["id"]], "warning"))
            self._remember_proposal_locked(proposal)
        return copy.deepcopy(proposal)

    def cancel_proposal(self, body):
        """Stop a running engineering plan (its planner process is terminated; it yields no command)."""
        body = self._require_fields(body, ("proposal_id",))
        with self._lock:
            plan = self._plans.get(body["proposal_id"]) if isinstance(body["proposal_id"], str) else None
            if plan is None:
                raise ServiceError(409, "no engineering plan with that ID is running",
                                   [diagnostic("plan_not_running", "unknown, finished or cancelled plan")])
            plan["cancel"].set()
        return self.state()

    def _engineering_command_locked(self, proposal, expected):
        """The checked batch of an applicable engineering proposal and its source digest; refuse anything else."""
        status = proposal.get("status")
        if status != "ready" or not proposal["supported"] or len(proposal["commands"]) != 1:
            code = "proposal_questions" if status == "questions" else "proposal_unsupported"
            raise ServiceError(400, f"an engineering proposal with status {status!r} has no command to apply",
                               [diagnostic(code, proposal.get("reason") or proposal["summary"])])
        if not proposal["validation"]["accepted"]:
            raise ServiceError(400, "this proposal did not pass validation and cannot be applied",
                               [diagnostic("proposal_invalid", proposal["summary"])])
        if proposal["applied"]:
            raise ServiceError(409, f"proposal {proposal['id']} was already applied")
        if proposal.get("stale"):
            raise ServiceError(409, f"proposal {proposal['id']} is stale: the project changed while it was planned",
                               [diagnostic("stale_proposal", "request a new plan")])
        try:
            expires = datetime.datetime.fromisoformat(proposal["expires_at"])
        except (KeyError, TypeError, ValueError):
            expires = None
        if expires is None or datetime.datetime.now(datetime.timezone.utc) >= expires:
            raise ServiceError(409, f"proposal {proposal['id']} has expired; request a new plan",
                               [diagnostic("proposal_expired", f"expired at {proposal.get('expires_at')}")])
        if expected != proposal["expected_revision"]:
            raise ServiceError(409, f"proposal {proposal['id']} was made for revision "
                               f"{proposal['expected_revision']}, not {expected}")
        return copy.deepcopy(proposal["commands"][0]), proposal["base_digest"]

    def apply_proposal(self, body):
        body = self._require_fields(body, ("proposal_id", "expected_revision"))
        expected = self._revision_field(body["expected_revision"])
        digest = None
        with self._lock:
            proposal = self._proposals.get(body["proposal_id"]) if isinstance(body["proposal_id"], str) else None
            if proposal is None:
                raise ServiceError(409, "unknown or expired proposal; request a new proposal")
            if proposal.get("mode") == "engineering":
                command, digest = self._engineering_command_locked(proposal, expected)
            else:
                if not proposal["supported"]:
                    raise ServiceError(400, "an unsupported proposal has no command to apply",
                                       [diagnostic("proposal_unsupported", proposal["summary"])])
                if proposal["applied"]:
                    raise ServiceError(409, f"proposal {proposal['id']} was already applied")
                if expected != proposal["expected_revision"]:
                    raise ServiceError(409, f"proposal {proposal['id']} was made for revision "
                                       f"{proposal['expected_revision']}, not {expected}")
                command = copy.deepcopy(proposal["commands"][0])
        outcome = self._commit(command, f"proposal {proposal['id']}", expected_digest=digest)
        with self._lock:
            proposal["applied"] = outcome == "accepted"
        return self.state()

    def reroute(self, body):
        """Guided reroute of one non-current physical route: compute with no lock held, commit one ``set_route``.

        The route comes from the authoritative current document only (``rerouting.plan_reroute``). The commit goes
        through ``_commit`` with the computed source digest, so a change between computation and commit is refused
        and nothing is written. Every refusal leaves revision, history, file and publication unchanged.
        """
        body = self._require_fields(body, ("connection_id", "expected_revision"))
        expected = self._revision_field(body["expected_revision"])
        connection_id = body["connection_id"]
        if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 200:
            raise ServiceError(400, "connection_id must be a nonempty string of at most 200 characters")
        if not self._reroute_slot.acquire(blocking=False):
            raise ServiceError(409, "another reroute is being computed; wait for it and retry",
                               [diagnostic("reroute_busy", "one route computation runs at a time", [connection_id])])
        try:
            self._refresh_from_disk(blocking=True)
            with self._lock:
                if self._disk_problem is not None:
                    raise ServiceError(409, "the project file on disk is invalid; repair it and reopen",
                                       [self._disk_problem])
                document, digest = self.document, self.digest
            project = document["current"]
            if expected != project["revision"]:
                raise ServiceError(409, f"the project is at revision {project['revision']}, not {expected}; refresh "
                                   "first", [diagnostic("stale_revision", "reroute requested against another "
                                                        "revision", [project["id"]])])
            try:
                plan = rerouting.plan_reroute(project, connection_id)
            except rerouting.RerouteError as exc:
                raise ServiceError(exc.status, f"no route was applied: {exc.message}", [exc.diagnostic]) from None
            token = secrets.token_hex(6)
            command = rerouting.reroute_command(project, plan, f"cmd-reroute-{token}", f"asm-reroute-{token}", _now())
            try:  # dry run outside every lock; the commit repeats it on the then-current document
                apply_command(document, command)
            except CommandError as exc:
                raise ServiceError(400, f"the computed route for {plan['tag']} did not pass validation; nothing was "
                                   "applied", exc.diagnostics) from None
            if self.before_reroute_commit is not None:
                self.before_reroute_commit(plan)
            details = {"route": {"connection_id": connection_id, "points": len(plan["points_m"]),
                                 "bends": plan["bends"], "bend_radii_mm": plan["bend_radii_mm"],
                                 "length_m": plan["length_m"], "candidates_checked": plan["candidates_checked"],
                                 "previous": plan["previous"]}}
            self._commit(command, f"reroute {connection_id}", expected_digest=digest, stale_what="route",
                         details=details)
        finally:
            self._reroute_slot.release()
        return self.state()

    # --- Ask (read-only) -----------------------------------------------------------

    def ask(self, body):
        """Answer one question about the persisted current revision (``docs/ASK.md``); never changes anything.

        The project file is read as persisted now (``_persisted_source``), not taken from the in-memory copy, so
        an external save is seen even before any state poll, yet it is neither adopted nor built here. The
        request's revision and digest must match it before anything else runs (409 ``stale_ask``); an unreadable
        or invalid file fails closed (409 ``ask_source_invalid`` / ``ask_source_locked``). Local answers need no
        slot; a broader question runs the shared tool-free bridge once on that frozen snapshot, with no lock held,
        cancellable and time-limited. Afterwards the file is read again: the answer keeps its snapshot label and
        is ``stale`` with the persisted ``current_revision`` if the project changed meanwhile.

        The Ask is tracked by ``request_id`` from acceptance, before the source read can wait for a writer, until
        its outcome is settled, so cancel-ask and ``close()`` reach it in every phase. Until its model starts it
        is checked during the source wait, after the read, after preparing and immediately before the bridge
        call, and stops with 409 ``ask_cancelled`` / ``ask_closed``: no model was started and nothing was sent.
        """
        try:
            request = assistant.parse_request(body)
        except assistant.AskRequestError as exc:
            raise ServiceError(400, f"invalid Ask request: {exc}", [diagnostic("invalid_ask", str(exc))]) from None
        record = self._track_ask(request.request_id)
        try:
            project, digest = self._persisted_source(record)  # a fresh structure nobody else holds: the frozen snapshot
            self._ask_checkpoint(record, "prepare")
            if request.expected_revision != project["revision"] or request.input_digest != digest:
                raise ServiceError(409, f"the project file is at revision {project['revision']} (digest "
                                   f"{digest[:12]}…), not revision {request.expected_revision} (digest "
                                   f"{request.input_digest[:12]}…); nothing was asked. Refresh and ask again.",
                                   [diagnostic("stale_ask", "the question names another revision or digest than the "
                                               "persisted project file", [project["id"]])])
            try:
                prepared = assistant.prepare(project, digest, request, f"ask-{secrets.token_hex(6)}")
            except assistant.AskRequestError as exc:
                raise ServiceError(400, f"invalid Ask request: {exc}", [diagnostic("invalid_ask", str(exc),
                                                                                   [project["id"]])]) from None
            local = prepared.job is None
            self._ask_checkpoint(record, settle=local)  # a local answer is settled here; a later cancel is refused
            response = prepared.response if local else self._run_ask(prepared, record)
        finally:
            self._untrack_ask(record)  # every exit: stale, invalid, locked, busy, stopped, fault, answered
        try:
            now, now_digest = self._persisted_source()
        except ServiceError as exc:  # fail closed: currentness unknown is never reported as current
            return assistant.stamp(response, None, None, None, unverified=exc.diagnostics[0]["message"])
        return assistant.stamp(response, now["id"], now["revision"], now_digest)

    def _track_ask(self, request_id):
        """Register an accepted Ask before its first wait, so cancel-ask and ``close()`` reach it from now on."""
        record = {"request_id": request_id, "cancel": threading.Event(), "reason": None, "phase": "source"}
        with self._lock:
            if self._asks_closed:
                raise ServiceError(409, "the service is shutting down; no question was started",
                                   [diagnostic("ask_closed", "the service is shutting down")])
            if request_id in self._asks:
                raise ServiceError(409, f"an Ask with request_id {request_id} is still in progress",
                                   [diagnostic("ask_busy", "the same request_id is still in progress")])
            if len(self._asks) >= MAX_ASKS_IN_PROGRESS:
                raise ServiceError(409, f"{MAX_ASKS_IN_PROGRESS} Asks are already in progress; wait for one to "
                                   "finish", [diagnostic("ask_busy", f"at most {MAX_ASKS_IN_PROGRESS} Asks are "
                                                         "tracked at once")])
            self._asks[request_id] = record
        return record

    def _untrack_ask(self, record):
        """End an Ask's tracking (idempotent) and return the stop reason recorded until then."""
        with self._lock:
            if self._asks.get(record["request_id"]) is record:
                del self._asks[record["request_id"]]  # later cancel or time-out signals no longer reach it
            return record["reason"]

    def _ask_checkpoint(self, record, phase=None, settle=False):
        """Stop an Ask that cancel-ask or ``close()`` reached before its model started, else enter ``phase``.

        The reason is read and the phase entered in one ``_lock`` hold, which cancel-ask and ``close()`` also take
        to record a stop: a stop recorded before the ``model`` phase is always seen here, a later one reaches the
        bridge through the cancel event. ``settle`` also ends the tracking, so a later cancel-ask is
        ``ask_not_running`` instead of acknowledged and then ignored."""
        with self._lock:
            reason = self._untrack_ask(record) if settle else record["reason"]
            if reason is None and phase is not None:
                record["phase"] = phase
        if reason == "shutdown":
            raise ServiceError(409, "the service is shutting down; the question was stopped before any model "
                               "started and nothing was sent", [diagnostic("ask_closed", "the service is shutting "
                                                                            "down")])
        if reason is not None:
            raise ServiceError(409, "the question was cancelled before any model started; nothing was sent to a "
                               "model and nothing was changed",
                               [diagnostic("ask_cancelled", "cancel-ask was accepted while the Ask was still "
                                           "checking the project file or preparing; no model was started")])

    def _persisted_source(self, record=None):
        """``(current, digest)`` of the project file as atomically persisted now; read-only and never cached.

        One strict ``load_document`` (parse and validate) under a brief shared storage lock (``<project>.lock``,
        which ``save_document`` and publication hold exclusively), so it reads one committed document. No service
        lock is held while reading or waiting, and it never calls ``_refresh_from_disk``: the in-memory document,
        its signature and disk problem, notices, history and builds stay with the ordinary state refresh. An
        unreadable or invalid file raises 409 ``ask_source_invalid``, a writer holding the lock past
        ``ASK_SOURCE_LOCK_TIMEOUT_S`` 409 ``ask_source_locked``; the in-memory revision is never substituted.
        With a tracked ``record`` the wait is taken in ``ASK_WAIT_SLICE_S`` slices and a cancel-ask or ``close()``
        stops it between slices (``_ask_checkpoint``), before anything is read.
        """
        with self._lock:
            project_id = self.document["current"]["id"]
        lock_path = self.path + LOCK_SUFFIX
        deadline = time.monotonic() + ASK_SOURCE_LOCK_TIMEOUT_S
        while True:
            if record is not None:
                self._ask_checkpoint(record)
            try:
                with _shared_lock(lock_path, max(0.0, min(ASK_WAIT_SLICE_S, deadline - time.monotonic()))):
                    document = load_document(self.path)
            except TimeoutError:
                if time.monotonic() < deadline:
                    continue
                waited = f"timed out after {ASK_SOURCE_LOCK_TIMEOUT_S:g} s waiting for {lock_path}"
                raise ServiceError(409, f"the project file is locked by a writer ({waited}); nothing was asked. Ask "
                                   "again in a moment.", [diagnostic("ask_source_locked", f"the persisted project "
                                                                     f"could not be read: {waited}",
                                                                     [project_id])]) from None
            except (DocumentError, OSError) as exc:
                reason = str(exc)[:400]
                raise ServiceError(409, f"the project file cannot be read or validated ({reason}); nothing was "
                                   "asked. Ask does not answer from the service's in-memory revision, because it "
                                   "cannot confirm that revision is still current. Repair the file and ask again.",
                                   [diagnostic("ask_source_invalid", f"the persisted project file is unreadable or "
                                               f"invalid: {reason}", [project_id])]
                                   + list(getattr(exc, "diagnostics", []))[:4]) from None
            current = document["current"]
            return current, canonical_digest(current)

    def _run_ask(self, prepared, record):
        """One model Ask on the frozen snapshot: separate slot, cancellation and time limit (``record`` is tracked)."""
        if not self._ask_slot.acquire(blocking=False):
            raise ServiceError(409, "another model Ask is running; wait for it or cancel it (identity and "
                               "connectivity questions are still answered locally)",
                               [diagnostic("ask_busy", "one model Ask runs at a time", [prepared.project["id"]])])
        timer = None
        reply = error = None
        try:
            self._ask_checkpoint(record, "model")  # the last check, immediately before the bridge call
            timer = threading.Timer(self.ask_timeout, self._ask_timed_out, args=(record,))
            timer.daemon = True
            timer.start()
            try:
                reply = self.planner.run(prepared.job.prompt, prepared.job.schema, record["cancel"])
            except Exception as exc:  # noqa: BLE001 - fail closed: any bridge fault is an unavailable answer
                error = exc
        finally:
            reason = self._untrack_ask(record)
            if timer is not None:
                timer.cancel()
            self._ask_slot.release()
        if reason in ("cancelled", "shutdown"):  # a requested stop wins, even over a reply that just arrived
            return assistant.finish(prepared, cancelled=reason)
        if reason == "timeout" and (error is not None or reply is None):
            return assistant.finish(prepared, timed_out=True)
        return assistant.finish(prepared, reply=reply, error=error)

    def _ask_timed_out(self, record):
        with self._lock:
            if self._asks.get(record["request_id"]) is not record:
                return
            record["reason"] = record["reason"] or "timeout"
        record["cancel"].set()

    def cancel_ask(self, body):
        """Signal one tracked Ask to stop; nothing else is affected.

        An Ask is tracked from acceptance until its outcome is settled. Still checking the project file or
        preparing, it stops at its next check and returns 409 ``ask_cancelled``: no model starts. Running its
        model, the bridge child is terminated and the Ask returns ``cancelled``. Returns only ``{request_id,
        cancel_requested: true}``: never ``state()``, whose disk refresh could adopt an external change and queue a
        build. Nothing is remembered for an unknown or settled ID (409 ``ask_not_running``)."""
        if not isinstance(body, dict) or set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ServiceError(400, "cancel-ask accepts exactly {request_id}",
                               [diagnostic("invalid_ask", "cancel-ask accepts exactly {request_id}")])
        with self._lock:
            record = self._asks.get(body["request_id"])
            if record is None:
                raise ServiceError(409, "no Ask with that request_id is in progress",
                                   [diagnostic("ask_not_running", "unknown or finished Ask, or a local answer "
                                               "already prepared")])
            record["reason"] = record["reason"] or "cancelled"
        record["cancel"].set()
        return {"request_id": record["request_id"], "cancel_requested": True}

    # --- Reads ---------------------------------------------------------------------

    def _is_current(self, published, notes=None):
        """The verified publication matches the current digest and readable notes; never with unreadable notes."""
        notes = notes or self._read_notes()
        if published is None or notes.error is not None:
            return False
        with self._lock:
            return (published["project_id"] == self.document["current"]["id"]
                    and published["input_digest"] == self.digest and published.get("notes_sha256") == notes.sha)

    def artifact_path(self, build_id, relpath):
        """Real path of a regular file inside one of this service's build directories; ``None`` otherwise."""
        if not isinstance(build_id, str) or not BUILD_ID_PATTERN.match(build_id):
            return None
        build_dir = os.path.join(self.builds_root, build_id)
        if not os.path.isdir(build_dir):
            return None
        return integrity.resolve_inside(build_dir, relpath)

    def events(self, build_id, after):
        with self._lock:
            record = self._builds.get(build_id)
            if record is None:
                raise ServiceError(404, f"unknown or expired build {str(build_id)[:80]!r}")
            return {"build_id": record.id, "source_revision": record.source_revision,
                    "input_digest": record.input_digest, "status": record.status,
                    "last_sequence": record.last_sequence, "events_dropped": record.events_dropped,
                    "events": [e for e in record.events if e["sequence"] > after]}

    def _open_items_locked(self):
        if self._open_items[0] != self.digest:
            items = [d for d in validate_project(self.document["current"]) if d["severity"] == "warning"]
            self._open_items = (self.digest, items)
        return list(self._open_items[1])

    @staticmethod
    def _rejected(record, published):
        """A completed (once published) build that is not the verified publication: its outputs are not offered."""
        return record.status == "complete" and (published is None or published["build_id"] != record.id)

    def _build_payload_locked(self, record, published):
        payload = {
            "id": record.id,
            "source_revision": record.source_revision,
            "input_digest": record.input_digest,
            "status": record.status,
            "last_sequence": record.last_sequence,
            "events": list(record.events),
            "previews": {a: artifact_url(record.id, p) for a, p in record.previews.items()},
            "artifacts": {k: artifact_url(record.id, p) for k, p in record.files.items()},
            "message": record.message,
            "project_id": record.project_id,
            "artifact_status": dict(record.artifact_status),
            "diagnostics": list(record.diagnostics),
            "reason": record.reason,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "published": record.published,
            "events_dropped": record.events_dropped,
            "worker": {"pid": record.process.pid if record.process else None, "exit_status": record.exit_status},
            "integrity": "verified" if record.status == "complete" else None,
        }
        if self._rejected(record, published):
            # Its lifecycle status stays 'complete' (it was published), but nothing of it is presented as current.
            payload.update(integrity="rejected", published=False, previews={}, artifacts={},
                           artifact_status=dict.fromkeys(REQUIRED_ARTIFACTS, "rejected"),
                           message=f"{record.message} Its outputs are no longer the verified published set (see the "
                                   "published_* diagnostics) and are not offered.")
        return payload

    def _published_payload(self, published):
        return {"build_id": published["build_id"], "project_id": published["project_id"],
                "source_revision": published["source_revision"], "input_digest": published["input_digest"],
                "status": "complete", "published_at": published.get("published_at"),
                "artifacts": {key: artifact_url(published["build_id"], name) for key, name in PUBLISHED_FILES.items()}}

    def _artifact_choice_locked(self, published):
        """Per key: the current unpublished build's output or preview, else the verified publication.

        A ``complete`` build is offered only as the verified published build; if the pointer or its files no
        longer verify, or the pointer names another build, none of its outputs are offered under any label.
        """
        urls, sources = {}, {}
        record = self.current
        working = record is not None and record.dir is not None and record.status != "complete"
        for key, name in PUBLISHED_FILES.items():
            relpath = None
            if working:
                relpath = record.files.get(key) or (record.previews.get(key) if key in REQUIRED_ARTIFACTS else None)
            if relpath is not None:
                urls[key] = artifact_url(record.id, relpath)
                sources[key] = {"origin": "working",
                                "kind": "preview" if relpath.startswith("preview/") else "output",
                                "build_id": record.id, "build_status": record.status,
                                "artifact_status": record.artifact_status.get(ARTIFACT_OF_KEY[key]),
                                "source_revision": record.source_revision, "input_digest": record.input_digest}
            elif published is not None:
                urls[key] = artifact_url(published["build_id"], name)
                sources[key] = {"origin": "published", "kind": "output", "build_id": published["build_id"],
                                "build_status": "complete", "artifact_status": "complete",
                                "source_revision": published["source_revision"],
                                "input_digest": published["input_digest"]}
        return urls, sources

    def _display_mapping_locked(self, source):
        """Representation map of the shown display output, or ``None`` when it is unknown.

        Only a display artifact verified ``complete`` or ``pending`` is trusted, and only if its manifest is a
        display manifest of that build's identity with a well-formed map; nothing else is cached.
        """
        if not source or source.get("artifact_status") not in ("complete", "pending"):
            return None
        build_id = source["build_id"]
        if build_id not in self._manifest_cache:
            path = self.artifact_path(build_id, integrity.manifest_name("display"))
            try:
                manifest = integrity.read_json(path) if path is not None else None
            except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                manifest = None
            mapping = manifest.get("representations") if isinstance(manifest, dict) else None
            if (not isinstance(mapping, dict) or manifest.get("adapter") != "display"
                    or manifest.get("status") not in ("complete", "pending")
                    or manifest.get("source_revision") != source["source_revision"]
                    or manifest.get("input_digest") != source["input_digest"]
                    or not all(isinstance(v, list) and all(isinstance(i, str) for i in v) for v in mapping.values())):
                return None
            self._manifest_cache[build_id] = mapping
        return self._manifest_cache[build_id]

    def _selection_payload_locked(self, sources):
        project = self.document["current"]
        model, pid = project["model"], project["pid"]
        source = sources.get("display")
        mapping = self._display_mapping_locked(source)
        display_build = source["build_id"] if mapping is not None else None
        display_revision = source["source_revision"] if mapping is not None else None
        resolved = []
        for sid in self.selection["semantic_ids"]:
            if sid in model["entities"]:
                kind, entity_id, tag = "entity", sid, model["entities"][sid]["tag"]
            elif sid in model["ports"]:
                kind, entity_id = "port", model["ports"][sid]["entity_id"]
                tag = f"{model['entities'].get(entity_id, {}).get('tag')}.{model['ports'][sid]['name']}"
            elif sid in model["connections"]:
                kind, entity_id, tag = "connection", None, model["connections"][sid]["tag"]
            else:
                continue
            if kind == "connection":
                occurrences = sorted(eid for eid, edge in pid["edges"].items() if edge["connection_id"] == sid)
            else:
                occurrences = sorted(s for s, symbol in pid["symbols"].items() if symbol["entity_id"] == entity_id)
            # Unknown (None) unless the trusted mapping names this ID; an explicit [] is an intentional absence.
            components = list(mapping[sid]) if mapping is not None and sid in mapping else None
            item = {"id": sid, "kind": kind, "tag": tag, "pid_occurrences": occurrences,
                    "display_components": components,
                    "display_build_id": display_build if components is not None else None}
            if components == [] and kind == "entity":
                placement = project["physical"]["placements"].get(sid) or {}
                if display_revision == project["revision"] and placement.get("status") != "placed":
                    item["absent_3d_reason"] = placement.get("reason") or "unresolved placement"
                else:
                    item["absent_3d_reason"] = (f"no 3D representation in the displayed revision {display_revision} "
                                                "(for example a conceptual item)")
            resolved.append(item)
        return dict(self.selection, resolved=resolved)

    def _sync_locked(self, published, record, problems, notes):
        project = self.document["current"]
        current = self._is_current(published, notes)
        payload = {"current_revision": project["revision"], "current_digest": self.digest,
                   "published_revision": published["source_revision"] if published else None,
                   "published_current": current}
        same = record is not None and record.input_digest == self.digest
        if current:
            state, message = "synchronized", (f"All five outputs are verified complete for revision "
                                              f"{project['revision']}.")
        elif same and record.status in ACTIVE_STATUSES:
            state, message = "building", f"Revision {project['revision']} is {record.status}."
        elif same and record.status in ("pending", "failed", "cancelled"):
            state, message = record.status, record.message
        elif notes.error is not None:
            state, message = "blocked", (f"The manual notes sidecar exists but cannot be read ({notes.error}); "
                                         "nothing can be published until it is readable or removed.")
        elif published is None and problems:
            state, message = "invalid", ("The published output set no longer verifies and is not offered (see the "
                                         "published_* diagnostics); rebuild to publish a verified set.")
        elif published is not None and published["input_digest"] == self.digest:
            state, message = "stale", ("The published outputs were generated with different manual notes; rebuild "
                                       "to include the current notes.")
        elif published is not None:
            state, message = "stale", (f"The published outputs are from revision {published['source_revision']}; "
                                       f"the project is at revision {project['revision']}.")
        else:
            state, message = "unpublished", "No complete output set has been published for this project."
        payload.update(state=state, message=message)
        return payload

    def state(self):
        self._refresh_from_disk(blocking=False)
        generators = availability() if self.suite == REAL_SUITE else None
        for _ in range(5):
            generation = self._pub_generation
            published, problems = self._published_info()
            with self._lock:
                if self._publishing is not None:
                    self._published_changed.wait_for(lambda: self._publishing is None, timeout=LOCK_TIMEOUT_S)
                if self._pub_generation == generation:
                    return self._state_locked(published, problems, generators)
        with self._lock:  # publications keep landing; report the latest pointer read with its own identity
            return self._state_locked(published, problems, generators)

    def _state_locked(self, published, problems, generators):
        document = self.document
        project = document["current"]
        diagnostics = self._open_items_locked()
        if self._disk_problem is not None:
            diagnostics.append(self._disk_problem)
        for problem in problems:
            diagnostics.append(dict(problem, code=f"published_{problem['code']}",
                                    message=f"published output rejected: {problem['message']}"))
        diagnostics += list(self._notices)
        for generator, reason in (generators or {}).items():
            if reason:
                diagnostics.append(diagnostic("generator_unavailable", f"{generator} generator unavailable: "
                                              f"{reason}; builds will fail until it exists", [project["id"]],
                                              "warning"))
        if self.interrupted:
            diagnostics.append(diagnostic("interrupted_builds", f"{len(self.interrupted)} build(s) from an earlier "
                                          "service run stopped before sealing and were never published",
                                          [project["id"]], "warning"))
        notes = self._read_notes()
        if notes.error is not None:
            diagnostics.append(self._notes_problem(notes))
        urls, sources = self._artifact_choice_locked(published)
        record = self.current
        return {
            "project": project,
            "diagnostics": diagnostics,
            "selection": self._selection_payload_locked(sources),
            "build": self._build_payload_locked(record, published) if record is not None else None,
            "published": self._published_payload(published) if published is not None else None,
            "artifacts": urls,
            "history": {"undo": len(document["undo"]), "redo": len(document["redo"]),
                        "recent": list(self._commands)},
            "artifact_sources": sources,
            "sync": self._sync_locked(published, record, problems, notes),
            "document": {"path": self.path, "revision": project["revision"], "input_digest": self.digest,
                         "saved": self._saved, "disk_valid": self._disk_problem is None,
                         "notes": {"path": self.notes_path, "present": notes.present,
                                   "readable": (notes.error is None) if notes.present else None,
                                   "sha256": notes.sha, "error": notes.error}},
            "active_build": ({"id": self.active.id, "status": self.active.status,
                              "source_revision": self.active.source_revision, "message": self.active.message}
                             if self.active is not None and self.active is not record else None),
            "builds": [{"id": b.id, "status": b.status, "source_revision": b.source_revision}
                       for b in reversed(self._builds.values())][:12],
            "last_command": self._last_command,
            "planning": self._planning_payload_locked(),
            "capabilities": dict(CAPABILITIES, equipment_research=self.researcher is not None),
            "service": {"version": SERVICE_VERSION, "suite": self.suite, "generators": generators,
                        "workflow_dir": self.root, "started_at": self.started_at,
                        "interrupted_builds": list(self.interrupted)},
        }

    def _planning_payload_locked(self):
        now = time.monotonic()
        try:
            planner = dict(self.planner.describe()) if hasattr(self.planner, "describe") else {}
        except Exception as exc:  # noqa: BLE001 - status reporting must not fail the state request
            planner = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        try:
            researcher = (dict(self.researcher.describe()) if hasattr(self.researcher, "describe") else {}) \
                if self.researcher is not None else None
        except Exception as exc:  # noqa: BLE001 - status reporting must not fail the state request
            researcher = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        return {"modes": list(PROPOSAL_MODES), "max_concurrent": self.max_plans, "planner": planner,
                "research": {"available": self.researcher is not None, "researcher": researcher,
                             "reason": self.research_unavailable, "modes": ["engineering"]},
                "active": [{"id": p["id"], "started_at": p["started_at"], "elapsed_s": round(now - p["started"], 3),
                            "expected_revision": p["expected_revision"], "cancelled": p["cancel"].is_set(),
                            "research": p["research"], "stage": p["stage"],
                            "stage_elapsed_s": round(now - p["stage_started"], 3), "stages": list(p["stages"])}
                           for p in self._plans.values()]}

    def project_view(self):
        state = self.state()
        return {"project": state["project"], "document": state["document"], "diagnostics": state["diagnostics"]}

    def history_view(self):
        return self.state()["history"]

    def selection_view(self):
        return self.state()["selection"]
