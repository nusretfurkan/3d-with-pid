# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Render an inspectable preview of a project's owned collection with Cycles on the CPU.

The preview uses a temporary scene that links only the project's root
collection, plus its own orthographic camera, sun, floor, and world. Unrelated
user objects and the user's scene and render settings are untouched, and the
temporary data is removed afterwards, so a preview never alters a saved build.
The floor is a viewing aid at z = 0, not a modeled skid deck.
"""

from __future__ import annotations

import math
import os
import uuid

import bpy
from mathutils import Vector

from .scene import find_owned

__all__ = ["render_preview"]


def _view_axes(azimuth_deg, elevation_deg):
    """Return the unit vector from the target towards the camera; azimuth turns from -y towards +x."""
    a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
    return Vector((math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e)))


def _owned_points(owned):
    points = []
    for collection in owned.collections:
        for obj in collection.all_objects:
            if obj.type == "MESH":
                points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    return points


def _set_world_colour(world, rgb, strength):
    tree = world.node_tree
    if tree is None:
        try:
            world.use_nodes = True
        except (AttributeError, TypeError):
            pass
        tree = world.node_tree
    world.color = rgb
    if tree is None:
        return
    background = next((n for n in tree.nodes if n.type == "BACKGROUND"), None)
    if background is None:
        output = next((n for n in tree.nodes if n.type == "OUTPUT_WORLD"), None) or tree.nodes.new("ShaderNodeOutputWorld")
        background = tree.nodes.new("ShaderNodeBackground")
        tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    background.inputs["Color"].default_value = (*rgb, 1.0)
    background.inputs["Strength"].default_value = strength


def render_preview(project_id: str, png_path: str, *, resolution=(1600, 1000), samples=64,
                   azimuth_deg=-38.0, elevation_deg=25.0) -> str:
    """Render the project's generated objects to ``png_path``; return the absolute path."""
    owned = find_owned(project_id)
    if owned.root is None:
        raise LookupError(f"project {project_id!r} has no generated collection to preview")
    bpy.context.view_layer.update()
    points = _owned_points(owned)
    if not points:
        raise LookupError(f"project {project_id!r} has no generated meshes to preview")
    target = os.path.abspath(os.fspath(png_path))
    temporary = os.path.join(os.path.dirname(target), f".{uuid.uuid4().hex}.tmp.png")

    scene = bpy.data.scenes.new("Skid preview")
    made = []  # (store, datablock) removed in reverse order
    try:
        scene.collection.children.link(owned.root)
        low = Vector([min(p[i] for p in points) for i in range(3)])
        high = Vector([max(p[i] for p in points) for i in range(3)])
        centre = (low + high) / 2

        floor_mesh = bpy.data.meshes.new("Skid preview floor")
        made.append((bpy.data.meshes, floor_mesh))
        margin = 2.5
        x0, y0, x1, y1 = low.x - margin, low.y - margin, high.x + margin, high.y + margin
        floor_mesh.from_pydata([(x0, y0, -0.001), (x1, y0, -0.001), (x1, y1, -0.001), (x0, y1, -0.001)], [],
                               [(0, 1, 2, 3)])
        floor_material = bpy.data.materials.new("Skid preview floor")
        made.append((bpy.data.materials, floor_material))
        floor_colour = (0.32, 0.34, 0.37, 1.0)
        floor_material.diffuse_color = floor_colour
        tree = floor_material.node_tree
        if tree is None:
            try:
                floor_material.use_nodes = True
            except (AttributeError, TypeError):
                pass
            tree = floor_material.node_tree
        if tree is not None:
            node = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if node is not None:
                node.inputs["Base Color"].default_value = floor_colour
                node.inputs["Roughness"].default_value = 0.9
        floor_mesh.materials.append(floor_material)
        floor = bpy.data.objects.new("Skid preview floor", floor_mesh)
        made.append((bpy.data.objects, floor))
        scene.collection.objects.link(floor)

        sun_data = bpy.data.lights.new("Skid preview sun", type="SUN")
        made.append((bpy.data.lights, sun_data))
        sun_data.energy = 2.0
        sun_data.angle = math.radians(8.0)
        sun = bpy.data.objects.new("Skid preview sun", sun_data)
        made.append((bpy.data.objects, sun))
        scene.collection.objects.link(sun)
        sun.rotation_euler = (-_view_axes(azimuth_deg - 35.0, 55.0)).to_track_quat("-Z", "Y").to_euler()

        camera_data = bpy.data.cameras.new("Skid preview camera")
        made.append((bpy.data.cameras, camera_data))
        camera = bpy.data.objects.new("Skid preview camera", camera_data)
        made.append((bpy.data.objects, camera))
        scene.collection.objects.link(camera)
        towards_camera = _view_axes(azimuth_deg, elevation_deg)
        rotation = (-towards_camera).to_track_quat("-Z", "Y")
        right, up = rotation @ Vector((1.0, 0.0, 0.0)), rotation @ Vector((0.0, 1.0, 0.0))
        across = [(p - centre).dot(right) for p in points]
        upward = [(p - centre).dot(up) for p in points]
        aspect = resolution[0] / resolution[1]
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = 1.08 * max(max(across) - min(across), (max(upward) - min(upward)) * aspect)
        camera_data.clip_start = 0.01
        camera_data.clip_end = 200.0
        middle = centre + right * (max(across) + min(across)) / 2 + up * (max(upward) + min(upward)) / 2
        camera.location = middle + towards_camera * 30.0
        camera.rotation_euler = rotation.to_euler()
        scene.camera = camera

        world = bpy.data.worlds.new("Skid preview world")
        made.append((bpy.data.worlds, world))
        _set_world_colour(world, (0.80, 0.83, 0.87), 0.55)
        scene.world = world

        render = scene.render
        render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
        scene.cycles.max_bounces = 4
        render.resolution_x, render.resolution_y = resolution
        render.resolution_percentage = 100
        render.film_transparent = False
        render.image_settings.file_format = "PNG"
        render.image_settings.color_mode = "RGB"
        try:
            scene.view_settings.view_transform = "Standard"
        except TypeError:  # keep the default transform if Standard is unavailable
            pass
        render.filepath = temporary
        bpy.ops.render.render(write_still=True, scene=scene.name)
        if not os.path.isfile(temporary):
            raise RuntimeError(f"Blender did not write the preview {temporary}")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
        bpy.data.scenes.remove(scene)
        for store, block in reversed(made):
            try:
                store.remove(block)
            except ReferenceError:
                pass
    return target
