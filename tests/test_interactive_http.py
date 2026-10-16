"""Live loopback HTTP tests of the interactive service (LABELED FAKE generators, real worker process).

Requests go through ``http.client`` to a server bound on 127.0.0.1 with an
ephemeral port: contract response shapes, Host/Origin/content-type enforcement,
traversal refusal, selection exchange between two clients, ordered event
recovery after a missed poll, and the ``python -m skid.interactive`` launch.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from skid.interactive.server import make_server
from tests.test_interactive_fakes import ROOT, make_service, new_project, wait_terminal


class Client:
    def __init__(self, port):
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            all_headers = {"Host": f"127.0.0.1:{self.port}"}
            if body is not None:
                raw = json.dumps(body).encode("utf-8")
                all_headers.update({"Content-Type": "application/json", "Origin": self.origin,
                                    "Sec-Fetch-Site": "same-origin"})
            all_headers.update(headers or {})
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in all_headers.items():
                if value is not None:
                    connection.putheader(key, value)
            if raw is not None and "Content-Length" not in all_headers:
                connection.putheader("Content-Length", str(len(raw)))
            connection.endheaders(raw)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def json(self, method, path, body=None, headers=None, raw=None):
        status, headers, data = self.request(method, path, body, headers, raw)
        return status, json.loads(data) if data else None, headers

    def state(self):
        status, payload, _ = self.json("GET", "/api/state")
        assert status == 200, payload
        return payload


class LiveServerCase(unittest.TestCase):
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

    def build_complete(self):
        self.service.request_build("test")
        return wait_terminal(self.service)


class ContractTests(LiveServerCase):
    def test_state_and_read_endpoints(self):
        status, state, headers = self.client.json("GET", "/api/state")
        self.assertEqual(status, 200)
        for key in ("project", "diagnostics", "selection", "build", "published", "artifacts", "history"):
            self.assertIn(key, state)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(state["project"]["id"], "project-pump-skid")
        for path, key in (("/api/project", "project"), ("/api/history", "undo"), ("/api/selection", "sequence")):
            status, payload, _ = self.client.json("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(key, payload)

    def test_command_build_and_error_shapes(self):
        envelope = {"id": "cmd-http-1", "expected_revision": 0, "type": "rename_entity",
                    "payload": {"entity_id": "ent-pump", "tag": "P-201"}}
        status, state, _ = self.client.json("POST", "/api/command", envelope)
        self.assertEqual(status, 200, state)
        self.assertEqual(state["project"]["revision"], 1)
        self.assertEqual(state["build"]["source_revision"], 1)
        status, error, _ = self.client.json("POST", "/api/command", envelope)
        self.assertEqual(status, 409)
        self.assertEqual(set(error), {"error", "diagnostics"})
        self.assertEqual(error["diagnostics"][0]["code"], "stale_revision")
        status, error, _ = self.client.json("POST", "/api/command", dict(envelope, expected_revision=1,
                                                                        payload={"entity_id": "ent-pump",
                                                                                 "tag": "TK-101"}))
        self.assertEqual(status, 400)
        self.assertEqual(error["diagnostics"][0]["code"], "duplicate_tag")
        state = wait_terminal(self.service)
        self.assertEqual(state["published"]["source_revision"], 1)
        status, error, _ = self.client.json("POST", "/api/cancel", {"build_id": state["build"]["id"]})
        self.assertEqual(status, 409)
        status, state, _ = self.client.json("POST", "/api/build", {})
        self.assertEqual(status, 200)
        status, state, _ = self.client.json("POST", "/api/save", {"expected_revision": 1})
        self.assertEqual((status, state["document"]["saved"]["revision"]), (200, 1))
        status, state, _ = self.client.json("POST", "/api/reopen", {})
        self.assertEqual(status, 200)

    def test_proposal_round_trip(self):
        status, proposal, _ = self.client.json("POST", "/api/propose", {"text": "move P-101 0.1 m in +y",
                                                                          "expected_revision": 0})
        self.assertEqual(status, 200)
        self.assertTrue(proposal["supported"])
        self.assertEqual(proposal["commands"][0]["type"], "move_equipment")
        status, state, _ = self.client.json("POST", "/api/apply-proposal", {"proposal_id": proposal["id"],
                                                                            "expected_revision": 0})
        self.assertEqual(status, 200)
        self.assertEqual(state["project"]["physical"]["placements"]["ent-pump"]["position_m"][1], 0.1)
        status, refused, _ = self.client.json("POST", "/api/propose", {"text": "add a second pump",
                                                                         "expected_revision": 1})
        self.assertEqual((status, refused["supported"], refused["commands"]), (200, False, []))


class SecurityTests(LiveServerCase):
    def test_host_origin_fetch_site_and_content_type(self):
        body = {"semantic_ids": ["ent-pump"], "origin": "pid"}
        status, _, _ = self.client.json("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.json("GET", "/api/state", headers={"Host": f"127.0.0.1.evil:{self.server.port}"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.json("POST", "/api/selection", body, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.json("POST", "/api/selection", body, headers={"Origin": "null"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.json("POST", "/api/selection", body, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.client.json("POST", "/api/selection", body, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.client.json("POST", "/api/selection", body,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, _ = self.client.json("POST", "/api/selection", body, headers={"Origin": None,
                                                                                 "Sec-Fetch-Site": None})
        self.assertEqual(status, 200)  # non-browser local clients send neither header
        self.assertEqual(self.client.state()["selection"]["sequence"], 1)

    def test_body_limits_and_strict_json(self):
        headers = {"Content-Type": "application/json", "Origin": self.client.origin}
        # The limit is enforced from Content-Length before any body byte is read.
        status, error, _ = self.client.json("POST", "/api/selection", raw=b"{}",
                                            headers=dict(headers, **{"Content-Length": str((1 << 20) + 1)}))
        self.assertEqual(status, 413)
        for raw in (b"{", b'{"origin": "pid", "origin": "display", "semantic_ids": []}', b"[1, 2]", b"NaN",
                    b'{"semantic_ids": [], "origin": "pid", "x": NaN}'):
            status, error, _ = self.client.json("POST", "/api/selection", headers=headers, raw=raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("error", error)
        status, _, _ = self.client.json("POST", "/api/selection", headers=dict(headers, **{"Content-Length": "abc"}),
                                        raw=b"{}")
        self.assertEqual(status, 411)
        self.assertEqual(self.client.state()["selection"]["sequence"], 0)

    def test_methods_and_unknown_routes(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, _ = self.client.request(method, "/api/state")
            self.assertEqual(status, 405, method)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _ = self.client.json("POST", "/api/shell", {"cmd": "ls"})
        self.assertEqual(status, 404)
        status, _, _ = self.client.json("GET", "/api/nothing")
        self.assertEqual(status, 404)
        status, _, _ = self.client.json("POST", "/api/state", {})
        self.assertEqual(status, 404)

    def test_artifact_and_static_traversal_is_refused(self):
        state = self.build_complete()
        build_id = state["published"]["build_id"]
        status, headers, data = self.client.request("GET", f"/artifacts/{build_id}/scene.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(json.loads(data)["source_revision"], 0)
        status, headers, _ = self.client.request("GET", f"/artifacts/{build_id}/pid.svg")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/svg+xml"))
        status, headers, _ = self.client.request("GET", f"/artifacts/{build_id}/model.step")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        status, headers, data = self.client.request("HEAD", f"/artifacts/{build_id}/scene.json")
        self.assertEqual((status, data), (200, b""))
        outside = Path(self.service.root, "secret.json")
        outside.write_text("{}")
        os.symlink(outside, Path(self.service.builds_root, build_id, "..", "link.json"))
        refused = [
            f"/artifacts/{build_id}/../current.json", f"/artifacts/{build_id}/%2e%2e/current.json",
            f"/artifacts/{build_id}/..%2fcurrent.json", f"/artifacts/{build_id}/preview/..%2f..%2fcurrent.json",
            f"/artifacts/{build_id}/%252e%252e/current.json", f"/artifacts/{build_id}//etc/passwd",
            f"/artifacts/{build_id}/worker.log", f"/artifacts/{build_id}/events.jsonl", f"/artifacts/{build_id}",
            f"/artifacts/{build_id}/", "/artifacts/../workflow.json", "/artifacts/b20990101T000000-r0-00000000/x.json",
            f"/artifacts/{build_id}%2f..%2fcurrent.json", f"/artifacts/{build_id}/scene.json%00.svg",
            f"/artifacts/{build_id}/..\\current.json", "/artifacts/builds/scene.json",
            "/../AGENTS.md", "/%2e%2e/AGENTS.md", "/vendor/../../skid/core/storage.py", "/skid/core/storage.py",
            "/.service.lock", "/%2e%2e%2fHANDOVER.md", "/index.html/../../README.md", "/web/index.html%00",
        ]
        for path in refused:
            status, headers, data = self.client.request("GET", path)
            self.assertIn(status, (403, 404), path)
            self.assertNotIn(b"project-pump-skid", data, path)

    def test_fallback_or_ui_root(self):
        status, headers, data = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        if not (ROOT / "web" / "index.html").exists():
            self.assertIn(b"/api/state", data)


class ExchangeTests(LiveServerCase):
    def test_selection_exchange_between_two_views(self):
        self.build_complete()
        pid_view, display_view = self.client, Client(self.server.port)
        seen = display_view.state()["selection"]["sequence"]
        status, state, _ = pid_view.json("POST", "/api/selection", {"semantic_ids": ["ent-pump"], "origin": "pid"})
        self.assertEqual(status, 200)
        update = display_view.state()["selection"]
        self.assertEqual((update["sequence"], update["origin"], update["semantic_ids"]), (seen + 1, "pid",
                                                                                          ["ent-pump"]))
        self.assertEqual(update["resolved"][0]["display_components"], ["fake:display:ent-pump"])
        self.assertEqual(update["resolved"][0]["pid_occurrences"], ["sym-pump"])
        # The display view highlights; an echo of the same set does not advance the sequence (no loop).
        status, state, _ = display_view.json("POST", "/api/selection", {"semantic_ids": ["ent-pump"],
                                                                        "origin": "display"})
        self.assertEqual((state["selection"]["sequence"], state["selection"]["origin"]), (seen + 1, "pid"))
        status, state, _ = display_view.json("POST", "/api/selection", {"semantic_ids": ["conn-discharge-1"],
                                                                        "origin": "display"})
        update = pid_view.state()["selection"]
        self.assertEqual((update["sequence"], update["origin"]), (seen + 2, "display"))
        self.assertEqual(update["resolved"][0]["pid_occurrences"], ["edge-discharge-1"])
        self.assertEqual(update["semantic_ids"], ["conn-discharge-1"])
        self.assertEqual(pid_view.state()["project"]["revision"], 0)

    def test_reconnect_recovers_ordered_events_and_full_state(self):
        gate = self.path.parent / "gate.txt"
        self.service.suite_options = {"gate": str(gate)}
        envelope = {"id": "cmd-http-2", "expected_revision": 0, "type": "rename_entity",
                    "payload": {"entity_id": "ent-pump", "tag": "P-202"}}
        status, state, _ = self.client.json("POST", "/api/command", envelope)
        build_id = state["build"]["id"]
        deadline = time.monotonic() + 30
        while not self.client.state()["build"]["previews"] and time.monotonic() < deadline:
            time.sleep(0.02)
        partial = self.client.state()["build"]
        seen = partial["last_sequence"]
        self.assertTrue(partial["previews"])
        for url in partial["previews"].values():
            status, _, _ = self.client.request("GET", url)
            self.assertEqual(status, 200, url)
        gate.write_text("open")
        wait_terminal(self.service)
        status, tail, _ = self.client.json("GET", f"/api/events?build_id={build_id}&after={seen}")
        self.assertEqual(status, 200)
        self.assertEqual(tail["events"][0]["sequence"], seen + 1)
        reconnected = Client(self.server.port).state()["build"]
        self.assertEqual(reconnected["id"], build_id)
        full = [e["sequence"] for e in reconnected["events"]]
        self.assertEqual(full, list(range(1, reconnected["last_sequence"] + 1)))
        self.assertEqual(partial["events"], reconnected["events"][:len(partial["events"])])
        self.assertEqual([e["sequence"] for e in tail["events"]], full[seen:])
        self.assertEqual(reconnected["status"], "complete")
        for query in ("build_id=x&after=-1", f"build_id={build_id}&after=abc", "after=1", f"build_id={build_id}&"
                      f"build_id={build_id}"):
            status, _, _ = self.client.json("GET", f"/api/events?{query}")
            self.assertIn(status, (400, 404), query)


class LaunchTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "skid.interactive", *arguments], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)

    def test_explicit_existing_project_is_required(self):
        missing = self.run_cli("--project", "/nonexistent/project.json", "--port", "0")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("never creates a project", missing.stderr)
        self.assertFalse(Path("/nonexistent/project.json").exists())
        fixture = self.run_cli("--project", str(ROOT / "examples" / "pump_skid.json"), "--port", "0")
        self.assertEqual(fixture.returncode, 2)
        self.assertIn("repository example", fixture.stderr)
        self.assertEqual(self.run_cli("--port", "0").returncode, 2)

    def test_launch_serves_state_and_stops_cleanly(self):
        path = new_project(self)
        process = subprocess.Popen([sys.executable, "-m", "skid.interactive", "--project", str(path), "--port", "0",
                                    "--no-initial-build", "--quiet"], cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            line = process.stdout.readline()
            self.assertIn("Serving project project-pump-skid revision 0 at http://127.0.0.1:", line)
            port = int(line.rsplit(":", 1)[1].strip().rstrip("/"))
            state = Client(port).state()
            self.assertEqual(state["project"]["revision"], 0)
            self.assertIsNone(state["build"])
            self.assertEqual(state["service"]["suite"], "skid.interactive.generators:real_suite")
        finally:
            process.terminate()
            process.wait(timeout=30)
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(process.returncode, 0)
        self.assertTrue(path.with_suffix(".workflow").is_dir())


if __name__ == "__main__":
    unittest.main()
