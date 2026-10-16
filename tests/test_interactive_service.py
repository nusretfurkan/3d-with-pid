"""Interactive service: real core, real worker process, LABELED FAKE generators (``tests.test_interactive_fakes``).

These tests exercise command writing, history and persistence, build queueing,
cancellation, supersession, publication integrity and failure recovery. They do
not show that the contracted geometry/drafting generators work; see
``tests/test_interactive_real.py`` for that.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import threading
import time
import unittest
from pathlib import Path

from skid.core import apply_command, canonical_digest, load_document, save_document
from skid.interactive import integrity
from skid.interactive.service import InteractiveService, ServiceError, ServiceStartError, default_workflow_dir
from tests.test_interactive_fakes import (FAKE_SUITE, FIXTURE, command, make_service, new_project, open_gate,
                                          wait_at_gate, wait_terminal, wait_until)

ARTIFACTS = integrity.REQUIRED_ARTIFACTS


def external_edit(path, kind, payload):
    """A competing writer outside the service: the core plus atomic storage, as the CLI does."""
    document = load_document(str(path))
    revision = document["current"]["revision"]
    new, _ = apply_command(document, {"id": f"cmd-external-{revision}", "expected_revision": revision, "type": kind,
                                      "payload": payload})
    save_document(str(path), new, revision)
    return new


def pointer_bytes(service):
    path = Path(service.root, "current.json")
    return path.read_bytes() if path.exists() else None


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)

    def service(self, **options):
        return make_service(self, self.path, **options)

    def publish_initial(self, service):
        service.request_build("test")
        state = wait_terminal(service)
        self.assertEqual(state["build"]["status"], "complete", state["build"]["diagnostics"])
        return state

    def assertEventsOrdered(self, build):
        sequences = [e["sequence"] for e in build["events"]]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        self.assertEqual(build["last_sequence"], sequences[-1])
        worker = [e["worker_sequence"] for e in build["events"] if e["source"] == "worker"]
        self.assertEqual(worker, list(range(1, len(worker) + 1)))
        for event in build["events"]:
            self.assertEqual((event["build_id"], event["source_revision"], event["input_digest"]),
                             (build["id"], build["source_revision"], build["input_digest"]))


class StartupTests(ServiceCase):
    def test_useful_state_before_the_first_build(self):
        service = self.service()
        state = service.state()
        for key in ("project", "diagnostics", "selection", "build", "published", "artifacts", "history"):
            self.assertIn(key, state)
        self.assertIsNone(state["build"])
        self.assertIsNone(state["published"])
        self.assertEqual(state["artifacts"], {})
        self.assertEqual(state["project"]["revision"], 0)
        self.assertEqual((state["history"]["undo"], state["history"]["redo"]), (0, 0))
        self.assertEqual(state["selection"]["sequence"], 0)
        self.assertEqual(state["sync"]["state"], "unpublished")
        self.assertEqual(state["service"]["suite"], FAKE_SUITE)
        self.assertEqual(Path(service.root), Path(default_workflow_dir(self.path)))
        json.dumps(state, allow_nan=False)

    def test_refuses_missing_projects_examples_and_foreign_roots(self):
        with self.assertRaises(ServiceStartError):
            InteractiveService(str(self.path.with_name("absent.json")), suite=FAKE_SUITE)
        self.assertFalse(self.path.with_name("absent.json").exists())
        with self.assertRaisesRegex(ServiceStartError, "repository example"):
            InteractiveService(str(FIXTURE), suite=FAKE_SUITE)
        foreign = self.path.parent / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("user data")
        with self.assertRaisesRegex(ServiceStartError, "not a workflow root"):
            InteractiveService(str(self.path), str(foreign), suite=FAKE_SUITE)
        self.assertEqual(sorted(os.listdir(foreign)), ["keep.txt"])

    def test_one_service_per_workflow_root_and_project_identity(self):
        self.service()
        with self.assertRaisesRegex(ServiceStartError, "already using"):
            InteractiveService(str(self.path), suite=FAKE_SUITE)
        other = new_project(self, "other.json")
        document = load_document(str(other))
        document["current"]["id"] = "project-other"
        other.unlink()
        save_document(str(other), document, None)
        with self.assertRaisesRegex(ServiceStartError, "belongs to project"):
            InteractiveService(str(other), default_workflow_dir(self.path), suite=FAKE_SUITE)

    def test_opt_in_retention_keeps_published_active_and_unsealed_builds(self):
        service = self.service(keep_builds=1)
        interrupted = Path(service.builds_root, "b20000101T000000-r0-0000abcd")
        interrupted.mkdir()
        (interrupted / "snapshot.json").write_text("{}")
        names = []
        for tag in ("P-901", "P-902", "P-903"):
            command(service, "rename_entity", {"entity_id": "ent-pump", "tag": tag})
            names.append(wait_terminal(service)["build"]["id"])
        deadline = time.monotonic() + 10  # pruning follows the build's terminal state outside the locks
        while Path(service.builds_root, names[0]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        remaining = sorted(os.listdir(service.builds_root))
        self.assertIn(interrupted.name, remaining)  # unsealed directories are evidence, never pruned
        self.assertIn(names[-1], remaining)  # published and current
        self.assertIn(names[-2], remaining)  # the newest other sealed build
        self.assertNotIn(names[0], remaining)
        self.assertEqual(service.state()["published"]["build_id"], names[-1])
        with self.assertRaises(ServiceStartError):
            InteractiveService(str(self.path), suite=FAKE_SUITE, keep_builds=-1)

    def test_offline_bundles_are_never_touched(self):
        offline = self.path.with_suffix(".skid")
        offline.mkdir()
        (offline / "current.json").write_text("offline pointer")
        service = self.service()
        self.publish_initial(service)
        command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        wait_terminal(service)
        self.assertEqual(sorted(os.listdir(offline)), ["current.json"])
        self.assertEqual((offline / "current.json").read_text(), "offline pointer")


class PublicationTests(ServiceCase):
    def test_complete_build_publishes_all_five_with_one_identity(self):
        service = self.service()
        state = self.publish_initial(service)
        build, published = state["build"], state["published"]
        digest = canonical_digest(load_document(str(self.path))["current"])
        self.assertEqual((published["build_id"], published["source_revision"], published["input_digest"]),
                         (build["id"], 0, digest))
        self.assertEqual(published["status"], "complete")
        self.assertEqual(set(published["artifacts"]), {"display", "cad", "pid", "legend", "description", "index",
                                                       "inventory", "recipe", "verification"})
        for url in published["artifacts"].values():
            build_id, relpath = url[len("/artifacts/"):].split("/", 1)
            self.assertIsNotNone(service.artifact_path(build_id, relpath), url)
        self.assertEqual(build["artifact_status"], dict.fromkeys(ARTIFACTS, "complete"))
        self.assertEqual(state["sync"]["state"], "synchronized")
        self.assertEventsOrdered(build)
        kinds = [(e["kind"], e["artifact"]) for e in build["events"] if e["source"] == "worker"]
        self.assertLess(kinds.index(("preview", "display")), kinds.index(("artifact", "display")))
        self.assertLess(kinds.index(("preview", "pid")), kinds.index(("artifact", "pid")))
        for event in build["events"]:
            if "url" in event:
                build_id, relpath = event["url"][len("/artifacts/"):].split("/", 1)
                self.assertIsNotNone(service.artifact_path(build_id, relpath))
        pointer = json.loads(pointer_bytes(service))
        self.assertEqual(integrity.verify_pointer(service.root, pointer), [])
        seal = json.loads(Path(service.builds_root, build["id"], "build.json").read_text())
        self.assertEqual(seal["status"], "complete")
        self.assertEqual(state["artifact_sources"]["display"]["origin"], "published")

    def test_state_snapshots_never_straddle_a_publication(self):
        service = self.service()
        inconsistent, samples, stop = [], [0], threading.Event()

        def poll():
            while not stop.is_set():
                state = service.state()
                samples[0] += 1
                build, published = state["build"], state["published"]
                if build and build["published"] and (published is None or published["build_id"] != build["id"]):
                    inconsistent.append((build["id"], published and published["build_id"]))
                if published and build and published["build_id"] == build["id"] and build["status"] != "complete":
                    inconsistent.append(("published while", build["status"]))

        poller = threading.Thread(target=poll)
        poller.start()
        try:
            for tag in ("P-801", "P-802", "P-803", "P-804"):
                command(service, "rename_entity", {"entity_id": "ent-pump", "tag": tag})
                wait_terminal(service)
        finally:
            stop.set()
            poller.join()
        self.assertGreater(samples[0], 20)
        self.assertEqual(inconsistent, [])
        self.assertEqual(service.state()["published"]["source_revision"], 4)

    def test_unchanged_rebuild_request_is_not_repeated(self):
        service = self.service()
        first = self.publish_initial(service)
        state = service.build({})
        self.assertEqual(state["build"]["id"], first["build"]["id"])
        self.assertIn("build_not_needed", {d["code"] for d in state["diagnostics"]})
        forced = service.build({"force": True})
        self.assertNotEqual(forced["build"]["id"], first["build"]["id"])
        state = wait_terminal(service)
        self.assertEqual(state["published"]["source_revision"], 0)
        self.assertEqual(state["published"]["build_id"], forced["build"]["id"])
        with self.assertRaises(ServiceError) as caught:
            service.build({"expected_revision": 5})
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ServiceError):
            service.build({"unexpected": 1})

    def test_failure_of_each_artifact_after_the_others_succeed_keeps_the_pointer(self):
        service = self.service()
        published = self.publish_initial(service)["published"]
        before = pointer_bytes(service)
        service.close()
        for artifact, mode in (("display", "raise"), ("cad", "status_failed"), ("pid", "error_diagnostic"),
                               ("legend", "raise"), ("description", "status_failed")):
            with self.subTest(artifact=artifact):
                failing = make_service(self, self.path, fail={artifact: mode}, after_other=True)
                self.addCleanup(failing.close)
                revision = failing.state()["project"]["revision"]
                command(failing, "rename_entity", {"entity_id": "ent-pump", "tag": f"P-{300 + revision}"})
                state = wait_terminal(failing)
                build = state["build"]
                self.assertEqual(build["status"], "failed")
                self.assertEqual(build["artifact_status"][artifact], "failed")
                if mode != "raise":  # a raising generator fails both of its artifacts
                    for other in ARTIFACTS:
                        if other != artifact:
                            self.assertEqual(build["artifact_status"][other], "complete", other)
                self.assertEqual(pointer_bytes(failing), before)
                self.assertEqual(state["published"]["build_id"], published["build_id"])
                self.assertEqual(state["published"]["source_revision"], 0)
                self.assertEqual(state["sync"]["state"], "failed")
                failing.close()  # before assertions, so one failed subtest cannot block the next service
                self.assertFalse(build["published"])
                self.assertNotIn(artifact, build["artifacts"])
                label = integrity.LABELS[artifact].lower()
                self.assertTrue(any(label in d["message"].lower() or "fault" in d["code"]
                                    for d in build["diagnostics"]), build["diagnostics"])
                seal = json.loads(Path(failing.builds_root, build["id"], "build.json").read_text())
                self.assertEqual(seal["status"], "failed")

    def test_manifest_and_protocol_faults_are_never_published(self):
        cases = [
            {"display": "bad_event", "cad": "missing_file", "pid": "wrong_revision", "legend": "mutate_preview",
             "description": "manifest_mismatch"},
            {"display": "mutate_input", "pid": "interrupt_without_cancel"},
            {"display": "wrong_digest", "legend": "svg_script", "description": "html_script"},
        ]
        expected_codes = {"bad_event": "event_invalid", "missing_file": "artifact_missing",
                          "wrong_revision": "manifest_mismatch", "mutate_preview": "preview_changed",
                          "manifest_mismatch": "manifest_mismatch", "mutate_input": "input_mutated",
                          "interrupt_without_cancel": "generator_fault", "wrong_digest": "manifest_mismatch",
                          "svg_script": "artifact_content", "html_script": "artifact_content"}
        for fail in cases:
            with self.subTest(fail=fail):
                service = make_service(self, self.path, fail=fail)
                service.request_build("fault injection", force=True)
                state = wait_terminal(service)
                service.close()
                build = state["build"]
                self.assertEqual(build["status"], "failed")
                self.assertIsNone(state["published"])
                self.assertIsNone(pointer_bytes(service))
                codes = {d["code"] for d in build["diagnostics"]}
                for artifact, mode in fail.items():
                    self.assertEqual(build["artifact_status"][artifact], "failed", artifact)
                    self.assertIn(expected_codes[mode], codes, (artifact, mode, codes))
                    if mode in ("svg_script", "html_script"):
                        self.assertNotIn(artifact, build["artifacts"])

    def test_pending_outputs_stay_working_previews(self):
        service = self.service()
        published = self.publish_initial(service)["published"]
        placement = dict(load_document(str(self.path))["current"]["physical"]["placements"]["ent-pump"])
        placement["position_m"] = [1.6, 0.0, 0.0]
        command(service, "move_equipment", {"entity_id": "ent-pump", "placement": placement})
        state = wait_terminal(service)
        build = state["build"]
        self.assertEqual(build["status"], "pending")
        self.assertEqual(state["published"]["build_id"], published["build_id"])
        self.assertEqual(state["sync"]["state"], "pending")
        self.assertEqual(state["artifact_sources"]["display"]["origin"], "working")
        self.assertEqual(state["artifact_sources"]["display"]["build_status"], "pending")
        self.assertTrue(state["artifacts"]["display"].startswith(f"/artifacts/{build['id']}/"))
        self.assertTrue(any(d["code"] == "route_invalidated" or d["severity"] == "warning"
                            for d in state["diagnostics"]))

    def test_source_changed_before_publication_is_superseded(self):
        service = self.service()
        service.before_publish = lambda record: external_edit(self.path, "rename_entity",
                                                               {"entity_id": "ent-tank", "tag": "TK-900"})
        service.request_build("test")
        record_id = service.state()["build"]["id"]
        wait_terminal(service, record_id)
        service.before_publish = None
        record = service._builds[record_id]
        self.assertEqual(record.status, "superseded")
        self.assertIn("source_changed", {d["code"] for d in record.diagnostics})
        self.assertIsNone(pointer_bytes(service))
        state = wait_terminal(service)  # the adopted external revision builds and publishes
        self.assertEqual(state["project"]["revision"], 1)
        self.assertEqual(state["published"]["source_revision"], 1)
        self.assertIn("external_change", {d["code"] for d in state["diagnostics"]})

    def test_only_the_verified_publication_is_offered(self):
        service = self.service()
        first = self.publish_initial(service)["published"]
        first_pointer = pointer_bytes(service)
        command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        second = wait_terminal(service)["build"]
        self.assertEqual(second["status"], "complete")
        # An independently valid previous set: the pointer now names revision 0 again.
        Path(service.root, "current.json").write_bytes(first_pointer)
        state = service.state()
        self.assertEqual(state["published"]["build_id"], first["build_id"])
        self.assertEqual(state["build"]["id"], second["id"])
        self.assertEqual(state["build"]["integrity"], "rejected")
        self.assertFalse(state["build"]["published"])
        self.assertEqual(set(state["artifacts"]), set(state["published"]["artifacts"]))
        for key, url in state["artifacts"].items():
            self.assertTrue(url.startswith(f"/artifacts/{first['build_id']}/"), key)
            self.assertEqual((state["artifact_sources"][key]["origin"], state["artifact_sources"][key]["build_id"],
                              state["artifact_sources"][key]["source_revision"]), ("published", first["build_id"], 0))
        self.assertEqual(state["sync"]["state"], "stale")

    def test_published_corruption_is_detected_and_reported(self):
        service = self.service()
        published = self.publish_initial(service)["published"]
        before = pointer_bytes(service)
        scene = Path(service.builds_root, published["build_id"], "scene.json")
        original = scene.read_bytes()
        scene.write_bytes(original + b" ")
        service.select({"semantic_ids": ["ent-pump"], "origin": "pid"})
        state = service.state()
        self.assertIsNone(state["published"])
        self.assertIn("published_build_file_changed", {d["code"] for d in state["diagnostics"]})
        self.assertEqual(state["sync"]["state"], "invalid")
        self.assertEqual(pointer_bytes(service), before)  # reported, never rewritten or deleted
        # The rejected set is not offered under any label, and its build no longer presents as published.
        self.assertEqual((state["artifacts"], state["artifact_sources"]), ({}, {}))
        build = state["build"]
        self.assertEqual(build["id"], published["build_id"])
        self.assertEqual((build["integrity"], build["published"], build["artifacts"], build["previews"]),
                         ("rejected", False, {}, {}))
        self.assertEqual(build["artifact_status"], dict.fromkeys(ARTIFACTS, "rejected"))
        resolved = state["selection"]["resolved"][0]
        self.assertIsNone(resolved["display_components"])
        self.assertNotIn("absent_3d_reason", resolved)
        scene.write_bytes(original)
        state = service.state()
        self.assertEqual(state["published"]["build_id"], published["build_id"])
        self.assertEqual(state["build"]["integrity"], "verified")
        self.assertEqual(state["artifact_sources"]["display"]["origin"], "published")
        self.assertEqual(state["selection"]["resolved"][0]["display_components"], ["fake:display:ent-pump"])
        self.assertEqual(state["sync"]["state"], "synchronized")
        Path(service.root, "current.json").write_text("{not json")
        state = service.state()
        self.assertIsNone(state["published"])
        self.assertIn("published_pointer_unreadable", {d["code"] for d in state["diagnostics"]})
        Path(service.root, "current.json").write_bytes(before)
        self.assertIsNotNone(service.state()["published"])


class CommandTests(ServiceCase):
    def test_rename_autobuilds_and_undo_redo_follow_revisions(self):
        service = self.service()
        self.publish_initial(service)
        state = command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        self.assertEqual(state["project"]["revision"], 1)
        self.assertIn(state["build"]["status"], ("queued", "running"))
        self.assertEqual(state["build"]["source_revision"], 1)
        self.assertEqual(state["sync"]["state"], "building")
        self.assertEqual(state["published"]["source_revision"], 0)  # labelled stale by revision, still verified
        self.assertEqual(state["last_command"]["status"], "accepted")
        state = wait_terminal(service)
        self.assertEqual(state["published"]["source_revision"], 1)
        state = command(service, "undo", {})
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-101")
        self.assertEqual((state["history"]["undo"], state["history"]["redo"]), (0, 1))
        command(service, "redo", {})
        state = wait_terminal(service)
        self.assertEqual(state["project"]["revision"], 3)
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        self.assertEqual(state["published"]["source_revision"], 3)
        on_disk = load_document(str(self.path))
        self.assertEqual(on_disk["current"]["revision"], 3)
        self.assertEqual((len(on_disk["undo"]), len(on_disk["redo"])), (1, 0))
        self.assertEqual([c["to_revision"] for c in state["history"]["recent"]], [1, 2, 3])

    def test_rejected_commands_preserve_document_and_start_nothing(self):
        service = self.service()
        before = self.path.read_bytes()
        cases = [({"entity_id": "ent-pump", "tag": "TK-101"}, "rename_entity", 400, "duplicate_tag"),
                 ({"entity_id": "ent-missing", "tag": "X"}, "rename_entity", 400, "unknown_reference"),
                 ({"port_id": "port-pump-inlet", "nominal_diameter_mm": 80, "assumption_id": "asm-dn50-pipe"},
                  "set_port_size", 400, None)]
        for payload, kind, status, code in cases:
            with self.assertRaises(ServiceError) as caught:
                command(service, kind, payload)
            self.assertEqual(caught.exception.status, status)
            if code:
                self.assertIn(code, {d["code"] for d in caught.exception.diagnostics})
        with self.assertRaises(ServiceError) as caught:
            command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-9"}, revision=4)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ServiceError) as caught:
            service.command({"type": "rename_entity"})
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ServiceError):
            service.command(["not", "an", "object"])
        self.assertEqual(self.path.read_bytes(), before)
        state = service.state()
        self.assertIsNone(state["build"])
        self.assertEqual(state["project"]["revision"], 0)

    def test_no_op_command_neither_saves_nor_builds(self):
        service = self.service()
        before = self.path.read_bytes()
        symbol = load_document(str(self.path))["current"]["pid"]["symbols"]["sym-pump"]
        state = command(service, "move_symbol", {"symbol_id": "sym-pump", "x": symbol["x"], "y": symbol["y"],
                                                 "rotation_deg": symbol["rotation_deg"]})
        self.assertEqual(state["last_command"]["status"], "no_op")
        self.assertIsNone(state["build"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_competing_external_writer_is_a_conflict_not_a_rebase(self):
        service = self.service()
        external_edit(self.path, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-777"})
        with self.assertRaises(ServiceError) as caught:
            command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-777"}, revision=0)
        self.assertEqual(caught.exception.status, 409)
        state = service.state()
        self.assertEqual(state["project"]["revision"], 1)
        self.assertEqual(state["project"]["model"]["entities"]["ent-tank"]["tag"], "TK-777")
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-101")
        on_disk = load_document(str(self.path))["current"]
        self.assertEqual(on_disk["model"]["entities"]["ent-pump"]["tag"], "P-101")
        state = command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-777"}, revision=1)
        self.assertEqual(state["project"]["revision"], 2)

    def test_external_write_racing_the_save_is_rejected(self):
        service = self.service()
        original_apply = apply_command

        def racing_apply(document, envelope):
            result = original_apply(document, envelope)
            external_edit(self.path, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-555"})
            return result

        import skid.interactive.service as service_module
        service_module.apply_command = racing_apply
        try:
            with self.assertRaises(ServiceError) as caught:
                command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-555"}, revision=0)
        finally:
            service_module.apply_command = original_apply
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("revision_conflict", {d["code"] for d in caught.exception.diagnostics})
        on_disk = load_document(str(self.path))["current"]
        self.assertEqual(on_disk["model"]["entities"]["ent-tank"]["tag"], "TK-555")
        self.assertEqual(on_disk["model"]["entities"]["ent-pump"]["tag"], "P-101")
        self.assertEqual(service.state()["project"]["revision"], 1)

    def test_two_writers_against_the_same_revision(self):
        service = self.service()
        results = []

        def submit(tag):
            try:
                command(service, "rename_entity", {"entity_id": "ent-pump", "tag": tag}, revision=0)
                results.append("accepted")
            except ServiceError as exc:
                results.append(exc.status)

        threads = [threading.Thread(target=submit, args=(f"P-{n}",)) for n in (401, 402)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(map(str, results)), ["409", "accepted"])
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 1)

    def test_save_reopen_and_restart_preserve_history_and_manual_work(self):
        service = self.service()
        command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        # The tank's edges are guided, so they are regenerated and the revision stays complete.
        command(service, "move_symbol", {"symbol_id": "sym-tank", "x": 100.0, "y": 280.0, "rotation_deg": 0.0})
        state = wait_terminal(service)
        self.assertEqual(state["build"]["status"], "complete", state["build"]["diagnostics"])
        state = service.save({"expected_revision": 2})
        self.assertEqual(state["document"]["saved"]["revision"], 2)
        with self.assertRaises(ServiceError) as caught:
            service.save({"expected_revision": 1})
        self.assertEqual(caught.exception.status, 409)
        published = state["published"]
        service.close()
        reopened = self.service()
        state = reopened.state()
        self.assertEqual(state["project"]["revision"], 2)
        self.assertEqual((state["history"]["undo"], state["history"]["redo"]), (2, 0))
        self.assertTrue(state["project"]["pid"]["symbols"]["sym-tank"]["manual"])
        self.assertEqual(state["published"]["build_id"], published["build_id"])
        self.assertEqual(state["sync"]["state"], "synchronized")
        state = reopened.reopen({})
        self.assertIn("reopened", {d["code"] for d in state["diagnostics"]})
        self.assertIsNone(state["build"])  # unchanged and already published: nothing to build
        state = command(reopened, "undo", {})
        self.assertEqual(state["project"]["pid"]["symbols"]["sym-tank"]["x"], 120.0)
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        with self.assertRaises(ServiceError):
            reopened.reopen({"path": "/etc/passwd"})

    def test_invalid_file_on_disk_blocks_writes_until_repaired(self):
        service = self.service()
        good = self.path.read_bytes()
        self.path.write_text("{broken")
        with self.assertRaises(ServiceError) as caught:
            service.reopen({})
        self.assertEqual(caught.exception.status, 409)
        state = service.state()
        self.assertEqual(state["project"]["revision"], 0)
        self.assertIn("disk_invalid", {d["code"] for d in state["diagnostics"]})
        with self.assertRaises(ServiceError) as caught:
            command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-2"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.path.read_text(), "{broken")
        self.path.write_bytes(good)
        state = service.reopen({})
        self.assertTrue(state["document"]["disk_valid"])


class BuildControlTests(ServiceCase):
    def gate(self):
        return str(self.path.parent / "gate.txt")

    def wait_preview(self, service, build_id):
        return wait_until(service, lambda s: s["build"]["id"] == build_id and s["build"]["previews"],
                          what="a progressive preview")

    def test_cancel_terminates_worker_and_never_promotes_output(self):
        service = self.service(gate=self.gate())
        service.request_build("test")
        build_id = service.state()["build"]["id"]
        state = self.wait_preview(service, build_id)
        process = service._builds[build_id].process
        self.assertIsNone(process.poll())
        state = service.cancel({"build_id": build_id})
        self.assertEqual(state["build"]["status"], "cancelled")
        state = wait_terminal(service, build_id)
        self.assertIsNotNone(process.poll())
        build = state["build"]
        self.assertEqual(build["status"], "cancelled")
        self.assertTrue(build["previews"])
        self.assertEqual(build["artifacts"], {})
        self.assertIsNone(state["published"])
        self.assertIsNone(pointer_bytes(service))
        self.assertEqual(state["artifact_sources"]["display"]["build_status"], "cancelled")
        self.assertEqual(state["artifact_sources"]["display"]["kind"], "preview")
        seal = json.loads(Path(service.builds_root, build_id, "build.json").read_text())
        self.assertEqual(seal["status"], "cancelled")
        self.assertEventsOrdered(build)
        for body, status in (({"build_id": build_id}, 409), ({"build_id": "b-unknown"}, 409), ({}, 400)):
            with self.assertRaises(ServiceError) as caught:
                service.cancel(body)
            self.assertEqual(caught.exception.status, status)
        open_gate(self.gate())
        state = service.build({})
        state = wait_terminal(service)
        self.assertEqual(state["build"]["status"], "complete")

    def test_cancel_escalates_to_kill_when_the_generator_ignores_it(self):
        service = self.service(gate=self.gate(), ignore_cancel=True, terminate_grace=0.3)
        service.request_build("test")
        build_id = service.state()["build"]["id"]
        # A first preview is not enough: a loaded machine can deliver it while both generators are still in
        # their cooperative preview loops, which then honour SIGTERM (exit 3). Cancel only once both announced
        # that they are inside the gate loop, where ignore_cancel makes them disregard cancellation.
        wait_at_gate(service, build_id)
        process = service._builds[build_id].process
        self.assertIsNone(process.poll())
        started = time.monotonic()
        service.cancel({"build_id": build_id})
        state = wait_terminal(service, build_id, timeout=15)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(state["build"]["status"], "cancelled")
        self.assertIsNone(pointer_bytes(service))

    def test_new_commit_supersedes_and_coalesces(self):
        service = self.service(gate=self.gate())
        service.request_build("test")
        first = service.state()["build"]["id"]
        self.wait_preview(service, first)
        for tag in ("P-601", "P-602", "P-603"):
            command(service, "rename_entity", {"entity_id": "ent-pump", "tag": tag})
        state = service.state()
        self.assertEqual(state["build"]["source_revision"], 3)
        self.assertEqual(service._builds[first].status, "superseded")
        open_gate(self.gate())
        state = wait_terminal(service)
        self.assertEqual(state["build"]["status"], "complete")
        self.assertEqual(state["published"]["source_revision"], 3)
        records = list(service._builds.values())
        self.assertEqual([r.status for r in records].count("complete"), 1)
        for record in records[:-1]:
            self.assertEqual(record.status, "superseded")
            self.assertFalse(record.published)
        intervals = []
        for record in records:
            if record.started_at:
                intervals.append((datetime.datetime.fromisoformat(record.started_at),
                                  datetime.datetime.fromisoformat(record.finished_at)))
        intervals.sort()
        for (_, end), (start, _) in zip(intervals, intervals[1:]):
            self.assertLessEqual(end, start)  # never two workers at once
        self.assertLessEqual(len(intervals), 4)

    def test_cancelling_a_queued_build(self):
        service = self.service(gate=self.gate(), ignore_cancel=True, terminate_grace=1.0)
        service.request_build("test")
        first = service.state()["build"]["id"]
        wait_at_gate(service, first)  # the superseded worker must outlive the request, so it may not cooperate
        state = command(service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-701"})
        queued = state["build"]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(state["active_build"]["id"], first)
        state = service.cancel({"build_id": queued["id"]})
        self.assertEqual(state["build"]["status"], "cancelled")
        wait_until(service, lambda s: service.active is None, timeout=15, what="the superseded worker to exit")
        self.assertEqual(service._builds[queued["id"]].status, "cancelled")
        self.assertIsNone(service._builds[queued["id"]].process)

    def test_worker_crash_and_timeout_fail_truthfully(self):
        service = self.service(gate=self.gate())
        service.request_build("test")
        build_id = service.state()["build"]["id"]
        self.wait_preview(service, build_id)
        os.killpg(service._builds[build_id].process.pid, signal.SIGKILL)
        state = wait_terminal(service, build_id)
        self.assertEqual(state["build"]["status"], "failed")
        self.assertIn("worker_crashed", {d["code"] for d in state["build"]["diagnostics"]})
        self.assertIsNone(pointer_bytes(service))
        service.close()
        slow = self.service(gate=self.gate(), timeout=1.0)
        slow.request_build("test")
        state = wait_terminal(slow, timeout=20)
        self.assertEqual(state["build"]["status"], "failed")
        self.assertIn("worker_timeout", {d["code"] for d in state["build"]["diagnostics"]})

    def test_restart_after_interrupted_service_reports_unsealed_builds(self):
        service = self.service(gate=self.gate(), ignore_cancel=True)
        service.request_build("test")
        build_id = service.state()["build"]["id"]
        self.wait_preview(service, build_id)
        process = service._builds[build_id].process
        os.killpg(process.pid, signal.SIGKILL)  # stands in for a service crash: the worker dies, nothing seals
        process.wait()
        seal = Path(service.builds_root, build_id, "build.json")
        wait_terminal(service, build_id)
        seal.unlink()
        service.close()
        restarted = self.service()
        state = restarted.state()
        self.assertIn(build_id, state["service"]["interrupted_builds"])
        self.assertIn("interrupted_builds", {d["code"] for d in state["diagnostics"]})
        self.assertIsNone(state["published"])

    def test_events_endpoint_recovers_missed_events_in_order(self):
        service = self.service()
        service.request_build("test")
        state = wait_terminal(service)
        build = state["build"]
        tail = service.events(build["id"], 5)
        self.assertEqual([e["sequence"] for e in tail["events"]], list(range(6, build["last_sequence"] + 1)))
        self.assertEqual(service.events(build["id"], build["last_sequence"])["events"], [])
        with self.assertRaises(ServiceError) as caught:
            service.events("b20990101T000000-r0-00000000", 0)
        self.assertEqual(caught.exception.status, 404)
        log = Path(service.builds_root, build["id"], "events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["sequence"] for line in log], [e["sequence"] for e in build["events"]])


class NotesTests(ServiceCase):
    def test_manual_notes_are_passed_through_verbatim_and_tracked(self):
        notes_path = self.path.with_name("pump skid.notes.json")
        notes = {"notes": [{"id": "note-1", "target_id": "ent-ghost", "text": "Keep this orphan text exactly."}]}
        notes_path.write_text(json.dumps(notes))
        service = self.service()
        state = self.publish_initial(service)
        build_dir = Path(service.builds_root, state["build"]["id"])
        description = json.loads((build_dir / "description.json").read_text())
        self.assertEqual(description["manual_notes"], notes)
        self.assertEqual((build_dir / "manual-notes.json").read_bytes(), notes_path.read_bytes())
        self.assertEqual(json.loads(notes_path.read_text()), notes)  # the service never rewrites the sidecar
        notes_path.write_text(json.dumps({"notes": []}))
        state = service.state()
        self.assertEqual(state["sync"]["state"], "stale")
        state = service.build({})
        self.assertNotEqual(state["build"]["id"], state["published"]["build_id"])
        self.assertEqual(wait_terminal(service)["sync"]["state"], "synchronized")

    def notes_path(self):
        return self.path.with_name("pump skid.notes.json")

    def assertNotesUnreadable(self, state):
        notes = state["document"]["notes"]
        self.assertEqual((notes["present"], notes["readable"], notes["sha256"]), (True, False, None))
        self.assertTrue(notes["error"])
        self.assertIn("manual_notes_unreadable", {d["code"] for d in state["diagnostics"]})
        self.assertNotEqual(state["sync"]["state"], "synchronized")

    def assertDraftingRefused(self, build):
        self.assertEqual(build["status"], "failed")
        self.assertEqual({a: build["artifact_status"][a] for a in ARTIFACTS},
                         {"display": "complete", "cad": "complete", "pid": "failed", "legend": "failed",
                          "description": "failed"})
        self.assertIn("manual_notes_unreadable", {d["code"] for d in build["diagnostics"]})
        self.assertFalse(build["published"])
        build_dir = Path(self.service_root, "builds", build["id"])
        self.assertFalse((build_dir / "manual-notes.json").exists())
        request = json.loads((build_dir / "request.json").read_text())
        self.assertFalse(request["manual_notes"])
        self.assertTrue(request["manual_notes_error"])
        self.assertEqual(json.loads((build_dir / "build.json").read_text())["status"], "failed")

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_permission_denied_notes_after_publication_block_publication(self):
        notes = self.notes_path()
        notes.write_text(json.dumps({"notes": [{"id": "note-1", "target_id": "ent-pump", "text": "Keep."}]}))
        self.addCleanup(os.chmod, notes, 0o644)
        service = self.service()
        self.service_root = service.root
        published = self.publish_initial(service)["published"]
        self.assertEqual(service.state()["sync"]["state"], "synchronized")
        before = pointer_bytes(service)
        os.chmod(notes, 0)
        state = service.state()
        self.assertNotesUnreadable(state)
        self.assertEqual(state["sync"]["state"], "blocked")
        self.assertEqual(state["published"]["build_id"], published["build_id"])  # the published set itself verifies
        self.assertFalse(service._is_current(service._published_info()[0]))
        state = service.build({})
        self.assertNotIn("build_not_needed", {d["code"] for d in state["diagnostics"]})
        state = wait_terminal(service)
        self.assertDraftingRefused(state["build"])
        self.assertEqual(pointer_bytes(service), before)
        self.assertEqual(state["sync"]["state"], "failed")
        os.chmod(notes, 0o644)
        self.assertEqual(service.state()["sync"]["state"], "synchronized")  # the original notes are back

    def test_unreadable_notes_at_startup_never_count_as_absent(self):
        service = self.service()
        published = self.publish_initial(service)["published"]  # built with no sidecar
        before = pointer_bytes(service)
        service.close()
        notes = self.notes_path()
        notes.mkdir()  # exists, but reading it raises IsADirectoryError (independent of privileges)
        restarted = self.service()
        self.service_root = restarted.root
        state = restarted.state()
        self.assertNotesUnreadable(state)
        self.assertEqual(state["sync"]["state"], "blocked")
        self.assertEqual(state["published"]["build_id"], published["build_id"])
        self.assertFalse(restarted._is_current(restarted._published_info()[0]))  # None == None must not match
        restarted.request_build("test")
        self.assertDraftingRefused(wait_terminal(restarted)["build"])
        self.assertEqual(pointer_bytes(restarted), before)
        notes.rmdir()
        os.symlink(self.path.parent / "missing-target.json", notes)  # a dangling link is not an absent file
        self.assertNotesUnreadable(restarted.state())
        notes.unlink()
        state = restarted.state()
        self.assertEqual((state["document"]["notes"]["present"], state["document"]["notes"]["readable"]), (False, None))
        self.assertEqual(state["sync"]["state"], "synchronized")

    def test_notes_becoming_unreadable_during_publication_refuse_it(self):
        notes = self.notes_path()
        notes.write_text(json.dumps({"notes": []}))
        service = self.service()
        self.service_root = service.root

        def break_notes(record):
            notes.unlink()
            notes.mkdir()

        service.before_publish = break_notes
        service.request_build("test")
        build_id = service.state()["build"]["id"]
        state = wait_terminal(service, build_id)
        service.before_publish = None
        build = state["build"]
        self.assertEqual(build["status"], "failed")
        self.assertIn("manual_notes_unreadable", {d["code"] for d in build["diagnostics"]})
        self.assertIsNone(pointer_bytes(service))
        self.assertIsNone(state["published"])
        self.assertEqual(json.loads(Path(service.builds_root, build_id, "build.json").read_text())["status"], "failed")
        self.assertNotesUnreadable(state)

    def test_unreadable_notes_fail_only_the_drafting_outputs(self):
        self.path.with_name("pump skid.notes.json").write_text("{not json")
        service = self.service()
        service.request_build("test")
        build = wait_terminal(service)["build"]
        self.assertEqual(build["status"], "failed")
        self.assertEqual({a: build["artifact_status"][a] for a in ("display", "cad")},
                         {"display": "complete", "cad": "complete"})
        for artifact in ("pid", "legend", "description"):
            self.assertEqual(build["artifact_status"][artifact], "failed")
        self.assertIn("manual_notes_invalid", {d["code"] for d in build["diagnostics"]})


class SelectionAndProposalTests(ServiceCase):
    def test_selection_maps_all_occurrences_and_suppresses_echo(self):
        service = self.service()
        self.publish_initial(service)
        state = service.select({"semantic_ids": ["ent-pump", "conn-suction-2", "port-pump-outlet"], "origin": "pid"})
        selection = state["selection"]
        self.assertEqual((selection["sequence"], selection["origin"]), (1, "pid"))
        self.assertEqual(state["project"]["revision"], 0)
        resolved = {item["id"]: item for item in selection["resolved"]}
        self.assertEqual(resolved["ent-pump"]["pid_occurrences"], ["sym-pump"])
        self.assertEqual(resolved["ent-pump"]["display_components"], ["fake:display:ent-pump"])
        self.assertEqual(resolved["conn-suction-2"]["pid_occurrences"], ["edge-suction-2"])
        self.assertEqual(resolved["port-pump-outlet"]["kind"], "port")
        echoed = service.select({"semantic_ids": ["port-pump-outlet", "ent-pump", "conn-suction-2"],
                                 "origin": "display"})
        self.assertEqual(echoed["selection"]["sequence"], 1)
        self.assertEqual(echoed["selection"]["origin"], "pid")
        cleared = service.select({"semantic_ids": [], "origin": "inspector"})
        self.assertEqual((cleared["selection"]["sequence"], cleared["selection"]["semantic_ids"]), (2, []))
        for body in ({"semantic_ids": ["sym-pump"], "origin": "pid"}, {"semantic_ids": ["ent-pump"], "origin": "x"},
                     {"semantic_ids": "ent-pump", "origin": "pid"}, {"semantic_ids": ["ent-pump"]}):
            with self.assertRaises(ServiceError) as caught:
                service.select(body)
            self.assertEqual(caught.exception.status, 400)
        state = service.state()
        self.assertEqual(state["selection"]["sequence"], 2)
        self.assertEqual(state["project"]["revision"], 0)  # selection never advances the revision

    def test_failed_display_mapping_is_unknown_not_an_intentional_absence(self):
        service = self.service(fail={"display": "missing_mapping"})
        service.request_build("test")
        build = wait_terminal(service)["build"]
        self.assertEqual((build["status"], build["artifact_status"]["display"]), ("failed", "failed"))
        placement = load_document(str(self.path))["current"]["physical"]["placements"]["ent-pump"]
        self.assertEqual(placement["status"], "placed")
        state = service.select({"semantic_ids": ["ent-pump", "ent-level-indicator"], "origin": "pid"})
        self.assertEqual(state["artifact_sources"]["display"]["artifact_status"], "failed")
        for item in state["selection"]["resolved"]:
            self.assertIsNone(item["display_components"], item)
            self.assertIsNone(item["display_build_id"], item)
            self.assertNotIn("absent_3d_reason", item)
        self.assertNotIn(build["id"], service._manifest_cache)  # a failed manifest is never cached as valid

    def test_verified_explicit_absence_keeps_its_reason(self):
        service = self.service(absent_display=["ent-level-indicator"])
        self.publish_initial(service)
        state = service.select({"semantic_ids": ["ent-level-indicator", "ent-pump"], "origin": "display"})
        resolved = {item["id"]: item for item in state["selection"]["resolved"]}
        indicator, pump = resolved["ent-level-indicator"], resolved["ent-pump"]
        self.assertEqual(indicator["display_components"], [])
        self.assertIn("conceptual item", indicator["absent_3d_reason"])
        self.assertEqual(indicator["display_build_id"], state["published"]["build_id"])
        self.assertEqual(pump["display_components"], ["fake:display:ent-pump"])
        self.assertNotIn("absent_3d_reason", pump)

    def test_selection_is_pruned_when_ids_disappear(self):
        service = self.service()
        project = load_document(str(self.path))["current"]
        payload = {
            "connection_id": "conn-discharge-2",
            "entity": {"id": "ent-hv103", "kind": "isolation_valve", "tag": "HV-103", "template_version": 1,
                       "parameters": {name: dict(project["model"]["entities"]["ent-discharge-valve"]["parameters"][name])
                                      for name in ("length", "body_diameter")}},
            "ports": [{"id": "port-hv103-in", "entity_id": "ent-hv103", "name": "inlet", "direction": "in",
                       "required": True, "nominal_diameter_mm": 50, "service": "liquid",
                       "assumption_id": "asm-dn50-liquid-service"},
                      {"id": "port-hv103-out", "entity_id": "ent-hv103", "name": "outlet", "direction": "out",
                       "required": True, "nominal_diameter_mm": 50, "service": "liquid",
                       "assumption_id": "asm-dn50-liquid-service"}],
            "downstream_connection": {"id": "conn-hv103-down", "tag": "L-106", "from_port_id": "port-hv103-out",
                                      "to_port_id": "port-discharge-valve-inlet", "nominal_diameter_mm": 50,
                                      "outside_diameter_m": 0.0603, "assumption_id": "asm-dn50-pipe"},
            "symbol": {"id": "sym-hv103", "entity_id": "ent-hv103", "x": 540.0, "y": 220.0, "rotation_deg": 0.0,
                       "manual": False},
            "downstream_edge": {"id": "edge-hv103-down", "connection_id": "conn-hv103-down",
                                "from_symbol_id": "sym-hv103", "to_symbol_id": "sym-discharge-valve", "points": [],
                                "manual": False, "status": "unresolved", "reason": "test"},
        }
        state = command(service, "insert_valve", payload)
        self.assertEqual(state["project"]["physical"]["placements"]["ent-hv103"]["status"], "unresolved")
        state = service.select({"semantic_ids": ["ent-hv103", "ent-pump"], "origin": "inspector"})
        self.assertEqual(state["selection"]["sequence"], 1)
        state = command(service, "undo", {})
        self.assertEqual(state["selection"]["semantic_ids"], ["ent-pump"])
        self.assertEqual((state["selection"]["sequence"], state["selection"]["origin"]), (2, "service"))
        wait_terminal(service)

    def test_proposal_review_apply_and_conflicts(self):
        service = self.service()
        proposal = service.propose({"text": "rename the pump to P-201", "expected_revision": 0})
        for key in ("id", "expected_revision", "summary", "commands", "assumptions", "unknowns", "supported"):
            self.assertIn(key, proposal)
        self.assertTrue(proposal["supported"])
        self.assertTrue(proposal["validation"]["accepted"])
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 0)  # proposing never writes
        with self.assertRaises(ServiceError) as caught:
            service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 1})
        self.assertEqual(caught.exception.status, 409)
        state = service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        self.assertEqual(state["last_command"]["source"], f"proposal {proposal['id']}")
        with self.assertRaises(ServiceError) as caught:
            service.apply_proposal({"proposal_id": proposal["id"], "expected_revision": 0})
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ServiceError) as caught:
            service.propose({"text": "rename the pump to P-301", "expected_revision": 0})
        self.assertEqual(caught.exception.status, 409)
        stale = service.propose({"text": "set the tank height to 1.3 m", "expected_revision": 1})
        command(service, "rename_entity", {"entity_id": "ent-tank", "tag": "TK-201"})
        with self.assertRaises(ServiceError) as caught:
            service.apply_proposal({"proposal_id": stale["id"], "expected_revision": 1})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 2)
        unsupported = service.propose({"text": "make it safer", "expected_revision": 2})
        self.assertFalse(unsupported["supported"])
        with self.assertRaises(ServiceError) as caught:
            service.apply_proposal({"proposal_id": unsupported["id"], "expected_revision": 2})
        self.assertEqual(caught.exception.status, 400)
        invalid = service.propose({"text": "rename the pump to TK-201", "expected_revision": 2})
        self.assertTrue(invalid["supported"])
        self.assertFalse(invalid["validation"]["accepted"])
        with self.assertRaises(ServiceError) as caught:
            service.apply_proposal({"proposal_id": invalid["id"], "expected_revision": 2})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(load_document(str(self.path))["current"]["revision"], 2)
        dimension = service.propose({"text": "set the tank height to 1.3 m", "expected_revision": 2})
        state = service.apply_proposal({"proposal_id": dimension["id"], "expected_revision": 2})
        tank = state["project"]["model"]["entities"]["ent-tank"]["parameters"]["height"]
        self.assertEqual(tank["value"], 1.3)
        self.assertIn(tank["assumption_id"], state["project"]["model"]["assumptions"])
        state = wait_terminal(service)
        self.assertEqual(state["published"]["source_revision"], 3)


if __name__ == "__main__":
    unittest.main()
