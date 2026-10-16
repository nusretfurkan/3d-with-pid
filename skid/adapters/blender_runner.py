# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Static entry script for the isolated Blender subprocess started by ``skid.build``.

``skid.build`` runs, as an argument array without a shell::

    <blender> --background --factory-startup --python-exit-code 1 \\
        --python <repo>/skid/adapters/blender_runner.py -- \\
        build --document SNAPSHOT --blend OUT.blend --manifest OUT.json [--preview OUT.png] [--select ID ...]

Startup path. ``--factory-startup`` ignores the user's startup file,
preferences, and add-ons, but factory startup still holds Blender's default
Cube, Camera, and Light. ``build`` therefore first reloads the factory settings
with an empty scene (``bpy.ops.wm.read_factory_settings(use_empty=True)``, as
``tests/blender_smoke.py`` does), checks that no object or collection remains,
and only then delegates to ``blender_support.run_build`` unchanged. A new bundle's
blend file thus holds only the project's generated collections. The reset
discards the open session, so never run this script inside a user's session;
there, call ``skid.adapters.blender.build_blender`` directly, which preserves
unrelated objects.

``inspect --blend FILE --report JSON`` opens a saved blend file and writes a
census of its objects, collections, and datablock counts, so a smoke test can
check the saved output instead of trusting the build's own report.

This script is part of the repository and executes no user-supplied code; its
arguments are only paths and IDs. Failures raise, so ``--python-exit-code 1``
reports them.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bpy  # noqa: E402

from skid.adapters.blender_support import run_build  # noqa: E402

CENSUS_PROPERTIES = ("skid_generator", "project_id", "representation_id", "entity_id", "connection_id",
                     "component_key", "source_revision", "skid_role")
CENSUS_COUNTS = ("objects", "meshes", "curves", "materials", "collections", "cameras", "lights", "worlds",
                 "images", "fonts", "scenes")


def empty_session() -> None:
    """Replace the session with an empty factory scene; raise if anything remains."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    leftovers = sorted(o.name for o in bpy.data.objects) + sorted(c.name for c in bpy.data.collections)
    if leftovers:
        raise RuntimeError(f"the empty factory scene still holds {leftovers}")


def build(argv) -> None:
    empty_session()
    manifest = run_build.main(argv)
    print(f"skid blender build: {manifest['status']} at revision {manifest['source_revision']}")
    if manifest["status"] == "failed":
        raise RuntimeError("Blender build failed; see the manifest diagnostics")


def _plain(value):
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def inspect(argv) -> None:
    parser = argparse.ArgumentParser(prog="blender_runner.py inspect")
    parser.add_argument("--blend", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(args.blend), load_ui=False)

    def record(block, **extra):
        return {"name": block.name, **extra, **{key: _plain(block.get(key)) for key in CENSUS_PROPERTIES}}

    census = {
        "blend": os.path.abspath(args.blend),
        "objects": sorted((record(o, type=o.type, collections=sorted(c.name for c in o.users_collection))
                           for o in bpy.data.objects), key=lambda r: r["name"]),
        "collections": sorted((record(c) for c in bpy.data.collections), key=lambda r: r["name"]),
        "counts": {name: len(getattr(bpy.data, name)) for name in CENSUS_COUNTS},
    }
    run_build.write_json(args.report, census)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    modes = {"build": build, "inspect": inspect}
    if not argv or argv[0] not in modes:
        raise RuntimeError(f"usage: blender_runner.py -- {{build|inspect}} ...; got {argv[:1]}")
    modes[argv[0]](argv[1:])


if __name__ == "__main__":
    main()
