# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Blender smoke test of the Blender adapter; runs inside Blender only.

    blender --background --factory-startup \\
        --python-exit-code 1 --python tests/blender_smoke.py

(``blender`` is the Blender executable for your platform.)

Builds the shared fixture into real Blender data and checks, on the Blender
objects themselves: stable representation keys, names and datablock counts over
unchanged rebuilds; nozzle ends and pipe endpoints against the core's world
port positions; exact semantic properties after a save and reload; survival of
unrelated user data (objects linked into the owned collections, user children
and constraint targets of generated objects, a name clash, an orphan mesh);
pending moved-equipment and unplaced-valve states and their resolution;
transient selection; and failed builds that leave the session and the previous
blend file untouched. Finally the reusable ``run_build`` entry point writes
clean fixture and pending blend files, manifests, and Cycles CPU previews to
``artifacts/blender-preview/``. Any failed check raises, so
``--python-exit-code 1`` reports it.
"""

import array
import copy
import hashlib
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

from skid.adapters.blender import build_blender  # noqa: E402
from skid.adapters.blender_support import run_build  # noqa: E402
from skid.adapters.blender_support import scene as skid_scene  # noqa: E402
from skid.adapters.blender_support.plan import GENERATOR, MANIFEST_FIELDS, plan_scene  # noqa: E402
from skid.core import save_document  # noqa: E402
from skid.core.validation import canonical_digest  # noqa: E402
from skid.templates import world_port_position  # noqa: E402
from tests.test_geometry_contract import (  # noqa: E402
    DOWNSTREAM,
    FIXTURE,
    MOVED_ROUTES,
    SPLIT,
    VALVE,
    insert_hv103,
    load_fixture,
    move_pump,
    place_hv103,
    reroute,
)

OUT = os.path.join(ROOT, "artifacts", "blender-preview")
SMOKE = os.path.join(OUT, "smoke")
PROJECT = "project-pump-skid"
TOLERANCE_M = 1e-6
COUNTED = ("objects", "meshes", "curves", "materials", "collections", "fonts", "cameras", "lights", "worlds", "scenes")

REPORT = {"checks": 0, "builds": [], "artifacts": {}}


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    REPORT["checks"] += 1


def build(project, blend_path, selected=(), note=""):
    started = time.perf_counter()
    manifest = build_blender(project, blend_path, selected)
    REPORT["builds"].append({"note": note, "status": manifest["status"], "revision": manifest["source_revision"],
                             "seconds": round(time.perf_counter() - started, 3)})
    check(tuple(manifest) == MANIFEST_FIELDS, f"{note}: manifest fields {tuple(manifest)}")
    return manifest


def generated():
    return skid_scene.generated_objects(PROJECT)


def counts(names=COUNTED):
    return {name: len(getattr(bpy.data, name)) for name in names}


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def plain(value):
    return value.to_list() if hasattr(value, "to_list") else value


def mapped_keys(manifest):
    return {key for keys in manifest["representations"].values() for key in keys}


def unused_generated_data():
    return [block.name for store in (bpy.data.meshes, bpy.data.curves) for block in store
            if block.get("skid_generator") == GENERATOR and block.users == 0]


def z_extent(obj):
    return max(v.co.z for v in obj.data.vertices)


def ring_centroid(obj, first, size):
    total = Vector((0.0, 0.0, 0.0))
    for vertex in obj.data.vertices[first:first + size]:
        total += obj.matrix_world @ vertex.co
    return total / size


def assert_close(actual, expected, message):
    gap = math.dist(tuple(actual), tuple(expected))
    check(gap <= TOLERANCE_M, f"{message}: {tuple(actual)} vs {tuple(expected)} ({gap:.3g} m)")


def assert_ports_and_routes(project, objects, note):
    """Compare real Blender nozzles and pipe pieces with the core's transformed port positions."""
    bpy.context.view_layer.update()
    model = project["model"]
    for port_id, port in model["ports"].items():
        expected = world_port_position(project, port_id)
        key = f"{port['entity_id']}#nozzle/{port['name']}"
        if expected is None:
            check(key not in objects, f"{note}: unplaced {port_id} has no nozzle object")
            continue
        nozzle = objects[key]
        check(nozzle["port_id"] == port_id, f"{note}: {key} carries port_id")
        assert_close(nozzle.matrix_world @ Vector((0.0, 0.0, z_extent(nozzle))), expected, f"{note}: nozzle end {port_id}")
    for cid, connection in model["connections"].items():
        route = project["physical"]["routes"][cid]
        pieces = sorted((obj for obj in objects.values() if obj.get("connection_id") == cid
                         and obj["component_key"].split("/")[0] in ("segment", "elbow", "joint")),
                        key=lambda o: (int(o["component_key"].split("/")[1]), o["component_key"].startswith("segment")))
        if route["status"] != "current":
            check(not pieces, f"{note}: non-current {cid} has no current piping objects")
            continue
        start = world_port_position(project, connection["from_port_id"])
        end = world_port_position(project, connection["to_port_id"])
        ends = []
        for piece in pieces:
            if piece["component_key"].startswith("segment/"):
                half = z_extent(piece)
                ends.append((piece.matrix_world @ Vector((0.0, 0.0, -half)), piece.matrix_world @ Vector((0.0, 0.0, half))))
            elif piece["component_key"].startswith("elbow/"):
                ring = piece["ring_vertices"]
                ends.append((ring_centroid(piece, 0, ring), ring_centroid(piece, len(piece.data.vertices) - ring, ring)))
            else:
                continue
            assert_close(ends[-1][0], piece["start_m"].to_list(), f"{note}: {piece['representation_id']} start")
            assert_close(ends[-1][1], piece["end_m"].to_list(), f"{note}: {piece['representation_id']} end")
        assert_close(ends[0][0], start, f"{note}: {cid} begins at its from port")
        assert_close(ends[-1][1], end, f"{note}: {cid} ends at its to port")
        for (_, previous), (following, _) in zip(ends, ends[1:]):
            assert_close(following, previous, f"{note}: {cid} pieces are continuous")


