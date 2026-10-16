"""Read-only Ask through the interactive service and its loopback HTTP routes.

    .venv-cad/bin/python -m unittest tests.test_assistant_service -v

Real core, storage, worker process and HTTP transport on fresh temporary
project copies; LABELED FAKE generator suite (``tests.test_interactive_fakes``)
and LABELED FAKE planners: the in-process ``AskPlanner``
(``tests.test_assistant_fakes``) and a fake ``claude`` executable driven
through the real subprocess bridge (``tests.test_planning_fakes.FakeCli``).
These show read-only behaviour, source labelling, staleness, limits,
cancellation and cleanup; they are not evidence that the real model answers
well. No example or inspection artifact is opened for writing.

``PersistedSourceTests`` cover the actual persisted source: another writer's
validated atomic save seen by an Ask or cancel-ask that is the FIRST request
afterwards (no state poll), slow answers stamped against the file, unreadable,
invalid, foreign and locked files failing closed, and lock responsiveness.
They are regressions for an earlier in-memory behaviour that a development
probe (not included) showed failing.

``PreflightCancelTests`` cover cancel-ask and shutdown before any model
starts: while a writer holds the storage lock, after the source read, and
immediately before the bridge call; bounded request tracking and its release
on every exit. They are regressions for an earlier failure seen in
development (cancel answered 409, then one bridge call still ran).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import itertools
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from skid import assistant
from skid.assistant import RESPONSE_FIELDS
from skid.core import canonical_digest, load_document, save_document
from skid.core.commands import apply_command
from skid.core.storage import LOCK_SUFFIX
from skid.interactive import integrity
from skid.interactive import service as service_module
from skid.interactive.server import make_server
from skid.interactive.service import MAX_ASKS_IN_PROGRESS, InteractiveService, ServiceError, ServiceStartError
from skid.planning.bridge import MODEL, ClaudeCliPlanner, PlannerConfig, PlannerError
from tests.test_assistant_fakes import (AskPlanner, answer, branched_document, cyclic_document, is_ask_schema,
                                        statement, write_document)
from tests.test_equipment import drum_document
from tests.test_interactive_fakes import FAKE_SUITE, command, new_project, wait_terminal
from tests.test_interactive_http import Client
from tests.test_planning_fakes import COMPOUND, FakeCli, process_gone, stream_events

COUNTER = itertools.count(1)
MODEL_QUESTION = "Why is there a check valve after P-101?"
WRITE_PATHS = ("_commit", "_request_build_locked", "_start_locked", "_publish_locked", "_finish_unstarted_locked",
               "_remember_proposal_locked", "_adopt_locked", "_refresh_from_disk", "_read_back_signature", "state")


def tree(root):
    """``{relative path: sha256}`` of every file below ``root``."""
    found = {}
    for directory, subdirs, names in os.walk(root):
        subdirs.sort()
        for name in sorted(names):
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = hashlib.sha256(handle.read()).hexdigest()
    return found


def replace_bytes(path, data):
    """Atomically replace ``path`` with arbitrary bytes, as a careless external writer might (no validation)."""
    temporary = f"{path}.test.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


class AskServiceCase(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)

    def service(self, planner=None, path=None, **options):
        service = InteractiveService(str(path or self.path), suite=FAKE_SUITE, suite_options={}, timeout=60,
                                     terminate_grace=0.5, planner=planner or AskPlanner(plan=COMPOUND), **options)
        self.addCleanup(service.close)
        return service

    @staticmethod
    def source(service):
        with service._lock:  # white-box read without state(), which may refresh from disk
            return service.document["current"]["revision"], service.digest

    def body(self, service, question, selected=(), request_id=None, **extra):
        revision, digest = self.source(service)
        return dict({"request_id": request_id or f"req-{next(COUNTER)}", "question": question,
                     "selected_ids": list(selected), "expected_revision": revision, "input_digest": digest}, **extra)

    def background(self, function, *args):
        outcome = {}

        def target():
            try:
                outcome["value"] = function(*args)
            except Exception as exc:  # noqa: BLE001 - surfaced to the test
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        return thread, outcome

    def assertRejected(self, call, status, code):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.status, status, caught.exception.error)
        self.assertIn(code, [d.get("code") for d in caught.exception.diagnostics], caught.exception.diagnostics)
        return caught.exception

    def assertDto(self, response):
        self.assertEqual(list(response), list(RESPONSE_FIELDS))
        json.dumps(response, allow_nan=False)
        return response

    @staticmethod
    def white_box(service):
        """Service fields an Ask or cancel-ask must leave unchanged, read WITHOUT state() (whose disk refresh adopts
        an external change and queues a build), plus the workflow tree."""
        with service._lock:
            document = service.document
            fields = {"revision": document["current"]["revision"], "digest": service.digest,
                      "undo": len(document["undo"]), "redo": len(document["redo"]), "builds": list(service._builds),
                      "current": service.current, "active": service.active, "queued": service.queued,
                      "commands": list(service._commands), "last_command": service._last_command,
                      "saved": service._saved, "notices": list(service._notices),
                      "proposals": list(service._proposals), "disk_sig": service._disk_sig,
                      "disk_problem": service._disk_problem, "selection": dict(service.selection)}
        fields["workflow"] = tree(service.root)
        return fields

    def frozen(self, service):
        """Everything an Ask must leave unchanged: file bytes, workflow tree, history, builds, proposals, plans."""
        state = service.state()
        return dict(self.white_box(service), file=hashlib.sha256(Path(service.path).read_bytes()).hexdigest(),
                    history=state["history"], published=state["published"], sync=state["sync"]["state"],
                    plans=state["planning"]["active"], asks=dict(service._asks),
                    on_disk=load_document(service.path)["current"]["revision"])

    def external_save(self, tag):
        """Another writer (for example the CLI) renames P-101: a validated, locked, atomic ``save_document``.
        Returns the persisted ``(revision, digest)``; the service is not told."""
        document = load_document(str(self.path))
        revision = document["current"]["revision"]
        new, _ = apply_command(document, {"id": f"cmd-external-{next(COUNTER)}", "expected_revision": revision,
                                          "type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": tag}})
        save_document(str(self.path), new, revision)
        current = load_document(str(self.path))["current"]
        return current["revision"], canonical_digest(current)

    def http(self, service):
        server = make_server(service, 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return Client(server.port)

    @contextlib.contextmanager
    def no_writes(self, service):
        """Every command, save, history, proposal, refresh/adoption, state and build path raises while the block
        runs. Reading the project file (``load_document``) stays allowed: it is how Ask checks the source."""
        with contextlib.ExitStack() as stack:
            for name in WRITE_PATHS:
                stack.enter_context(mock.patch.object(service, name, side_effect=AssertionError(
                    f"Ask reached {name}")))
            for name in ("apply_command", "save_document"):
                stack.enter_context(mock.patch(f"skid.interactive.service.{name}", side_effect=AssertionError(
                    f"Ask reached {name}")))
            for name in ("write_atomic", "file_lock"):
                stack.enter_context(mock.patch.object(integrity, name, side_effect=AssertionError(
                    f"Ask reached integrity.{name}")))
            yield


class ReadOnlyTests(AskServiceCase):
    def test_every_ask_outcome_leaves_document_history_publication_and_builds_unchanged(self):
        planner = AskPlanner(plan=COMPOUND)
        service = self.service(planner)
        service.request_build("test publication")
        wait_terminal(service)
        before = self.frozen(service)
        self.assertEqual((before["sync"], before["published"]["source_revision"]), ("synchronized", 0))
        local_questions = [("What is HV-101?", (), "answered"), ("What is upstream and downstream?", ("ent-pump",),
                                                                 "answered"),
                           ("trace downstream of TK-101", (), "answered"), ("What is C-101?", (), "clarification"),
                           ("What is it?", ("ent-pump", "ent-tank"), "clarification"),
                           ("rename P-101 to P-201", (), "clarification"),
                           ("Move P-101 to x 2 m and rebuild", ("ent-pump",), "clarification"),
                           ("What was P-101 called before the rename?", (), "clarification"),
                           ("Is HV-101 upstream of P-101?", (), "answered"), ("List all equipment", (), "answered")]
        model_cases = [(answer(), None, "answered"),
                       (answer("change_request", summary="That is an edit.", statements=[]), None, "clarification"),
                       (answer("clarification", statements=[], clarification_question="Which?"), None, "clarification"),
                       (answer(statements=[statement("recorded_fact", "FAKE", "ent-ghost")]), None, "unavailable"),
                       (dict(answer(), operations=[{"op": "rename_entity", "args": {"entity_id": "ent-pump",
                                                                                     "tag": "P-666"}}],
                             commands=[{"type": "save"}]), None, "unavailable"),
                       (None, PlannerError("planner_unavailable", "FAKE CLI missing"), "unavailable"),
                       (None, RuntimeError("FAKE bridge crash"), "unavailable")]
        bodies = [(self.body(service, q, s), status) for q, s, status in local_questions]
        model_bodies = [self.body(service, MODEL_QUESTION) for _ in model_cases]
        with self.no_writes(service):
            for body, status in bodies:
                with self.subTest(question=body["question"]):
                    response = self.assertDto(service.ask(body))
                    self.assertEqual(response["status"], status, response["answer"])
                    self.assertFalse(response["responder"]["model_called"])
            self.assertEqual(planner.calls, [])  # local questions never reach the bridge
            for body, (value, error, status) in zip(model_bodies, model_cases):
                planner.answer, planner.ask_error = value, error
                with self.subTest(model=status, error=repr(error)[:40], value=str(value)[:60]):
                    response = self.assertDto(service.ask(body))
                    self.assertEqual(response["status"], status, response["answer"])
                    self.assertTrue(response["responder"]["model_called"])
                    self.assertNotIn("P-666", json.dumps(response))
            cancel_body = self.body(service, MODEL_QUESTION)
            planner.answer, planner.ask_error = answer(), None
            planner.ask_gate.clear()
            planner.ask_entered.clear()
            thread, outcome = self.background(service.ask, cancel_body)
            self.assertTrue(planner.ask_entered.wait(10))
            ack = service.cancel_ask({"request_id": cancel_body["request_id"]})
            self.assertEqual(ack, {"request_id": cancel_body["request_id"], "cancel_requested": True})
            thread.join(10)
            self.assertEqual(outcome["value"]["status"], "cancelled")
            with self.assertRaisesRegex(AssertionError, "_commit"):  # the guards are live: a real command trips one
                command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-666"}, revision=0)
            with self.assertRaisesRegex(AssertionError, "_request_build_locked"):
                service.request_build("guard check")
            with self.assertRaisesRegex(AssertionError, "_refresh_from_disk"):
                service.build({"force": True})
            with self.assertRaisesRegex(AssertionError, "Ask reached state"):
                service.state()
        self.assertEqual(len(planner.asks()), len(model_cases) + 1)
        self.assertTrue(all(c["ask"] for c in planner.calls))  # never the engineering-plan schema
        self.assertEqual(self.frozen(service), before)


class PersistedSourceTests(AskServiceCase):
    """The persisted project file, not the service's in-memory copy, decides Ask currentness (regression for a
    review blocker in which Ask judged currentness from the in-memory copy)."""

    def test_first_request_after_an_external_save_is_stale_with_no_side_effects(self):
        # Before the fix this FIRST request returned HTTP 200 describing P-101 as current revision 0 with stale
        # false, although the persisted project was revision 1 with P-999.
        planner = AskPlanner()
        service = self.service(planner)
        client = self.http(service)
        before = self.white_box(service)
        r1 = self.external_save("P-999")
        persisted = Path(self.path).read_bytes()
        for question in ("What is P-101?", MODEL_QUESTION):  # a local and a model question, r0 revision/digest
            with self.subTest(question=question):
                status, error, _ = client.json("POST", "/api/ask", self.body(service, question))
                self.assertEqual((status, [d["code"] for d in error["diagnostics"]]), (409, ["stale_ask"]))
                self.assertIn("project file is at revision 1", error["error"])
        self.assertEqual(planner.calls, [])  # nothing reached the model
        self.assertEqual(self.white_box(service), before)  # nothing adopted, built, saved, recorded or published
        self.assertEqual(Path(self.path).read_bytes(), persisted)
        current = dict(self.body(service, "What is P-999?"), expected_revision=r1[0], input_digest=r1[1])
        status, response, _ = client.json("POST", "/api/ask", current)
        self.assertEqual((status, response["status"], response["source_revision"], response["input_digest"],
                          response["current_revision"], response["stale"]), (200, "answered", 1, r1[1], 1, False))
        self.assertEqual((response["references"][0]["id"], response["references"][0]["tag"]), ("ent-pump", "P-999"))
        self.assertIn("P-999 (ent-pump) is a centrifugal pump", response["answer"])
        model = dict(self.body(service, "Why is there a check valve after P-999?"), expected_revision=r1[0],
                     input_digest=r1[1])
        status, response, _ = client.json("POST", "/api/ask", model)
        self.assertEqual((status, response["status"], response["responder"]["language_model"],
                          response["source_revision"], response["stale"]), (200, "answered", True, 1, False))
        context = json.loads(planner.asks()[0]["prompt"].split("\nAsk context (JSON):\n", 1)[1])
        self.assertEqual((context["project"]["revision"], context["project"]["input_digest"]), r1)
        self.assertIn("P-999", planner.asks()[0]["prompt"])
        self.assertEqual(self.white_box(service), before)  # the service still holds revision 0 and built nothing
        state = service.state()  # ordinary polling adopts the external save and builds it, exactly as before
        self.assertEqual((state["project"]["revision"], state["build"]["source_revision"]), (1, 1))
        self.assertIn("external_change", [d["code"] for d in state["diagnostics"]])
        wait_terminal(service)

    def test_slow_answer_is_stamped_against_the_persisted_file_without_a_poll(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner)
        _, digest = self.source(service)
        before = self.white_box(service)
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, ["ent-pump"]))
        self.assertTrue(planner.ask_entered.wait(10))
        self.external_save("P-998")  # succeeds while the model runs: no storage or service lock is held
        planner.ask_gate.set()  # the answer returns directly; nothing polls state
        thread.join(10)
        response = self.assertDto(outcome["value"])
        self.assertEqual((response["status"], response["source_revision"], response["input_digest"],
                          response["current_revision"], response["stale"]), ("answered", 0, digest, 1, True))
        self.assertIn("now at revision 1", response["limitations"][0])
        self.assertIn("nothing was resent", response["limitations"][0])
        self.assertEqual(response["references"][0]["tag"], "P-101")  # the snapshot's tag, not P-998
        self.assertEqual(len(planner.asks()), 1)
        self.assertEqual(self.white_box(service), before)

    def test_cancel_ask_first_after_an_external_save_acknowledges_without_refresh_or_build(self):
        planner = AskPlanner(plan=COMPOUND, ask_gated=True, plan_gated=True)
        service = self.service(planner)
        client = self.http(service)
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, request_id="req-first"))
        self.assertTrue(planner.ask_entered.wait(10))
        plan_thread, plan_outcome = self.background(service.propose, {"text": "FAKE engineering request",
                                                                      "expected_revision": 0, "mode": "engineering"})
        self.assertTrue(planner.plan_entered.wait(10))
        before = self.white_box(service)
        self.external_save("P-997")
        status, ack, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-first"})
        self.assertEqual((status, ack), (200, {"request_id": "req-first", "cancel_requested": True}))
        thread.join(10)
        cancelled = self.assertDto(outcome["value"])
        self.assertEqual((cancelled["status"], cancelled["request_id"], cancelled["source_revision"],
                          cancelled["current_revision"], cancelled["stale"]), ("cancelled", "req-first", 0, 1, True))
        self.assertEqual(self.white_box(service), before)  # nothing adopted, no build queued, nothing recorded
        self.assertEqual(planner.cancelled, ["ask"])  # only that request: the engineering plan keeps running
        with service._lock:
            self.assertEqual([plan["cancel"].is_set() for plan in service._plans.values()], [False])
        status, error, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-first"})
        self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "ask_not_running"))
        planner.plan_gate.set()
        plan_thread.join(10)
        self.assertTrue(plan_outcome["value"]["stale"])  # the plan's own (unchanged) source re-check adopts r1
        wait_terminal(service)

    def test_unreadable_invalid_or_missing_persisted_source_fails_closed(self):
        planner = AskPlanner()
        service = self.service(planner)
        good = Path(self.path).read_bytes()
        document = json.loads(good)
        schema = dict(document, schema_version=99)
        dangling = json.loads(good)
        dangling["current"]["model"]["connections"]["conn-suction-2"]["to_port_id"] = "port-missing"
        cases = {"truncated JSON": good[:len(good) // 2], "not UTF-8": b"\xff" + good,
                 "duplicate key": good.replace(b"{", b'{"schema_version": 1, ', 1),
                 "unsupported schema": json.dumps(schema).encode(), "dangling port": json.dumps(dangling).encode()}
        before = self.white_box(service)
        moved = f"{self.path}.moved"

        def refused(label):
            for question in ("What is P-101?", MODEL_QUESTION):  # in-memory revision 0 and its digest
                with self.subTest(case=label, question=question):
                    error = self.assertRejected(lambda: service.ask(self.body(service, question)), 409,
                                                "ask_source_invalid")
                    self.assertIn("does not answer from the service's in-memory revision", error.error)

        for label, data in cases.items():
            replace_bytes(self.path, data)
            refused(label)
        os.replace(self.path, moved)
        refused("missing file")
        os.replace(moved, self.path)
        replace_bytes(self.path, good)
        os.chmod(self.path, 0)
        try:
            if not os.access(self.path, os.R_OK):  # a privileged user would still read it
                refused("mode 000")
        finally:
            os.chmod(self.path, 0o644)
        self.assertEqual(planner.calls, [])
        self.assertEqual(self.white_box(service), before)  # Ask recorded no disk problem and adopted nothing
        response = service.ask(self.body(service, "What is P-101?"))  # repaired: the same revision answers again
        self.assertEqual((response["status"], response["source_revision"], response["stale"]), ("answered", 0, False))

    def test_an_answer_whose_source_cannot_be_rechecked_is_never_current(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner)
        good = Path(self.path).read_bytes()
        other = json.loads(good)
        other["current"]["id"] = "project-other"
        for label, data, why in (("garbage", b"{ not json", "could not be checked after answering"),
                                 ("another project", json.dumps(other).encode(), "now holds another project")):
            with self.subTest(case=label):
                planner.ask_gate.clear()
                planner.ask_entered.clear()
                thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION))
                self.assertTrue(planner.ask_entered.wait(10))
                replace_bytes(self.path, data)
                planner.ask_gate.set()
                thread.join(10)
                response = self.assertDto(outcome["value"])
                self.assertEqual((response["status"], response["source_revision"], response["current_revision"],
                                  response["stale"]), ("answered", 0, None, True))
                self.assertTrue(response["limitations"][0].startswith("Whether revision 0 is still current is "
                                                                      "unknown"), response["limitations"][0])
                self.assertIn(why, response["limitations"][0])
                if label == "another project":  # and a new question about the foreign file is refused as stale
                    self.assertRejected(lambda: service.ask(self.body(service, "What is P-101?")), 409, "stale_ask")
                replace_bytes(self.path, good)
        self.assertEqual(len(planner.asks()), 2)

    def test_source_reads_take_only_a_brief_shared_storage_lock(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner)
        lock_path = f"{self.path}{LOCK_SUFFIX}"
        held, release = threading.Event(), threading.Event()

        def writer():  # another writer's save holds the exclusive storage lock
            with integrity.file_lock(lock_path, 5):
                held.set()
                release.wait(10)

        threading.Thread(target=writer, daemon=True).start()
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        thread, outcome = self.background(service.ask, self.body(service, "What is HV-101?"))
        time.sleep(0.3)
        self.assertNotIn("value", outcome)  # the read waits for the writer ...
        release.set()
        thread.join(10)
        self.assertEqual(outcome["value"]["status"], "answered")  # ... then proceeds
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        with mock.patch("skid.interactive.service.ASK_SOURCE_LOCK_TIMEOUT_S", 0.3), \
                integrity.file_lock(lock_path, 5):  # a writer that keeps the lock: fail closed, quickly
            started = time.monotonic()
            self.assertRejected(lambda: service.ask(self.body(service, MODEL_QUESTION)), 409, "ask_source_locked")
            self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(planner.calls, [])
        with service._write_lock:  # a commit or publication holding the service's write lock never blocks Ask
            thread, outcome = self.background(service.ask, self.body(service, "What is upstream of P-101?"))
            thread.join(5)
            self.assertEqual(outcome["value"]["status"], "answered")
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION))
        self.assertTrue(planner.ask_entered.wait(10))
        for lock in (service._write_lock, service._lock):  # no lock is held while the model runs
            self.assertTrue(lock.acquire(timeout=1))
            lock.release()
        with integrity.file_lock(lock_path, 1):  # nor the storage lock: another writer can save meanwhile
            pass
        planner.ask_gate.set()
        thread.join(10)
        self.assertEqual((outcome["value"]["status"], outcome["value"]["stale"]), ("answered", False))

    def test_a_missing_storage_lock_file_is_never_created_by_ask(self):
        service = self.service()
        lock_path = Path(f"{self.path}{LOCK_SUFFIX}")
        lock_path.unlink()  # for example a project copied without its lock sidecar
        response = service.ask(self.body(service, "What is P-101?"))
        self.assertEqual((response["status"], response["stale"]), ("answered", False))
        self.assertFalse(lock_path.exists())


class PreflightCancelTests(AskServiceCase):
    """An Ask is tracked from acceptance, before its source read can wait for a writer, so cancel-ask and close()
    stop it before any model starts (fix for task-a-fix-extra-findings.md). Before the fix a cancel during the source
    wait was 409 ``ask_not_running`` and the bridge ran once after the writer released."""

    def hold_writer_lock(self):
        """Another writer's save in progress: the exclusive storage lock, taken with a raw ``flock`` so it can be
        released while the ``no_writes`` guards (which patch ``integrity.file_lock``) are live. Returns release()."""
        held = {"fd": os.open(f"{self.path}{LOCK_SUFFIX}", os.O_RDWR)}
        fcntl.flock(held["fd"], fcntl.LOCK_EX)

        def release():
            fd = held.pop("fd", None)
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        self.addCleanup(release)
        return release

    def observe_source_reads(self):
        """Report which threads have begun waiting for the storage lock (black-box sync, as in root's probe).
        Returns ``wait(count)``."""
        entered, condition = set(), threading.Condition()
        real = service_module._shared_lock

        @contextlib.contextmanager
        def observed(path, timeout):
            with condition:
                entered.add(threading.get_ident())
                condition.notify_all()
            with real(path, timeout):
                yield

        patcher = mock.patch.object(service_module, "_shared_lock", observed)
        patcher.start()
        self.addCleanup(patcher.stop)

        def wait(count=1, timeout=5):
            with condition:
                return condition.wait_for(lambda: len(entered) >= count, timeout)

        return wait

    @staticmethod
    def tracked(service, request_id, phase, timeout=5):
        """Wait until ``request_id`` is tracked in ``phase`` (``source``, ``prepare`` or ``model``)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with service._lock:
                record = service._asks.get(request_id)
                if record is not None and record["phase"] == phase:
                    return record
            time.sleep(0.005)
        raise AssertionError(f"{request_id} was not tracked in phase {phase}")

    def gated_prepare(self):
        """``assistant.prepare`` waits for ``gate`` after the source read; returns ``(entered, gate)``."""
        entered, gate = threading.Event(), threading.Event()
        real = assistant.prepare

        def prepare(*args, **kwargs):
            entered.set()
            gate.wait(10)
            return real(*args, **kwargs)

        patcher = mock.patch("skid.assistant.prepare", prepare)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(gate.set)
        return entered, gate

    def assertStopped(self, outcome, code):
        error = outcome.get("error")
        self.assertIsInstance(error, ServiceError, outcome)
        self.assertEqual((error.status, [d["code"] for d in error.diagnostics]), (409, [code]), error.error)
        self.assertIn("nothing was sent", error.error)
        return error

    def assertSettled(self, service):
        """No tracked Ask, the one model slot free, no live Ask timer."""
        with service._lock:
            self.assertEqual(service._asks, {})
        self.assertTrue(service._ask_slot.acquire(blocking=False))
        service._ask_slot.release()
        timers = [t for t in threading.enumerate()
                  if isinstance(t, threading.Timer) and t.function == service._ask_timed_out]
        for timer in timers:
            timer.join(2)
        self.assertEqual([t for t in timers if t.is_alive()], [])

    def test_cancel_while_a_writer_holds_the_storage_lock_starts_no_model(self):
        planner = AskPlanner(plan=COMPOUND)  # ungated: the FAKE bridge would answer at once if it were ever called
        service = self.service(planner)
        client = self.http(service)
        service.request_build("test publication")
        wait_terminal(service)
        before = self.frozen(service)
        body = self.body(service, MODEL_QUESTION, request_id="req-held")
        release = self.hold_writer_lock()
        waiting = self.observe_source_reads()
        with self.no_writes(service):
            reply = {}
            thread = threading.Thread(target=lambda: reply.update(result=client.json("POST", "/api/ask", body)),
                                      daemon=True)
            thread.start()
            self.addCleanup(thread.join, 15)
            self.assertTrue(waiting(1))
            time.sleep(0.2)
            self.assertNotIn("result", reply)  # waiting for the writer's lock
            cancel_status, ack, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-held"})
            thread.join(5)  # the wait itself observes the cancel: the Ask ends while the writer still holds the lock
            ended_while_held = not thread.is_alive()
            release()  # the writer finishes: nothing may start afterwards
            thread.join(10)
            time.sleep(0.3)
            status, error, _ = reply["result"]
            self.assertEqual({"cancel": (cancel_status, ack), "ended_while_lock_held": ended_while_held,
                              "ask": (status, [d["code"] for d in error.get("diagnostics", [])], error.get("status")),
                              "bridge_calls": len(planner.calls)},
                             {"cancel": (200, {"request_id": "req-held", "cancel_requested": True}),
                              "ended_while_lock_held": True, "ask": (409, ["ask_cancelled"], None), "bridge_calls": 0})
            self.assertEqual(error["error"], "the question was cancelled before any model started; nothing was sent "
                                             "to a model and nothing was changed")
            status, error, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-held"})
            self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "ask_not_running"))
        self.assertEqual(planner.calls, [])  # ZERO bridge calls
        self.assertSettled(service)
        self.assertEqual(self.frozen(service), before)  # no command, save, history, build, publication or proposal
        again = service.ask(self.body(service, MODEL_QUESTION, request_id="req-held"))  # ID and slot are free
        self.assertEqual((again["status"], len(planner.asks())), ("answered", 1))

    def test_cancel_after_the_source_read_and_before_the_model_starts_nothing(self):
        planner = AskPlanner()
        service = self.service(planner)
        entered, gate = self.gated_prepare()
        for question in (MODEL_QUESTION, "What is P-101?"):  # an acknowledged cancel wins over a local answer too
            with self.subTest(question=question):
                entered.clear()
                gate.clear()
                body = self.body(service, question)
                thread, outcome = self.background(service.ask, body)
                self.assertTrue(entered.wait(5))
                ack = service.cancel_ask({"request_id": body["request_id"]})
                self.assertEqual(ack, {"request_id": body["request_id"], "cancel_requested": True})
                gate.set()
                thread.join(5)
                self.assertStopped(outcome, "ask_cancelled")
                self.assertSettled(service)
        real_slot = service._ask_slot

        class CancellingSlot:  # the cancel lands after the last preparation check, as the model slot is taken
            def acquire(self, blocking=True):
                service.cancel_ask({"request_id": "req-edge"})
                return real_slot.acquire(blocking)

            def release(self):
                real_slot.release()

        with mock.patch.object(service, "_ask_slot", CancellingSlot()):
            error = self.assertRejected(lambda: service.ask(self.body(service, MODEL_QUESTION, request_id="req-edge")),
                                        409, "ask_cancelled")
        self.assertIn("nothing was sent", error.error)
        self.assertEqual(planner.calls, [])
        self.assertSettled(service)
        # Once a local answer is settled a cancel is refused, never acknowledged and then ignored.
        entered.clear()
        gate.clear()
        thread, outcome = self.background(service.ask, self.body(service, "What is HV-101?", request_id="req-local"))
        self.assertTrue(entered.wait(5))
        release = self.hold_writer_lock()  # keeps the settled answer in its post-answer source check
        gate.set()
        deadline = time.monotonic() + 5
        while "req-local" in service._asks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertRejected(lambda: service.cancel_ask({"request_id": "req-local"}), 409, "ask_not_running")
        release()
        thread.join(5)
        self.assertEqual((outcome["value"]["status"], outcome["value"]["stale"]), ("answered", False))
        self.assertEqual(planner.calls, [])

    def test_close_stops_an_ask_before_any_model_starts(self):
        planner = AskPlanner()
        service = self.service(planner)
        release = self.hold_writer_lock()
        waiting = self.observe_source_reads()
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, request_id="req-closing"))
        self.assertTrue(waiting(1))
        time.sleep(0.1)
        started = time.monotonic()
        service.close()
        thread.join(5)
        self.assertLess(time.monotonic() - started, 3)  # stopped while the writer still holds the lock
        error = self.assertStopped(outcome, "ask_closed")
        self.assertIn("shutting down", error.error)
        release()
        time.sleep(0.2)
        self.assertEqual(planner.calls, [])
        self.assertSettled(service)
        self.assertRejected(lambda: service.ask(self.body(service, "What is P-101?")), 409, "ask_closed")
        other = self.service(planner)  # and at the last check immediately before the bridge call
        real_slot = other._ask_slot

        class ClosingSlot:
            def acquire(self, blocking=True):
                other.close()
                return real_slot.acquire(blocking)

            def release(self):
                real_slot.release()

        with mock.patch.object(other, "_ask_slot", ClosingSlot()):
            self.assertRejected(lambda: other.ask(self.body(other, MODEL_QUESTION)), 409, "ask_closed")
        self.assertEqual(planner.calls, [])
        self.assertSettled(other)

    def test_request_tracking_is_bounded_and_released_on_every_exit(self):
        planner = AskPlanner()
        service = self.service(planner)
        release = self.hold_writer_lock()
        bodies = [self.body(service, MODEL_QUESTION if i % 2 else "What is P-101?", request_id=f"req-cap-{i}")
                  for i in range(MAX_ASKS_IN_PROGRESS)]
        waiting = [self.background(service.ask, body) for body in bodies]
        for body in bodies:
            self.tracked(service, body["request_id"], "source")
        started = time.monotonic()
        error = self.assertRejected(lambda: service.ask(self.body(service, "What is HV-101?")), 409, "ask_busy")
        self.assertIn(f"{MAX_ASKS_IN_PROGRESS} Asks are already in progress", error.error)
        self.assertRejected(lambda: service.ask(dict(bodies[0])), 409, "ask_busy")  # the same ID is in progress
        self.assertLess(time.monotonic() - started, 1)  # refused at once, without waiting for the writer
        for body in bodies:
            self.assertEqual(service.cancel_ask({"request_id": body["request_id"]})["cancel_requested"], True)
        for thread, outcome in waiting:
            thread.join(5)
            self.assertStopped(outcome, "ask_cancelled")
        release()
        self.assertEqual(planner.calls, [])
        self.assertSettled(service)
        # Nothing is remembered for an unknown ID: a later Ask with that ID runs normally.
        self.assertRejected(lambda: service.cancel_ask({"request_id": "req-later"}), 409, "ask_not_running")
        self.assertEqual(service._asks, {})
        self.assertEqual(service.ask(self.body(service, "What is P-101?", request_id="req-later"))["status"],
                         "answered")
        good = Path(self.path).read_bytes()

        def locked():
            with mock.patch("skid.interactive.service.ASK_SOURCE_LOCK_TIMEOUT_S", 0.2):
                hold = self.hold_writer_lock()
                try:
                    service.ask(self.body(service, MODEL_QUESTION))
                finally:
                    hold()

        def invalid_source():
            replace_bytes(self.path, b"{ not json")
            try:
                service.ask(self.body(service, MODEL_QUESTION))
            finally:
                replace_bytes(self.path, good)

        def prepare_fault():
            with mock.patch("skid.assistant.prepare", side_effect=RuntimeError("FAKE prepare fault")):
                service.ask(self.body(service, MODEL_QUESTION))

        for label, call, status, code in (
                ("stale", lambda: service.ask(self.body(service, MODEL_QUESTION, expected_revision=5)), 409,
                 "stale_ask"),
                ("unknown selection", lambda: service.ask(self.body(service, MODEL_QUESTION, ["ent-ghost"])), 400,
                 "invalid_ask"),
                ("locked", locked, 409, "ask_source_locked"), ("invalid", invalid_source, 409, "ask_source_invalid")):
            with self.subTest(exit=label):
                self.assertRejected(call, status, code)
                self.assertSettled(service)
        with self.assertRaisesRegex(RuntimeError, "FAKE prepare fault"):
            prepare_fault()
        self.assertSettled(service)
        self.assertEqual(planner.calls, [])
        planner.ask_error = RuntimeError("FAKE bridge crash")
        self.assertEqual(service.ask(self.body(service, MODEL_QUESTION))["status"], "unavailable")
        self.assertSettled(service)
        planner.ask_error = None
        planner.ask_gate.clear()
        planner.ask_entered.clear()
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, request_id="req-running"))
        self.assertTrue(planner.ask_entered.wait(5))
        self.assertEqual(self.tracked(service, "req-running", "model")["request_id"], "req-running")
        self.assertRejected(lambda: service.ask(self.body(service, MODEL_QUESTION)), 409, "ask_busy")  # one slot
        self.assertEqual(service.ask(self.body(service, "What is HV-101?"))["status"], "answered")  # local: no slot
        with service._lock:
            self.assertEqual(list(service._asks), ["req-running"])  # the refused and local Asks left no record
        service.cancel_ask({"request_id": "req-running"})
        thread.join(5)
        self.assertEqual(outcome["value"]["status"], "cancelled")
        self.assertSettled(service)
        planner.ask_gate.set()
        self.assertEqual(service.ask(self.body(service, MODEL_QUESTION))["status"], "answered")
        self.assertSettled(service)
        self.assertEqual(len(planner.asks()), 3)


