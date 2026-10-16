"""Snapshot provenance: recipe content digest, standalone rebuild labelling and rejection of edits.

    .venv-cad/bin/python -m unittest tests.test_cad_snapshot -v

Regression for review finding 4: an edited geometry.recipe.json must not be
rebuilt under the authoritative project identity. Unchanged snapshots stay
rebuildable and every rebuild is labelled as a snapshot rebuild carrying the
recipe content digest and the source identity it records.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unittest

try:
    import OCP  # noqa: F401  (CadQuery's OCCT bindings, only in .venv-cad)
except ImportError:  # e.g. Blender's Python running the existing suite
    raise unittest.SkipTest("OCP/CadQuery unavailable; run with .venv-cad/bin/python")

from skid.core import apply_command
from skid.core.validation import canonical_digest

from skid.geometry import SnapshotError, build_geometry, rebuild_from_recipe
from skid.geometry.__main__ import main as cli
from skid.geometry.authored import build_authored
from skid.geometry.snapshot import DIGEST_FIELD, SNAPSHOT_SCHEMA, content_digest, with_digest
from skid.geometry.step import header_facts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "examples", "pump_skid.json")
DRUM = os.path.join(ROOT, "examples", "recipes", "horizontal_drum.recipe.json")
OLD_EVIDENCE = os.path.join(ROOT, "artifacts", "cad-spike", "fixture-r0", "geometry.recipe.json")
_DIRS = []


def tearDownModule():
    for path in _DIRS:
        shutil.rmtree(path, ignore_errors=True)


def new_dir():
    path = tempfile.mkdtemp(prefix="test-cad-snapshot-")
    _DIRS.append(path)
    return path


def load(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return json.load(handle)


def fixture_project():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)["current"]


def write_snapshot(document, text=None):
    path = os.path.join(new_dir(), "geometry.recipe.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text if text is not None else json.dumps(document, indent=1))
    return path


def unit(document, unit_id):
    return [u for u in document["units_built"] if u["id"] == unit_id][0]


def step_facts(directory):
    with open(os.path.join(directory, "model.step"), encoding="ascii") as handle:
        return header_facts(handle.read())


class SnapshotProvenanceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.project = fixture_project()
        cls.out = new_dir()
        cls.events = []
        cls.result = build_geometry(cls.project, cls.out, emit=cls.events.append)
        cls.recipe = load(cls.out, "geometry.recipe.json")
        cls.recipe_path = os.path.join(cls.out, "geometry.recipe.json")
        cls.rebuild_dir = new_dir()
        cls.rebuild_events = []
        cls.rebuilt = rebuild_from_recipe(cls.recipe_path, cls.rebuild_dir, emit=cls.rebuild_events.append)

    def rejected(self, document, pattern, text=None):
        """Rebuilding ``document`` raises SnapshotError matching ``pattern`` and writes nothing."""
        out = new_dir()
        with self.assertRaisesRegex(SnapshotError, pattern):
            rebuild_from_recipe(write_snapshot(document, text), out)
        self.assertEqual(os.listdir(out), [])

    def test_project_build_records_recipe_digest_without_changing_contract_identity(self):
        digest = canonical_digest(self.project)
        recipe_sha = self.recipe[DIGEST_FIELD]
        self.assertEqual(self.recipe["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(content_digest(self.recipe), recipe_sha)
        self.assertNotEqual(recipe_sha, digest)
        expected = {"kind": "project_build", "recipe_sha256": recipe_sha, "recipe_file": "geometry.recipe.json",
                    "source": {"project_id": "project-pump-skid", "source_revision": 0, "input_digest": digest}}
        scene = load(self.out, "scene.json")
        for record in (self.result["display"]["provenance"], self.result["cad"]["provenance"], scene["provenance"],
                       load(self.out, "cad.verification.json")["provenance"]):
            self.assertEqual({k: record[k] for k in expected}, expected)
        for name in ("display", "cad"):
            manifest = self.result[name]
            self.assertEqual((manifest["status"], manifest["project_id"], manifest["source_revision"],
                              manifest["input_digest"]), ("complete", "project-pump-skid", 0, digest))
        description = step_facts(self.out)["description"]
        self.assertIn("provenance=project_build;", description)
        self.assertIn(f"recipe_sha256={recipe_sha};", description)
        self.assertIn(f"source_input_digest={digest};", description)
        preview = load(self.out, "preview/display-0001.json")["provenance"]
        self.assertEqual((preview["kind"], preview["recipe_sha256"]), ("project_build", None))

    def test_unchanged_snapshot_rebuilds_with_same_geometry_mappings_and_rebuild_label(self):
        original, rebuilt = self.result, self.rebuilt
        self.assertEqual((rebuilt["display"]["status"], rebuilt["cad"]["status"]), ("complete", "complete"))
        for name in ("display", "cad"):
            self.assertEqual(rebuilt[name]["representations"], original[name]["representations"])
            provenance = rebuilt[name]["provenance"]
            self.assertEqual(provenance["kind"], "snapshot_rebuild")
            self.assertEqual(provenance["recipe_sha256"], self.recipe[DIGEST_FIELD])
            self.assertEqual(provenance["source"], original[name]["provenance"]["source"])
            self.assertEqual(provenance["snapshot_source_kind"], "project")
            self.assertEqual(provenance["snapshot_generator"], self.recipe["generator"])
            self.assertIn("not a signature", provenance["statement"])
        scene = load(self.rebuild_dir, "scene.json")
        self.assertEqual(scene["components"], load(self.out, "scene.json")["components"])
        self.assertEqual(scene["provenance"]["kind"], "snapshot_rebuild")
        preview = load(self.rebuild_dir, "preview/display-0001.json")["provenance"]
        self.assertEqual((preview["kind"], preview["recipe_sha256"]), ("snapshot_rebuild", self.recipe[DIGEST_FIELD]))
        self.assertEqual(load(self.rebuild_dir, "geometry.recipe.json"), self.recipe)
        facts = step_facts(self.rebuild_dir)
        self.assertIn("provenance=snapshot_rebuild;", facts["description"])
        self.assertIn(f"recipe_sha256={self.recipe[DIGEST_FIELD]};", facts["description"])
        self.assertTrue(rebuilt["cad"]["step_header"]["name"].endswith("(snapshot rebuild)"))
        mine, theirs = load(self.out, "cad.verification.json"), load(self.rebuild_dir, "cad.verification.json")
        self.assertTrue(theirs["ok"], theirs["problems"])
        for key in ("groups", "parts", "ports", "envelopes"):
            self.assertEqual(theirs[key], mine[key], key)
        self.assertTrue(self.rebuild_events[0]["message"].startswith("Rebuilding snapshot"))
        self.assertIn("(snapshot rebuild)", [e for e in self.rebuild_events if e["kind"] == "artifact"][-1]["message"])
        # The written-back snapshot is itself independently rebuildable.
        again = rebuild_from_recipe(os.path.join(self.rebuild_dir, "geometry.recipe.json"), new_dir())
        self.assertEqual(again["cad"]["provenance"]["recipe_sha256"], self.recipe[DIGEST_FIELD])

    def test_review_reproductions_are_rejected_before_any_output(self):
        removed = copy.deepcopy(self.recipe)
        removed["units_built"] = [u for u in removed["units_built"] if u["id"] != "ent-level-indicator"]
        self.rejected(removed, "does not match its recorded recipe_sha256")
        moved = copy.deepcopy(self.recipe)
        indicator = unit(moved, "ent-level-indicator")
        indicator["placement"]["position_m"][0] += 0.5
        indicator["matrix_mm"][0][3] += 500.0
        self.rejected(moved, "does not match its recorded recipe_sha256")
        tank_moved = copy.deepcopy(self.recipe)
        tank = unit(tank_moved, "ent-tank")
        tank["placement"]["position_m"][0] += 0.5
        tank["matrix_mm"][0][3] += 500.0
        for port in tank["ports"]:
            port["position_m"][0] += 0.5
        self.rejected(tank_moved, "does not match its recorded recipe_sha256")

    def test_any_unrehashed_edit_is_rejected(self):
        edits = {
            "component part": lambda d: unit(d, "ent-pump")["components"][6]["parts"][1].update(fin_count=20),
            "assumption text": lambda d: d["assumptions"]["rcp-appearance"].update(description="edited"),
            "material colour": lambda d: d["materials"]["pump_paint"].update(color="#ff0000"),
            "tessellation": lambda d: d["tessellation"].update(linear_deflection_mm=0.05),
            "source digest": lambda d: d.update(input_digest="0" * 64),
            "source revision": lambda d: d.update(source_revision=7),
            "project id": lambda d: d.update(project_id="project-other"),
            "generator": lambda d: d["generator"].update(version="9.9.9"),
            "diagnostics": lambda d: d["diagnostics"].append(
                {"code": "x", "severity": "warning", "entity_ids": [], "message": "added"}),
            "mapping ids": lambda d: d["representation_ids"].pop(),
            "unit order": lambda d: d["units_built"].reverse(),
            "usage hint": lambda d: d.update(rebuild="edited"),
        }
        for label, edit in edits.items():
            with self.subTest(label):
                document = copy.deepcopy(self.recipe)
                edit(document)
                self.rejected(document, "does not match its recorded recipe_sha256")

    def test_recomputed_digest_still_requires_this_generators_conventions_and_consistent_units(self):
        def rehashed(edit):
            document = copy.deepcopy(self.recipe)
            edit(document)
            return with_digest(document)

        cases = [
            (lambda d: d["tessellation"].update(linear_deflection_mm=0.05), "tessellation .* differs"),
            (lambda d: d["world"].update(up_axis="Y"), "world .* differs"),
            (lambda d: d["materials"]["pump_paint"].update(color="#ff0000"), "material 'pump_paint'"),
            (lambda d: d["generator"].update(version="9.9.9"), "only replayed by the generator version"),
            (lambda d: d.update(extra=True), "unsupported \\['extra'\\]"),
            (lambda d: unit(d, "ent-tank")["matrix_mm"][0].__setitem__(3, 123.0), "does not equal its recorded"),
            (lambda d: unit(d, "ent-tank")["ports"][0]["position_m"].__setitem__(0, 9.0), "position_m does not"),
            (lambda d: unit(d, "ent-tank")["ports"][0].__setitem__("direction", [0.0, 1.0, 0.0]),
             "direction does not"),
            (lambda d: unit(d, "ent-tank").update(note="x"), "unsupported \\['note'\\]"),
            (lambda d: d["assumptions"].pop("rcp-appearance"), "exactly the assumption IDs"),
            (lambda d: d["source"].update(kind="hand_edit"), "source.kind"),
            (lambda d: d["diagnostics"].append({"code": "geometry_build_failed", "severity": "error",
                                                "entity_ids": [], "message": "x"}), "failed build"),
            (lambda d: d.update(input_digest="not-a-digest"), "input_digest must be"),
        ]
        for edit, pattern in cases:
            with self.subTest(pattern):
                self.rejected(rehashed(edit), pattern)

    def test_rehashed_consistent_edit_never_reuses_the_original_recipe_digest(self):
        # The digest is not a signature: a deliberately re-hashed, consistent edit replays, but it is
        # attributed to its own recipe_sha256, never to the authoritative build's recipe.
        document = copy.deepcopy(self.recipe)
        indicator = unit(document, "ent-level-indicator")
        indicator["placement"]["position_m"][0] += 0.5
        indicator["matrix_mm"][0][3] += 500.0
        document = with_digest(document)
        out = new_dir()
        result = rebuild_from_recipe(write_snapshot(document), out)
        provenance = result["cad"]["provenance"]
        self.assertEqual(provenance["kind"], "snapshot_rebuild")
        self.assertEqual(provenance["recipe_sha256"], document[DIGEST_FIELD])
        self.assertNotEqual(provenance["recipe_sha256"], self.recipe[DIGEST_FIELD])
        self.assertIn(f"recipe_sha256={document[DIGEST_FIELD]};", step_facts(out)["description"])

    def test_legacy_missing_and_malformed_snapshots_get_actionable_errors(self):
        legacy = copy.deepcopy(self.recipe)
        legacy.pop(DIGEST_FIELD)
        legacy["schema"] = "skid.geometry.snapshot-recipe/1"
        self.rejected(legacy, r"legacy schema .*\(python -m skid.geometry build")
        unsigned = copy.deepcopy(self.recipe)
        unsigned.pop(DIGEST_FIELD)
        self.rejected(unsigned, "has no recipe_sha256")
        bad = dict(self.recipe, recipe_sha256=self.recipe[DIGEST_FIELD].upper())
        self.rejected(bad, "64 lowercase hexadecimal")
        self.rejected(dict(self.recipe, schema="something/else"), "not a generated geometry snapshot")
        text = json.dumps(self.recipe)
        self.rejected(None, "repeats the key 'schema'", text='{"schema": 1, ' + text[1:])
        self.rejected(None, "non-finite number NaN", text=text.replace('"source_revision": 0', '"source_revision": NaN'))
        self.rejected(None, "not valid JSON", text=text[:-10])
        self.rejected(None, "must be a JSON object", text="[]")
        with open(DRUM, encoding="utf-8") as handle:
            self.rejected(None, "python -m skid.geometry recipe", text=handle.read())
        if os.path.exists(OLD_EVIDENCE):  # earlier evidence, read only
            with self.assertRaisesRegex(SnapshotError, "legacy schema"):
                rebuild_from_recipe(OLD_EVIDENCE, new_dir())

    def test_cli_rebuild_reports_rejection(self):
        document = copy.deepcopy(self.recipe)
        document["units_built"].pop()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = cli(["rebuild", write_snapshot(document), os.path.join(new_dir(), "out")])
        self.assertEqual(code, 2)
        self.assertIn("snapshot rejected:", stderr.getvalue())
        self.assertIn("does not match its recorded recipe_sha256", stderr.getvalue())


class PendingAndAuthoredSnapshotTest(unittest.TestCase):

    def test_pending_build_snapshot_rebuild_keeps_status_and_every_mapping_key(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            document = json.load(handle)
        document, _ = apply_command(document, {
            "id": "cmd-test-cad-snapshot-1", "expected_revision": 0, "type": "set_parameter",
            "payload": {"entity_id": "ent-pump", "name": "body_height",
                        "quantity": {"value": 0.5, "unit": "m", "assumption_id": "asm-pump-dimensions"}}})
        out = new_dir()
        original = build_geometry(document["current"], out)
        rebuilt = rebuild_from_recipe(os.path.join(out, "geometry.recipe.json"), new_dir())
        for name in ("display", "cad"):
            self.assertEqual(rebuilt[name]["status"], "pending")
            self.assertEqual(rebuilt[name]["representations"], original[name]["representations"])
            self.assertEqual(rebuilt[name]["diagnostics"], original[name]["diagnostics"])
            self.assertEqual(rebuilt[name]["source_revision"], 1)
        self.assertEqual(rebuilt["cad"]["representations"]["conn-discharge-1"], [])

    def test_authored_recipe_snapshot_is_labelled_and_replayable(self):
        out = new_dir()
        result = build_authored(DRUM, out)
        provenance = result["cad"]["provenance"]
        self.assertEqual(provenance["kind"], "authored_recipe")
        snapshot = load(out, "geometry.recipe.json")
        self.assertEqual(provenance["recipe_sha256"], snapshot[DIGEST_FIELD])
        self.assertEqual(snapshot["source"]["kind"], "authored_recipe")
        self.assertNotEqual(snapshot[DIGEST_FIELD], result["cad"]["input_digest"])
        rebuilt = rebuild_from_recipe(os.path.join(out, "geometry.recipe.json"), new_dir())
        self.assertEqual(rebuilt["cad"]["status"], "complete")
        self.assertEqual(rebuilt["cad"]["provenance"]["snapshot_source_kind"], "authored_recipe")


class ContentDigestTest(unittest.TestCase):

    def test_digest_covers_every_field_but_itself_and_ignores_formatting(self):
        base = with_digest({"a": 1, "b": [1.5, {"c": "x"}], "schema": SNAPSHOT_SCHEMA})
        self.assertEqual(content_digest(base), base[DIGEST_FIELD])
        self.assertEqual(content_digest(dict(base, recipe_sha256="0" * 64)), base[DIGEST_FIELD])
        self.assertEqual(content_digest(json.loads(json.dumps(base, indent=3))), base[DIGEST_FIELD])
        for key, value in (("a", 2), ("b", [1.5, {"c": "y"}]), ("schema", "x"), ("new", None)):
            self.assertNotEqual(content_digest(dict(base, **{key: value})), base[DIGEST_FIELD], key)


if __name__ == "__main__":
    unittest.main()
