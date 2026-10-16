"""Persistence tests: strict loading, validated atomic saves, revision checks, and recovery."""

import copy
import errno
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from examples.make_fixture import make_document
from skid.core import DocumentError, RevisionConflict, canonical_digest, load_document, save_document
from skid.core import storage

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "examples" / "pump_skid.json"


def snapshot_of(project):
    return copy.deepcopy({key: project[key] for key in ("model", "pid", "physical")})


def edited(document, suffix):
    """Return the document one accepted edit later, recorded the way the command layer records it."""
    doc = copy.deepcopy(document)
    project = doc["current"]
    doc["undo"].append(snapshot_of(project))
    doc["redo"] = []
    entity = next(iter(project["model"]["entities"].values()))
    entity["tag"] += suffix
    project["revision"] += 1
    return doc


def undone(document):
    doc = copy.deepcopy(document)
    project = doc["current"]
    previous = doc["undo"].pop()
    doc["redo"].append(snapshot_of(project))
    project.update(previous)
    project["revision"] += 1
    return doc


def codes(exc):
    return {d["code"] for d in exc.diagnostics}


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "skid.json")
        self.base = make_document()
        self.revision = self.base["current"]["revision"]

    def read(self, path=None):
        with open(path or self.path, "rb") as handle:
            return handle.read()

    def write(self, data, path=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path or self.path, "wb") as handle:
            handle.write(data)

    def leftovers(self):
        return sorted(name for name in os.listdir(self.dir) if name.endswith(".tmp"))

    def save_base(self):
        save_document(self.path, self.base, None)
        return self.read()


class RoundTripTests(StorageTestCase):
    def test_new_document_round_trip(self):
        save_document(self.path, self.base, None)
        loaded = load_document(self.path)
        self.assertEqual(loaded, self.base)
        self.assertEqual(canonical_digest(loaded["current"]), canonical_digest(self.base["current"]))
        for part, name in (("model", "entities"), ("model", "ports"), ("pid", "symbols"), ("physical", "routes")):
            self.assertEqual(list(loaded["current"][part][name]), list(self.base["current"][part][name]))
        self.assertEqual(self.leftovers(), [])

    def test_history_round_trip(self):
        document = undone(edited(edited(self.base, "-A"), "-B"))
        save_document(self.path, document, None)
        loaded = load_document(self.path)
        self.assertEqual(loaded, document)
        self.assertEqual((len(loaded["undo"]), len(loaded["redo"])), (1, 1))
        self.assertEqual(loaded["current"]["revision"], self.revision + 3)

    def test_presentation_provenance_and_unresolved_state_round_trip(self):
        document = copy.deepcopy(self.base)
        project = document["current"]
        entities = project["model"]["entities"]
        tank = next(entity for entity in entities.values() if entity["kind"] == "tank")
        tank["tag"] = "Behälter-101 ✓"
        indicator = next(entity for entity in entities.values() if entity["kind"] == "indicator")
        unit = indicator["parameters"]["diameter"]["unit"]
        indicator["parameters"]["diameter"] = {"value": 0.1 + 0.2, "unit": unit, "source": "Measured on site"}
        pump_id = next(eid for eid, entity in entities.items() if entity["kind"] == "centrifugal_pump")
        project["physical"]["placements"][pump_id].update(
            position_m=None, rotation_deg=None, status="unresolved", reason="awaiting layout", manual=True
        )
        ports = project["model"]["ports"]
        for cid, connection in project["model"]["connections"].items():
            ends = (ports[connection["from_port_id"]]["entity_id"], ports[connection["to_port_id"]]["entity_id"])
            if pump_id in ends:
                project["physical"]["routes"][cid].update(status="needs_reroute", reason="pump unplaced")
        for symbol in project["pid"]["symbols"].values():
            symbol["manual"] = not symbol["manual"]
        save_document(self.path, document, None)
        loaded = load_document(self.path)
        self.assertEqual(loaded, document)
        value = next(e for e in loaded["current"]["model"]["entities"].values() if e["kind"] == "indicator")
        self.assertEqual(value["parameters"]["diameter"]["value"], 0.1 + 0.2)

    def test_saved_bytes_are_strict_utf8_json(self):
        document = copy.deepcopy(self.base)
        next(iter(document["current"]["model"]["entities"].values()))["tag"] = "Pümpe-101"
        save_document(self.path, document, None)
        data = self.read()
        self.assertIn("Pümpe-101".encode("utf-8"), data)
        self.assertEqual(json.loads(data.decode("utf-8")), document)

    def test_committed_fixture_loads(self):
        self.assertEqual(load_document(FIXTURE_PATH), make_document())

    def test_successive_saves(self):
        save_document(self.path, self.base, None)
        first = edited(load_document(self.path), "-A")
        save_document(self.path, first, self.revision)
        second = edited(load_document(self.path), "-B")
        save_document(self.path, second, self.revision + 1)
        loaded = load_document(self.path)
        self.assertEqual(loaded, second)
        self.assertEqual(len(loaded["undo"]), 2)

    def test_resaving_identical_content(self):
        save_document(self.path, self.base, None)
        save_document(self.path, load_document(self.path), self.revision)
        self.assertEqual(load_document(self.path), self.base)

    def test_save_does_not_mutate_input(self):
        document = edited(self.base, "-A")
        before = copy.deepcopy(document)
        save_document(self.path, document, None)
        self.assertEqual(document, before)


