"""LABELED FAKES for the equipment-research tests. Nothing here calls Claude, the web or any network service.

- ``FakeResearchCli``: writes a FAKE ``claude`` executable into a private
  temporary directory. It answers ``auth status --json`` and ``-p ...`` from a
  control file, logs its argv, working directory, stdin size and whether
  ``ANTHROPIC_API_KEY`` reached it, can save the prompt, and can hang after N
  events (optionally ignoring SIGTERM and spawning a grandchild), flood stdout,
  exit nonzero or emit any lines. It exercises the real research subprocess
  bridge, not the real CLI.
- ``research_events``: a stream-json sequence shaped like the real CLI stream
  root recorded on October 16, 2026 (``REAL_PROBE``): init with
  ``StructuredOutput``/``WebSearch``, parallel ``WebSearch`` calls in separate
  assistant events, ``user`` ``tool_result`` strings with the
  ``Web search results for query: "..."`` header, a ``Links:`` JSON array, a
  summary body and the CLI's REMINDER trailer, thinking-token and rate-limit
  events, a ``StructuredOutput`` call and acknowledgement, and a ``result``
  whose ``modelUsage`` lists the exact model plus the internal retrieval model.
- ``REAL_PROBE`` / ``real_probe_lines``: the actual recorded stream, used read-only.
- ``REAL_REPLY`` / ``REAL_FAILURE``: root's actual successful manufacturer-HTML
  reply and its earlier fail-closed ``research_unverified_source`` record, read-only.
- ``verified_metadata``: LABELED FAKE metadata complete and consistent with a
  brief, as ``validate_research_result`` requires of a verified reply.

A pass with these fakes is not evidence that real research works; root runs
the actual exact-model subscription research separately.

This module defines no test cases.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

from skid.research import (EFFORT, MODEL, RETRIEVAL_MODEL, RETRIEVAL_ROLE, ClaudeCliResearcher, ResearchConfig,
                           research_prompt)

EVIDENCE = Path(__file__).resolve().parent.parent / "artifacts" / "agents" / "empty-research"
REAL_PROBE = EVIDENCE / "real-search-probe.jsonl"
REAL_REPLY = EVIDENCE / "root-real-research-html" / "reply.json"
REAL_FAILURE = EVIDENCE / "root-real-research" / "failure.json"
SESSION = "fake-session-0000"
REQUEST = "FAKE request: shell-and-tube heat exchanger HX-101 with separate shell-side and tube-side circuits"

FAKE_SCRIPT = r'''
import json, os, signal, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "control.json")) as handle:
    control = json.load(handle)
argv = sys.argv[1:]
is_auth = argv[:2] == ["auth", "status"]
data = b"" if is_auth else sys.stdin.buffer.read()
record = {"argv": argv, "cwd": os.getcwd(), "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
          "stdin_bytes": len(data), "pid": os.getpid(), "pgid": os.getpgid(0), "auth": is_auth}
with open(os.path.join(HERE, "calls.jsonl"), "a") as log:
    log.write(json.dumps(record) + "\n")
if not is_auth and control.get("save_prompt"):
    with open(os.path.join(HERE, "prompt.txt"), "wb") as handle:
        handle.write(data)
def emit(lines):
    for line in lines:
        sys.stdout.write(line if isinstance(line, str) else json.dumps(line))
        sys.stdout.write("\n")
        sys.stdout.flush()
def hang(prefix):
    if control.get(prefix + "ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if control.get(prefix + "grandchild"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(os.path.join(HERE, "grandchild.pid"), "w") as handle:
            handle.write(str(child.pid))
    with open(os.path.join(HERE, prefix + "hanging.pid"), "w") as handle:
        handle.write(str(os.getpid()))
    while True:
        time.sleep(0.05)
if is_auth:
    if control.get("auth_hang"):
        hang("auth_")
    if control.get("auth_exit"):
        sys.stderr.write("FAKE auth failure\n")
        sys.exit(control["auth_exit"])
    auth = control.get("auth", {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                                "email": "private@example.invalid", "orgName": "FAKE org"})
    sys.stdout.write(auth if isinstance(auth, str) else json.dumps(auth, indent=2))
    sys.stdout.write("\n")
    sys.exit(0)
if control.get("hang"):
    emit(control.get("events", [])[:control.get("hang_after", 0)])
    hang("")
if control.get("flood"):
    line = json.dumps({"type": "system", "subtype": "status", "pad": "x" * 65536})
    while True:
        sys.stdout.write(line + "\n")
emit(control.get("events", []))
if control.get("stderr"):
    sys.stderr.write(control["stderr"])
sys.exit(control.get("exit", 0))
'''


class FakeResearchCli:
    """A FAKE ``claude`` executable in its own temporary directory (no spaces in its path)."""

    def __init__(self, test, **control):
        self.dir = Path(tempfile.mkdtemp(prefix="skid-fake-research-cli-"))
        test.addCleanup(self.cleanup)
        script = self.dir / "fake_claude.py"
        script.write_text(FAKE_SCRIPT, encoding="utf-8")
        self.path = self.dir / "claude"
        self.path.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} \"$@\"\n",
                             encoding="utf-8")
        self.path.chmod(0o755)
        self.set(**control)

    def set(self, **control):
        (self.dir / "control.json").write_text(json.dumps(control), encoding="utf-8")

    def researcher(self, timeout=10.0, auth_timeout=5.0, grace=0.3, max_stdout_bytes=None):
        config = ResearchConfig(cli_path=str(self.path), timeout_s=timeout, auth_timeout_s=auth_timeout,
                                grace_s=grace, **({"max_stdout_bytes": max_stdout_bytes} if max_stdout_bytes else {}))
        return ClaudeCliResearcher(config)

    def calls(self):
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def prompt(self):
        return (self.dir / "prompt.txt").read_text(encoding="utf-8")

    def read_pid(self, name, timeout=10.0):
        path = self.dir / name
        deadline = time.monotonic() + timeout
        while not path.exists() or not path.read_text().strip():
            if time.monotonic() > deadline:
                raise AssertionError(f"FAKE CLI did not write {name}")
            time.sleep(0.01)
        return int(path.read_text())

    def cleanup(self):
        for name in ("grandchild.pid", "hanging.pid", "auth_hanging.pid"):
            path = self.dir / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), 9)
                except (OSError, ValueError):
                    pass
        for item in sorted(self.dir.rglob("*"), reverse=True):
            if item.is_file() or item.is_symlink():
                item.unlink()
            else:
                item.rmdir()
        self.dir.rmdir()


def process_gone(pid, timeout=10.0):
    """True once ``pid`` no longer exists (an orphan is reaped by init/launchd)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.02)
    return False


