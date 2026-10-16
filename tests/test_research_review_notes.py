"""Where a researched engineering plan's references go (review note F3, docs/CREATION_AND_RESEARCH_UI.md).

Real core, storage, planner translation and research verification; LABELED FAKE plans, brief and planner
(``tests.test_creation_fakes``, ``tests.test_planning_fakes``). A pass shows that the proposal states before
apply whether the model will keep any research reference, and that the statement still holds after apply, save
and reload. It is not evidence that any real model planned or researched anything.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from skid.core import load_document, save_document
from skid.planning.engineering import RESEARCH_NOT_RECORDED
from tests.test_creation_fakes import blank_document, exchanger_plan
from tests.test_planning_creation import PROPOSAL, apply, propose, verified_research
from tests.test_planning_fakes import op, plan


def saved_and_reloaded(document):
    with tempfile.TemporaryDirectory(prefix="skid review notes ") as tmp:
        path = os.path.join(tmp, "project.json")
        save_document(path, document, None)
        return load_document(path)


def citing(document, research):
    marker = f"Research {research['id']} "
    return sorted(aid for aid, record in document["current"]["model"]["assumptions"].items()
                  if marker in record["source"])


class ResearchPersistenceTests(unittest.TestCase):
    CREATE = "Add a shell-and-tube heat exchanger HX-101 with separate shell and tube circuits"
    RENAME = "Rename the shell-and-tube heat exchanger HX-101 to HX-102"

    def created(self):
        research = verified_research(self.CREATE)
        proposal = propose(blank_document(), exchanger_plan(assumption_sources=["S1"]), self.CREATE,
                           research=research)
        return research, proposal

    def test_researched_creation_states_the_assumptions_that_keep_its_references(self):
        research, proposal = self.created()
        self.assertEqual(proposal["status"], "ready", proposal["reason"])
        recorded = sorted(a["id"] for a in proposal["proposed_assumptions"] if a["recorded"])
        self.assertEqual(proposal["research"]["persistence"],
                         {"status": "saved_with_assumptions", "assumption_ids": recorded})
        self.assertTrue(recorded)
        reloaded = saved_and_reloaded(apply(blank_document(), proposal))
        self.assertEqual(citing(reloaded, research), recorded, "every listed record cites the research after reload")

    def test_researched_rename_is_proposal_only_and_saves_no_reference(self):
        _, created = self.created()
        document = saved_and_reloaded(apply(blank_document(), created))
        rename = plan([op("rename_entity", entity_id="ent-hx101", tag="HX-102")], summary="FAKE: rename HX-101.",
                      unknowns=["FAKE: nothing else changes."])
        research = verified_research(self.RENAME, research_id="res-c0ffee000002")
        researched = propose(document, rename, self.RENAME, research=research)
        plain = propose(document, rename, self.RENAME)
        self.assertEqual((researched["status"], plain["status"]), ("ready", "ready"), researched["reason"])
        self.assertEqual(researched["research"]["persistence"],
                         {"status": "proposal_only", "assumption_ids": [], "note": RESEARCH_NOT_RECORDED})
        self.assertNotIn("persistence", plain["research"])
        self.assertEqual(researched["commands"], plain["commands"], "research does not alter the valid command")
        self.assertEqual(researched["unknowns"], plain["unknowns"], "model unknowns are kept")
        before = document["current"]["model"]["assumptions"]
        reloaded = saved_and_reloaded(apply(document, researched))
        self.assertEqual(reloaded["current"]["revision"], document["current"]["revision"] + 1)
        self.assertEqual(reloaded["current"]["model"]["entities"]["ent-hx101"]["tag"], "HX-102")
        self.assertEqual(reloaded["current"]["model"]["assumptions"], before, "no assumption is created")
        self.assertNotIn(research["id"], json.dumps(reloaded), "nothing saved cites the rename's research")

    def test_a_refused_researched_plan_states_no_persistence(self):
        research = verified_research(self.CREATE)
        proposal = propose(blank_document(), exchanger_plan(assumption_sources=["S7"]), self.CREATE, research=research)
        self.assertEqual((proposal["status"], proposal["commands"]), ("rejected", []))
        self.assertNotIn("persistence", proposal["research"])


if __name__ == "__main__":
    unittest.main()
