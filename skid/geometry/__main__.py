"""Command line: build project geometry, rebuild from a saved recipe, or compile an authored recipe.

    .venv-cad/bin/python -m skid.geometry build PROJECT.json OUT_DIR
    .venv-cad/bin/python -m skid.geometry rebuild OUT_DIR/geometry.recipe.json NEW_DIR   # unchanged snapshots only
    .venv-cad/bin/python -m skid.geometry recipe examples/recipes/NAME.recipe.json OUT_DIR [--set name=value ...]

``PROJECT.json`` may be a persisted document (its ``current`` project is used)
or a bare project. Output directories are created if missing and must be
empty. Events are printed as JSON lines; the exit status is 0 only when both
manifests are ``complete`` or ``pending``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _prepare(path):
    os.makedirs(path, exist_ok=True)
    if os.listdir(path):
        raise SystemExit(f"output directory {path} is not empty")
    return path


def _printer():
    start = time.monotonic()

    def emit(event):
        print(json.dumps({"t": round(time.monotonic() - start, 3), **event}), flush=True)

    return emit


def _report(result):
    ok = all(m["status"] in ("complete", "pending") for m in result.values())
    for name, manifest in result.items():
        print(f"{name}: {manifest['status']} ({len(manifest['diagnostics'])} diagnostics)")
        for item in manifest["diagnostics"]:
            print(f"  [{item['severity']}] {item['code']}: {item['message']}")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m skid.geometry", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build scene.json and model.step for a project")
    b.add_argument("project")
    b.add_argument("out")
    r = sub.add_parser("rebuild", help="rebuild from a saved geometry.recipe.json")
    r.add_argument("recipe")
    r.add_argument("out")
    c = sub.add_parser("recipe", help="compile an authored recipe (examples/recipes)")
    c.add_argument("recipe")
    c.add_argument("out")
    c.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="override a parameter in its declared unit (metres)")
    args = parser.parse_args(argv)

    if args.command == "build":
        from .build import build_geometry

        with open(args.project, encoding="utf-8") as handle:
            data = json.load(handle)
        project = data["current"] if isinstance(data, dict) and "current" in data else data
        return _report(build_geometry(project, _prepare(args.out), emit=_printer()))
    if args.command == "rebuild":
        from .build import rebuild_from_recipe
        from .snapshot import SnapshotError

        try:
            return _report(rebuild_from_recipe(args.recipe, _prepare(args.out), emit=_printer()))
        except SnapshotError as exc:
            print(f"snapshot rejected: {exc}", file=sys.stderr)
            return 2
    from .authored import build_authored
    from .recipes import RecipeError

    overrides = {}
    for item in args.set:
        name, _, value = item.partition("=")
        overrides[name] = float(value)
    try:
        return _report(build_authored(args.recipe, _prepare(args.out), overrides, emit=_printer()))
    except RecipeError as exc:
        print(f"recipe rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
