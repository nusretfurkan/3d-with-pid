"""Interactive service with the REAL contracted generators (``skid.geometry``, ``skid.drafting``).

Skipped, with the reason stated, when either generator package or CadQuery is
not importable in the running interpreter (for example Blender's Python); run
with ``.venv-cad/bin/python``. These tests check the service's integration with
the generators: identity, progressive previews, publication, rename
propagation, pending state, cancellation and corruption detection. They do not
assess CAD accuracy or drawing quality, which belong to the generator suites
and visual review.
"""

from __future__ import annotations

import importlib.util
import json
import time
import unittest
from pathlib import Path

from skid.core import load_document
from skid.interactive import integrity
from skid.interactive.service import InteractiveService, ServiceError
from tests.test_interactive_fakes import new_project, wait_terminal, wait_until

_MISSING = [name for name in ("skid.geometry", "skid.drafting", "cadquery") if importlib.util.find_spec(name) is None]
BUILD_TIMEOUT_S = 600


@unittest.skipIf(_MISSING, f"real generators unavailable in this interpreter: {', '.join(_MISSING)}")
class RealGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)
        self.service = InteractiveService(str(self.path), timeout=BUILD_TIMEOUT_S, terminate_grace=2.0)
        self.addCleanup(self.service.close)

    def build(self, **body):
        self.service.build(body) if body else self.service.request_build("real generator test")
        state = wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)
        build = state["build"]
        if build["status"] == "failed":
            log = Path(self.service.builds_root, build["id"], "worker.log")
            tail = log.read_text("utf-8", "replace")[-3000:] if log.exists() else ""
            self.fail(f"real build failed: {json.dumps(build['diagnostics'][:6], indent=1)}\nworker log tail:\n{tail}")
        return state

    def file(self, state, key):
        build_id, relpath = state["published"]["artifacts"][key][len("/artifacts/"):].split("/", 1)
        return Path(self.service.artifact_path(build_id, relpath))

    def test_real_build_publishes_five_verified_outputs_with_progressive_previews(self):
        state = self.build()
        build = state["build"]
        self.assertEqual(build["status"], "complete", json.dumps(build["diagnostics"][:5], indent=1))
        self.assertEqual(build["artifact_status"], dict.fromkeys(integrity.REQUIRED_ARTIFACTS, "complete"))
        published = state["published"]
        self.assertEqual((published["build_id"], published["source_revision"]), (build["id"], 0))
        self.assertEqual(state["sync"]["state"], "synchronized")
        pointer = json.loads(Path(self.service.root, "current.json").read_text())
        self.assertEqual(integrity.verify_pointer(self.service.root, pointer), [])
        events = build["events"]
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        worker = [e for e in events if e["source"] == "worker"]
        for artifact in ("display", "pid", "legend"):
            previews = [i for i, e in enumerate(worker) if e["kind"] == "preview" and e["artifact"] == artifact]
            finals = [i for i, e in enumerate(worker) if e["kind"] == "artifact" and e["artifact"] == artifact]
            self.assertGreaterEqual(len(previews), 2, f"{artifact} should develop progressively")
            if finals:
                self.assertLess(previews[0], finals[0], artifact)
        for event in worker:
            if "url" in event:
                build_id, relpath = event["url"][len("/artifacts/"):].split("/", 1)
                self.assertIsNotNone(self.service.artifact_path(build_id, relpath), event["url"])
        scene = json.loads(self.file(state, "display").read_text())
        self.assertEqual((scene["project_id"], scene["source_revision"], scene["input_digest"]),
                         ("project-pump-skid", 0, published["input_digest"]))
        self.assertTrue(self.file(state, "cad").read_bytes().startswith(b"ISO-10303-21;"))
        state = self.service.build({})
        self.assertIn("build_not_needed", {d["code"] for d in state["diagnostics"]})

    def test_proposal_rename_propagates_to_all_outputs_and_undo_restores(self):
        self.assertEqual(self.build()["build"]["status"], "complete")
        proposal = self.service.propose({"text": "rename the pump to P-201", "expected_revision": 0})
        self.assertTrue(proposal["validation"]["accepted"])
        self.service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        state = wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)
        self.assertEqual(state["build"]["status"], "complete", state["build"]["diagnostics"][:5])
        self.assertEqual(state["published"]["source_revision"], 1)
        for key in ("pid", "legend", "description", "index", "display"):
            text = self.file(state, key).read_text("utf-8")
            self.assertIn("P-201", text, key)
            self.assertNotIn(">P-101<", text, key)
        description = json.loads(self.file(state, "description").with_name("description.json").read_text())
        self.assertEqual(description["source_revision"], 1)
        state = self.service.command({"id": "cmd-real-undo", "expected_revision": 1, "type": "undo", "payload": {}})
        state = wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)
        self.assertEqual(state["published"]["source_revision"], 2)
        self.assertIn("P-101", self.file(state, "pid").read_text("utf-8"))
        self.assertEqual(load_document(str(self.path))["current"]["model"]["entities"]["ent-pump"]["tag"], "P-101")

    def test_physical_move_is_pending_and_never_published(self):
        published = self.build()["published"]
        proposal = self.service.propose({"text": "move P-101 0.1 m in +y", "expected_revision": 0})
        self.service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        state = wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)
        build = state["build"]
        self.assertEqual(build["status"], "pending", build["message"])
        self.assertEqual(state["published"]["build_id"], published["build_id"])
        self.assertEqual(state["sync"]["state"], "pending")
        self.assertEqual(state["artifact_sources"]["display"]["origin"], "working")
        self.assertTrue(any(d["severity"] == "warning" for d in build["diagnostics"]))

    def test_cancelling_a_real_build_keeps_the_pointer(self):
        self.assertEqual(self.build()["build"]["status"], "complete")
        pointer = Path(self.service.root, "current.json").read_bytes()
        for _ in range(5):
            build_id = self.service.build({"force": True})["build"]["id"]
            wait_until(self.service, lambda s: s["build"]["previews"] or s["build"]["status"] != "running",
                       timeout=BUILD_TIMEOUT_S, what="a first real preview")
            try:
                self.service.cancel({"build_id": build_id})
            except ServiceError:
                wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)
                pointer = Path(self.service.root, "current.json").read_bytes()
                continue  # finished before the cancel arrived; try again
            state = wait_terminal(self.service, build_id, timeout=60)
            self.assertEqual(state["build"]["status"], "cancelled")
            self.assertEqual(state["build"]["artifacts"], {})
            self.assertEqual(Path(self.service.root, "current.json").read_bytes(), pointer)
            # A cancelled forced rebuild does not make the verified publication of this revision less current.
            self.assertEqual(state["sync"]["state"], "synchronized")
            self.assertEqual(state["artifact_sources"]["pid"]["build_status"], "cancelled")
            return
        self.skipTest("every real build finished before its cancellation arrived")

    def test_requests_stay_responsive_during_a_real_build(self):
        self.service.request_build("responsiveness")
        latencies, selections = [], 0
        while self.service.state()["build"]["status"] in ("queued", "running"):
            started = time.monotonic()
            state = self.service.state()
            latencies.append(time.monotonic() - started)
            ids = ["ent-pump"] if selections % 2 == 0 else ["ent-tank"]
            self.service.select({"semantic_ids": ids, "origin": "display"})
            selections += 1
        self.assertGreater(len(latencies), 3, "the build finished before responsiveness could be sampled")
        self.assertLess(max(latencies), 1.0, f"state latency while building: max {max(latencies):.3f} s")
        self.assertEqual(self.service.state()["selection"]["sequence"], selections)
        self.assertEqual(wait_terminal(self.service, timeout=BUILD_TIMEOUT_S)["project"]["revision"], 0)

    def test_corrupted_real_output_is_reported_not_trusted(self):
        state = self.build()
        target = self.file(state, "legend")
        original = target.read_bytes()
        target.write_bytes(original.replace(b"</svg>", b"<!-- edited --></svg>"))
        try:
            self.service.select({"semantic_ids": ["ent-pump"], "origin": "pid"})
            state = self.service.state()
            self.assertIsNone(state["published"])
            self.assertIn("published_build_file_changed", {d["code"] for d in state["diagnostics"]})
            self.assertEqual((state["artifacts"], state["artifact_sources"]), ({}, {}))
            self.assertEqual((state["build"]["integrity"], state["build"]["published"]), ("rejected", False))
            self.assertEqual(state["sync"]["state"], "invalid")
            self.assertIsNone(state["selection"]["resolved"][0]["display_components"])
        finally:
            target.write_bytes(original)
        state = self.service.state()
        self.assertIsNotNone(state["published"])
        self.assertEqual(state["artifact_sources"]["legend"]["origin"], "published")
        self.assertTrue(state["selection"]["resolved"][0]["display_components"])


if __name__ == "__main__":
    unittest.main()
