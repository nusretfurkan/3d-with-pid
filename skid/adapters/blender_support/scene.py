# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Realize an adapter plan in the running Blender session.

Ownership. A project's generated data lives in one local root collection
tagged ``skid_generator``, ``project_id`` and ``skid_role == "project"``, with
one tagged sub-collection per role in ``COLLECTION_ROLES``. The collections
persist across rebuilds, so their view-layer visibility survives. Generated
objects are the local objects linked directly into those collections that carry
the same ``skid_generator`` and ``project_id`` properties. Anything else is
preserved: user objects linked into the owned collections, user children of
generated objects, library-linked or overridden objects, and generated objects
copied into other collections. Names never establish ownership. If two local
collections claim the same project role, the build fails before any change.

Rebuild. Every new object is first created in an unlinked staging collection;
a failure there removes the new data and leaves the previous output untouched.
Then each previous object is remapped to the new object with the same
representation ID (``ID.user_remap``), so user parenting, constraints,
modifiers and extra collection links follow it. User children of objects with
no successor are unparented in place. Previous objects and the generated meshes
and text curves that become unused are removed, and the new objects are linked
into the owned collections under their stable names. Materials are reused per
project and role. Unrelated orphan data is never purged.
"""

from __future__ import annotations

import json
import math
import os
import uuid

import bpy

from . import geometry as geo
from .plan import ADAPTER, ADAPTER_VERSION, COLLECTION_ROLES, GENERATOR, MATERIAL_STYLES, diagnostic

__all__ = ["OwnershipError", "Owned", "SwapError", "apply_plan", "find_owned", "generated_objects", "save_copy"]

GENERATOR_PROPERTY = "skid_generator"
ROLE_PROPERTY = "skid_role"
PROJECT_ROLE = "project"


class OwnershipError(Exception):
    """More than one local collection claims the same project role."""

    def __init__(self, diagnostics):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(d["message"] for d in self.diagnostics))


class SwapError(Exception):
    """Replacing the previous objects failed after new ones were built; the session may be partial."""


class Owned:
    """The owned collections of one project and the generated objects in them."""

    def __init__(self, root, subs, objects):
        self.root = root
        self.subs = subs
        self.objects = objects

    @property
    def collections(self):
        return [c for c in (self.root, *self.subs.values()) if c is not None]


def _is_local(block) -> bool:
    return block.library is None and getattr(block, "override_library", None) is None


def _marked(block, project_id) -> bool:
    return block.get(GENERATOR_PROPERTY) == GENERATOR and block.get("project_id") == project_id


def is_generated(obj, project_id) -> bool:
    return _is_local(obj) and _marked(obj, project_id)


def find_owned(project_id: str) -> Owned:
    """Locate the project's owned collections and generated objects; raise ``OwnershipError`` if ambiguous."""
    roots, subs = [], {}
    for collection in bpy.data.collections:
        if not _is_local(collection) or not _marked(collection, project_id):
            continue
        role = collection.get(ROLE_PROPERTY)
        if role == PROJECT_ROLE:
            roots.append(collection)
        elif role in COLLECTION_ROLES:
            subs.setdefault(role, []).append(collection)
    problems = []
    for role, found in ((PROJECT_ROLE, roots), *sorted(subs.items())):
        if len(found) > 1:
            names = ", ".join(sorted(c.name for c in found))
            problems.append(diagnostic(
                "ambiguous_ownership", "error", [project_id],
                f"{len(found)} collections claim the {role!r} role of project {project_id!r} ({names}); "
                "remove or untag the copies before rebuilding",
            ))
    if problems:
        raise OwnershipError(problems)
    owned = Owned(roots[0] if roots else None, {role: found[0] for role, found in subs.items()}, [])
    seen = set()
    for collection in owned.collections:
        for obj in collection.objects:
            if is_generated(obj, project_id) and obj.as_pointer() not in seen:
                seen.add(obj.as_pointer())
                owned.objects.append(obj)
    owned.objects.sort(key=lambda o: o.name)
    return owned


def generated_objects(project_id: str) -> dict:
    """Return the project's generated objects keyed by representation ID."""
    return {obj["representation_id"]: obj for obj in find_owned(project_id).objects}


# --- Materials ---------------------------------------------------------------


def _linear(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]


