"""Incremental verification of the research CLI's stream-json events and of the WebSearch evidence they carry.

Format (recorded from the real CLI, 2.1.300, October 16, 2026): the model calls
``WebSearch`` in an assistant ``tool_use`` block (``input.query``); the CLI
answers in a ``user`` event whose ``message.content`` holds a ``tool_result``
block with the same ``tool_use_id`` and a string ``content``::

    Web search results for query: "<query>"\\n\\nLinks: [{"title":...,"url":...},...]\\n\\n<summary text>...

``parse_search_result`` accepts exactly that shape for the call's own query and
reads only the leading ``Links`` array with a strict JSON decoder; URLs or
``Links:`` lines anywhere in the summary text are ignored. ``ResearchStream``
matches every tool result to an observed, unanswered call, records failed or
unrecognized searches without evidence, keeps the ordered set of public URLs
observed so far, and snapshots it when the model makes its final
``StructuredOutput`` call, so later results cannot justify that output.
"""

from __future__ import annotations

import unicodedata

from .brief import MAX_TITLE_CHARS, url_problem
from .process import STRICT_DECODER, clip, strict_json

__all__ = ["MAX_SEARCH_CALLS", "MAX_STRUCTURED_CALLS", "MODEL", "RESEARCH_TOOLS", "RETRIEVAL_MODEL", "SEARCH_BUDGET",
           "SEARCH_STATUSES", "ResearchStream", "parse_search_result", "sanitize_title"]

MODEL = "claude-opus-5-5"
EFFORT = "medium"
RETRIEVAL_MODEL = "claude-haiku-5-5"  # the CLI's internal WebSearch retrieval, reported only in result.modelUsage
SEARCH_TOOL = "WebSearch"
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"  # the CLI's --json-schema return channel; no side effects
RESEARCH_TOOLS = (STRUCTURED_OUTPUT_TOOL, SEARCH_TOOL)
ALLOWED_BLOCKS = ("text", "thinking", "redacted_thinking", "tool_use")
SEARCH_HEADER = "Web search results for query: \""
LINKS_MARKER = "\"\n\nLinks: "
SEARCH_BUDGET = 5  # stated in the prompt
MAX_SEARCH_CALLS = 8  # enforced: one more WebSearch call stops the run
MAX_STRUCTURED_CALLS = 3
MAX_QUERY_CHARS = 400
MAX_SEARCH_RESULT_CHARS = 64 << 10
MAX_LINKS_PER_RESULT = 20
MAX_OBSERVED_URLS = 96
# A search record's status: ``ok`` is the only evidence; ``error`` is an is_error result, ``unanswered`` none yet.
SEARCH_STATUSES = ("ok", "no_links", "no_public_links", "not_text", "oversized", "unrecognized", "error", "unanswered")
_STRIP_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")


def sanitize_title(title, url):
    """An observed (untrusted) title as one clean line of at most ``MAX_TITLE_CHARS``; the host when empty."""
    text = "".join(" " if ch.isspace() else ch for ch in title if ch.isspace()
                   or unicodedata.category(ch) not in _STRIP_CATEGORIES)
    text = " ".join(text.split())
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    if not text:
        text = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return text


def parse_search_result(content, query):
    """``(status, links, dropped)`` for one WebSearch result body; ``links`` are ``(url, title)`` pairs.

    Status ``ok`` (at least one public link), ``no_links``, ``no_public_links``, ``not_text``, ``oversized`` or
    ``unrecognized`` (any other shape, including a header for a different query). Only ``ok`` is evidence.
    """
    if not isinstance(content, str):
        return "not_text", [], 0
    if len(content) > MAX_SEARCH_RESULT_CHARS:
        return "oversized", [], 0
    prefix = SEARCH_HEADER + query + LINKS_MARKER if isinstance(query, str) else None
    if prefix is None or not content.startswith(prefix):
        return "unrecognized", [], 0
    try:
        links, end = STRICT_DECODER.raw_decode(content, len(prefix))
    except (ValueError, RecursionError):
        return "unrecognized", [], 0
    if end < len(content) and content[end] != "\n":
        return "unrecognized", [], 0
    if not isinstance(links, list) or len(links) > MAX_LINKS_PER_RESULT:
        return "unrecognized", [], 0
    if not links:
        return "no_links", [], 0
    if not all(isinstance(item, dict) and isinstance(item.get("url"), str) and isinstance(item.get("title"), str)
               for item in links):
        return "unrecognized", [], 0
    found = [(item["url"], sanitize_title(item["title"], item["url"])) for item in links
             if url_problem(item["url"]) is None]
    return ("ok" if found else "no_public_links"), found, len(links) - len(found)


