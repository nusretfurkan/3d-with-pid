"""LABELED FAKE generator suite and helpers for the interactive-service tests.

``FakeSuite`` is NOT the contracted ``skid.geometry``/``skid.drafting``
generators. The worker process imports it by name
(``tests.test_interactive_fakes:fake_suite``) and calls it exactly as it calls
the real suite. It writes every file the 0.2 contract names with clearly marked
FAKE content, emits events for files that really exist, honours
``cancelled()``, and injects faults selected by its options:

- ``fail``: ``{artifact: mode}`` (see ``MODES``); a failure is applied after the
  failing generator's own other outputs are written, and with ``after_other``
  only once the other generator has finished.
- ``gate``: a path; generators named in ``gate_generators`` (default both) wait
  for it to exist before writing final outputs, polling ``cancelled()`` unless
  ``ignore_cancel`` is set.
- ``absent_display``: semantic IDs the display manifest maps to an explicit
  empty list (an intentional absence of 3D representation).

This module defines no test cases.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from skid.core import canonical_digest, load_document, save_document, validate_project

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "pump_skid.json"
FAKE_SUITE = "tests.test_interactive_fakes:fake_suite"
ARTIFACTS = ("display", "cad", "pid", "legend", "description")
MODES = ("raise", "status_failed", "error_diagnostic", "warning_complete", "wrong_digest", "wrong_revision",
         "wrong_version", "missing_file", "unsafe_file", "missing_mapping", "unknown_mapping", "svg_script",
         "html_script", "manifest_mismatch", "scene_identity", "bad_event", "mutate_input", "mutate_preview",
         "interrupt_without_cancel")
GENERATOR_OF = {"display": "geometry", "cad": "geometry", "pid": "drafting", "legend": "drafting",
                "description": "drafting"}
GATE_ARTIFACT = {"geometry": "display", "drafting": "pid"}


def gate_message(generator):
    """Activity message a gated FAKE generator emits immediately before entering its gate loop."""
    return f"FAKE {generator} waiting at gate"


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _json(path, value):
    _write(path, json.dumps(value, indent=1, sort_keys=True))


class FakeSuite:
    """FAKE generators honouring the 0.2 generator contract (files, manifests, events, cancellation)."""

    def __init__(self, options):
        self.options = dict(options)
        self.fail = dict(self.options.get("fail", {}))

    # --- shared ---------------------------------------------------------------------

    def _identity(self, project):
        return {"project_id": project["id"], "source_revision": project["revision"],
                "input_digest": canonical_digest(project)}

    def _manifest(self, adapter, project, files):
        model = project["model"]
        open_items = [d for d in validate_project(project) if d["severity"] == "warning"]
        representations = {i: [f"fake:{adapter}:{i}"] for i in
                           list(model["entities"]) + list(model["ports"]) + list(model["connections"])}
        if adapter == "display":  # an explicit, intentional absence (for example a conceptual item)
            representations.update(dict.fromkeys(self.options.get("absent_display", ()), []))
        return dict(self._identity(project), adapter=adapter, adapter_version="0.2.0",
                    status="pending" if open_items else "complete",
                    diagnostics=[dict(d) for d in open_items[:3]], representations=representations, files=files,
                    fake=True)

    def _wait(self, generator, out, emit, cancelled):
        gate = self.options.get("gate")
        if gate and generator in self.options.get("gate_generators", ("geometry", "drafting")):
            # Synchronization point for tests: emitted immediately before the gate loop. With ignore_cancel
            # nothing after this event honours cancellation until the gate opens (or 120 s pass).
            emit({"kind": "activity", "artifact": GATE_ARTIFACT[generator], "message": gate_message(generator)})
            deadline = time.monotonic() + 120
            while not os.path.exists(gate) and time.monotonic() < deadline:
                if cancelled() and not self.options.get("ignore_cancel"):
                    raise InterruptedError("FAKE generator cancelled while waiting")
                time.sleep(0.01)  # test-only gate polling; product generators never sleep
        if cancelled():
            raise InterruptedError("FAKE generator cancelled")

    def _after_other(self, generator, out, cancelled):
        other = "drafting" if generator == "geometry" else "geometry"
        deadline = time.monotonic() + 60
        while not os.path.exists(os.path.join(out, f"fake-{other}.done")) and time.monotonic() < deadline:
            if cancelled():
                raise InterruptedError("FAKE generator cancelled")
            time.sleep(0.01)

    def _finish(self, generator, project, out, emit, cancelled, manifests):
        """Apply injected failures for ``generator``'s artifacts; write manifests; return the result."""
        members = [a for a in ARTIFACTS if GENERATOR_OF[a] == generator]
        failing = {a: self.fail[a] for a in members if a in self.fail}
        if failing and self.options.get("after_other"):
            self._after_other(generator, out, cancelled)
        returned = {}
        for artifact in members:
            manifest = manifests[artifact]
            mode = failing.get(artifact)
            if mode == "status_failed":
                manifest.update(status="failed", diagnostics=[{"code": "fake_failure", "severity": "error",
                                                               "entity_ids": [], "message": "FAKE injected failure"}])
            elif mode == "error_diagnostic":
                manifest["diagnostics"] = [{"code": "fake_error", "severity": "error", "entity_ids": ["ent-pump"],
                                            "message": "FAKE injected error with a complete status"}]
            elif mode == "warning_complete":
                manifest.update(status="complete", diagnostics=[{"code": "fake_warning", "severity": "warning",
                                                                 "entity_ids": [], "message": "FAKE open item"}])
            elif mode == "wrong_digest":
                manifest["input_digest"] = "0" * 64
            elif mode == "wrong_revision":
                manifest["source_revision"] = project["revision"] + 1
            elif mode == "wrong_version":
                manifest["adapter_version"] = "0.1.0"
            elif mode == "missing_mapping":
                manifest["representations"].pop("ent-pump", None)
            elif mode == "unknown_mapping":
                manifest["representations"]["ent-ghost"] = ["fake:ghost"]
            elif mode == "unsafe_file":
                manifest["files"].append("../outside.json")
            elif mode == "svg_script":
                target = os.path.join(out, manifest["files"][0])
                _write(target, Path(target).read_text("utf-8").replace("</svg>", "<script>alert(1)</script></svg>"))
            elif mode == "html_script":
                target = os.path.join(out, "description.html")
                _write(target, Path(target).read_text("utf-8").replace("</body>", "<img src=x onerror=alert(1)></body>"))
            elif mode == "scene_identity":
                scene = json.loads(Path(out, "scene.json").read_text("utf-8"))
                scene["source_revision"] = project["revision"] + 7
                _json(os.path.join(out, "scene.json"), scene)
            elif mode == "bad_event":
                try:
                    emit({"kind": "preview", "artifact": artifact, "message": "FAKE event naming a missing file",
                          "path": "preview/missing.json"})
                except ValueError:
                    pass  # a generator swallowing the rejection must still fail
            elif mode == "mutate_preview":
                previews = sorted(p for p in os.listdir(os.path.join(out, "preview")) if p.startswith(artifact))
                _write(os.path.join(out, "preview", previews[0]), "<svg xmlns='http://www.w3.org/2000/svg'/>"
                       if previews[0].endswith(".svg") else json.dumps(dict(self._identity(project), mutated=True)))
            elif mode == "mutate_input":
                project["model"]["entities"]["ent-pump"]["tag"] = "MUTATED"
            _json(os.path.join(out, f"{artifact}.manifest.json"), manifest)
            returned[artifact] = json.loads(json.dumps(manifest))
            if mode == "missing_file":
                os.unlink(os.path.join(out, manifest["files"][0]))
            elif mode == "manifest_mismatch":
                returned[artifact]["status"] = "complete" if manifest["status"] == "pending" else "pending"
        _write(os.path.join(out, f"fake-{generator}.done"), "FAKE marker\n")
        modes = set(failing.values())
        if "raise" in modes:
            raise RuntimeError(f"FAKE injected {generator} fault")
        if "interrupt_without_cancel" in modes:
            raise InterruptedError("FAKE interruption without a cancellation request")
        return returned

    # --- geometry -------------------------------------------------------------------

    def geometry(self, project, out, emit, cancelled):
        identity = self._identity(project)
        emit({"kind": "activity", "artifact": "display", "message": "FAKE geometry started"})
        entities = sorted(project["model"]["entities"])
        components = []
        for index, entity_id in enumerate(entities, 1):
            if cancelled():
                raise InterruptedError("FAKE geometry cancelled")
            entity = project["model"]["entities"][entity_id]
            components.append({"id": f"{entity_id}/body", "semantic_id": entity_id, "component_key": "body",
                               "tag": entity["tag"], "positions": [0, 0, 0, 1, 0, 0, 0, 1, 0], "normals": [0, 0, 1] * 3,
                               "indices": [0, 1, 2], "material": {"color": "#3366aa", "metalness": 0.2,
                                                                  "roughness": 0.6},
                               "bounds_m": {"min": [0, 0, 0], "max": [1, 1, 0]}, "display_only": False})
            relpath = f"preview/display-{index:04d}.json"
            _json(os.path.join(out, relpath), dict(identity, schema_version=1, units="m", up_axis="Z",
                                                   components=list(components), ports=[], assumptions=[],
                                                   bounds_m={"min": [0, 0, 0], "max": [1, 1, 0]}, fake=True))
            emit({"kind": "preview", "artifact": "display", "message": f"FAKE placed {entity['tag']}",
                  "path": relpath, "completed": index, "total": len(entities)})
        self._wait("geometry", out, emit, cancelled)
        _json(os.path.join(out, "scene.json"), dict(identity, schema_version=1, units="m", up_axis="Z",
                                                     components=components, ports=[], assumptions=[],
                                                     bounds_m={"min": [0, 0, 0], "max": [1, 1, 0]}, fake=True))
        emit({"kind": "artifact", "artifact": "display", "message": "FAKE scene written", "path": "scene.json"})
        emit({"kind": "activity", "artifact": "cad", "message": "FAKE CAD export"})
        _write(os.path.join(out, "model.step"), "ISO-10303-21;\nHEADER;\n/* FAKE STEP, not CAD output */\nENDSEC;\n"
                                                "END-ISO-10303-21;\n")
        _json(os.path.join(out, "geometry.recipe.json"), dict(identity, fake=True, components=len(components)))
        _json(os.path.join(out, "cad.verification.json"), dict(identity, fake=True, reimported=False))
        emit({"kind": "artifact", "artifact": "cad", "message": "FAKE STEP written", "path": "model.step"})
        manifests = {"display": self._manifest("display", project, ["scene.json"]),
                     "cad": self._manifest("cad", project, ["model.step", "geometry.recipe.json",
                                                            "cad.verification.json"])}
        return self._finish("geometry", project, out, emit, cancelled, manifests)

    # --- drafting -------------------------------------------------------------------

    def _svg(self, title, project, symbols):
        groups = "".join(
            f'<g data-semantic-id="{escape(s["entity_id"], {chr(34): "&quot;"})}" '
            f'data-occurrence-id="{escape(sid, {chr(34): "&quot;"})}"><rect x="{s["x"]}" y="{s["y"]}" width="20" '
            f'height="20"/><text x="{s["x"]}" y="{s["y"] - 4}">'
            f'{escape(project["model"]["entities"][s["entity_id"]]["tag"])}</text></g>'
            for sid, s in symbols)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 840"><text x="40" y="40">{escape(title)}'
                f'</text>{groups}</svg>')

    def drafting(self, project, out, emit, cancelled, manual_notes):
        identity = self._identity(project)
        emit({"kind": "activity", "artifact": "pid", "message": "FAKE drafting started"})
        symbols = sorted(project["pid"]["symbols"].items())
        for index in range(1, len(symbols) + 1):
            if cancelled():
                raise InterruptedError("FAKE drafting cancelled")
            for artifact in ("pid", "legend"):
                relpath = f"preview/{artifact}-{index:04d}.svg"
                _write(os.path.join(out, relpath), self._svg(f"FAKE {artifact} draft", project, symbols[:index]))
                emit({"kind": "preview", "artifact": artifact, "message": f"FAKE {artifact} stage {index}",
                      "path": relpath, "completed": index, "total": len(symbols)})
        self._wait("drafting", out, emit, cancelled)
        _write(os.path.join(out, "pid.svg"), self._svg("FAKE P&ID, not a generated drawing", project, symbols))
        _write(os.path.join(out, "legend.svg"), self._svg("FAKE legend", project, []))
        _json(os.path.join(out, "drawing.inventory.json"), dict(identity, fake=True, keys=[]))
        _json(os.path.join(out, "item-index.json"), dict(identity, fake=True, items=[]))
        tags = sorted(e["tag"] for e in project["model"]["entities"].values())
        _json(os.path.join(out, "description.json"), dict(identity, purpose="FAKE", flow_sequence=[], items=[],
                                                           assumptions=[], unknowns=[], manual_notes=manual_notes,
                                                           conflicts=[], fake=True))
        _write(os.path.join(out, "description.html"),
               "<!doctype html><html><body><h1>FAKE description</h1><ul>"
               + "".join(f"<li>{escape(t)}</li>" for t in tags) + "</ul></body></html>")
        for artifact, relpath in (("pid", "pid.svg"), ("legend", "legend.svg"), ("description", "description.html")):
            emit({"kind": "artifact", "artifact": artifact, "message": f"FAKE {artifact} written", "path": relpath})
        manifests = {"pid": self._manifest("pid", project, ["pid.svg", "drawing.inventory.json", "item-index.json"]),
                     "legend": self._manifest("legend", project, ["legend.svg"]),
                     "description": self._manifest("description", project, ["description.json", "description.html"])}
        manifests["legend"]["representations"] = {}
        return self._finish("drafting", project, out, emit, cancelled, manifests)


