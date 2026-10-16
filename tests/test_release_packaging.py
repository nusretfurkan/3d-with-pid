"""Packaging contracts: the release exporter, the reference script and the pinned Node dev install.

Standard library only. The exporter cases use small synthetic source trees in
temporary directories (fault injection replaces ``export_release._checkpoint``;
the Git cases use real ``git`` or a failing stand-in on ``PATH``). Workspace
manifest cases run only in the private workspace (``repository/`` present);
the shipped-manifest case runs only in an exported candidate. The reference case runs the real core lane of
``scripts/reference.py`` (CLI and drafting subprocesses) into a temporary
directory and a negative control with a deliberately wrong expectation.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import export_release  # noqa: E402

MANIFEST = {
    "format": "skid-release-manifest/1",
    "templates": {"README.md": "repository/README.md"},
    "trees": ["pkg"],
    "files": ["notes.txt"],
    "optional_files": ["maybe.txt"],
    "exclude_names": ["__pycache__"],
    "exclude_suffixes": [".pyc"],
    "forbidden_roots": ["artifacts", "release", "repository", ".agents"],
    "max_file_bytes": 1000,
}


class ExporterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="skid export ")
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "workspace"
        for rel, text in {"repository/README.md": "public readme\n", "README.md": "PRIVATE readme\n",
                          "notes.txt": "notes\n", "pkg/a.py": "A = 1\n", "pkg/sub/b.txt": "b\n",
                          "pkg/__pycache__/a.cpython-312.pyc": "x", "pkg/c.pyc": "x",
                          "artifacts/private.log": "private\n"}.items():
            path = self.src / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        os.chmod(self.src / "pkg" / "a.py", 0o755)
        self.manifest = dict(MANIFEST)

    def export(self, name="release/github", **changes):
        manifest = dict(self.manifest, **changes)
        path = self.src / "manifest.json"
        path.write_text(json.dumps(manifest))
        return export_release.export(str(self.src), str(path), str(self.src / name))

    def refused(self, fragment, name="release/github", **changes):
        with self.assertRaises(export_release.Refused) as caught:
            self.export(name, **changes)
        self.assertTrue(any(fragment in p for p in caught.exception.problems), caught.exception.problems)
        target = self.src / name
        self.assertTrue(not target.exists() or not any(target.iterdir()), "a refused export writes nothing")
        self.assertEqual([p for p in (self.src / "release").glob(".*staging*")] if (self.src / "release").exists()
                         else [], [])

    def test_exports_exact_listed_contents_with_template_override_and_inventory(self):
        report, report_path = self.export()
        out = self.src / "release" / "github"
        files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        self.assertEqual(files, ["README.md", "notes.txt", "pkg/a.py", "pkg/sub/b.txt"])
        self.assertEqual((out / "README.md").read_text(), "public readme\n")  # the template, not the private file
        self.assertEqual(os.stat(out / "pkg" / "a.py").st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(out / "notes.txt").st_mode & 0o777, 0o644)
        self.assertEqual(report["optional_absent"], ["maybe.txt"])
        self.assertEqual(report["skipped"], {".pyc": 1, "__pycache__": 1})
        self.assertEqual(Path(report_path).parent, out.parent)  # the report lives beside the candidate
        on_disk = json.loads(Path(report_path).read_text())
        self.assertEqual([f["path"] for f in on_disk["files"]], files)

    def test_export_is_deterministic_except_the_timestamp(self):
        first, _ = self.export("release/one")
        second, _ = self.export("release/two")
        self.assertEqual(first["inventory_sha256"], second["inventory_sha256"])
        self.assertEqual(first["files"], second["files"])

    def test_refuses_overwrite_of_a_nonempty_output_or_existing_report(self):
        self.export()
        with self.assertRaises(export_release.Refused):
            self.export()
        (self.src / "release" / "empty").mkdir()
        (self.src / "release" / "empty.export.json").write_text("{}")
        self.refused("exists", name="release/empty")

    def test_refuses_missing_unsafe_forbidden_and_colliding_entries(self):
        self.refused("is missing", files=["notes.txt", "gone.txt"])
        self.refused("not a safe relative path", files=["../outside.txt"])
        self.refused("not a safe relative path", files=["/etc/hosts"])
        self.refused("excluded 'artifacts'", files=["artifacts/private.log"])
        self.refused("excluded 'release'", trees=["pkg", "release"])
        self.refused("map to", files=["notes.txt", "pkg/a.py"])

    def test_refuses_symlinks_large_binary_personal_and_credential_content(self):
        link = self.src / "pkg" / "link.txt"
        link.symlink_to(self.src / "notes.txt")
        self.refused("symlink")
        link.unlink()
        (self.src / "pkg" / "big.txt").write_text("x" * 1001)
        self.refused("limit 1000")
        (self.src / "pkg" / "big.txt").unlink()
        (self.src / "pkg" / "blob.bin").write_bytes(b"\xff\xfe\x00")
        self.refused("not UTF-8")
        (self.src / "pkg" / "blob.bin").unlink()
        (self.src / "pkg" / "home.txt").write_text(f"cli = '{os.path.realpath(os.path.expanduser('~'))}/bin/x'\n")
        self.refused("home.txt:1: matches home-directory")
        (self.src / "pkg" / "home.txt").unlink()
        (self.src / "pkg" / "key.txt").write_text("token = sk-ant-" + "a" * 24 + "\n")
        self.refused("key.txt:1: matches anthropic-key")

    def test_never_recurses_into_its_own_output(self):
        manifest = dict(self.manifest, trees=["pkg"])
        path = self.src / "manifest.json"
        path.write_text(json.dumps(manifest))
        report, _ = export_release.export(str(self.src), str(path), str(self.src / "pkg" / "out"))
        self.assertFalse(any(f["path"].startswith("pkg/out") for f in report["files"]))

    def stages(self):
        """Record the export stages reached; refusals in ``plan`` must happen before any content is read."""
        reached = []
        patcher = mock.patch.object(export_release, "_checkpoint", reached.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reached

    def test_refuses_symlinked_ancestors_and_realpath_escapes_before_reading(self):
        outside = self.src.parent / "outside"
        (outside / "tpl").mkdir(parents=True)
        (outside / "tpl" / "README.md").write_text("OUTSIDE readme\n")
        (outside / "sub").mkdir()
        (outside / "sub" / "x.txt").write_text("outside\n")
        reached = self.stages()
        # The reproduced counterexample: a template reached through a directory symlink pointing outside --source.
        (self.src / "tpl").symlink_to(outside / "tpl", target_is_directory=True)
        self.refused("through the symlink 'tpl'", templates={"README.md": "tpl/README.md"})
        # A listed file through a symlinked directory is refused even when the link stays inside the source.
        (self.src / "alias").symlink_to(self.src / "pkg", target_is_directory=True)
        self.refused("through the symlink 'alias'", files=["notes.txt", "alias/a.py"])
        self.refused("through the symlink 'alias'", optional_files=["alias/a.py"])
        # A tree below a symlinked directory, and a final-component file symlink that escapes the source.
        (self.src / "deep").symlink_to(outside, target_is_directory=True)
        self.refused("through the symlink 'deep'", trees=["pkg", "deep/sub"])
        (self.src / "escape.txt").symlink_to(outside / "sub" / "x.txt")
        self.refused("resolves outside the source workspace", files=["notes.txt", "escape.txt"])
        self.assertEqual(reached, [], "every refusal happened in plan, before any content was read")
        self.assertFalse((self.src / "release").exists())
        # Positive control: a --source given through a symlink is resolved first and exports normally.
        alias_source = self.src.parent / "workspace-alias"
        alias_source.symlink_to(self.src, target_is_directory=True)
        path = self.src / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        report, _ = export_release.export(str(alias_source), str(path), str(self.src / "release" / "ok"))
        self.assertEqual(report["counts"]["files"], 4)

    def test_size_utf8_and_content_are_checked_on_the_bytes_actually_read(self):
        # Plan sees a small benign file; the file changes before it is read. The checks apply to the read bytes.
        for content, fragment in ((b"x" * 1001, "more than 1000 bytes when read"),
                                  (b"\xff\xfe\x00", "notes.txt: not UTF-8"),
                                  (("token = sk-ant-" + "a" * 24 + "\n").encode(), "notes.txt:1: matches anthropic-key")):
            (self.src / "notes.txt").write_text("notes\n")
            with mock.patch.object(export_release, "_checkpoint",
                                   lambda stage, c=content: stage == "planned" and (self.src / "notes.txt").write_bytes(c)):
                self.refused(fragment)
        (self.src / "notes.txt").write_text("notes\n")
        outside = self.src.parent / "outside.txt"
        outside.write_text("outside\n")

        def swap(stage):
            if stage == "planned":
                (self.src / "notes.txt").unlink()
                (self.src / "notes.txt").symlink_to(outside)

        with mock.patch.object(export_release, "_checkpoint", swap):
            self.refused("'notes.txt'")

    def test_source_changed_after_scan_is_never_published(self):
        secret = "token = sk-ant-" + "b" * 24 + "\n"
        for stage in ("scanned", "staged"):
            (self.src / "pkg" / "a.py").write_text("A = 1\n")
            with mock.patch.object(export_release, "_checkpoint",
                                   lambda s, at=stage: s == at and (self.src / "pkg" / "a.py").write_text(secret)):
                self.refused("'pkg/a.py': the source changed during the export")
            self.assertFalse((self.src / "release" / "github").exists())
            written = [p for p in self.src.parent.rglob("*") if p.is_file() and "release" in p.parts]
            self.assertFalse(any(secret in p.read_text(errors="replace") for p in written))

    def test_staged_copy_that_differs_from_the_scanned_bytes_is_refused(self):
        def tamper(stage):
            if stage == "staged":
                (staging,) = (self.src / "release").glob(".github.staging-*")
                (staging / "notes.txt").write_text("tampered\n")

        with mock.patch.object(export_release, "_checkpoint", tamper):
            self.refused("'notes.txt': the staged copy differs from the scanned bytes")
        self.assertFalse((self.src / "release" / "github").exists())

    def test_missing_template_is_refused_never_replaced_by_the_private_file(self):
        (self.src / "repository" / "README.md").unlink()
        self.refused("template 'repository/README.md' for 'README.md' is missing")

    def test_failure_after_publication_is_partial_not_nothing_exported(self):
        def fail(stage):
            if stage == "published":
                raise OSError("disk full (injected)")

        with mock.patch.object(export_release, "_checkpoint", fail):
            with self.assertRaises(export_release.Partial) as caught:
                self.export()
            self.assertIn("disk full", caught.exception.reason)
            self.assertTrue((self.src / "release" / "github" / "notes.txt").is_file())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = export_release.main(["--output", str(self.src / "release" / "other"), "--manifest",
                                            str(self.src / "manifest.json"), "--source", str(self.src)])
        self.assertEqual(code, 3)
        self.assertIn("PARTIAL: the candidate WAS exported", err.getvalue())
        self.assertNotIn("nothing was exported", err.getvalue())

    def test_check_mode_writes_nothing(self):
        path = self.src / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        before = sorted(str(p) for p in self.src.rglob("*"))
        summary = export_release.check(str(self.src), str(path))
        self.assertEqual(summary["files"], 4)
        self.assertEqual(before, sorted(str(p) for p in self.src.rglob("*")))
        (self.src / "notes.txt").unlink()
        with self.assertRaises(export_release.Refused):
            export_release.check(str(self.src), str(path))


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class GitInitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="skid export git ")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = self.tmp / "workspace"
        (self.src / "pkg").mkdir(parents=True)
        (self.src / "pkg" / "a.py").write_text("A = 1\n")
        (self.src / "notes.txt").write_text("notes\n")
        self.manifest_path = self.tmp / "manifest.json"
        self.write_manifest(files=["notes.txt"])

    def write_manifest(self, **changes):
        manifest = dict(MANIFEST, templates={}, trees=["pkg"], optional_files=[])
        manifest.update(changes)
        self.manifest_path.write_text(json.dumps(manifest))

    def run_cli(self, out, env=None):
        argv = [sys.executable, "-I", str(ROOT / "scripts" / "export_release.py"), "--output", str(out),
                "--manifest", str(self.manifest_path), "--source", str(self.src), "--git-init"]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)

    def git(self, out, *args):
        return subprocess.run(["git", "-C", str(out), *args], capture_output=True, text=True)

    def test_git_init_stages_exactly_the_inventory_without_commit_or_remote(self):
        out = self.tmp / "release" / "ok"
        done = self.run_cli(out)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads((self.tmp / "release" / "ok.export.json").read_text())
        self.assertEqual(report["git"]["initialized"], True)
        self.assertEqual(report["git"]["staged_files"], 2)
        self.assertNotEqual(self.git(out, "rev-parse", "--verify", "-q", "HEAD").returncode, 0)  # no commit
        self.assertEqual(self.git(out, "remote").stdout.strip(), "")

    def test_failed_git_step_exits_3_and_reports_the_exported_candidate(self):
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "git").write_text("#!/bin/sh\necho 'fatal: injected failure' >&2\nexit 7\n")
        os.chmod(fake / "git", 0o755)
        out = self.tmp / "release" / "fail"
        done = self.run_cli(out, env=dict(os.environ, PATH=f"{fake}{os.pathsep}{os.environ.get('PATH', '')}"))
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("PARTIAL: the candidate WAS exported", done.stderr)
        self.assertNotIn("nothing was exported", done.stderr)
        self.assertTrue((out / "notes.txt").is_file())
        git = json.loads((self.tmp / "release" / "fail.export.json").read_text())["git"]
        self.assertEqual((git["initialized"], git["steps"][0]["exit"]), (False, 7))
        empty = self.tmp / "emptybin"
        empty.mkdir()
        done = self.run_cli(self.tmp / "release" / "nogit", env=dict(os.environ, PATH=str(empty)))
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("git is not on PATH", done.stderr)

    def test_exported_file_hidden_by_ignore_rules_is_a_partial_git_setup(self):
        (self.src / "candidate.gitignore").write_text("notes.txt\n")
        self.write_manifest(files=["notes.txt"], templates={".gitignore": "candidate.gitignore"})
        out = self.tmp / "release" / "ignored"
        done = self.run_cli(out)
        self.assertEqual(done.returncode, 3, done.stderr)
        git = json.loads((self.tmp / "release" / "ignored.export.json").read_text())["git"]
        self.assertEqual((git["initialized"], git["unstaged"]), (False, ["notes.txt"]))


class SelfReexportTests(unittest.TestCase):
    """The workspace manifest ships a portable overlay manifest, so the candidate can export itself."""

    def test_candidate_reexports_itself_without_the_private_templates(self):
        with tempfile.TemporaryDirectory(prefix="skid reexport ") as tmp:
            src = Path(tmp) / "workspace"
            (src / "scripts").mkdir(parents=True)
            (src / "repository" / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "export_release.py", src / "scripts" / "export_release.py")
            (src / "README.md").write_text("PRIVATE readme\n")
            (src / "repository" / "README.md").write_text("public readme\n")
            (src / "notes.txt").write_text("notes\n")
            common = dict(MANIFEST, trees=["scripts"], files=["notes.txt"], optional_files=[], max_file_bytes=1 << 20)
            workspace = dict(common, templates={"README.md": "repository/README.md",
                                                "scripts/release_manifest.json": "repository/scripts/release_manifest.json"})
            public = dict(common, templates={}, files=["README.md", "notes.txt"])
            (src / "scripts" / "release_manifest.json").write_text(json.dumps(workspace))
            (src / "repository" / "scripts" / "release_manifest.json").write_text(json.dumps(public))
            first, _ = export_release.export(str(src), str(src / "scripts" / "release_manifest.json"),
                                             str(Path(tmp) / "one"))
            candidate = Path(tmp) / "one"
            self.assertEqual(first["template_overrides"], ["scripts/release_manifest.json"])
            self.assertEqual(json.loads((candidate / "scripts" / "release_manifest.json").read_text()), public)
            self.assertEqual((candidate / "README.md").read_text(), "public readme\n")
            self.assertFalse((candidate / "repository").exists())
            done = subprocess.run([sys.executable, "-I", str(candidate / "scripts" / "export_release.py"),
                                   "--output", str(Path(tmp) / "two")], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            second = json.loads((Path(tmp) / "two.export.json").read_text())
            self.assertEqual(second["files"], first["files"])
            self.assertEqual(second["inventory_sha256"], first["inventory_sha256"])


TEMPLATE_DOCS = ("PUMP_SKID", "CONTRACTS", "WORKFLOW_CONTRACTS", "EXTENSION_CONTRACTS", "EQUIPMENT", "DRAFTING",
                 "PIPE_SEGMENTS", "PROCESS_NARRATIVE", "PLANNING", "ASK", "EMPTY_START", "EQUIPMENT_RESEARCH",
                 "CREATION_AND_RESEARCH_UI", "EMPTY_START_AND_RESEARCH_CONTRACT", "PROJECT_NAMING", "INTERACTIVE",
                 "UI", "CAD_SPIKE", "ASSISTANT_3D_CONTRACTS")
WORKSPACE = (ROOT / "repository").is_dir()  # the private source workspace; a public candidate has no repository/
PUBLIC_MANIFEST = ROOT / "repository" / "scripts" / "release_manifest.json"


class WorkspaceManifestTests(unittest.TestCase):
    def check_common(self, manifest):
        for root in ("artifacts", "release", "repository", ".agents", ".claude", ".venv-cad", "skills-lock.json"):
            self.assertIn(root, manifest["forbidden_roots"])
        self.assertIn("constraints-cad.txt", manifest["files"])
        self.assertIn("docs/FIXTURE_PROVENANCE.md", manifest["files"])
        self.assertNotIn("scripts/constraints-cad.txt", manifest["files"])

    @unittest.skipUnless(WORKSPACE, "public candidate: the workspace manifest and its templates are not shipped")
    def test_workspace_manifest_excludes_private_material(self):
        manifest = export_release.load_manifest(export_release.MANIFEST)
        self.check_common(manifest)
        listed = set(manifest["files"]) | set(manifest["optional_files"]) | set(manifest["trees"])
        for private in ("README.md", "AGENTS.md", "HANDOVER.md", ".gitignore", "skills-lock.json"):
            self.assertNotIn(private, listed)  # only the portable templates provide these
        expected = {name: f"repository/{name}" for name in ("README.md", "AGENTS.md", "HANDOVER.md", ".gitignore")}
        expected["scripts/release_manifest.json"] = "repository/scripts/release_manifest.json"
        expected.update({f"docs/{name}.md": f"repository/docs/{name}.md" for name in TEMPLATE_DOCS})
        self.assertEqual(manifest["templates"], expected)
        self.assertTrue(all(v.startswith("repository/") for v in manifest["templates"].values()))
        self.assertEqual(listed & set(manifest["templates"]), set())  # no raw historical doc beside its template

    @unittest.skipUnless(WORKSPACE, "public candidate: the overlay is already scripts/release_manifest.json")
    def test_public_overlay_manifest_equals_the_workspace_manifest_with_templates_folded_in(self):
        workspace = export_release.load_manifest(export_release.MANIFEST)
        public = export_release.load_manifest(PUBLIC_MANIFEST)
        self.assertEqual(public["templates"], {})
        folded = set(workspace["files"]) | set(workspace["templates"]) - {"scripts/release_manifest.json"}
        self.assertEqual(sorted(public["files"]), sorted(folded))
        for key in ("trees", "optional_files", "exclude_names", "exclude_suffixes", "forbidden_roots",
                    "max_file_bytes"):
            self.assertEqual(public[key], workspace[key], key)
        self.assertNotIn("repository/", json.dumps({k: v for k, v in public.items() if k != "_comment"}))

    @unittest.skipUnless(WORKSPACE, "public candidate: checked by the shipped-manifest test instead")
    def test_workspace_templates_and_required_files_exist(self):
        manifest = export_release.load_manifest(export_release.MANIFEST)
        missing = [p for p in list(manifest["templates"].values()) + manifest["files"] if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], "required template/file missing: the export refuses until it exists")

    @unittest.skipIf(WORKSPACE, "private workspace: the shipped manifest is repository/scripts/release_manifest.json")
    def test_shipped_manifest_is_portable_and_complete(self):
        manifest = export_release.load_manifest(export_release.MANIFEST)
        self.check_common(manifest)
        self.assertEqual(manifest["templates"], {})
        self.assertEqual([p for p in manifest["files"] if not (ROOT / p).is_file()], [])


class ToolingTargetTests(unittest.TestCase):
    def test_one_cad_constraint_source_applied_by_requirements(self):
        self.assertFalse((ROOT / "scripts" / "constraints-cad.txt").exists())
        requirements = (ROOT / "requirements-cad.txt").read_text().splitlines()
        self.assertIn("-c constraints-cad.txt", requirements)
        pins = [line for line in (ROOT / "constraints-cad.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertEqual(len(pins), 39)
        self.assertTrue(all("==" in line for line in pins))
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertNotIn("scripts/constraints-cad.txt", ci)
        self.assertIn("pip install -r requirements-cad.txt\n", ci)

    def test_node_22_is_the_stated_and_ci_target(self):
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        self.assertEqual(package["engines"], {"node": ">=22"})
        self.assertEqual(lock["packages"][""]["engines"], package["engines"])
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertEqual(ci.count('node-version: "22"'), 2)


class NodePinTests(unittest.TestCase):
    def test_playwright_is_pinned_exactly_in_package_and_lock(self):
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        self.assertEqual(package["devDependencies"], {"playwright": "1.58.2"})
        self.assertNotIn("dependencies", package)  # the web UI has no npm runtime dependency (Three.js is vendored)
        self.assertEqual(lock["packages"][""]["devDependencies"], package["devDependencies"])
        for name in ("playwright", "playwright-core"):
            entry = lock["packages"][f"node_modules/{name}"]
            self.assertEqual(entry["version"], "1.58.2")
            self.assertTrue(entry["integrity"].startswith("sha512-"))


class ReferenceScriptTests(unittest.TestCase):
    def run_reference(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "reference.py"), *args], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=600)

    def test_core_lane_passes_and_records_evidence(self):
        with tempfile.TemporaryDirectory(prefix="skid reference ") as tmp:
            out = Path(tmp) / "run"
            done = self.run_reference("--output", str(out))
            self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-2000:])
            evidence = json.loads((out / "evidence.json").read_text())
            self.assertEqual(evidence["result"], "passed")
            self.assertEqual(evidence["lanes"], ["core"])
            self.assertEqual([c for c in evidence["checks"] if not c["ok"]], [])
            self.assertIn("3D display and CAD/STEP generation", evidence["not_exercised"])
            exits = {c["label"]: (c["exit"], c["expected_exit"]) for c in evidence["commands"]}
            self.assertEqual(exits["stale rename refused"], (4, 4))
            self.assertEqual(exits["drafting drafting-r3"], (2, 2))  # pending is exit 2, never reported complete
            self.assertFalse(any(str(ROOT) in json.dumps(c["argv"]) for c in evidence["commands"]))

    def test_wrong_expectation_fails_and_occupied_output_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="skid reference ") as tmp:
            expected = json.loads((ROOT / "scripts" / "reference_expected.json").read_text())
            expected["legend_keys"] += 1
            wrong = Path(tmp) / "wrong.json"
            wrong.write_text(json.dumps(expected))
            out = Path(tmp) / "run"
            done = self.run_reference("--output", str(out), "--expected", str(wrong))
            self.assertEqual(done.returncode, 1, done.stdout[-2000:])
            evidence = json.loads((out / "evidence.json").read_text())
            self.assertEqual(evidence["result"], "failed")
            failed = {c["name"] for c in evidence["checks"] if not c["ok"]}
            self.assertIn("drafting r0: legend key count", failed)
            done = self.run_reference("--output", str(out))
            self.assertEqual(done.returncode, 2)
            self.assertIn("not an empty directory", done.stderr)


if __name__ == "__main__":
    unittest.main()
