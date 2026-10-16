"""Command-line interface for the offline pump-skid workflow.

Run from the repository root with Python 3.11+ (Blender's bundled Python works)::

    python -m skid.cli new demo/blank.json --name "Blank study"
    python -m skid.cli sample demo/pump_skid.json
    python -m skid.cli validate demo/pump_skid.json
    python -m skid.cli lists demo/pump_skid.json
    python -m skid.cli rename demo/pump_skid.json ent-pump P-201 --expected-revision 0
    python -m skid.cli undo demo/pump_skid.json --expected-revision 1
    python -m skid.cli build demo/pump_skid.json --blender /path/to/blender
    python -m skid.cli status demo/pump_skid.json

``new`` writes a genuinely empty document (revision 0, no equipment, no
history) from ``skid.core.new_document``; without ``--project-id`` a stable
UUID-based ID is generated. Like ``sample`` it never overwrites: an existing
target (or dangling link) is refused with exit status 4.

Edits name permanent IDs and state the revision they are based on
(``--expected-revision``). The core rejects a stale revision and the save
rereads the file under its lock, so the CLI never overwrites another writer.
Undo/redo history and manual layout live in the document and are preserved.

Exit statuses: 0 success; 1 failure (structural errors, rejected command,
failed build, unreadable input); 2 usage error; 3 valid but incomplete (open
presentation items, a pending bundle); 4 conflict or stale (revision conflict,
existing target, source changed during a build, no current published bundle).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .build import (
    DEFAULT_TIMEOUT_S,
    build_lists,
    build_project,
    find_blender,
    parse_json,
    project_status,
)
from .core import CommandError, DocumentError, RevisionConflict, apply_command, load_document, save_document
from .core import new_document, validate_project

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_CONFLICT = 0, 1, 2, 3, 4
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "pump_skid.json")


class _Exit(Exception):
    def __init__(self, code):
        self.code = code


def _show(diagnostics, stream=None, limit=40):
    stream = stream or sys.stdout
    for d in diagnostics[:limit]:
        ids = f" ({', '.join(map(str, d.get('entity_ids') or []))})" if d.get("entity_ids") else ""
        print(f"  {str(d.get('severity', 'error')).upper():7} [{d.get('code')}] {d.get('message')}{ids}", file=stream)
    if len(diagnostics) > limit:
        print(f"  ... {len(diagnostics) - limit} more (use --json)", file=stream)


def _error(message, diagnostics=()):
    print(f"error: {message}", file=sys.stderr)
    _show(list(diagnostics), sys.stderr)


def _json(data):
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def _load(path):
    try:
        return load_document(path)
    except DocumentError as exc:
        _error(exc.message, exc.diagnostics)
        raise _Exit(EXIT_FAILED) from None


def _command_id():
    return f"cmd-{uuid.uuid4()}"


# --- Document commands -----------------------------------------------------------


def _sample(args):
    document = _load(FIXTURE)
    target = os.path.abspath(args.path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        save_document(target, document, None)
    except RevisionConflict as exc:
        _error(f"{exc.message}; nothing was written")
        return EXIT_CONFLICT
    project = document["current"]
    print(f"created {target}: project {project['id']} at revision {project['revision']} "
          "(a copy of the pump-skid fixture; all dimensions are prototype assumptions)")
    return EXIT_OK


def _new(args):
    project_id = args.project_id if args.project_id is not None else f"project-{uuid.uuid4()}"
    try:
        document = new_document(project_id, args.name)
    except DocumentError as exc:
        _error(f"{exc.message}; nothing was written", exc.diagnostics)
        return EXIT_USAGE
    target = os.path.abspath(args.path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        save_document(target, document, None)
    except RevisionConflict as exc:
        _error(f"{exc.message}; nothing was written")
        return EXIT_CONFLICT
    name = f" named {document['current']['name']!r}" if "name" in document["current"] else ""
    print(f"created {target}: empty project {project_id}{name} at revision 0 (no equipment, connections, "
          "assumptions or history; add items with validated commands)")
    return EXIT_OK


def _validate(args):
    try:
        document = load_document(args.path)
    except DocumentError as exc:
        if args.json:
            _json({"valid": False, "message": exc.message, "diagnostics": exc.diagnostics})
        else:
            print(f"INVALID: {exc.message}")
            _show(exc.diagnostics)
        return EXIT_FAILED
    project = document["current"]
    open_items = validate_project(project)  # loading succeeded, so only warnings (open items) remain
    if args.json:
        _json({"valid": True, "complete": not open_items, "project_id": project["id"],
               "revision": project["revision"], "undo": len(document["undo"]), "redo": len(document["redo"]),
               "diagnostics": open_items})
    else:
        model = project["model"]
        print(f"{os.path.abspath(args.path)}: project {project['id']} revision {project['revision']}; "
              f"{len(model['entities'])} entities, {len(model['connections'])} connections; "
              f"undo {len(document['undo'])}, redo {len(document['redo'])}")
        if open_items:
            print(f"VALID BUT INCOMPLETE: no structural errors; {len(open_items)} open item(s) (explicitly "
                  "unresolved required ports or unresolved presentation). A build will be published as pending.")
            _show(open_items)
        else:
            print("VALID: no structural errors and no open presentation items "
                  "(a model consistency check, not a process-design check).")
    return EXIT_INCOMPLETE if open_items else EXIT_OK


def _commit(path, command):
    document = _load(path)
    revision = document["current"]["revision"]
    try:
        new, diagnostics = apply_command(document, command)
    except CommandError as exc:
        stale = any(d.get("code") == "stale_revision" for d in exc.diagnostics)
        _error(f"{command.get('type')} rejected; {path} is unchanged at revision {revision}", exc.diagnostics)
        return EXIT_CONFLICT if stale else EXIT_FAILED
    if any(d.get("code") == "no_op" for d in diagnostics):
        print(f"no change: {command['type']} left revision {revision} as it was; nothing was written")
        return EXIT_OK
    try:
        save_document(path, new, revision)
    except RevisionConflict as exc:
        _error(f"{exc.message}; nothing was written", exc.diagnostics)
        return EXIT_CONFLICT
    except DocumentError as exc:
        _error(exc.message, exc.diagnostics)
        return EXIT_FAILED
    print(f"accepted {command['type']} {command['id']}: revision {revision} -> {new['current']['revision']}; "
          f"saved {os.path.abspath(path)} (undo {len(new['undo'])}, redo {len(new['redo'])})")
    open_items = [d for d in diagnostics if d.get("severity") == "warning"]
    if open_items:
        print(f"open items ({len(open_items)}):")
        _show(open_items)
    return EXIT_OK


def _apply(args):
    try:
        if args.command == "-":
            text = sys.stdin.read()
        else:
            with open(args.command, encoding="utf-8") as handle:
                text = handle.read()
        command = parse_json(text)
    except (OSError, ValueError) as exc:
        _error(f"cannot read the command {args.command}: {exc}")
        return EXIT_FAILED
    if not isinstance(command, dict):
        _error("the command must be a JSON object {id?, expected_revision?, type, payload, assumptions?}")
        return EXIT_USAGE
    if args.expected_revision is not None:
        if "expected_revision" in command and command["expected_revision"] != args.expected_revision:
            _error(f"--expected-revision {args.expected_revision} contradicts the command's "
                   f"expected_revision {command['expected_revision']!r}")
            return EXIT_USAGE
        command["expected_revision"] = args.expected_revision
    elif "expected_revision" not in command:
        _error("the command needs expected_revision, in the file or as --expected-revision")
        return EXIT_USAGE
    command.setdefault("id", _command_id())
    return _commit(args.path, command)


def _rename(args):
    return _commit(args.path, {"id": _command_id(), "expected_revision": args.expected_revision,
                               "type": "rename_entity", "payload": {"entity_id": args.entity_id, "tag": args.tag}})


def _history(kind):
    def run(args):
        return _commit(args.path, {"id": _command_id(), "expected_revision": args.expected_revision, "type": kind,
                                   "payload": {}})
    return run


# --- Lists -------------------------------------------------------------------------


def _quantity(name, quantity):
    if isinstance(quantity, dict) and "value" in quantity:
        marker = "*" if quantity.get("assumption_id") else ""
        return f"{name}={quantity['value']:g} {quantity.get('unit', '')}{marker}".rstrip()
    return f"{name}={quantity}"


def _lists(args):
    document = _load(args.path)
    lists = build_lists(document["current"])
    if args.json:
        _json(lists)
        return EXIT_OK
    print(f"Project {lists['project_id']} revision {lists['source_revision']}: lists derived from the current "
          "snapshot (IDs are permanent, tags editable)")
    print("\nEQUIPMENT AND INSTRUMENTS")
    for row in lists["entities"]:
        placement = row["placement"]
        where = "no placement" if placement is None else (
            f"placed at {placement['position_m']}" if placement["status"] == "placed" else "placement UNRESOLVED")
        details = [_quantity(name, value) for name, value in sorted(row["parameters"].items())]
        if row["observes"]:
            details.append(f"observes {row['observes']['entity_id']}.{row['observes']['variable']}")
        print(f"  {row['tag']:<9} {row['kind']:<17} {row['id']:<24} {where}; {', '.join(details)}")
    print("\nLINES")
    for row in lists["lines"]:
        route = row["route"]
        state = "no route" if route is None else f"route {route['status']} ({route['mode']})"
        ends = [f"{end['entity_tag']}.{end['port_name']}" for end in (row["from"], row["to"])]
        print(f"  {row['tag']:<7} {ends[0]:>16} -> {ends[1]:<16} DN{row['nominal_diameter_mm']:g} "
              f"OD {row['outside_diameter_m']:g} m*  {state}  {row['id']}")
    print("\n* prototype assumption; `lists --json` gives each assumption_id and record.")
    return EXIT_OK


# --- Build and status -------------------------------------------------------------


def _bundle_path(report, name):
    return os.path.join(report["out_dir"], "bundles", report["bundle"], name)


def _print_build(report):
    outcome, revision = report["outcome"], report["requested_revision"]
    if outcome in ("published", "unchanged"):
        how = {"unchanged": "already published, unchanged", "published": "published"}[outcome]
        if report["reused"] and outcome == "published":
            how = "published (reused the verified bundle)"
        print(f"{how}: project {report['project_id']} revision {revision} -> bundle {report['bundle']}")
        for label, name in (("P&ID", "pid.svg"), ("Blender", "model.blend"), ("lists", "lists.json"),
                            ("bundle", "bundle.json")):
            print(f"  {label:<8} {_bundle_path(report, name)}")
        print(f"  pointer  {os.path.join(report['out_dir'], 'current.json')}")
        if report["selected_ids"]:
            print(f"  highlighted selection: {', '.join(report['selected_ids'])}")
        if report["bundle_status"] == "complete":
            print("COMPLETE: both views were generated from this revision and their manifests verified.")
        else:
            print(f"PENDING: both views were generated from revision {revision}, but "
                  f"{len(report['open_items'])} open item(s) remain; the bundle is published as pending.")
            _show(report["open_items"])
        return
    print(f"{outcome.upper()}: nothing was published for revision {revision}.")
    _show(report["diagnostics"])
    published = report["published"]
    if published is None:
        print("No bundle has been published for this output directory.")
    else:
        relation = ("matches this snapshot" if published.get("input_digest") == report["input_digest"]
                    else "is stale relative to the source")
        print(f"Still published: bundle {published.get('bundle')} of revision {published.get('source_revision')} "
              f"({published.get('status')}), which {relation}.")


def _build(args):
    blender = find_blender(args.blender)
    if blender is None:
        hint = f"{args.blender!r} is not an executable file" if args.blender else "none on PATH"
        _error(f"no Blender executable ({hint}); pass --blender PATH or set SKID_BLENDER")
        return EXIT_FAILED
    report = build_project(args.path, args.out, blender=blender, selected_ids=tuple(args.select),
                           preview=args.preview, timeout=args.timeout)
    if args.json:
        _json(report)
    else:
        _print_build(report)
    if report["outcome"] in ("published", "unchanged"):
        return EXIT_OK if report["bundle_status"] == "complete" else EXIT_INCOMPLETE
    return EXIT_CONFLICT if report["outcome"] == "superseded" else EXIT_FAILED


_STATE_TEXT = {
    "synchronized": "SYNCHRONIZED: the published P&ID and Blender bundle match the current revision; both "
                    "manifests and file digests verify.",
    "pending": "PENDING: the published bundle matches the current revision but is explicitly pending.",
    "stale": "STALE: the published bundle is not of the current source; run build.",
    "unpublished": "UNPUBLISHED: no bundle has been published; run build.",
    "bundle_invalid": "INVALID BUNDLE: the published pointer or bundle does not verify.",
    "invalid_source": "INVALID SOURCE: the project document does not load.",
}
_STATE_EXIT = {"synchronized": EXIT_OK, "pending": EXIT_INCOMPLETE, "stale": EXIT_CONFLICT,
               "unpublished": EXIT_CONFLICT, "bundle_invalid": EXIT_FAILED, "invalid_source": EXIT_FAILED}


def _status(args):
    report = project_status(args.path, args.out)
    if args.json:
        _json(report)
        return _STATE_EXIT[report["state"]]
    source, published = report["source"], report["published"]
    if source:
        print(f"source     project {source['project_id']} revision {source['revision']} ({report['project_path']})")
    if published:
        selection = f", selection {published.get('selected_ids')}" if published.get("selected_ids") else ""
        print(f"published  bundle {published.get('bundle')}: project {published.get('project_id')} revision "
              f"{published.get('source_revision')}, {published.get('status')}{selection}")
    print(_STATE_TEXT[report["state"]])
    _show(report["problems"])
    if report["state"] in ("pending", "stale") and report["open_items"]:
        print(f"open items in the current source ({len(report['open_items'])}):")
        _show(report["open_items"])
    last = report["last_build"]
    if isinstance(last, dict) and last.get("outcome") not in ("published", "unchanged"):
        print(f"last build attempt: {last.get('outcome')} for revision {last.get('requested_revision')} "
              f"at {last.get('finished_at')}")
        _show(last.get("diagnostics") or [], limit=5)
    if report["staging"]:
        print(f"note: {len(report['staging'])} unpublished staging directory(ies) in "
              f"{os.path.join(report['out_dir'], 'staging')} (a build is running or was interrupted); left untouched.")
    return _STATE_EXIT[report["state"]]


# --- Entry point -----------------------------------------------------------------------


def _parser():
    parser = argparse.ArgumentParser(prog="python -m skid.cli", description="Offline pump-skid project workflow.")
    commands = parser.add_subparsers(dest="name", required=True, metavar="COMMAND")

    def command(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument("path", help="project document (JSON)")
        sub.set_defaults(handler=handler)
        return sub

    def revision(sub, required=True):
        sub.add_argument("--expected-revision", type=int, required=required, metavar="N",
                         help="revision the edit is based on; rejected if the document has moved on")

    new = command("new", _new, "create a new, empty project document; fails if the target exists")
    new.add_argument("--name", help="optional display name (the permanent project ID is separate)")
    new.add_argument("--project-id", metavar="ID", help="permanent project ID (default: project-<UUID4>)")
    command("sample", _sample, "create a new project from the pump-skid fixture; fails if the target exists")
    command("validate", _validate, "validate a document; exit 3 if valid but incomplete").add_argument(
        "--json", action="store_true")
    apply = command("apply", _apply, "apply one JSON command envelope from a file ('-' for stdin)")
    apply.add_argument("command", help="command JSON {id?, expected_revision?, type, payload, assumptions?}")
    revision(apply, required=False)
    rename = command("rename", _rename, "rename an entity by its permanent ID")
    rename.add_argument("entity_id")
    rename.add_argument("tag")
    revision(rename)
    revision(command("undo", _history("undo"), "undo the latest change (as a new revision)"))
    revision(command("redo", _history("redo"), "redo the latest undone change (as a new revision)"))
    build = command("build", _build, "generate both views and publish one revision bundle")
    build.add_argument("--out", help="output directory (default: <project stem>.skid beside the project)")
    build.add_argument("--blender", help="Blender executable (default: $SKID_BLENDER, then blender on PATH)")
    build.add_argument("--select", nargs="+", default=[], metavar="ID",
                       help="entity, port, or connection IDs to highlight in both generated views")
    build.add_argument("--preview", action="store_true", help="also render a Cycles CPU preview PNG (slow)")
    build.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Blender time limit in seconds")
    build.add_argument("--json", action="store_true")
    status = command("status", _status, "compare the current source with the published bundle")
    status.add_argument("--out", help="output directory (default: <project stem>.skid beside the project)")
    status.add_argument("--json", action="store_true")
    command("lists", _lists, "print entity and line lists from the current snapshot").add_argument(
        "--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except _Exit as exc:
        return exc.code
    except OSError as exc:
        _error(f"{type(exc).__name__}: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
