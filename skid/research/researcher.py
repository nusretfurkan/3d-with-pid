"""Search-only bridge to the locally installed Claude Code CLI on the user's Claude Max subscription.

``ClaudeCliResearcher.run(request, cancel)`` runs two child processes from an
argument list (never a shell), each in its own process group, in a fresh
private working directory that is deleted afterwards:

1. ``<cli> auth status --json``; the reply must report ``authMethod``
   ``claude.ai`` and ``subscriptionType`` ``max`` (``loggedIn`` true when
   present). Only those fields are read or reported.
2. ``research_argv``: ``<cli> -p --model claude-opus-5-5 --effort medium
   --output-format stream-json --verbose --json-schema <schema> --tools
   WebSearch --allowedTools WebSearch --permission-mode dontAsk
   --strict-mcp-config --setting-sources "" --no-session-persistence``, with
   the prompt (instructions plus the request text as a JSON string) on stdin.

Both children get this process's environment with only ``ANTHROPIC_API_KEY``
removed. No API client, fallback model, MCP server, settings file, session
file, page fetch, file, shell or code tool is involved; the model, effort and
tool are constants. ``ResearchStream`` checks the stream line by line and stops
the child on the first violation. Afterwards the final ``result`` must be a
success whose ``modelUsage`` names the exact model and at most the CLI's
internal WebSearch retrieval model (with web-search requests, disclosed in
metadata), whose ``structured_output`` is the model's last ``StructuredOutput``
call, citing only public URLs observed in successful search results before that
call. The brief is composed (observed titles, standard warning) and validated.
A summary whose only problem is its length (full text otherwise valid, every
other field valid, brief within its byte cap) is replaced by a marked,
derived overview of whole sourced features and revalidated; the replacement
is disclosed in ``metadata.overview_replaced``. The finished reply must pass the published ``validate_research_result``;
anything else raises ``ResearchError`` and nothing partial is returned.
``shutdown()`` terminates every child it owns and refuses later runs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass

from .brief import (BRIEF_FIELDS, BRIEF_SCHEMA_ID, MAX_BRIEF_BYTES, MAX_REQUEST_CHARS, MAX_SUMMARY_CHARS,
                    OUTPUT_SCHEMA, TEXT_EVIDENCE_WARNING, cli_output_schema, derived_overview, text_problem,
                    url_problem, validate_brief)
from .process import ProcessOwner, ResearchError, clip, strict_json
from .prompt import research_prompt
from .stream import (EFFORT, MAX_LINKS_PER_RESULT, MAX_OBSERVED_URLS, MAX_SEARCH_CALLS, MAX_STRUCTURED_CALLS, MODEL,
                     RESEARCH_TOOLS, RETRIEVAL_MODEL, SEARCH_BUDGET, SEARCH_STATUSES, SEARCH_TOOL, ResearchStream)

__all__ = ["DEFAULT_CLI", "RETRIEVAL_ROLE", "VERIFIED_METADATA_FIELDS", "ClaudeCliResearcher", "ResearchConfig",
           "compose_brief", "research_argv", "validate_research_result"]

# ``claude`` as found on PATH when this module is imported (an absolute path), else the bare name, which the
# executable check in ``run`` reports as unavailable (the same strategy as ``skid.planning.bridge``).
DEFAULT_CLI = shutil.which("claude") or "claude"
REQUIRED_AUTH = (("authMethod", "claude.ai"), ("subscriptionType", "max"))
METADATA_AUTH = {"method": "claude.ai", "subscription": "max"}  # REQUIRED_AUTH as recorded in metadata.auth
DEFAULT_TIMEOUT_S = 480.0
AUTH_TIMEOUT_S = 30.0
TERMINATE_GRACE_S = 3.0
MAX_PROMPT_BYTES = 64 << 10
MAX_STDOUT_BYTES = 16 << 20
MAX_AUTH_BYTES = 64 << 10
MAX_OUTPUT_BYTES = 64 << 10
OUTPUT_FIELDS = tuple(OUTPUT_SCHEMA["properties"])
OUTPUT_SOURCE_FIELDS = ("id", "url", "observations")
RETRIEVAL_ROLE = "CLI-internal WebSearch retrieval reported in result modelUsage; not the research assistant"
RETRIEVAL_FIELDS = frozenset(("model", "web_search_requests", "role", "usage"))
RETRIEVAL_USAGE_FIELDS = frozenset(("inputTokens", "outputTokens"))
# Metadata keys validate_research_result requires of a verified reply (other keys are informational, not read).
VERIFIED_METADATA_FIELDS = ("verified", "model", "effort", "init_model", "response_models", "usage_models",
                            "internal_retrieval", "auth", "api_key_source", "tools", "mcp_servers", "search_calls",
                            "successful_searches", "failed_searches", "searches", "structured_output_calls",
                            "observed_url_count", "observed_urls", "verified_sources", "source_urls",
                            "request_sha256", "prompt_sha256", "output_sha256", "brief_sha256", "image_inspection",
                            "session_persistence")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# validate_brief's exact problem for a summary over the limit, taken from the validator itself.
_SUMMARY_TOO_LONG = "brief.summary " + text_problem("x" * (MAX_SUMMARY_CHARS + 1), MAX_SUMMARY_CHARS, "text")


@dataclass(frozen=True)
class ResearchConfig:
    cli_path: str = DEFAULT_CLI
    timeout_s: float = DEFAULT_TIMEOUT_S
    auth_timeout_s: float = AUTH_TIMEOUT_S
    grace_s: float = TERMINATE_GRACE_S
    max_stdout_bytes: int = MAX_STDOUT_BYTES

    def __post_init__(self):
        if not isinstance(self.cli_path, str) or not self.cli_path:
            raise ValueError("cli_path must be a nonempty string")
        for name, low, high in (("timeout_s", 0.1, 3600.0), ("auth_timeout_s", 0.1, 600.0), ("grace_s", 0.0, 60.0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not low <= value <= high:
                raise ValueError(f"{name} must be a number from {low:g} to {high:g}")
        value = self.max_stdout_bytes
        if isinstance(value, bool) or not isinstance(value, int) or not 64 << 10 <= value <= 64 << 20:
            raise ValueError("max_stdout_bytes must be an integer from 64 KiB to 64 MiB")


def research_argv(cli_path) -> list:
    return [cli_path, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(cli_output_schema(), separators=(",", ":"), ensure_ascii=False),
            "--tools", SEARCH_TOOL, "--allowedTools", SEARCH_TOOL, "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha256(data) -> str:
    return hashlib.sha256(data).hexdigest()


def _line(value):
    """One trimmed line: every whitespace run (newline, tab, separators) becomes one space."""
    return " ".join(value.split())


def _paragraphs(value):
    """Trimmed text keeping single line breaks between nonblank lines."""
    lines = (_line(part) for part in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(part for part in lines if part)


def _url_key(url):
    """Scheme and host case-folded, fragment removed: spelling differences reconciled to the observed URL."""
    scheme, _, rest = url.partition("://")
    cut = min([index for index in (rest.find("/"), rest.find("?"), rest.find("#")) if index >= 0] or [len(rest)])
    return f"{scheme.lower()}://{rest[:cut].lower()}{rest[cut:].split('#', 1)[0]}"


def _shape_problems(output):
    """Exact field sets and JSON types of the model output; lengths and characters are checked after composing."""
    if not isinstance(output, dict):
        return ["output must be an object"]
    problems = []
    if set(output) != set(OUTPUT_FIELDS):
        problems.append(f"output must have exactly the fields {', '.join(OUTPUT_FIELDS)}")
        return problems
    for name in ("equipment_type", "summary"):
        if not isinstance(output[name], str):
            problems.append(f"output.{name} must be a string")
    for name in ("assumptions", "unknowns", "warnings"):
        if not isinstance(output[name], list) or not all(isinstance(item, str) for item in output[name]):
            problems.append(f"output.{name} must be a list of strings")
    sources = output["sources"]
    if not isinstance(sources, list):
        problems.append("output.sources must be a list")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict) or set(source) != set(OUTPUT_SOURCE_FIELDS):
                problems.append(f"output.sources[{index}] must have exactly the fields id, url, observations")
            elif not isinstance(source["id"], str) or not isinstance(source["url"], str) or \
                    not isinstance(source["observations"], list) or \
                    not all(isinstance(item, str) for item in source["observations"]):
                problems.append(f"output.sources[{index}] has a field of the wrong type")
    features = output["features"]
    if not isinstance(features, list):
        problems.append("output.features must be a list")
    else:
        for index, feature in enumerate(features):
            if not isinstance(feature, dict) or set(feature) != {"text", "source_ids"} or \
                    not isinstance(feature["text"], str) or not isinstance(feature["source_ids"], list):
                problems.append(f"output.features[{index}] must be {{text: string, source_ids: list}}")
    return problems


def compose_brief(request, output, evidence):
    """``(brief, unverified_urls, problems)`` from the model output and the observed ``{url: title}`` evidence.

    Source URLs are replaced by their observed spelling and titles by the observed (sanitized) titles; text is
    whitespace-normalized; empty list items are dropped; ``TEXT_EVIDENCE_WARNING`` is put first. The caller
    still runs ``validate_brief`` on the result.
    """
    problems = _shape_problems(output)
    if problems:
        return None, [], problems
    keys = {}
    for url in evidence:
        keys.setdefault(_url_key(url), url)
    sources, unverified = [], []
    for source in output["sources"]:
        url = source["url"].strip()
        observed = url if url in evidence else keys.get(_url_key(url))
        if observed is None:
            unverified.append(url)
            continue
        sources.append({"id": source["id"], "url": observed, "title": evidence[observed],
                        "observations": [text for text in map(_line, source["observations"]) if text]})
    if unverified:
        return None, unverified, []
    warnings = [text for text in map(_line, output["warnings"]) if text and text != TEXT_EVIDENCE_WARNING]
    brief = {"schema": BRIEF_SCHEMA_ID, "query": request, "equipment_type": _line(output["equipment_type"]),
             "summary": _paragraphs(output["summary"]), "sources": sources,
             "features": [{"text": _line(feature["text"]), "source_ids": list(feature["source_ids"])}
                          for feature in output["features"]],
             "assumptions": [text for text in map(_line, output["assumptions"]) if text],
             "unknowns": [text for text in map(_line, output["unknowns"]) if text],
             "warnings": [TEXT_EVIDENCE_WARNING, *warnings]}
    return {name: brief[name] for name in BRIEF_FIELDS}, [], []


def _bound_overview(brief, evidence, problems):
    """``(brief, problems, replacement)`` after ``validate_brief(brief, evidence)`` reported ``problems``.

    Only when the summary's length is the sole problem, the full summary otherwise passes the text rules
    (trimming, control/format/bidi/surrogate characters) and the composed brief is within the encoded-size cap,
    the summary is replaced by ``derived_overview`` and the result is validated again strictly against the same
    observed evidence. Every other field stays exactly as composed. Anything else returns the problems
    (extended with what the full summary or the size check found) and no replacement.
    """
    if problems != [_SUMMARY_TOO_LONG]:
        return brief, problems, None
    summary = brief["summary"]
    reason = text_problem(summary, len(summary), "text")  # the whole text, not only the length
    if reason:
        return brief, problems + [f"brief.summary {reason}"], None
    try:
        size = len(json.dumps(brief, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        size = None
    if size is None or size > MAX_BRIEF_BYTES:
        return brief, problems + [f"brief encodes to more than {MAX_BRIEF_BYTES} bytes"], None
    overview, shown = derived_overview(len(summary), brief["features"])
    bounded = dict(brief, summary=overview)
    checked = validate_brief(bounded, observed_urls=evidence)
    if checked:
        return brief, problems + checked, None
    return bounded, [], {"reason": "summary_over_limit", "original_chars": len(summary),
                         "limit": MAX_SUMMARY_CHARS, "features_shown": shown,
                         "features_total": len(brief["features"])}


def _count(value, low=0, high=None):
    """A JSON integer from ``low`` to ``high``; a bool is never a count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)


