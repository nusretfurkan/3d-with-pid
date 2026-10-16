"""Integrity checks shared by worker and service: paths, active content, manifests, outputs, seals.

Outputs here are written in-process by the LABELED FAKE suite (``tests.test_interactive_fakes``).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from skid.core import canonical_digest, load_document
from skid.interactive import integrity
from tests.test_interactive_fakes import FIXTURE, FakeSuite


def _quiet(event):
    pass


def _never():
    return False


class PathTests(unittest.TestCase):
    def test_safe_relpath(self):
        for good in ("scene.json", "preview/display-0001.json", "a/b/c.svg", "A_b-1.2.json"):
            self.assertEqual(integrity.safe_relpath(good), good)
        for bad in ("", "/etc/passwd", "../x", "a/../b", "./a", "a//b", ".hidden", "a/.b", "a\\b", "a\x00b", "a b",
                    "%2e%2e/x", None, 3, "x" * 600, "/".join("a" * 9)):
            self.assertIsNone(integrity.safe_relpath(bad), bad)

    def test_resolve_inside_refuses_symlink_escape_and_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "root")
            (root / "sub").mkdir(parents=True)
            (root / "sub" / "ok.json").write_text("{}")
            outside = Path(tmp, "secret.json")
            outside.write_text("{}")
            os.symlink(outside, root / "link.json")
            os.symlink(root / "sub" / "ok.json", root / "inner.json")
            self.assertIsNotNone(integrity.resolve_inside(root, "sub/ok.json"))
            self.assertIsNotNone(integrity.resolve_inside(root, "inner.json"))
            self.assertIsNone(integrity.resolve_inside(root, "link.json"))
            self.assertIsNone(integrity.resolve_inside(root, "sub"))
            self.assertIsNone(integrity.resolve_inside(root, "missing.json"))


class ActiveContentTests(unittest.TestCase):
    def test_svg_sanitization(self):
        ok = b'<svg xmlns="http://www.w3.org/2000/svg"><a href="#x"><text>P&amp;ID</text></a>' \
             b'<image href="data:image/png;base64,AAAA"/><style>.a{fill:red}</style></svg>'
        self.assertEqual(integrity.svg_problems(ok), [])
        bad = {
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>': "script",
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect onclick="x()"/></svg>': "event handler",
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<a xlink:href="javascript:alert(1)"/></svg>': "active URL",
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>': "external",
            b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>': "foreignobject",
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://x)</style></svg>': "style",
            b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="href" to="javascript:x"/></svg>': "animates",
            b'<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"/>': "doctype",
            b'<svg xmlns="http://www.w3.org/2000/svg"><g>': "well-formed",
            b'<html/>': "root element",
        }
        for data, fragment in bad.items():
            problems = integrity.svg_problems(data)
            self.assertTrue(problems, data)
            self.assertIn(fragment.lower(), " ".join(problems).lower(), data)

    def test_html_sanitization(self):
        ok = b'<!doctype html><html><head><meta charset="utf-8"><style>p{color:#333}</style></head><body>' \
             b'<a href="https://dexpi.org">link</a><a href="#P-101">P-101</a><img src="data:image/png;base64,AA">' \
             b'</body></html>'
        self.assertEqual(integrity.html_problems(ok), [])
        for data in (b"<script>x</script>", b'<img src=x onerror="x()">', b'<a href="javascript:x">',
                     b'<img src="https://example.com/t.png">', b'<iframe src="/x"></iframe>',
                     b'<meta http-equiv="refresh" content="0;url=/x">', b'<link rel="stylesheet" href="/x.css">',
                     b'<div style="background:url(http://x)">', b'<form action="/api/command"></form>'):
            self.assertTrue(integrity.html_problems(data), data)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="skid integrity ")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        os.mkdir(os.path.join(self.dir, "preview"))
        self.project = load_document(str(FIXTURE))["current"]
        self.digest = canonical_digest(self.project)

    def generate(self, **options):
        suite = FakeSuite(options)
        returned = {}
        returned.update(suite.geometry(copy.deepcopy(self.project), self.dir, _quiet, _never))
        returned.update(suite.drafting(copy.deepcopy(self.project), self.dir, _quiet, _never, None))
        return returned

    def verify(self, returned=None):
        return integrity.verify_outputs(self.dir, self.project, self.digest, returned)

    def codes(self, results, artifact):
        return {d["code"] for d in results[artifact]["diagnostics"]}

    def test_clean_fake_outputs_verify_complete(self):
        returned = self.generate()
        results = self.verify(returned)
        self.assertEqual({a: r["status"] for a, r in results.items()}, dict.fromkeys(integrity.REQUIRED_ARTIFACTS,
                                                                                     "complete"))
        self.assertEqual(integrity.overall_status(self.project, results), "complete")

    def test_each_manifest_fault_fails_only_its_artifact(self):
        cases = [
            ({"display": "wrong_digest", "cad": "wrong_version", "pid": "missing_mapping", "legend": "svg_script",
              "description": "html_script"},
             {"display": "manifest_mismatch", "cad": "unsupported_adapter_version", "pid": "mapping_incomplete",
              "legend": "artifact_content", "description": "artifact_content"}),
            ({"display": "scene_identity", "cad": "unsafe_file", "pid": "unknown_mapping",
              "legend": "warning_complete", "description": "error_diagnostic"},
             {"display": "artifact_content", "cad": "unsafe_path", "pid": "mapping_unknown_id",
              "legend": "manifest_contradiction", "description": "adapter_reported_error"}),
            ({"display": "status_failed", "cad": "missing_file", "pid": "wrong_revision"},
             {"display": "adapter_failed", "cad": "artifact_missing", "pid": "manifest_mismatch"}),
        ]
        for fail, expected in cases:
            with self.subTest(fail=fail):
                for name in os.listdir(self.dir):
                    if name != "preview":
                        os.unlink(os.path.join(self.dir, name))
                results = self.verify(self.generate(fail=fail))
                for artifact in integrity.REQUIRED_ARTIFACTS:
                    if artifact in expected:
                        self.assertEqual(results[artifact]["status"], "failed", artifact)
                        self.assertIn(expected[artifact], self.codes(results, artifact), artifact)
                    else:
                        self.assertEqual(results[artifact]["status"], "complete", artifact)
                self.assertEqual(integrity.overall_status(self.project, results), "failed")

    def test_returned_manifest_must_equal_its_file(self):
        results = self.verify(self.generate(fail={"description": "manifest_mismatch"}))
        self.assertEqual(results["description"]["status"], "failed")
        self.assertIn("manifest_mismatch", self.codes(results, "description"))

    def test_group_files_are_required_across_a_generators_manifests(self):
        returned = self.generate()
        manifest_path = os.path.join(self.dir, "cad.manifest.json")
        manifest = json.loads(Path(manifest_path).read_text())
        manifest["files"] = ["model.step"]
        Path(manifest_path).write_text(json.dumps(manifest))
        results = self.verify()
        self.assertEqual(results["cad"]["status"], "failed")
        self.assertIn("geometry.recipe.json", " ".join(d["message"] for d in results["cad"]["diagnostics"]))
        del returned

    def test_pending_project_cannot_be_complete(self):
        self.generate()
        moved = copy.deepcopy(self.project)
        moved["physical"]["routes"]["conn-suction-1"]["status"] = "needs_reroute"
        moved["physical"]["routes"]["conn-suction-1"]["reason"] = "test"
        statuses = {a: {"status": "complete", "diagnostics": []} for a in integrity.REQUIRED_ARTIFACTS}
        self.assertEqual(integrity.overall_status(moved, statuses), "pending")
        statuses["cad"]["status"] = "cancelled"
        self.assertEqual(integrity.overall_status(moved, statuses), "cancelled")
        statuses["pid"]["status"] = "failed"
        self.assertEqual(integrity.overall_status(moved, statuses), "failed")

    def test_check_manifest_rejects_contradictions(self):
        returned = self.generate()
        manifest = copy.deepcopy(returned["pid"])
        manifest["diagnostics"] = [{"code": "x", "severity": "info", "entity_ids": [], "message": "m"}]
        problems = integrity.check_manifest(manifest, "pid", self.project, self.digest, self.dir)
        self.assertIn("diagnostics_invalid", {d["code"] for d in problems})
        manifest = copy.deepcopy(returned["pid"])
        manifest["source_revision"] = True  # bool is not an int revision
        problems = integrity.check_manifest(manifest, "pid", self.project, self.digest, self.dir)
        self.assertIn("manifest_mismatch", {d["code"] for d in problems})
        manifest = copy.deepcopy(returned["pid"])
        manifest["adapter"] = "legend"
        problems = integrity.check_manifest(manifest, "pid", self.project, self.digest, self.dir)
        self.assertIn("manifest_mismatch", {d["code"] for d in problems})
        self.assertEqual(integrity.check_manifest(None, "pid", self.project, self.digest, self.dir)[0]["code"],
                         "manifest_missing")


class SealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="skid seal ")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.build_id = "b20261014T000000-r0-0123abcd"
        self.dir = os.path.join(self.root, "builds", self.build_id)
        os.makedirs(os.path.join(self.dir, "preview"))
        document = load_document(str(FIXTURE))
        self.project = document["current"]
        digest = canonical_digest(self.project)
        Path(self.dir, "snapshot.json").write_bytes(integrity.json_bytes(document))
        suite = FakeSuite({})
        suite.geometry(copy.deepcopy(self.project), self.dir, _quiet, _never)
        suite.drafting(copy.deepcopy(self.project), self.dir, _quiet, _never, None)
        seal = {"format": 2, "build_id": self.build_id, "project_id": self.project["id"],
                "source_revision": self.project["revision"], "input_digest": digest, "status": "complete",
                "files": integrity.hash_tree(self.dir)}
        Path(self.dir, "build.json").write_bytes(integrity.json_bytes(seal))
        self.pointer = {"format": 2, "build_id": self.build_id, "project_id": self.project["id"],
                        "source_revision": 0, "input_digest": digest, "status": "complete",
                        "build_sha256": integrity.sha256_file(os.path.join(self.dir, "build.json"))}

    def test_sealed_build_verifies_and_detects_tampering(self):
        self.assertEqual(integrity.verify_pointer(self.root, self.pointer), [])
        Path(self.dir, "events.jsonl").write_text("{}\n")  # unsealed logs may change
        self.assertEqual(integrity.verify_pointer(self.root, self.pointer), [])
        scene = Path(self.dir, "scene.json")
        original = scene.read_bytes()
        scene.write_bytes(original.replace(b"FAKE", b"EDIT", 1) if b"FAKE" in original else original + b" ")
        self.assertIn("build_file_changed", {d["code"] for d in integrity.verify_pointer(self.root, self.pointer)})
        scene.write_bytes(original)
        Path(self.dir, "extra.json").write_text("{}")
        self.assertIn("build_file_unexpected", {d["code"] for d in integrity.verify_pointer(self.root, self.pointer)})
        os.unlink(os.path.join(self.dir, "extra.json"))
        os.unlink(os.path.join(self.dir, "legend.svg"))
        self.assertIn("build_file_missing", {d["code"] for d in integrity.verify_pointer(self.root, self.pointer)})

    def test_pointer_identity_and_seal_digest(self):
        for key, value in (("source_revision", 1), ("build_sha256", "0" * 64), ("format", 1)):
            with self.subTest(key=key):
                pointer = dict(self.pointer, **{key: value})
                self.assertEqual(integrity.verify_pointer(self.root, pointer)[0]["code"], "pointer_mismatch")
        self.assertEqual(integrity.verify_pointer(self.root, dict(self.pointer, build_id="../x"))[0]["code"],
                         "build_id_invalid")

    def test_status_must_follow_from_outputs(self):
        manifest = Path(self.dir, "pid.manifest.json")
        data = json.loads(manifest.read_text())
        data["status"] = "pending"
        data["diagnostics"] = [{"code": "x", "severity": "warning", "entity_ids": [], "message": "open"}]
        manifest.write_text(json.dumps(data))
        seal = json.loads(Path(self.dir, "build.json").read_text())
        seal["files"] = integrity.hash_tree(self.dir)
        Path(self.dir, "build.json").write_bytes(integrity.json_bytes(seal))
        pointer = dict(self.pointer, build_sha256=integrity.sha256_file(os.path.join(self.dir, "build.json")))
        self.assertIn("seal_mismatch", {d["code"] for d in integrity.verify_pointer(self.root, pointer)})


if __name__ == "__main__":
    unittest.main()
