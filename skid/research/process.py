"""Owned CLI child processes for the researcher: the planner bridge's process pattern, kept local.

``skid.planning.bridge`` stays unchanged and is not imported: ``skid.planning``
imports the planner/integration modules, which in turn use this package, so an
import from here would be circular. ``Child`` and ``ProcessOwner.execute``
follow the bridge's ``_Child`` and ``_execute`` exactly: argument list (never
a shell), own process group, environment copy without ``ANTHROPIC_API_KEY``,
stdin fed and closed by a thread, bounded stderr tail, a reader thread that
checks each stdout line and stops the group on the first violation, and
``SIGTERM`` to the group followed by ``SIGKILL`` after the grace period on
time-out, cancellation, violation or shutdown. ``finish`` kills any remaining
group members once the leader has exited.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import signal
import subprocess
import threading
import time

__all__ = ["ERROR_CODES", "Child", "ProcessOwner", "ResearchError", "child_environment", "clip", "strict_json"]

ERROR_CODES = ("research_input_invalid", "research_unavailable", "research_auth", "research_cancelled",
               "research_timeout", "research_output_limit", "research_malformed_output", "research_model_mismatch",
               "research_api_key_source", "research_tools_enabled", "research_tool_use", "research_tool_limit",
               "research_error_result", "research_exit_status", "research_output_missing", "research_no_evidence",
               "research_unverified_source", "research_invalid_brief")
MAX_LINE_BYTES = 8 << 20
MAX_STDERR_BYTES = 16 << 10
MAX_DEPTH = 40
POLL_S = 0.05


class ResearchError(Exception):
    """Research produced nothing usable. ``code`` is stable; ``message`` and ``metadata`` are safe to show."""

    def __init__(self, code, message, metadata=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.metadata = dict(metadata or {})


def child_environment() -> dict:
    """This process's environment with only ``ANTHROPIC_API_KEY`` removed (subscription login, not API billing)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def clip(value, limit=300):
    text = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def _depth(value):
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if level > MAX_DEPTH:
            break
        if isinstance(node, dict):
            stack.extend((item, level + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, level + 1) for item in node)
    return deepest


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(name):
    raise ValueError(f"nonfinite number {name}")


STRICT_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def strict_json(text):
    """Parse JSON text strictly: duplicate keys, NaN/Infinity and excessive nesting raise ``ValueError``."""
    try:
        value = STRICT_DECODER.decode(text)
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None
    if _depth(value) > MAX_DEPTH:
        raise ValueError(f"JSON nesting deeper than {MAX_DEPTH} levels")
    return value


class Child:
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
        return clip(text[-limit:], limit) if text else ""

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


class ProcessOwner:
    """Starts, watches and cleans up children; ``shutdown`` stops them all and refuses new ones."""

    def __init__(self, grace):
        self.grace = grace
        self._children = set()
        self._lock = threading.Lock()
        self.closed = False

    def shutdown(self):
        with self._lock:
            self.closed = True
            children = list(self._children)
        for child in children:
            child.stop("shutdown")

    def execute(self, argv, data, timeout, cancel, cwd, handler, limit, metadata):
        """Run one child to completion; return it with ``reason`` set if it was stopped."""
        with self._lock:
            if self.closed:
                raise ResearchError("research_cancelled", "the service is shutting down; no research was started",
                                    metadata)
            if cancel is not None and cancel.is_set():
                raise ResearchError("research_cancelled", "the research was cancelled before it started", metadata)
            try:
                child = Child(argv, data, cwd, self.grace)
            except OSError as exc:
                raise ResearchError("research_unavailable", f"the Claude Code CLI could not be started "
                                    f"({type(exc).__name__}: {clip(exc, 160)})", metadata) from None
            self._children.add(child)
        reader = threading.Thread(target=self._read, args=(child, handler, limit), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    child.process.wait(timeout=POLL_S)
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
            child.reason = child.reason or ("research_malformed_output", "the CLI output stream did not close")
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
                    child.stop(("research_output_limit", f"the CLI wrote more than {limit} bytes or a line longer "
                                f"than {MAX_LINE_BYTES} bytes"))
                    return
                try:
                    problem = handler(line)
                except Exception as exc:  # noqa: BLE001 - an unexpected checker fault fails closed, never open
                    problem = ("research_malformed_output", f"the stream check failed on a line "
                               f"({type(exc).__name__}: {clip(exc, 120)})")
                if problem is not None:
                    child.stop(problem)
                    return
