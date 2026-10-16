"""Saved editable project names: validation, the rename_project command, history, storage and drafting titles."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from html.parser import HTMLParser

from skid.core.commands import COMMAND_TYPES, apply_command
from skid.core.errors import CommandError
from skid.core.storage import load_document, save_document
from skid.core.validation import (
    PROJECT_NAME_MAX_LENGTH,
    canonical_digest,
    project_display_name,
    project_name_problem,
    validate_document,
)
from tests.test_drafting_support import DraftingCase, fixture_document, parse, visible_text

PROJECT_ID = "project-pump-skid"
HOSTILE = "Plant <script>alert(\"x\")</script> & 'Co' ]]> <!--"
# The twelve bidirectional formatting code points a name may not contain.
BIDI_FORBIDDEN = (0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
# Text with no base character: invisible format characters, combining marks and spaces only.
NO_BASE_CHARACTER = (
    "\u200b", "\ufeff", "\u2060", "\u00ad", "\u200b\u200b\u2060",  # format only
    "\u0301", "\u0301\u0308", "\u20dd",  # combining marks only
    "\u200b \u200b", "\u00ad\u00a0\u0301", "\u2060\u3000\ufeff", "\u0301\u200d\u0300",  # mixtures
)
# Genuine names with marks, format joiners or non-Latin scripts that must remain valid.
WITH_BASE_CHARACTER = (
    "e\u0301té", "नमस\u094dत\u0947", "ป\u0e31\u0e4aม",
    "משאבה", "\U0001f469\u200d\U0001f527", "\U0001f1f9\U0001f1f7", "1\ufe0f\u20e3",
    "Skid\u200b2", "co\u00adoperative", "—", "™", "#",
)


def errors(diagnostics):
    return [d for d in diagnostics if d["severity"] == "error"]


def codes(diagnostics):
    return {d["code"] for d in diagnostics}


def command(document, kind, payload, command_id=None, **extra):
    return {"id": command_id or f"cmd-{kind}", "expected_revision": document["current"]["revision"],
            "type": kind, "payload": payload, **extra}


def run(document, kind, payload, command_id=None):
    return apply_command(document, command(document, kind, payload, command_id))


def rename(document, name):
    return run(document, "rename_project", {"name": name})[0]


def rejected_codes(test, document, cmd):
    with test.assertRaises(CommandError) as caught:
        apply_command(document, cmd)
    return codes(caught.exception.diagnostics)


class _HeadingText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings, self._open = [], False

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self.headings.append("")
            self._open = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self._open = False

    def handle_data(self, data):
        if self._open:
            self.headings[-1] += data


def h1_texts(html):
    """Decoded visible text of every h1, including text inside nested inline elements."""
    parser = _HeadingText()
    parser.feed(html)
    parser.close()
    return parser.headings


class NameRuleTests(unittest.TestCase):
    def test_valid_names_including_safe_unicode(self):
        for name in ("Pump skid", "P", "\u00dcn\u00efc\u00f6d\u00e9 \u6cf5\u9001\u64ac \u2014 \u041d\u0430\u0441\u043e\u0441",
                     "Skid \U0001f469\u200d\U0001f527 #2", "a" * PROJECT_NAME_MAX_LENGTH, "Tab inner\u00a0NBSP",
                     "\u0645\u0636\u062e\u0629", HOSTILE, *WITH_BASE_CHARACTER):
            self.assertIsNone(project_name_problem(name), repr(name))

    def test_malformed_names(self):
        bad = [None, 7, True, ["x"], {"name": "x"}, "", "   ", "\t", " lead", "trail ", "\u3000wide",
               "a" * (PROJECT_NAME_MAX_LENGTH + 1), "line\nbreak", "tab\there", "nul\x00", "del\x7f", "c1\x85",
               "sep\u2028", "para\u2029", "lone\ud800", "non\ufffe", "non\uffff", "non\ufdd0", "astral\U0001fffe"]
        for value in bad:
            self.assertIsInstance(project_name_problem(value), str, repr(value))

    def test_every_bidi_formatting_character_is_refused_by_code_point(self):
        for code in BIDI_FORBIDDEN:
            for value in (f"Pump{chr(code)}skid", f"{chr(code)}Pump", f"Pump{chr(code)}"):
                self.assertEqual(project_name_problem(value),
                                 f"project name contains the disallowed character U+{code:04X}", repr(value))
        # Neighbouring format characters outside the blocklist stay allowed beside a base character.
        for value in ("a\u200bb", "a\u200cb", "a\u200db", "a\u2060b", "a\u206ab"):
            self.assertIsNone(project_name_problem(value), repr(value))

    def test_text_without_a_base_character_is_refused(self):
        for value in NO_BASE_CHARACTER:
            self.assertEqual(project_name_problem(value),
                             "project name must contain a visible letter, number, punctuation mark or symbol",
                             repr(value))
        # A forbidden character keeps its specific error even when no base character is present.
        self.assertIn("U+202E", project_name_problem("\u200b\u202e"))

    def test_display_name_falls_back_to_stable_id(self):
        self.assertEqual(project_display_name({"id": PROJECT_ID}), PROJECT_ID)
        self.assertEqual(project_display_name({"id": PROJECT_ID, "name": "Skid"}), "Skid")
        self.assertEqual(project_display_name({"id": PROJECT_ID, "name": " bad"}), PROJECT_ID)
        self.assertIsNone(project_display_name({"id": ""}))
        self.assertIsNone(project_display_name(None))

    def test_helper_is_pure_and_cad_free(self):
        import subprocess
        import sys
        code = ("import sys; from skid.core.validation import project_name_problem; "
                "assert project_name_problem('x') is None; "
                "bad = [m for m in ('cadquery', 'OCP', 'bpy') if m in sys.modules]; assert not bad, bad")
        subprocess.run([sys.executable, "-c", code], check=True, cwd=os.path.dirname(os.path.dirname(__file__)))


class DocumentValidationTests(unittest.TestCase):
    def setUp(self):
        self.document = fixture_document()

    def test_legacy_unnamed_document_is_valid(self):
        self.assertNotIn("name", self.document["current"])
        self.assertEqual(errors(validate_document(self.document)), [])

    def test_stored_names_are_validated_on_project_and_snapshots(self):
        named = copy.deepcopy(self.document)
        named["current"]["name"] = "Skid"
        self.assertEqual(errors(validate_document(named)), [])
        for value in ("", " Skid", 3, None, "a\nb"):
            bad = copy.deepcopy(self.document)
            bad["current"]["name"] = value
            self.assertIn("invalid_project_name", codes(errors(validate_document(bad))), repr(value))
        moved = rename(rename(self.document, "One"), "Two")
        self.assertEqual(errors(validate_document(moved)), [])
        moved["undo"][-1]["name"] = "bad\x00"
        found = errors(validate_document(moved))
        self.assertIn("invalid_project_name", codes(found))
        self.assertTrue(any(d["message"].startswith("undo[1]:") for d in found))

    def test_name_does_not_satisfy_or_require_assumption_references(self):
        named = rename(self.document, "Skid")
        cmd = command(named, "rename_project", {"name": "Other"},
                      assumptions={"asm-new": {"description": "d", "source": "s"}})
        self.assertIn("unreferenced_assumption", rejected_codes(self, named, cmd))


class RenameCommandTests(unittest.TestCase):
    def setUp(self):
        self.document = fixture_document()
        self.before = copy.deepcopy(self.document)

    def test_rename_is_one_revision_and_preserves_identity_and_engineering_data(self):
        self.assertIn("rename_project", COMMAND_TYPES)
        document, warnings = run(self.document, "rename_project", {"name": "  Pump skid A  "})
        project = document["current"]
        self.assertEqual(project["name"], "Pump skid A")
        self.assertEqual(project["id"], PROJECT_ID)
        self.assertEqual(project["revision"], 1)
        self.assertNotIn("no_op", codes(warnings))
        for key in ("model", "pid", "physical"):
            self.assertEqual(project[key], self.before["current"][key])
        self.assertEqual(len(document["undo"]), 1)
        self.assertNotIn("name", document["undo"][0])
        self.assertEqual(self.document, self.before)  # input untouched
        self.assertNotEqual(canonical_digest(project), canonical_digest(self.before["current"]))

    def test_malformed_payloads_are_rejected_without_change(self):
        cases = [{}, {"name": "x", "extra": 1}, {"name": None}, {"name": 5}, {"name": ""}, {"name": "   "},
                 {"name": "a" * (PROJECT_NAME_MAX_LENGTH + 1)}, {"name": "x\ny"}, {"name": "\u202eevil"},
                 {"name": "nul\x00"}]
        for payload in cases:
            with self.assertRaises(CommandError, msg=repr(payload)):
                apply_command(self.document, command(self.document, "rename_project", payload))
        self.assertEqual(self.document, self.before)
        self.assertIn("invalid_project_name",
                      rejected_codes(self, self.document, command(self.document, "rename_project", {"name": " "})))
        stale = dict(command(self.document, "rename_project", {"name": "x"}), expected_revision=5)
        self.assertIn("stale_revision", rejected_codes(self, self.document, stale))

    def test_invisible_and_bidi_names_are_rejected_by_the_command_without_change(self):
        named = rename(self.document, "Skid")
        named_before = copy.deepcopy(named)
        surrounded = [f" {value} " for value in NO_BASE_CHARACTER[:4]]  # trimming leaves no base character
        bidi = [f"Pump{chr(code)}skid" for code in BIDI_FORBIDDEN]
        for document, before in ((self.document, self.before), (named, named_before)):
            for value in (*NO_BASE_CHARACTER, *surrounded, *bidi):
                cmd = command(document, "rename_project", {"name": value})
                self.assertEqual(rejected_codes(self, document, cmd), {"invalid_project_name"}, repr(value))
                batch = command(document, "batch", {"operations": [
                    {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-201"}},
                    {"type": "rename_project", "payload": {"name": value}}]}, "cmd-batch")
                self.assertIn("invalid_project_name", rejected_codes(self, document, batch), repr(value))
            self.assertEqual(document, before)
        self.assertEqual(named["current"]["name"], "Skid")
        # Genuine names with marks, joiners and other scripts are accepted and stored as given.
        for value in WITH_BASE_CHARACTER:
            self.assertEqual(rename(self.document, value)["current"]["name"], value, repr(value))

    def test_stored_invisible_name_is_an_invalid_document(self):
        for value in ("\u200b", "\u0301", f"a{chr(BIDI_FORBIDDEN[0])}b"):
            bad = copy.deepcopy(self.document)
            bad["current"]["name"] = value
            self.assertIn("invalid_project_name", codes(errors(validate_document(bad))), repr(value))
            self.assertEqual(project_display_name(bad["current"]), PROJECT_ID)

    def test_trimmed_bound_counts_after_trimming(self):
        name = "b" * PROJECT_NAME_MAX_LENGTH
        document = rename(self.document, f" {name} ")
        self.assertEqual(document["current"]["name"], name)

    def test_unchanged_name_is_a_no_op(self):
        named = rename(self.document, "Skid")
        for value in ("Skid", "  Skid "):
            again, warnings = run(named, "rename_project", {"name": value})
            self.assertEqual(again, named)
            self.assertIn("no_op", codes(warnings))
        # A name equal to the id is still a distinct (stored) state from an unnamed project.
        changed, warnings = run(self.document, "rename_project", {"name": PROJECT_ID})
        self.assertEqual(changed["current"]["revision"], 1)
        self.assertNotIn("no_op", codes(warnings))
        # Case changes are real changes.
        self.assertEqual(rename(named, "SKID")["current"]["revision"], named["current"]["revision"] + 1)

    def test_rename_and_ordinary_commands_undo_redo_through_unnamed_history(self):
        d1 = rename(self.document, "First")
        d2 = run(d1, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})[0]
        d3 = rename(d2, "Second")
        self.assertEqual(d3["current"]["revision"], 3)
        self.assertEqual([s.get("name") for s in d3["undo"]], [None, "First", "First"])
        self.assertNotIn("name", d3["undo"][0])

        u1 = run(d3, "undo", {})[0]
        self.assertEqual(u1["current"]["name"], "First")
        self.assertEqual(u1["current"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        u2 = run(u1, "undo", {})[0]
        self.assertEqual(u2["current"]["name"], "First")
        self.assertEqual(u2["current"]["model"]["entities"]["ent-pump"]["tag"], "P-101")
        u3 = run(u2, "undo", {})[0]
        self.assertNotIn("name", u3["current"])  # absence restored, not copied from the present
        self.assertEqual(project_display_name(u3["current"]), PROJECT_ID)
        for key in ("model", "pid", "physical", "id"):
            self.assertEqual(u3["current"][key], self.before["current"][key])
        self.assertEqual(u3["current"]["revision"], 6)
        self.assertEqual([s.get("name") for s in u3["redo"]], ["Second", "First", "First"])
        self.assertEqual(errors(validate_document(u3)), [])

        r1 = run(u3, "redo", {})[0]
        self.assertEqual(r1["current"]["name"], "First")
        self.assertNotIn("name", r1["undo"][-1])
        r3 = run(run(r1, "redo", {})[0], "redo", {})[0]
        self.assertEqual(r3["current"]["name"], "Second")
        self.assertEqual(r3["current"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        self.assertEqual(r3["redo"], [])

        # A new command after undo clears redo, and the restored-unnamed state is a plain legacy project.
        branched = run(u3, "rename_entity", {"entity_id": "ent-pump", "tag": "P-301"})[0]
        self.assertEqual(branched["redo"], [])
        self.assertNotIn("name", branched["current"])
        self.assertNotIn("name", branched["undo"][-1])

    def test_legacy_snapshots_without_name_restore_to_unnamed(self):
        legacy = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})[0]
        named = rename(legacy, "Named later")
        back = run(run(named, "undo", {})[0], "undo", {})[0]
        self.assertNotIn("name", back["current"])
        self.assertEqual(back["current"]["model"]["entities"]["ent-pump"]["tag"], "P-101")


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.document = fixture_document()

    def batch(self, document, operations):
        return apply_command(document, command(document, "batch", {"operations": operations}, "cmd-batch"))

    def test_batch_rename_with_ordinary_edit_is_one_revision_and_one_undo(self):
        document, _ = self.batch(self.document, [
            {"type": "rename_project", "payload": {"name": "Batch skid"}},
            {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-201"}},
        ])
        self.assertEqual(document["current"]["revision"], 1)
        self.assertEqual(document["current"]["name"], "Batch skid")
        self.assertEqual(len(document["undo"]), 1)
        undone = run(document, "undo", {})[0]
        self.assertNotIn("name", undone["current"])
        self.assertEqual(undone["current"]["model"]["entities"]["ent-pump"]["tag"], "P-101")

    def test_batch_change_and_restore_is_a_no_op(self):
        named = rename(self.document, "Original")
        same, warnings = self.batch(named, [
            {"type": "rename_project", "payload": {"name": "Temporary"}},
            {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-999"}},
            {"type": "rename_entity", "payload": {"entity_id": "ent-pump", "tag": "P-101"}},
            {"type": "rename_project", "payload": {"name": "Original"}},
        ])
        self.assertEqual(same, named)
        self.assertIn("no_op", codes(warnings))

    def test_rejected_operation_rejects_the_whole_batch(self):
        before = copy.deepcopy(self.document)
        with self.assertRaises(CommandError) as caught:
            self.batch(self.document, [
                {"type": "rename_project", "payload": {"name": "Fine"}},
                {"type": "rename_project", "payload": {"name": "bad\x00"}},
            ])
        self.assertTrue(any(d["message"].startswith("operations[1] (rename_project)")
                            for d in caught.exception.diagnostics))
        self.assertEqual(self.document, before)


class StorageTests(unittest.TestCase):
    def test_save_reload_preserves_name_and_unnamed_history(self):
        document = fixture_document()
        with tempfile.TemporaryDirectory(prefix="skid-name-") as tmp:
            path = os.path.join(tmp, "project.json")
            save_document(path, document, None)
            unicode_name = "Насос 泵 ✓ <&>"
            named = rename(document, unicode_name)
            save_document(path, named, 0)
            loaded = load_document(path)
            self.assertEqual(loaded, named)
            self.assertEqual(loaded["current"]["name"], unicode_name)
            self.assertNotIn("name", loaded["undo"][0])
            self.assertEqual(canonical_digest(loaded["current"]), canonical_digest(named["current"]))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["current"]["name"], unicode_name)
            undone = run(loaded, "undo", {})[0]
            save_document(path, undone, 1)
            reloaded = load_document(path)
            self.assertNotIn("name", reloaded["current"])
            self.assertEqual(reloaded["redo"][0]["name"], unicode_name)
            redone = run(reloaded, "redo", {})[0]
            self.assertEqual(redone["current"]["name"], unicode_name)

    def test_invalid_stored_name_is_refused_on_save_and_load(self):
        from skid.core.errors import DocumentError
        document = fixture_document()
        bad = copy.deepcopy(document)
        bad["current"]["name"] = " padded"
        with tempfile.TemporaryDirectory(prefix="skid-name-") as tmp:
            path = os.path.join(tmp, "project.json")
            with self.assertRaises(DocumentError):
                save_document(path, bad, None)
            self.assertFalse(os.path.exists(path))
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(bad, handle)
            with self.assertRaises(DocumentError):
                load_document(path)


class DraftingTitleTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()

    def title_block_text(self, root):
        return visible_text([e for e in root.iter() if e.get("data-layer") == "title-block"][0])

    def svg_title(self, root):
        return [e for e in root if e.tag.endswith("title")][0].text

    def test_named_titles_show_name_and_identities_keep_project_id(self):
        named = rename(self.document, "North pump skid")
        out, manifests, _ = self.build(named["current"])
        self.assertEqual(manifests["pid"]["status"], "complete")
        for adapter in ("pid", "legend", "description"):
            self.assertEqual(manifests[adapter]["project_id"], PROJECT_ID)
        for name in ("pid.svg", "legend.svg"):
            root = parse(out / name)
            self.assertEqual(root.get("data-project-id"), PROJECT_ID)
            self.assertIn("North pump skid", self.title_block_text(root))
            self.assertNotIn(PROJECT_ID, self.title_block_text(root))
            self.assertIn(f"North pump skid ({PROJECT_ID})", self.svg_title(root))
        metadata = [e for e in parse(out / "pid.svg").iter() if e.get("project-id") is not None]
        self.assertTrue(metadata and all(e.get("project-id") == PROJECT_ID for e in metadata))
        description = self.read_json(out / "description.json")
        self.assertEqual(description["project_id"], PROJECT_ID)
        self.assertEqual(description["project_name"], "North pump skid")
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertEqual(h1_texts(html), ["Process description — North pump skid: TK-101 TO OUT-101 VIA P-101"])
        self.assertIn(f'data-project-id="{PROJECT_ID}"', html)
        self.assertIn(f"<code>{PROJECT_ID}</code>", html)
        inventory = self.read_json(out / "drawing.inventory.json")
        self.assertEqual(inventory["project_id"], PROJECT_ID)
        # Equipment tags and stored coordinates are untouched by naming.
        self.assertIn("P-101", visible_text(parse(out / "pid.svg")))
        self.assertEqual(named["current"]["pid"], self.document["current"]["pid"])

    def test_unnamed_legacy_titles_fall_back_to_project_id(self):
        out, _, _ = self.build(self.document["current"])
        root = parse(out / "pid.svg")
        self.assertIn(PROJECT_ID, self.title_block_text(root))
        self.assertTrue(self.svg_title(root).startswith(f"P&ID-001 · {PROJECT_ID} · revision 0"))
        self.assertIsNone(self.read_json(out / "description.json")["project_name"])
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertEqual(h1_texts(html), ["Process description — TK-101 TO OUT-101 VIA P-101"])
        self.assertIn(f"<span>Project <b>{PROJECT_ID}</b></span>", html)

    def test_name_change_changes_digest_and_rebuild_identity(self):
        named = rename(self.document, "Digest check")
        _, unnamed_manifests, _ = self.build(self.document["current"])
        _, named_manifests, _ = self.build(named["current"])
        self.assertNotEqual(unnamed_manifests["pid"]["input_digest"], named_manifests["pid"]["input_digest"])
        self.assertEqual(named_manifests["pid"]["input_digest"], canonical_digest(named["current"]))
        self.assertEqual(named_manifests["pid"]["source_revision"], 1)

    def test_hostile_name_is_escaped_in_svg_and_html(self):
        named = rename(self.document, HOSTILE)
        out, manifests, _ = self.build(named["current"])
        self.assertEqual(manifests["pid"]["status"], "complete")
        for name in ("pid.svg", "legend.svg"):
            raw = (out / name).read_text(encoding="utf-8")
            self.assertNotIn("<script", raw)
            self.assertNotIn("]]> <!--", raw)
            root = parse(out / name)  # still well-formed XML
            self.assertFalse([e for e in root.iter() if e.tag.endswith("script")])
            self.assertIn(HOSTILE, self.title_block_text(root))
            self.assertIn(HOSTILE, self.svg_title(root))
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", html)
        self.assertNotIn("<!--", html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;Co&#x27;", html)
        self.assertEqual(self.read_json(out / "description.json")["project_name"], HOSTILE)


if __name__ == "__main__":
    unittest.main()