def _principled(material):
    tree = material.node_tree
    if tree is None:
        try:
            material.use_nodes = True  # deprecated where node materials are always on
        except (AttributeError, TypeError):
            pass
        tree = material.node_tree
    if tree is None:
        return None
    for node in tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
    node = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(node.outputs["BSDF"], output.inputs["Surface"])
    return node


def _style(material, role):
    hex_color, roughness, metallic, emission = MATERIAL_STYLES[role]
    rgb = _linear(hex_color)
    material.diffuse_color = (*rgb, 1.0)
    material.roughness = roughness
    material.metallic = metallic
    node = _principled(material)
    if node is None:
        return
    values = {
        "Base Color": (*rgb, 1.0),
        "Roughness": roughness,
        "Metallic": metallic,
        "Emission Color": (*rgb, 1.0),
        "Emission Strength": emission,
    }
    for name, value in values.items():
        socket = node.inputs.get(name)
        if socket is not None:
            socket.default_value = value


def _ensure_materials(project_id, roles, created):
    found = {}
    for material in sorted(bpy.data.materials, key=lambda m: m.name):
        role = material.get("material_role")
        if _is_local(material) and _marked(material, project_id) and role in roles and role not in found:
            found[role] = material
    for role in sorted(roles):
        material = found.get(role)
        if material is None:
            material = bpy.data.materials.new(f"Skid {project_id} {role}")
            created.materials.append(material)
            material[GENERATOR_PROPERTY] = GENERATOR
            material["project_id"] = project_id
            material["material_role"] = role
            found[role] = material
        _style(material, role)
    return found


# --- Object creation ---------------------------------------------------------


class _Created:
    """New datablocks of an unfinished build, removed again if it fails."""

    def __init__(self):
        self.objects, self.meshes, self.curves, self.collections, self.materials = [], [], [], [], []


def _tag_data(data, spec):
    data[GENERATOR_PROPERTY] = GENERATOR
    data["project_id"] = spec["properties"]["project_id"]
    data["representation_id"] = spec["key"]


def _set_smooth(mesh, flags):
    try:
        mesh.polygons.foreach_set("use_smooth", flags)
    except (AttributeError, TypeError, RuntimeError):
        for polygon, flag in zip(mesh.polygons, flags):
            polygon.use_smooth = flag


def _create_object(spec, materials, staging, new_objects, created):
    data = None
    if spec["type"] == "mesh":
        data = bpy.data.meshes.new(spec["name"])
        created.meshes.append(data)
        mesh = geo.mesh_for(spec["mesh"])
        data.from_pydata(mesh["vertices"], [], mesh["faces"])
        _set_smooth(data, mesh["smooth"])
        data.update()
    elif spec["type"] == "text":
        data = bpy.data.curves.new(spec["name"], type="FONT")
        created.curves.append(data)
        text = spec["text"]
        data.body = text["body"]
        data.size = text["size"]
        data.align_x = text["align_x"]
        data.align_y = text["align_y"]
    elif spec["type"] != "empty":
        raise ValueError(f"unknown object type {spec['type']!r}")
    if data is not None:
        _tag_data(data, spec)
        if spec["material"] is not None:
            data.materials.append(materials[spec["material"]])
    obj = bpy.data.objects.new(spec["name"], data)
    created.objects.append(obj)
    staging.objects.link(obj)
    if data is None:
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = spec["empty_size"]
    obj.rotation_mode = "XYZ"
    if spec["parent"] is not None:
        obj.parent = new_objects[spec["parent"]]  # identity parent inverse: local = template frame
    obj.location = spec["location"]
    obj.rotation_euler = [math.radians(a) for a in spec["rotation_deg"]]
    for name, value in spec["properties"].items():
        obj[name] = value
    return obj


def _rollback(created):
    for blocks, store in (
        (created.objects, bpy.data.objects),
        (created.meshes, bpy.data.meshes),
        (created.curves, bpy.data.curves),
        (created.collections, bpy.data.collections),
        (created.materials, bpy.data.materials),
    ):
        for block in reversed(blocks):
            try:
                store.remove(block)
            except ReferenceError:  # already freed
                pass


# --- Commit ------------------------------------------------------------------


def _new_collection(name, project_id, role):
    collection = bpy.data.collections.new(name)
    collection[GENERATOR_PROPERTY] = GENERATOR
    collection["project_id"] = project_id
    collection[ROLE_PROPERTY] = role
    return collection


