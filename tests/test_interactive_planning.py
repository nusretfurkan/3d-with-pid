"""Engineering-plan lifecycle in the interactive service (contract 0.3).

Real core, storage, worker process and HTTP transport; LABELED FAKE generator
suite (``tests.test_interactive_fakes``) and LABELED FAKE planners
(``tests.test_planning_fakes``: an in-process planner and a fake ``claude``
executable). These show atomic apply, staleness, responsiveness, limits and
cleanup; they are not evidence that the real model or CLI planned anything.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import unittest

from skid.core import load_document
from skid.interactive.server import make_server
from skid.interactive.service import InteractiveService, ServiceError
from skid.planning.bridge import PlannerError
from tests.test_interactive_fakes import FAKE_SUITE, command, new_project, wait_terminal
from tests.test_interactive_http import Client
from tests.test_planning_fakes import COMPOUND, FakeCli, FakePlanner, drum_insert, op, plan, process_gone

QUESTION = {"id": "which", "question": "Which line?", "reason": "Two lines are plausible."}


class PlanningServiceCase(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)

    def service(self, planner=None, **options):
        service = InteractiveService(str(self.path), suite=FAKE_SUITE, suite_options={}, timeout=60,
                                     terminate_grace=0.5, planner=planner or FakePlanner(COMPOUND), **options)
        self.addCleanup(service.close)
        return service

    def propose(self, service, text="FAKE engineering request", revision=0):
        return service.propose({"text": text, "expected_revision": revision, "mode": "engineering"})

    def background(self, function, *args):
        outcome = {}

        def target():
            try:
                outcome["value"] = function(*args)
            except Exception as exc:  # noqa: BLE001 - surfaced to the test
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, outcome

    def assertRejected(self, call, status, code=None):
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.status, status, caught.exception.error)
        if code:
            self.assertIn(code, [d.get("code") for d in caught.exception.diagnostics], caught.exception.diagnostics)
        return caught.exception


class ApplyTests(PlanningServiceCase):
    def test_compound_plan_applies_as_one_revision_one_undo_and_one_build(self):
        service = self.service()
        state = wait_terminal(service) if service.state()["build"] else service.state()
        builds_before = len(state["builds"])
        proposal = self.propose(service)
        self.assertEqual((proposal["status"], proposal["mode"], proposal["command_count"], proposal["operation_count"]),
                         ("ready", "engineering", 1, 2))
        self.assertFalse(proposal["validation"]["geometry_checked"])
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 0)  # proposing never writes
        self.assertEqual(service.state()["builds"], state["builds"])  # nor builds
        state = service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        self.assertEqual(state["project"]["revision"], 1)
        self.assertEqual((state["history"]["undo"], state["history"]["redo"]), (1, 0))
        self.assertEqual(state["last_command"]["type"], "batch")
        self.assertEqual(state["last_command"]["operations"], 2)
        self.assertEqual(state["last_command"]["source"], f"proposal {proposal['id']}")
        pump = state["project"]["model"]["entities"]["ent-pump"]
        self.assertEqual((pump["tag"], pump["parameters"]["motor_length"]["value"]), ("P-201", 0.42))
        self.assertEqual(len(state["builds"]), builds_before + 1)
        self.assertEqual(state["build"]["source_revision"], 1)
        on_disk = load_document(str(self.path))
        self.assertEqual((on_disk["current"]["revision"], len(on_disk["undo"])), (1, 1))
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            409)
        state = wait_terminal(service)
        self.assertEqual(state["published"]["source_revision"], 1)
        state = command(service, "undo", {})
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-101")
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["parameters"]["motor_length"]["value"], 0.35)
        self.assertEqual((state["project"]["revision"], state["history"]["undo"], state["history"]["redo"]), (2, 0, 1))
        wait_terminal(service)

    def test_rejected_batches_change_nothing(self):
        bad = copy.deepcopy(COMPOUND)
        bad["operations"].append(op("rename_entity", entity_id="ent-tank", tag="P-201"))
        service = self.service(FakePlanner(bad))
        before = self.path.read_bytes()
        proposal = self.propose(service)
        self.assertEqual((proposal["status"], proposal["commands"]), ("invalid", []))
        self.assertIn("operations[2]", json.dumps(proposal["validation"]["diagnostics"]))
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            400, "proposal_unsupported")
        envelope = {"id": "cmd-direct-batch", "expected_revision": 0, "type": "batch", "payload": {"operations": [
            {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-777"}},
            {"type": "rename_entity", "payload": {"entity_id": "ent-tank", "tag": "P-777"}}]}}
        error = self.assertRejected(lambda: service.command(envelope), 400, "duplicate_tag")
        self.assertIn("operations[1]", json.dumps(error.diagnostics))
        state = service.state()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((state["project"]["revision"], state["history"]["undo"], state["build"]), (0, 0, None))

    def test_questions_unsupported_and_unavailable_plans_cannot_apply(self):
        cases = ((FakePlanner(plan(COMPOUND["operations"], [("motor", "x")], outcome="questions",
                                   questions=[QUESTION])), "questions", "proposal_questions"),
                 (FakePlanner(plan(outcome="unsupported")), "unsupported", "proposal_unsupported"),
                 (FakePlanner(error=PlannerError("planner_auth", "FAKE not the Max subscription")), "unavailable",
                  "proposal_unsupported"),
                 (FakePlanner(error=PlannerError("planner_model_mismatch", "FAKE wrong model")), "rejected",
                  "proposal_unsupported"),
                 (FakePlanner(plan([op("delete_entity", entity_id="ent-pump")])), "rejected",
                  "proposal_unsupported"))
        for planner, status, code in cases:
            with self.subTest(status=status, code=code):
                service = self.service(planner)
                proposal = self.propose(service)
                self.assertEqual((proposal["status"], proposal["supported"], proposal["commands"]), (status, False, []))
                self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"],
                                                                    "expected_revision": 0}), 400, code)
                self.assertEqual(service.state()["project"]["revision"], 0)
                service.close()

    def test_composed_drum_insertion_persists_through_save_reopen_and_undo(self):
        drum_plan = plan([drum_insert(connection_id="conn-discharge-3")], [("drum", "Example drum dimensions.")])
        service = self.service(FakePlanner(drum_plan))
        proposal = self.propose(service)
        self.assertEqual(proposal["status"], "ready", proposal["summary"])
        state = service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        drum = state["project"]["model"]["entities"]["ent-drum-1"]
        self.assertEqual(drum["kind"], "composed_equipment")
        state = service.save({"expected_revision": 1})
        state = service.reopen({})
        self.assertEqual(state["project"]["model"]["entities"]["ent-drum-1"]["definition"], drum["definition"])
        self.assertEqual(state["project"]["physical"]["placements"]["ent-drum-1"]["status"], "unresolved")
        wait_terminal(service)
        state = command(service, "undo", {})
        self.assertNotIn("ent-drum-1", state["project"]["model"]["entities"])
        wait_terminal(service)


class StalenessTests(PlanningServiceCase):
    def test_slow_plan_is_unlocked_and_stale_after_a_concurrent_change(self):
        planner = FakePlanner(COMPOUND, gated=True)
        service = self.service(planner)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(planner.entered.wait(10))
        started = time.monotonic()
        state = service.state()  # navigation and polling while the planner runs
        self.assertEqual([p["expected_revision"] for p in state["planning"]["active"]], [0])
        service.select({"semantic_ids": ["ent-pump"], "origin": "pid"})
        state = command(service, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-201"})  # a write
        self.assertEqual(state["project"]["revision"], 1)
        quick = service.propose({"text": "rename P-101 to P-301", "expected_revision": 1})
        self.assertTrue(quick["supported"])
        self.assertLess(time.monotonic() - started, 5)
        planner.gate.set()
        thread.join(10)
        proposal = outcome["value"]
        self.assertTrue(proposal["stale"])
        self.assertIn("stale_proposal", [w["code"] for w in proposal["validation"]["warnings"]])
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            409, "stale_proposal")
        self.assertEqual(service.state()["planning"]["active"], [])
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 1)
        wait_terminal(service)

    def test_apply_rechecks_digest_revision_and_expiry(self):
        service = self.service()
        proposal = self.propose(service)
        stored = service._proposals[proposal["id"]]
        original = stored["base_digest"]
        stored["base_digest"] = "0" * 64  # white-box: the digest gate is checked under the write lock
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            409, "stale_proposal")
        stored["base_digest"] = original
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 1}),
                            409)
        stored["expires_at"] = "2000-01-01T00:00:00.000+00:00"
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            409, "proposal_expired")
        fresh = self.propose(service)
        command(service, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-201"})
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": fresh["id"], "expected_revision": 0}), 409)
        self.assertRejected(lambda: self.propose(service, revision=0), 409, "stale_revision")
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 1)
        wait_terminal(service)


class LimitTests(PlanningServiceCase):
    def test_concurrent_plan_limit_and_cancellation(self):
        planner = FakePlanner(COMPOUND, gated=True)
        service = self.service(planner)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(planner.entered.wait(10))
        self.assertRejected(lambda: self.propose(service), 409, "planner_busy")
        (active,) = service.state()["planning"]["active"]
        state = service.cancel_proposal({"proposal_id": active["id"]})
        self.assertTrue(state["planning"]["active"][0]["cancelled"] if state["planning"]["active"] else True)
        thread.join(10)
        proposal = outcome["value"]
        self.assertEqual((proposal["id"], proposal["status"], proposal["commands"]), (active["id"], "unavailable", []))
        self.assertIn("planner_cancelled", json.dumps(proposal["validation"]["diagnostics"]))
        self.assertRejected(lambda: service.cancel_proposal({"proposal_id": active["id"]}), 409, "plan_not_running")
        planner.gate.set()
        self.assertEqual(self.propose(service)["status"], "ready")  # the slot was released

    def test_mode_and_field_validation_and_quick_compatibility(self):
        service = self.service()
        self.assertRejected(lambda: service.propose({"text": "x", "expected_revision": 0, "mode": "ai"}), 400)
        self.assertRejected(lambda: service.propose({"text": "x", "expected_revision": 0, "planner": "other"}), 400)
        self.assertRejected(lambda: service.cancel_proposal({}), 400)
        quick = service.propose({"text": "rename P-101 to P-201 and set the tank height to 1.5 m",
                                 "expected_revision": 0})
        self.assertFalse(quick["supported"])  # the deterministic grammar is unchanged: compound stays refused
        self.assertIn("compound", quick["reason"])
        quick = service.propose({"text": "rename P-101 to P-201", "expected_revision": 0, "mode": "quick"})
        self.assertEqual((quick["mode"], quick["intent"], quick["command_count"], quick["operation_count"]),
                         ("quick", "rename", 1, 1))
        self.assertEqual((quick["questions"], quick["impacts"], quick["validation"]["geometry_checked"]),
                         ([], [], False))
        self.assertEqual(quick["planner"], {"kind": "deterministic", "parser": "deterministic-v1",
                                            "language_model": False})
        self.assertEqual(quick["commands"][0]["type"], "rename_entity")
        state = service.apply_proposal({"proposal_id": quick["id"], "expected_revision": 0})
        self.assertEqual(state["last_command"]["type"], "rename_entity")
        self.assertEqual(service.planner.calls, [])  # quick mode never reaches the planner
        wait_terminal(service)


class ProcessCleanupTests(PlanningServiceCase):
    def test_close_terminates_a_running_planner_process_group(self):
        cli = FakeCli(self, hang=True, grandchild=True, ignore_term=True)
        service = self.service(cli.planner(timeout=120, grace=0.3))
        thread, outcome = self.background(self.propose, service)
        pid = cli.read_pid("hanging.pid")
        grandchild = cli.read_pid("grandchild.pid")
        self.assertEqual(len(service.state()["planning"]["active"]), 1)
        started = time.monotonic()
        service.close()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 10)
        proposal = outcome["value"]
        self.assertEqual((proposal["status"], proposal["commands"]), ("unavailable", []))
        self.assertTrue(process_gone(pid))
        self.assertTrue(process_gone(grandchild))

    def test_planner_timeout_through_the_service_returns_an_unavailable_proposal(self):
        cli = FakeCli(self, hang=True)
        service = self.service(cli.planner(timeout=0.8, grace=0.2))
        proposal = self.propose(service)
        self.assertEqual((proposal["status"], proposal["commands"]), ("unavailable", []))
        self.assertIn("planner_timeout", json.dumps(proposal["validation"]["diagnostics"]))
        self.assertTrue(process_gone(cli.read_pid("hanging.pid")))
        self.assertEqual(service.state()["planning"]["active"], [])


class HttpTests(PlanningServiceCase):
    def test_engineering_round_trip_over_loopback(self):
        service = self.service()
        server = make_server(service, 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Client(server.port)
        status, proposal, _ = client.json("POST", "/api/propose", {"text": "rename the pump and lengthen its motor",
                                                                   "expected_revision": 0, "mode": "engineering"})
        self.assertEqual((status, proposal["status"], proposal["commands"][0]["type"]), (200, "ready", "batch"))
        status, state, _ = client.json("POST", "/api/apply-proposal", {"proposal_id": proposal["id"],
                                                                       "expected_revision": 0})
        self.assertEqual((status, state["project"]["revision"]), (200, 1))
        self.assertIn("planning", state)
        status, error, _ = client.json("POST", "/api/cancel-proposal", {"proposal_id": proposal["id"]})
        self.assertEqual((status, error["diagnostics"][0]["code"]), (409, "plan_not_running"))
        status, error, _ = client.json("POST", "/api/propose", {"text": "x", "expected_revision": 1, "mode": "llm"})
        self.assertEqual(status, 400)
        wait_terminal(service)


if __name__ == "__main__":
    unittest.main()