class StrictLoadTests(StorageTestCase):
    def assertLoadRejects(self, data, *accepted_codes):
        self.write(data)
        with self.assertRaises(DocumentError) as caught:
            load_document(self.path)
        self.assertTrue(codes(caught.exception) & set(accepted_codes), caught.exception.diagnostics)
        return caught.exception

    def with_number(self, token):
        document = copy.deepcopy(self.base)
        placement = next(iter(document["current"]["physical"]["placements"].values()))
        placement["position_m"] = ["__NUMBER__", 0.0, 0.0]
        return json.dumps(document).replace('"__NUMBER__"', token, 1)

    def test_duplicate_top_level_key(self):
        self.assertLoadRejects('{"schema_version": 1, ' + json.dumps(self.base)[1:], "duplicate_json_key")

    def test_duplicate_map_key(self):
        key, record = next(iter(self.base["current"]["model"]["entities"].items()))
        needle = '"entities": {'
        text = json.dumps(self.base)
        self.assertIn(needle, text)
        duplicate = needle + json.dumps(key) + ": " + json.dumps(record) + ", "
        self.assertLoadRejects(text.replace(needle, duplicate, 1), "duplicate_json_key")

    def test_duplicate_record_field(self):
        entity = next(iter(self.base["current"]["model"]["entities"].values()))
        needle = '"tag": ' + json.dumps(entity["tag"])
        text = json.dumps(self.base)
        self.assertIn(needle, text)
        self.assertLoadRejects(text.replace(needle, '"tag": "DUPLICATE", ' + needle, 1), "duplicate_json_key")

    def test_nonfinite_numbers(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(token=token):
                self.assertLoadRejects(self.with_number(token), "nonfinite_number")

    def test_oversized_integer_literal(self):
        self.assertLoadRejects(self.with_number("1" * 5000), "invalid_json", "invalid_number")

    def test_invalid_utf8(self):
        self.assertLoadRejects(b'{"schema_version": 1, "tag": "\xff"}', "invalid_encoding")

    def test_byte_order_mark(self):
        self.assertLoadRejects(b"\xef\xbb\xbf" + json.dumps(self.base).encode("utf-8"), "invalid_json")

    def test_truncated_and_empty_files(self):
        text = json.dumps(self.base)
        for data in (text[: len(text) // 2], "", "   ", "{", "[1, 2"):
            with self.subTest(length=len(data)):
                self.assertLoadRejects(data, "invalid_json")

    def test_deep_nesting(self):
        self.assertLoadRejects("[" * 20000 + "]" * 20000, "invalid_json", "invalid_type")

    def test_lone_surrogate(self):
        document = copy.deepcopy(self.base)
        next(iter(document["current"]["model"]["entities"].values()))["tag"] = "__TAG__"
        self.assertLoadRejects(json.dumps(document).replace('"__TAG__"', '"\\ud800"', 1), "invalid_encoding")

    def test_top_level_must_be_an_object(self):
        self.assertLoadRejects("[]", "invalid_type")

    def test_missing_file(self):
        with self.assertRaises(DocumentError) as caught:
            load_document(os.path.join(self.dir, "absent.json"))
        self.assertIn("unreadable_document", codes(caught.exception))

    def test_structural_errors(self):
        document = copy.deepcopy(self.base)
        symbols = document["current"]["pid"]["symbols"]
        symbols[next(iter(symbols))]["entity_id"] = "no-such-entity"
        self.assertLoadRejects(json.dumps(document), "dangling_reference")

    def test_corrupt_history(self):
        document = edited(self.base, "-A")
        document["undo"][0]["revision"] = self.revision
        error = self.assertLoadRejects(json.dumps(document), "unknown_field")
        self.assertTrue(any(d["message"].startswith("undo[0]: ") for d in error.diagnostics), error.diagnostics)

    def test_history_cannot_outnumber_revisions(self):
        document = edited(self.base, "-A")
        document["undo"].append(snapshot_of(document["current"]))
        document["current"]["revision"] = 1
        self.assertLoadRejects(json.dumps(document), "history_revision_inconsistent")

    def test_unsupported_schema_version(self):
        document = copy.deepcopy(self.base)
        document["schema_version"] = 2
        self.assertLoadRejects(json.dumps(document), "unsupported_schema_version")


class SaveValidationTests(StorageTestCase):
    def test_invalid_document_is_not_written(self):
        document = copy.deepcopy(self.base)
        first, second = list(document["current"]["model"]["entities"].values())[:2]
        second["tag"] = first["tag"]
        with self.assertRaises(DocumentError) as caught:
            save_document(self.path, document, None)
        self.assertIn("duplicate_tag", codes(caught.exception))
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + storage.LOCK_SUFFIX), "validation must precede locking")
        self.assertEqual(self.leftovers(), [])

    def test_invalid_document_preserves_existing_file(self):
        original = self.save_base()
        document = edited(self.base, "-A")
        symbols = document["current"]["pid"]["symbols"]
        symbols[next(iter(symbols))]["entity_id"] = "no-such-entity"
        with self.assertRaises(DocumentError) as caught:
            save_document(self.path, document, self.revision)
        self.assertNotIsInstance(caught.exception, RevisionConflict)
        self.assertEqual(self.read(), original)

    def test_nonfinite_values_are_not_written(self):
        document = copy.deepcopy(self.base)
        placement = next(iter(document["current"]["physical"]["placements"].values()))
        placement["position_m"] = [0.0, float("inf"), 0.0]
        with self.assertRaises(DocumentError):
            save_document(self.path, document, None)
        self.assertFalse(os.path.exists(self.path))

    def test_non_documents_are_rejected(self):
        for value in (None, [], "document", {"schema_version": 1}):
            with self.subTest(value=value):
                with self.assertRaises(DocumentError):
                    save_document(self.path, value, None)
        self.assertFalse(os.path.exists(self.path))

    def test_expected_revision_type(self):
        for value in (True, -1, "0", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    save_document(self.path, self.base, value)


class RevisionTests(StorageTestCase):
    def assertConflict(self, document, expected, code):
        before = self.read() if os.path.exists(self.path) else None
        with self.assertRaises(RevisionConflict) as caught:
            save_document(self.path, document, expected)
        self.assertIn(code, codes(caught.exception))
        self.assertEqual(self.read() if os.path.exists(self.path) else None, before)
        self.assertEqual(self.leftovers(), [])

    def test_new_document_requires_absent_target(self):
        self.save_base()
        self.assertConflict(self.base, None, "target_exists")

    def test_new_document_never_overwrites_unrelated_files(self):
        self.write(b"not a skid document")
        self.assertConflict(self.base, None, "target_exists")

    def test_expected_revision_requires_existing_target(self):
        self.assertConflict(self.base, self.revision, "target_missing")

    def test_stale_expected_revision(self):
        self.save_base()
        first = edited(self.base, "-A")
        save_document(self.path, first, self.revision)
        self.assertConflict(edited(self.base, "-B"), self.revision, "revision_conflict")
        self.assertEqual(load_document(self.path), first)

    def test_expected_revision_must_match_exactly(self):
        self.save_base()
        self.assertConflict(edited(self.base, "-A"), self.revision + 1, "revision_conflict")

    def test_project_identity_must_match(self):
        self.save_base()
        other = edited(self.base, "-A")
        other["current"]["id"] += "-other"
        self.assertConflict(other, self.revision, "project_mismatch")

    def test_revision_must_not_regress(self):
        self.save_base()
        save_document(self.path, edited(self.base, "-A"), self.revision)
        self.assertConflict(self.base, self.revision + 1, "revision_regression")

    def test_changed_content_must_advance_revision(self):
        self.save_base()
        changed = copy.deepcopy(self.base)
        next(iter(changed["current"]["model"]["entities"].values()))["tag"] += "-A"
        self.assertConflict(changed, self.revision, "revision_not_advanced")

    def test_unverifiable_target_is_not_overwritten(self):
        other_schema = copy.deepcopy(self.base)
        other_schema["schema_version"] = 2
        contents = (
            b"{ corrupt",
            ('{"schema_version": 1, ' + json.dumps(self.base)[1:]).encode("utf-8"),
            json.dumps(other_schema).encode("utf-8"),
            b"[]",
            json.dumps({"schema_version": 1, "current": {}}).encode("utf-8"),
        )
        for data in contents:
            with self.subTest(data=data[:24]):
                self.write(data)
                with self.assertRaises(DocumentError):
                    save_document(self.path, edited(self.base, "-A"), self.revision)
                self.assertEqual(self.read(), data)
                self.assertEqual(self.leftovers(), [])


@unittest.skipIf(fcntl is None, "requires fcntl.flock")
class ConcurrencyTests(StorageTestCase):
    def hold_lock(self):
        fd = os.open(self.path + storage.LOCK_SUFFIX, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def release_lock(self, fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def attempt(self, document, expected):
        try:
            save_document(self.path, document, expected)
            return "saved"
        except Exception as exc:  # returned for assertions in the main thread
            return exc

    def test_competing_writers_cannot_both_commit(self):
        for round_index in range(5):
            with self.subTest(round=round_index):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                self.save_base()
                candidates = (edited(self.base, "-A"), edited(self.base, "-B"))
                barrier = threading.Barrier(len(candidates))
                outcomes = [None] * len(candidates)

                def write(index, candidates=candidates, barrier=barrier, outcomes=outcomes):
                    barrier.wait()
                    outcomes[index] = self.attempt(candidates[index], self.revision)

                threads = [threading.Thread(target=write, args=(index,)) for index in range(len(candidates))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(15)
                saved = [index for index, outcome in enumerate(outcomes) if outcome == "saved"]
                conflicts = [outcome for outcome in outcomes if isinstance(outcome, RevisionConflict)]
                self.assertEqual((len(saved), len(conflicts)), (1, 1), outcomes)
                self.assertEqual(load_document(self.path), candidates[saved[0]])
                self.assertEqual(self.leftovers(), [])

    def test_revision_is_checked_after_the_lock_is_acquired(self):
        original = self.save_base()
        mine = edited(self.base, "-A")
        theirs = edited(self.base, "-B")
        outcome = []
        fd = self.hold_lock()
        thread = threading.Thread(target=lambda: outcome.append(self.attempt(mine, self.revision)))
        thread.start()
        try:
            time.sleep(0.3)
            waited = thread.is_alive()
            untouched = self.read() == original
            # Another writer commits while this save is waiting for the lock.
            self.write(json.dumps(theirs, ensure_ascii=False, indent=2))
        finally:
            self.release_lock(fd)
            thread.join(15)
        self.assertTrue(waited, "save must wait for the lock")
        self.assertTrue(untouched)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], RevisionConflict)
        self.assertEqual(load_document(self.path), theirs)

    def test_lock_wait_times_out_without_writing(self):
        original = self.save_base()
        fd = self.hold_lock()
        try:
            with mock.patch.object(storage, "LOCK_TIMEOUT_S", 0.2):
                with self.assertRaises(DocumentError) as caught:
                    save_document(self.path, edited(self.base, "-A"), self.revision)
        finally:
            self.release_lock(fd)
        self.assertIn("lock_timeout", codes(caught.exception))
        self.assertNotIsInstance(caught.exception, RevisionConflict)
        self.assertEqual(self.read(), original)

    def test_lock_file_is_stable(self):
        self.save_base()
        lock = self.path + storage.LOCK_SUFFIX
        first = os.stat(lock)
        save_document(self.path, edited(self.base, "-A"), self.revision)
        second = os.stat(lock)
        self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))
        self.assertEqual(second.st_size, 0)
        self.assertEqual(sorted(os.listdir(self.dir)), ["skid.json", "skid.json.lock"])

    def test_reads_do_not_wait_for_the_lock(self):
        self.save_base()
        fd = self.hold_lock()
        try:
            self.assertEqual(load_document(self.path), self.base)
        finally:
            self.release_lock(fd)


class FailureRecoveryTests(StorageTestCase):
    def assertPreserved(self, original):
        self.assertEqual(self.read(), original)
        self.assertEqual(load_document(self.path), self.base)
        self.assertEqual(self.leftovers(), [])

    def test_failed_replace_preserves_previous_bytes(self):
        original = self.save_base()
        update = edited(self.base, "-A")
        with mock.patch("skid.core.storage.os.replace", side_effect=OSError(errno.EIO, "simulated I/O failure")):
            with self.assertRaises(OSError):
                save_document(self.path, update, self.revision)
        self.assertPreserved(original)
        save_document(self.path, update, self.revision)
        self.assertEqual(load_document(self.path), update)

    def test_failed_flush_preserves_previous_bytes(self):
        original = self.save_base()
        with mock.patch("skid.core.storage.os.fsync", side_effect=OSError(errno.ENOSPC, "simulated full disk")):
            with self.assertRaises(OSError):
                save_document(self.path, edited(self.base, "-A"), self.revision)
        self.assertPreserved(original)

    def test_interrupted_save_preserves_previous_bytes(self):
        original = self.save_base()
        with mock.patch("skid.core.storage.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                save_document(self.path, edited(self.base, "-A"), self.revision)
        self.assertPreserved(original)

    def test_failed_first_save_leaves_no_document(self):
        with mock.patch("skid.core.storage.os.replace", side_effect=OSError(errno.ENOSPC, "simulated full disk")):
            with self.assertRaises(OSError):
                save_document(self.path, self.base, None)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.leftovers(), [])
        save_document(self.path, self.base, None)
        self.assertEqual(load_document(self.path), self.base)

    def test_temporary_file_left_by_a_crash_is_ignored(self):
        original = self.save_base()
        stale = os.path.join(self.dir, ".skid.json.deadbeef.tmp")
        partial = original[: len(original) // 3]
        self.write(partial, stale)
        self.assertEqual(load_document(self.path), self.base)
        update = edited(self.base, "-A")
        save_document(self.path, update, self.revision)
        self.assertEqual(load_document(self.path), update)
        self.assertEqual(self.read(stale), partial)

    def test_file_mode_is_preserved(self):
        self.save_base()
        os.chmod(self.path, 0o640)
        save_document(self.path, edited(self.base, "-A"), self.revision)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