def fake_suite(options):
    return FakeSuite(options)


# --- Test helpers ---------------------------------------------------------------------


def new_project(test, name="pump skid.json"):
    """A fresh copy of the fixture in a temporary directory with spaces in its path."""
    tmp = tempfile.TemporaryDirectory(prefix="skid interactive ")
    test.addCleanup(tmp.cleanup)
    directory = Path(tmp.name) / "my project"
    directory.mkdir()
    path = directory / name
    save_document(str(path), load_document(str(FIXTURE)), None)
    return path


def make_service(test, path, workflow_dir=None, **options):
    from skid.interactive.service import InteractiveService

    settings = {"timeout": options.pop("timeout", 60), "terminate_grace": options.pop("terminate_grace", 0.5),
                "keep_builds": options.pop("keep_builds", 0)}
    notes = options.pop("notes_path", None)
    service = InteractiveService(str(path), workflow_dir, suite=FAKE_SUITE, suite_options=options, notes_path=notes,
                                 **settings)
    test.addCleanup(service.close)
    return service


def wait_until(service, predicate, timeout=30.0, what="condition"):
    deadline = time.monotonic() + timeout
    state = service.state()
    while not predicate(state):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}; build: "
                                 f"{json.dumps((state.get('build') or {}).get('message'))}")
        time.sleep(0.02)
        state = service.state()
    return state