class ResearchStream:
    """Checks one research run's events as they arrive; ``feed`` returns ``(code, message)`` for a violation."""

    def __init__(self):
        self.init = None
        self.result = None
        self.assistant_messages = 0
        self.response_models = set()
        self.calls = {}
        self.searches = []
        self.search_calls = 0
        self.structured_calls = 0
        self.successful_searches = 0
        self.failed_searches = 0
        self.evidence = {}  # public URL -> sanitized title, in first-observed order
        self.unrecorded_urls = 0
        self.output_call = None

    def feed(self, line):
        try:
            text = line.decode("utf-8").strip()
        except UnicodeDecodeError:
            return "research_malformed_output", "the CLI wrote a line that is not UTF-8"
        if not text:
            return None
        try:
            event = strict_json(text)
        except ValueError as exc:
            return "research_malformed_output", f"the CLI wrote a line that is not strict JSON ({clip(exc, 120)})"
        if not isinstance(event, dict):
            return "research_malformed_output", "a stream event is not an object"
        if self.result is not None:
            return "research_malformed_output", "the CLI wrote events after its final result"
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            if self.init is not None:
                return "research_malformed_output", "the CLI started a second session in one run"
            self.init = event
            return self._check_init(event)
        if kind in ("assistant", "user", "result") and self.init is None:
            return "research_malformed_output", f"a {kind} event arrived before the init event"
        if kind in ("assistant", "user") and event.get("parent_tool_use_id") is not None:
            return "research_tool_use", "the stream contains nested agent activity; only top-level search is allowed"
        if kind == "assistant":
            return self._check_assistant(event)
        if kind == "user":
            return self._check_user(event)
        if kind == "result":
            self.result = event
        return None

    @staticmethod
    def _check_init(event):
        if event.get("model") != MODEL:
            return "research_model_mismatch", (f"the CLI session model is {clip(event.get('model'), 60)!r}, not "
                                               f"{MODEL}")
        if event.get("apiKeySource") != "none":
            return "research_api_key_source", (f"the CLI reports apiKeySource {clip(event.get('apiKeySource'), 40)!r};"
                                               " only the subscription login (apiKeySource none) is accepted")
        tools = event.get("tools")
        if not isinstance(tools, list) or sorted(map(str, tools)) != sorted(RESEARCH_TOOLS) or len(tools) != 2:
            return "research_tools_enabled", (f"the CLI session exposes tools {clip(tools, 200)}; exactly "
                                              f"{', '.join(RESEARCH_TOOLS)} are required")
        servers = event.get("mcp_servers")
        if not isinstance(servers, list) or servers:
            return "research_tools_enabled", "the CLI session lists MCP servers; none are allowed"
        return None

    def _check_assistant(self, event):
        message = event.get("message")
        if not isinstance(message, dict):
            return "research_malformed_output", "an assistant event has no message object"
        model = message.get("model")
        if model != MODEL:
            return "research_model_mismatch", f"an assistant message came from {clip(model, 60)!r}, not {MODEL}"
        self.assistant_messages += 1
        self.response_models.add(model)
        content = message.get("content")
        if not isinstance(content, list):
            return "research_malformed_output", "an assistant message has no content list"
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind not in ALLOWED_BLOCKS:
                return "research_tool_use", f"an assistant message contains a {clip(kind, 40)!r} block"
            if kind == "tool_use":
                problem = self._tool_call(block)
                if problem is not None:
                    return problem
        return None

    def _tool_call(self, block):
        call_id, name, payload = block.get("id"), block.get("name"), block.get("input")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or call_id in self.calls:
            return "research_malformed_output", "a tool call has a missing, oversized or repeated id"
        if name == SEARCH_TOOL:
            self.search_calls += 1
            if self.search_calls > MAX_SEARCH_CALLS:
                return "research_tool_limit", f"the model made more than {MAX_SEARCH_CALLS} WebSearch calls"
            query = payload.get("query") if isinstance(payload, dict) else None
            if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
                query = None  # never evidence: its result cannot be tied to a well-formed query
            record = {"query": clip(query, 100) if query is not None else None, "status": "unanswered", "links": 0}
            self.searches.append(record)
            self.calls[call_id] = {"name": name, "query": query, "record": record, "answered": False}
            return None
        if name == STRUCTURED_OUTPUT_TOOL:
            self.structured_calls += 1
            if self.structured_calls > MAX_STRUCTURED_CALLS:
                return "research_tool_limit", f"the model made more than {MAX_STRUCTURED_CALLS} StructuredOutput calls"
            self.output_call = {"id": call_id, "input": payload, "evidence": dict(self.evidence),
                                "successful_searches": self.successful_searches, "error": False}
            self.calls[call_id] = {"name": name, "answered": False}
            return None
        return "research_tool_use", (f"the model tried to use tool {clip(name, 60)!r}; only WebSearch and the "
                                     "structured-output return channel are allowed")

    def _check_user(self, event):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None  # plain user text carries no tool result and is never evidence
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            call = self.calls.get(call_id) if isinstance(call_id, str) else None
            if call is None:
                return "research_malformed_output", "a tool result answers no observed tool call"
            if call["answered"]:
                return "research_malformed_output", "a tool call was answered twice"
            call["answered"] = True
            failed = block.get("is_error") not in (None, False)
            if call["name"] == STRUCTURED_OUTPUT_TOOL:
                if failed and self.output_call is not None and self.output_call["id"] == call_id:
                    self.output_call["error"] = True
                continue
            record = call["record"]
            if failed:
                record["status"] = "error"
                self.failed_searches += 1
                continue
            status, links, _dropped = parse_search_result(block.get("content"), call["query"])
            record.update(status=status, links=len(links))
            if status != "ok":
                self.failed_searches += 1
                continue
            self.successful_searches += 1
            for url, title in links:
                if url in self.evidence:
                    continue
                if len(self.evidence) >= MAX_OBSERVED_URLS:
                    self.unrecorded_urls += 1
                    continue
                self.evidence[url] = title
        return None
