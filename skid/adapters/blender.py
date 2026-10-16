# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Blender adapter: build a project snapshot into owned Blender data and a blend file.

``build_blender`` runs inside Blender. This module imports ``bpy`` only when
called, so it stays importable by plain Python. The adapter never writes
engineering JSON and never mutates the project or its manual points.

A build digests and validates the snapshot with the core, plans every object in
pure Python (``blender_support.plan``), realizes the plan in the running
session (``blender_support.scene``), and saves a copy of the session to
``blend_path`` through a same-directory temporary file and an atomic rename.

The manifest is ``{adapter, adapter_version, project_id, source_revision,
input_digest, status, diagnostics, representations}``. ``representations``
maps every entity, port, connection, and the project to the representation IDs
of its objects; an empty list means no 3D geometry at this revision. Status:

- ``complete``: every entity is placed and every route is current.
- ``pending``: valid topology, but some placement is unresolved or missing, or
  some route is stale, unresolved, or missing. Such equipment is omitted;
  retained stale points are drawn as red ghosts with warning labels, never as
  current piping.
- ``failed``: structural validation errors, unplannable geometry, ambiguous
  collection ownership, or a Blender build or save fault. ``blend_path`` is not
  replaced, nothing is mapped, and, except for a fault during the final swap
  (reported in the diagnostic), the previous owned objects are untouched.

Selection is transient: selected IDs are highlighted and selected in the view
layer, but never change the digest, revision, or engineering values.
"""

from __future__ import annotations

from skid.core.validation import canonical_digest

from .blender_support.plan import ADAPTER, ADAPTER_VERSION, diagnostic, manifest_from_plan, plan_scene

__all__ = ["ADAPTER", "ADAPTER_VERSION", "build_blender"]


def _scene_module():
    try:
        import bpy  # noqa: F401  (availability check only)
    except ImportError:
        raise RuntimeError("build_blender must run inside Blender; bpy is not available") from None
    from .blender_support import scene

    return scene


def _failed(plan, code, message):
    ids = [plan["project_id"]] if plan["project_id"] else []
    return manifest_from_plan(plan, "failed", [diagnostic(code, "error", ids, message)])


def build_blender(project: dict, blend_path: str, selected_ids: tuple[str, ...] = ()) -> dict:
    """Build ``project`` into this project's owned collection and save ``blend_path``; return the manifest."""
    scene = _scene_module()
    plan = plan_scene(project, selected_ids)
    if plan["status"] == "failed":
        return manifest_from_plan(plan)
    if not isinstance(blend_path, str) or not blend_path:
        return _failed(plan, "invalid_output_path", f"blend_path must be a nonempty string, not {blend_path!r}")
    manifest = manifest_from_plan(plan)
    try:
        scene.apply_plan(plan, manifest)
    except scene.OwnershipError as exc:
        return manifest_from_plan(plan, "failed", exc.diagnostics)
    except scene.SwapError as exc:
        return _failed(plan, "swap_failed", f"replacing the previous objects failed ({exc}); the session may "
                       f"hold a partial rebuild and {blend_path} was not written")
    except Exception as exc:  # report Blender faults in the manifest rather than escape the build
        return _failed(plan, "build_failed", f"Blender build failed ({type(exc).__name__}: {exc}); the previous "
                       f"objects are unchanged and {blend_path} was not written")
    try:
        unchanged = canonical_digest(project) == plan["input_digest"]
    except (TypeError, ValueError):
        unchanged = False
    if not unchanged:
        return _failed(plan, "input_mutated", "the project changed during the build; output not saved")
    try:
        scene.save_copy(blend_path)
    except Exception as exc:  # keep the previous file and report the fault
        return _failed(plan, "save_failed", f"could not save {blend_path} ({type(exc).__name__}: {exc}); "
                       "the previous file, if any, is unchanged")
    return manifest