def _ensure_collections(owned, project_id, scene):
    root = owned.root
    if root is None:
        root = _new_collection(f"Skid {project_id}", project_id, PROJECT_ROLE)
    if root not in scene.collection.children_recursive:
        scene.collection.children.link(root)
    subs = {}
    for role in COLLECTION_ROLES:
        sub = owned.subs.get(role) or _new_collection(f"Skid {project_id} {role}", project_id, role)
        if sub not in root.children.values():
            root.children.link(sub)
        subs[role] = sub
    return root, subs


def _unparent_in_place(child):
    world = child.matrix_world.copy()
    child.parent = None
    child.matrix_world = world


def _purge_generated_data(project_id):
    for store in (bpy.data.meshes, bpy.data.curves):
        for block in list(store):
            if block.users == 0 and _is_local(block) and _marked(block, project_id):
                store.remove(block)


def _commit(plan, manifest, owned, staging, new_objects):
    project_id = plan["project_id"]
    scene = bpy.context.scene
    bpy.context.view_layer.update()  # current world matrices for user children
    root, subs = _ensure_collections(owned, project_id, scene)

    successors, superseded = {}, []
    for obj in owned.objects:  # sorted by name, so duplicates resolve deterministically
        key = obj.get("representation_id")
        if key in new_objects and key not in successors:
            successors[key] = obj
        else:
            superseded.append(obj)
    for obj in superseded:
        for child in obj.children:
            if not is_generated(child, project_id):
                _unparent_in_place(child)
    for key, obj in successors.items():
        obj.user_remap(new_objects[key])
    for obj in owned.objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    _purge_generated_data(project_id)

    owned_collections = [root, *subs.values()]
    for spec in plan["objects"]:
        obj = new_objects[spec["key"]]
        target = subs[spec["collection"]]
        linked = obj.users_collection
        if target not in linked:
            target.objects.link(obj)
        for collection in owned_collections:
            if collection != target and collection in linked:
                collection.objects.unlink(obj)
        staging.objects.unlink(obj)
    bpy.data.collections.remove(staging)
    for spec in plan["objects"]:
        obj = new_objects[spec["key"]]
        obj.name = spec["name"]
        if obj.data is not None:
            obj.data.name = spec["name"]

    for name, value in {
        GENERATOR_PROPERTY: GENERATOR,
        "project_id": project_id,
        ROLE_PROPERTY: PROJECT_ROLE,
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "source_revision": plan["source_revision"],
        "input_digest": plan["input_digest"],
        "status": manifest["status"],
        "manifest_json": json.dumps(manifest, sort_keys=True, ensure_ascii=False),
    }.items():
        root[name] = value

    for spec in plan["objects"]:
        if spec["properties"]["selected"]:
            try:
                new_objects[spec["key"]].select_set(True)
            except RuntimeError:  # not in the active view layer, for example an excluded collection
                pass


def apply_plan(plan: dict, manifest: dict) -> None:
    """Replace the project's generated objects with the planned ones.

    Raises ``OwnershipError`` (nothing changed), or re-raises a failure while
    creating new objects after removing them again (nothing changed). A failure
    during the final swap raises ``SwapError``; the session may then be partial.
    """
    project_id = plan["project_id"]
    owned = find_owned(project_id)
    created = _Created()
    new_objects = {}
    try:
        roles = {spec["material"] for spec in plan["objects"] if spec["material"] is not None}
        materials = _ensure_materials(project_id, roles, created)
        staging = bpy.data.collections.new(f"Skid {project_id} staging")
        created.collections.append(staging)
        for spec in plan["objects"]:
            new_objects[spec["key"]] = _create_object(spec, materials, staging, new_objects, created)
    except BaseException:
        _rollback(created)
        raise
    try:
        _commit(plan, manifest, owned, staging, new_objects)
    except Exception as exc:
        raise SwapError(f"{type(exc).__name__}: {exc}") from exc


def save_copy(blend_path: str) -> None:
    """Save the session to ``blend_path`` via a same-directory temporary file and atomic rename."""
    target = os.path.abspath(os.fspath(blend_path))
    directory, name = os.path.split(target)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"output directory {directory} does not exist")
    temporary = os.path.join(directory, f".{name}.{uuid.uuid4().hex}.tmp.blend")
    try:
        result = bpy.ops.wm.save_as_mainfile(filepath=temporary, copy=True, check_existing=False)
        if "FINISHED" not in result or not os.path.isfile(temporary):
            raise RuntimeError(f"Blender did not save {temporary} ({sorted(result)})")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
