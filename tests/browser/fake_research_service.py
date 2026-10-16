"""Serve one project with a SCRIPTED FAKE planner and a SCRIPTED FAKE researcher, for browser tests of creation and
research UI mechanics.

    .venv-cad/bin/python tests/browser/fake_research_service.py --project PATH --port 0

FAKE: no language model, CLI, web search or network access is involved and nothing here is evidence of AI
planning or research. The fakes return fixed structured plans and briefs chosen from the request text;
everything after them is the real service path: research brief re-validation, plan schema check,
translation into one batch (including the deterministic guided routes), core dry-run, stale/expiry
checks, apply through ``apply_command``, persistence and the real generators.

Plans (by request text): ``storage tank`` -> tank TK-101 with outlet valve HV-101 (valve outlet left open);
``centrifugal pump`` -> pump P-101 with its own suction valve HV-102 after HV-101 and boundary OUT-101;
``heat exchanger`` -> the shell-and-tube example HX-101 placed beside the train, its four ports open, citing
the research sources when a brief is present. ``fake-stages`` makes each stage take about four seconds;
``fake-slow`` makes research wait up to 120 s (honouring cancellation); ``fake-research-fail`` raises a
research error; ``fake-forged`` returns a source URL the fake's own search evidence does not contain.
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
from skid.planning.prompt import MARKER  # noqa: E402
from skid.research import ResearchError  # noqa: E402
from tests.test_creation_fakes import (exchanger_plan, fake_brief, fake_metadata, placed, pump_train_plan,  # noqa: E402
                                       tank_and_valve_plan)

PLANNER_LABEL = "SCRIPTED FAKE planner (no language model)"
RESEARCH_LABEL = "SCRIPTED FAKE researcher (no model, no web search)"
HOSTILE_TITLE = "FAKE <img src=x onerror=alert(1)> exchanger & \"nozzles\""


def _wait(seconds, cancel, closed, error):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if (cancel is not None and cancel.is_set()) or closed():
            raise error
        time.sleep(0.05)


class ScriptedResearcher:
    kind = "scripted-fake"

    def __init__(self):
        self.closed = False

    def describe(self):
        return {"kind": self.kind, "label": RESEARCH_LABEL, "model": "none (fake)", "effort": "n/a (fake)",
                "cli": "none", "cli_present": True, "timeout_s": 150, "tools": [], "fake": True}

    def shutdown(self):
        self.closed = True

    def run(self, request, cancel=None):
        cancelled = ResearchError("research_cancelled", "scripted fake: research was cancelled", {"fake": True})
        if "fake-slow" in request:
            _wait(120, cancel, lambda: self.closed, cancelled)
        if "fake-stages" in request:
            _wait(4, cancel, lambda: self.closed, cancelled)
        if "fake-research-fail" in request:
            raise ResearchError("research_no_evidence", "scripted fake: no successful search was observed",
                                {"fake": True, "label": RESEARCH_LABEL})
        brief = fake_brief(request)
        brief["sources"][1]["title"] = HOSTILE_TITLE
        brief["sources"][1]["observations"].append("FAKE: <script>alert('observation')</script> stays text.")
        metadata = fake_metadata(brief, label=RESEARCH_LABEL)
        if "fake-forged" in request:
            metadata["observed_urls"] = metadata["observed_urls"][:1]  # S2 was never in a search result
        return {"brief": brief, "metadata": metadata}


class ScriptedPlanner:
    kind = "scripted-fake"

    def __init__(self):
        self.closed = False

    def describe(self):
        return {"kind": self.kind, "label": PLANNER_LABEL, "model": "none (fake)", "cli": "none", "cli_present": True,
                "timeout_s": 150, "fake": True}

    def shutdown(self):
        self.closed = True

    def run(self, prompt, schema, cancel=None):
        metadata = {"fake": True, "label": PLANNER_LABEL, "model": "none (fake)",
                    "effort": {"requested": "n/a (fake)", "reported": None}, "verified": False}
        context = json.loads(prompt.partition(MARKER)[2])
        text = context["request"]["text"]
        cancelled = PlannerError("planner_cancelled", "scripted fake: the plan was cancelled", metadata)
        if "fake-stages" in text:
            _wait(4, cancel, lambda: self.closed, cancelled)
        if "heat exchanger" in text:
            cited = ["S1", "S2"] if context.get("research") else []
            the_plan = exchanger_plan(cited, placement=placed((1.6, 2.0, 0.0)))
        elif "centrifugal pump" in text:
            the_plan = pump_train_plan()
        elif "storage tank" in text:
            the_plan = tank_and_valve_plan()
        else:
            raise PlannerError("planner_error_result", "scripted fake: no scripted plan for this request", metadata)
        return {"plan": the_plan, "metadata": metadata}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--web-root", help="serve another copy of web/ (negative controls only)")
    args = parser.parse_args()
    service = InteractiveService(args.project, None, suite=REAL_SUITE, planner=ScriptedPlanner(),
                                 researcher=ScriptedResearcher())
    server = make_server(service, args.port, **({"web_root": args.web_root} if args.web_root else {}), quiet=True)
    print(f"Serving project at {server.url} with {PLANNER_LABEL} and {RESEARCH_LABEL}", flush=True)
    if service.state()["sync"]["state"] != "synchronized":
        service.request_build("fake-research test service started")
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