class SourceTests(AskServiceCase):
    def test_stale_preflight_is_refused_before_any_model_call(self):
        planner = AskPlanner()
        service = self.service(planner)
        revision, digest = self.source(service)
        stale = self.body(service, MODEL_QUESTION, expected_revision=revision + 1)
        self.assertRejected(lambda: service.ask(stale), 409, "stale_ask")
        wrong = self.body(service, MODEL_QUESTION, input_digest="0" * 64)
        self.assertRejected(lambda: service.ask(wrong), 409, "stale_ask")
        command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        old = dict(self.body(service, "What is P-201?"), expected_revision=revision, input_digest=digest)
        self.assertRejected(lambda: service.ask(old), 409, "stale_ask")
        self.assertEqual(planner.calls, [])
        fresh = service.ask(self.body(service, "What is P-201?"))
        self.assertEqual((fresh["status"], fresh["source_revision"], fresh["references"][0]["id"]),
                         ("answered", 1, "ent-pump"))
        old_tag = service.ask(self.body(service, "What is P-101?"))
        self.assertEqual((old_tag["status"], old_tag["responder"]["query"]), ("clarification", "unknown"))
        wait_terminal(service)

    def test_slow_model_answer_stays_labelled_with_its_snapshot_without_resend(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner)
        _, digest = self.source(service)
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, ["ent-pump"]))
        self.assertTrue(planner.ask_entered.wait(10))
        started = time.monotonic()
        service.state()  # polling, selection, writes and local answers continue: no lock is held
        service.select({"semantic_ids": ["ent-pump"], "origin": "pid"})
        state = command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        self.assertEqual(state["project"]["revision"], 1)
        local = service.ask(self.body(service, "What is upstream and downstream of P-201?"))
        self.assertEqual((local["status"], local["source_revision"]), ("answered", 1))
        quick = service.propose({"text": "rename P-201 to P-301", "expected_revision": 1})
        self.assertTrue(quick["supported"])
        self.assertLess(time.monotonic() - started, 5)
        planner.ask_gate.set()
        thread.join(10)
        response = self.assertDto(outcome["value"])
        self.assertEqual((response["status"], response["source_revision"], response["input_digest"],
                          response["current_revision"], response["stale"]), ("answered", 0, digest, 1, True))
        self.assertIn("now at revision 1", response["limitations"][0])
        self.assertIn("nothing was resent", response["limitations"][0])
        self.assertEqual(response["references"][0]["tag"], "P-101")  # snapshot tag, not rewritten after rename
        self.assertIn("[P-101]", response["answer"])
        self.assertEqual(len(planner.asks()), 1)  # no automatic resend
        wait_terminal(service)

    def test_visible_published_source_is_echoed_and_labelled(self):
        service = self.service()
        service.request_build("test publication")
        state = wait_terminal(service)
        published = {"source_revision": state["published"]["source_revision"],
                     "input_digest": state["published"]["input_digest"], "mode": "published"}
        state = command(service, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-201"})
        response = service.ask(self.body(service, "What is TK-201?", displayed_source=published))
        self.assertEqual(response["displayed_source"], published)
        self.assertIn("Answer: current model r1; visible published content r0.", " ".join(response["limitations"]))
        wait_terminal(service)


class LimitTests(AskServiceCase):
    def test_one_model_ask_at_a_time_tracked_apart_from_engineering_plans(self):
        planner = AskPlanner(plan=COMPOUND, ask_gated=True, plan_gated=True)
        service = self.service(planner)
        ask_body = self.body(service, MODEL_QUESTION, request_id="req-slow-ask")
        ask_thread, ask_outcome = self.background(service.ask, ask_body)
        self.assertTrue(planner.ask_entered.wait(10))
        self.assertRejected(lambda: service.ask(self.body(service, "Why is HV-101 there?")), 409, "ask_busy")
        self.assertRejected(lambda: service.ask(self.body(service, MODEL_QUESTION, request_id="req-slow-ask")), 409,
                            "ask_busy")
        local = service.ask(self.body(service, "What is HV-101?"))
        self.assertEqual(local["status"], "answered")  # deterministic answers need no model slot
        plan_thread, plan_outcome = self.background(service.propose, {"text": "FAKE engineering request",
                                                                      "expected_revision": 0, "mode": "engineering"})
        self.assertTrue(planner.plan_entered.wait(10))
        (active,) = service.state()["planning"]["active"]
        self.assertEqual(list(service._asks), ["req-slow-ask"])
        self.assertRejected(lambda: service.cancel_proposal({"proposal_id": "req-slow-ask"}), 409, "plan_not_running")
        self.assertRejected(lambda: service.cancel_ask({"request_id": active["id"]}), 409, "ask_not_running")
        ack = service.cancel_ask({"request_id": "req-slow-ask"})
        self.assertEqual(ack, {"request_id": "req-slow-ask", "cancel_requested": True})
        self.assertEqual([p["cancelled"] for p in service.state()["planning"]["active"]], [False])  # plan unaffected
        ask_thread.join(10)
        cancelled = self.assertDto(ask_outcome["value"])
        self.assertEqual((cancelled["status"], cancelled["request_id"], cancelled["responder"]["error"]["code"]),
                         ("cancelled", "req-slow-ask", "ask_cancelled"))
        self.assertEqual(planner.cancelled, ["ask"])
        self.assertEqual(service.state()["planning"]["active"][0]["id"], active["id"])
        self.assertRejected(lambda: service.cancel_ask({"request_id": "req-slow-ask"}), 409, "ask_not_running")
        planner.plan_gate.set()
        plan_thread.join(10)
        self.assertEqual(plan_outcome["value"]["status"], "ready")
        planner.ask_gate.set()
        again = service.ask(self.body(service, MODEL_QUESTION, request_id="req-slow-ask"))  # slot and ID released
        self.assertEqual(again["status"], "answered")

    def test_model_ask_time_limit(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner, ask_timeout=0.5)
        started = time.monotonic()
        response = self.assertDto(service.ask(self.body(service, MODEL_QUESTION)))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((response["status"], response["responder"]["error"]["code"]), ("unavailable", "ask_timeout"))
        self.assertIn("P-101 (ent-pump) is a centrifugal pump", response["answer"])  # local facts remain
        self.assertEqual((planner.cancelled, service._asks), (["ask"], {}))
        planner.ask_gate.set()
        self.assertEqual(service.ask(self.body(service, MODEL_QUESTION))["status"], "answered")
        for bad in (0, -1, True, "5"):
            with self.assertRaisesRegex(ServiceStartError, "ask_timeout"):
                InteractiveService(str(self.path), suite=FAKE_SUITE, planner=planner, ask_timeout=bad)

    def test_invalid_requests_and_unknown_selection(self):
        service = self.service()
        cases = [dict(selected_ids=["ent-ghost"]), dict(selected_ids=["ent-pump"] * 3 + [f"e{i}" for i in range(14)]),
                 dict(question="x" * 4001), dict(question=" "), dict(request_id="bad id"),
                 dict(command={"type": "rename_entity"}), dict(history=[{"question": "q"}]),
                 dict(displayed_source={"source_revision": 0, "input_digest": "x", "mode": "published"})]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                body = dict(self.body(service, "What is it?"), **case)
                self.assertRejected(lambda: service.ask(body), 400, "invalid_ask")
        for body in ({}, {"request_id": 3}, {"request_id": "r", "extra": 1}, ["r"]):
            with self.subTest(cancel=str(body)):
                self.assertRejected(lambda: service.cancel_ask(body), 400, "invalid_ask")
        duplicates = service.ask(self.body(service, "What are these?", ["ent-pump", "ent-pump", "ent-tank"]))
        self.assertEqual(duplicates["status"], "answered")


class CleanupTests(AskServiceCase):
    def test_close_terminates_a_running_model_ask_process_group(self):
        cli = FakeCli(self, hang=True, grandchild=True, ignore_term=True)
        service = self.service(cli.planner(timeout=120, grace=0.3))
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION))
        pid = cli.read_pid("hanging.pid")
        grandchild = cli.read_pid("grandchild.pid")
        self.assertEqual(len(service._asks), 1)
        started = time.monotonic()
        service.close()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 10)
        response = self.assertDto(outcome["value"])
        self.assertEqual(response["status"], "cancelled")
        self.assertIn("shutting down", response["answer"])
        self.assertTrue(process_gone(pid))
        self.assertTrue(process_gone(grandchild))
        self.assertRejected(lambda: service.ask(self.body(service, "What is P-101?")), 409, "ask_closed")

    def test_cancel_and_time_limit_terminate_the_fake_cli_child(self):
        cli = FakeCli(self, hang=True)
        service = self.service(cli.planner(timeout=120, grace=0.2))
        thread, outcome = self.background(service.ask, self.body(service, MODEL_QUESTION, request_id="req-cli"))
        pid = cli.read_pid("hanging.pid")
        service.cancel_ask({"request_id": "req-cli"})
        thread.join(15)
        self.assertEqual(outcome["value"]["status"], "cancelled")
        self.assertTrue(process_gone(pid))
        (cli.dir / "hanging.pid").unlink()
        timed = InteractiveService(str(self.path), service.root + "-timed", suite=FAKE_SUITE, timeout=60,
                                   terminate_grace=0.5, planner=cli.planner(timeout=120, grace=0.2), ask_timeout=0.8)
        self.addCleanup(timed.close)
        response = timed.ask(self.body(timed, MODEL_QUESTION))
        self.assertEqual((response["status"], response["responder"]["error"]["code"]), ("unavailable", "ask_timeout"))
        self.assertTrue(process_gone(cli.read_pid("hanging.pid")))


