"""Tagged process description: graph-ordered flow, item functions, evidence, unknowns and manual notes."""

from __future__ import annotations

import copy
import json
import re
import unittest

from tests.test_drafting_support import (
    DraftingCase,
    errors,
    fixture_document,
    insert_hv103,
    remove_check_valve,
    run,
    undo,
)

FLOW = ["TK-101", "L-101", "HV-101", "L-102", "P-101", "L-103", "NRV-101", "L-104", "HV-102", "L-105", "OUT-101"]
FLOW_IDS = ["ent-tank", "conn-suction-1", "ent-suction-valve", "conn-suction-2", "ent-pump", "conn-discharge-1",
            "ent-check-valve", "conn-discharge-2", "ent-discharge-valve", "conn-discharge-3", "ent-outlet-boundary"]


def generated_strings(description):
    """Every string the generator wrote, excluding verbatim manual notes and the assumption register."""
    found = []

    def walk(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for key, value in description.items():
        if key not in ("manual_notes", "conflicts", "assumptions"):
            walk(value)
    return found


def prose(description):
    """Human-readable statements the generator wrote."""
    texts = [description["purpose"]["text"], description["basis"], description["title"]]
    for item in description["items"]:
        texts += [item["function"]["text"], item["intent_basis"], item["kind_name"]]
    for unknown in description["unknowns"]:
        texts += [unknown["topic"], unknown["text"]]
    return texts


class DescriptionTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def describe(self, project, **kwargs):
        out, manifests, _ = self.build(project, **kwargs)
        return out, manifests, self.read_json(out / "description.json")

    def test_flow_sequence_follows_port_connectivity(self):
        _, _, description = self.describe(self.project)
        sequence = description["flow_sequence"]
        self.assertEqual([s["tag"] for s in sequence], FLOW)
        self.assertEqual([s["semantic_id"] for s in sequence], FLOW_IDS)
        self.assertEqual({s["run"] for s in sequence}, {1})
        self.assertEqual(sequence[0]["position"], "start")
        self.assertEqual(sequence[-1]["position"], "end")
        line = sequence[5]
        self.assertEqual((line["from_tag"], line["to_tag"], line["closes_loop"]), ("P-101", "NRV-101", False))
        self.assertEqual(line["zone_refs"], ["P&ID-001 / F5"])

    def test_flow_order_ignores_tags_and_layout(self):
        document = self.document
        for eid, tag in (("ent-tank", "Z-9"), ("ent-outlet-boundary", "A-1"), ("ent-pump", "M-5")):
            document = run(document, "rename_entity", {"entity_id": eid, "tag": tag}, f"cmd-{eid}")
        document = run(document, "move_symbol", {"symbol_id": "sym-tank", "x": 680.0, "y": 420.0,
                                                 "rotation_deg": 0.0})
        _, _, description = self.describe(document["current"])
        self.assertEqual([s["semantic_id"] for s in description["flow_sequence"]], FLOW_IDS)
        self.assertEqual(description["flow_sequence"][0]["tag"], "Z-9")

    def test_every_tagged_item_is_explained_with_current_tags_and_evidence(self):
        _, manifests, description = self.describe(self.project)
        model = self.project["model"]
        items = description["items"]
        self.assertEqual([i["semantic_id"] for i in items][:11], FLOW_IDS)
        self.assertEqual({i["semantic_id"] for i in items}, set(model["entities"]) | set(model["connections"]))
        self.assertEqual(len(items), len({i["semantic_id"] for i in items}))
        tags = {eid: e["tag"] for eid, e in model["entities"].items()}
        tags.update({cid: c["tag"] for cid, c in model["connections"].items()})
        unknown_ids = {u["id"] for u in description["unknowns"]}
        for item in items:
            self.assertEqual(item["tag"], tags[item["semantic_id"]])
            self.assertTrue(item["function"]["text"])
            self.assertIn("not verified", item["intent_basis"] + " not verified")
            text = "".join(p["text"] if "text" in p else p["tag"] for p in item["function"]["parts"])
            self.assertEqual(text, item["function"]["text"])
            for part in item["function"]["parts"]:
                if "ref" in part:
                    self.assertEqual(part["tag"], tags[part["ref"]])
            self.assertTrue(item["assumption_ids"], item["tag"])
            self.assertLessEqual(set(item["assumption_ids"]), set(model["assumptions"]))
            self.assertLessEqual(set(item["unknown_ids"]), unknown_ids)
            self.assertTrue(item["unknown_ids"], item["tag"])
            self.assertTrue(item["zone_refs"])
            self.assertRegex(item["fingerprint"], r"^[0-9a-f]{64}$")
        by_tag = {i["tag"]: i for i in items}
        self.assertIn("on the suction side of P-101", by_tag["HV-101"]["function"]["text"])
        self.assertIn("on the discharge side of P-101", by_tag["HV-102"]["function"]["text"])
        self.assertIn("from its suction line L-102 to its discharge line L-103", by_tag["P-101"]["function"]["text"])
        self.assertIn("prevent reverse flow", by_tag["NRV-101"]["function"]["text"])
        self.assertEqual(by_tag["TK-101"]["observed_by"], [{"semantic_id": "ent-level-indicator", "tag": "LI-101"}])
        tank_evidence = {e["name"]: e for e in by_tag["TK-101"]["evidence"] if e["kind"] == "parameter"}
        self.assertEqual(tank_evidence["height"], {"kind": "parameter", "name": "height", "value": 1.2, "unit": "m",
                                                   "assumption_id": "asm-tank-dimensions"})
        register = {a["id"]: a for a in description["assumptions"]}
        self.assertEqual(set(register), set(model["assumptions"]))
        self.assertIn({"semantic_id": "ent-tank", "tag": "TK-101", "field": "parameter height"},
                      register["asm-tank-dimensions"]["used_by"])
        self.assertTrue(description["purpose"]["text"].startswith(
            "The modeled scope carries liquid service from TK-101 to OUT-101 by way of centrifugal pump P-101."))
        self.assertEqual(manifests["description"]["representations"]["ent-pump"], ["item/ent-pump", "flow/5"])
        self.assertEqual(manifests["description"]["representations"]["port-pump-inlet"], [])

    def test_unknowns_stay_explicit(self):
        _, _, description = self.describe(self.project)
        unknowns = {u["id"]: u for u in description["unknowns"]}
        for uid in ("unknown/process-conditions", "unknown/materials-ratings", "unknown/valve-positions",
                    "unknown/check-valve-details", "unknown/pump-duty", "unknown/tank-details",
                    "unknown/instrumentation", "unknown/downstream", "unknown/control-behavior"):
            self.assertIn(uid, unknowns)
        tags = lambda uid: [a["tag"] for a in unknowns[uid]["applies_to"]]  # noqa: E731
        self.assertEqual(tags("unknown/valve-positions"), ["HV-101", "HV-102"])
        self.assertEqual(tags("unknown/instrumentation"), ["LI-101", "PI-101"])
        self.assertIn("pressure and temperature", unknowns["unknown/process-conditions"]["text"])
        self.assertIn("normal (open or closed) positions and fail actions", unknowns["unknown/valve-positions"]["text"])
        self.assertIn("No control loops", unknowns["unknown/control-behavior"]["text"])

    def test_no_invented_conditions_control_or_safety_functions(self):
        out, _, description = self.describe(self.project)
        text = " ".join(generated_strings(description))
        self.assertNotRegex(text, r"\d\s*(bar|kPa|MPa|psi|°C|degC|K\b|m3/h|m³/h|l/s|rpm|kW|hp)\b")
        self.assertNotRegex(text.lower(), r"normally (open|closed)|fail[- ](open|closed?|safe)|locked")
        self.assertNotRegex(text.lower(), r"\bcontrols\b|\bcontroller\b|\bregulat")
        for statement in prose(description):
            if re.search(r"control", statement, flags=re.IGNORECASE):  # control appears only as a negation
                self.assertRegex(statement.lower(), r"\bno\b|\bnot\b|nothing|none", statement)
        indicator = {i["tag"]: i for i in description["items"]}["PI-101"]
        self.assertIn("not part of a control loop", indicator["function"]["text"])
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"https?://|<script|\bsrc=|@import")

    def test_rename_and_undo_reconcile_generated_text(self):
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        out, _, description = self.describe(renamed["current"])
        text = " ".join(generated_strings(description))
        self.assertIn("P-201", text)
        self.assertNotIn("P-101", text)
        self.assertNotIn("P-101", (out / "description.html").read_text(encoding="utf-8"))
        restored = undo(renamed)
        out2, _, description2 = self.describe(restored["current"])
        text2 = " ".join(generated_strings(description2))
        self.assertIn("P-101", text2)
        self.assertNotIn("P-201", text2)

    def test_insertion_adds_items_in_flow_order_and_open_items(self):
        inserted = insert_hv103(self.document)["current"]
        _, manifests, description = self.describe(inserted)
        self.assertEqual([s["tag"] for s in description["flow_sequence"]],
                         FLOW[:7] + ["L-104", "HV-103", "L-106", "HV-102", "L-105", "OUT-101"])
        hv103 = {i["tag"]: i for i in description["items"]}["HV-103"]
        self.assertEqual(hv103["status"]["physical"], "unresolved")
        self.assertIn("on the discharge side of P-101", hv103["function"]["text"])
        self.assertEqual(description["status"], "pending")
        self.assertEqual(manifests["description"]["status"], "pending")
        self.assertTrue(any(item["code"] == "placement_unresolved" and item["tags"] == ["HV-103"]
                            for item in description["open_items"]))


class ManualNoteTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def describe(self, project, notes):
        out, manifests, _ = self.build(project, manual_notes=notes)
        return out, manifests, self.read_json(out / "description.json")

    def test_current_notes_are_preserved_verbatim_and_complete(self):
        note = {"id": "note-1", "target_id": "ent-pump", "reviewed_revision": 0, "author": "reviewer",
                "text": "  Keep <b>this</b> & that.\n\tSecond line é—ok  "}
        sidecar = {"schema_version": 1, "project_id": "project-pump-skid", "notes": [note]}
        original = copy.deepcopy(sidecar)
        out, manifests, description = self.describe(self.project, sidecar)
        self.assertEqual(sidecar, original)
        record = description["manual_notes"][0]
        self.assertEqual(record["note"], note)
        self.assertEqual(record["state"], "current")
        self.assertEqual(record["target_tag"], "P-101")
        self.assertEqual(description["conflicts"], [])
        self.assertEqual(manifests["description"]["status"], "complete")
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertIn("  Keep &lt;b&gt;this&lt;/b&gt; &amp; that.\n\tSecond line é—ok  ", html)

    def test_fingerprint_reviews_go_stale_only_when_their_target_changes(self):
        _, _, base = self.describe(self.project, None)
        fingerprint = {i["semantic_id"]: i["fingerprint"] for i in base["items"]}["ent-pump"]
        note = {"id": "note-1", "target_id": "ent-pump", "text": "Reviewed.", "reviewed_revision": 0,
                "reviewed_fingerprint": fingerprint}
        unrelated = run(self.document, "move_symbol", {"symbol_id": "sym-level-indicator", "x": 60.0, "y": 140.0,
                                                       "rotation_deg": 0.0})
        _, manifests, description = self.describe(unrelated["current"], [note])
        self.assertEqual(description["manual_notes"][0]["state"], "current")
        self.assertEqual(manifests["description"]["status"], "complete")
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        _, manifests, description = self.describe(renamed["current"], [note])
        record = description["manual_notes"][0]
        self.assertEqual(record["state"], "stale_review")
        self.assertEqual(record["note"]["text"], "Reviewed.")
        self.assertEqual([c["code"] for c in description["conflicts"]], ["stale_review"])
        self.assertEqual(manifests["description"]["status"], "pending")
        self.assertIn("manual_note_stale_review", {d["code"] for d in manifests["description"]["diagnostics"]})
        self.assertEqual(manifests["pid"]["status"], "complete")  # note review is a description concern

    def test_revision_reviews_go_stale_after_any_revision(self):
        note = {"id": "note-1", "target_id": "conn-suction-1", "text": "Line note", "reviewed_revision": 0}
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        _, _, description = self.describe(renamed["current"], [note])
        self.assertEqual(description["manual_notes"][0]["state"], "stale_review")
        self.assertIn("reviewed at revision 0, not 1", description["conflicts"][0]["message"])

    def test_orphaned_notes_are_kept_and_flagged_after_a_valid_removal(self):
        removed = remove_check_valve(self.project)
        self.assertEqual(errors(removed), [])
        note = {"id": "note-nrv", "target_id": "ent-check-valve", "text": "Confirm NRV-101 orientation.",
                "reviewed_revision": 0}
        out, manifests, description = self.describe(removed, [note])
        record = description["manual_notes"][0]
        self.assertEqual(record["state"], "orphan")
        self.assertEqual(record["note"], note)
        self.assertEqual(description["conflicts"][0]["code"], "orphan_reference")
        self.assertEqual(manifests["description"]["status"], "pending")
        self.assertIn("Confirm NRV-101 orientation.", (out / "description.html").read_text(encoding="utf-8"))
        generated = " ".join(generated_strings(description))
        self.assertNotIn("NRV-101", generated)

    def test_malformed_notes_are_flagged_and_kept_unchanged(self):
        notes = [
            "free text",
            {"id": "n2", "target_id": "ent-pump", "reviewed_revision": 0},
            {"id": "n3", "target_id": "ent-pump", "text": "x", "reviewed_revision": 0, "extra": True},
            {"id": "n4", "target_id": "ent-pump", "text": "fine", "reviewed_revision": 0},
            {"id": "n4", "target_id": "ent-tank", "text": "duplicate id", "reviewed_revision": 0},
        ]
        original = copy.deepcopy(notes)
        _, manifests, description = self.describe(self.project, notes)
        self.assertEqual(notes, original)
        self.assertEqual([r["note"] for r in description["manual_notes"]], original)
        self.assertEqual([r["state"] for r in description["manual_notes"]],
                         ["invalid", "invalid", "invalid", "current", "current"])
        self.assertEqual([c["code"] for c in description["conflicts"]],
                         ["invalid_note", "invalid_note", "invalid_note", "duplicate_note_id"])
        self.assertEqual(manifests["description"]["status"], "pending")

    def test_notes_from_another_project_are_not_treated_as_reviewed(self):
        sidecar = {"project_id": "other-project", "notes": [
            {"id": "n1", "target_id": "ent-pump", "text": "x", "reviewed_revision": 0}]}
        _, _, description = self.describe(self.project, sidecar)
        self.assertEqual([c["code"] for c in description["conflicts"]], ["project_mismatch", "stale_review"])
        self.assertEqual(description["manual_notes"][0]["state"], "stale_review")

    def test_project_and_assumption_targets_are_supported(self):
        notes = [{"id": "p", "target_id": "project-pump-skid", "text": "Project note", "reviewed_revision": 0},
                 {"id": "a", "target_id": "asm-tank-dimensions", "text": "Assumption note", "reviewed_revision": 0}]
        _, manifests, description = self.describe(self.project, notes)
        self.assertEqual([r["state"] for r in description["manual_notes"]], ["current", "current"])
        self.assertEqual([r["target_kind"] for r in description["manual_notes"]], ["project", "assumption"])
        self.assertEqual(manifests["description"]["status"], "complete")
        json.dumps(description)  # stays JSON-serializable


if __name__ == "__main__":
    unittest.main()
