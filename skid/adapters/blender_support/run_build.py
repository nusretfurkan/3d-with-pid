# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 nusretfurkan
# Blender integration file: licensed under the GNU General Public License v3.0 or later,
# not the project's default MIT License. See LICENSES/GPL-3.0-or-later.txt and docs/LICENSING.md.
"""Blender command-line entry point: build one persisted project document.

Run from the repository root, where ``blender`` is the Blender executable for your platform::

    blender --background --factory-startup \\
        --python-exit-code 1 --python skid/adapters/blender_support/run_build.py -- \\
        --document examples/pump_skid.json --blend out/pump_skid.blend \\
        --manifest out/pump_skid.manifest.json [--preview out/pump_skid.png] [--select ID ...]

The document is loaded and validated by ``skid.core.load_document`` and its
current project is built with ``build_blender``. The manifest is written
atomically even when the build fails; a failed build then raises, so
``--python-exit-code`` reports it. ``--preview`` renders the result with Cycles
on the CPU unless the build failed. Output directories must already exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from skid.adapters.blender import build_blender  # noqa: E402
from skid.core import load_document  # noqa: E402

__all__ = ["main", "write_json"]


def _arguments(argv):
    parser = argparse.ArgumentParser(prog="run_build.py", description="Build a skid project into a blend file.")
    parser.add_argument("--document", required=True, help="persisted project document (JSON)")
    parser.add_argument("--blend", required=True, help="blend file to write")
    parser.add_argument("--manifest", required=True, help="manifest JSON to write")
    parser.add_argument("--preview", help="optional PNG preview to render")
    parser.add_argument("--select", nargs="*", default=[], help="entity, port or connection IDs to highlight")
    return parser.parse_args(argv)


def write_json(path: str, data) -> None:
    """Write JSON through a same-directory temporary file and an atomic rename."""
    target = os.path.abspath(path)
    temporary = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> dict:
    """Build, write the manifest, optionally render a preview, and return the manifest."""
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = _arguments(argv)
    document = load_document(args.document)
    manifest = build_blender(document["current"], args.blend, tuple(args.select))
    write_json(args.manifest, manifest)
    if args.preview and manifest["status"] != "failed":
        from skid.adapters.blender_support.preview import render_preview

        render_preview(manifest["project_id"], args.preview)
    return manifest


if __name__ == "__main__":
    result = main()
    print(f"skid blender build: {result['status']} at revision {result['source_revision']}")
    if result["status"] == "failed":
        raise RuntimeError("Blender build failed; see the manifest diagnostics")
