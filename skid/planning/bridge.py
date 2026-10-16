"""Tool-free bridge to the locally installed Claude Code CLI on the user's Claude Max subscription.

``ClaudeCliPlanner.run(prompt, schema, cancel)`` runs two child processes from
an argument list (never a shell), each in its own process group, in a fresh
private working directory that is deleted afterwards:

1. ``<cli> auth status --json``; the reply must report ``authMethod``
   ``claude.ai`` and ``subscriptionType`` ``max`` (``loggedIn`` true when
   present). Only those fields are read or reported.
2. ``<cli> -p --model claude-opus-5-5 --effort medium --output-format
   stream-json --verbose --json-schema <schema> --tools "" --strict-mcp-config
   --setting-sources "" --no-session-persistence``, with the prompt on stdin.

Both children get a copy of this process's environment with only
``ANTHROPIC_API_KEY`` removed, so the CLI uses the subscription login; no API
client, API key, ``--bare``, fallback model, tool, MCP server, settings file
or session file is involved. The model and effort are constants, not options.

The stream is checked line by line while it arrives: the ``system/init``
event must name the exact model, ``apiKeySource: none``, no MCP servers and no
tools other than the CLI's structured-output return channel; every assistant
message must come from the exact model and may call no other tool; the final
``result`` must be ``success`` with a ``structured_output`` object, and its
``modelUsage`` may name no other model. Any violation stops the child at once.
Time-outs, cancellation, output beyond the limits, a nonzero exit and
malformed or truncated output raise ``PlannerError`` with a stable code; no
partial output is ever returned. ``shutdown()`` terminates every child it owns
(``SIGTERM`` to the group, ``SIGKILL`` after a grace period).
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from .schema import strict_json

__all__ = ["DEFAULT_CLI", "EFFORT", "MODEL", "ClaudeCliPlanner", "PlannerConfig", "PlannerError", "child_environment",
           "plan_argv"]

# ``claude`` as found on PATH when this module is imported (an absolute path), else the bare name, which the
# executable check in ``run`` reports as unavailable. ``--planner-cli PATH`` selects another installed executable.
DEFAULT_CLI = shutil.which("claude") or "claude"
MODEL = "claude-opus-5-5"
EFFORT = "medium"
REQUIRED_AUTH = (("authMethod", "claude.ai"), ("subscriptionType", "max"))
STRUCTURED_OUTPUT_TOOLS = ("StructuredOutput",)  # the CLI's --json-schema return channel; no side effects
ALLOWED_BLOCKS = ("text", "thinking", "redacted_thinking", "tool_use")
DEFAULT_TIMEOUT_S = 600.0
AUTH_TIMEOUT_S = 30.0
TERMINATE_GRACE_S = 3.0
MAX_PROMPT_BYTES = 1 << 20
MAX_STDOUT_BYTES = 16 << 20
MAX_LINE_BYTES = 8 << 20
MAX_AUTH_BYTES = 64 << 10
MAX_STDERR_BYTES = 16 << 10
MAX_PLAN_BYTES = 256 << 10


class PlannerError(Exception):
    """The planner produced nothing usable. ``code`` is stable; ``metadata`` is safe to show."""

    def __init__(self, code, message, metadata=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class PlannerConfig:
    cli_path: str = DEFAULT_CLI
    timeout_s: float = DEFAULT_TIMEOUT_S
    auth_timeout_s: float = AUTH_TIMEOUT_S
    grace_s: float = TERMINATE_GRACE_S
    max_stdout_bytes: int = MAX_STDOUT_BYTES


def child_environment() -> dict:
    """This process's environment with only ``ANTHROPIC_API_KEY`` removed (subscription login, not API billing)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def plan_argv(cli_path, schema) -> list:
    return [cli_path, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema, separators=(",", ":"), ensure_ascii=False), "--tools", "",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]


def _clip(value, limit=300):
    text = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in str(value))
    return text if len(text) <= limit else text[:limit] + "…"


