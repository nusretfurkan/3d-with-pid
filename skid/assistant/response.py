"""Ask response records: the frozen DTO, verified references and source labels.

Every response has exactly the frozen fields ``{id, request_id, project_id,
source_revision, input_digest, current_revision, stale, status, answer,
references, unknowns, limitations, responder, displayed_source}``. ``status``
is ``answered``, ``clarification``, ``unavailable`` or ``cancelled``.
``answer`` is inert plain text. ``references`` are records the service built
from the frozen snapshot, ``{id, tag, kind, field_paths, facts}``, never text
supplied by a model; a model can only cite IDs, which must resolve in that
snapshot. ``unknowns`` and ``limitations`` are bounded lists of plain
strings. ``responder`` is ``{kind, language_model, query, model_called}``
plus, for a model call, safe bridge metadata (no prompt, raw output, e-mail
or credentials) and an ``error`` record when it failed.

``source_revision``/``input_digest`` name the snapshot the answer describes.
``stamp`` adds ``current_revision`` and ``stale`` from the persisted project
the service reads after answering: an answer whose project changed meanwhile
keeps its source and says so, and one whose currentness cannot be checked is
stale with ``current_revision: null``. Its text and tags are never rewritten
after a rename.
"""

from __future__ import annotations

from .query import plain

__all__ = ["MAX_ANSWER_CHARS", "MAX_REFERENCES", "RESPONSE_FIELDS", "STATUSES", "answer_text", "make_response",
           "references", "safe_bridge", "source_label", "stamp"]

STATUSES = ("answered", "clarification", "unavailable", "cancelled")
RESPONSE_FIELDS = ("id", "request_id", "project_id", "source_revision", "input_digest", "current_revision", "stale",
                   "status", "answer", "references", "unknowns", "limitations", "responder", "displayed_source")
MAX_REFERENCES = 32
MAX_UNKNOWNS = 16
MAX_LIMITATIONS = 12
MAX_ITEM_CHARS = 600
MAX_ANSWER_CHARS = 16000
SAFE_BRIDGE_KEYS = ("kind", "model", "effort", "verified", "init_model", "response_models", "api_key_source", "auth",
                    "tools", "mcp_servers", "cli_version", "elapsed_s", "duration_ms", "num_turns", "usage",
                    "output_sha256", "structured_output_calls", "session_persistence", "fake")


def answer_text(lines) -> str:
    """Join answer lines as inert plain text (control characters other than newline removed), bounded."""
    cleaned = []
    for line in lines:
        text = "".join(ch if ch.isprintable() or ch == " " else " " for ch in str(line)).rstrip()
        cleaned.append(text)
    text = "\n".join(cleaned).strip()
    if len(text) > MAX_ANSWER_CHARS:
        note = "\n… (the rest of this local answer is not shown: it exceeded the answer limit)"
        text = text[:MAX_ANSWER_CHARS - len(note)].rstrip() + note
    return text


def references(query, ids, limit=MAX_REFERENCES):
    """Verified reference records for the semantic IDs among ``ids`` (unknown IDs are skipped), bounded."""
    found = []
    for record_id in dict.fromkeys(ids):
        if len(found) >= limit:
            break
        if query.kind_of(record_id):
            found.append(query.reference(record_id))
    return found


def _bounded(items, limit, chars=MAX_ITEM_CHARS):
    out = []
    for item in items:
        text = plain(item, chars)
        if text and text not in out:
            out.append(text)
    if len(out) > limit:
        out = out[:limit - 1] + [f"{len(out) - limit + 1} further item(s) not listed"]
    return out


def _safe(value, depth=0):
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return plain(value, 200)
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in list(value)[:16]]
    if isinstance(value, dict):
        return {plain(k, 60): _safe(v, depth + 1) for k, v in list(value.items())[:16] if isinstance(k, str)}
    return plain(value, 200)


def safe_bridge(metadata) -> dict:
    """Bridge metadata safe to show: model, effort, auth method/subscription, tools, timing, usage, hashes."""
    if not isinstance(metadata, dict):
        return {}
    return {key: _safe(metadata[key]) for key in SAFE_BRIDGE_KEYS if key in metadata}


def source_label(revision, digest):
    return f"current model revision {revision} (digest {str(digest)[:12]}…)"


def make_response(answer_id, request, project, digest, status, lines, reference_records, unknowns, limitations,
                  responder) -> dict:
    if status not in STATUSES:  # pragma: no cover - programming fault
        raise ValueError(f"unknown Ask status {status!r}")
    return {
        "id": answer_id,
        "request_id": request.request_id,
        "project_id": project.get("id"),
        "source_revision": project.get("revision"),
        "input_digest": digest,
        "current_revision": project.get("revision"),
        "stale": False,
        "status": status,
        "answer": answer_text(lines),
        "references": list(reference_records)[:MAX_REFERENCES],
        "unknowns": _bounded(unknowns, MAX_UNKNOWNS),
        "limitations": _bounded(limitations, MAX_LIMITATIONS),
        "responder": responder,
        "displayed_source": dict(request.displayed_source) if request.displayed_source else None,
    }


def stamp(response, project_id, revision, digest, unverified=None) -> dict:
    """The response as sent, labelled against the persisted project read after answering.

    ``current_revision`` is that revision; ``stale`` is true when its digest differs from the snapshot's. If the
    project could not be read (``unverified`` says why) or now holds another project, currentness is unknown:
    ``current_revision`` is ``None`` and ``stale`` is true, never current.
    """
    result = dict(response)
    source = response["source_revision"]
    if unverified is None and project_id == response["project_id"]:
        result.update(current_revision=revision, stale=digest != response["input_digest"])
        if not result["stale"]:
            return result
        note = (f"The project changed while this answer was prepared: it describes revision {source}, and the "
                f"project is now at revision {revision}. Ask again to get an answer for the current revision; "
                "nothing was resent automatically.")
    else:
        result.update(current_revision=None, stale=True)
        why = (f"the project file could not be checked after answering ({plain(unverified, 300)})" if unverified
               is not None else f"the project file now holds another project ({plain(project_id, 80)})")
        note = (f"Whether revision {source} is still current is unknown: {why}. This answer describes revision "
                f"{source} and is marked stale; ask again once the project file is readable. Nothing was resent "
                "automatically.")
    result["limitations"] = [note] + [item for item in response["limitations"] if item != note][:MAX_LIMITATIONS - 1]
    return result
