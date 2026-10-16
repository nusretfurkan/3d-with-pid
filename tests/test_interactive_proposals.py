"""Deterministic proposal parser: supported single changes, explicit refusals, and core validation of the result."""

from __future__ import annotations

import copy
import unittest

from skid.core import CommandError, apply_command, load_document
from skid.interactive.proposals import propose
from tests.test_interactive_fakes import FIXTURE


class ProposalCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = load_document(str(FIXTURE))
        cls.project = cls.document["current"]

    def propose(self, text, project=None):
        return propose(project or self.project, text, 0, "prop-test")

    def apply(self, proposal):
        self.assertTrue(proposal["supported"], proposal["summary"])
        self.assertEqual(len(proposal["commands"]), 1)
        new, _ = apply_command(self.document, proposal["commands"][0])
        return new["current"]

    def assertUnsupported(self, text, fragment):
        proposal = self.propose(text)
        self.assertFalse(proposal["supported"], proposal)
        self.assertEqual(proposal["commands"], [])
        self.assertIn("No change will be made", proposal["summary"])
        self.assertIn(fragment.lower(), proposal["reason"].lower())
        return proposal


class RenameTests(ProposalCase):
    def test_rename_by_tag_and_by_unique_kind_word(self):
        for text in ("rename P-101 to P-201", "Please rename the pump to P-201.", "change the tag of P-101 to P-201",
                     "retag p-101 as P-201", "change pump's tag to P-201"):
            with self.subTest(text=text):
                proposal = self.propose(text)
                self.assertEqual(proposal["intent"], "rename")
                command = proposal["commands"][0]
                self.assertEqual(command["type"], "rename_entity")
                self.assertEqual(command["payload"], {"entity_id": "ent-pump", "tag": "P-201"})
                self.assertEqual(command["expected_revision"], 0)
                after = self.apply(proposal)
                self.assertEqual(after["model"]["entities"]["ent-pump"]["tag"], "P-201")
                self.assertEqual(after["pid"], self.project["pid"])
                self.assertEqual(after["physical"], self.project["physical"])

    def test_rename_to_duplicate_tag_is_supported_but_rejected_by_core(self):
        proposal = self.propose("rename P-101 to TK-101")
        self.assertTrue(proposal["supported"])
        with self.assertRaises(CommandError) as caught:
            apply_command(self.document, proposal["commands"][0])
        self.assertEqual(caught.exception.diagnostics[0]["code"], "duplicate_tag")

    def test_ambiguous_unknown_and_line_references_are_refused(self):
        self.assertUnsupported("rename the valve to V-1", "ambiguous")
        self.assertUnsupported("rename the isolation valve to V-1", "HV-101")
        self.assertUnsupported("rename X-999 to X-1", "no equipment matches")
        self.assertUnsupported("rename L-101 to L-201", "line (connection) tag")
        self.assertUnsupported("rename P-101 to <script>", "must start with a letter")

    def test_indicator_by_observed_variable(self):
        proposal = self.propose("rename the level indicator to LI-201")
        self.assertEqual(proposal["commands"][0]["payload"]["entity_id"], "ent-level-indicator")


class ParameterTests(ProposalCase):
    def test_dimension_with_unit_conversion_and_new_assumption(self):
        cases = {"set the tank height to 1.5 m": ("ent-tank", "height", 1.5),
                 "set body length of P-101 to 480 mm": ("ent-pump", "body_length", 0.48),
                 "change the pump motor length to 38 cm": ("ent-pump", "motor_length", 0.38),
                 "set TK-101 diameter = 0.9 metres": ("ent-tank", "diameter", 0.9)}
        for text, (entity_id, name, value) in cases.items():
            with self.subTest(text=text):
                proposal = self.propose(text)
                command = proposal["commands"][0]
                self.assertEqual(command["type"], "set_parameter")
                self.assertEqual(command["payload"]["entity_id"], entity_id)
                self.assertEqual(command["payload"]["name"], name)
                quantity = command["payload"]["quantity"]
                self.assertAlmostEqual(quantity["value"], value, places=12)
                self.assertEqual(quantity["unit"], "m")
                (assumption_id, record), = command["assumptions"].items()
                self.assertEqual(quantity["assumption_id"], assumption_id)
                self.assertIn("not a vendor", record["description"])
                after = self.apply(proposal)
                self.assertAlmostEqual(after["model"]["entities"][entity_id]["parameters"][name]["value"], value)
                self.assertIn(assumption_id, after["model"]["assumptions"])
                self.assertTrue(proposal["unknowns"])

    def test_unknown_parameter_and_out_of_range_values(self):
        self.assertUnsupported("set the tank body length to 2 m", "has no parameter")
        self.assertUnsupported("set the tank height to 900 m", "limit")
        self.assertUnsupported("set the tank height to 0 m", "positive")

    def test_template_bounds_still_validated_by_core(self):
        proposal = self.propose("set the tank outlet height to 3 m")  # above the tank height
        self.assertTrue(proposal["supported"])
        with self.assertRaises(CommandError):
            apply_command(self.document, proposal["commands"][0])


