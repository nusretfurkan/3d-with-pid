#!/usr/bin/env python3
"""Reproducible pump-skid reference run with recorded evidence.

Usage, from a checkout (any working directory)::

    python scripts/reference.py --output DIR          # core lane: Python standard library only
    python scripts/reference.py --output DIR --cad    # core lane + real five-output service lane (CadQuery)

``DIR`` must not exist or must be an empty directory; nothing is ever
overwritten and nothing is written outside it. The repository fixture
``examples/pump_skid.json`` is only read: every lane works on its own copy.
No language model, network service, Blender or user project is used.

Core lane (``core/``): the real ``python -m skid.cli`` and ``python -m
skid.drafting`` commands, run as subprocesses with their exit statuses
recorded: copy the fixture, validate, list, refuse overwrite, refuse a stale
and a duplicate-tag edit, rename -> drafting -> undo (a new revision) ->
reload, an explicit nozzle move that invalidates its physical route (pending),
a P&ID symbol move that leaves physical data unchanged, and repeated drafting
of unchanged input. The 3D/CAD generators and the offline Blender bundle are
not exercised in this lane; ``status`` honestly reports ``unpublished``.

CAD lane (``cad/``, ``--cad``): the genuine ``InteractiveService`` with the
real generator suite (``skid.geometry`` + ``skid.drafting``) on an isolated
copy. Every build is awaited with a bound (``--timeout``). Checks: all five
outputs complete for the same project/revision/digest, the verified publish
pointer, the generator's STEP reimport verification plus an independent
CadQuery STEP reimport solid count, the exact legend and item index, a forced
rebuild of unchanged input reproducing geometry/source/mappings (not STEP
bytes), rename -> rebuild -> undo (new revision) -> save -> service restart
(reload) restoring semantics/layout/physical records, stale/conflict refusals,
and an explicit pending nozzle move that keeps the last complete publication
while the standalone geometry CLI exits 0 with a *pending* manifest (exit 0 is
not synchronization). Manual route and diagram records are compared exactly.

Writes ``DIR/evidence.json``: expected counts (``scripts/reference_expected.json``),
every command with its argv/exit status/expected status/output tails, every
check with its outcome, and SHA-256 of the key output files. Exit status: 0
every asserted contract held; 1 at least one check failed (all checks still
run where possible and are recorded); 2 usage or precondition error (for
example ``--cad`` without CadQuery, or a non-empty ``--output``).

This is a model-consistency reference, not a process, mechanical or
fabrication assessment; all fixture dimensions are labelled prototype
assumptions.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from skid.core import RevisionConflict, canonical_digest, load_document, save_document  # noqa: E402

FIXTURE = os.path.join(ROOT, "examples", "pump_skid.json")
EXPECTED = os.path.join(ROOT, "scripts", "reference_expected.json")
EVIDENCE_FORMAT = "skid-reference-evidence/1"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
TAIL = 1200
FIVE = ("display", "cad", "pid", "legend", "description")
RECORDS = ("model", "pid", "physical")
PUMP, TANK = "ent-pump", "ent-tank"
NEW_TAG = "P-201"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _records(document):
    project = document["current"]
    return {key: copy.deepcopy(project[key]) for key in RECORDS}


class CheckFailed(Exception):
    pass


class Evidence:
    """Collects commands and checks; a failed check is recorded, never hidden."""

    def __init__(self, output):
        self.output = os.path.realpath(output)
        self.commands = []
        self.checks = []
        self.lane = None
        self.files = {}

    def rel(self, path):
        return os.path.relpath(os.path.realpath(path), self.output)

    def inside(self, value):
        return isinstance(value, str) and os.path.isabs(value) and (
            os.path.commonpath([os.path.realpath(value), self.output]) == self.output)

    def check(self, name, ok, detail=None, fatal=False):
        entry = {"lane": self.lane, "name": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        self.checks.append(entry)
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark} [{self.lane}] {name}" + ("" if ok or detail is None else f": {_clip(detail)}"), flush=True)
        if not ok and fatal:
            raise CheckFailed(name)
        return bool(ok)

    def equal(self, name, actual, expected, fatal=False):
        ok = actual == expected
        return self.check(name, ok, None if ok else {"actual": actual, "expected": expected}, fatal)

    def run(self, argv, expect, label, timeout=600):
        """Run ``python -m ...`` style argv from the repository root and record it."""
        started = time.monotonic()
        try:
            done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                                  env=_child_env())
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = None, exc.stdout or "", f"timed out after {timeout} s"
            out = out if isinstance(out, str) else out.decode("utf-8", "replace")
        shown = [self.rel(a) if self.inside(a) else ("<python>" if a == sys.executable else a) for a in argv]
        record = {"lane": self.lane, "label": label, "argv": shown, "exit": code, "expected_exit": expect,
                  "seconds": round(time.monotonic() - started, 3), "stdout_tail": out[-TAIL:], "stderr_tail": err[-TAIL:]}
        self.commands.append(record)
        self.check(f"command {label}: exit {expect}", code == expect,
                   None if code == expect else {"exit": code, "stderr": err[-400:], "stdout": out[-400:]})
        return code, out, err

    def keep(self, key, path):
        if os.path.isfile(path):
            self.files[key] = {"path": self.rel(path), "bytes": os.path.getsize(path), "sha256": _sha256_file(path)}


def _clip(value, limit=300):
    text = value if isinstance(value, str) else _canonical(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _child_env():
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # no model is used; never pass an API key to children
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _write_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


# --- Drafting output checks (shared by both lanes) ------------------------------------------------


def _legend_and_index(ev, where, project, expected, prefix):
    """Exact legend: the keys the P&ID uses == legend entries == inventory keys; index == tagged items."""
    pid = open(os.path.join(where, "pid.svg"), encoding="utf-8").read()
    legend = open(os.path.join(where, "legend.svg"), encoding="utf-8").read()
    inventory = _read_json(os.path.join(where, "drawing.inventory.json"))
    index = _read_json(os.path.join(where, "item-index.json"))
    used = set()
    for keys in re.findall(r'data-legend-keys="([^"]*)"', pid):
        used.update(keys.split())
    entries = re.findall(r'data-legend-entry="([^"]*)"', legend)
    ev.check(f"{prefix}: legend has no duplicate entries", len(entries) == len(set(entries)), sorted(entries))
    ev.equal(f"{prefix}: legend entries == keys used on the P&ID", sorted(set(entries)), sorted(used))
    ev.equal(f"{prefix}: inventory keys == keys used on the P&ID", sorted(inventory["keys"]), sorted(used))
    ev.equal(f"{prefix}: legend key count", len(set(entries)), expected["legend_keys"])
    model = project["model"]
    tagged = {i: e["tag"] for i, e in model["entities"].items()}
    tagged.update({i: c["tag"] for i, c in model["connections"].items()})
    indexed = {item["semantic_id"]: item["tag"] for item in index["items"]}
    ev.equal(f"{prefix}: item index == every tagged entity and line (ID -> current tag)", indexed, tagged)
    rows = dict(re.findall(r'data-index-row="([^"]*)"[^>]*?data-tag="([^"]*)"', legend))
    ev.equal(f"{prefix}: legend-sheet index rows == item index", rows, indexed)


def _drafting_identity(ev, where, project, prefix, statuses=("complete",)):
    digest = canonical_digest(project)
    out = {}
    for artifact in ("pid", "legend", "description"):
        manifest = _read_json(os.path.join(where, f"{artifact}.manifest.json"))
        ev.equal(f"{prefix}: {artifact} manifest identity",
                 (manifest["project_id"], manifest["source_revision"], manifest["input_digest"]),
                 (project["id"], project["revision"], digest))
        ev.check(f"{prefix}: {artifact} status in {list(statuses)}", manifest["status"] in statuses, manifest["status"])
        out[artifact] = manifest
    return out


def _tag_texts(ev, where, prefix, present, absent):
    for name in ("pid.svg", "legend.svg", "description.html", "item-index.json"):
        text = open(os.path.join(where, name), encoding="utf-8").read()
        ev.check(f"{prefix}: {name} shows {present}", present in text)
        ev.check(f"{prefix}: {name} no longer shows {absent} as a tag",
                 f">{absent}<" not in text and f'"tag": "{absent}"' not in text and f'"tag":"{absent}"' not in text)


# --- Core lane --------------------------------------------------------------------------------------


def core_lane(ev, expected):
    ev.lane = "core"
    base = os.path.join(ev.output, "core")
    os.makedirs(base)
    project_path = os.path.join(base, "project.json")
    py = sys.executable
    cli = [py, "-m", "skid.cli"]

    ev.run(cli + ["sample", project_path], 0, "sample (copy fixture)")
    ev.run(cli + ["sample", project_path], 4, "sample refuses an existing target")
    code, out, _ = ev.run(cli + ["validate", project_path, "--json"], 0, "validate r0")
    if code == 0:
        report = json.loads(out)
        ev.equal("validate r0: valid, complete, revision 0, empty history",
                 (report["valid"], report["complete"], report["revision"], report["undo"], report["redo"]),
                 (True, True, 0, 0, 0))
    document = load_document(project_path)
    project = document["current"]
    model = project["model"]
    counts = {key: len(model[key]) for key in ("entities", "ports", "connections", "assumptions")}
    ev.equal("fixture counts", counts, expected["model"])
    ev.equal("fixture project ID", project["id"], expected["project_id"])
    baseline = _records(document)
    ids = {key: sorted(model[key]) for key in ("entities", "ports", "connections")}
    code, out, _ = ev.run(cli + ["lists", project_path, "--json"], 0, "lists r0")
    if code == 0:
        lists = json.loads(out)
        ev.equal("lists r0: one row per entity and line", (len(lists["entities"]), len(lists["lines"])),
                 (counts["entities"], counts["connections"]))

    def draft(name, expect=0):
        where = os.path.join(base, name)
        ev.run([py, "-m", "skid.drafting", "--project", project_path, "--out", where, "--quiet"], expect,
               f"drafting {name}")
        return where

    r0 = draft("drafting-r0")
    _drafting_identity(ev, r0, project, "drafting r0")
    _legend_and_index(ev, r0, project, expected, "drafting r0")
    repeat = draft("drafting-r0-repeat")
    for name in ("pid.svg", "legend.svg", "item-index.json", "drawing.inventory.json", "description.json",
                 "pid.manifest.json", "legend.manifest.json", "description.manifest.json"):
        ev.equal(f"drafting repeat: {name} identical for unchanged input",
                 _sha256_file(os.path.join(repeat, name)), _sha256_file(os.path.join(r0, name)))
    before = open(project_path, "rb").read()
    ev.run(cli + ["rename", project_path, PUMP, NEW_TAG, "--expected-revision", "5"], 4, "stale rename refused")
    ev.run(cli + ["rename", project_path, PUMP, "TK-101", "--expected-revision", "0"], 1, "duplicate tag refused")
    ev.check("refused edits left the file byte-identical", open(project_path, "rb").read() == before)

    ev.run(cli + ["rename", project_path, PUMP, NEW_TAG, "--expected-revision", "0"], 0, "rename r0 -> r1")
    document = load_document(project_path)
    project = document["current"]
    ev.equal("rename: revision 1", project["revision"], 1)
    renamed = _records(document)
    ev.equal("rename: same permanent IDs", {k: sorted(project["model"][k]) for k in ids}, ids)
    ev.equal("rename: P&ID layout record unchanged", renamed["pid"], baseline["pid"])
    ev.equal("rename: physical record unchanged", renamed["physical"], baseline["physical"])
    expected_model = copy.deepcopy(baseline["model"])
    expected_model["entities"][PUMP]["tag"] = NEW_TAG
    ev.check("rename: model differs only in the pump tag", renamed["model"] == expected_model)
    r1 = draft("drafting-r1")
    _drafting_identity(ev, r1, project, "drafting r1")
    _tag_texts(ev, r1, "drafting r1", NEW_TAG, "P-101")
    _legend_and_index(ev, r1, project, expected, "drafting r1")

    ev.run(cli + ["undo", project_path, "--expected-revision", "1"], 0, "undo r1 -> r2")
    code, out, _ = ev.run(cli + ["validate", project_path, "--json"], 0, "reload (new process) r2")
    if code == 0:
        report = json.loads(out)
        ev.equal("reload r2: revision is monotonic and history kept", (report["revision"], report["undo"],
                                                                         report["redo"]), (2, 0, 1))
    document = load_document(project_path)
    restored = _records(document)
    for key in RECORDS:
        ev.equal(f"undo + reload: {key} record restored exactly", _digest(restored[key]), _digest(baseline[key]))

    # Explicit nozzle move: the tank outlet height moves the outlet nozzle; its current route must be invalidated.
    outlet_routes = _tank_outlet_routes(document["current"])
    command = {"id": "cmd-reference-nozzle-move", "expected_revision": 2, "type": "set_parameter",
               "payload": {"entity_id": TANK, "name": "outlet_height",
                           "quantity": {"value": expected["nozzle_move"]["outlet_height_m"], "unit": "m",
                                        "assumption_id": "asm-tank-dimensions"}}}
    command_path = os.path.join(base, "nozzle-move.command.json")
    _write_json(command_path, command)
    ev.run(cli + ["apply", project_path, command_path], 0, "apply nozzle move r2 -> r3")
    document = load_document(project_path)
    project = document["current"]
    _route_invalidation_checks(ev, project, baseline, outlet_routes, "core nozzle move")
    ev.run(cli + ["validate", project_path], 3, "validate r3 reports valid but incomplete")
    r3 = draft("drafting-r3", expect=2)  # the open route item makes every drafting output pending (exit 2)
    _drafting_identity(ev, r3, project, "drafting r3", statuses=("pending",))

    symbol = baseline["pid"]["symbols"]["sym-pump"]
    move = {"id": "cmd-reference-move-symbol", "expected_revision": 3, "type": "move_symbol",
            "payload": {"symbol_id": "sym-pump", "x": symbol["x"] + 20.0, "y": symbol["y"],
                        "rotation_deg": symbol["rotation_deg"]}}
    move_path = os.path.join(base, "move-symbol.command.json")
    _write_json(move_path, move)
    physical_before = _records(load_document(project_path))["physical"]
    ev.run(cli + ["apply", project_path, move_path], 0, "apply P&ID symbol move r3 -> r4")
    after = load_document(project_path)["current"]
    ev.equal("P&ID symbol move: physical record unchanged", _digest(after["physical"]), _digest(physical_before))
    ev.check("P&ID symbol move: symbol is now a manual layout choice", after["pid"]["symbols"]["sym-pump"]["manual"]
             is True and after["pid"]["symbols"]["sym-pump"]["x"] == symbol["x"] + 20.0)
    ev.run(cli + ["status", project_path], 4, "status: no offline Blender bundle published (unpublished)")
    ev.keep("core/project.json", project_path)
    for name in ("pid.svg", "legend.svg", "item-index.json", "description.json"):
        ev.keep(f"core/drafting-r0/{name}", os.path.join(r0, name))


def _tank_outlet_routes(project):
    model = project["model"]
    ports = {p for p, port in model["ports"].items() if port["entity_id"] == TANK}
    return sorted(c for c, conn in model["connections"].items() if conn["from_port_id"] in ports
                  or conn["to_port_id"] in ports)


def _route_invalidation_checks(ev, project, baseline, moved, prefix):
    routes = project["physical"]["routes"]
    ev.check(f"{prefix}: the tank has connected routes to invalidate", bool(moved), moved)
    for cid in moved:
        route = routes[cid]
        ev.check(f"{prefix}: route {cid} explicitly not current", route["status"] in ("needs_reroute", "unresolved")
                 and bool(route.get("reason")), {k: route.get(k) for k in ("status", "reason")})
        ev.equal(f"{prefix}: route {cid} keeps its points and mode",
                 (route["points_m"], route["mode"]), (baseline["physical"]["routes"][cid]["points_m"],
                                                      baseline["physical"]["routes"][cid]["mode"]))
    for cid, route in baseline["physical"]["routes"].items():
        if cid not in moved:
            ev.equal(f"{prefix}: unrelated route {cid} unchanged", routes[cid], route)
    manual = [c for c, r in baseline["physical"]["routes"].items() if r["mode"] == "manual"]
    ev.check(f"{prefix}: fixture has a manual physical route to preserve", bool(manual), manual)
    ev.equal(f"{prefix}: placements unchanged", project["physical"]["placements"], baseline["physical"]["placements"])
    ev.equal(f"{prefix}: P&ID layout record unchanged (including manual symbols/edges)", _digest(project["pid"]),
             _digest(baseline["pid"]))


# --- CAD lane ---------------------------------------------------------------------------------------


class Service:
    """Thin wrapper over the genuine InteractiveService that awaits builds with a bound."""

    def __init__(self, ev, path, workflow, timeout):
        from skid.interactive.service import InteractiveService

        self.ev = ev
        self.timeout = timeout
        # researcher=False: no optional research; the default planner object is constructed but never run.
        self.svc = InteractiveService(path, workflow, timeout=timeout, terminate_grace=2.0, researcher=False)

    def wait(self, what):
        deadline = time.monotonic() + self.timeout + 60
        while True:
            state = self.svc.state()
            build = state["build"]
            idle = self.svc.active is None and self.svc.queued is None
            if build is not None and build["status"] not in ("queued", "running") and idle:
                return state
            if time.monotonic() > deadline:
                raise CheckFailed(f"{what}: no terminal build within {self.timeout + 60:g} s")
            time.sleep(0.1)

    def file(self, build_id, name):
        return self.svc.artifact_path(build_id, name)

    def close(self):
        self.svc.close()


def _published_checks(ev, svc, state, expected, prefix):
    """All five outputs complete with one identity; pointer verifies. Returns the stable digests of the set."""
    from skid.interactive import integrity

    build, published, project = state["build"], state["published"], state["project"]
    ev.equal(f"{prefix}: build complete", build["status"], "complete", fatal=True)
    ev.equal(f"{prefix}: every output complete", build["artifact_status"], dict.fromkeys(FIVE, "complete"))
    ev.check(f"{prefix}: published is this build at the current revision", published is not None and
             (published["build_id"], published["source_revision"]) == (build["id"], project["revision"]))
    ev.equal(f"{prefix}: sync state", state["sync"]["state"], "synchronized")
    pointer = _read_json(os.path.join(svc.svc.root, integrity.POINTER))
    ev.equal(f"{prefix}: publish pointer verifies", integrity.verify_pointer(svc.svc.root, pointer), [])
    identity = (project["id"], project["revision"], canonical_digest(project))
    build_id = build["id"]
    mappings = {}
    for artifact in FIVE:
        manifest = _read_json(svc.file(build_id, integrity.manifest_name(artifact)))
        ev.equal(f"{prefix}: {artifact} manifest identity and status",
                 (manifest["project_id"], manifest["source_revision"], manifest["input_digest"], manifest["status"]),
                 identity + ("complete",))
        mappings[artifact] = manifest["representations"]
    scene = _read_json(svc.file(build_id, "scene.json"))
    ev.equal(f"{prefix}: scene identity", (scene["project_id"], scene["source_revision"], scene["input_digest"]),
             identity)
    components = scene["components"]
    ev.equal(f"{prefix}: display components", len(components), expected["cad"]["display_components"])
    ev.check(f"{prefix}: display component IDs unique (no accumulated duplicates)",
             len({c["id"] for c in components}) == len(components))
    step = svc.file(build_id, "model.step")
    with open(step, "rb") as handle:
        ev.check(f"{prefix}: model.step is ISO 10303-21", handle.read(13) == b"ISO-10303-21;")
    verification = _read_json(svc.file(build_id, "cad.verification.json"))
    summary = verification["summary"]
    ev.check(f"{prefix}: generator STEP reimport verification ok", verification["ok"] is True and
             not verification["problems"], verification["problems"][:5])
    want = expected["cad"]
    ev.equal(f"{prefix}: STEP parts/solids", (summary["parts"], summary["solids"]), (want["parts"], want["solids"]))
    ev.equal(f"{prefix}: port flange faces", (summary["ports_checked"], summary["ports_on_flange_faces"]),
             (want["ports"], want["ports"]))
    ev.equal(f"{prefix}: envelope checks", (summary["envelope_checks"], summary["envelope_checks_passed"]),
             (want["envelope_checks"], want["envelope_checks"]))
    build_dir = os.path.dirname(svc.file(build_id, "scene.json"))
    _legend_and_index(ev, build_dir, project, expected, prefix)
    recipe = _read_json(svc.file(build_id, "geometry.recipe.json"))
    return {
        "build_id": build_id,
        "components": _digest([{k: v for k, v in c.items()} for c in components]),
        "recipe": _digest(recipe),
        "mappings": _digest(mappings),
        "verification_counts": {k: summary[k] for k in ("parts", "solids", "groups", "ports_checked",
                                                         "ports_on_flange_faces", "envelope_checks_passed")},
        "drafting": {name: _sha256_file(os.path.join(build_dir, name))
                     for name in ("pid.svg", "legend.svg", "item-index.json", "drawing.inventory.json")},
        "step": step,
    }


def _independent_step_reimport(ev, step, expected_solids, prefix):
    import cadquery as cq

    started = time.monotonic()
    shape = cq.importers.importStep(step)
    solids = sum(len(obj.Solids()) if hasattr(obj, "Solids") else 0 for obj in shape.vals())
    ev.equal(f"{prefix}: independent CadQuery STEP reimport solid count", solids, expected_solids)
    return round(time.monotonic() - started, 3)


def _expect_service_error(ev, name, fn, status, code=None):
    from skid.interactive.service import ServiceError

    try:
        fn()
    except ServiceError as exc:
        codes = [d.get("code") for d in exc.diagnostics]
        ok = exc.status == status and (code is None or code in codes)
        return ev.check(name, ok, None if ok else {"status": exc.status, "codes": codes, "error": exc.error})
    return ev.check(name, False, "accepted, expected a refusal")


def cad_lane(ev, expected, timeout):
    from skid.interactive import integrity

    ev.lane = "cad"
    base = os.path.join(ev.output, "cad")
    os.makedirs(base)
    path = os.path.join(base, "project.json")
    workflow = os.path.join(base, "project.workflow")
    save_document(path, load_document(FIXTURE), None)
    baseline = _records(load_document(path))
    timings = {}

    svc = Service(ev, path, workflow, timeout)
    try:
        started = time.monotonic()
        svc.svc.request_build("reference r0")
        state = svc.wait("r0 build")
        timings["build_r0_s"] = round(time.monotonic() - started, 3)
        first = _published_checks(ev, svc, state, expected, "r0")
        timings["step_reimport_s"] = _independent_step_reimport(ev, first["step"], expected["cad"]["solids"], "r0")
        state = svc.svc.build({})
        ev.check("r0: unchanged build request is not needed",
                 "build_not_needed" in {d["code"] for d in state["diagnostics"]})

        started = time.monotonic()
        svc.svc.build({"force": True})
        state = svc.wait("forced r0 rebuild")
        timings["rebuild_r0_s"] = round(time.monotonic() - started, 3)
        again = _published_checks(ev, svc, state, expected, "r0 forced rebuild")
        ev.check("rebuild: a new build was published", again["build_id"] != first["build_id"])
        for key in ("components", "recipe", "mappings", "verification_counts", "drafting"):
            ev.equal(f"rebuild of unchanged input: {key} identical", again[key], first[key])

        _expect_service_error(ev, "stale command refused (409 stale_revision)", lambda: svc.svc.command(
            {"id": "cmd-ref-stale", "expected_revision": 7, "type": "rename_entity",
             "payload": {"entity_id": PUMP, "tag": NEW_TAG}}), 409, "stale_revision")
        _expect_service_error(ev, "duplicate tag refused (400)", lambda: svc.svc.command(
            {"id": "cmd-ref-dup", "expected_revision": 0, "type": "rename_entity",
             "payload": {"entity_id": PUMP, "tag": "TK-101"}}), 400)
        ev.equal("refusals left revision 0", svc.svc.state()["project"]["revision"], 0)

        svc.svc.command({"id": "cmd-ref-rename", "expected_revision": 0, "type": "rename_entity",
                         "payload": {"entity_id": PUMP, "tag": NEW_TAG}})
        state = svc.wait("r1 build")
        renamed = _published_checks(ev, svc, state, expected, "r1 rename")
        build_dir = os.path.dirname(svc.file(renamed["build_id"], "scene.json"))
        _tag_texts(ev, build_dir, "r1 rename", NEW_TAG, "P-101")
        scene_text = open(os.path.join(build_dir, "scene.json"), encoding="utf-8").read()
        ev.check("r1 rename: 3D scene labels show the new tag", NEW_TAG in scene_text)
        ev.equal("r1 rename: mapping keys unchanged (identity preserved)",
                 sorted(_read_json(svc.file(renamed["build_id"], integrity.manifest_name("display")))
                        ["representations"]),
                 sorted(_read_json(svc.file(first["build_id"], integrity.manifest_name("display")))
                        ["representations"]))
        records = _records(load_document(path))
        ev.equal("r1 rename: P&ID and physical records unchanged", (records["pid"], records["physical"]),
                 (baseline["pid"], baseline["physical"]))

        svc.svc.command({"id": "cmd-ref-undo", "expected_revision": 1, "type": "undo", "payload": {}})
        state = svc.wait("r2 build")
        ev.equal("undo: new revision 2 (monotonic, not a decrement)", state["project"]["revision"], 2)
        undone = _published_checks(ev, svc, state, expected, "r2 undo")
        ev.equal("undo: display geometry identical to r0", undone["components"], first["components"])
        ev.equal("undo: mappings identical to r0", undone["mappings"], first["mappings"])
        _expect_service_error(ev, "stale build request refused", lambda: svc.svc.build({"expected_revision": 1}),
                              409, "stale_revision")
        _expect_service_error(ev, "stale save refused", lambda: svc.svc.save({"expected_revision": 1}), 409,
                              "stale_revision")
        state = svc.svc.save({"expected_revision": 2})
        ev.check("save r2 verified against disk", (state["document"]["saved"] or {}).get("revision") == 2)
        stale_copy = load_document(path)
        try:
            save_document(path, stale_copy, 1)
            ev.check("competing writer with an old base revision refused", False, "saved")
        except RevisionConflict:
            ev.check("competing writer with an old base revision refused", True)
        history = state["history"]
        pointer_r2 = open(os.path.join(workflow, integrity.POINTER), "rb").read()
    finally:
        svc.close()

    svc = Service(ev, path, workflow, timeout)  # reload: a new service process state on the same files
    try:
        state = svc.svc.state()
        ev.equal("reload: revision and history", (state["project"]["revision"], state["history"]["undo"],
                                                  state["history"]["redo"]), (2, history["undo"], history["redo"]))
        ev.equal("reload: published r2 still verified and current", (state["sync"]["state"],
                 (state["published"] or {}).get("build_id")), ("synchronized", undone["build_id"]))
        state = svc.svc.reopen({})
        ev.check("reopen of an unchanged synchronized file starts no build",
                 state["build"] is None and state["sync"]["state"] == "synchronized")
        records = _records(load_document(path))
        for key in RECORDS:
            ev.equal(f"undo + save + reload: {key} record restored exactly", _digest(records[key]),
                     _digest(baseline[key]))

        moved = _tank_outlet_routes(state["project"])
        svc.svc.command({"id": "cmd-ref-nozzle", "expected_revision": 2, "type": "set_parameter",
                         "payload": {"entity_id": TANK, "name": "outlet_height",
                                     "quantity": {"value": expected["nozzle_move"]["outlet_height_m"], "unit": "m",
                                                  "assumption_id": "asm-tank-dimensions"}}})
        state = svc.wait("r3 pending build")
        project = state["project"]
        ev.equal("nozzle move: build is pending, not complete", state["build"]["status"], "pending")
        ev.equal("nozzle move: sync state pending", state["sync"]["state"], "pending")
        ev.equal("nozzle move: last complete publication retained", (state["published"] or {}).get("build_id"),
                 undone["build_id"])
        ev.check("nozzle move: publish pointer bytes unchanged",
                 open(os.path.join(workflow, integrity.POINTER), "rb").read() == pointer_r2)
        ev.check("nozzle move: pending diagnostics explain the open items",
                 any(d["severity"] == "warning" for d in state["build"]["diagnostics"]))
        _route_invalidation_checks(ev, project, baseline, moved, "nozzle move")

        out = os.path.join(base, "geometry-cli-r3")
        ev.run([sys.executable, "-m", "skid.geometry", "build", path, out], 0, "standalone geometry CLI on r3",
               timeout=timeout)
        statuses = {a: _read_json(os.path.join(out, f"{a}.manifest.json"))["status"] for a in ("display", "cad")}
        ev.equal("standalone geometry CLI exit 0 still reports pending manifests (not synchronized)", statuses,
                 {"display": "pending", "cad": "pending"})
        ev.equal("service still pending after the CLI run", svc.svc.state()["sync"]["state"], "pending")

        svc.svc.command({"id": "cmd-ref-undo-nozzle", "expected_revision": 3, "type": "undo", "payload": {}})
        state = svc.wait("r4 build")
        final = _published_checks(ev, svc, state, expected, "r4 undo nozzle move")
        ev.equal("r4: display geometry identical to r0", final["components"], first["components"])
        records = _records(load_document(path))
        for key in RECORDS:
            ev.equal(f"r4: {key} record restored exactly", _digest(records[key]), _digest(baseline[key]))
        for key, name in (("scene.json", "scene.json"), ("model.step", "model.step"), ("pid.svg", "pid.svg"),
                          ("legend.svg", "legend.svg"), ("description.html", "description.html"),
                          ("cad.verification.json", "cad.verification.json")):
            ev.keep(f"cad/published-r4/{key}", svc.file(final["build_id"], name))
        ev.keep("cad/project.json", path)
    finally:
        svc.close()
    return timings


# --- Entry point ------------------------------------------------------------------------------------


def _prepare_output(path):
    target = os.path.abspath(path)
    if os.path.lexists(target) and (not os.path.isdir(target) or os.path.islink(target) or os.listdir(target)):
        raise SystemExit(f"error: --output {target} exists and is not an empty directory; nothing was written")
    examples = os.path.realpath(os.path.join(ROOT, "examples"))
    real = os.path.realpath(target)
    if os.path.commonpath([real, examples]) == examples:
        raise SystemExit("error: --output may not be inside examples/")
    os.makedirs(target, exist_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python scripts/reference.py", description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True, metavar="DIR", help="new or empty directory for all outputs")
    parser.add_argument("--cad", action="store_true", help="also run the real five-output service lane (CadQuery)")
    parser.add_argument("--timeout", type=float, default=900.0, metavar="S", help="per-build bound (default 900)")
    parser.add_argument("--expected", default=EXPECTED, metavar="JSON",
                        help="expected identity/counts (default scripts/reference_expected.json)")
    args = parser.parse_args(argv)
    if not args.timeout > 0:
        parser.error("--timeout must be positive")
    if args.cad:
        missing = [m for m in ("cadquery", "OCP") if importlib.util.find_spec(m) is None]
        if missing:
            print(f"error: --cad needs {', '.join(missing)} in this interpreter ({sys.executable}); install "
                  "requirements-cad.txt into a Python 3.12 virtual environment", file=sys.stderr)
            return EXIT_USAGE
    try:
        output = _prepare_output(args.output)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    with open(args.expected, encoding="utf-8") as handle:
        expected = json.load(handle)
    ev = Evidence(output)
    evidence = {"format": EVIDENCE_FORMAT, "started_at": _now(), "lanes": ["core"] + (["cad"] if args.cad else []),
                "python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
                "fixture_sha256": _sha256_file(FIXTURE), "expected": expected, "errors": [],
                "not_exercised": ["language-model planner/Ask/research", "Blender offline bundle and live Blender",
                                  "browser UI"] + ([] if args.cad else ["3D display and CAD/STEP generation"])}
    fixture_before = evidence["fixture_sha256"]
    lanes = [("core", lambda: core_lane(ev, expected))]
    if args.cad:
        lanes.append(("cad", lambda: evidence.setdefault("timings", {}).update(cad_lane(ev, expected, args.timeout)
                                                                              or {})))
    for name, lane in lanes:
        print(f"== {name} lane", flush=True)
        try:
            lane()
        except Exception as exc:  # noqa: BLE001 - recorded as a failure, never hidden
            ev.lane = name
            ev.check(f"{name} lane ran to completion", False, f"{type(exc).__name__}: {exc}")
            evidence["errors"].append({"lane": name, "error": f"{type(exc).__name__}: {exc}",
                                       "traceback": traceback.format_exc()[-4000:]})
    ev.lane = "all"
    ev.check("repository fixture unchanged", _sha256_file(FIXTURE) == fixture_before)
    failed = [c for c in ev.checks if not c["ok"]]
    evidence.update(finished_at=_now(), commands=ev.commands, checks=ev.checks, files=ev.files,
                    summary={"checks": len(ev.checks), "failed": len(failed), "commands": len(ev.commands)},
                    result="passed" if not failed else "failed")
    _write_json(os.path.join(output, "evidence.json"), evidence)
    print(f"{evidence['result'].upper()}: {len(ev.checks) - len(failed)}/{len(ev.checks)} checks; evidence "
          f"{os.path.join(output, 'evidence.json')}")
    return EXIT_OK if not failed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
