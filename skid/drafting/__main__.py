"""Command line: ``python -m skid.drafting --project PATH --out DIR [--notes NOTES.json]``.

Builds the drafting outputs for the current project of a persisted document
into a new directory. Exit status 0 is complete, 2 pending, 1 failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..core import DocumentError, load_document
from .builder import build_drafting


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m skid.drafting", description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="persisted project document (JSON)")
    parser.add_argument("--out", required=True, help="new output directory (created; must not hold drafting outputs)")
    parser.add_argument("--notes", help="manual-note sidecar (JSON)")
    parser.add_argument("--quiet", action="store_true", help="print only the final statuses")
    args = parser.parse_args(argv)
    try:
        document = load_document(args.project)
    except DocumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    notes = None
    if args.notes:
        with open(args.notes, encoding="utf-8") as handle:
            notes = json.load(handle)
    os.makedirs(args.out, exist_ok=True)

    def emit(event):
        if not args.quiet:
            progress = f" [{event['completed']}/{event['total']}]" if "completed" in event else ""
            path = f" -> {event['path']}" if "path" in event else ""
            print(f"{event['kind']:8} {event['artifact']:11} {event['message']}{progress}{path}")

    manifests = build_drafting(document["current"], args.out, emit=emit, manual_notes=notes)
    for adapter, manifest in manifests.items():
        print(f"{adapter}: {manifest['status']} ({len(manifest['diagnostics'])} diagnostic(s))")
    statuses = {m["status"] for m in manifests.values()}
    return 1 if "failed" in statuses else 2 if "pending" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