class _Child:
    """One owned child process group; ``stop`` records why it was stopped and terminates the group."""

    def __init__(self, argv, data, cwd, grace):
        self.grace = grace
        self.reason = None
        self._lock = threading.Lock()
        self._kill_timer = None
        self.stderr = collections.deque()
        self.stderr_size = 0
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=child_environment(), cwd=cwd, start_new_session=True, close_fds=True)
        self._feeder = threading.Thread(target=self._feed, args=(data,), daemon=True)
        self._errors = threading.Thread(target=self._drain_stderr, daemon=True)
        self._feeder.start()
        self._errors.start()

    def _feed(self, data):
        try:
            self.process.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.close()

    def _drain_stderr(self):
        with contextlib.suppress(OSError, ValueError):
            while True:
                chunk = self.process.stderr.read1(4096) if hasattr(self.process.stderr, "read1") else \
                    self.process.stderr.read(4096)
                if not chunk:
                    break
                self.stderr.append(chunk)
                self.stderr_size += len(chunk)
                while self.stderr_size > MAX_STDERR_BYTES and len(self.stderr) > 1:
                    self.stderr_size -= len(self.stderr.popleft())

    def stderr_tail(self, limit=600):
        text = b"".join(self.stderr).decode("utf-8", "replace").strip()
        return _clip(text[-limit:], limit) if text else ""

    def _signal(self, sig):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self.process.pid, sig)

    def stop(self, reason):
        """Record the first reason; ``SIGTERM`` the group now and ``SIGKILL`` it after the grace period."""
        with self._lock:
            if self.reason is None:
                self.reason = reason
            if self._kill_timer is not None:
                return
            self._kill_timer = threading.Timer(self.grace, self._signal, args=(signal.SIGKILL,))
            self._kill_timer.daemon = True
            self._kill_timer.start()
        self._signal(signal.SIGTERM)

    def finish(self):
        """After the leader exited: kill any remaining group members and release the pipes."""
        self._signal(signal.SIGKILL)
        with self._lock:
            if self._kill_timer is not None:
                self._kill_timer.cancel()
        self._feeder.join(2)
        self._errors.join(2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()


class _Stream:
    """Incremental verification of the CLI's stream-json events."""

    def __init__(self):
        self.init = None
        self.result = None
        self.assistant_messages = 0
        self.response_models = set()
        self.structured_calls = 0

    def feed(self, line):
        """Check one line; return ``(code, message)`` for a violation, else ``None``."""
        try:
            text = line.decode("utf-8").strip()
        except UnicodeDecodeError:
            return "planner_malformed_output", "the CLI wrote a line that is not UTF-8"
        if not text:
            return None
        try:
            event = strict_json(text)
        except ValueError as exc:
            return "planner_malformed_output", f"the CLI wrote a line that is not strict JSON ({_clip(exc, 120)})"
        if not isinstance(event, dict):
            return "planner_malformed_output", "a stream event is not an object"
        if self.result is not None:
            return "planner_malformed_output", "the CLI wrote events after its final result"
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            if self.init is not None:
                return "planner_malformed_output", "the CLI started a second session in one run"
            self.init = event
            return self._check_init(event)
        if kind == "assistant":
            if self.init is None:
                return "planner_malformed_output", "an assistant message arrived before the init event"
            return self._check_assistant(event)
        if kind == "result":
            self.result = event
        return None

    @staticmethod
    def _check_init(event):
        if event.get("model") != MODEL:
            return "planner_model_mismatch", (f"the CLI session model is {_clip(event.get('model'), 60)!r}, not "
                                              f"{MODEL}")
        if event.get("apiKeySource") != "none":
            return "planner_api_key_source", (f"the CLI reports apiKeySource {_clip(event.get('apiKeySource'), 40)!r}; "
                                              "only the subscription login (apiKeySource none) is accepted")
        tools = event.get("tools")
        if not isinstance(tools, list) or any(t not in STRUCTURED_OUTPUT_TOOLS for t in tools):
            return "planner_tools_enabled", f"the CLI session exposes tools {_clip(tools, 200)}; none are allowed"
        servers = event.get("mcp_servers")
        if not isinstance(servers, list) or servers:
            return "planner_tools_enabled", "the CLI session lists MCP servers; none are allowed"
        return None

    def _check_assistant(self, event):
        message = event.get("message")
        if not isinstance(message, dict):
            return "planner_malformed_output", "an assistant event has no message object"
        model = message.get("model")
        if model != MODEL:
            return "planner_model_mismatch", f"an assistant message came from {_clip(model, 60)!r}, not {MODEL}"
        self.assistant_messages += 1
        self.response_models.add(model)
        content = message.get("content")
        if not isinstance(content, list):
            return "planner_malformed_output", "an assistant message has no content list"
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind not in ALLOWED_BLOCKS:
                return "planner_tool_use", f"an assistant message contains a {_clip(kind, 40)!r} block"
            if kind == "tool_use":
                if block.get("name") not in STRUCTURED_OUTPUT_TOOLS:
                    return "planner_tool_use", (f"the model tried to use tool {_clip(block.get('name'), 60)!r}; "
                                                "the planner has no tools")
                self.structured_calls += 1
        return None


class ClaudeCliPlanner:
    """Runs one exact-model, tool-free plan per call; owns and cleans up its children."""

    kind = "claude-code-cli"

    def __init__(self, config=None):
        self.config = config or PlannerConfig()
        self._children = set()
        self._lock = threading.Lock()
        self._closed = False

    def describe(self) -> dict:
        """Static configuration for status displays; starts no process."""
        cli = self.config.cli_path
        return {"kind": self.kind, "model": MODEL, "effort": EFFORT, "cli": cli,
                "cli_present": os.path.isfile(cli) and os.access(cli, os.X_OK), "timeout_s": self.config.timeout_s,
                "tools": [], "session_persistence": False, "api_key_env_removed": True}

    def shutdown(self):
        """Stop every running child (``SIGTERM`` to its group, ``SIGKILL`` after the grace period)."""
        with self._lock:
            self._closed = True
            children = list(self._children)
        for child in children:
            child.stop("shutdown")

    def _execute(self, argv, data, timeout, cancel, cwd, handler, limit):
        with self._lock:
            if self._closed:
                raise PlannerError("planner_cancelled", "the service is shutting down; no plan was started")
            try:
                child = _Child(argv, data, cwd, self.config.grace_s)
            except OSError as exc:
                raise PlannerError("planner_unavailable", f"the Claude Code CLI could not be started "
                                   f"({type(exc).__name__}: {_clip(exc, 160)})") from None
            self._children.add(child)
        reader = threading.Thread(target=self._read, args=(child, handler, limit), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    child.process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel.is_set():
                    child.stop("cancelled")
                if time.monotonic() >= deadline:
                    child.stop("timeout")
            reader.join(5)
        finally:
            child.finish()
            reader.join(5)  # a descendant holding stdout open is killed by finish(); never verify mid-read
            with self._lock:
                self._children.discard(child)
        if reader.is_alive():
            child.reason = child.reason or ("planner_malformed_output", "the CLI output stream did not close")
        return child

    @staticmethod
    def _read(child, handler, limit):
        total = 0
        stream = child.process.stdout
        with contextlib.suppress(OSError, ValueError):
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                total += len(line)
                if len(line) > MAX_LINE_BYTES or total > limit:
                    child.stop(("planner_output_limit", f"the CLI wrote more than {limit} bytes or a line longer "
                                f"than {MAX_LINE_BYTES} bytes"))
                    return
                problem = handler(line)
                if problem is not None:
                    child.stop(problem)
                    return

    def _failure(self, child, timeout, metadata):
        reason = child.reason
        if isinstance(reason, tuple):
            return PlannerError(reason[0], reason[1], metadata)
        if reason == "timeout":
            return PlannerError("planner_timeout", f"the planner did not finish within {timeout:g} s and was "
                                "terminated", metadata)
        if reason in ("cancelled", "shutdown"):
            why = "the service is shutting down" if reason == "shutdown" else "the plan was cancelled"
            return PlannerError("planner_cancelled", f"{why}; the planner process was terminated", metadata)
        return None

    def _check_auth(self, cwd, cancel, metadata):
        cli = self.config.cli_path
        chunks = []
        child = self._execute([cli, "auth", "status", "--json"], b"", self.config.auth_timeout_s, cancel, cwd,
                              lambda line: chunks.append(line), MAX_AUTH_BYTES)
        failure = self._failure(child, self.config.auth_timeout_s, metadata)
        if failure is not None:
            if failure.code == "planner_timeout":
                failure.code = "planner_auth"
                failure.message = f"the CLI auth status check did not finish within {self.config.auth_timeout_s:g} s"
            raise failure
        if child.process.returncode != 0:
            tail = child.stderr_tail(200)
            raise PlannerError("planner_auth", f"the CLI auth status check exited with status "
                               f"{child.process.returncode}{': ' + tail if tail else ''}", metadata)
        try:
            status = strict_json(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PlannerError("planner_auth", "the CLI auth status reply is not a JSON object", metadata) from None
        if not isinstance(status, dict):
            raise PlannerError("planner_auth", "the CLI auth status reply is not a JSON object", metadata)
        found = {key: status.get(key) for key, _ in REQUIRED_AUTH}
        metadata["auth"] = {"method": _clip(found["authMethod"], 40) if found["authMethod"] is not None else None,
                            "subscription": _clip(found["subscriptionType"], 40)
                            if found["subscriptionType"] is not None else None}
        wrong = [f"{key} is {_clip(found[key], 40)!r}, not {wanted!r}" for key, wanted in REQUIRED_AUTH
                 if found[key] != wanted]
        if "loggedIn" in status and status["loggedIn"] is not True:
            wrong.insert(0, "the CLI is not logged in")
        if wrong:
            raise PlannerError("planner_auth", "the Claude Code CLI is not using the Claude Max subscription login: "
                               + "; ".join(wrong), metadata)

    def run(self, prompt, schema, cancel=None) -> dict:
        """Return ``{"plan": dict, "metadata": dict}``; raise ``PlannerError`` for anything unverified."""
        started = time.monotonic()
        cli = self.config.cli_path
        metadata = {"kind": self.kind, "cli": cli, "model": MODEL, "effort": {"requested": EFFORT, "reported": None},
                    "verified": False}
        data = prompt.encode("utf-8")
        if len(data) > MAX_PROMPT_BYTES:
            raise PlannerError("planner_input_limit", f"the planning prompt is {len(data)} bytes; the limit is "
                               f"{MAX_PROMPT_BYTES}", metadata)
        if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
            raise PlannerError("planner_unavailable", f"the Claude Code CLI executable {cli} is not installed or not "
                               "executable; nothing is downloaded or installed at runtime", metadata)
        workdir = tempfile.mkdtemp(prefix="skid-planner-")
        try:
            self._check_auth(workdir, cancel, metadata)
            stream = _Stream()
            child = self._execute(plan_argv(cli, schema), data, self.config.timeout_s, cancel, workdir, stream.feed,
                                  self.config.max_stdout_bytes)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            metadata["elapsed_s"] = round(time.monotonic() - started, 3)
        return self._verify(child, stream, metadata)

    def _verify(self, child, stream, metadata):
        if stream.init is not None:
            metadata.update(cli_version=_clip(stream.init.get("claude_code_version"), 40),
                            init_model=stream.init.get("model"), api_key_source=stream.init.get("apiKeySource"),
                            tools=list(stream.init.get("tools") or []), mcp_servers=[])
            for key in ("effort", "effort_level", "effortLevel"):
                if isinstance(stream.init.get(key), str):
                    metadata["effort"]["reported"] = _clip(stream.init[key], 20)
        metadata["response_models"] = sorted(stream.response_models)
        failure = self._failure(child, self.config.timeout_s, metadata)
        if failure is not None:
            raise failure
        result = stream.result
        if isinstance(result, dict) and (result.get("is_error") is not False or result.get("subtype") != "success"):
            raise PlannerError("planner_error_result", f"the CLI reported {_clip(result.get('subtype'), 60)!r} "
                               f"(is_error {result.get('is_error')!r}, API status "
                               f"{_clip(result.get('api_error_status'), 20)})", metadata)
        if child.process.returncode != 0:
            tail = child.stderr_tail()
            raise PlannerError("planner_exit_status", f"the CLI exited with status {child.process.returncode}"
                               f"{': ' + tail if tail else ''}", metadata)
        if stream.init is None:
            raise PlannerError("planner_malformed_output", "the CLI output has no init event, so the session model, "
                               "tools and API-key source are unverified", metadata)
        if stream.assistant_messages == 0:
            raise PlannerError("planner_malformed_output", "the CLI output has no assistant message, so the response "
                               "model is unverified", metadata)
        if result is None:
            raise PlannerError("planner_malformed_output", "the CLI output ended without a final result (truncated)",
                               metadata)
        usage = result.get("modelUsage")
        if usage is not None and (not isinstance(usage, dict) or set(usage) - {MODEL}):
            raise PlannerError("planner_model_mismatch", f"the CLI reports usage of other models "
                               f"{_clip(sorted(set(usage) - {MODEL}) if isinstance(usage, dict) else usage, 120)}",
                               metadata)
        plan = result.get("structured_output")
        if not isinstance(plan, dict):
            raise PlannerError("planner_output_missing", "the CLI result has no structured_output object matching "
                               "the plan schema", metadata)
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PLAN_BYTES:
            raise PlannerError("planner_output_limit", f"the structured plan is {len(encoded)} bytes; the limit is "
                               f"{MAX_PLAN_BYTES}", metadata)
        stats = usage.get(MODEL, {}) if isinstance(usage, dict) else {}
        metadata.update(
            verified=True, structured_output_calls=stream.structured_calls,
            output_sha256=hashlib.sha256(encoded).hexdigest(), session_persistence=False,
            duration_ms=result.get("duration_ms") if isinstance(result.get("duration_ms"), int) else None,
            num_turns=result.get("num_turns") if isinstance(result.get("num_turns"), int) else None,
            usage={key: stats[key] for key in ("inputTokens", "outputTokens", "thinkingTokens")
                   if isinstance(stats.get(key), int)})
        return {"plan": plan, "metadata": metadata}
