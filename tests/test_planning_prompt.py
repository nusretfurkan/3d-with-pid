"""The prompt communicates exactly the output limits the local checker enforces (single source: ``PLAN_SCHEMA``).

Regression for the first real planner run (October 14, 2026). A questions-only
request returned more than 8 questions, so the plan was correctly rejected. The
CLI schema has no limit keywords, and the prompt had said "items <= 600"
although questions/unknowns allow 400 characters; it never stated the 8-question
cap or the question-id pattern. The planner here is the LABELED FAKE
``FakePlanner``; no model is called.
"""

from __future__ import annotations

import copy
import re
import unittest
from unittest import mock

from skid.core import load_document
from skid.planning import prompt as prompt_module
from skid.planning import schema as schema_module
from skid.planning.context import build_context
from skid.planning.engineering import propose_engineering
from skid.planning.prompt import MARKER, build_prompt, instructions
from skid.planning.schema import PLAN_SCHEMA, cli_schema, validate_plan
from tests.test_interactive_fakes import FIXTURE
from tests.test_planning_fakes import FakePlanner, plan

LIMIT_KEYWORDS = ("minItems", "maxItems", "maxLength", "pattern", "minimum", "maximum", "enum")


def enforced_bounds(node, path=""):
    """Independently enumerate ``(path, keyword, value)`` for every limit in the schema."""
    if "anyOf" in node:
        for variant in node["anyOf"]:
            name = variant["properties"]["op"]["enum"][0]
            for key, sub in variant["properties"].items():
                if key != "op":
                    yield from enforced_bounds(sub, f"{path} (op {name}).{key}")
        return
    for keyword in LIMIT_KEYWORDS:
        if keyword in node and not (keyword == "minItems" and node[keyword] == 0):
            yield path, keyword, node[keyword]
    if node.get("type") == "object":
        for key, sub in node["properties"].items():
            yield from enforced_bounds(sub, f"{path}.{key}" if path else key)
    elif node.get("type") == "array":
        yield from enforced_bounds(node["items"], f"{path}[]")


def questions(count):
    return [{"id": f"q-{index}", "question": f"FAKE question {index}?", "reason": "FAKE reason."}
            for index in range(count)]


class PromptLimitTests(unittest.TestCase):
    def setUp(self):
        self.document = load_document(str(FIXTURE))
        self.prompt = build_prompt(build_context(self.document, "FAKE request"))
        self.head = self.prompt.partition(MARKER)[0]
        self.lines = {line[2:].split(": ", 1)[0]: line for line in self.head.splitlines() if line.startswith("- ")}

    def test_every_enforced_bound_is_stated_on_its_path(self):
        checked = 0
        for path, keyword, value in enforced_bounds(PLAN_SCHEMA):
            with self.subTest(path=path, keyword=keyword):
                self.assertIn(path, self.lines, f"no limit line for {path}")
                line = self.lines[path]
                if keyword == "pattern":
                    self.assertIn(f"^{value}$", line)
                elif keyword == "enum":
                    for item in value:
                        self.assertIn(item, line)
                elif keyword in ("minimum", "maximum"):
                    self.assertIn(f"{value:g}", line)
                else:
                    self.assertIn(str(value), line)
                checked += 1
        self.assertGreater(checked, 80)

    def test_the_failed_real_run_limits_are_now_explicit(self):
        cap = PLAN_SCHEMA["properties"]["questions"]["maxItems"]
        self.assertEqual(cap, 8)  # the checker is unchanged; only the communication was fixed
        self.assertEqual(self.lines["questions"], "- questions: at most 8 items")
        self.assertEqual(self.lines["questions[].id"], "- questions[].id: must fully match ^[a-z][a-z0-9-]{0,23}$")
        self.assertEqual(self.lines["questions[].question"], "- questions[].question: nonempty; at most 400 characters")
        self.assertEqual(self.lines["questions[].reason"], "- questions[].reason: nonempty; at most 400 characters")
        self.assertEqual(self.lines["unknowns"], "- unknowns: at most 24 items")
        self.assertEqual(self.lines["unknowns[]"], "- unknowns[]: nonempty; at most 400 characters")
        self.assertEqual(self.lines["summary"], "- summary: nonempty; at most 1000 characters")
        self.assertIn(f"Ask at most {cap} questions", self.head)
        self.assertNotIn("items <= 600", self.prompt)  # the old, inaccurate literal

    def test_grouping_guidance_keeps_every_required_topic(self):
        text = " ".join(self.head.split())
        for phrase in ("group related ones into a single question instead of dropping any",
                       "Name every required sub-item inside that question's text",
                       "Missing information that the user does not need to decide now belongs in \"unknowns\"",
                       "rejected as a whole: it is not truncated, repaired or retried"):
            self.assertIn(phrase, text)

    def test_limits_are_read_from_the_schema_when_the_prompt_is_built(self):
        changed = copy.deepcopy(PLAN_SCHEMA)
        changed["properties"]["questions"]["maxItems"] = 5
        changed["properties"]["unknowns"]["items"]["maxLength"] = 123
        changed["properties"]["operations"]["maxItems"] = 7
        with mock.patch.object(schema_module, "PLAN_SCHEMA", changed):
            head = build_prompt(build_context(self.document, "FAKE request")).partition(MARKER)[0]
            self.assertIn("- questions: at most 5 items", head)
            self.assertIn("Ask at most 5 questions", head)
            self.assertIn("- unknowns[]: nonempty; at most 123 characters", head)
            self.assertIn("at most 7, in execution order", head)
            self.assertEqual(validate_plan(plan(outcome="questions", questions=questions(6))),
                             [("plan.questions", "has more than 5 items")])  # the checker reads the same source
        self.assertEqual(instructions(changed), head)

    def test_static_text_has_no_separately_maintained_limits(self):
        digits = set(re.findall(r"\d+", prompt_module._TEXT))
        self.assertLessEqual(digits, {"3", "50", "1"}, "limits belong in PLAN_SCHEMA, rendered by limit_lines()")
        context = build_context(self.document, "FAKE request")
        limits = context["capabilities"]["limits"]
        self.assertEqual(set(limits), {"operations_per_plan", "operation_types", "plan_output"})
        self.assertEqual(limits["operations_per_plan"], PLAN_SCHEMA["properties"]["operations"]["maxItems"])


class StrictnessTests(unittest.TestCase):
    """Communicating the limits does not relax them: over-limit output is still rejected whole."""

    def setUp(self):
        self.document = load_document(str(FIXTURE))

    def test_cli_schema_still_omits_limits_and_the_checker_still_enforces_them(self):
        self.assertNotIn("maxItems", cli_schema()["properties"]["questions"])
        proposal = propose_engineering(self.document, "FAKE request", "prop-0123456789ab",
                                       FakePlanner(plan(outcome="questions", questions=questions(9))))
        self.assertEqual((proposal["status"], proposal["commands"], proposal["questions"]), ("rejected", [], []))
        self.assertEqual(proposal["validation"]["diagnostics"][0]["message"], "plan.questions: has more than 8 items")
        grouped = propose_engineering(self.document, "FAKE request", "prop-0123456789ab",
                                      FakePlanner(plan(outcome="questions", questions=questions(8))))
        self.assertEqual((grouped["status"], len(grouped["questions"]), grouped["commands"]), ("questions", 8, []))
        long_question = questions(1)
        long_question[0]["question"] = "x" * 401
        self.assertEqual(validate_plan(plan(outcome="questions", questions=long_question)),
                         [("plan.questions[0].question", "is longer than 400 characters")])


if __name__ == "__main__":
    unittest.main()
