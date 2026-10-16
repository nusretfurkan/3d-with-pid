"""``POST /api/reroute`` and ``InteractiveService.reroute``: real core, real worker, LABELED FAKE generators.

    .venv-cad/bin/python -m unittest tests.test_interactive_rerouting -v

Covers one-revision/undo/save/build commits from the authoritative current
document, refusals that change nothing (revision, history, file bytes,
publication pointer, build), stale revision and stale source between route
computation and commit, a busy computation, concurrent clicks, preserved
manual work, and the HTTP whitelist/schema checks. Actual CAD validity of the
chosen routes is covered by ``tests.test_rerouting``; these fake generators
prove only service behaviour.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import threading
import time
import unittest
from pathlib import Path

from skid.core import apply_command, load_document, save_document
from skid.interactive import integrity
from skid.interactive.rerouting import port_frame
from skid.interactive.server import make_server
from skid.interactive.service import InteractiveService, ServiceError
from tests.test_interactive_fakes import command, make_service, new_project, wait_terminal
from tests.test_interactive_http import Client

OFFSET = [1.6, 0.1, 0.0]
TIGHT_VERTICAL = [1.5, 0.0, 0.05]


def placement(position, rotation=(0, 0, 0)):
    return {"position_m": list(position), "rotation_deg": list(rotation), "status": "placed", "manual": True,
            "assumption_id": "asm-skid-layout"}


def external_move(path, entity_id, position):
    """A competing writer outside the service (as the CLI does): core plus atomic storage."""
    document = load_document(str(path))
    revision = document["current"]["revision"]
    new, _ = apply_command(document, {"id": f"cmd-external-{revision}", "expected_revision": revision,
                                      "type": "move_equipment",
                                      "payload": {"entity_id": entity_id, "placement": placement(position)}})
    save_document(str(path), new, revision)
    return new


class RerouteCase(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)
        self.service = make_service(self, self.path)
        self.service.request_build("test")
        state = wait_terminal(self.service)
        self.assertEqual(state["build"]["status"], "complete", state["build"]["diagnostics"])

    def move_pump(self, position=OFFSET):
        state = command(self.service, "move_equipment", {"entity_id": "ent-pump", "placement": placement(position)})
        return wait_terminal(self.service) if state else state

    def snapshot(self):
        """Everything a refused reroute must leave unchanged."""
        state = self.service.state()
        pointer = Path(self.service.root, "current.json")
        return {"bytes": Path(self.path).read_bytes(), "revision": state["project"]["revision"],
                "history": (state["history"]["undo"], state["history"]["redo"]),
                "pointer": pointer.read_bytes() if pointer.exists() else None,
                "build": state["build"]["id"] if state["build"] else None,
                "last_command": state["last_command"]}

    def reroute(self, connection_id, revision=None):
        if revision is None:
            revision = self.service.state()["project"]["revision"]
        return self.service.reroute({"connection_id": connection_id, "expected_revision": revision})

    def refused(self, body, status, code=None):
        before = self.snapshot()
        with self.assertRaises(ServiceError) as caught:
            self.service.reroute(body)
        self.assertEqual(caught.exception.status, status, caught.exception.error)
        if code is not None:
            self.assertIn(code, [d["code"] for d in caught.exception.diagnostics], caught.exception.diagnostics)
        self.assertEqual(self.snapshot(), before)
        return caught.exception


class CommitTests(RerouteCase):
    def test_reroute_commits_one_revision_undo_save_and_build(self):
        moved = self.move_pump()
        self.assertEqual(moved["build"]["status"], "pending")
        revision, undo = moved["project"]["revision"], moved["history"]["undo"]
        published = moved["published"]["build_id"]
        state = self.reroute("conn-suction-2")
        self.assertEqual(state["project"]["revision"], revision + 1)
        self.assertEqual((state["history"]["undo"], state["history"]["redo"]), (undo + 1, 0))
        route = state["project"]["physical"]["routes"]["conn-suction-2"]
        self.assertEqual((route["status"], route["mode"]), ("current", "guided"))
        self.assertNotIn("reason", route)
        assumption = state["project"]["model"]["assumptions"][route["assumption_id"]]
        self.assertIn("not checked", assumption["description"])
        self.assertTrue(route["assumption_id"].startswith("asm-reroute-"))
        last = state["last_command"]
        self.assertEqual((last["type"], last["status"], last["source"]), ("set_route", "accepted",
                                                                           "reroute conn-suction-2"))
        self.assertTrue(last["command_id"].startswith("cmd-reroute-"))
        self.assertEqual(last["route"]["connection_id"], "conn-suction-2")
        self.assertEqual(load_document(str(self.path))["current"], state["project"])  # saved atomically
        self.assertEqual(state["build"]["source_revision"], revision + 1)  # the existing all-five build started
        self.assertEqual(state["published"]["build_id"], published)  # until it verifies, the old set stays
        # The second pending route, then every open route item is resolved and all five publish together.
        self.reroute("conn-discharge-1")
        final = wait_terminal(self.service)
        self.assertEqual(final["build"]["status"], "complete", final["build"]["diagnostics"])
        self.assertEqual(final["sync"]["state"], "synchronized")
        self.assertEqual(final["published"]["source_revision"], revision + 2)

    def test_route_starts_and_ends_at_the_authoritative_current_ports(self):
        self.move_pump()
        external_move(self.path, "ent-pump", [1.7, -0.1, 0.0])  # adopted from disk before routing
        state = self.reroute("conn-suction-2")
        project = state["project"]
        self.assertEqual(project["physical"]["placements"]["ent-pump"]["position_m"], [1.7, -0.1, 0.0])
        points = project["physical"]["routes"]["conn-suction-2"]["points_m"]
        self.assertEqual(points[0], port_frame(project, "port-suction-valve-outlet")["position_m"])
        self.assertEqual(points[-1], port_frame(project, "port-pump-inlet")["position_m"])
        self.assertEqual(points[-1], [1.7 - 0.325, -0.1, 0.2])
        self.assertEqual([round(points[1][i] - points[0][i], 9) > 0 for i in range(3)], [True, False, False])
        self.assertEqual(points[-2][1:], points[-1][1:])  # enters the pump inlet along +x (its inward axis)
        wait_terminal(self.service)

    def test_undo_restores_the_pending_route_and_redo_reapplies(self):
        moved = self.move_pump()
        pending = copy.deepcopy(moved["project"]["physical"]["routes"]["conn-suction-2"])
        rerouted = self.reroute("conn-suction-2")
        undone = command(self.service, "undo", {})
        self.assertEqual(undone["project"]["physical"]["routes"]["conn-suction-2"], pending)
        self.assertNotIn(rerouted["project"]["physical"]["routes"]["conn-suction-2"]["assumption_id"],
                         undone["project"]["model"]["assumptions"])
        redone = command(self.service, "redo", {})
        self.assertEqual(redone["project"]["physical"], rerouted["project"]["physical"])
        state = self.service.save({"expected_revision": redone["project"]["revision"]})
        self.assertIsNotNone(state["document"]["saved"])
        reopened = self.service.reopen({})
        self.assertEqual(reopened["project"], redone["project"])
        wait_terminal(self.service)

    def test_manual_and_unrelated_work_is_preserved(self):
        state = command(self.service, "move_symbol", {"symbol_id": "sym-tank", "x": 100.0, "y": 280.0,
                                                      "rotation_deg": 0.0})
        moved = self.move_pump()
        before = moved["project"]
        after = self.reroute("conn-discharge-1")["project"]  # a pending MANUAL route the user chose to reroute
        self.assertEqual(after["pid"], before["pid"])
        self.assertEqual(after["pid"]["symbols"]["sym-tank"]["x"], state["project"]["pid"]["symbols"]["sym-tank"]["x"])
        self.assertEqual(after["model"]["entities"], before["model"]["entities"])
        self.assertEqual(after["physical"]["placements"], before["physical"]["placements"])
        for cid, route in before["physical"]["routes"].items():
            if cid != "conn-discharge-1":
                self.assertEqual(after["physical"]["routes"][cid], route, cid)  # incl. the other pending route
        self.assertEqual(after["physical"]["routes"]["conn-discharge-1"]["mode"], "guided")
        wait_terminal(self.service)


class RefusalTests(RerouteCase):
    def test_malformed_requests(self):
        self.move_pump()
        revision = self.service.state()["project"]["revision"]
        for body in ({}, {"connection_id": "conn-suction-2"}, {"expected_revision": revision},
                     {"connection_id": "conn-suction-2", "expected_revision": revision, "points_m": []},
                     {"connection_id": 7, "expected_revision": revision},
                     {"connection_id": "", "expected_revision": revision},
                     {"connection_id": "x" * 201, "expected_revision": revision},
                     {"connection_id": "conn-suction-2", "expected_revision": True},
                     {"connection_id": "conn-suction-2", "expected_revision": -1},
                     {"connection_id": "conn-suction-2", "expected_revision": "1"}, []):
            with self.subTest(body=body):
                self.refused(body, 400)

    def test_stale_revision_unknown_and_current_routes(self):
        self.move_pump()
        revision = self.service.state()["project"]["revision"]
        self.refused({"connection_id": "conn-suction-2", "expected_revision": revision - 1}, 409, "stale_revision")
        self.refused({"connection_id": "conn-unknown", "expected_revision": revision}, 400,
                     "reroute_unknown_connection")
        # Clicking a current route never rewrites it.
        self.refused({"connection_id": "conn-suction-1", "expected_revision": revision}, 409, "route_current")

    def test_unplaced_unsupported_and_impossible_change_nothing(self):
        self.move_pump(TIGHT_VERTICAL)
        revision = self.service.state()["project"]["revision"]
        error = self.refused({"connection_id": "conn-suction-2", "expected_revision": revision}, 400,
                             "reroute_impossible")
        self.assertIn("no route was applied", error.error)
        self.move_pump([1.5, 0.0, 0.0])
        command(self.service, "move_equipment", {"entity_id": "ent-pump", "placement": placement([1.6, 0, 0],
                                                                                                (0, 0, 30))})
        revision = self.service.state()["project"]["revision"]
        self.refused({"connection_id": "conn-suction-2", "expected_revision": revision}, 400,
                     "reroute_unsupported_rotation")
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
        state = command(self.service, "insert_valve", payload)
        self.assertEqual(state["project"]["physical"]["placements"]["ent-hv103"]["status"], "unresolved")
        revision = state["project"]["revision"]
        for cid in ("conn-discharge-2", "conn-hv103-down"):
            error = self.refused({"connection_id": cid, "expected_revision": revision}, 400,
                                 "reroute_unplaced_endpoint")
            self.assertIn("ent-hv103", error.diagnostics[0]["entity_ids"])
        wait_terminal(self.service)

    def test_disk_invalid_blocks_reroute(self):
        self.move_pump()
        revision = self.service.state()["project"]["revision"]
        Path(self.path).write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ServiceError) as caught:
            self.service.reroute({"connection_id": "conn-suction-2", "expected_revision": revision})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), "{ not json")


class RaceTests(RerouteCase):
    def test_source_change_between_computation_and_commit_is_refused(self):
        self.move_pump()
        revision = self.service.state()["project"]["revision"]
        seen = []

        def competing_writer(plan):
            seen.append(plan["points_m"])
            external_move(self.path, "ent-pump", [1.7, 0.1, 0.0])  # the user/CLI moves the pump meanwhile

        self.service.before_reroute_commit = competing_writer
        with self.assertRaises(ServiceError) as caught:
            self.reroute("conn-suction-2", revision)
        self.service.before_reroute_commit = None
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual([d["code"] for d in caught.exception.diagnostics], ["stale_route"])
        self.assertEqual(len(seen), 1)
        on_disk = load_document(str(self.path))
        self.assertEqual(on_disk["current"]["revision"], revision + 1)  # only the competing change
        self.assertEqual(on_disk["current"]["physical"]["placements"]["ent-pump"]["position_m"], [1.7, 0.1, 0.0])
        self.assertEqual(on_disk["current"]["physical"]["routes"]["conn-suction-2"]["status"], "needs_reroute")
        state = self.service.state()
        self.assertEqual(state["project"], on_disk["current"])
        self.assertNotEqual(state["last_command"]["type"], "set_route")
        wait_terminal(self.service)

    def test_service_command_between_computation_and_commit_is_protected(self):
        self.move_pump()

        def user_edit(plan):
            command(self.service, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})

        self.service.before_reroute_commit = user_edit
        with self.assertRaises(ServiceError) as caught:
            self.reroute("conn-suction-2")
        self.service.before_reroute_commit = None
        self.assertEqual(caught.exception.status, 409)
        state = self.service.state()
        self.assertEqual(state["project"]["model"]["entities"]["ent-pump"]["tag"], "P-201")
        self.assertEqual(state["last_command"]["type"], "rename_entity")
        self.assertEqual(state["project"]["physical"]["routes"]["conn-suction-2"]["status"], "needs_reroute")
        wait_terminal(self.service)

    def test_busy_computation_refuses_a_second_request(self):
        self.move_pump()
        release, entered = threading.Event(), threading.Event()

        def hold(plan):
            entered.set()
            release.wait(10)

        self.service.before_reroute_commit = hold
        results = []
        worker = threading.Thread(target=lambda: results.append(self.reroute("conn-suction-2")))
        worker.start()
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        state = self.service.state()  # reads stay responsive: no service lock is held while routing
        self.assertLess(time.monotonic() - started, 2.0)
        error = self.refused({"connection_id": "conn-discharge-1", "expected_revision": state["project"]["revision"]},
                             409, "reroute_busy")
        self.assertIn("another reroute", error.error)
        release.set()
        worker.join(10)
        self.service.before_reroute_commit = None
        self.assertEqual(results[0]["project"]["physical"]["routes"]["conn-suction-2"]["status"], "current")
        wait_terminal(self.service)

    def test_concurrent_clicks_commit_exactly_once(self):
        moved = self.move_pump()
        revision = moved["project"]["revision"]
        outcomes, barrier = [], threading.Barrier(4)

        def click():
            barrier.wait()
            try:
                self.reroute("conn-suction-2", revision)
                outcomes.append("accepted")
            except ServiceError as exc:
                outcomes.append(exc.status)

        threads = [threading.Thread(target=click) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(outcomes.count("accepted"), 1, outcomes)
        self.assertEqual(sorted(o for o in outcomes if o != "accepted"), [409, 409, 409])
        state = self.service.state()
        self.assertEqual(state["project"]["revision"], revision + 1)
        self.assertEqual(state["history"]["undo"], moved["history"]["undo"] + 1)
        wait_terminal(self.service)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.path = new_project(self)
        self.service = make_service(self, self.path)
        self.server = make_server(self.service, 0, quiet=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.client = Client(self.server.port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def test_reroute_endpoint(self):
        status, state, _ = self.client.json("POST", "/api/command", {
            "id": "cmd-http-move", "expected_revision": 0, "type": "move_equipment",
            "payload": {"entity_id": "ent-pump", "placement": placement(OFFSET)}})
        self.assertEqual(status, 200, state)
        before = Path(self.path).read_bytes()
        status, body, _ = self.client.json("POST", "/api/reroute", {"connection_id": "conn-suction-2",
                                                                     "expected_revision": 1, "extra": 1})
        self.assertEqual(status, 400)
        self.assertIn("unsupported", body["error"])
        status, body, _ = self.client.json("POST", "/api/reroute", {"connection_id": "conn-suction-2",
                                                                     "expected_revision": 0})
        self.assertEqual((status, body["diagnostics"][0]["code"]), (409, "stale_revision"))
        status, body, _ = self.client.json("POST", "/api/reroute", {"connection_id": "conn-suction-1",
                                                                     "expected_revision": 1})
        self.assertEqual((status, body["diagnostics"][0]["code"]), (409, "route_current"))
        status, _, _ = self.client.request("POST", "/api/reroute", raw=b'{"connection_id":"conn-suction-2"}',
                                           headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.client.json("POST", "/api/reroute", {"connection_id": "conn-suction-2",
                                                                 "expected_revision": 1},
                                        headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.request("GET", "/api/reroute")
        self.assertEqual(status, 404)
        self.assertEqual(Path(self.path).read_bytes(), before)
        status, state, _ = self.client.json("POST", "/api/reroute", {"connection_id": "conn-suction-2",
                                                                     "expected_revision": 1})
        self.assertEqual(status, 200, state)
        for key in ("project", "diagnostics", "selection", "build", "published", "artifacts", "history", "sync"):
            self.assertIn(key, state)
        self.assertEqual(state["project"]["revision"], 2)
        self.assertEqual(state["project"]["physical"]["routes"]["conn-suction-2"]["mode"], "guided")
        self.assertEqual(state["build"]["source_revision"], 2)
        wait_terminal(self.service)


_MISSING = [name for name in ("skid.geometry", "skid.drafting", "cadquery") if importlib.util.find_spec(name) is None]


@unittest.skipIf(_MISSING, f"real generators unavailable in this interpreter: {', '.join(_MISSING)}")
class RealGeneratorRerouteTests(unittest.TestCase):
    """REAL geometry/drafting workers: rerouted pipes become CAD solids and all five outputs publish."""

    def test_move_then_reroute_publishes_all_five_with_cad_pipes(self):
        path = new_project(self)
        service = InteractiveService(str(path), timeout=600, terminate_grace=2.0)
        self.addCleanup(service.close)
        command(service, "move_equipment", {"entity_id": "ent-pump",
                                            "placement": placement([2.0, -0.5, 0.3], (0, 0, 90))})
        moved = wait_terminal(service, timeout=600)
        self.assertEqual(moved["build"]["status"], "pending", moved["build"]["message"])
        for cid in ("conn-suction-2", "conn-discharge-1"):
            service.reroute({"connection_id": cid, "expected_revision": service.state()["project"]["revision"]})
        state = wait_terminal(service, timeout=600)
        build = state["build"]
        self.assertEqual(build["status"], "complete", build["diagnostics"][:5])
        self.assertEqual(build["artifact_status"], dict.fromkeys(integrity.REQUIRED_ARTIFACTS, "complete"))
        self.assertEqual(state["sync"]["state"], "synchronized")
        self.assertEqual(state["published"]["source_revision"], state["project"]["revision"])
        build_dir = Path(service.builds_root, build["id"])
        manifest = json.loads((build_dir / "cad.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        scene = json.loads((build_dir / "scene.json").read_text(encoding="utf-8"))
        pipes = {c["id"]: c for c in scene["components"] if c["id"].endswith("#pipe")}
        for cid in ("conn-suction-2", "conn-discharge-1"):
            self.assertIn(f"{cid}#pipe", pipes)
            self.assertFalse(pipes[f"{cid}#pipe"]["display_only"])
            self.assertIn(f"{cid}#pipe", manifest["representations"][cid])
        self.assertTrue((build_dir / "model.step").read_bytes().startswith(b"ISO-10303-21;"))


if __name__ == "__main__":
    unittest.main()
