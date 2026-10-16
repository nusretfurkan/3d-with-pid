"""Engineering-plan dry runs of ordered composed-equipment edits report the true committed route state.

    .venv-cad/bin/python -m unittest tests.test_equipment_planning -v

Uses the LABELED in-process ``FakePlanner`` from ``tests.test_planning_fakes``:
no Claude process or network call is made, and a pass is not evidence that the
real planner works. The plans go through the planning translator and the
shared ``apply_command`` dry run exactly as an applied plan would.
"""

from __future__ import annotations

import unittest

from examples.make_fixture import make_document
from skid.core.commands import apply_command
from tests.test_equipment import DRUM, drum_batch

try:
    from skid.planning.engineering import propose_engineering
    from tests.test_planning_fakes import FakePlanner, op, plan
except ImportError as exc:  # the planning layer is owned and delivered separately
    raise unittest.SkipTest(f"planning layer unavailable: {exc}")

ROUTES = ("conn-discharge-3", "conn-drum-out")


def length_plan(*values, unit="mm"):
    operations = [op("set_parameter", entity_id=DRUM, name=name, value=value, unit=unit, assumption="drum")
                  for name, value in values]
    return plan(operations, assumptions=[("drum", "FAKE illustrative drum dimension change.")])


class OrderedComposedPlanTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        base = make_document()
        cls.document, _ = apply_command(base, drum_batch(base))

    def propose(self, *values, unit="mm"):
        proposal = propose_engineering(self.document, "FAKE request: change the drum", "prop-000000000042",
                                       FakePlanner(length_plan(*values, unit=unit)))
        self.assertTrue(proposal["supported"], proposal.get("reason"))
        self.assertEqual(len(proposal["commands"]), 1)
        return proposal

    def test_transiently_violating_order_reports_rerouting_not_missing_placement(self):
        for values in ((("shell_length", 800), ("inlet_offset", 400)), (("inlet_offset", 400), ("shell_length", 800))):
            with self.subTest(order=[name for name, _ in values]):
                proposal = self.propose(*values)
                warnings = proposal["validation"]["warnings"]
                self.assertFalse([w for w in warnings if "placement unresolved" in w["message"]], warnings)
                route_impacts = [i for i in proposal["impacts"] if set(i["ids"]) & set(ROUTES)]
                self.assertTrue(route_impacts)
                self.assertTrue(all("needs_reroute" in i["effect"] for i in route_impacts), route_impacts)
                applied, _ = apply_command(self.document, proposal["commands"][0])
                self.assertEqual({applied["current"]["physical"]["routes"][cid]["status"] for cid in ROUTES},
                                 {"needs_reroute"})

    def test_restored_dimension_leaves_routes_current(self):
        proposal = self.propose(("shell_length", 1.8), ("shell_length", 1.4), unit="m")
        self.assertFalse([w for w in proposal["validation"]["warnings"] if w["code"].startswith("route")])
        self.assertFalse([i for i in proposal["impacts"] if set(i["ids"]) & set(ROUTES)])
        applied, _ = apply_command(self.document, proposal["commands"][0])
        self.assertEqual(applied["current"]["physical"], self.document["current"]["physical"])


if __name__ == "__main__":
    unittest.main()