def assert_semantic_properties(plan, objects, note, clashes=()):
    specs = {spec["key"]: spec for spec in plan["objects"]}
    check(set(objects) == set(specs), f"{note}: generated keys {sorted(set(objects) ^ set(specs))} differ from the plan")
    for key, spec in specs.items():
        obj = objects[key]
        for name, value in spec["properties"].items():
            check(plain(obj.get(name)) == value, f"{note}: {key}[{name!r}] is {plain(obj.get(name))!r}, not {value!r}")
        extra = set(obj.keys()) - set(spec["properties"])
        check(not extra, f"{note}: {key} has unexpected properties {sorted(extra)}")
        expected_type = {"mesh": "MESH", "text": "FONT", "empty": "EMPTY"}[spec["type"]]
        check(obj.type == expected_type, f"{note}: {key} is {obj.type}")
        parent = obj.parent.get("representation_id") if obj.parent else None
        check(parent == spec["parent"], f"{note}: {key} parent {parent}")
        if key in clashes:
            check(obj.name.startswith(spec["name"] + "."), f"{note}: clashing {key} is named {obj.name}")
        else:
            check(obj.name == spec["name"], f"{note}: {key} is named {obj.name}, not {spec['name']}")
        if spec["material"] is not None:
            check(obj.data.materials[0].get("material_role") == spec["material"], f"{note}: {key} material")
        if spec["type"] == "text":
            check(obj.data.body == spec["text"]["body"], f"{note}: {key} text")