def group_gone(pgid, timeout=10.0):
    """True once process group ``pgid`` has no members left."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


# ---- FAKE search evidence (public-looking but invented domains; never fetched) ----

MAKER_URL = "https://www.fake-exchanger-maker.com/products/shell-and-tube-heat-exchanger/"
MAKER_TITLE = "FAKE Maker - Shell-and-tube heat exchanger"
TECH_URL = "https://assets.fake-heat-institute.org/tech-sheets/basics-of-shell-and-tube.pdf"
TECH_TITLE = "FAKE Institute Tech Sheet: Basics of Shell & Tube Heat Exchangers"
OTHER_URL = "https://en.fake-encyclopedia.org/wiki/Shell_and_tube_heat_exchanger"
MAKER_QUERY = "FAKE Maker shell-and-tube heat exchanger product nozzles"
TECH_QUERY = "shell and tube heat exchanger shell side tube side nozzles channel"
MAKER_BODY = ("FAKE summary. FAKE Maker's shell-and-tube unit has a cylindrical shell with a tube bundle; one fluid "
              "flows inside the tubes and the other around them inside the shell.\n\n- **Connections:** tube-side "
              "inlet and outlet on the channel heads; shell-side inlet and outlet nozzles on the shell.")
TECH_BODY = ("FAKE summary. Channels contain the tube-side fluid and distribute it into the tubes. Tube-side nozzles "
             "are where the tube-side fluid enters and leaves; baffles direct the shell-side flow. Tubesheets "
             "separate the two sides so the fluids never mix.")
REMINDER = "REMINDER: You MUST include the sources above in your response to the user using markdown hyperlinks."


def links(*pairs):
    return [{"title": title, "url": url} for url, title in pairs]


def search_body(query, link_list, body=MAKER_BODY):
    """The real CLI's WebSearch tool_result string shape."""
    return (f"Web search results for query: \"{query}\"\n\nLinks: {json.dumps(link_list, separators=(',', ':'))}"
            f"\n\n{body}\n\n\n{REMINDER}")


def search(call_id, query, link_list, body=MAKER_BODY, is_error=None, content=None):
    """A search spec for ``research_events``; ``content`` overrides the rendered result string."""
    return {"id": call_id, "query": query, "links": link_list, "body": body, "is_error": is_error,
            "content": content}


def standard_searches():
    return [search("toolu_fake_search_1", MAKER_QUERY, links((MAKER_URL, MAKER_TITLE), (OTHER_URL, "FAKE wiki"))),
            search("toolu_fake_search_2", TECH_QUERY, links((TECH_URL, TECH_TITLE), (OTHER_URL, "FAKE wiki")),
                   body=TECH_BODY)]