def wait_terminal(service, build_id=None, timeout=30.0):
    def done(state):
        build = state["build"]
        if build is None or (build_id is not None and build["id"] != build_id):
            record = service._builds.get(build_id) if build_id else None
            return record is not None and record.status not in ("queued", "running") and (
                record.process is None or record.process.poll() is not None) and service.active is not record
        return build["status"] not in ("queued", "running") and service.active is None and service.queued is None
    return wait_until(service, done, timeout, f"build {build_id or ''} to finish")


def gate_reached(build, generators=("geometry", "drafting")):
    """Generators of ``build`` (a state build payload) known to be inside their gate loop."""
    messages = {e["message"] for e in build["events"] if e["source"] == "worker" and e["kind"] == "activity"}
    return {g for g in generators if gate_message(g) in messages}


def wait_at_gate(service, build_id, generators=("geometry", "drafting"), timeout=30.0):
    """Wait until every named generator has announced that it is inside its gate loop."""
    return wait_until(service, lambda s: s["build"]["id"] == build_id
                      and gate_reached(s["build"], generators) == set(generators), timeout,
                      f"{', '.join(generators)} to reach the gate")


def command(service, kind, payload, revision=None):
    if revision is None:
        revision = service.state()["project"]["revision"]
    return service.command({"id": f"cmd-test-{kind}-{revision}-{time.monotonic_ns()}", "expected_revision": revision,
                            "type": kind, "payload": payload})


def open_gate(path):
    Path(path).write_text("open\n", encoding="utf-8")
