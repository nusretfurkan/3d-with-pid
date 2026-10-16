"""Persistence of engineering documents.

Loading is strict: invalid UTF-8, duplicate object keys, nonfinite numbers, and
documents that fail validation (including undo/redo history) raise
``DocumentError``. Readers need no lock because writers replace the file
atomically.

Saving validates the whole document and its JSON round trip before touching the
disk, then takes an exclusive lock on the stable sibling file ``<path>.lock``
(which is never removed). Under the lock it rereads the target and compares the
project identity and the expected disk revision, writes a temporary file in the
same directory, flushes it, and atomically replaces the target. A failure before
the replacement leaves the previous bytes untouched. I/O failures propagate as
``OSError``.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import stat
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from .errors import DocumentError, RevisionConflict
from .validation import SCHEMA_VERSION, validate_document

LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.01


def load_document(path: str) -> dict:
    """Read, strictly parse, and validate a document; raise ``DocumentError`` if unusable."""
    source = os.fspath(path)
    try:
        with open(source, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise DocumentError(
            f"cannot read {source}",
            [_diagnostic("unreadable_document", f"cannot read {source}: {exc}")],
        ) from exc
    document = _parse(data, source)
    _require_valid(document, f"{source} is not a valid document")
    _encode(document)  # a loaded document must be savable unchanged
    return document


def save_document(path: str, document: dict, expected_disk_revision: int | None) -> None:
    """Validate and atomically write ``document`` if the disk still holds the expected revision.

    ``expected_disk_revision=None`` creates a new file and requires an absent
    target. Otherwise the target must contain the same project at exactly that
    revision; the new document may not lower the revision, and may keep it only
    when its content is identical. Mismatches raise ``RevisionConflict``.
    """
    if expected_disk_revision is not None and not _is_revision(expected_disk_revision):
        raise TypeError("expected_disk_revision must be a nonnegative int or None")
    target = os.path.abspath(os.fspath(path))
    _require_valid(document, "refusing to save an invalid document")
    data = _encode(document)
    if _canonical(_parse(data, "serialized document")) != _canonical(document):
        raise DocumentError(
            "document does not survive a JSON round trip unchanged",
            [_diagnostic("lossy_serialization", "the serialized document differs from the input")],
        )
    with _locked(target + LOCK_SUFFIX):
        _check_disk(target, document, expected_disk_revision)
        _replace_atomically(target, data)


class _DuplicateKey(ValueError):
    pass


class _NonFiniteNumber(ValueError):
    pass


def _diagnostic(code, message):
    return {"code": code, "severity": "error", "entity_ids": [], "message": message}


def _is_revision(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(name):
    raise _NonFiniteNumber(name)


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise _NonFiniteNumber(text)
    return value


def _parse(data, source):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError(
            f"{source} is not valid UTF-8",
            [_diagnostic("invalid_encoding", f"{source} is not valid UTF-8: {exc}")],
        ) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except _DuplicateKey as exc:
        message = f"{source} repeats JSON object key {exc.args[0]!r}"
        raise DocumentError(message, [_diagnostic("duplicate_json_key", message)]) from exc
    except _NonFiniteNumber as exc:
        message = f"{source} contains the nonfinite number {exc.args[0]}"
        raise DocumentError(message, [_diagnostic("nonfinite_number", message)]) from exc
    except (ValueError, RecursionError) as exc:
        message = f"{source} is not valid JSON: {exc}"
        raise DocumentError(message, [_diagnostic("invalid_json", message)]) from exc


def _require_valid(document, message):
    diagnostics = validate_document(document)
    errors = [d for d in diagnostics if d["severity"] == "error"]
    if errors:
        raise DocumentError(f"{message} ({len(errors)} error(s))", diagnostics)


def _encode(document):
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        message = f"document text cannot be encoded as UTF-8: {exc}"
        raise DocumentError(message, [_diagnostic("invalid_encoding", message)]) from exc
    except (TypeError, ValueError, RecursionError) as exc:
        message = f"document is not serializable as strict JSON: {exc}"
        raise DocumentError(message, [_diagnostic("invalid_json", message)]) from exc


def _canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@contextlib.contextmanager
def _locked(lock_path):
    if fcntl is None:
        raise DocumentError(
            "file locking is unavailable on this platform",
            [_diagnostic("lock_unavailable", "fcntl.flock is required to save documents safely")],
        )
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    message = f"timed out after {LOCK_TIMEOUT_S} s waiting for {lock_path}"
                    raise DocumentError(message, [_diagnostic("lock_timeout", message)]) from None
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _check_disk(target, document, expected):
    """Compare the on-disk document with the save request; must run under the lock."""
    project_id = document["current"]["id"]
    revision = document["current"]["revision"]
    if expected is None:
        if os.path.lexists(target):
            message = f"{target} already exists; creating a new document requires an absent target"
            raise RevisionConflict(message, [_diagnostic("target_exists", message)])
        return
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        message = f"expected revision {expected} on disk, but {target} does not exist"
        raise RevisionConflict(message, [_diagnostic("target_missing", message)]) from None
    try:
        disk = _parse(data, target)
    except DocumentError as exc:
        raise DocumentError(
            f"cannot verify the existing {target}; refusing to overwrite it",
            exc.diagnostics,
        ) from exc
    disk_id, disk_revision, problem = _identity(disk)
    if problem:
        message = f"existing {target} {problem}; refusing to overwrite it"
        raise DocumentError(message, [_diagnostic("unverifiable_target", message)])
    if disk_id != project_id:
        message = f"{target} holds project {disk_id!r}, not {project_id!r}"
        raise RevisionConflict(message, [_diagnostic("project_mismatch", message)])
    if disk_revision != expected:
        message = f"{target} is at revision {disk_revision}, but revision {expected} was expected"
        raise RevisionConflict(message, [_diagnostic("revision_conflict", message)])
    if revision < disk_revision:
        message = f"document revision {revision} would move {target} back from revision {disk_revision}"
        raise RevisionConflict(message, [_diagnostic("revision_regression", message)])
    if revision == disk_revision and _canonical(disk) != _canonical(document):
        message = f"document differs from {target} at revision {disk_revision} without advancing the revision"
        raise RevisionConflict(message, [_diagnostic("revision_not_advanced", message)])


def _identity(document):
    """Return ``(project_id, revision, problem)`` for an on-disk document."""
    if not isinstance(document, dict):
        return None, None, "is not a document object"
    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return None, None, f"has unsupported schema_version {version!r}"
    current = document.get("current")
    project_id = current.get("id") if isinstance(current, dict) else None
    revision = current.get("revision") if isinstance(current, dict) else None
    if not isinstance(project_id, str) or not project_id.strip() or not _is_revision(revision):
        return None, None, "has no readable project identity and revision"
    return project_id, revision, None


def _replace_atomically(target, data):
    directory, name = os.path.split(target)
    temporary = os.path.join(directory, f".{name}.{uuid.uuid4().hex}.tmp")
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if mode is not None:
                os.chmod(temporary, mode)
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    _sync_directory(directory)


def _sync_directory(directory):
    """Best-effort durability of the rename; the replacement itself has already succeeded."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
