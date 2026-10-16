"""Cross-view integration through the documented source invocation ``python -m skid.cli``.

Real core and real P&ID adapter throughout. ``CliBuildTest`` replaces only the
Blender subprocess with ``FakeBlender`` (a pure-Python plan manifest and a
placeholder blend file), so it checks orchestration and cross-view identity, not
Blender geometry. ``ActualBlenderTest`` runs actual Blender and is skipped
unless ``SKID_BLENDER`` names a Blender executable.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skid import cli
from skid.adapters.blender_support.plan import GENERATOR, plan_scene
from skid.build import BOOTSTRAP, find_blender, project_status
from skid.core import load_document, save_document, validate_project
from tests.test_build_failures import FakeBlender
from tests.test_geometry_contract import insert_hv103, move_pump, place_hv103

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "pump_skid.json"
SNAPSHOT_KEYS = ("model", "pid", "physical")
BLENDER = find_blender(os.environ["SKID_BLENDER"]) if os.environ.get("SKID_BLENDER") else None


def content(document):
    return copy.deepcopy({key: document["current"][key] for key in SNAPSHOT_KEYS})


def run_cli(*args, stdin=None, timeout=120):
    return subprocess.run([sys.executable, "-m", "skid.cli", *map(str, args)], cwd=ROOT, input=stdin,
                          capture_output=True, text=True, timeout=timeout)


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="skid cli ")
        self.path = Path(self._tmp.name, "demo dir", "pump skid.json")
        self.out = self.path.with_name("pump skid.skid")

    def tearDown(self):
        self._tmp.cleanup()


class CliWorkflowTest(Workspace):
    """A08/A09 through the CLI: save, reload, rename, stale and invalid edits, undo, redo, manual layout."""

    def cli(self, *args, code=0, stdin=None):
        result = run_cli(*args, stdin=stdin)
        self.assertEqual(result.returncode, code, f"{args}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def test_sample_rename_undo_redo_and_conflicts(self):
        fixture = load_document(FIXTURE)
        self.assertIn("created", self.cli("sample", self.path).stdout)
        original = self.path.read_bytes()
        self.assertIn("already exists", self.cli("sample", self.path, code=4).stderr)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIn("VALID:", self.cli("validate", self.path).stdout)
        listing = self.cli("lists", self.path).stdout
        self.assertRegex(listing, r"P-101\s+centrifugal_pump\s+ent-pump")
        self.assertIn("UNPUBLISHED", self.cli("status", self.path, code=4).stdout)

        self.cli("rename", self.path, "ent-pump", "P-201", code=2)  # --expected-revision is required
        self.assertIn("revision 0 -> 1", self.cli("rename", self.path, "ent-pump", "P-201",
                                                  "--expected-revision", 0).stdout)
        renamed = load_document(self.path)
        project = renamed["current"]
        self.assertEqual((project["id"], project["revision"], len(renamed["undo"])), (fixture["current"]["id"], 1, 1))
        self.assertEqual(project["model"]["entities"]["ent-pump"]["tag"], "P-201")
        expected = content(fixture)
        expected["model"]["entities"]["ent-pump"]["tag"] = "P-201"
        self.assertEqual(content(renamed), expected)  # ports, connections, symbols, routes untouched

        before = self.path.read_bytes()
        stale = self.cli("rename", self.path, "ent-tank", "TK-9", "--expected-revision", 0, code=4)
        self.assertIn("stale_revision", stale.stderr)
        duplicate = self.cli("rename", self.path, "ent-tank", " p-201 ", "--expected-revision", 1, code=1)
        self.assertIn("rejected", duplicate.stderr)
        self.assertEqual(self.path.read_bytes(), before)

        self.assertIn("revision 1 -> 2", self.cli("undo", self.path, "--expected-revision", 1).stdout)
        undone = load_document(self.path)
        self.assertEqual((undone["current"]["revision"], len(undone["undo"]), len(undone["redo"])), (2, 0, 1))
        self.assertEqual(content(undone), content(fixture))  # manual symbols and routes restored exactly
        self.assertTrue(any(s["manual"] for s in undone["current"]["pid"]["symbols"].values()))
        self.assertTrue(any(r["mode"] == "manual" for r in undone["current"]["physical"]["routes"].values()))

        self.cli("redo", self.path, "--expected-revision", 2)
        redone = load_document(self.path)
        self.assertEqual((redone["current"]["revision"], redone["current"]["model"]["entities"]["ent-pump"]["tag"]),
                         (3, "P-201"))
        self.cli("redo", self.path, "--expected-revision", 3, code=1)  # nothing left to redo

    def test_apply_reads_a_command_envelope(self):
        self.cli("sample", self.path)
        symbol = load_document(self.path)["current"]["pid"]["symbols"]["sym-pump"]
        command = {"type": "move_symbol", "payload": {"symbol_id": "sym-pump", "x": symbol["x"] + 40.0,
                                                      "y": symbol["y"], "rotation_deg": symbol["rotation_deg"]}}
        self.cli("apply", self.path, "-", stdin=json.dumps(command), code=2)  # no expected revision anywhere
        self.cli("apply", self.path, "-", "--expected-revision", 0, stdin=json.dumps(command))
        moved = load_document(self.path)["current"]
        fixture = load_document(FIXTURE)["current"]
        self.assertEqual(moved["pid"]["symbols"]["sym-pump"]["x"], symbol["x"] + 40.0)
        self.assertEqual(moved["physical"], fixture["physical"])  # A06: no physical change
        self.assertEqual(moved["model"], fixture["model"])
        self.cli("validate", self.path, code=3 if validate_project(moved) else 0)

        duplicate = dict(command, expected_revision=1, payload=dict(command["payload"], x=symbol["x"] + 40.0))
        self.assertIn("no change", self.cli("apply", self.path, "-", stdin=json.dumps(duplicate)).stdout)
        self.assertEqual(load_document(self.path)["current"]["revision"], 1)
        bad = dict(command, payload=dict(command["payload"], symbol_id="sym-missing"))
        self.cli("apply", self.path, "-", "--expected-revision", 1, stdin=json.dumps(bad), code=1)
        self.cli("apply", self.path, "-", "--expected-revision", 1, stdin='{"type": "undo", "type": "redo"}', code=1)


class CliBuildTest(Workspace):
    """A01, A02, A04, A05 and A10 for offline outputs, with the Blender subprocess faked."""

    def setUp(self):
        super().setUp()
        self.runner = FakeBlender()
        patcher = mock.patch("skid.build.run_subprocess", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(run_cli("sample", self.path).returncode, 0)

    def main(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main([str(a) for a in args])
        return code, output.getvalue()

    def build(self, code=0):
        # The fake runner never executes the --blender path; it only needs to name an executable file.
        result = self.main("build", self.path, "--blender", sys.executable)
        self.assertEqual(result[0], code, result[1])
        return result[1]

    def bundle(self):
        pointer = json.loads((self.out / "current.json").read_text(encoding="utf-8"))
        folder = self.out / "bundles" / pointer["bundle"]
        read = lambda name: json.loads((folder / name).read_text(encoding="utf-8"))  # noqa: E731
        return pointer, folder, read("pid.manifest.json"), read("blender.manifest.json"), read("lists.json")

    def test_both_views_and_lists_follow_a_rename_and_undo(self):
        self.assertIn("COMPLETE", self.build())
        pointer, folder, pid, blender, lists = self.bundle()
        project = load_document(self.path)["current"]
        for manifest in (pid, blender):
            self.assertEqual((manifest["project_id"], manifest["source_revision"], manifest["input_digest"]),
                             (project["id"], 0, pointer["input_digest"]))
            self.assertEqual(manifest["status"], "complete")
        ids = set(project["model"]["entities"]) | set(project["model"]["ports"]) | set(project["model"]["connections"])
        self.assertLessEqual(ids, set(pid["representations"]) & set(blender["representations"]))
        svg = (folder / "pid.svg").read_text(encoding="utf-8")
        for entity_id, entity in project["model"]["entities"].items():
            self.assertIn(f'data-semantic-id="{entity_id}"', svg)
            self.assertIn(entity["tag"], svg)
        self.assertIn("does not show that a 3D build succeeded", svg)  # the SVG alone never certifies Blender
        self.assertEqual([row["tag"] for row in lists["lines"]], ["L-101", "L-102", "L-103", "L-104", "L-105"])
        pump = next(row for row in lists["entities"] if row["id"] == "ent-pump")
        self.assertEqual(pump["parameters"]["body_length"]["assumption_id"], "asm-pump-dimensions")
        self.assertEqual({p["connection_id"] for p in pump["ports"]}, {"conn-suction-2", "conn-discharge-1"})
        self.assertEqual(self.main("status", self.path)[0], 0)
        self.assertIn("already published, unchanged", self.build())  # A10: no duplicate bundle
        self.assertEqual(len(os.listdir(self.out / "bundles")), 1)

        self.assertEqual(run_cli("rename", self.path, "ent-pump", "P-201", "--expected-revision", 0).returncode, 0)
        code, text = self.main("status", self.path)
        self.assertEqual(code, 4)
        self.assertIn("STALE", text)
        self.build()
        _, folder1, pid1, blender1, lists1 = self.bundle()
        svg1 = (folder1 / "pid.svg").read_text(encoding="utf-8")
        self.assertIn("P-201", svg1)
        self.assertNotIn("P-101", svg1)
        self.assertEqual(next(r["tag"] for r in lists1["entities"] if r["id"] == "ent-pump"), "P-201")
        for before, after in ((pid, pid1), (blender, blender1)):  # identity-keyed mappings are unchanged (A02)
            self.assertEqual(before["representations"], after["representations"])
            self.assertEqual(after["source_revision"], 1)

        self.assertEqual(run_cli("undo", self.path, "--expected-revision", 1).returncode, 0)
        self.build()
        _, folder2, pid2, _, lists2 = self.bundle()
        self.assertEqual(pid2["source_revision"], 2)
        self.assertEqual(next(r["tag"] for r in lists2["entities"] if r["id"] == "ent-pump"), "P-101")
        self.assertEqual(len(os.listdir(self.out / "bundles")), 3)

    def test_unplaced_valve_and_moved_pump_publish_pending_bundles(self):
        self.build()
        document = load_document(self.path)
        save_document(self.path, insert_hv103(document), 0)
        self.assertEqual(self.main("validate", self.path)[0], 3)
        text = self.build(code=3)
        self.assertIn("PENDING", text)
        pointer, folder, pid, blender, lists = self.bundle()
        self.assertEqual((pointer["status"], pid["status"], blender["status"]), ("pending", "pending", "pending"))
        self.assertIn("OPEN ITEMS", (folder / "pid.svg").read_text(encoding="utf-8"))
        valve = next(row for row in lists["entities"] if row["id"] == "ent-hv-103")
        self.assertEqual((valve["tag"], valve["placement"]["status"]), ("HV-103", "unresolved"))
        self.assertEqual(blender["representations"]["ent-hv-103"], [])
        code, text = self.main("status", self.path)
        self.assertEqual(code, 3)
        self.assertIn("PENDING", text)

        # Placing the valve completes the 3D view, but the P&ID edge is still unresolved: the bundle stays pending.
        document = load_document(self.path)
        save_document(self.path, place_hv103(document), document["current"]["revision"])
        self.build(code=3)
        pointer, _, pid, blender, _ = self.bundle()
        self.assertEqual((pointer["status"], pid["status"], blender["status"]), ("pending", "pending", "complete"))

        document = load_document(self.path)
        save_document(self.path, move_pump(document), document["current"]["revision"])
        self.build(code=3)
        _, _, _, blender, lists = self.bundle()
        self.assertEqual(blender["status"], "pending")
        stale = {row["id"]: row["route"]["status"] for row in lists["lines"]}
        self.assertEqual((stale["conn-suction-2"], stale["conn-discharge-1"]), ("needs_reroute", "needs_reroute"))


@unittest.skipUnless(BLENDER, "set SKID_BLENDER to an actual Blender executable to run this check")
class ActualBlenderTest(Workspace):
    """Actual Blender: CLI build, saved-file census, and an unchanged rebuild."""

    def test_cli_build_saves_only_project_content(self):
        blender = BLENDER
        self.assertEqual(run_cli("sample", self.path).returncode, 0)
        result = run_cli("build", self.path, "--blender", blender, timeout=1200)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COMPLETE", result.stdout)
        status = project_status(self.path)
        self.assertEqual(status["state"], "synchronized", status["problems"])
        folder = self.out / "bundles" / status["published"]["bundle"]

        report = Path(self._tmp.name, "census.json")
        census_run = subprocess.run([blender, "--background", "--factory-startup", "--python-exit-code", "1",
                                     "--python", BOOTSTRAP, "--", "inspect", "--blend", str(folder / "model.blend"),
                                     "--report", str(report)], capture_output=True, text=True, timeout=600)
        self.assertEqual(census_run.returncode, 0, census_run.stdout + census_run.stderr)
        census = json.loads(report.read_text(encoding="utf-8"))
        project = load_document(folder / "snapshot.json")["current"]
        planned = {spec["key"] for spec in plan_scene(project)["objects"]}
        self.assertEqual({o["representation_id"] for o in census["objects"]}, planned)
        self.assertEqual(len(census["objects"]), len(planned))  # no default Cube/Camera/Light, no duplicates
        for obj in census["objects"]:
            self.assertEqual((obj["skid_generator"], obj["project_id"]), (GENERATOR, project["id"]), obj["name"])
        for collection in census["collections"]:
            self.assertEqual((collection["skid_generator"], collection["project_id"]), (GENERATOR, project["id"]))
        self.assertEqual((census["counts"]["cameras"], census["counts"]["lights"]), (0, 0))

        again = run_cli("build", self.path, "--blender", blender, timeout=1200)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already published, unchanged", again.stdout)
        self.assertEqual(len(os.listdir(self.out / "bundles")), 1)


if __name__ == "__main__":
    unittest.main()