def output(**changes):
    """A schema-shaped FAKE model output citing the standard observed URLs."""
    value = {
        "equipment_type": "Shell-and-tube heat exchanger",
        "summary": ("FAKE: a long cylindrical shell holds a tube bundle between two tubesheets, with a channel "
                    "head at each end.\nThe tube-side and shell-side circuits are separate."),
        "sources": [{"id": "S1", "url": MAKER_URL, "observations": [
                        "FAKE: one fluid flows inside the tubes and the other around them inside the shell."]},
                    {"id": "S2", "url": TECH_URL, "observations": [
                        "FAKE: channels distribute the tube-side fluid; baffles direct the shell-side flow."]}],
        "features": [{"text": "Two separate circuits: tube side and shell side, divided by the tube walls.",
                      "source_ids": ["S1", "S2"]},
                     {"text": "Tube inlet/outlet nozzles on the channel heads; shell inlet/outlet on the shell.",
                      "source_ids": ["S1"]}],
        "assumptions": ["Illustrative shell length 3 m and diameter 0.6 m; not from a source."],
        "unknowns": ["Nozzle sizes, pressure ratings and materials."],
        "warnings": ["Search results were short summaries."]}
    value.update(changes)
    return value


def _uuid(n):
    return f"fake-uuid-{n:04d}"


def init_event(model=MODEL, tools=("StructuredOutput", "WebSearch"), api_key_source="none", mcp_servers=()):
    return {"type": "system", "subtype": "init", "cwd": "/fake/private", "session_id": SESSION, "tools": list(tools),
            "mcp_servers": list(mcp_servers), "model": model, "permissionMode": "dontAsk", "slash_commands": [],
            "apiKeySource": api_key_source, "claude_code_version": "2.1.300-FAKE", "output_style": "default",
            "agents": [], "skills": [], "plugins": [], "uuid": _uuid(0), "fast_mode_state": "off"}


def _usage():
    return {"input_tokens": 2, "cache_creation_input_tokens": 4293, "cache_read_input_tokens": 0,
            "output_tokens": 17, "service_tier": "standard"}


def assistant(content, model=MODEL, message_id="msg_fake_1", parent=None, n=1):
    event = {"type": "assistant", "message": {"model": model, "id": message_id, "type": "message", "role": "assistant",
                                              "content": content, "stop_reason": None, "usage": _usage()},
             "parent_tool_use_id": parent, "session_id": SESSION, "uuid": _uuid(n),
             "timestamp": "2026-10-16T06:28:29.908Z", "request_id": "req_fake_1"}
    calls = {block["id"]: block.get("input") for block in content if block.get("type") == "tool_use"}
    if calls:
        event["wire_tool_inputs"] = calls
    return event


def tool_use(call_id, name, payload):
    return {"type": "tool_use", "id": call_id, "name": name, "input": payload, "caller": {"type": "direct"}}


def search_call(spec, model=MODEL, message_id="msg_fake_1", parent=None):
    return assistant([tool_use(spec["id"], "WebSearch", {"query": spec["query"], "mode": "standard"})], model=model,
                     message_id=message_id, parent=parent)


def tool_result(call_id, content, is_error=None, parent=None, extra=None):
    block = {"tool_use_id": call_id, "type": "tool_result", "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    event = {"type": "user", "message": {"role": "user", "content": [block]}, "parent_tool_use_id": parent,
             "session_id": SESSION, "uuid": _uuid(2), "timestamp": "2026-10-16T06:28:37.850Z",
             "tool_result_meta": [{"id": call_id, "permission_decision": {"decision": "accept", "source": "config",
                                                                          "reason_type": "rule"}}]}
    event.update(extra or {})
    return event


def search_result(spec):
    content = spec["content"] if spec["content"] is not None else search_body(spec["query"], spec["links"],
                                                                              spec["body"])
    extra = {"tool_use_result": {"query": spec["query"], "results": [
        {"tool_use_id": "srvtoolu_fake", "content": spec["links"]}, spec["body"]], "durationSeconds": 7.9,
        "searchCount": 1}}
    return tool_result(spec["id"], content, spec["is_error"], extra=extra)


def thinking_tokens():
    return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 120, "estimated_tokens_delta": 40,
            "session_id": SESSION, "uuid": _uuid(3)}


def rate_limit():
    return {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
            "uuid": _uuid(4), "session_id": SESSION}


def output_call(payload, call_id="toolu_fake_output", model=MODEL):
    return assistant([tool_use(call_id, "StructuredOutput", payload)], model=model, message_id="msg_fake_3")


def output_ack(call_id="toolu_fake_output", is_error=None):
    content = "FAKE schema validation failed" if is_error else "Structured output provided successfully"
    return tool_result(call_id, content, is_error)


