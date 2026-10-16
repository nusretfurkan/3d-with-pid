"""The equipment research brief ``skid.equipment.research/1``: schema, exact bounds and validator.

A brief is plain JSON::

    {"schema": "skid.equipment.research/1",
     "query": <the exact request text sent to the researcher>,
     "equipment_type": <one line>,
     "summary": <concise paraphrase; newlines allowed>,
     "sources": [{"id": "S1", "url": <public http(s) URL observed in a search result>,
                  "title": <title observed for that URL in the search result>,
                  "observations": [<concise paraphrase>, ...]}, ...],
     "features": [{"text": <source-supported characteristic>, "source_ids": ["S1", ...]}, ...],
     "assumptions": [<illustrative choice, e.g. a dimension, not taken from a source>, ...],
     "unknowns": [<information the search did not establish>, ...],
     "warnings": [TEXT_EVIDENCE_WARNING, ...]}

``validate_brief(brief, observed_urls=None)`` returns path-qualified problems
(an empty list when the brief passes). It checks the exact field sets, counts,
lengths, text characters, source-ID grammar and uniqueness, feature
references, the public-URL grammar (``url_problem``), the mandatory
``TEXT_EVIDENCE_WARNING`` and the encoded size. With ``observed_urls`` it also
requires every source URL to be one of them. Without it, a URL is only known
to be well formed: the researcher's stream verification is what ties sources to
actually observed search results, so an integrator that re-validates a brief
cannot re-establish that link from the brief alone.

``derived_overview`` builds the researcher's notice-plus-whole-features replacement for a summary that is
valid except for its length; the validator's own limits are unchanged.

``OUTPUT_SCHEMA`` is what the model returns (the researcher adds ``schema``,
``query`` and source titles, and prepends the standard warning);
``cli_output_schema()`` is its conservative subset for the CLI's
``--json-schema`` (limit keywords stripped; the prompt states every limit).
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import unicodedata

__all__ = ["BOUNDS", "BRIEF_FIELDS", "BRIEF_JSON_SCHEMA", "BRIEF_SCHEMA_ID", "FEATURE_FIELDS", "OUTPUT_SCHEMA",
           "SOURCE_FIELDS", "SOURCE_ID_PATTERN", "TEXT_EVIDENCE_WARNING", "cli_output_schema", "derived_overview",
           "is_public_url", "text_problem", "url_problem", "validate_brief"]

BRIEF_SCHEMA_ID = "skid.equipment.research/1"
BRIEF_FIELDS = ("schema", "query", "equipment_type", "summary", "sources", "features", "assumptions", "unknowns",
                "warnings")
SOURCE_FIELDS = ("id", "url", "title", "observations")
FEATURE_FIELDS = ("text", "source_ids")
SOURCE_ID_PATTERN = r"S[1-9][0-9]?"
TEXT_EVIDENCE_WARNING = ("Research used text web-search results only; no images, drawings or CAD files were "
                         "inspected, and any dimensions are illustrative assumptions, not source data.")

MAX_REQUEST_CHARS = 2000
MAX_EQUIPMENT_TYPE_CHARS = 80
MAX_SUMMARY_CHARS = 1500
MAX_SOURCES = 8
MAX_URL_CHARS = 600
MAX_TITLE_CHARS = 200
MAX_OBSERVATIONS = 4
MAX_OBSERVATION_CHARS = 300
MAX_FEATURES = 16
MAX_FEATURE_CHARS = 300
MAX_FEATURE_SOURCES = 4
MAX_ASSUMPTIONS = 12
MAX_UNKNOWNS = 12
MAX_WARNINGS = 8
MAX_MODEL_WARNINGS = MAX_WARNINGS - 1  # the researcher prepends TEXT_EVIDENCE_WARNING
MAX_NOTE_CHARS = 300
MAX_BRIEF_BYTES = 64 << 10
MAX_PROBLEMS = 25

BOUNDS = {"request_chars": MAX_REQUEST_CHARS, "equipment_type_chars": MAX_EQUIPMENT_TYPE_CHARS,
          "summary_chars": MAX_SUMMARY_CHARS, "sources": [1, MAX_SOURCES], "url_chars": MAX_URL_CHARS,
          "title_chars": MAX_TITLE_CHARS, "observations_per_source": [1, MAX_OBSERVATIONS],
          "observation_chars": MAX_OBSERVATION_CHARS, "features": [1, MAX_FEATURES],
          "feature_chars": MAX_FEATURE_CHARS, "sources_per_feature": [1, MAX_FEATURE_SOURCES],
          "assumptions": [0, MAX_ASSUMPTIONS], "unknowns": [0, MAX_UNKNOWNS], "warnings": [1, MAX_WARNINGS],
          "note_chars": MAX_NOTE_CHARS, "brief_bytes": MAX_BRIEF_BYTES}

_SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
# Bidirectional embedding/override/isolate controls and marks: they can visually reorder displayed text.
_BIDI = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
_JOINERS = frozenset("‌‍")
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
_BLOCKED_HOSTS = frozenset(("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"))
_BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home", ".corp",
                     ".private", ".arpa", ".test", ".example", ".invalid", ".onion", ".alt")


def _text(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _texts(maximum_items, maximum_chars, minimum_items=0):
    return {"type": "array", "items": _text(maximum_chars), "minItems": minimum_items, "maxItems": maximum_items}


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_SOURCE_REF = {"type": "string", "pattern": SOURCE_ID_PATTERN}
_FEATURE = _obj({"text": _text(MAX_FEATURE_CHARS),
                 "source_ids": {"type": "array", "items": _SOURCE_REF, "minItems": 1,
                                "maxItems": MAX_FEATURE_SOURCES}})
_OUTPUT_SOURCE = _obj({"id": _SOURCE_REF, "url": _text(MAX_URL_CHARS),
                       "observations": _texts(MAX_OBSERVATIONS, MAX_OBSERVATION_CHARS, 1)})

OUTPUT_SCHEMA = _obj({
    "equipment_type": _text(MAX_EQUIPMENT_TYPE_CHARS),
    "summary": _text(MAX_SUMMARY_CHARS),
    "sources": {"type": "array", "items": _OUTPUT_SOURCE, "minItems": 1, "maxItems": MAX_SOURCES},
    "features": {"type": "array", "items": _FEATURE, "minItems": 1, "maxItems": MAX_FEATURES},
    "assumptions": _texts(MAX_ASSUMPTIONS, MAX_NOTE_CHARS),
    "unknowns": _texts(MAX_UNKNOWNS, MAX_NOTE_CHARS),
    "warnings": _texts(MAX_MODEL_WARNINGS, MAX_NOTE_CHARS)})

BRIEF_JSON_SCHEMA = _obj({
    "schema": {"type": "string", "enum": [BRIEF_SCHEMA_ID]},
    "query": _text(MAX_REQUEST_CHARS),
    "equipment_type": _text(MAX_EQUIPMENT_TYPE_CHARS),
    "summary": _text(MAX_SUMMARY_CHARS),
    "sources": {"type": "array", "minItems": 1, "maxItems": MAX_SOURCES, "items": _obj({
        "id": _SOURCE_REF, "url": _text(MAX_URL_CHARS), "title": _text(MAX_TITLE_CHARS),
        "observations": _texts(MAX_OBSERVATIONS, MAX_OBSERVATION_CHARS, 1)})},
    "features": {"type": "array", "items": _FEATURE, "minItems": 1, "maxItems": MAX_FEATURES},
    "assumptions": _texts(MAX_ASSUMPTIONS, MAX_NOTE_CHARS),
    "unknowns": _texts(MAX_UNKNOWNS, MAX_NOTE_CHARS),
    "warnings": _texts(MAX_WARNINGS, MAX_NOTE_CHARS, 1)})

_LIMIT_KEYWORDS = ("minLength", "maxLength", "pattern", "minItems", "maxItems")


def cli_output_schema() -> dict:
    """``OUTPUT_SCHEMA`` without limit keywords: the conservative subset passed to ``--json-schema``."""

    def strip(node):
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key not in _LIMIT_KEYWORDS}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(copy.deepcopy(OUTPUT_SCHEMA))


def text_problem(value, limit, kind="line"):
    """``None`` for acceptable text, else why not.

    ``kind``: ``line`` (one line, trimmed), ``text`` (newlines allowed, trimmed) or ``query`` (newlines and tabs
    allowed, untrimmed: the exact request text). Control, surrogate, private-use, line/paragraph-separator,
    bidirectional-control and other format characters are refused everywhere (zero-width joiners excepted).
    """
    if not isinstance(value, str):
        return "must be a string"
    if not value.strip():
        return "must not be empty"
    if len(value) > limit:
        return f"is longer than {limit} characters"
    if kind != "query" and value != value.strip():
        return "has leading or trailing whitespace"
    allowed = {"line": "", "text": "\n", "query": "\n\t"}[kind]
    for ch in value:
        if ch in allowed or ch in _JOINERS:
            continue
        if ch in _BIDI or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"):
            return f"contains a control or format character U+{ord(ch):04X}"
    return None


def url_problem(value):
    """``None`` for a public absolute http(s) URL with a DNS host name or global IP literal, else the reason.

    Strict grammar, checked without a lenient parser: ASCII URL characters only (no whitespace, controls,
    backslashes, quotes or angle brackets), well-formed percent escapes, lowercase ``http``/``https`` scheme,
    no credentials, no explicit port, no trailing-dot host. A host name needs at least two LDH labels and an
    alphabetic or ``xn--`` top-level label (so numeric/hex IPv4 spellings are refused); local and special-use
    names (``localhost``, ``.local``, ``.internal``, ``.arpa``, ``.test``, ...) are refused. IP literals must be
    dotted-decimal IPv4 or bracketed IPv6 and globally routable (no private, loopback, link-local, shared,
    reserved, multicast or IPv4-mapped addresses). The URL is never fetched.
    """
    if not isinstance(value, str) or not value:
        return "must be a nonempty string"
    if len(value) > MAX_URL_CHARS:
        return f"is longer than {MAX_URL_CHARS} characters"
    if not _URL_CHARS.fullmatch(value):
        return "contains characters outside the URL grammar (whitespace, control, non-ASCII or unsafe)"
    if _BAD_ESCAPE.search(value):
        return "has a malformed percent escape"
    scheme, separator, rest = value.partition("://")
    if not separator or scheme not in ("http", "https"):
        return "must be an absolute http:// or https:// URL"
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not authority:
        return "has no host"
    if "@" in authority:
        return "must not contain credentials"
    if authority.startswith("["):
        literal, bracket, tail = authority[1:].partition("]")
        if not bracket or tail:
            return "has a malformed IPv6 host or an explicit port"
        try:
            address = ipaddress.IPv6Address(literal)
        except ValueError:
            return "has an invalid IPv6 host"
        if address.ipv4_mapped is not None or not address.is_global or address.is_multicast:
            return "names a non-public IP address"
        return None
    if ":" in authority or "[" in authority or "]" in authority:
        return "must not name an explicit port"
    host = authority.lower()
    if re.fullmatch(r"[0-9.]+", host):
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            return "has an invalid IPv4 host"
        if not address.is_global or address.is_multicast:
            return "names a non-public IP address"
        return None
    if host.endswith("."):
        return "has a trailing-dot host name"
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return "names a local or special-use host"
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return "has an invalid host name"
    if not _TLD.fullmatch(labels[-1]):
        return "has an invalid top-level domain"
    return None


def is_public_url(value) -> bool:
    return url_problem(value) is None


def derived_overview(original_chars, features):
    """``(text, shown)``: a replacement for an over-limit summary, at most ``MAX_SUMMARY_CHARS`` characters.

    Deterministic and built only from a visible notice and the first ``shown`` whole, already validated
    ``features`` (text and source IDs). Nothing of the replaced prose is reused, and no feature is cut: a feature
    that does not fit ends the list. The notice points to the full features, observations, assumptions and
    unknowns, which the caller keeps unchanged.
    """
    total = len(features)
    for shown in range(total, -1, -1):
        lines = [f"Derived overview: the researcher's {original_chars}-character overview exceeded the "
                 f"{MAX_SUMMARY_CHARS}-character limit and is not shown; this is not a shortened copy of it. The "
                 f"first {shown} of {total} sourced features follow in full with their source IDs. All {total} "
                 "sourced features, every source observation, the assumptions and the unknowns are listed in full "
                 "in their own sections."]
        lines += [f"- {feature['text']} [{', '.join(feature['source_ids'])}]" for feature in features[:shown]]
        text = "\n".join(lines)
        if len(text) <= MAX_SUMMARY_CHARS:
            return text, shown
    raise ValueError("the overview notice alone exceeds the summary limit")


def _fields(problems, value, fields, path):
    if not isinstance(value, dict):
        problems.append(f"{path} must be an object")
        return False
    missing = [name for name in fields if name not in value]
    extra = sorted(str(name)[:40] for name in value if name not in fields)
    if missing:
        problems.append(f"{path} is missing {', '.join(missing)}")
    if extra:
        problems.append(f"{path} has unsupported fields {', '.join(extra)}")
    return not missing and not extra


def _check_text(problems, value, path, limit, kind="line"):
    reason = text_problem(value, limit, kind)
    if reason:
        problems.append(f"{path} {reason}")


def _check_list(problems, value, path, minimum, maximum):
    if not isinstance(value, list):
        problems.append(f"{path} must be a list")
        return False
    if not minimum <= len(value) <= maximum:
        problems.append(f"{path} must have {minimum} to {maximum} items, not {len(value)}")
        return False
    return True


def _check_texts(problems, value, path, minimum, maximum, limit):
    if _check_list(problems, value, path, minimum, maximum):
        for index, item in enumerate(value):
            _check_text(problems, item, f"{path}[{index}]", limit)


def validate_brief(brief, observed_urls=None) -> list:
    """Path-qualified problems with a brief; an empty list when it passes. Never raises for bad input.

    ``observed_urls`` (optional collection of strings): every source URL must also be one of them.
    """
    problems = []
    if not _fields(problems, brief, BRIEF_FIELDS, "brief"):
        return problems
    if brief["schema"] != BRIEF_SCHEMA_ID:
        problems.append(f"brief.schema must be {BRIEF_SCHEMA_ID!r}")
    _check_text(problems, brief["query"], "brief.query", MAX_REQUEST_CHARS, "query")
    _check_text(problems, brief["equipment_type"], "brief.equipment_type", MAX_EQUIPMENT_TYPE_CHARS)
    _check_text(problems, brief["summary"], "brief.summary", MAX_SUMMARY_CHARS, "text")
    ids, urls = set(), set()
    observed = None if observed_urls is None else frozenset(u for u in observed_urls if isinstance(u, str))
    if _check_list(problems, brief["sources"], "brief.sources", 1, MAX_SOURCES):
        for index, source in enumerate(brief["sources"]):
            path = f"brief.sources[{index}]"
            if not _fields(problems, source, SOURCE_FIELDS, path):
                continue
            sid = source["id"]
            if not isinstance(sid, str) or not _SOURCE_ID.fullmatch(sid):
                problems.append(f"{path}.id must match {SOURCE_ID_PATTERN}")
            elif sid in ids:
                problems.append(f"{path}.id {sid!r} is not unique")
            else:
                ids.add(sid)
            reason = url_problem(source["url"])
            if reason:
                problems.append(f"{path}.url {reason}")
            elif source["url"] in urls:
                problems.append(f"{path}.url repeats another source's URL")
            else:
                urls.add(source["url"])
                if observed is not None and source["url"] not in observed:
                    problems.append(f"{path}.url was not observed in a successful search result")
            _check_text(problems, source["title"], f"{path}.title", MAX_TITLE_CHARS)
            _check_texts(problems, source["observations"], f"{path}.observations", 1, MAX_OBSERVATIONS,
                         MAX_OBSERVATION_CHARS)
    if _check_list(problems, brief["features"], "brief.features", 1, MAX_FEATURES):
        for index, feature in enumerate(brief["features"]):
            path = f"brief.features[{index}]"
            if not _fields(problems, feature, FEATURE_FIELDS, path):
                continue
            _check_text(problems, feature["text"], f"{path}.text", MAX_FEATURE_CHARS)
            cited = feature["source_ids"]
            if not _check_list(problems, cited, f"{path}.source_ids", 1, MAX_FEATURE_SOURCES):
                continue
            if len({sid for sid in cited if isinstance(sid, str)}) != len(cited):
                problems.append(f"{path}.source_ids must be distinct strings")
            for sid in cited:
                if isinstance(sid, str) and sid not in ids:
                    problems.append(f"{path}.source_ids names an unknown source {sid[:40]!r}")
    _check_texts(problems, brief["assumptions"], "brief.assumptions", 0, MAX_ASSUMPTIONS, MAX_NOTE_CHARS)
    _check_texts(problems, brief["unknowns"], "brief.unknowns", 0, MAX_UNKNOWNS, MAX_NOTE_CHARS)
    _check_texts(problems, brief["warnings"], "brief.warnings", 1, MAX_WARNINGS, MAX_NOTE_CHARS)
    if isinstance(brief["warnings"], list) and TEXT_EVIDENCE_WARNING not in brief["warnings"]:
        problems.append("brief.warnings must include the standard text-evidence warning (TEXT_EVIDENCE_WARNING)")
    if not problems:
        try:
            size = len(json.dumps(brief, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            size = None
        if size is None or size > MAX_BRIEF_BYTES:
            problems.append(f"brief encodes to more than {MAX_BRIEF_BYTES} bytes")
    return problems[:MAX_PROBLEMS]
