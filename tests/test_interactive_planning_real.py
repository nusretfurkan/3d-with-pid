"""A reviewed engineering plan through the service with the REAL generators (``skid.geometry``, ``skid.drafting``).

The planner is the LABELED FAKE in-process ``FakePlanner`` returning a fixed
plan; everything after it is real: translation, the core ``batch`` and
``insert_equipment`` commands, persistence, the worker and both generator
suites. Skipped, with the reason, when the generators or CadQuery are not
importable (run with ``.venv-cad/bin/python``). It does not show that the real
model would propose this plan, and it does not assess CAD or drawing quality.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from skid.core import apply_command, load_document
from skid.interactive import integrity
from skid.interactive.service import InteractiveService
from skid.planning.engineering import propose_engineering
from skid.templates import world_port_position
from tests.test_interactive_fakes import new_project, wait_terminal
from tests.test_planning_engineering import orthogonal
from tests.test_planning_fakes import FakePlanner, drum_insert, op, plan

_MISSING = [name for name in ("skid.geometry", "skid.drafting", "cadquery") if importlib.util.find_spec(name) is None]
BUILD_TIMEOUT_S = 600
PLACEMENT = {"status": "placed", "position_m": [3.6, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0],
             "assumption": "layout"}
ASSUMPTIONS = [("drum", "Example drum dimensions from the horizontal_drum definition."),
               ("layout", "Illustrative drum position downstream of HV-102, with the scope boundary moved after it."),
               ("route", "Illustrative orthogonal routes to and from the drum nozzles.")]
MOVE_BOUNDARY = op("move_equipment", entity_id="ent-outlet-boundary", position_m=[5.2, 0.0, 0.75],
                   rotation_deg=[0.0, 0.0, 0.0], assumption="layout")


def resolved_drum_plan(document):
    """Insert and place the example drum on L-105, move the boundary after it and route both lines (FAKE plan)."""
    staged = propose_engineering(document, "stage", "prop-000000000000", FakePlanner(plan(
        [drum_insert(connection_id="conn-discharge-3", placement=PLACEMENT), MOVE_BOUNDARY], ASSUMPTIONS)))
    scratch = apply_command(document, staged["commands"][0])[0]["current"]
    ports = {name: world_port_position(scratch, name) for name in (
        "port-discharge-valve-outlet", "port-drum-1-inlet", "port-drum-1-outlet", "port-boundary-inlet")}
    upstream_z = max(ports["port-discharge-valve-outlet"][2], ports["port-drum-1-inlet"][2]) + 0.4
    return plan([drum_insert(connection_id="conn-discharge-3", placement=PLACEMENT), MOVE_BOUNDARY,
                 op("set_route", connection_id="conn-discharge-3", assumption="route",
                    points_m=orthogonal(ports["port-discharge-valve-outlet"], ports["port-drum-1-inlet"], upstream_z,
                                        lead=(0.0, -0.1, 0.0))),
                 op("set_route", connection_id="conn-drum-1-down", assumption="route",
                    points_m=orthogonal(ports["port-drum-1-outlet"], ports["port-boundary-inlet"],
                                        ports["port-drum-1-outlet"][2]))],
                ASSUMPTIONS, summary="Insert drum D-101 on L-105, place it, move the boundary after it and route.")


@unittest.skipIf(_MISSING, f"real generators unavailable in this interpreter: {', '.join(_MISSING)}")
class RealPlanTests(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)

    def test_resolved_composed_plan_publishes_all_five_outputs(self):
        document = load_document(str(self.path))
        planner = FakePlanner(resolved_drum_plan(document))
        service = InteractiveService(str(self.path), timeout=BUILD_TIMEOUT_S, terminate_grace=2.0, planner=planner)
        self.addCleanup(service.close)
        proposal = service.propose({"text": "add a drum on L-105, placed and routed", "expected_revision": 0,
                                    "mode": "engineering"})
        self.assertEqual(proposal["status"], "ready", proposal["summary"])
        self.assertEqual((proposal["command_count"], proposal["operation_count"]), (1, 4))
        self.assertFalse(proposal["validation"]["geometry_checked"])
        state = service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        self.assertEqual((state["project"]["revision"], state["history"]["undo"]), (1, 1))
        self.assertEqual([d for d in state["diagnostics"] if d["severity"] == "warning"
                          and d["code"] not in ("generator_unavailable",)], [])
        state = wait_terminal(service, timeout=BUILD_TIMEOUT_S)
        build = state["build"]
        if build["status"] != "complete":
            log = Path(service.builds_root, build["id"], "worker.log")
            tail = log.read_text("utf-8", "replace")[-3000:] if log.exists() else ""
            self.fail(f"build {build['status']}: {json.dumps(build['diagnostics'][:6], indent=1)}\n{tail}")
        self.assertEqual(build["artifact_status"], dict.fromkeys(integrity.REQUIRED_ARTIFACTS, "complete"))
        self.assertEqual((state["published"]["source_revision"], state["sync"]["state"]), (1, "synchronized"))
        root = Path(service.builds_root, build["id"])
        scene = json.loads((root / "scene.json").read_text())
        self.assertTrue(any(c["semantic_id"] == "ent-drum-1" for c in scene["components"]))
        self.assertIn('data-semantic-id="ent-drum-1"', (root / "pid.svg").read_text())
        self.assertIn("D-101", (root / "description.json").read_text())
        cad = json.loads((root / "cad.manifest.json").read_text())
        self.assertTrue(cad["representations"].get("ent-drum-1"))


if __name__ == "__main__":
    unittest.main()