def make_sentinels():
    """Unrelated user data the adapter must preserve."""
    user = bpy.data.collections.new("User sentinels")
    bpy.context.scene.collection.children.link(user)
    user.objects.link(bpy.data.objects.new("Sentinel empty", None))
    mesh = bpy.data.meshes.new("Sentinel mesh")
    mesh.from_pydata([(0.0, 0.0, 0.0), (0.2, 0.0, 0.0), (0.0, 0.2, 0.0)], [], [(0, 1, 2)])
    user.objects.link(bpy.data.objects.new("ent-pump/body", mesh))  # takes a generated object's preferred name
    marked = bpy.data.objects.new("Marked copy", None)
    for name, value in {"skid_generator": GENERATOR, "project_id": PROJECT, "representation_id": "ent-pump#body",
                        "component_key": "body", "entity_id": "ent-pump"}.items():
        marked[name] = value  # carries generated markers but lives outside the owned collections
    user.objects.link(marked)
    bpy.data.materials.new("User material").use_fake_user = True
    bpy.data.meshes.new("User orphan mesh")


def assert_sentinels(note, reloaded=False):
    objects, collections = bpy.data.objects, bpy.data.collections
    for name in ("Cube", "Camera", "Light", "Sentinel empty", "ent-pump/body", "Marked copy"):
        check(name in objects, f"{note}: user object {name!r} survived")
    clash = objects["ent-pump/body"]
    check(clash.get("skid_generator") is None and clash.data.name == "Sentinel mesh", f"{note}: name clash object intact")
    check("User sentinels" in collections, f"{note}: user collection survived")
    check("User material" in bpy.data.materials, f"{note}: fake-user material survived")
    if not reloaded:  # zero-user data is legitimately dropped by Blender when saving
        check("User orphan mesh" in bpy.data.meshes, f"{note}: unrelated orphan mesh was not purged")


