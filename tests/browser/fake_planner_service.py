"""Serve one project with a SCRIPTED FAKE engineering planner, for browser tests of the composer UI.

    .venv-cad/bin/python tests/browser/fake_planner_service.py --project PATH --port 0

FAKE: no language model, CLI or network is involved and nothing here is evidence of AI planning.
The fake returns fixed structured plans chosen by a marker in the request text; everything after
that is the real service path: plan schema check, translation into one batch envelope, core
dry-run, stale/expiry checks, apply through ``apply_command`` and the real generators.

Markers: ``fake-questions``, ``fake-eight`` (replays the eight questions of an actual planner reply
from ``unit/fixtures/real-questions-8.json``), ``fake-unavailable``, ``fake-slow`` (waits up to 120 s,
honouring cancellation), ``fake-drum`` (inserts the example horizontal drum, placement unresolved);
anything else gets a two-operation rename + motor-length plan for P-101.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skid.interactive.generators import REAL_SUITE  # noqa: E402
from skid.interactive.server import make_server  # noqa: E402
from skid.interactive.service import InteractiveService  # noqa: E402
from skid.planning.bridge import PlannerError  # noqa: E402

LABEL = "SCRIPTED FAKE (no language model)"


def _plan(summary, operations=(), assumptions=(), unknowns=(), questions=(), impacts=(), outcome="proposal",
          reasoning=("Scripted fake plan for UI testing; not produced by a language model.",)):
    return {"outcome": outcome, "summary": summary, "reasoning": list(reasoning), "operations": list(operations),
            "assumptions": list(assumptions), "unknowns": list(unknowns), "questions": list(questions),
            "impacts": list(impacts)}


def _rename_and_motor():
    return _plan(
        "Rename P-101 to P-201 and set its motor length to 0.42 m.",
        operations=[
            {"op": "rename_entity", "purpose": "The request names the new tag.",
             "args": {"entity_id": "ent-pump", "tag": "P-201"}},
            {"op": "set_parameter", "purpose": "The request gives the new motor length.",
             "args": {"entity_id": "ent-pump", "name": "motor_length", "value": 0.42, "unit": "m",
                      "assumption": "motor"}},
        ],
        assumptions=[{"key": "motor", "description": "User-requested illustrative motor length of 0.42 m.",
                      "source_ids": []}],
        unknowns=["Motor rating and duty are not specified."],
        impacts=[{"ids": ["ent-pump"], "effect": "Pump tag and motor envelope change; routes keep their points."}],
    )


def _drum():
    return _plan(
        "Insert the example horizontal drum into L-104 between NRV-101 and HV-102; its 3D placement stays unresolved.",
        operations=[{"op": "insert_equipment", "purpose": "The request asks for a hold-up drum in that line.",
                     "args": {"connection_id": "conn-discharge-2", "entity_id": "ent-drum-fake", "tag": "D-101",
                              "downstream_tag": "L-106",
                              "parameters": [{"name": name, "value": value, "unit": "m", "assumption": "drum"}
                                             for name, value in (("shell_length", 1.4), ("shell_diameter", 0.6),
                                                                 ("centreline_height", 0.75), ("inlet_offset", 0.6))],
                              "symbol": {"x": 540.0, "y": 140.0, "rotation_deg": 0.0},
                              "placement": {"status": "unresolved", "position_m": [], "rotation_deg": [],
                                            "assumption": ""},
                              "definition_ref": "example:horizontal_drum", "definition_json": ""}}],
        assumptions=[{"key": "drum", "description": "Illustrative drum dimensions equal to the example definition's "
                                                    "authoring defaults.", "source_ids": []}],
        unknowns=["Drum capacity, duty and ratings are unspecified."],
        impacts=[{"ids": ["conn-discharge-2"], "effect": "L-104 now ends at the drum; a new line continues to HV-102."}],
    )


def _eight_questions():
    """The eight questions of an actual planner reply (copied fixture), replayed by the fake."""
    fixture = Path(__file__).resolve().parent / "unit" / "fixtures" / "real-questions-8.json"
    questions = json.loads(fixture.read_text(encoding="utf-8"))["questions"]
    return _plan("Replayed questions (fixture copied from an actual planner reply; replayed by the scripted fake).",
                 outcome="questions", questions=[{k: q[k] for k in ("id", "question", "reason")} for q in questions])


def _questions():
    return _plan("The request leaves the insertion point open.", outcome="questions",
                 questions=[{"id": "line", "question": "Which line should receive the drum: L-103 or L-104?",
                             "reason": "Both lines could hold an inline drum."},
                            {"id": "tag", "question": "Which tag should the new drum have?",
                             "reason": "No tag was given."}])


class ScriptedFakePlanner:
    kind = "scripted-fake"

    def __init__(self):
        self.closed = False

    def describe(self):
        return {"kind": self.kind, "label": LABEL, "model": "none (fake)", "cli": "none", "cli_present": True,
                "timeout_s": 150, "fake": True}

    def shutdown(self):
        self.closed = True

    def run(self, prompt, schema, cancel=None):
        # same field shapes as the real bridge metadata (effort {requested, reported}); values say "fake"
        metadata = {"fake": True, "label": LABEL, "model": "none (fake)",
                    "effort": {"requested": "n/a (fake)", "reported": None}, "verified": False}
        if "fake-unavailable" in prompt:
            raise PlannerError("planner_unavailable", "scripted fake: the planner is unavailable", metadata)
        if "fake-slow" in prompt:
            end = time.monotonic() + 120
            while time.monotonic() < end:
                if (cancel is not None and cancel.is_set()) or self.closed:
                    raise PlannerError("planner_cancelled", "scripted fake: the plan was cancelled", metadata)
                time.sleep(0.05)
        if "fake-eight" in prompt:
            return {"plan": _eight_questions(), "metadata": metadata}
        if "fake-questions" in prompt:
            return {"plan": _questions(), "metadata": metadata}
        if "fake-drum" in prompt:
            return {"plan": _drum(), "metadata": metadata}
        return {"plan": _rename_and_motor(), "metadata": metadata}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    # research disabled: this harness must never reach a real researcher (see fake_research_service.py)
    service = InteractiveService(args.project, None, suite=REAL_SUITE, planner=ScriptedFakePlanner(), researcher=False)
    server = make_server(service, args.port, quiet=True)
    print(f"Serving project at {server.url} with {LABEL}", flush=True)
    if service.state()["sync"]["state"] != "synchronized":
        service.request_build("fake-planner test service started")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