def result_event(structured, usage_models=None, retrieval_requests=2, subtype="success", is_error=False,
                 web_fetch_requests=0, permission_denials=(), include_structured=True):
    if usage_models is None:
        usage_models = [MODEL, RETRIEVAL_MODEL] if retrieval_requests else [MODEL]
    usage = {}
    for name in usage_models:
        usage[name] = {"inputTokens": 12, "outputTokens": 3299, "cacheReadInputTokens": 0, "webSearchRequests": 0,
                       "costUSD": 0.0, "thinkingTokens": 1484, "canonicalModel": name, "provider": "firstParty"}
        if name == RETRIEVAL_MODEL and retrieval_requests is not None:
            usage[name]["webSearchRequests"] = retrieval_requests
    event = {"type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": 81887, "num_turns": 4,
             "result": json.dumps(structured) if structured is not None else "", "session_id": SESSION,
             "stop_reason": "tool_use", "total_cost_usd": 0.0, "permission_denials": list(permission_denials),
             "terminal_reason": "completed", "modelUsage": usage, "uuid": _uuid(9),
             "usage": {"input_tokens": 12, "output_tokens": 3299,
                       "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": web_fetch_requests}}}
    if include_structured and structured is not None:
        event["structured_output"] = structured
    return event


def research_events(payload=None, searches=None, init=None, final=None, between=(), after_output=(),
                    ack_error=None, **result_options):
    """A complete FAKE research stream: parallel searches, their results, output call, ack and result."""
    payload = output() if payload is None else payload
    searches = standard_searches() if searches is None else searches
    events = [init or init_event(), rate_limit()]
    events += [search_call(spec) for spec in searches]
    events += [search_result(spec) for spec in searches]
    events += [thinking_tokens(), assistant([{"type": "thinking", "thinking": "", "signature": "FAKE"}],
                                            message_id="msg_fake_2"),
               assistant([{"type": "text", "text": "FAKE: searches are done; writing the brief."}],
                         message_id="msg_fake_3")]
    events += list(between)
    events += [output_call(payload)]
    events += list(after_output)
    events += [output_ack(is_error=ack_error)]
    events += [result_event(copy.deepcopy(payload) if final is None else final, **result_options)]
    return events


def real_probe_lines():
    """The actual recorded CLI stream (read-only), one parsed event per line."""
    return [json.loads(line) for line in REAL_PROBE.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_json(path):
    """A recorded artifact (read-only)."""
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verified_metadata(brief, observed_urls=None, retrieval=True, **overrides):
    """LABELED FAKE metadata, complete and consistent with ``brief`` as ``validate_research_result`` requires.

    Nothing was searched. ``observed_urls`` defaults to the brief's source URLs; one ``ok`` search record is
    written per 20 observed URLs; ``retrieval=False`` reports no internal retrieval model. Digests are computed
    from the final brief, so changing the brief afterwards (a forgery) no longer matches. ``overrides`` replace
    keys afterwards without recomputing anything. A pass with this metadata proves only that the reply is well
    formed and self-consistent, never that a search happened.
    """
    urls = [source["url"] for source in brief["sources"]]
    observed = list(urls if observed_urls is None else observed_urls)
    searches = [{"query": f"FAKE generic public query {n + 1}", "status": "ok", "links": len(observed[i:i + 20])}
                for n, i in enumerate(range(0, max(len(observed), 1), 20))]
    canonical = json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    metadata = {
        "fake": True, "label": "LABELED FAKE research metadata; nothing was searched",
        "kind": "claude-code-cli-research", "model": MODEL, "effort": {"requested": EFFORT, "reported": None},
        "verified": True, "request_sha256": _sha256(brief["query"]),
        "prompt_sha256": _sha256(research_prompt(brief["query"])),
        "auth": {"method": "claude.ai", "subscription": "max"}, "init_model": MODEL, "api_key_source": "none", "tools": ["StructuredOutput", "WebSearch"], "mcp_servers": [],
        "permission_mode": "dontAsk", "response_models": [MODEL], "search_calls": len(searches),
        "successful_searches": len(searches), "failed_searches": 0, "searches": searches,
        "observed_url_count": len(observed), "observed_urls": observed, "structured_output_calls": 1,
        "usage_models": sorted([MODEL, RETRIEVAL_MODEL]) if retrieval else [MODEL],
        "internal_retrieval": {"model": RETRIEVAL_MODEL, "web_search_requests": len(searches), "role": RETRIEVAL_ROLE,
                               "usage": {"inputTokens": 100, "outputTokens": 10}} if retrieval else None,
        "output_sha256": _sha256("FAKE model output"), "verified_sources": len(urls), "source_urls": urls,
        "brief_sha256": _sha256(canonical), "session_persistence": False, "image_inspection": False}
    metadata.update(overrides)
    return metadata
