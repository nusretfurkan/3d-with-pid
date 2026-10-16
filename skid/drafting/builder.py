"""``build_drafting``: P&ID sheet, exact legend, item index and process description from one snapshot.

Outputs in ``output_dir`` (which must exist and hold no earlier drafting outputs):

- ``preview/pid-NNNN.svg`` and ``preview/legend-NNNN.svg``: immutable working
  previews written as each unit of drawing work completes;
- ``pid.svg`` and ``drawing.inventory.json``, then ``pid.manifest.json``;
- ``legend.svg`` and ``item-index.json``, then ``legend.manifest.json``;
- ``description.json`` and ``description.html``, then ``description.manifest.json``.

Structural validation errors produce failed manifests and no drawings. Any
warning (unresolved presentation, sheet findings, manual-note conflicts) makes
the affected manifest ``pending``; only a warning-free artifact is ``complete``.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from ..core.validation import canonical_digest, project_name_problem, validate_project
from . import description as desc
from . import inventory as inv
from . import legend as leg
from . import sheet as pid_sheet
from .graph import FlowGraph

__all__ = ["OUTPUT_FILES", "PREVIEW_DIR", "build_drafting"]

PREVIEW_DIR = "preview"
PREVIEW_NAME = re.compile(r"(pid|legend)-\d{4,}\.svg")
OUTPUT_FILES = (
    "pid.svg",
    "drawing.inventory.json",
    "pid.manifest.json",
    "legend.svg",
    "item-index.json",
    "legend.manifest.json",
    "description.json",
    "description.html",
    "description.manifest.json",
)
ADAPTERS = {
    "pid": pid_sheet.ADAPTER_VERSION,
    "legend": leg.ADAPTER_VERSION,
    "description": desc.ADAPTER_VERSION,
}


def _diagnostic(code, severity, ids, message):
    return {"code": code, "severity": severity, "entity_ids": [i for i in dict.fromkeys(ids) if isinstance(i, str) and i],
            "message": message}


def _ordered(diagnostics):
    unique, seen = [], set()
    for item in diagnostics:
        key = (item["code"], item["severity"], tuple(item["entity_ids"]), item["message"])
        if key not in seen:
            seen.add(key)
            unique.append(copy.deepcopy(item))
    return sorted(unique, key=lambda d: (d["severity"] != "error", d["code"], d["message"], tuple(d["entity_ids"])))


def _status(diagnostics):
    if any(d["severity"] == "error" for d in diagnostics):
        return "failed"
    return "pending" if diagnostics else "complete"


def _thaw(value):
    """Plain JSON-shaped copy of a (possibly read-only) project snapshot."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _identity(project):
    if not isinstance(project, dict):
        return None, None
    project_id, revision = project.get("id"), project.get("revision")
    if not (isinstance(project_id, str) and project_id.strip()):
        project_id = None
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        revision = None
    return project_id, revision


def _project_name(project):
    """The stored display name, or None for an unnamed (legacy) project; titles then show the stable id."""
    name = project.get("name") if isinstance(project, dict) else None
    return name if name is not None and project_name_problem(name) is None else None


