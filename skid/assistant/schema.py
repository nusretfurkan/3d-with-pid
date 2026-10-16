"""Ask request checks and the strict, answer-only output schema (pure standard library).

``parse_request(body)`` accepts exactly the frozen ``POST /api/ask`` fields
(``request_id``, ``question``, ``selected_ids``, ``expected_revision``,
``input_digest`` and optional ``history`` and ``displayed_source``) and
returns an ``AskRequest``; anything else raises ``AskRequestError`` (HTTP 400
``invalid_ask``). It checks shape and bounds only; whether the IDs and the
revision/digest match the project is decided against the frozen snapshot.

``ANSWER_SCHEMA`` is the complete Ask-specific output schema with every limit.
It has no operations, commands, URLs, HTML, file or apply fields: a model can
only return an outcome, a summary, labelled statements citing snapshot IDs,
unknowns and a clarification question. ``cli_schema(allowed_ids)`` strips the
limit keywords for the CLI's advisory ``--json-schema`` and constrains cited
IDs to an ``enum`` of the snapshot's IDs. ``validate_answer`` applies the
complete schema plus the reference and consistency rules to untrusted output;
any problem rejects the whole output. Nothing is truncated or repaired.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass

__all__ = ["ANSWER_SCHEMA", "AskRequest", "AskRequestError", "MAX_HISTORY", "MAX_HISTORY_FIELD", "MAX_QUESTION",
           "MAX_SELECTED", "OUTCOMES", "STATEMENT_KINDS", "cli_schema", "parse_request", "validate_answer"]

REQUEST_FIELDS = ("request_id", "question", "selected_ids", "expected_revision", "input_digest")
OPTIONAL_FIELDS = ("history", "displayed_source")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_QUESTION = 4000
MAX_SELECTED = 16
MAX_ID_CHARS = 200
MAX_HISTORY = 6
MAX_HISTORY_FIELD = 2000
DISPLAY_MODES = ("live", "published")
MAX_ENUM_IDS = 2000
MAX_OUTPUT_BYTES = 64 << 10
_LIMIT_KEYWORDS = ("minLength", "maxLength", "pattern", "minItems", "maxItems", "minimum", "maximum")

OUTCOMES = ("answer", "clarification", "change_request")
STATEMENT_KINDS = ("recorded_fact", "graph_conclusion", "general_explanation", "unknown")
CITING_KINDS = ("recorded_fact", "graph_conclusion")


class AskRequestError(ValueError):
    """An Ask request outside the frozen shape or bounds (HTTP 400 ``invalid_ask``)."""


@dataclass(frozen=True)
class AskRequest:
    request_id: str
    question: str
    selected_ids: tuple
    expected_revision: int
    input_digest: str
    history: tuple = ()  # ((question, answer), ...), untrusted page-session context
    displayed_source: dict | None = None


def _bad_text(value, newlines=True):
    allowed = "\n\t\r" if newlines else ""
    return any((ord(ch) < 32 and ch not in allowed) or ord(ch) == 127 or ch in "  " for ch in value)


def _revision(value, name):
    if type(value) is not int or value < 0:
        raise AskRequestError(f"{name} must be a nonnegative integer")
    return value


def _digest(value, name):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise AskRequestError(f"{name} must be a 64-character lowercase hexadecimal SHA-256 digest")
    return value


def parse_request(body) -> AskRequest:
    if not isinstance(body, dict):
        raise AskRequestError("the request body must be a JSON object")
    missing = [name for name in REQUEST_FIELDS if name not in body]
    extra = sorted(str(name)[:40] for name in body if name not in REQUEST_FIELDS + OPTIONAL_FIELDS)
    if missing or extra:
        raise AskRequestError(f"request fields: missing {missing}, unsupported {extra}")
    request_id = body["request_id"]
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise AskRequestError("request_id must be a nonempty safe ID of at most 80 characters "
                              "(letters, digits and . _ : -, starting with a letter or digit)")
    question = body["question"]
    if not isinstance(question, str) or not 1 <= len(question) <= MAX_QUESTION:
        raise AskRequestError(f"question must be a string of 1 to {MAX_QUESTION} characters")
    if not question.strip():
        raise AskRequestError("question must not be blank")
    if _bad_text(question):
        raise AskRequestError("question contains control characters")
    selected = body["selected_ids"]
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED:
        raise AskRequestError(f"selected_ids must be a list of at most {MAX_SELECTED} IDs")
    for value in selected:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_CHARS or _bad_text(value, False):
            raise AskRequestError(f"each selected ID must be a nonblank string of at most {MAX_ID_CHARS} characters")
    expected = _revision(body["expected_revision"], "expected_revision")
    digest = _digest(body["input_digest"], "input_digest")
    history = body.get("history", [])
    if history is None:
        history = []
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise AskRequestError(f"history must be a list of at most {MAX_HISTORY} records")
    turns = []
    for index, record in enumerate(history):
        if not isinstance(record, dict) or set(record) != {"question", "answer"}:
            raise AskRequestError(f"history[{index}] must be exactly {{question, answer}}")
        for key in ("question", "answer"):
            value = record[key]
            if not isinstance(value, str) or len(value) > MAX_HISTORY_FIELD or _bad_text(value):
                raise AskRequestError(f"history[{index}].{key} must be a string of at most {MAX_HISTORY_FIELD} "
                                      "characters without control characters")
        turns.append((record["question"], record["answer"]))
    displayed = body.get("displayed_source")
    if displayed is not None:
        if not isinstance(displayed, dict) or set(displayed) != {"source_revision", "input_digest", "mode"}:
            raise AskRequestError("displayed_source must be null or exactly {source_revision, input_digest, mode}")
        if displayed["mode"] not in DISPLAY_MODES:
            raise AskRequestError(f"displayed_source.mode must be one of {', '.join(DISPLAY_MODES)}")
        displayed = {"source_revision": _revision(displayed["source_revision"], "displayed_source.source_revision"),
                     "input_digest": _digest(displayed["input_digest"], "displayed_source.input_digest"),
                     "mode": displayed["mode"]}
    return AskRequest(request_id, question, tuple(dict.fromkeys(selected)), expected, digest, tuple(turns), displayed)


# --- Answer schema -----------------------------------------------------------------


def _text(maximum, minimum=0):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


ANSWER_SCHEMA = _obj({
    "outcome": {"type": "string", "enum": list(OUTCOMES)},
    "summary": _text(800, 1),
    "statements": {"type": "array", "maxItems": 16, "items": _obj({
        "kind": {"type": "string", "enum": list(STATEMENT_KINDS)},
        "text": _text(600, 1),
        "reference_ids": {"type": "array", "maxItems": 8, "items": _text(MAX_ID_CHARS, 1)}})},
    "unknowns": {"type": "array", "maxItems": 12, "items": _text(400, 1)},
    "clarification_question": _text(400),
})


def cli_schema(allowed_ids=()) -> dict:
    """The schema without limit keywords; cited IDs restricted to ``allowed_ids`` (advisory; checked locally)."""

    def strip(node):
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key not in _LIMIT_KEYWORDS}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    schema = strip(copy.deepcopy(ANSWER_SCHEMA))
    allowed = sorted(set(allowed_ids))
    if 0 < len(allowed) <= MAX_ENUM_IDS:
        schema["properties"]["statements"]["items"]["properties"]["reference_ids"]["items"] = {
            "type": "string", "enum": allowed}
    return schema


def _check(value, schema, path, problems):
    if len(problems) >= 25:
        return
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            problems.append((path, "must be an object"))
            return
        properties = schema["properties"]
        missing = [name for name in schema.get("required", ()) if name not in value]
        extra = sorted(str(name)[:40] for name in value if name not in properties)
        if missing:
            problems.append((path, f"is missing {', '.join(missing)}"))
        if extra:
            problems.append((path, f"has unsupported fields {', '.join(extra)}"))
        for name, sub in properties.items():
            if name in value:
                _check(value[name], sub, f"{path}.{name}", problems)
    elif kind == "array":
        if not isinstance(value, list):
            problems.append((path, "must be an array"))
            return
        if len(value) > schema.get("maxItems", len(value)):
            problems.append((path, f"has more than {schema['maxItems']} items"))
            return
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{index}]", problems)
    elif kind == "string":
        if not isinstance(value, str):
            problems.append((path, "must be a string"))
            return
        if len(value) < schema.get("minLength", 0) or (schema.get("minLength", 0) > 0 and not value.strip()):
            problems.append((path, "must not be empty"))
        if len(value) > schema.get("maxLength", len(value)):
            problems.append((path, f"is longer than {schema['maxLength']} characters"))
        elif any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 or ch in "  " for ch in value):
            problems.append((path, "contains control characters"))
        if "enum" in schema and value not in schema["enum"]:
            problems.append((path, f"{value[:40]!r} is not one of {', '.join(schema['enum'])}"))
    else:  # pragma: no cover - schema construction fault
        problems.append((path, f"unsupported schema node {kind!r}"))


def validate_answer(answer, allowed_ids) -> list:
    """``[(path, problem), ...]`` for untrusted model output; empty when it may be shown."""
    problems = []
    try:
        encoded = json.dumps(answer, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return [("answer", f"is not plain JSON ({str(exc)[:80]})")]
    if len(encoded) > MAX_OUTPUT_BYTES:
        return [("answer", f"is {len(encoded)} bytes; the limit is {MAX_OUTPUT_BYTES}")]
    _check(answer, ANSWER_SCHEMA, "answer", problems)
    if problems:
        return problems
    allowed = set(allowed_ids)
    for index, statement in enumerate(answer["statements"]):
        for position, rid in enumerate(statement["reference_ids"]):
            if rid not in allowed:
                problems.append((f"answer.statements[{index}].reference_ids[{position}]",
                                 f"{rid[:80]!r} is not an entity, port or connection ID of this revision"))
        if statement["kind"] in CITING_KINDS and not statement["reference_ids"]:
            problems.append((f"answer.statements[{index}]", f"a {statement['kind']} must cite at least one ID"))
    if answer["outcome"] == "answer" and not answer["statements"]:
        problems.append(("answer.statements", "an answer needs at least one statement"))
    if answer["outcome"] == "clarification" and not answer["clarification_question"].strip():
        problems.append(("answer.clarification_question", "a clarification needs a question"))
    return problems[:25]