def main():
    os.makedirs(SMOKE, exist_ok=True)
    fixture_hash = sha256(FIXTURE)
    document = load_fixture()
    project = document["current"]
    pristine = copy.deepcopy(project)
    blend = os.path.join(SMOKE, "pump_skid_smoke.blend")
    REPORT["blender_version"] = bpy.app.version_string

    # A01/A10: first build, mapping, ports and routes on real objects.
    make_sentinels()
    first = build(project, blend, note="fixture build 1")
    check(first["status"] == "complete", f"fixture build status {first['status']}: {first['diagnostics']}")
    check(first["input_digest"] == canonical_digest(pristine) and first["source_revision"] == 0, "manifest provenance")
    check(not [d for d in first["diagnostics"] if d["severity"] == "error"], "no error diagnostics")
    check(project == pristine, "build left the input project unchanged")
    objects = generated()
    check(set(objects) == mapped_keys(first) and len(objects) == 76, f"76 generated objects, got {len(objects)}")
    check(objects["ent-pump#body"].name == "ent-pump/body.001", "clashing name resolved by suffix, not by deletion")
    assert_ports_and_routes(project, objects, "build 1")
    for eid, entity in project["model"]["entities"].items():
        check(objects[f"{eid}#label"].data.body.startswith(entity["tag"]), f"label of {eid} shows its tag")
    owned = skid_scene.find_owned(PROJECT)
    check(json.loads(owned.root["manifest_json"]) == first, "root collection stores the manifest")
    check(owned.root["source_revision"] == 0 and owned.root["input_digest"] == first["input_digest"], "root provenance")
    check(os.path.isfile(blend), "blend file written")
    assert_sentinels("build 1")

    # User work attached to generated data.
    user = bpy.data.collections["User sentinels"]
    owned.subs["equipment"].objects.link(bpy.data.objects["Sentinel empty"])
    child = bpy.data.objects.new("User child", None)
    user.objects.link(child)
    child.parent = objects["ent-pump#frame"]
    child.location = (0.1, 0.2, 0.3)
    tracker = bpy.data.objects.new("User tracker", None)
    user.objects.link(tracker)
    tracker.constraints.new("COPY_LOCATION").target = objects["ent-tank#body"]
    user.objects.link(objects["ent-tank#label"])
    bpy.context.view_layer.update()
    child_world = child.matrix_world.copy()
    before = counts()
    names = {key: obj.name for key, obj in objects.items()}

    # A10: unchanged rebuild.
    second = build(project, blend, note="fixture build 2")
    check(second == first, "unchanged rebuild returns an identical manifest")
    objects = generated()
    check(counts() == before, f"datablock counts stable: {before} -> {counts()}")
    check({key: obj.name for key, obj in objects.items()} == names, "generated names stable")
    check(not unused_generated_data(), f"no unused generated meshes or curves: {unused_generated_data()}")
    equipment = skid_scene.find_owned(PROJECT).subs["equipment"]
    sentinel = bpy.data.objects["Sentinel empty"]
    check(equipment in sentinel.users_collection and user in sentinel.users_collection,
          "user object linked into the owned collection survived in both collections")
    bpy.context.view_layer.update()
    check(child.parent == objects["ent-pump#frame"], "user child follows the rebuilt pump frame")
    check(all(abs(a - b) < 1e-9 for ra, rb in zip(child.matrix_world, child_world) for a, b in zip(ra, rb)),
          "user child kept its world transform")
    check(tracker.constraints[0].target == objects["ent-tank#body"], "user constraint retargeted to rebuilt tank")
    check(user in objects["ent-tank#label"].users_collection, "user collection link of a generated label kept")
    assert_sentinels("build 2")

    # Save, reload, inspect exact semantic properties, rebuild.
    bpy.ops.wm.open_mainfile(filepath=blend)
    objects = generated()
    expected_plan = plan_scene(pristine)
    assert_semantic_properties(expected_plan, objects, "reloaded", clashes={"ent-pump#body"})
    assert_ports_and_routes(pristine, objects, "reloaded")
    owned = skid_scene.find_owned(PROJECT)
    check(json.loads(owned.root["manifest_json"]) == first, "reloaded manifest matches")
    check(set(owned.subs) == {"equipment", "piping", "annotations"}, "owned sub-collections reloaded")
    check(bpy.data.objects["User child"].parent == objects["ent-pump#frame"], "reloaded user child parent")
    check(bpy.data.objects["User tracker"].constraints[0].target == objects["ent-tank#body"], "reloaded constraint")
    assert_sentinels("reloaded", reloaded=True)
    reloaded = counts()
    third = build(project, blend, note="fixture build 3 after reload")
    check(third == first and counts() == reloaded, "rebuild after reload is stable")
    check({key: obj.name for key, obj in generated().items()} == names, "names stable after reload")
    REPORT["fixture_counts"] = {"generated_objects": len(generated()), **counts(("objects", "meshes", "curves", "materials"))}
    REPORT["representations"] = {"semantic_ids": len(first["representations"]), "object_keys": len(mapped_keys(first))}

    # Transient selection.
    selected = build(project, os.path.join(SMOKE, "pump_skid_selected.blend"), ("ent-pump", "no-such-id"),
                     note="selection")
    objects = generated()
    check(selected["status"] == "complete" and selected["representations"] == first["representations"],
          "selection keeps status and mapping")
    check("selection_unknown" in {d["code"] for d in selected["diagnostics"]}, "unknown selection reported")
    check(objects["ent-pump#body"].select_get() and objects["ent-pump#body"]["selected"], "pump selected")
    check(objects["ent-pump#motor"].data.materials[0]["material_role"] == "selected", "pump highlighted")
    check(not objects["ent-tank#body"].select_get(), "tank not selected")
    plain_again = build(project, blend, note="selection cleared")
    check(plain_again == first and generated()["ent-pump#motor"].data.materials[0]["material_role"] == "motor",
          "selection is transient")
    stable = counts(("objects", "meshes", "curves", "collections"))

    # A05: moved equipment leaves incident routes visibly stale until rerouted.
    moved = move_pump(document)
    moved_points = copy.deepcopy(moved["current"]["physical"]["routes"])
    pending = build(moved["current"], os.path.join(SMOKE, "pump_skid_moved.blend"), note="pump moved")
    objects = generated()
    check(pending["status"] == "pending", f"moved pump is pending, got {pending['status']}")
    check({d["entity_ids"][0] for d in pending["diagnostics"] if d["code"] == "route_flagged"} == set(MOVED_ROUTES),
          "both incident routes flagged")
    check(moved["current"]["physical"]["routes"] == moved_points, "build preserved retained route points")
    assert_ports_and_routes(moved["current"], objects, "moved")
    for cid in MOVED_ROUTES:
        ghost = objects[f"{cid}#stale/segment/0"]
        check(ghost["route_status"] == "needs_reroute" and ghost.data.materials[0]["material_role"] == "stale",
              f"{cid} ghost marked stale")
        assert_close(ghost["start_m"].to_list(), moved_points[cid]["points_m"][0], f"{cid} ghost uses retained points")
        check(objects[f"{cid}#label"].data.body.endswith(" REROUTE"), f"{cid} warning label")
    check("PENDING" in objects[f"{PROJECT}#status"].data.body, "status board shows pending")
    check(len(objects) == len(plan_scene(moved["current"])["objects"]), "moved build object count matches plan")
    rerouted = reroute(moved, MOVED_ROUTES)
    resolved = build(rerouted["current"], os.path.join(SMOKE, "pump_skid_rerouted.blend"), note="pump rerouted")
    check(resolved["status"] == "complete", "explicit reroute completes the build")
    assert_ports_and_routes(rerouted["current"], generated(), "rerouted")

    # A04: inserted valve without placement, then resolved.
    inserted = insert_hv103(document)
    pending = build(inserted["current"], os.path.join(SMOKE, "pump_skid_inserted.blend"), note="valve inserted")
    objects = generated()
    check(pending["status"] == "pending", "unplaced valve is pending")
    for semantic_id in (VALVE, DOWNSTREAM, "port-hv-103-inlet", "port-hv-103-outlet"):
        check(pending["representations"][semantic_id] == [], f"{semantic_id} explicitly unmapped")
    check(not [o for o in objects.values() if o.get("entity_id") == VALVE], "no invented HV-103 geometry")
    check(objects[f"{SPLIT}#label"].data.body == "L-104 UNRESOLVED", "split route flagged")
    check("HV-103: placement unresolved" in objects[f"{PROJECT}#status"].data.body, "status board lists HV-103")
    placed = place_hv103(inserted)
    resolved = build(placed["current"], os.path.join(SMOKE, "pump_skid_valve_placed.blend"), note="valve placed")
    objects = generated()
    check(resolved["status"] == "complete" and objects[f"{VALVE}#label"].data.body == "HV-103", "placed valve built")
    assert_ports_and_routes(placed["current"], objects, "valve placed")

    # Back to the fixture: no leftovers from the pending states.
    restored = build(project, blend, note="fixture restored")
    check(restored == first and set(generated()) == mapped_keys(first), "fixture keys restored")
    check(counts(("objects", "meshes", "curves", "collections")) == stable, "no accumulated objects or data")
    check(not unused_generated_data(), "no unused generated data after pending states")

    # Failures keep the previous objects and blend file.
    file_hash, snapshot = sha256(blend), counts()
    names = {key: obj.name for key, obj in generated().items()}
    broken = copy.deepcopy(project)
    broken["model"]["entities"]["ent-pump"]["tag"] = "TK-101"
    failed = build(broken, blend, note="duplicate tag")
    check(failed["status"] == "failed" and failed["representations"] == {}, "invalid project fails")
    check("duplicate_tag" in {d["code"] for d in failed["diagnostics"]}, "duplicate tag reported")
    check(sha256(blend) == file_hash and counts() == snapshot, "invalid project changed nothing")

    original_create, calls = skid_scene._create_object, []

    def faulty(*args):
        calls.append(1)
        if len(calls) > 30:
            raise RuntimeError("injected fault while creating objects")
        return original_create(*args)

    skid_scene._create_object = faulty
    try:
        failed = build(project, blend, note="injected fault")
    finally:
        skid_scene._create_object = original_create
    check(failed["status"] == "failed" and failed["diagnostics"][-1]["code"] == "build_failed", "fault reported")
    check(counts() == snapshot and sha256(blend) == file_hash, "fault rolled back every new datablock")
    check({key: obj.name for key, obj in generated().items()} == names, "previous objects kept after fault")

    impostor = bpy.data.collections.new("User copy of skid root")
    for name in ("skid_generator", "project_id", "skid_role"):
        impostor[name] = skid_scene.find_owned(PROJECT).root[name]
    failed = build(project, blend, note="ambiguous ownership")
    check(failed["status"] == "failed" and failed["diagnostics"][-1]["code"] == "ambiguous_ownership",
          "ambiguous ownership refused")
    bpy.data.collections.remove(impostor)
    check(counts() == snapshot and sha256(blend) == file_hash, "ambiguous ownership changed nothing")

    failed = build(project, os.path.join(SMOKE, "missing-directory", "out.blend"), note="unwritable path")
    check(failed["status"] == "failed" and failed["diagnostics"][-1]["code"] == "save_failed", "save failure reported")
    check(project == pristine and sha256(FIXTURE) == fixture_hash, "input and fixture file untouched")

    # Reusable entry point in clean sessions: fixture and pending artifacts with previews.
    images = {}
    for name, path in (("fixture", FIXTURE), ("pending", os.path.join(OUT, "pump_skid_pending.json"))):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        if name == "pending":
            if os.path.exists(path):
                os.unlink(path)
            save_document(path, move_pump(insert_hv103(document)), None)
        stem = os.path.join(OUT, "pump_skid" if name == "fixture" else "pump_skid_pending")
        started = time.perf_counter()
        manifest = run_build.main(["--document", path, "--blend", stem + ".blend", "--manifest",
                                   stem + ".manifest.json", "--preview", stem + "_preview.png"])
        REPORT["builds"].append({"note": f"run_build {name} with preview", "status": manifest["status"],
                                 "revision": manifest["source_revision"],
                                 "seconds": round(time.perf_counter() - started, 3)})
        with open(stem + ".manifest.json", encoding="utf-8") as handle:
            check(json.load(handle) == manifest, f"{name} manifest file matches")
        check(manifest["status"] == ("complete" if name == "fixture" else "pending"), f"{name} status")
        image = bpy.data.images.load(stem + "_preview.png")
        width, height = image.size
        pixels = array.array("f", [0.0]) * (width * height * 4)
        image.pixels.foreach_get(pixels)
        bpy.data.images.remove(image)
        check((width, height) == (1600, 1000), f"{name} preview size {width}x{height}")
        samples = [pixels[i:i + 3] for i in range(0, len(pixels), 4 * 97)]
        luminance = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in samples]
        mean = sum(luminance) / len(luminance)
        images[name] = {
            "distinct_colours": len({tuple(round(c * 16) for c in s) for s in samples}),
            "luminance_mean": round(mean, 4),
            "luminance_stddev": round(math.sqrt(sum((v - mean) ** 2 for v in luminance) / len(luminance)), 4),
            "red_fraction": round(sum(1 for s in samples if s[0] > 0.6 and s[1] < 0.3 and s[2] < 0.3) / len(samples), 5),
        }
        print(f"blender smoke: {name} preview statistics {images[name]}")
        check(images[name]["distinct_colours"] > 40 and images[name]["luminance_stddev"] > 0.08,
              f"{name} preview has visible content: {images[name]}")
        REPORT["artifacts"][name] = {"blend": stem + ".blend", "manifest": stem + ".manifest.json",
                                     "preview": stem + "_preview.png", "objects": len(generated())}
    check(images["pending"]["red_fraction"] > images["fixture"]["red_fraction"], "pending preview shows more red flags")
    REPORT["preview_statistics"] = images

    report_path = os.path.join(OUT, "smoke_report.json")
    run_build.write_json(report_path, REPORT)
    print(f"blender smoke: {REPORT['checks']} checks passed; report {report_path}")


main()