def _json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _write(path: Path, data: bytes, immutable=False):
    """Write through a same-directory temporary file; immutable files are never replaced."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if not immutable:
        os.replace(temporary, path)
        return
    try:
        os.link(temporary, path)  # fails if the path exists; never overwrites
    except FileExistsError:
        raise
    except OSError:  # no hard links on this filesystem: exclusive create instead
        with open(path, "xb") as handle:
            handle.write(data)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class _Events:
    def __init__(self, emit):
        self.emit = emit

    def send(self, kind, artifact, message, path=None, completed=None, total=None):
        if self.emit is None:
            return
        event = {"kind": kind, "artifact": artifact, "message": message}
        if path is not None:
            event["path"] = path
        if completed is not None:
            event["completed"] = completed
            event["total"] = total
        self.emit(event)


def _check(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("drafting generation was cancelled")


def _prepare_output(output_dir):
    """Check that no drafting output exists yet; other generators may share the directory and ``preview/``."""
    out = Path(output_dir)
    if not out.is_dir():
        raise NotADirectoryError(f"drafting output directory {str(out)!r} does not exist")
    present = [name for name in OUTPUT_FILES if (out / name).exists()]
    preview = out / PREVIEW_DIR
    if preview.exists() and not preview.is_dir():
        present.append(PREVIEW_DIR)
    elif preview.is_dir():
        present += sorted(f"{PREVIEW_DIR}/{p.name}" for p in preview.iterdir()
                          if PREVIEW_NAME.fullmatch(p.name))
    if present:
        raise FileExistsError(f"drafting output directory {str(out)!r} already holds {', '.join(present[:4])}; "
                              "each build writes to a fresh directory so previews stay immutable")
    preview.mkdir(exist_ok=True)
    return out


def _manifest(adapter, identity, status, diagnostics, representations, files, previews, extra=None):
    manifest = {
        "adapter": adapter,
        "adapter_version": ADAPTERS[adapter],
        "project_id": identity["project_id"],
        "source_revision": identity["source_revision"],
        "input_digest": identity["input_digest"],
        "status": status,
        "diagnostics": _ordered(diagnostics),
        "representations": representations if status != "failed" else {},
        "files": list(files),
        "previews": list(previews),
    }
    manifest.update(extra or {})
    return manifest


def _fail(out, identity, diagnostics, events):
    manifests = {}
    for adapter in ADAPTERS:
        manifests[adapter] = _manifest(adapter, identity, "failed", diagnostics, {}, [], [],
                                       {"sheet_id": {"pid": "P&ID-001", "legend": "LEG-001"}.get(adapter)})
        _write(out / f"{adapter}.manifest.json", _json_bytes(manifests[adapter]))
    errors = sum(1 for d in diagnostics if d["severity"] == "error")
    for adapter in ADAPTERS:
        events.send("activity", adapter, f"Generation failed with {errors} structural error(s); no drawing written")
    return manifests


def build_drafting(project, output_dir, emit=None, cancelled=None, manual_notes=None) -> dict:
    """Generate the P&ID, legend and description for ``project``; return ``{pid, legend, description}`` manifests."""
    out = _prepare_output(output_dir)
    events = _Events(emit)
    _check(cancelled)
    events.send("activity", "pid", "Validating the project snapshot")
    diagnostics, snapshot, digest = [], None, None
    try:
        snapshot = _thaw(project)
    except Exception as exc:  # an unreadable snapshot cannot be drawn faithfully
        diagnostics.append(_diagnostic("snapshot_unreadable", "error", [], f"project could not be copied ({exc})"))
    if snapshot is not None:
        try:
            digest = canonical_digest(snapshot)
        except (TypeError, ValueError) as exc:
            diagnostics.append(_diagnostic("digest_unavailable", "error", [], f"canonical input digest failed ({exc})"))
        diagnostics.extend(validate_project(snapshot))
    project_id, revision = _identity(snapshot)
    identity = {"project_id": project_id, "source_revision": revision, "input_digest": digest,
                "project_name": _project_name(snapshot)}
    if any(d["severity"] == "error" for d in diagnostics):
        return _fail(out, identity, diagnostics, events)
    model_warnings = [d for d in diagnostics if d["severity"] == "warning"]
    try:
        graph = FlowGraph(snapshot)
        sheet = pid_sheet.PidSheet(snapshot, graph, model_warnings)
    except Exception as exc:  # an adapter fault must never pass for a drawing
        return _fail(out, identity, diagnostics + [_diagnostic("pid_render_failed", "error", [],
                                                               f"drawing setup failed ({type(exc).__name__}: {exc})")],
                     events)

    # Progressive drawing: one immutable P&ID and legend preview per completed unit of work.
    total = len(sheet.units)
    previews = {"pid": [], "legend": []}
    for step, unit in enumerate(sheet.units, 1):
        _check(cancelled)
        events.send("activity", "pid", unit[2], completed=step - 1, total=total)
        message = sheet.run_unit(unit)
        state = sheet.status_diagnostics()
        svg = sheet.render(identity, _status(state), state, preview=(step, total))
        inventory = inv.parse_inventory(svg)
        index = inv.build_index(snapshot, sheet, inventory)
        legend_svg, _ = leg.render_legend(identity, _status(state), len(sheet.open_items(state)), inventory["keys"],
                                          leg.used_by_tags(snapshot, inventory), index, preview=(step, total))
        for name, text in (("pid", svg), ("legend", legend_svg)):
            relative = f"{PREVIEW_DIR}/{name}-{step:04d}.svg"
            _write(out / relative, text.encode("utf-8"), immutable=True)
            previews[name].append(relative)
        events.send("preview", "pid", message, path=previews["pid"][-1], completed=step, total=total)
        events.send("preview", "legend", f"Legend preview lists {len(inventory['keys'])} convention(s) and "
                    f"{len(index['items'])} tagged item(s) drawn so far", path=previews["legend"][-1],
                    completed=step, total=total)
    _check(cancelled)

    # Final sheet, inventory, index and legend, cross-checked before anything is written.
    events.send("activity", "pid", "Checking the drawing inventory, zone references and item index")
    pid_diagnostics = model_warnings + sheet.diagnostics
    pid_status = _status(pid_diagnostics)
    svg = sheet.render(identity, pid_status, pid_diagnostics)
    inventory = inv.parse_inventory(svg)
    index = inv.build_index(snapshot, sheet, inventory)
    used_by = leg.used_by_tags(snapshot, inventory)
    open_count = len(sheet.open_items(pid_diagnostics))
    legend_svg, legend_warnings = leg.render_legend(identity, pid_status, open_count, inventory["keys"], used_by, index)
    legend_diagnostics = pid_diagnostics + legend_warnings
    if _status(legend_diagnostics) != pid_status:
        legend_svg, legend_warnings = leg.render_legend(identity, _status(legend_diagnostics),
                                                        open_count + len(legend_warnings), inventory["keys"], used_by,
                                                        index)
    problems = inv.verify_drawing(inventory, index) + inv.verify_legend(inv.parse_legend(legend_svg), inventory, index)
    if problems:
        return _fail(out, identity, diagnostics + problems, events)
    legend_status = _status(legend_diagnostics)
    inventory_record = dict(inventory, project_id=identity["project_id"], source_revision=identity["source_revision"],
                            input_digest=identity["input_digest"])
    index_record = dict(index, input_digest=identity["input_digest"])

    events.send("activity", "description", f"Composing the process description: {len(graph.steps)} flow step(s), "
                f"{len(snapshot['model']['entities']) + len(snapshot['model']['connections'])} tagged item(s)")
    builder = desc.DescriptionBuilder(snapshot, graph, index, model_warnings)
    description = builder.build(manual_notes)
    description_diagnostics = model_warnings + desc.conflict_diagnostics(description["conflicts"])
    description_status = _status(description_diagnostics)
    description["input_digest"] = identity["input_digest"]
    description["status"] = description_status
    description["title"] = sheet.title_fields(pid_status, 0, None).subtitle
    description["project_name"] = identity["project_name"]
    description_html = desc.render_html(description, description_status, description_diagnostics)
    _check(cancelled)

    # Publication inside the build directory: artifact files first, then each manifest.
    model = snapshot["model"]
    checks = {
        "legend_keys_equal_inventory": True,
        "index_ids_equal_drawn_tagged_ids": True,
        "zones_recomputed_from_paper_positions": True,
        "legend_samples_in_inventory": 0,
    }
    _write(out / "pid.svg", svg.encode("utf-8"))
    _write(out / "drawing.inventory.json", _json_bytes(inventory_record))
    manifests = {
        "pid": _manifest("pid", identity, pid_status, pid_diagnostics, sheet.representations(),
                         ["pid.svg", "drawing.inventory.json"], previews["pid"],
                         {"sheet_id": "P&ID-001", "legend_keys": inventory["keys"], "checks": checks,
                          "layout_findings": copy.deepcopy(sheet.layout_findings)}),
    }
    _write(out / "pid.manifest.json", _json_bytes(manifests["pid"]))
    events.send("artifact", "pid", f"P&ID sheet complete with status {pid_status}", path="pid.svg")

    legend_representations = {}
    keys_by_item = {item["semantic_id"]: item for item in index["items"]}
    for semantic_id in sorted(set(model["entities"]) | set(model["connections"]) | set(model["ports"])):
        item = keys_by_item.get(semantic_id)
        legend_representations[semantic_id] = (
            [f"index/{semantic_id}"] + [f"legend/{key}" for key in item["legend_keys"]] if item else []
        )
    _write(out / "legend.svg", legend_svg.encode("utf-8"))
    _write(out / "item-index.json", _json_bytes(index_record))
    manifests["legend"] = _manifest("legend", identity, legend_status, legend_diagnostics, legend_representations,
                                    ["legend.svg", "item-index.json"], previews["legend"],
                                    {"sheet_id": "LEG-001", "legend_for": "P&ID-001", "legend_keys": inventory["keys"],
                                     "index_items": len(index["items"])})
    _write(out / "legend.manifest.json", _json_bytes(manifests["legend"]))
    events.send("artifact", "legend", f"Legend sheet complete with {len(inventory['keys'])} convention(s) and "
                f"{len(index['items'])} indexed item(s)", path="legend.svg")

    description_representations = {}
    steps_by_id = {}
    for step in description["flow_sequence"]:
        steps_by_id.setdefault(step["semantic_id"], []).append(f"flow/{step['step']}")
    for semantic_id in sorted(set(model["entities"]) | set(model["connections"]) | set(model["ports"])):
        if semantic_id in model["ports"]:
            description_representations[semantic_id] = []
        else:
            description_representations[semantic_id] = [f"item/{semantic_id}"] + steps_by_id.get(semantic_id, [])
    _write(out / "description.json", _json_bytes(description))
    _write(out / "description.html", description_html.encode("utf-8"))
    manifests["description"] = _manifest("description", identity, description_status, description_diagnostics,
                                         description_representations, ["description.json", "description.html"], [],
                                         {"conflicts": len(description["conflicts"]),
                                          "manual_notes": len(description["manual_notes"])})
    _write(out / "description.manifest.json", _json_bytes(manifests["description"]))
    events.send("artifact", "description", f"Process description complete with status {description_status}",
                path="description.html")
    return manifests