class BridgeTests(AskServiceCase):
    def test_fake_cli_round_trip_uses_the_tool_free_bridge_with_the_ask_schema(self):
        reply = answer()
        cli = FakeCli(self, events=stream_events(reply), save_prompt=True)
        service = self.service(cli.planner())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "FAKE-test-key-not-a-secret"}):
            response = self.assertDto(service.ask(self.body(service, MODEL_QUESTION, ["ent-pump"])))
        self.assertEqual((response["status"], response["responder"]["kind"]), ("answered", "language_model"))
        bridge = response["responder"]["bridge"]
        self.assertEqual((bridge["verified"], bridge["model"], bridge["api_key_source"], bridge["tools"]),
                         (True, MODEL, "none", []))
        self.assertEqual(bridge["auth"], {"method": "claude.ai", "subscription": "max"})
        self.assertEqual(bridge["effort"], {"requested": "medium", "reported": None})
        self.assertTrue(any("requested effort medium" in n for n in response["limitations"]))
        auth, run = cli.calls()
        argv = run["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertTrue(is_ask_schema(schema))
        self.assertIn("ent-pump", schema["properties"]["statements"]["items"]["properties"]["reference_ids"]["items"]
                      ["enum"])
        self.assertNotIn("operations", json.dumps(schema))
        for call in (auth, run):
            self.assertFalse(call["api_key_present"])
        prompt = (cli.dir / "prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(run["stdin_bytes"], response["responder"]["prompt_bytes"])
        self.assertEqual(hashlib.sha256(prompt.encode("utf-8")).hexdigest(), response["responder"]["prompt_sha256"])
        context = json.loads(prompt.split("\nAsk context (JSON):\n", 1)[1])
        self.assertEqual(context["question"]["text"], MODEL_QUESTION)
        self.assertEqual(context["project"]["revision"], 0)
        self.assertNotIn(str(self.path), prompt)  # no file paths
        self.assertNotIn("FAKE-test-key", prompt)

    def test_unverified_streams_and_a_missing_cli_keep_local_answers_working(self):
        cases = ((FakeCli(self, events=stream_events(answer(), tools=["Bash"])), "planner_tools_enabled"),
                 (FakeCli(self, events=stream_events(answer(), response_model="claude-other")),
                  "planner_model_mismatch"),
                 (FakeCli(self, events=stream_events(answer(), api_key_source="ANTHROPIC_API_KEY")),
                  "planner_api_key_source"))
        for cli, code in cases:
            with self.subTest(code=code):
                service = self.service(cli.planner())
                response = service.ask(self.body(service, MODEL_QUESTION))
                self.assertEqual((response["status"], response["responder"]["error"]["code"]), ("unavailable", code))
                service.close()
        missing = ClaudeCliPlanner(PlannerConfig(cli_path=str(self.path.parent / "no-such-claude")))
        service = self.service(missing)
        general = service.ask(self.body(service, "Why is HV-101 needed?"))
        self.assertEqual((general["status"], general["responder"]["error"]["code"]),
                         ("unavailable", "planner_unavailable"))
        self.assertIn("HV-101 (ent-suction-valve) is an isolation valve", general["answer"])
        for question in ("What is HV-101?", "What is upstream and downstream of P-101?", "trace upstream of OUT-101"):
            with self.subTest(question=question):
                self.assertEqual(service.ask(self.body(service, question))["status"], "answered")


class TopologyTests(AskServiceCase):
    def test_branched_cyclic_and_composed_documents_through_the_service(self):
        branched = self.service(path=write_document(self, branched_document()))
        text = branched.ask(self.body(branched, "trace downstream of P-101"))["answer"]
        self.assertIn("P-101 → L-103 → T-101, which has 2 branches:", text)
        self.assertIn("branch 2: T-101 → L-202 → OUT-102 [end: no further outgoing process connection]", text)
        immediate = branched.ask(self.body(branched, "What is downstream of T-101?"))["answer"]
        self.assertIn("Downstream (outgoing), 2 branches: L-201 from T-101.outlet to NRV-101.inlet; L-202 from "
                      "T-101.outlet_b to OUT-102.inlet.", immediate)
        cyclic = self.service(path=write_document(self, cyclic_document()))
        loop = cyclic.ask(self.body(cyclic, "trace upstream of P-101"))["answer"]
        self.assertIn("P-101 ← L-102 ← HV-101 ← L-199 ← HV-102 ← L-104 ← NRV-101 ← L-103 ← P-101 [loop: returns to "
                      "P-101 on this path; traversal stops]", loop)
        composed = self.service(path=write_document(self, drum_document()))
        drum = composed.ask(self.body(composed, "What is it?", ["ent-drum"]))
        self.assertIn("V-101 (ent-drum) is a composed equipment item", drum["answer"])
        self.assertEqual(drum["references"][0]["id"], "ent-drum")


class HttpTests(AskServiceCase):
    def test_ask_routes_over_loopback(self):
        planner = AskPlanner(ask_gated=True)
        service = self.service(planner)
        client = self.http(service)
        status, response, headers = client.json("POST", "/api/ask", self.body(service, "What is HV-101?"))
        self.assertEqual((status, response["status"]), (200, "answered"))
        self.assertEqual(list(response), list(RESPONSE_FIELDS))
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        status, error, _ = client.json("POST", "/api/ask", self.body(service, "What is P-101?", expected_revision=9))
        self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "stale_ask"))
        status, error, _ = client.json("POST", "/api/ask", dict(self.body(service, "x"), html="<b>"))
        self.assertEqual((status, error["diagnostics"][0]["code"]), (400, "invalid_ask"))
        status, error, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-none"})
        self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "ask_not_running"))
        status, _, _ = client.json("GET", "/api/ask")
        self.assertEqual(status, 404)
        status, _, _ = client.json("POST", "/api/ask", self.body(service, "What is P-101?"),
                                   headers={"Origin": "http://evil.invalid"})
        self.assertEqual(status, 403)
        slow = {}

        def run():
            slow["result"] = client.json("POST", "/api/ask", self.body(service, MODEL_QUESTION, request_id="req-http"))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(planner.ask_entered.wait(10))
        status, ack, _ = client.json("POST", "/api/cancel-ask", {"request_id": "req-http"})
        self.assertEqual((status, ack), (200, {"request_id": "req-http", "cancel_requested": True}))  # never state
        thread.join(15)
        status, response, _ = slow["result"]
        self.assertEqual((status, response["status"], response["request_id"]), (200, "cancelled", "req-http"))
        planner.ask_gate.set()
        status, response, _ = client.json("POST", "/api/ask", self.body(service, MODEL_QUESTION))
        self.assertEqual((status, response["status"], response["responder"]["language_model"]),
                         (200, "answered", True))
        self.assertEqual(client.state()["project"]["revision"], 0)
        replace_bytes(self.path, b"[]")  # an invalid persisted file fails closed through the transport too
        status, error, _ = client.json("POST", "/api/ask", self.body(service, "What is HV-101?"))
        self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "ask_source_invalid"))


if __name__ == "__main__":
    unittest.main()
