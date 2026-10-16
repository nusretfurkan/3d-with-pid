"""Equipment research and empty-project creation through the interactive service (C-owned integration).

Real core, storage, service, HTTP transport, research brief validator and drafting description builder;
LABELED FAKE generator suite (``tests.test_interactive_fakes``), LABELED FAKE planners and researchers
(``tests.test_planning_fakes``, ``tests.test_creation_fakes``). These show the research/plan stage
boundaries, refusals, staleness and provenance persistence; they are not evidence that any real model
searched the web or planned anything (root runs the actual Claude Max research separately).
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from skid.core import load_document
from skid.drafting import build_drafting
from skid.interactive.server import make_server
from skid.interactive.service import InteractiveService, ServiceError
from skid.planning.prompt import MARKER
from skid.research import ResearchError
from tests.test_creation_fakes import (FakeResearcher, exchanger_plan, fake_brief, new_blank_project,
                                       tank_and_valve_plan)
from tests.test_interactive_fakes import FAKE_SUITE, command, wait_terminal, wait_until
from tests.test_interactive_http import Client
from tests.test_planning_fakes import FakePlanner

TEXT = "Add a shell-and-tube heat exchanger HX-101 with separate shell and tube circuits"


class ResearchServiceCase(unittest.TestCase):
    def setUp(self):
        self.path = new_blank_project(self)

    def service(self, planner=None, researcher=None, **options):
        service = InteractiveService(str(self.path), suite=FAKE_SUITE, suite_options={}, timeout=60,
                                     terminate_grace=0.5, planner=planner or FakePlanner(exchanger_plan(["S1"])),
                                     researcher=FakeResearcher() if researcher is None else researcher, **options)
        self.addCleanup(service.close)
        return service

    def propose(self, service, text=TEXT, research=True, revision=None, mode="engineering"):
        body = {"text": text, "expected_revision": service.state()["project"]["revision"] if revision is None
                else revision, "mode": mode}
        if research is not None:
            body["research"] = research
        return service.propose(body)

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

    def active(self, service):
        return service.state()["planning"]["active"]


class OptInTests(ResearchServiceCase):
    def test_research_is_strict_opt_in_and_engineering_only(self):
        researcher = FakeResearcher()
        service = self.service(researcher=researcher)
        state = service.state()
        self.assertTrue(state["capabilities"]["equipment_research"])
        self.assertTrue(state["capabilities"]["equipment_creation"])
        self.assertTrue(state["planning"]["research"]["available"])
        for bad in ("true", 1, 0, [], {}):
            self.assertRejected(lambda: self.propose(service, research=bad), 400)
        self.assertRejected(lambda: service.propose({"text": TEXT, "expected_revision": 0, "mode": "engineering",
                                                     "research": None}), 400)  # JSON null is not a boolean
        self.assertRejected(lambda: self.propose(service, mode="quick"), 400, "research_mode")
        body = {"text": TEXT, "expected_revision": 0, "research": True}  # no mode: quick
        self.assertRejected(lambda: service.propose(body), 400, "research_mode")
        self.assertRejected(lambda: service.propose({"text": TEXT, "expected_revision": 0, "mode": "engineering",
                                                     "research": True, "sources": []}), 400)
        quick = self.propose(service, "Rename TK-101 to TK-201", research=False, mode="quick")
        self.assertFalse(quick["supported"])  # nothing to rename in a blank project; quick never researches
        plain = self.propose(service, research=None)
        self.assertEqual(plain["research"], {"requested": False, "status": "not_requested"})
        self.assertEqual(researcher.calls, [])  # default false: the researcher never ran
        explicit_false = self.propose(service, research=False)
        self.assertFalse(explicit_false["research"]["requested"])
        self.assertEqual(researcher.calls, [])

    def test_only_the_request_text_reaches_the_researcher_and_the_brief_reaches_the_planner(self):
        planner = FakePlanner(exchanger_plan(["S1"]))
        researcher = FakeResearcher()
        service = self.service(planner, researcher)
        proposal = self.propose(service, f"  {TEXT}\n\n  with   room ")
        self.assertEqual(proposal["status"], "ready", proposal["reason"])
        self.assertEqual(researcher.calls, [{"request": f"{TEXT}\n\nwith room", "type": "str"}])  # cleaned text only
        context = json.loads(planner.calls[0]["prompt"].partition(MARKER)[2])
        self.assertEqual(context["research"]["trust"], "untrusted")
        self.assertEqual(context["research"]["brief"]["query"], f"{TEXT}\n\nwith room")
        research = proposal["research"]
        self.assertEqual((research["requested"], research["status"]), (True, "verified"))
        self.assertEqual([s["id"] for s in research["brief"]["sources"]], ["S1", "S2"])
        self.assertEqual([s["stage"] for s in proposal["stages"]], ["researching", "planning"])
        self.assertTrue(research["checks"]["research_package_validator"])

    def test_without_a_researcher_research_is_not_advertised_and_refused_explicitly(self):
        planner = FakePlanner(exchanger_plan())
        service = self.service(planner, researcher=False)
        state = service.state()
        self.assertFalse(state["capabilities"]["equipment_research"])
        self.assertFalse(state["planning"]["research"]["available"])
        proposal = self.propose(service)
        self.assertEqual((proposal["status"], proposal["commands"]), ("unavailable", []))
        self.assertEqual(proposal["research"]["error"]["code"], "research_unavailable")
        self.assertEqual(planner.calls, [])  # no source-free plan is substituted


class StageTests(ResearchServiceCase):
    def test_stages_are_reported_then_cancel_stops_research_without_planning(self):
        planner = FakePlanner(exchanger_plan(["S1"]))
        researcher = FakeResearcher(gated=True)
        service = self.service(planner, researcher)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(researcher.entered.wait(10))
        active = self.active(service)
        self.assertEqual([(a["stage"], a["research"]) for a in active], [("researching", True)])
        self.assertGreaterEqual(active[0]["stage_elapsed_s"], 0)
        state = service.cancel_proposal({"proposal_id": active[0]["id"]})
        self.assertTrue(state["planning"]["active"][0]["cancelled"])
        thread.join(10)
        proposal = outcome["value"]
        self.assertTrue(researcher.cancelled.is_set())
        self.assertEqual((proposal["status"], proposal["commands"]), ("unavailable", []))
        self.assertEqual(proposal["research"]["status"], "cancelled")
        self.assertEqual(planner.calls, [])
        self.assertEqual(self.active(service), [])
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            400, "proposal_unsupported")

    def test_stage_changes_to_planning_and_the_plan_slot_covers_both_stages(self):
        planner = FakePlanner(exchanger_plan(["S1"]), gated=True)
        researcher = FakeResearcher(gated=True)
        service = self.service(planner, researcher)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(researcher.entered.wait(10))
        self.assertRejected(lambda: self.propose(service, research=False), 409, "planner_busy")
        researcher.gate.set()
        self.assertTrue(planner.entered.wait(10))
        self.assertEqual([a["stage"] for a in self.active(service)], ["planning"])
        self.assertEqual(self.active(service)[0]["stages"][0]["stage"], "researching")
        command_state = service.state()  # the service stays responsive (no lock is held by either stage)
        self.assertEqual(command_state["project"]["revision"], 0)
        planner.gate.set()
        thread.join(10)
        self.assertEqual(outcome["value"]["status"], "ready", outcome["value"]["reason"])

    def test_shutdown_stops_a_running_research_stage(self):
        planner = FakePlanner(exchanger_plan())
        researcher = FakeResearcher(gated=True)
        service = self.service(planner, researcher)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(researcher.entered.wait(10))
        service.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(researcher.shutdowns, 1)
        self.assertTrue(researcher.cancelled.is_set())
        self.assertEqual(outcome["value"]["commands"], [])
        self.assertEqual(planner.calls, [])


class FailureTests(ResearchServiceCase):
    def test_research_errors_refuse_without_a_source_free_fallback(self):
        cases = (
            (ResearchError("research_unavailable", "FAKE CLI missing", {"fake": True}), "unavailable"),
            (ResearchError("research_auth", "FAKE not the Max subscription", {"fake": True}), "unavailable"),
            (ResearchError("research_no_evidence", "FAKE no successful search observed", {"fake": True}), "rejected"),
            (RuntimeError("FAKE unexpected researcher fault"), "rejected"),
        )
        for error, status in cases:
            with self.subTest(error=str(error)):
                planner = FakePlanner(exchanger_plan())
                service = self.service(planner, FakeResearcher(error=error))
                proposal = self.propose(service)
                self.assertEqual((proposal["status"], proposal["commands"]), (status, []))
                self.assertFalse(proposal["supported"])
                self.assertIn("no plan is made without verified sources", proposal["reason"])
                self.assertEqual(planner.calls, [])
                service.close()

    def test_forged_briefs_from_an_injected_researcher_are_revalidated_and_refused(self):
        def forged(**changes):
            brief = fake_brief(TEXT)
            brief.update(changes)
            return brief

        hostile = fake_brief(TEXT)
        hostile["sources"][0]["url"] = "javascript:alert(1)"
        cases = {
            "invented url not in the reported search results": FakeResearcher(metadata={
                "observed_urls": ["https://www.fake-exchanger-maker.com/products/shell-and-tube"]}),
            "hostile url": FakeResearcher(brief=hostile),
            "query swapped": FakeResearcher(brief=forged(query="a request nobody sent")),
            "extra claim field": FakeResearcher(brief=forged(images_inspected=True)),
            "not a reply": FakeResearcher(reply={"plan": {}}),
        }
        for name, researcher in cases.items():
            with self.subTest(name):
                planner = FakePlanner(exchanger_plan(["S1"]))
                service = self.service(planner, researcher)
                proposal = self.propose(service)
                self.assertEqual((proposal["status"], proposal["commands"]), ("rejected", []))
                self.assertEqual(proposal["research"]["error"]["code"], "research_brief_invalid")
                self.assertIsNone(proposal["research"]["brief"])
                self.assertEqual(planner.calls, [])
                service.close()

    def test_a_change_during_research_makes_the_whole_proposal_stale(self):
        planner = FakePlanner(exchanger_plan())
        researcher = FakeResearcher(gated=True)
        service = self.service(planner, researcher)
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(researcher.entered.wait(10))
        command(service, "rename_project", {"name": "Changed while researching"})
        researcher.gate.set()
        thread.join(10)
        proposal = outcome["value"]
        self.assertEqual((proposal["status"], proposal["stale"], proposal["commands"]), ("unavailable", True, []))
        self.assertEqual(proposal["research"]["status"], "verified")  # the sources are shown, never applied
        self.assertEqual(planner.calls, [])
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 1}),
                            400)

    def test_a_change_during_planning_makes_a_researched_plan_stale(self):
        planner = FakePlanner(exchanger_plan(["S1"]), gated=True)
        service = self.service(planner, FakeResearcher())
        thread, outcome = self.background(self.propose, service)
        self.assertTrue(planner.entered.wait(10))
        command(service, "rename_project", {"name": "Changed while planning"})
        planner.gate.set()
        thread.join(10)
        proposal = outcome["value"]
        self.assertTrue(proposal["stale"])
        self.assertEqual(proposal["research"]["status"], "verified")
        self.assertRejected(lambda: service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0}),
                            409)


class PersistenceTests(ResearchServiceCase):
    def test_applied_research_citations_survive_save_reload_and_a_rebuilt_description(self):
        service = self.service(FakePlanner(exchanger_plan(["S1", "S2"])), FakeResearcher())
        proposal = self.propose(service)
        self.assertEqual(proposal["status"], "ready", proposal["reason"])
        state = service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        self.assertEqual((state["project"]["revision"], state["history"]["undo"]), (1, 1))
        wait_terminal(service)
        aid = f"asm-{proposal['id']}-hx-dims"
        urls = [s["url"] for s in proposal["research"]["brief"]["sources"]]
        saved = load_document(str(self.path))
        source = saved["current"]["model"]["assumptions"][aid]["source"]
        self.assertIn(proposal["research"]["id"], source)
        for url in urls:
            self.assertIn(url, source)
        service.reopen({})
        reloaded = service.state()["project"]["model"]["assumptions"][aid]["source"]
        self.assertEqual(reloaded, source)
        with tempfile.TemporaryDirectory(prefix="skid description ") as tmp:
            build_drafting(saved["current"], tmp)
            files = {p.name: p.read_text(encoding="utf-8") for p in Path(tmp).rglob("*") if p.is_file()
                     and p.suffix in (".json", ".html")}
        described = [name for name, text in files.items() if "description" in name and urls[0] in text]
        self.assertTrue(described, sorted(files))
        html = next((text for name, text in files.items() if name.endswith(".html") and "description" in name), "")
        self.assertIn(urls[0], html)
        self.assertNotIn("<b>exchanger</b>", html)  # the hostile-looking FAKE source title is not markup
        state = command(service, "undo", {})
        self.assertNotIn(aid, state["project"]["model"]["assumptions"])
        wait_terminal(service)


class OverviewTests(ResearchServiceCase):
    """The real researcher bridge on the LABELED FAKE CLI (``tests.test_research_fakes``) inside the service."""

    def test_an_over_limit_overview_reaches_the_planner_only_as_a_marked_derived_overview(self):
        from tests.test_research_fakes import FakeResearchCli, output, research_events
        long = " ".join(f"FAKE overview sentence {n} about the shell, tube bundle and nozzles." for n in range(40))
        payload = output(summary=long)
        planner = FakePlanner(exchanger_plan(["S1"]))
        service = self.service(planner, FakeResearchCli(self, events=research_events(payload)).researcher())
        proposal = self.propose(service)
        self.assertEqual(proposal["status"], "ready", proposal["reason"])
        research = proposal["research"]
        self.assertEqual(research["status"], "verified")
        self.assertTrue(research["checks"]["research_package_validator"])
        brief = research["brief"]
        self.assertLessEqual(len(brief["summary"]), 1500)
        self.assertTrue(brief["summary"].startswith(f"Derived overview: the researcher's {len(long)}-character"))
        self.assertNotIn("FAKE overview sentence", brief["summary"])
        self.assertEqual([f["text"] for f in brief["features"]], [f["text"] for f in payload["features"]])
        self.assertEqual(brief["unknowns"], payload["unknowns"])
        context = json.loads(planner.calls[0]["prompt"].partition(MARKER)[2])
        self.assertEqual(context["research"]["brief"]["summary"], brief["summary"])
        service.close()

        unsafe = output(summary=long + " tail‮end")  # the same overview with a bidi override past the limit
        planner = FakePlanner(exchanger_plan(["S1"]))
        service = self.service(planner, FakeResearchCli(self, events=research_events(unsafe)).researcher())
        proposal = self.propose(service)
        self.assertEqual((proposal["status"], proposal["commands"]), ("rejected", []))
        self.assertIsNone(proposal["research"]["brief"])
        self.assertEqual(planner.calls, [])


class HttpTests(ResearchServiceCase):
    def test_http_research_flag_and_capability(self):
        service = self.service(FakePlanner(tank_and_valve_plan()), FakeResearcher())
        server = make_server(service, 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Client(server.port)
        status, state, _ = client.json("GET", "/api/state")
        self.assertEqual((status, state["capabilities"]["equipment_research"]), (200, True))
        status, body, _ = client.json("POST", "/api/propose", {"text": TEXT, "expected_revision": 0, "research": "yes",
                                                               "mode": "engineering"})
        self.assertEqual(status, 400, body)
        status, body, _ = client.json("POST", "/api/propose", {"text": TEXT, "expected_revision": 0, "research": True})
        self.assertEqual(status, 400, body)
        status, body, _ = client.json("POST", "/api/propose", {"text": "Add a tank TK-101 with valve HV-101",
                                                               "expected_revision": 0, "mode": "engineering",
                                                               "research": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["research"]["status"], "verified")
        digest = client.json("GET", "/api/state")[1]["document"]["input_digest"]
        ask = {"request_id": "ask-fake-1", "question": "What is here?", "selected_ids": [], "expected_revision": 0,
               "input_digest": digest}
        status, body, _ = client.json("POST", "/api/ask", dict(ask, research=True))
        self.assertEqual(status, 400, body)  # Ask never researches; the unknown field is refused
        self.assertIn("research", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
