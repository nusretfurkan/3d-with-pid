"""CLI bridge against a LABELED FAKE ``claude`` executable (``tests.test_planning_fakes.FakeCli``).

Real subprocess handling is exercised: argument list, environment, private
working directory, auth check, stream verification, time-outs, cancellation,
shutdown and process-group cleanup. Nothing here runs the real Claude Code CLI
or reaches a model; that acceptance run is separate.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import unittest
from unittest import mock

from skid.planning.bridge import DEFAULT_CLI, EFFORT, MODEL, PlannerError, plan_argv
from skid.planning.schema import cli_schema
from tests.test_planning_fakes import COMPOUND, ChildCapture, FakeCli, group_gone, process_gone, stream_events

PROMPT = "FAKE prompt {\"request\": {\"text\": \"rename P-101\"}}"


class BridgeCase(unittest.TestCase):
    def run_plan(self, cli, **planner_options):
        return cli.planner(**planner_options).run(PROMPT, cli_schema())

    def assertFails(self, code, fragment=None, **control):
        cli = FakeCli(self, **control)
        with self.assertRaises(PlannerError) as caught:
            self.run_plan(cli)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        if fragment:
            self.assertIn(fragment.lower(), caught.exception.message.lower())
        self.assertFalse(caught.exception.metadata.get("verified"))
        return caught.exception, cli


class SuccessTests(BridgeCase):
    def test_verified_plan_argv_environment_and_private_directory(self):
        cli = FakeCli(self, events=stream_events(COMPOUND), save_prompt=True)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "FAKE-test-key-not-a-secret"}):
            reply = self.run_plan(cli)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "FAKE-test-key-not-a-secret")  # parent untouched
        self.assertEqual(reply["plan"], COMPOUND)
        meta = reply["metadata"]
        self.assertTrue(meta["verified"])
        self.assertEqual((meta["model"], meta["init_model"], meta["response_models"]), (MODEL, MODEL, [MODEL]))
        self.assertEqual(meta["effort"], {"requested": EFFORT, "reported": None})
        self.assertEqual(meta["auth"], {"method": "claude.ai", "subscription": "max"})
        self.assertEqual(meta["api_key_source"], "none")
        self.assertEqual((meta["tools"], meta["mcp_servers"], meta["session_persistence"]), ([], [], False))
        self.assertEqual(meta["usage"], {"inputTokens": 10, "outputTokens": 20, "thinkingTokens": 5})
        self.assertEqual(meta["structured_output_calls"], 1)
        self.assertNotIn("private@example.invalid", json.dumps(meta))  # only the two auth fields are kept
        auth, run = cli.calls()
        self.assertEqual(auth["argv"], ["auth", "status", "--json"])
        self.assertEqual(run["argv"], plan_argv(str(cli.path), cli_schema())[1:])
        for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--verbose"):
            self.assertIn(flag, run["argv"])
        argv = run["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), cli_schema())
        self.assertNotIn("--bare", argv)
        self.assertFalse(any("fallback" in a for a in argv))
        for call in (auth, run):
            self.assertFalse(call["api_key_present"])  # removed in the child environment only
            self.assertEqual(call["pgid"], call["pid"])  # its own process group
            self.assertTrue(os.path.basename(call["cwd"]).startswith("skid-planner-"))
            self.assertFalse(os.path.exists(call["cwd"]))  # private directory removed afterwards
        self.assertEqual(run["stdin_bytes"], len(PROMPT.encode()))  # the prompt travels on stdin, not argv
        self.assertEqual((cli.dir / "prompt.txt").read_text(encoding="utf-8"), PROMPT)
        self.assertNotIn(PROMPT, argv)

    def test_default_executable_is_claude_on_path(self):
        # Portable default: no user-specific install path; --planner-cli overrides it.
        self.assertEqual(os.path.basename(DEFAULT_CLI), "claude")
        self.assertTrue(DEFAULT_CLI == "claude" or os.path.isabs(DEFAULT_CLI), DEFAULT_CLI)
        cli = FakeCli(self)
        description = cli.planner().describe()
        self.assertEqual((description["model"], description["effort"], description["tools"]), (MODEL, "medium", []))
        self.assertTrue(description["cli_present"])


class AuthTests(BridgeCase):
    def test_wrong_method_subscription_login_and_unreadable_status(self):
        for auth, fragment in (({"loggedIn": True, "authMethod": "api_key", "subscriptionType": "max"}, "authMethod"),
                               ({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"},
                                "subscriptionType"),
                               ({"loggedIn": False, "authMethod": "claude.ai", "subscriptionType": "max"},
                                "not logged in"),
                               ({"authMethod": "claude.ai"}, "subscriptionType"),
                               ("not json", "not a JSON object"),
                               ("[1, 2]", "not a JSON object")):
            with self.subTest(auth=auth):
                error, cli = self.assertFails("planner_auth", fragment, auth=auth, events=stream_events(COMPOUND))
                self.assertEqual(len(cli.calls()), 1)  # the plan never starts after a failed auth check
                self.assertNotIn("private@example.invalid", json.dumps(error.metadata) + error.message)

    def test_auth_exit_status_and_hang(self):
        self.assertFails("planner_auth", "exited with status 3", auth_exit=3)
        # The 1.0 s FAKE startup delay exceeds the 0.5 s auth time-out: the readiness gate holds the bridge's
        # deadline until the FAKE is actually hanging, so a slow startup (e.g. under load) cannot pre-empt it.
        for delay in (0, 1.0):
            with self.subTest(auth_startup_delay=delay):
                cli = FakeCli(self, auth_hang=True, auth_startup_delay=delay)
                with ChildCapture(cli, ready="auth_hanging.pid") as capture:
                    with self.assertRaises(PlannerError) as caught:
                        self.run_plan(cli, auth_timeout=0.5)
                    stopped = time.monotonic()
                self.assertEqual(caught.exception.code, "planner_auth")
                self.assertIn("within 0.5 s", caught.exception.message)
                self.assertEqual((len(capture.children), len(capture.ready_at)), (1, 1))  # the plan never started
                self.assertGreaterEqual(stopped - capture.ready_at[0], 0.5)
                self.assertLess(stopped - capture.ready_at[0], 6)
                pid = cli.read_pid("auth_hanging.pid")
                self.assertEqual(pid, capture.children[0].process.pid)
                self.assertTrue(process_gone(pid))
                self.assertTrue(group_gone(pid))

    def test_auth_timeout_before_the_fake_is_ready_kills_the_launched_process(self):
        delay = 5.0  # FAKE startup far beyond the 0.5 s auth time-out; no readiness gate
        cli = FakeCli(self, auth_hang=True, auth_startup_delay=delay)
        with ChildCapture() as capture:
            started = time.monotonic()
            with self.assertRaises(PlannerError) as caught:
                self.run_plan(cli, auth_timeout=0.5)
            elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "planner_auth")
        self.assertIn("within 0.5 s", caught.exception.message)
        self.assertEqual(len(capture.children), 1)
        process = capture.children[0].process
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, delay)  # stopped by the auth time-out, not by finishing its startup
        self.assertEqual(process.returncode, -signal.SIGTERM)  # the bridge's group termination, not a normal exit
        self.assertTrue(process_gone(process.pid))
        self.assertTrue(group_gone(process.pid))
        self.assertFalse((cli.dir / "auth_hanging.pid").exists())
        self.assertEqual(cli.calls(), [])  # killed before the FAKE logged anything

    def test_missing_executable_is_unavailable_without_install(self):
        cli = FakeCli(self)
        cli.path.unlink()
        with self.assertRaises(PlannerError) as caught:
            self.run_plan(cli)
        self.assertEqual(caught.exception.code, "planner_unavailable")
        self.assertIn("nothing is downloaded or installed", caught.exception.message)
        cli.path.write_text("#!/bin/sh\nexit 0\n")
        cli.path.chmod(0o644)  # present but not executable
        with self.assertRaises(PlannerError) as caught:
            self.run_plan(cli)
        self.assertEqual(caught.exception.code, "planner_unavailable")


class StreamTests(BridgeCase):
    def test_model_identity_must_be_exact(self):
        self.assertFails("planner_model_mismatch", "session model", events=stream_events(COMPOUND,
                                                                                          model="claude-fable-5-1"))
        self.assertFails("planner_model_mismatch", "assistant message came from",
                         events=stream_events(COMPOUND, response_model="claude-haiku-5-5"))
        self.assertFails("planner_model_mismatch", "other models",
                         events=stream_events(COMPOUND, usage_models=[MODEL, "claude-haiku-5-5"]))

    def test_api_key_source_tools_and_mcp_are_refused(self):
        self.assertFails("planner_api_key_source", "apiKeySource", events=stream_events(
            COMPOUND, api_key_source="ANTHROPIC_API_KEY"))
        self.assertFails("planner_tools_enabled", "tools", events=stream_events(COMPOUND, tools=["Bash", "Read"]))
        self.assertFails("planner_tools_enabled", "MCP", events=stream_events(COMPOUND, mcp_servers=[{"name": "x"}]))
        self.assertFails("planner_tool_use", "Bash", events=stream_events(COMPOUND, tool_name="Bash"))
        self.assertFails("planner_tool_use", "server_tool_use", events=stream_events(
            COMPOUND, extra_blocks=[{"type": "server_tool_use", "name": "web_search"}]))

    def test_truncated_malformed_and_incomplete_streams(self):
        events = stream_events(COMPOUND)
        truncated = [json.dumps(events[0]), json.dumps(events[1]), json.dumps(events[3])[:120]]
        self.assertFails("planner_malformed_output", "not strict JSON", events=truncated)
        self.assertFails("planner_malformed_output", "without a final result", events=events[:3])
        self.assertFails("planner_malformed_output", "no init event", events=[events[2], events[3]])
        self.assertFails("planner_malformed_output", "before the init", events=[events[1], events[0], events[3]])
        self.assertFails("planner_malformed_output", "no assistant message", events=[events[0], events[3]])
        self.assertFails("planner_malformed_output", "after its final result", events=events + [events[2]])
        self.assertFails("planner_malformed_output", "second session", events=[events[0]] + events)
        self.assertFails("planner_malformed_output", "not strict JSON", events=[json.dumps(events[0]),
                                                                                 '{"type": "assistant", "a": NaN}'])
        self.assertFails("planner_malformed_output", "not strict JSON", events=[
            json.dumps(events[0]), '{"type": "assistant", "type": "result"}'])

    def test_structured_output_missing_error_result_and_exit_status(self):
        self.assertFails("planner_output_missing", "structured_output",
                         events=stream_events(COMPOUND, structured=False))
        self.assertFails("planner_output_missing", "structured_output", events=stream_events(["not", "an object"]))
        self.assertFails("planner_error_result", "error_max_structured_output_retries", events=stream_events(
            COMPOUND, subtype="error_max_structured_output_retries", is_error=True), exit=1)
        error, _ = self.assertFails("planner_exit_status", "status 7", events=stream_events(COMPOUND), exit=7,
                                    stderr="FAKE stderr detail\n")
        self.assertIn("FAKE stderr detail", error.message)

    def test_oversized_plan_and_output_flood(self):
        huge = dict(COMPOUND, summary="x" * (300 << 10))
        self.assertFails("planner_output_limit", "structured plan", events=stream_events(huge))
        cli = FakeCli(self, flood=True)
        started = time.monotonic()
        with self.assertRaises(PlannerError) as caught:
            self.run_plan(cli, max_stdout_bytes=200_000)
        self.assertEqual(caught.exception.code, "planner_output_limit")
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(process_gone(cli.calls()[-1]["pid"]))


class LifecycleTests(BridgeCase):
    def test_timeout_kills_the_whole_group_including_a_grandchild_ignoring_sigterm(self):
        cli = FakeCli(self, hang=True, grandchild=True, ignore_term=True, events=stream_events(COMPOUND),
                      hang_after=1)
        started = time.monotonic()
        with self.assertRaises(PlannerError) as caught:
            self.run_plan(cli, timeout=1.0, grace=0.3)
        self.assertEqual(caught.exception.code, "planner_timeout")
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(process_gone(cli.read_pid("hanging.pid")))
        self.assertTrue(process_gone(cli.read_pid("grandchild.pid")))
        self.assertFalse(os.path.exists(cli.calls()[-1]["cwd"]))

    def test_cancel_and_shutdown_terminate_the_running_child(self):
        for how in ("cancel", "shutdown"):
            with self.subTest(how=how):
                cli = FakeCli(self, hang=True, grandchild=True)
                planner = cli.planner(timeout=60, grace=0.3)
                cancel = threading.Event()
                outcome = {}

                def target():
                    try:
                        planner.run(PROMPT, cli_schema(), cancel)
                    except PlannerError as exc:
                        outcome["error"] = exc

                thread = threading.Thread(target=target)
                thread.start()
                pid = cli.read_pid("hanging.pid")
                grandchild = cli.read_pid("grandchild.pid")
                cancel.set() if how == "cancel" else planner.shutdown()
                thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome["error"].code, "planner_cancelled")
                self.assertTrue(process_gone(pid))
                self.assertTrue(process_gone(grandchild))
                if how == "shutdown":
                    with self.assertRaises(PlannerError) as caught:
                        planner.run(PROMPT, cli_schema())
                    self.assertEqual(caught.exception.code, "planner_cancelled")

    def test_oversized_prompt_is_refused_before_any_process_starts(self):
        cli = FakeCli(self, events=stream_events(COMPOUND))
        with self.assertRaises(PlannerError) as caught:
            cli.planner().run("x" * ((1 << 20) + 1), cli_schema())
        self.assertEqual(caught.exception.code, "planner_input_limit")
        self.assertEqual(cli.calls(), [])


if __name__ == "__main__":
    unittest.main()