def _text_sha256(text):
    try:
        return _sha256(text.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return None


def _brief_sha256(brief):
    try:
        return _sha256(_canonical(brief))
    except (TypeError, ValueError, RecursionError):
        return None


def _search_record_ok(record):
    """One ``metadata.searches`` item as the stream records it: links >= 1 exactly when the status is ``ok``."""
    return (isinstance(record, dict) and set(record) == {"query", "status", "links"}
            and (record["query"] is None or isinstance(record["query"], str))
            and isinstance(record["status"], str) and record["status"] in SEARCH_STATUSES
            and _count(record["links"], 0, MAX_LINKS_PER_RESULT)
            and (record["status"] == "ok") == (record["links"] > 0))


def _retrieval_ok(retrieval):
    """The one disclosed exception: the CLI's own WebSearch retrieval model, with search requests, nothing else."""
    if not isinstance(retrieval, dict) or set(retrieval) != RETRIEVAL_FIELDS:
        return False
    usage = retrieval["usage"]
    return (retrieval["model"] == RETRIEVAL_MODEL and retrieval["role"] == RETRIEVAL_ROLE
            and _count(retrieval["web_search_requests"], 1) and isinstance(usage, dict)
            and set(usage) <= RETRIEVAL_USAGE_FIELDS and all(_count(value) for value in usage.values()))


def _metadata_problems(brief, metadata, request):
    """The verified-run metadata invariants, cross-checked against the brief and the request."""
    problems = []
    get = metadata.get
    missing = [name for name in VERIFIED_METADATA_FIELDS if name not in metadata]
    if missing:
        problems.append("metadata is missing " + ", ".join(missing))
    if get("verified") is not True:
        problems.append("metadata.verified must be true")
    if get("model") != MODEL or get("init_model") != MODEL or get("response_models") != [MODEL]:
        problems.append(f"metadata must report only the exact assistant model {MODEL} (model, init_model and "
                        "response_models)")
    effort = get("effort")
    if not isinstance(effort, dict) or set(effort) != {"requested", "reported"} or effort["requested"] != EFFORT \
            or effort["reported"] not in (None, EFFORT):
        problems.append(f"metadata.effort must be {{requested: {EFFORT}, reported: null or {EFFORT}}}")
    if get("auth") != METADATA_AUTH:
        problems.append("metadata.auth must be exactly {method: claude.ai, subscription: max} (the Claude Max "
                        "subscription login)")
    if get("api_key_source") != "none":
        problems.append("metadata.api_key_source must be none (no API-key billing)")
    if get("tools") != sorted(RESEARCH_TOOLS):
        problems.append(f"metadata.tools must be exactly {sorted(RESEARCH_TOOLS)} (no shell, file, fetch or agent "
                        "tools)")
    if get("mcp_servers") != []:
        problems.append("metadata.mcp_servers must be empty")
    retrieval = get("internal_retrieval")
    if retrieval is not None and not _retrieval_ok(retrieval):
        problems.append(f"metadata.internal_retrieval may only disclose {RETRIEVAL_MODEL} as the CLI-internal "
                        "WebSearch retrieval: exactly model, web_search_requests >= 1, the standard role and token "
                        "usage")
    usage_models = [MODEL] if retrieval is None else sorted((MODEL, RETRIEVAL_MODEL))
    if get("usage_models") != usage_models:
        problems.append(f"metadata.usage_models must be exactly {usage_models}, matching internal_retrieval")
    calls, ok, failed = get("search_calls"), get("successful_searches"), get("failed_searches")
    searches, linked = get("searches"), None
    if not (_count(calls, 1, MAX_SEARCH_CALLS) and _count(ok, 1) and _count(failed) and ok + failed <= calls):
        problems.append(f"metadata.successful_searches must be at least 1, and search_calls, successful_searches "
                        f"and failed_searches integers (not booleans) with successful + failed <= search_calls <= "
                        f"{MAX_SEARCH_CALLS}")
    elif not (isinstance(searches, list) and len(searches) == calls and all(map(_search_record_ok, searches))
              and sum(item["status"] == "ok" for item in searches) == ok
              and sum(item["status"] not in ("ok", "unanswered") for item in searches) == failed):
        problems.append("metadata.searches must hold one {query, status, links} record per search call, "
                        "consistent with the search counts")
    else:
        linked = sum(item["links"] for item in searches)
    if not _count(get("structured_output_calls"), 1, MAX_STRUCTURED_CALLS):
        problems.append(f"metadata.structured_output_calls must be an integer from 1 to {MAX_STRUCTURED_CALLS}")
    observed, count = get("observed_urls"), get("observed_url_count")
    if isinstance(observed, list) and not (_count(count) and count == len(observed)
                                           and (linked is None or count <= linked)):
        problems.append("metadata.observed_url_count must equal the number of observed_urls and not exceed the "
                        "links of the successful searches")
    sources = brief.get("sources") if isinstance(brief, dict) else None
    if isinstance(sources, list):
        if not (_count(get("verified_sources"), 1) and get("verified_sources") == len(sources)):
            problems.append("metadata.verified_sources does not match the brief")
        if get("source_urls") != [source.get("url") if isinstance(source, dict) else None for source in sources]:
            problems.append("metadata.source_urls must list the brief's source URLs in order")
    for name in ("request_sha256", "prompt_sha256", "output_sha256", "brief_sha256"):
        if not isinstance(get(name), str) or not _SHA256_HEX.fullmatch(get(name)):
            problems.append(f"metadata.{name} must be a lowercase hex SHA-256 digest")
    query = brief.get("query") if isinstance(brief, dict) else None
    digest = get("request_sha256")
    if isinstance(digest, str) and _SHA256_HEX.fullmatch(digest) \
            and digest != _text_sha256(request if request is not None else query):
        problems.append("metadata.request_sha256 is not the SHA-256 of the request text")
    digest = get("brief_sha256")
    if isinstance(digest, str) and _SHA256_HEX.fullmatch(digest) and digest != _brief_sha256(brief):
        problems.append("metadata.brief_sha256 is not the SHA-256 of the canonical brief")
    if get("image_inspection") is not False:
        problems.append("metadata.image_inspection must be false (text search evidence only)")
    if get("session_persistence") is not False:
        problems.append("metadata.session_persistence must be false")
    return problems


def validate_research_result(reply, request=None) -> list:
    """Problems with a researcher reply ``{brief, metadata}`` (e.g. from an injected FAKE); ``[]`` when it passes.

    Checks the brief against ``metadata.observed_urls`` and every ``VERIFIED_METADATA_FIELDS`` invariant of a
    verified run: exact model/effort/init model/response models, Max subscription auth, API-key source ``none``,
    exactly the search and structured-output tools, no MCP servers, usage models consistent with the one
    disclosed internal retrieval exception, integer (never bool) search counts consistent with the search
    records, observed/source URL lists consistent with the brief, and the request/brief SHA-256 digests
    recomputed from ``request`` (else ``brief.query``) and the canonical brief. ``prompt_sha256`` and
    ``output_sha256`` are checked for form only (the prompt is versioned with the code, the raw model output is
    not in the reply). Other metadata keys are informational. Never raises for bad input.

    Passing proves self-consistency only, not that a search happened: only ``ClaudeCliResearcher.run``'s
    stream verification ties sources to observed search results.
    """
    if not isinstance(reply, dict) or set(reply) != {"brief", "metadata"}:
        return ["the reply must be an object with exactly brief and metadata"]
    brief, metadata = reply["brief"], reply["metadata"]
    if not isinstance(metadata, dict):
        return ["reply.metadata must be an object"]
    problems = []
    observed = metadata.get("observed_urls")
    if not isinstance(observed, list) or not all(isinstance(url, str) for url in observed):
        problems.append("metadata.observed_urls must be a list of URL strings")
        observed = None
    elif len(observed) > MAX_OBSERVED_URLS or len(set(observed)) != len(observed) \
            or any(url_problem(url) is not None for url in observed):
        problems.append(f"metadata.observed_urls must be at most {MAX_OBSERVED_URLS} distinct public URLs")
    problems += validate_brief(brief, observed_urls=observed)
    if request is not None and isinstance(brief, dict) and brief.get("query") != request:
        problems.append("brief.query is not the request text")
    problems += _metadata_problems(brief, metadata, request)
    return problems[:25]


class ClaudeCliResearcher:
    """Runs one exact-model, search-only research per call; owns and cleans up its children."""

    kind = "claude-code-cli-research"

    def __init__(self, config=None):
        self.config = config or ResearchConfig()
        self._owner = ProcessOwner(self.config.grace_s)

    def describe(self) -> dict:
        """Static configuration for status displays; starts no process."""
        cli = self.config.cli_path
        return {"kind": self.kind, "model": MODEL, "effort": EFFORT, "cli": cli,
                "cli_present": os.path.isfile(cli) and os.access(cli, os.X_OK), "timeout_s": self.config.timeout_s,
                "tools": [SEARCH_TOOL], "structured_output": True, "search_budget": SEARCH_BUDGET,
                "max_search_calls": MAX_SEARCH_CALLS, "internal_retrieval_model": RETRIEVAL_MODEL,
                "schema": BRIEF_SCHEMA_ID, "session_persistence": False, "api_key_env_removed": True,
                "image_inspection": False}

    def shutdown(self):
        """Stop every running child (``SIGTERM`` to its group, ``SIGKILL`` after the grace period)."""
        self._owner.shutdown()

    def run(self, request, cancel=None) -> dict:
        """Return ``{"brief": dict, "metadata": dict}``; raise ``ResearchError`` for anything unverified."""
        started = time.monotonic()
        cli = self.config.cli_path
        metadata = {"kind": self.kind, "cli": cli, "model": MODEL, "effort": {"requested": EFFORT, "reported": None},
                    "verified": False, "started_at": _now()}
        reason = text_problem(request, MAX_REQUEST_CHARS, "query")
        if reason:
            raise ResearchError("research_input_invalid", f"the research request {reason}", metadata)
        data = research_prompt(request).encode("utf-8")
        if len(data) > MAX_PROMPT_BYTES:
            raise ResearchError("research_input_invalid", f"the research prompt is {len(data)} bytes; the limit is "
                                f"{MAX_PROMPT_BYTES}", metadata)
        metadata.update(request_sha256=_sha256(request.encode("utf-8")), prompt_sha256=_sha256(data))
        if self._owner.closed:
            raise ResearchError("research_cancelled", "the service is shutting down; no research was started",
                                metadata)
        if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
            raise ResearchError("research_unavailable", f"the Claude Code CLI executable {clip(cli, 200)} is not "
                                "installed or not executable; nothing is downloaded or installed at runtime", metadata)
        workdir = tempfile.mkdtemp(prefix="skid-research-")
        try:
            self._check_auth(workdir, cancel, metadata)
            stream = ResearchStream()
            child = self._owner.execute(research_argv(cli), data, self.config.timeout_s, cancel, workdir,
                                        stream.feed, self.config.max_stdout_bytes, metadata)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            metadata.update(elapsed_s=round(time.monotonic() - started, 3), finished_at=_now())
        return self._verify(request, child, stream, metadata)

    def _failure(self, child, timeout, metadata):
        reason = child.reason
        if isinstance(reason, tuple):
            return ResearchError(reason[0], reason[1], metadata)
        if reason == "timeout":
            return ResearchError("research_timeout", f"the research did not finish within {timeout:g} s and was "
                                 "terminated", metadata)
        if reason in ("cancelled", "shutdown"):
            why = "the service is shutting down" if reason == "shutdown" else "the research was cancelled"
            return ResearchError("research_cancelled", f"{why}; the research process was terminated", metadata)
        return None

    def _check_auth(self, cwd, cancel, metadata):
        cli = self.config.cli_path
        chunks = []

        def collect(line):
            chunks.append(line)

        child = self._owner.execute([cli, "auth", "status", "--json"], b"", self.config.auth_timeout_s, cancel, cwd,
                                    collect, MAX_AUTH_BYTES, metadata)
        failure = self._failure(child, self.config.auth_timeout_s, metadata)
        if failure is not None:
            if failure.code == "research_timeout":
                failure.code = "research_auth"
                failure.message = (f"the CLI auth status check did not finish within "
                                   f"{self.config.auth_timeout_s:g} s")
            raise failure
        if child.process.returncode != 0:
            tail = child.stderr_tail(200)
            raise ResearchError("research_auth", f"the CLI auth status check exited with status "
                                f"{child.process.returncode}{': ' + tail if tail else ''}", metadata)
        try:
            status = strict_json(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ResearchError("research_auth", "the CLI auth status reply is not a JSON object", metadata) from None
        if not isinstance(status, dict):
            raise ResearchError("research_auth", "the CLI auth status reply is not a JSON object", metadata)
        found = {key: status.get(key) for key, _ in REQUIRED_AUTH}
        metadata["auth"] = {"method": clip(found["authMethod"], 40) if found["authMethod"] is not None else None,
                            "subscription": clip(found["subscriptionType"], 40)
                            if found["subscriptionType"] is not None else None}
        wrong = [f"{key} is {clip(found[key], 40)!r}, not {wanted!r}" for key, wanted in REQUIRED_AUTH
                 if found[key] != wanted]
        if "loggedIn" in status and status["loggedIn"] is not True:
            wrong.insert(0, "the CLI is not logged in")
        if wrong:
            raise ResearchError("research_auth", "the Claude Code CLI is not using the Claude Max subscription "
                                "login: " + "; ".join(wrong), metadata)

    @staticmethod
    def _record_stream(stream, metadata):
        init = stream.init
        if init is not None:
            tools = init.get("tools") if isinstance(init.get("tools"), list) else []
            metadata.update(cli_version=clip(init.get("claude_code_version"), 40), init_model=clip(init.get("model"), 60),
                            api_key_source=clip(init.get("apiKeySource"), 40),
                            tools=sorted(clip(tool, 40) for tool in tools[:8]), mcp_servers=[],
                            permission_mode=clip(init.get("permissionMode"), 40))
            for key in ("effort", "effort_level", "effortLevel"):
                if isinstance(init.get(key), str):
                    metadata["effort"]["reported"] = clip(init[key], 20)
        metadata.update(response_models=sorted(clip(model, 60) for model in stream.response_models),
                        search_calls=stream.search_calls, successful_searches=stream.successful_searches,
                        failed_searches=stream.failed_searches, searches=[dict(item) for item in stream.searches],
                        observed_url_count=len(stream.evidence), observed_urls=list(stream.evidence),
                        structured_output_calls=stream.structured_calls)

    def _check_usage(self, result, stream, metadata):
        usage = result.get("modelUsage")
        if not isinstance(usage, dict) or MODEL not in usage:
            raise ResearchError("research_model_mismatch", f"the CLI result does not report usage of {MODEL}",
                                metadata)
        metadata["usage_models"] = sorted(clip(name, 60) for name in list(usage)[:6])
        other = sorted(set(usage) - {MODEL, RETRIEVAL_MODEL})
        if other:
            raise ResearchError("research_model_mismatch", f"the CLI reports usage of other models {clip(other, 120)}",
                                metadata)
        for name, stats in usage.items():
            if not isinstance(stats, dict) or stats.get("canonicalModel", name) != name:
                raise ResearchError("research_model_mismatch", f"the CLI usage entry for {name} is malformed or names "
                                    "another canonical model", metadata)
            if stats.get("webFetchRequests") not in (None, 0):
                raise ResearchError("research_tool_use", "the CLI reports web fetch requests; only search is allowed",
                                    metadata)
        totals = result.get("usage")
        server = totals.get("server_tool_use") if isinstance(totals, dict) else None
        if isinstance(server, dict) and server.get("web_fetch_requests") not in (None, 0):
            raise ResearchError("research_tool_use", "the CLI reports web fetch requests; only search is allowed",
                                metadata)
        retrieval = None
        if RETRIEVAL_MODEL in usage:
            stats = usage[RETRIEVAL_MODEL]
            requests = stats.get("webSearchRequests")
            if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1 or stream.search_calls < 1:
                raise ResearchError("research_model_mismatch", f"the CLI reports usage of {RETRIEVAL_MODEL} without "
                                    "observed WebSearch retrieval; only that internal retrieval use is accepted",
                                    metadata)
            retrieval = {"model": RETRIEVAL_MODEL, "web_search_requests": requests, "role": RETRIEVAL_ROLE,
                         "usage": {key: stats[key] for key in ("inputTokens", "outputTokens")
                                   if isinstance(stats.get(key), int) and not isinstance(stats.get(key), bool)}}
        metadata["internal_retrieval"] = retrieval
        return usage

    def _verify(self, request, child, stream, metadata):
        self._record_stream(stream, metadata)
        failure = self._failure(child, self.config.timeout_s, metadata)
        if failure is not None:
            raise failure
        result = stream.result
        if isinstance(result, dict) and (result.get("is_error") is not False or result.get("subtype") != "success"):
            raise ResearchError("research_error_result", f"the CLI reported {clip(result.get('subtype'), 60)!r} "
                                f"(is_error {clip(result.get('is_error'), 20)}, API status "
                                f"{clip(result.get('api_error_status'), 20)})", metadata)
        if child.process.returncode != 0:
            tail = child.stderr_tail()
            raise ResearchError("research_exit_status", f"the CLI exited with status {child.process.returncode}"
                                f"{': ' + tail if tail else ''}", metadata)
        if stream.init is None:
            raise ResearchError("research_malformed_output", "the CLI output has no init event, so the session model, "
                                "tools and API-key source are unverified", metadata)
        if stream.assistant_messages == 0:
            raise ResearchError("research_malformed_output", "the CLI output has no assistant message, so the response "
                                "model is unverified", metadata)
        if metadata["effort"]["reported"] not in (None, EFFORT):
            raise ResearchError("research_model_mismatch", f"the CLI session reports effort "
                                f"{metadata['effort']['reported']!r}, not {EFFORT}", metadata)
        if result is None:
            raise ResearchError("research_malformed_output", "the CLI output ended without a final result (truncated)",
                                metadata)
        usage = self._check_usage(result, stream, metadata)
        denials = result.get("permission_denials")
        metadata["permission_denials"] = len(denials) if isinstance(denials, list) else None
        output = result.get("structured_output")
        if not isinstance(output, dict):
            raise ResearchError("research_output_missing", "the CLI result has no structured_output object matching "
                                "the research schema", metadata)
        try:
            encoded = _canonical(output)
        except (UnicodeEncodeError, ValueError):
            raise ResearchError("research_malformed_output", "the structured research output is not encodable "
                                "UTF-8 JSON text (for example, it contains a lone surrogate)", metadata) from None
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ResearchError("research_output_limit", f"the structured research output is {len(encoded)} bytes; "
                                f"the limit is {MAX_OUTPUT_BYTES}", metadata)
        metadata["output_sha256"] = _sha256(encoded)
        call = stream.output_call
        if call is None or call["error"] or not isinstance(call["input"], dict) or _canonical(call["input"]) != encoded:
            raise ResearchError("research_malformed_output", "the final structured output is not the input of the "
                                "model's last accepted StructuredOutput call", metadata)
        evidence = call["evidence"]
        metadata.update(observed_url_count=len(evidence), observed_urls=list(evidence))
        if call["successful_searches"] < 1 or not evidence:
            raise ResearchError("research_no_evidence", f"no successful WebSearch result with a public source URL was "
                                f"observed before the final output ({stream.search_calls} search calls, "
                                f"{stream.failed_searches} failed or unrecognized); research is unavailable", metadata)
        brief, unverified, problems = compose_brief(request, output, evidence)
        if unverified:
            raise ResearchError("research_unverified_source", f"{len(unverified)} cited source URL(s) were not observed "
                                f"in any successful search result before the output: "
                                f"{clip(', '.join(unverified[:3]), 300)}", metadata)
        if not problems:
            problems = validate_brief(brief, observed_urls=evidence)
            if problems:
                brief, problems, replacement = _bound_overview(brief, evidence, problems)
                if replacement is not None:
                    metadata["overview_replaced"] = replacement
        if problems:
            raise ResearchError("research_invalid_brief", "the research brief is invalid: " +
                                clip("; ".join(problems[:5]), 600), metadata)
        stats = usage.get(MODEL, {})
        metadata.update(
            verified=True, verified_sources=len(brief["sources"]),
            source_urls=[source["url"] for source in brief["sources"]],
            brief_sha256=_sha256(_canonical(brief)), session_persistence=False, image_inspection=False,
            duration_ms=result.get("duration_ms") if isinstance(result.get("duration_ms"), int) else None,
            num_turns=result.get("num_turns") if isinstance(result.get("num_turns"), int) else None,
            usage={key: stats[key] for key in ("inputTokens", "outputTokens", "thinkingTokens")
                   if isinstance(stats.get(key), int) and not isinstance(stats.get(key), bool)})
        reply = {"brief": brief, "metadata": metadata}
        problems = validate_research_result(reply, request)  # never return what the published check refuses
        if problems:
            metadata["verified"] = False
            raise ResearchError("research_malformed_output", "the research reply fails validate_research_result: " +
                                clip("; ".join(problems[:3]), 600), metadata)
        return reply
