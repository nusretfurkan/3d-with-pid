"""Packaging contracts: the release exporter, the reference script and the pinned Node dev install.

Standard library only. The exporter cases use small synthetic source trees in
temporary directories (fault injection replaces ``export_release._checkpoint``,
or makes listing one real directory raise ``PermissionError``; the Git cases
use real ``git`` or a failing stand-in on ``PATH``; the Blender-lane case uses
a labelled fake Blender, never a real one). Workspace
manifest cases run only in the private workspace (``repository/`` present);
the shipped-manifest case runs only in an exported candidate. The reference case runs the real core lane of
``scripts/reference.py`` (CLI and drafting subprocesses) into a temporary
directory and a negative control with a deliberately wrong expectation.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
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

    def test_unlistable_tree_directory_is_refused_not_silently_omitted(self):
        # Fault injection: listing the real directory pkg/sub raises PermissionError, as a mode-000 directory does
        # for a non-root user. Every other listing, and the tree on disk, are real and unchanged.
        real_scandir = os.scandir
        blocked = os.path.realpath(self.src / "pkg" / "sub")

        def scandir(path="."):
            if os.path.realpath(path) == blocked:
                raise PermissionError(13, "Permission denied (injected)", os.fspath(path))
            return real_scandir(path)

        reached = self.stages()
        with mock.patch.object(os, "scandir", scandir):
            self.refused("tree directory 'pkg/sub' cannot be listed")
            with self.assertRaises(export_release.Refused):
                export_release.check(str(self.src), str(self.src / "manifest.json"))
        self.assertEqual(reached, [], "refused in plan, before any content was read")
        report, _ = self.export()  # positive control: the same tree, listable, exports pkg/sub/b.txt
        self.assertIn("pkg/sub/b.txt", [f["path"] for f in report["files"]])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "a superuser can list a mode-000 directory")
    def test_real_mode_000_tree_directory_is_refused(self):
        sub = self.src / "pkg" / "sub"
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o755)
        if os.access(sub, os.R_OK):
            self.skipTest("this file system still allows listing a mode-000 directory")
        self.refused("tree directory 'pkg/sub' cannot be listed")

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


def png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def tiny_png(width=3, height=2, before_idat=(), after_idat=(), rows=None, colour=2, level=9):
    """A real, complete RGB PNG built with the standard library (only IHDR/IDAT/IEND unless chunks are added)."""
    if rows is None:
        rows = b"".join(b"\x00" + bytes((40 * y % 256, 120, 200)) * width for y in range(height))
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
    return (export_release.PNG_SIGNATURE + ihdr + b"".join(before_idat) + png_chunk(b"IDAT", zlib.compress(rows, level)) + b"".join(after_idat)
            + png_chunk(b"IEND", b""))


class ReviewedImageTests(unittest.TestCase):
    """Individually reviewed PNGs: the only binary exception, bound to exact bytes, path and a provenance text."""

    setUp_text = ExporterTests.setUp
    export = ExporterTests.export
    refused = ExporterTests.refused
    stages = ExporterTests.stages
    IMAGE = "docs/media/screen.png"

    def setUp(self):
        self.setUp_text()
        self.image = tiny_png()
        (self.src / "docs" / "media").mkdir(parents=True)
        (self.src / self.IMAGE).write_bytes(self.image)
        (self.src / "docs" / "MEDIA.md").write_text("screen.png: reviewed capture of the synthetic example\n")
        self.manifest["files"] = ["notes.txt", "docs/MEDIA.md", self.IMAGE]
        self.manifest["reviewed_images"] = {self.IMAGE: self.approval()}

    def approval(self, data=None, provenance="docs/MEDIA.md"):
        return {"sha256": hashlib.sha256(self.image if data is None else data).hexdigest(), "provenance": provenance}

    def approve(self, data):
        """Approve ``data`` exactly, so a refusal can only come from its structure, never from the digest."""
        (self.src / self.IMAGE).write_bytes(data)
        return {self.IMAGE: self.approval(data)}

    def test_exact_reviewed_png_is_exported_checked_and_reexported(self):
        report, _ = self.export()
        out = self.src / "release" / "github"
        self.assertEqual((out / self.IMAGE).read_bytes(), self.image)
        (entry,) = [f for f in report["files"] if f["path"] == self.IMAGE]
        self.assertEqual(entry, {"path": self.IMAGE, "bytes": len(self.image), "mode": "644",
                                 "sha256": hashlib.sha256(self.image).hexdigest(), "type": "image/png",
                                 "width": 3, "height": 2, "provenance": "docs/MEDIA.md"})
        self.assertNotIn("type", [f for f in report["files"] if f["path"] == "notes.txt"][0])  # text entries unchanged
        summary = export_release.check(str(self.src), str(self.src / "manifest.json"))
        self.assertEqual((summary["files"], summary["reviewed_images"]), (6, [self.IMAGE]))
        text_only = dict(self.manifest, files=["notes.txt"])
        del text_only["reviewed_images"]
        (self.src / "text.json").write_text(json.dumps(text_only))
        self.assertNotIn("reviewed_images", export_release.check(str(self.src), str(self.src / "text.json")))
        # The candidate carries its manifest and exporter and re-exports the image byte-identically.
        (self.src / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts" / "export_release.py", self.src / "scripts" / "export_release.py")
        listed = ["README.md", "notes.txt", "docs/MEDIA.md", self.IMAGE]
        (self.src / "scripts" / "release_manifest.json").write_text(json.dumps(dict(
            self.manifest, templates={}, trees=["scripts"], files=listed, optional_files=[], max_file_bytes=1 << 20)))
        first, _ = export_release.export(str(self.src), str(self.src / "scripts" / "release_manifest.json"),
                                         str(self.src.parent / "one"))
        candidate = self.src.parent / "one"
        script = str(candidate / "scripts" / "export_release.py")
        done = subprocess.run([sys.executable, "-I", script, "--check"], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("reviewed PNG images: 1", done.stdout)
        self.assertIn("pixels are not inspected", done.stdout)
        done = subprocess.run([sys.executable, "-I", script, "--output", str(self.src.parent / "two")],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        second = json.loads((self.src.parent / "two.export.json").read_text())
        self.assertEqual(second["files"], first["files"])
        self.assertEqual(second["inventory_sha256"], first["inventory_sha256"])
        self.assertEqual((self.src.parent / "two" / self.IMAGE).read_bytes(), self.image)

    def test_unapproved_png_is_still_refused_as_binary(self):
        self.refused(f"{self.IMAGE}: not UTF-8", reviewed_images={})
        manifest = dict(self.manifest)
        del manifest["reviewed_images"]
        self.manifest = manifest
        self.refused(f"{self.IMAGE}: not UTF-8")
        # Another PNG beside an approved one gains nothing from the path suffix or the other approval.
        self.manifest = dict(manifest, reviewed_images={self.IMAGE: self.approval()})
        (self.src / "pkg" / "shot.png").write_bytes(self.image)
        self.refused("pkg/shot.png: not UTF-8")

    def test_wrong_digest_and_bytes_changed_after_planning_are_refused(self):
        other = tiny_png(width=4)
        self.refused("differs from the reviewed digest", reviewed_images={self.IMAGE: self.approval(other)})
        # The digest is checked on the bytes actually read: a valid PNG swapped in after planning is refused.
        with mock.patch.object(export_release, "_checkpoint",
                               lambda stage: stage == "planned" and (self.src / self.IMAGE).write_bytes(other)):
            self.refused(f"{self.IMAGE}: SHA-256 of the bytes read differs")
        (self.src / self.IMAGE).write_bytes(self.image)
        with mock.patch.object(export_release, "_checkpoint",
                               lambda stage: stage == "scanned" and (self.src / self.IMAGE).write_bytes(other)):
            self.refused(f"'{self.IMAGE}': the source changed during the export")

    def test_unsafe_or_misplaced_image_paths_are_refused_before_reading(self):
        reached = self.stages()
        for dest, fragment in (("../screen.png", "not a safe relative path"),
                               ("/docs/media/screen.png", "not a safe relative path"),
                               ("docs/media/../screen.png", "not a safe relative path"),
                               ("artifacts/media/screen.png", "excluded 'artifacts'"),
                               ("docs/screen.png", "must be a .png file under 'docs/media/'"),
                               ("docs/mediax/screen.png", "must be a .png file under 'docs/media/'"),
                               ("docs/media/screen.PNG", "must be a .png file under 'docs/media/'"),
                               ("docs/media/screen.jpg", "must be a .png file under 'docs/media/'"),
                               ("docs/media/.png", "must be a .png file under 'docs/media/'")):
            with self.subTest(dest):
                self.refused(fragment, reviewed_images={dest: self.approval()})
        self.assertEqual(reached, [], "every path refusal happened before any content was read")

    def test_missing_unexported_unsafe_or_binary_provenance_is_refused(self):
        self.refused("provenance 'docs/MEDIA.md' is not an exported file", files=["notes.txt", self.IMAGE])
        self.refused("provenance 'docs/GONE.md' is not an exported file",
                     reviewed_images={self.IMAGE: self.approval(provenance="docs/GONE.md")})
        self.refused("not a safe relative path", reviewed_images={self.IMAGE: self.approval(provenance="../MEDIA.md")})
        self.refused("excluded 'artifacts'",
                     reviewed_images={self.IMAGE: self.approval(provenance="artifacts/private.log")})
        self.refused("must be a text document, not a reviewed image",
                     reviewed_images={self.IMAGE: self.approval(provenance=self.IMAGE)})
        (self.src / "docs" / "MEDIA.md").write_bytes(b"\xff\xfe review notes\n")
        self.refused(f"{self.IMAGE}: provenance 'docs/MEDIA.md' is not UTF-8 text")

    def test_malformed_duplicate_and_unused_approvals_are_refused(self):
        good = self.approval()
        for approvals, fragment in (([self.IMAGE], "reviewed_images must be an object"),
                                    ({self.IMAGE: good["sha256"]}, "the approval must be an object"),
                                    ({self.IMAGE: {"sha256": good["sha256"]}}, "must have exactly the keys"),
                                    ({self.IMAGE: dict(good, reviewer="me")}, "must have exactly the keys"),
                                    ({self.IMAGE: dict(good, sha256=good["sha256"].upper())}, "64 lowercase hex"),
                                    ({self.IMAGE: dict(good, sha256=good["sha256"][:63])}, "64 lowercase hex"),
                                    ({self.IMAGE: dict(good, sha256=None)}, "64 lowercase hex"),
                                    ({self.IMAGE: good, "docs/media/Screen.png": good}, "differ only in case"),
                                    ({self.IMAGE: good, "docs/media/other.png": good},
                                     "'docs/media/other.png' is not an exported file; the approval is unused")):
            with self.subTest(fragment):
                self.refused(fragment, reviewed_images=approvals)
        # An approval for an optional file that is absent is unused too.
        (self.src / self.IMAGE).unlink()
        self.refused("approval is unused", files=["notes.txt", "docs/MEDIA.md"], optional_files=[self.IMAGE])
        (self.src / self.IMAGE).write_bytes(self.image)
        # Duplicate JSON keys (silently last-wins in json.load) are refused, for the image and for approval keys.
        entry, image, digest = json.dumps(good), json.dumps(self.IMAGE), json.dumps(good["sha256"])
        twice = "{" + f"{image}: {entry}, {image}: {entry}" + "}"
        key_twice = "{" + f'{image}: {entry[:-1]}, "sha256": {digest}' + "}}"
        for raw, fragment in ((twice, "approved more than once"),
                              (key_twice, "approval key 'sha256' is given more than once")):
            with self.subTest(fragment):
                text = json.dumps(dict(self.manifest, reviewed_images="@@")).replace('"@@"', raw)
                (self.src / "manifest.json").write_text(text)
                with self.assertRaises(export_release.Refused) as caught:
                    export_release.check(str(self.src), str(self.src / "manifest.json"))
                self.assertTrue(any(fragment in p for p in caught.exception.problems), caught.exception.problems)

    def test_metadata_ancillary_and_palette_chunks_are_refused(self):
        for kind, body in ((b"tEXt", b"Comment\x00hello"), (b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00x"),
                           (b"eXIf", b"MM\x00*"), (b"tIME", b"\x07\xea\x0a\x10\x00\x00\x00"),
                           (b"pHYs", b"\x00\x00\x0b\x13\x00\x00\x0b\x13\x01"), (b"PLTE", b"\x00\x00\x00")):
            for where in ("before_idat", "after_idat"):
                with self.subTest(kind=kind, where=where):
                    data = tiny_png(**{where: [png_chunk(kind, body)]})
                    self.refused(f"chunk {kind.decode()} is not permitted",
                                 reviewed_images=self.approve(data))
        rows = b"\x00\x00\x00\x00" * 2
        self.refused("colour type 3 is not permitted", reviewed_images=self.approve(tiny_png(colour=3, rows=rows)))

    def test_truncated_corrupt_and_trailing_png_bytes_are_refused(self):
        good = self.image
        flipped = bytearray(good)
        flipped[45] ^= 0x01  # inside the IDAT data: the CRC no longer matches
        ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0))
        iend = png_chunk(b"IEND", b"")
        idat = png_chunk(b"IDAT", zlib.compress(b"\x00" * 10))
        rows = b"".join(b"\x00" + b"\x01\x02\x03" * 3 for _ in range(2))
        whole = zlib.compress(rows)
        cases = {
            "truncated": (good[:-5], "truncated chunk"),
            "no IEND": (good[:-12], "no IEND chunk"),
            "bad CRC": (bytes(flipped), "bad CRC"),
            "trailing bytes": (good + b"\x00", "1 byte(s) after IEND"),
            "bad signature": (b"\x89PNX" + good[4:], "no PNG signature"),
            "IHDR not first": (export_release.PNG_SIGNATURE + idat + ihdr + iend, "IHDR must be the first chunk"),
            "two IHDR": (export_release.PNG_SIGNATURE + ihdr + ihdr + idat + iend, "IHDR must be the first chunk"),
            "no IDAT": (export_release.PNG_SIGNATURE + ihdr + iend, "no IDAT chunk"),
            "zero width": (tiny_png(width=0, rows=b"\x00\x00"), "outside 1..16384"),
            "too tall": (tiny_png(height=16385, rows=b""), "outside 1..16384"),
            "short data": (export_release.PNG_SIGNATURE + ihdr + idat + iend, "decompresses to 10 bytes"),
            "long data": (tiny_png(rows=rows + b"\x00"), "more than the 20 bytes"),
            "not zlib": (export_release.PNG_SIGNATURE + ihdr + png_chunk(b"IDAT", b"not zlib") + iend,
                         "not a valid zlib stream"),
            "incomplete zlib": (export_release.PNG_SIGNATURE + ihdr + png_chunk(b"IDAT", whole[:-6]) + iend,
                                "incomplete"),
            "IDAT after IEND": (export_release.PNG_SIGNATURE + ihdr + png_chunk(b"IDAT", whole[:5])
                           + png_chunk(b"IEND", b"") + png_chunk(b"IDAT", whole[5:]), "after IEND"),
        }
        for name, (data, fragment) in cases.items():
            with self.subTest(name):
                self.refused(fragment, reviewed_images=self.approve(data))
        # Positive control: an exact, interlaced and multi-IDAT image is accepted.
        interlaced_rows = b"".join(b"\x00" + b"\x05\x06\x07" for _ in range(1))  # Adam7 pass 1 only (1x1 image)
        interlaced = (export_release.PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 1))
                      + png_chunk(b"IDAT", zlib.compress(interlaced_rows)) + iend)
        split = (export_release.PNG_SIGNATURE + ihdr + png_chunk(b"IDAT", whole[:5]) + png_chunk(b"IDAT", whole[5:])
                 + iend)
        for name, data in (("interlaced", interlaced), ("split", split)):
            with self.subTest(name):
                self.assertEqual(export_release.png_structure(data), (1, 1) if name == "interlaced" else (3, 2))
                self.export(f"release/{name}", reviewed_images=self.approve(data))

    def test_reviewed_png_bytes_are_still_scanned_for_credentials(self):
        secret = ("sk-ant-" + "c" * 24).encode()
        width = len(secret) // 3 + 1
        rows = b"\x00" + secret.ljust(width * 3, b"x")
        data = tiny_png(width=width, height=1, rows=rows, level=0)  # stored (uncompressed) zlib block
        self.assertIn(secret, data)
        self.refused(f"{self.IMAGE}: matches anthropic-key", reviewed_images=self.approve(data))

    def test_alpha_16_bit_and_sub_byte_samples_are_refused(self):
        """Only 8-bit greyscale/RGB: alpha and 16-bit samples can carry bytes a visual review never sees."""
        hidden = b"/Users/someone/private/path/"
        width = len(hidden) // 3 + 1
        payload = hidden.ljust(width * 3, b"/")

        def png(depth, colour, rows, w=width):
            ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, 1, depth, colour, 0, 0, 0))
            return export_release.PNG_SIGNATURE + ihdr + png_chunk(b"IDAT", zlib.compress(rows, 9)) + png_chunk(
                b"IEND", b"")

        # Fully transparent RGBA pixels whose RGB values carry the payload: renders as nothing, hides the bytes.
        rgba = b"\x00" + b"".join(payload[i:i + 3] + b"\x00" for i in range(0, len(payload), 3))
        # 16-bit RGB whose low bytes carry the payload under black high bytes.
        rgb16 = b"\x00" + b"".join(b"\x00" + bytes((c,)) for c in payload)
        cases = {"RGBA transparent": (png(8, 6, rgba), "colour type 6 is not permitted"),
                 "grey+alpha": (png(8, 4, b"\x00" + payload[:width * 2], w=width), "colour type 4 is not permitted"),
                 "RGB 16-bit": (png(16, 2, rgb16), "bit depth 16 is not permitted"),
                 "grey 16-bit": (png(16, 0, b"\x00" + payload[:width * 2]), "bit depth 16 is not permitted"),
                 "grey 1-bit": (png(1, 0, b"\x00" + bytes((width + 7) // 8)), "bit depth 1 is not permitted")}
        for name, (data, fragment) in cases.items():
            with self.subTest(name):
                self.assertNotIn(hidden, data)  # compressed: the raw-byte pattern scan alone could not see it
                self.refused(fragment, reviewed_images=self.approve(data))
        # Positive controls: 8-bit greyscale and the RGB 8-bit default are accepted.
        grey = png(8, 0, b"\x00" + bytes(range(width)))
        self.assertEqual(export_release.png_structure(grey), (width, 1))
        self.export("release/grey", reviewed_images=self.approve(grey))
        self.assertEqual(export_release.png_structure(self.image), (3, 2))

    def test_duplicate_top_level_reviewed_images_key_is_refused_before_reading(self):
        good = json.dumps({self.IMAGE: self.approval()})
        reached = self.stages()
        for first, second in ((good, "{}"), ('{"docs/media/bogus.png": 1}', good), (good, good)):
            with self.subTest(first=first, second=second):
                text = json.dumps(dict(self.manifest, reviewed_images="@@"))
                text = text[:-1] + ', "reviewed_images": "@@"}'
                text = text.replace('"@@"', first, 1).replace('"@@"', second, 1)
                self.assertEqual(text.count('"reviewed_images"'), 2)
                (self.src / "manifest.json").write_text(text)
                with self.assertRaises(export_release.Refused) as caught:
                    export_release.export(str(self.src), str(self.src / "manifest.json"),
                                          str(self.src / "release" / "dup"))
                self.assertTrue(any("reviewed_images is given more than once" in p for p in caught.exception.problems),
                                caught.exception.problems)
                self.assertFalse((self.src / "release" / "dup").exists())
        self.assertEqual(reached, [], "the duplicate key is refused before any content is read")
        # Unchanged: another duplicated top-level key keeps json's last-wins value, and a manifest without
        # reviewed_images keeps the text-only behaviour.
        text_only = {k: v for k, v in dict(self.manifest, files=["notes.txt"]).items() if k != "reviewed_images"}
        (self.src / "manifest.json").write_text(json.dumps(text_only)[:-1] + ', "max_file_bytes": 1000}')
        summary = export_release.check(str(self.src), str(self.src / "manifest.json"))
        self.assertEqual(summary["files"], 4)
        self.assertNotIn("reviewed_images", summary)

    def test_cli_states_the_limits_of_the_image_checks(self):
        (self.src / "manifest.json").write_text(json.dumps(self.manifest))
        args = ["--manifest", str(self.src / "manifest.json"), "--source", str(self.src)]
        for argv in (["--check"], ["--output", str(self.src / "release" / "cli")]):
            with self.subTest(argv[0]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(export_release.main(argv + args), 0)
                line = [x for x in out.getvalue().splitlines() if x.startswith("reviewed PNG images: 1 (")]
                self.assertEqual(len(line), 1, out.getvalue())
                for claim in ("pixels are not inspected", "no decoding, OCR or steganography check",
                              "does not see compressed image data", "externally decoded and visually reviewed"):
                    self.assertIn(claim, line[0])
        doc = export_release.__doc__
        for claim in ("does not decode or inspect pixels", "no\nOCR or steganography", "filter bytes are not validated",
                      "certifies nothing about secrets"):
            self.assertIn(claim, doc)
        # The documented limit, pinned: a credential inside compressed image data is not found by the byte scan.
        secret = ("sk-ant-" + "c" * 24).encode()
        width = len(secret) // 3 + 1
        data = tiny_png(width=width, height=1, rows=b"\x00" + secret.ljust(width * 3, b"x"), level=9)
        self.assertNotIn(secret, data)
        self.assertEqual(export_release.scan({self.IMAGE: (data, False), "docs/MEDIA.md": (b"notes\n", False)},
                                             export_release.SECRET_PATTERNS, {self.IMAGE: self.approval(data)}), [])


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

    def test_blender_lane_stops_on_a_failing_version_query(self):
        # Labelled fake Blender (no real Blender is run): it logs each call and fails as configured.
        with tempfile.TemporaryDirectory(prefix="skid blender ") as tmp:
            fake, log = Path(tmp) / "fake-blender", Path(tmp) / "calls.log"
            fake.write_text('#!/bin/sh\necho "$1" >> "$FAKE_LOG"\n'
                            'if [ "$1" = "--version" ]; then echo "Blender 0.0 (fake)"; echo second; '
                            'exit "$FAKE_VERSION_EXIT"; fi\nexit 6\n')
            os.chmod(fake, 0o755)
            for version_exit, want in ((5, 5), (0, 6)):
                log.write_text("")
                done = subprocess.run(["sh", str(ROOT / "scripts" / "check_blender.sh")], capture_output=True,
                                      text=True, timeout=60, env=dict(os.environ, SKID_BLENDER=str(fake),
                                                                      FAKE_LOG=str(log),
                                                                      FAKE_VERSION_EXIT=str(version_exit)))
                calls = log.read_text().split()
                self.assertEqual(done.returncode, want, done.stdout + done.stderr)
                if version_exit:
                    self.assertEqual(calls, ["--version"])  # nothing runs after the failed version query
                    self.assertIn("--version failed (exit 5)", done.stderr)
                else:
                    self.assertEqual(calls, ["--version", "--background"])
                    self.assertEqual(done.stdout, "Blender 0.0 (fake)\n")  # first line only


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
            checked = {c["name"] for c in evidence["checks"]}
            for name in ("drafting r0: item index count", "drafting r1: item index count",
                         "core nozzle move: ent-tank outlet_height before the move == expected from_m",
                         "core nozzle move: ent-tank outlet_height after the move"):
                self.assertIn(name, checked)  # the declared expectations are asserted, not only recorded

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

    def test_wrong_index_count_and_nozzle_origin_fail_their_checks(self):
        with tempfile.TemporaryDirectory(prefix="skid reference ") as tmp:
            expected = json.loads((ROOT / "scripts" / "reference_expected.json").read_text())
            expected["index_items"] += 1
            expected["nozzle_move"]["from_m"] += 0.05
            wrong = Path(tmp) / "wrong.json"
            wrong.write_text(json.dumps(expected))
            out = Path(tmp) / "run"
            done = self.run_reference("--output", str(out), "--expected", str(wrong))
            self.assertEqual(done.returncode, 1, done.stdout[-2000:])
            evidence = json.loads((out / "evidence.json").read_text())
            failed = {c["name"] for c in evidence["checks"] if not c["ok"]}
            self.assertEqual(failed, {"drafting r0: item index count", "drafting r1: item index count",
                                      "core nozzle move: ent-tank outlet_height before the move == expected from_m"})

    def test_missing_or_malformed_expectations_are_usage_errors_before_any_output(self):
        good = json.loads((ROOT / "scripts" / "reference_expected.json").read_text())
        nozzle = {k: v for k, v in good["nozzle_move"].items() if k != "from_m"}
        cases = {"missing": None, "not-json": "{", "not-utf8": b"\xff\xfe", "not-an-object": "[]",
                 "index-items-text": dict(good, index_items="13"), "count-boolean": dict(good, legend_keys=True),
                 "count-negative": dict(good, model=dict(good["model"], ports=-1)),
                 "no-from-m": dict(good, nozzle_move=nozzle), "no-cad": {k: v for k, v in good.items() if k != "cad"},
                 "nan-height": json.dumps(good).replace('"outlet_height_m": 0.3', '"outlet_height_m": NaN')}
        self.assertIn("NaN", cases["nan-height"])
        with tempfile.TemporaryDirectory(prefix="skid reference ") as tmp:
            for name, content in cases.items():
                with self.subTest(name):
                    path = Path(tmp) / f"{name}.json"
                    if isinstance(content, bytes):
                        path.write_bytes(content)
                    elif content is not None:
                        path.write_text(content if isinstance(content, str) else json.dumps(content))
                    out = Path(tmp) / f"out-{name}"
                    done = self.run_reference("--output", str(out), "--expected", str(path))
                    self.assertEqual(done.returncode, 2, done.stdout[-1000:] + done.stderr[-2000:])
                    self.assertIn("--expected", done.stderr)
                    self.assertNotIn("Traceback", done.stderr)
                    self.assertFalse(out.exists(), "a usage error creates no output directory")


if __name__ == "__main__":
    unittest.main()