class MovementTests(ProposalCase):
    def test_relative_and_absolute_physical_moves(self):
        start = self.project["physical"]["placements"]["ent-pump"]["position_m"]
        cases = {"move P-101 0.5 m in +x": [start[0] + 0.5, start[1], start[2]],
                 "move the pump by 200 mm along -y": [start[0], start[1] - 0.2, start[2]],
                 "move P-101 up 100 mm": [start[0], start[1], start[2] + 0.1],
                 "move P-101 x 0.25 m": [start[0] + 0.25, start[1], start[2]],
                 "move P-101 physically to (2, 0.1, 0) m": [2.0, 0.1, 0.0],
                 "move P-101 to (2,0,0)": [2.0, 0.0, 0.0]}
        for text, position in cases.items():
            with self.subTest(text=text):
                proposal = self.propose(text)
                command = proposal["commands"][0]
                self.assertEqual(command["type"], "move_equipment")
                placement = command["payload"]["placement"]
                for got, wanted in zip(placement["position_m"], position):
                    self.assertAlmostEqual(got, wanted, places=9)
                self.assertEqual(placement["rotation_deg"],
                                 self.project["physical"]["placements"]["ent-pump"]["rotation_deg"])
                self.assertTrue(placement["manual"])
                after = self.apply(proposal)
                self.assertEqual(after["pid"]["symbols"], self.project["pid"]["symbols"])
                self.assertEqual(after["model"]["connections"], self.project["model"]["connections"])
                for connection_id in ("conn-suction-2", "conn-discharge-1"):
                    self.assertEqual(after["physical"]["routes"][connection_id]["status"], "needs_reroute")
                self.assertTrue(any("needs_reroute" in u for u in proposal["unknowns"]))

    def test_diagram_moves_keep_physical_placement(self):
        symbol = self.project["pid"]["symbols"]["sym-pump"]
        cases = {"move the pump symbol right by 40": (symbol["x"] + 40, symbol["y"]),
                 "move P-101 on the P&ID up 20": (symbol["x"], symbol["y"] - 20),
                 "move P-101 on the diagram to (400, 320)": (400.0, 320.0),
                 "move the P-101 symbol to 410, 300": (410.0, 300.0)}
        for text, (x, y) in cases.items():
            with self.subTest(text=text):
                proposal = self.propose(text)
                command = proposal["commands"][0]
                self.assertEqual(command["type"], "move_symbol")
                self.assertEqual(command["payload"], {"symbol_id": "sym-pump", "x": x, "y": y,
                                                      "rotation_deg": symbol["rotation_deg"]})
                after = self.apply(proposal)
                self.assertEqual(after["physical"], self.project["physical"])

    def test_unplaced_equipment_needs_an_absolute_position(self):
        project = copy.deepcopy(self.project)
        project["physical"]["placements"]["ent-pump"] = {"position_m": None, "rotation_deg": None,
                                                         "status": "unresolved", "manual": False,
                                                         "assumption_id": "asm-skid-layout", "reason": "test"}
        refused = propose(project, "move P-101 0.5 m in +x", 0, "prop-test")
        self.assertFalse(refused["supported"])
        self.assertIn("absolute position", refused["reason"])
        placed = propose(project, "move P-101 to (1, 0, 0) m", 0, "prop-test")
        self.assertEqual(placed["commands"][0]["type"], "place_equipment")


class RefusalTests(ProposalCase):
    def test_compound_requests_are_never_split(self):
        proposal = self.assertUnsupported("rename P-101 to P-201 and set the tank height to 1.5 m", "compound")
        self.assertEqual(proposal["commands"], [])
        self.assertUnsupported("move the pump 0.1 m in x then rename it to P-2", "compound")

    def test_unsupported_intents_are_explicit(self):
        self.assertUnsupported("insert a valve between NRV-101 and HV-102", "insert_valve")
        self.assertUnsupported("delete HV-101", "deletion")
        self.assertUnsupported("rotate the pump 90 degrees", "rotation")
        self.assertUnsupported("make the skid look nicer", "none of the supported")
        self.assertUnsupported("what is the flow rate?", "none of the supported")
        self.assertUnsupported("", "empty")
        self.assertUnsupported("x" * 600, "longer than")
        self.assertUnsupported("rename P-101\x00 to P-2", "control characters")

    def test_unsupported_reply_shape_and_no_llm_claim(self):
        proposal = self.propose("optimise the pump for 40 m3/h")
        for key in ("id", "expected_revision", "summary", "commands", "assumptions", "unknowns", "supported"):
            self.assertIn(key, proposal)
        self.assertIn("not a language model", proposal["limitation"])
        self.assertTrue(proposal["examples"])


if __name__ == "__main__":
    unittest.main()
