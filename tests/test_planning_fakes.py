"""LABELED FAKES for the engineering-planner tests. Nothing here calls Claude or any network service.

- ``FakeCli``: writes a FAKE ``claude`` executable into a private temporary
  directory. It answers ``auth status --json`` and ``-p ...`` from a control
  file, logs its argv, working directory, stdin size and whether
  ``ANTHROPIC_API_KEY`` reached it, and can hang (optionally ignoring SIGTERM
  and spawning a grandchild), wait at a gate, exit nonzero or emit any lines.
  ``auth_startup_delay`` deliberately slows the FAKE auth child's startup
  before it logs or writes any marker. It exercises the real subprocess
  bridge, not the real CLI.
- ``ChildCapture``: patches ``bridge._Child`` with a wrapper that constructs the
  REAL child unchanged, records it and its PID in the parent, and optionally
  waits until the FAKE has written a PID marker matching that process before
  the bridge starts its deadline. Termination stays the bridge's own.
- ``FakePlanner``: an in-process planner object with the bridge's interface,
  returning a given plan or raising a given ``PlannerError``, optionally
  blocking until released.
- ``stream_events``: a stream-json event sequence shaped like the real CLI's.

A pass with these fakes is not evidence that the real planner works; the GPT
orchestrator runs the actual exact-model subscription plan separately.

This module defines no test cases.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from skid.planning import bridge
from skid.planning.bridge import EFFORT, MODEL, ClaudeCliPlanner, PlannerConfig, PlannerError

FAKE_SCRIPT = r'''
import json, os, signal, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "control.json")) as handle:
    control = json.load(handle)
argv = sys.argv[1:]
is_auth = argv[:2] == ["auth", "status"]
if is_auth and control.get("auth_startup_delay"):
    time.sleep(control["auth_startup_delay"])
data =b"" if is_auth else sys.stdin.buffer.read()
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
gate = control.get("gate")
if gate:
    with open(os.path.join(HERE, "at-gate"), "w") as handle:
        handle.write("waiting")
    while not os.path.exists(gate):
        time.sleep(0.01)
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


def stream_events(plan, model=MODEL, api_key_source="none", tools=(), mcp_servers=(), response_model=None,
                  tool_name="StructuredOutput", usage_models=None, subtype="success", is_error=False,
                  structured=True, extra_blocks=()):
    """A FAKE stream-json sequence: init, assistant (thinking + structured-output call), tool result, result."""
    response_model = response_model or model
    init = {"type": "system", "subtype": "init", "cwd": "/fake", "session_id": "fake-session", "tools": list(tools),
            "mcp_servers": list(mcp_servers), "model": model, "permissionMode": "default",
            "apiKeySource": api_key_source, "claude_code_version": "0.0.0-FAKE", "output_style": "default"}
    content = [{"type": "thinking", "thinking": "FAKE"}, *extra_blocks,
               {"type": "tool_use", "id": "toolu_fake", "name": tool_name, "input": plan}]
    assistant = {"type": "assistant", "message": {"model": response_model, "id": "msg_fake", "type": "message",
                                                  "role": "assistant", "content": content},
                 "session_id": "fake-session"}
    tool_result = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_fake", "content": "Structured output provided successfully"}]}}
    usage = {name: {"inputTokens": 10, "outputTokens": 20, "thinkingTokens": 5}
             for name in (usage_models if usage_models is not None else [model])}
    result = {"type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": 1234, "num_turns": 2,
              "result": "", "session_id": "fake-session", "total_cost_usd": 0.0, "modelUsage": usage}
    if structured:
        result["structured_output"] = plan
    return [init, assistant, tool_result, result]


class FakeCli:
    """A FAKE ``claude`` executable in its own temporary directory (no spaces in its path)."""

    def __init__(self, test, **control):
        self.dir = Path(tempfile.mkdtemp(prefix="skid-fake-cli-"))
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

    def planner(self, timeout=10.0, auth_timeout=5.0, grace=0.3, max_stdout_bytes=None):
        config = PlannerConfig(cli_path=str(self.path), timeout_s=timeout, auth_timeout_s=auth_timeout, grace_s=grace,
                               **({"max_stdout_bytes": max_stdout_bytes} if max_stdout_bytes else {}))
        return ClaudeCliPlanner(config)

    def calls(self):
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

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


class ChildCapture:
    """Context manager: wraps the REAL ``bridge._Child`` to record each launched child in the parent test.

    With ``ready`` (a PID marker name in ``cli.dir``), each child is returned to
    the bridge only after the FAKE wrote that marker and its PID equals
    ``child.process.pid``; the bridge sets its deadline after ``_Child``
    returns, so a slow FAKE startup cannot use up the time-out before the
    hang it is meant to exercise. A readiness failure kills the real owned
    group and raises ``AssertionError`` (never ``OSError``, which the bridge
    would translate). Children still running on exit are killed and reaped.
    """

    def __init__(self, cli=None, ready=None, ready_timeout=30.0):
        self.cli = cli
        self.ready = ready
        self.ready_timeout = ready_timeout
        self.children = []
        self.ready_at = []
        self._real = bridge._Child
        self._patch = mock.patch.object(bridge, "_Child", self._start)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        for child in self.children:
            self._kill(child)
        return False

    @staticmethod
    def _kill(child, owned=False):
        """Kill a still-running child's group; ``owned``: never handed to the bridge, so release it here too."""
        if child.process.poll() is None:  # Popen still owns the unreaped PID, so its group cannot have been reused
            try:
                os.killpg(child.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            child.process.wait(10)
            owned = True
        if owned:  # an already-reaped child handed to the bridge was finished there; never signal its PID again
            child.finish()

    def _start(self, *args, **kwargs):
        child = self._real(*args, **kwargs)
        self.children.append(child)
        if self.ready is None:
            return child
        try:
            self._await_ready(child)
        except BaseException:
            self._kill(child, owned=True)
            raise
        self.ready_at.append(time.monotonic())
        return child

    def _await_ready(self, child):
        path = self.cli.dir / self.ready
        deadline = time.monotonic() + self.ready_timeout
        while not path.exists() or not path.read_text().strip():
            if child.process.poll() is not None:
                raise AssertionError(f"FAKE child {child.process.pid} exited ({child.process.returncode}) before "
                                     f"writing {self.ready}")
            if time.monotonic() > deadline:
                raise AssertionError(f"FAKE child {child.process.pid} did not write {self.ready} within "
                                     f"{self.ready_timeout:g} s")
            time.sleep(0.01)
        marker = int(path.read_text())
        if marker != child.process.pid:
            raise AssertionError(f"FAKE marker {self.ready} names PID {marker}, not the launched child "
                                 f"{child.process.pid}")


class FakePlanner:
    """LABELED FAKE in-process planner: returns ``plan`` (or raises ``error``), optionally after a gate opens."""

    kind = "FAKE"

    def __init__(self, plan=None, error=None, gated=False, metadata=None):
        self.plan = plan
        self.error = error
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.entered = threading.Event()
        self.calls = []
        self.shutdowns = 0
        self.metadata = dict(metadata or {"kind": "FAKE", "model": MODEL, "verified": True, "fake": True})

    def describe(self):
        return {"kind": "FAKE", "model": MODEL, "effort": EFFORT, "cli": None, "cli_present": False}

    def run(self, prompt, schema, cancel=None):
        self.calls.append({"prompt": prompt, "schema": schema})
        self.entered.set()
        while not self.gate.wait(0.01):
            if cancel is not None and cancel.is_set():
                raise PlannerError("planner_cancelled", "FAKE planner cancelled", {"fake": True})
        if cancel is not None and cancel.is_set():
            raise PlannerError("planner_cancelled", "FAKE planner cancelled", {"fake": True})
        if self.error is not None:
            raise self.error
        return {"plan": json.loads(json.dumps(self.plan)), "metadata": dict(self.metadata)}

    def shutdown(self):
        self.shutdowns += 1


def plan(operations=(), assumptions=(), outcome="proposal", summary="FAKE plan.", questions=(), impacts=(),
         unknowns=(), reasoning=("FAKE reasoning note.",)):
    """A schema-shaped FAKE plan. An assumption is ``(key, description)`` or ``(key, description, source_ids)``."""
    return {"outcome": outcome, "summary": summary, "reasoning": list(reasoning), "operations": list(operations),
            "assumptions": [{"key": a[0], "description": a[1], "source_ids": list(a[2]) if len(a) > 2 else []}
                            for a in assumptions], "unknowns": list(unknowns),
            "questions": list(questions), "impacts": list(impacts)}


def op(kind, /, purpose="FAKE purpose.", **args):
    return {"op": kind, "purpose": purpose, "args": args}


DRUM_PARAMETERS = (("shell_length", 1.4), ("shell_diameter", 0.6), ("centreline_height", 0.75), ("inlet_offset", 0.6))


def drum_insert(entity_id="ent-drum-1", connection_id="conn-discharge-2", tag="D-101", downstream_tag="L-110",
                assumption="drum", placement=None, definition_ref="example:horizontal_drum", definition_json="",
                parameters=DRUM_PARAMETERS, x=600.0, y=220.0):
    return op("insert_equipment", connection_id=connection_id, entity_id=entity_id, tag=tag,
              downstream_tag=downstream_tag,
              parameters=[{"name": n, "value": v, "unit": "m", "assumption": assumption} for n, v in parameters],
              symbol={"x": x, "y": y, "rotation_deg": 0.0},
              placement=placement or {"status": "unresolved", "position_m": [], "rotation_deg": [], "assumption": ""},
              definition_ref=definition_ref, definition_json=definition_json)


COMPOUND = plan(
    operations=[op("rename_entity", entity_id="ent-pump", tag="P-201"),
                op("set_parameter", entity_id="ent-pump", name="motor_length", value=420, unit="mm",
                   assumption="motor")],
    assumptions=[("motor", "User-requested illustrative motor length of 420 mm.")],
    summary="Rename P-101 to P-201 and set its motor length to 420 mm.",
    impacts=[{"ids": ["ent-pump"], "effect": "tag and motor length change"}])
