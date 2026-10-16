"""Tagged process description generated from one project revision.

Text is assembled from structured evidence: equipment kinds, port
connectivity, recorded parameters and their assumptions. Every reference is a
stable ID resolved to the current tag when rendered, so a rename updates every
generated mention. Statements are intended roles derived from kind and
connectivity, never verified operating behavior. Absent data stays an explicit
unknown; conceptual observation relations are never described as control.

The ``process_narrative`` paragraphs (``narrative.py``) lead the HTML; the
detailed item report follows inside a closed ``<details id="item-details">``.

Manual notes are sidecar records keyed to stable IDs. Their text is copied
verbatim and never rewritten; orphaned targets, stale reviews and malformed
records are reported as conflicts.

Composed equipment is described from its embedded definition: the declared
name and function are quoted as authored intent (never as verified behavior),
the definition's identity and content digest are recorded, the entity's own
parameters are the authoritative quantities, and the definition's local
recipe assumptions and declared unknowns are listed with their scope (the
entity they belong to). Authored text is shown verbatim; only generated
references are resolved to current tags.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re

from . import paper
from .narrative import process_narrative
from .definitions import (
    declared_unknowns,
    definition_identity,
    definition_name,
    definition_of,
    is_composed,
    kind_label,
    local_assumptions,
    parameter_order,
)
from .svgkit import xml_text

ADAPTER = "description"
ADAPTER_VERSION = "0.2.0"
SCHEMA_VERSION = 1
NOTE_FIELDS = {"id", "target_id", "text", "reviewed_revision", "reviewed_fingerprint", "author"}
KIND_NAMES = {
    "tank": "tank",
    "centrifugal_pump": "centrifugal pump",
    "isolation_valve": "isolation valve",
    "check_valve": "check valve",
    "indicator": "indicator",
    "boundary": "scope boundary",
}
CATEGORY = {"tank": "equipment", "centrifugal_pump": "equipment", "isolation_valve": "valve", "check_valve": "valve",
            "indicator": "instrument", "boundary": "boundary"}
INTENT_BASIS = "Intended role derived from the item kind and modeled connectivity; not verified operating behavior."
DECLARED_BASIS = ("Declared function quoted from the equipment definition (authored text) with the modeled "
                  "connectivity; not verified operating behavior, duty or design.")


def _digest(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


class _Text:
    """Text built from literal parts and stable-ID references."""

    def __init__(self, tags):
        self.tags = tags
        self.parts = []

    def add(self, text):
        if self.parts and "text" in self.parts[-1]:
            self.parts[-1]["text"] += text
        else:
            self.parts.append({"text": text})
        return self

    def ref(self, semantic_id):
        self.parts.append({"ref": semantic_id, "tag": self.tags[semantic_id]})
        return self

    def refs(self, ids, conjunction="and"):
        ids = list(ids)
        for index, semantic_id in enumerate(ids):
            if index:
                self.add(f" {conjunction} " if index == len(ids) - 1 else ", ")
            self.ref(semantic_id)
        return self

    def record(self):
        text = "".join(p["text"] if "text" in p else p["tag"] for p in self.parts)
        refs = list(dict.fromkeys(p["ref"] for p in self.parts if "ref" in p))
        return {"text": text, "parts": copy.deepcopy(self.parts), "refs": refs}


class DescriptionBuilder:
    def __init__(self, project, graph, index, model_diagnostics):
        model, physical = project["model"], project["physical"]
        self.project = project
        self.graph = graph
        self.entities = model["entities"]
        self.ports = model["ports"]
        self.connections = model["connections"]
        self.assumptions = model["assumptions"]
        self.placements = physical["placements"]
        self.routes = physical["routes"]
        self.edges = project["pid"]["edges"]
        self.model_diagnostics = model_diagnostics
        self.tags = {eid: e["tag"] for eid, e in self.entities.items()}
        self.tags.update({cid: c["tag"] for cid, c in self.connections.items()})
        self.refs_by_id = {}
        for item in index["items"]:
            self.refs_by_id[item["semantic_id"]] = [o["ref"] for o in item["occurrences"]]

    def text(self):
        return _Text(self.tags)

    # --- Structure -----------------------------------------------------------------------

    def build(self, manual_notes):
        entity_items = {eid: self._entity_item(eid) for eid in self.graph.entity_order}
        connection_items = {cid: self._connection_item(cid) for cid in self.connections}
        # Flow order first (entities and lines interleaved), then items outside the flow (indicators).
        order = [step["id"] for step in self.graph.steps] + self.graph.entity_order + sorted(self.connections)
        ordered = [entity_items.get(i) or connection_items[i] for i in dict.fromkeys(order)]
        unknowns = self._unknowns()
        for item in ordered:
            item["unknown_ids"] = [u["id"] for u in unknowns if item["semantic_id"] in
                                   {a["semantic_id"] for a in u["applies_to"]}]
        purpose = self._purpose()
        fingerprints = {item["semantic_id"]: item["fingerprint"] for item in ordered}
        fingerprints[self.project["id"]] = _digest({"purpose": purpose["text"],
                                                    "flow": [self.tags.get(s["id"]) for s in self.graph.steps]})
        for pid, port in self.ports.items():
            fingerprints[pid] = _digest({"record": port})
        for aid, record in self.assumptions.items():
            fingerprints[aid] = _digest({"record": record})
        notes, conflicts = reconcile_notes(manual_notes, self.project, fingerprints, self._target_label)
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION,
            "project_id": self.project["id"],
            "source_revision": self.project["revision"],
            "input_digest": None,
            "status": None,
            "title": None,
            "sheet_id": paper.SHEET_ID,
            "legend_sheet_id": paper.LEGEND_SHEET_ID,
            "basis": "Generated from the engineering model only. Roles are intended functions derived from item kinds "
                     "and connectivity, not verified operating behavior. No process, hydraulic, safety or fabrication "
                     "adequacy is established.",
            "process_narrative": process_narrative(self),
            "purpose": purpose,
            "flow_sequence": self._flow_sequence(),
            "items": ordered,
            "assumptions": self._assumption_register(),
            "definition_assumptions": self._definition_assumptions(),
            "unknowns": unknowns,
            "open_items": self._open_items(),
            "manual_notes": notes,
            "conflicts": conflicts,
        }

    def _target_label(self, target_id):
        if target_id in self.tags:
            return self.tags[target_id], "entity" if target_id in self.entities else "connection"
        if target_id in self.ports:
            port = self.ports[target_id]
            return f"{self.tags[port['entity_id']]} {port['name']}", "port"
        if target_id in self.assumptions:
            return target_id, "assumption"
        if target_id == self.project["id"]:
            return "PROJECT", "project"
        return None, None

    # --- Purpose and flow ------------------------------------------------------------------

    def _purpose(self):
        """One clause per connected flow (``FlowGraph.flow_groups``: lines join flow nodes, so separate declared
        paths through one item stay separate), naming only that flow's start and end items and pumps. A flow
        without a start or an end item closes a loop and is stated as such. Items without any line are stated as
        unconnected, never as a start or end."""
        groups, unconnected = self.graph.flow_groups()
        flows = [group for group in groups if group["starts"] and group["ends"]]
        loops = [group for group in groups if not (group["starts"] and group["ends"])]
        text = self.text()
        if len(flows) == 1:
            text.add("The modeled scope carries ")
            self._flow_clause(text, flows[0])
            text.add(". ")
        elif flows:
            text.add("The modeled scope carries separate connected flows that share no line: ")
            for index, flow in enumerate(flows):
                if index:
                    text.add("; and " if index == len(flows) - 1 else "; ")
                self._flow_clause(text, flow)
            text.add(". ")
        elif not loops:
            text.add("The model contains no connected process flow path. ")
        for loop in loops:
            missing = "end" if loop["starts"] else "start" if loop["ends"] else "start or end"
            text.add("Lines joining ").refs(loop["entities"])
            text.add(f" close a loop, so that connected flow has no {missing} item. ")
        if unconnected:
            text.refs(unconnected).add(f" {'are' if len(unconnected) > 1 else 'is'} not connected to any process "
                                       "line. ")
        text.add("The model does not record the process purpose, fluid identity or duty; this statement is derived "
                 "from item kinds and connectivity only.")
        return text.record()

    def _flow_clause(self, text, flow):
        services = sorted({self.ports[p]["service"] for p in flow["ports"]})
        pumps = [e for e in flow["entities"] if self.entities[e]["kind"] == "centrifugal_pump"]
        text.add(f"{' and '.join(services) or 'process'} service from ").refs(flow["starts"])
        text.add(" to ").refs(flow["ends"])
        if pumps:
            text.add(" by way of centrifugal pump" + ("s " if len(pumps) > 1 else " ")).refs(pumps)

    def _flow_sequence(self):
        sequence = []
        for number, step in enumerate(self.graph.steps, 1):
            record = {
                "step": number,
                "run": step["run"],
                "item_kind": step["item_kind"],
                "semantic_id": step["id"],
                "tag": self.tags[step["id"]],
                "zone_refs": list(self.refs_by_id.get(step["id"], [])),
            }
            if step["item_kind"] == "entity":
                record["kind"] = self.entities[step["id"]]["kind"]
                record["position"] = ("start" if not self.graph.incoming[step["id"]]
                                      else "end" if not self.graph.outgoing[step["id"]] else "inline")
                for key in ("flow_path", "flow_port"):  # only items that declare flow paths
                    if key in step:
                        node = self.graph.step_node(step)
                        record[key] = copy.deepcopy(step[key])
                        record["position"] = ("start" if not self.graph.node_in[node]
                                              else "end" if not self.graph.node_out[node] else "inline")
            else:
                source, target = self.graph.ends[step["id"]]
                record.update({"kind": "process_line", "from_semantic_id": source, "from_tag": self.tags[source],
                               "to_semantic_id": target, "to_tag": self.tags[target],
                               "closes_loop": step["closes_loop"]})
                if "branch_from" in step:
                    record["branch_from"] = step["branch_from"]
            sequence.append(record)
        return sequence

    # --- Items ---------------------------------------------------------------------------------

    def _reach(self, eid, direction, kind):
        """Nearest entities of ``kind`` reachable along ``direction`` (outgoing or incoming), following flow
        nodes: never across the separate declared flow paths of one item."""
        return self.graph.nearest(eid, direction, lambda other: self.entities[other]["kind"] == kind)

    def _entity_function(self, eid):
        entity = self.entities[eid]
        kind = entity["kind"]
        upstream = [(cid, self.graph.ends[cid][0]) for cid in self.graph.incoming[eid]]
        downstream = [(cid, self.graph.ends[cid][1]) for cid in self.graph.outgoing[eid]]
        text = self.text()
        if kind == "tank":
            text.add("Intended role: vessel" + ("" if upstream else " at the start of the modeled flow path") + ". ")
        elif kind == "centrifugal_pump":
            text.add("Intended role: centrifugal pump that moves liquid from its suction")
            if upstream:
                text.add(" line ").refs([c for c, _ in upstream])
            text.add(" to its discharge")
            if downstream:
                text.add(" line ").refs([c for c, _ in downstream])
            text.add(". ")
        elif kind == "isolation_valve":
            text.add("Intended role: isolation valve that allows the line to be shut off")
            pumps_down, pumps_up = self._reach(eid, "down", "centrifugal_pump"), self._reach(eid, "up", "centrifugal_pump")
            if pumps_down:
                text.add(" on the suction side of ").refs(pumps_down)
            elif pumps_up:
                text.add(" on the discharge side of ").refs(pumps_up)
            text.add(". ")
        elif kind == "check_valve":
            text.add("Intended role: check (non-return) valve that permits flow")
            if upstream and downstream:
                text.add(" from ").refs([c for c, _ in upstream]).add(" to ").refs([c for c, _ in downstream])
            text.add(" and is intended to prevent reverse flow. ")
        elif kind == "boundary":
            text.add("Marks the end of the modeled scope; the destination beyond it is outside the model and not "
                     "specified. ")
        elif kind == "indicator":
            observes = entity.get("observes") or {}
            subject = observes.get("entity_id")
            text.add(f"Conceptual indication of {str(observes.get('variable', 'an unspecified variable')).replace('_', ' ')}")
            if subject in self.tags:
                text.add(" of ").ref(subject)
            text.add(". It is an observation relation only: no instrument hookup, range, mounting location, alarm or "
                     "control action is modeled, and it is not part of a control loop. ")
        elif is_composed(entity):
            declared = definition_of(entity).get("function")
            text.add(f"Composed equipment from the definition “{definition_name(entity)}”. ")
            if isinstance(declared, str) and declared.strip():
                text.add(f"Declared function (authored, not verified): “{' '.join(declared.split())}” ")
            else:
                text.add("The definition declares no function. ")
        else:
            text.add(f"Item of kind {kind}; this generator does not describe its role. ")
        if self.graph.flow_paths(eid):
            self._path_connectivity(text, eid)
        else:
            for cid, source in upstream:
                text.add("Receives flow from ").ref(source).add(" through ").ref(cid).add(". ")
            for cid, target in downstream:
                text.add("Discharges to ").ref(target).add(" through ").ref(cid).add(". ")
        if kind == "tank":
            text.add("Capacity, operating levels, venting and design conditions are not specified.")
        elif kind == "centrifugal_pump":
            text.add("A motor driver envelope is modeled; duty point, speed and driver rating are not specified.")
        elif kind == "isolation_valve":
            text.add("Operator, normal position and fail action are not modeled.")
        elif kind == "check_valve":
            text.add("Valve type, rating and cracking pressure are not specified.")
        elif is_composed(entity):
            text.add("Its geometry is composed from the definition's reusable parts using the current parameter values "
                     "listed below; duty, operating conditions, materials and ratings are not specified.")
        record = text.record()
        record["text"] = record["text"].rstrip()
        if record["parts"] and "text" in record["parts"][-1]:
            record["parts"][-1]["text"] = record["parts"][-1]["text"].rstrip()
        return record

    def _path_connectivity(self, text, eid):
        """Connectivity of an item with declared flow paths, told per path so separate circuits never read as one."""
        for node in self.graph.nodes_of[eid]:
            label = self.graph.node_label[node]
            if "flow_path" in label:
                inlet, outlet = (name.replace("_", " ") for name in label["flow_path"])
                text.add(f"Declared {inlet} to {outlet} flow path (authored, not verified): ")
            else:
                text.add(f"Port {label['flow_port'].replace('_', ' ')}, outside the declared flow paths: ")
            told = False
            for cid in self.graph.node_in[node]:
                text.add("receives flow from ").ref(self.graph.ends[cid][0]).add(" through ").ref(cid).add("; ")
                told = True
            for cid in self.graph.node_out[node]:
                text.add("discharges to ").ref(self.graph.ends[cid][1]).add(" through ").ref(cid).add("; ")
                told = True
            if not told:
                text.add("no process connection; ")
            last = text.parts[-1]
            last["text"] = last["text"][:-2] + ". "
        if len(self.graph.flow_paths(eid)) > 1:
            text.add("The definition declares these paths separate, so their streams do not mix in the model. ")

    def _entity_evidence(self, eid):
        entity = self.entities[eid]
        evidence = []
        if is_composed(entity):
            identity = definition_identity(entity)
            evidence.append({"kind": "definition", **identity})
            declared = definition_of(entity).get("function")
            if isinstance(declared, str) and declared.strip():
                evidence.append({"kind": "declared_function", "text": declared})
        order = parameter_order(entity, self.graph.templates[eid])
        for name in sorted(entity["parameters"], key=lambda n: (order.index(n) if n in order else len(order), n)):
            quantity = entity["parameters"][name]
            record = {"kind": "parameter", "name": name, "value": quantity["value"], "unit": quantity["unit"]}
            record.update({k: quantity[k] for k in ("assumption_id", "source") if k in quantity})
            evidence.append(record)
        for local in local_assumptions(entity):
            evidence.append({"kind": "definition_assumption", "local_id": local["id"], "scoped_id": f"{eid}/{local['id']}",
                             "category": local["category"], "description": local["description"]})
        if isinstance(entity.get("observes"), dict):
            evidence.append({"kind": "observes", "entity_id": entity["observes"]["entity_id"],
                             "tag": self.tags.get(entity["observes"]["entity_id"]),
                             "variable": entity["observes"]["variable"]})
        for port_id in self.graph.ports_of[eid]:
            port = self.ports[port_id]
            evidence.append({"kind": "port", "port_id": port_id, "name": port["name"], "direction": port["direction"],
                             "nominal_diameter_mm": port["nominal_diameter_mm"], "service": port["service"],
                             "assumption_id": port["assumption_id"]})
            if "connection_status" in port:  # an explicitly open draft port, recorded as authored
                evidence[-1]["connection_status"] = port["connection_status"]
        placement = self.placements.get(eid)
        if placement is None:
            evidence.append({"kind": "physical_placement", "status": "missing"})
        else:
            record = {"kind": "physical_placement", "status": placement["status"]}
            if placement.get("assumption_id"):
                record["assumption_id"] = placement["assumption_id"]
            if placement.get("reason"):
                record["reason"] = placement["reason"]
            evidence.append(record)
        for ref in self.refs_by_id.get(eid, []):
            evidence.append({"kind": "diagram_reference", "ref": ref})
        return evidence

    def _entity_item(self, eid):
        entity = self.entities[eid]
        function = self._entity_function(eid)
        evidence = self._entity_evidence(eid)
        observed_by = sorted((oid for oid, other in self.entities.items()
                              if isinstance(other.get("observes"), dict) and other["observes"]["entity_id"] == eid),
                             key=lambda oid: (self.graph.entity_order.index(oid), oid))
        composed = is_composed(entity)
        item = {
            "semantic_id": eid,
            "item_kind": "entity",
            "kind": entity["kind"],
            "kind_name": KIND_NAMES.get(entity["kind"], kind_label(entity)),
            "category": CATEGORY.get(entity["kind"], "equipment"),
            "tag": entity["tag"],
            "function": function,
            "intent_basis": DECLARED_BASIS if composed else INTENT_BASIS,
            "evidence": evidence,
            "assumption_ids": sorted({e["assumption_id"] for e in evidence if e.get("assumption_id")}),
            "observed_by": [{"semantic_id": oid, "tag": self.tags[oid]} for oid in observed_by],
            "zone_refs": list(self.refs_by_id.get(eid, [])),
            "status": {"diagram": "drawn" if self.refs_by_id.get(eid) else "not drawn",
                       "physical": self.placements[eid]["status"] if eid in self.placements else "missing"},
            "fingerprint": _digest({"record": entity, "function": function["text"]}),
        }
        if composed:
            item["definition"] = definition_identity(entity)
            item["definition_assumption_ids"] = [e["scoped_id"] for e in evidence if e["kind"] == "definition_assumption"]
        return item

    def _connection_item(self, cid):
        connection = self.connections[cid]
        source, target = self.graph.ends[cid]
        from_port, to_port = self.ports[connection["from_port_id"]], self.ports[connection["to_port_id"]]
        text = self.text()
        text.add("Process line from ").ref(source).add(f" {from_port['name']} to ").ref(target).add(f" {to_port['name']}. ")
        if self.entities[target]["kind"] == "centrifugal_pump":
            text.add("Pump suction line of ").ref(target).add(". ")
        if self.entities[source]["kind"] == "centrifugal_pump":
            text.add("Pump discharge line of ").ref(source).add(". ")
        text.add(f"Nominal size DN{_num(connection['nominal_diameter_mm'])}; outside diameter "
                 f"{_num(connection['outside_diameter_m'])} m as recorded. Flow direction follows the modeled outlet "
                 "to inlet port order.")
        function = text.record()
        evidence = [{"kind": "line", "nominal_diameter_mm": connection["nominal_diameter_mm"],
                     "outside_diameter_m": connection["outside_diameter_m"],
                     "assumption_id": connection["assumption_id"]}]
        route = self.routes.get(cid)
        if route is None:
            evidence.append({"kind": "physical_route", "status": "missing"})
        else:
            record = {"kind": "physical_route", "status": route["status"], "mode": route["mode"]}
            if route.get("assumption_id"):
                record["assumption_id"] = route["assumption_id"]
            if route.get("reason"):
                record["reason"] = route["reason"]
            evidence.append(record)
        for edge_id in sorted(e for e, edge in self.edges.items() if edge["connection_id"] == cid):
            edge = self.edges[edge_id]
            record = {"kind": "diagram_edge", "edge_id": edge_id, "status": edge["status"], "manual": edge["manual"]}
            if edge.get("reason"):
                record["reason"] = edge["reason"]
            evidence.append(record)
        for ref in self.refs_by_id.get(cid, []):
            evidence.append({"kind": "diagram_reference", "ref": ref})
        statuses = [e["status"] for e in evidence if e["kind"] == "diagram_edge"]
        return {
            "semantic_id": cid,
            "item_kind": "connection",
            "kind": "process_line",
            "kind_name": "process line",
            "category": "line",
            "tag": connection["tag"],
            "from": {"semantic_id": source, "tag": self.tags[source], "port": from_port["name"]},
            "to": {"semantic_id": target, "tag": self.tags[target], "port": to_port["name"]},
            "function": function,
            "intent_basis": "Connectivity and size as recorded in the model; not a pipe specification.",
            "evidence": evidence,
            "assumption_ids": sorted({e["assumption_id"] for e in evidence if e.get("assumption_id")}),
            "observed_by": [],
            "zone_refs": list(self.refs_by_id.get(cid, [])),
            "status": {"diagram": ", ".join(statuses) if statuses else "not drawn",
                       "physical": route["status"] if route else "missing"},
            "fingerprint": _digest({"record": connection, "function": function["text"]}),
        }

    # --- Registers ------------------------------------------------------------------------------

    def _assumption_register(self):
        used = {aid: [] for aid in self.assumptions}

        def note(aid, semantic_id, field):
            if aid in used:
                entry = {"semantic_id": semantic_id, "tag": self.tags.get(semantic_id, semantic_id), "field": field}
                if entry not in used[aid]:
                    used[aid].append(entry)

        for eid in self.graph.entity_order:
            entity = self.entities[eid]
            for name, quantity in sorted(entity["parameters"].items()):
                if "assumption_id" in quantity:
                    note(quantity["assumption_id"], eid, f"parameter {name}")
            for port_id in self.graph.ports_of[eid]:
                note(self.ports[port_id]["assumption_id"], eid, f"port {self.ports[port_id]['name']}")
            placement = self.placements.get(eid)
            if placement and placement.get("assumption_id"):
                note(placement["assumption_id"], eid, "physical placement")
        for cid in self.graph.connection_order:
            note(self.connections[cid]["assumption_id"], cid, "line size")
            route = self.routes.get(cid)
            if route and route.get("assumption_id"):
                note(route["assumption_id"], cid, "physical route")
        return [{"id": aid, "description": self.assumptions[aid]["description"],
                 "source": self.assumptions[aid]["source"], "used_by": used[aid]} for aid in sorted(self.assumptions)]

    def _definition_assumptions(self):
        """Recipe-local assumptions of each composed entity's definition, scoped by its stable ID.

        They are not project model assumptions: the same local ID in two definitions names two
        different records, so each is keyed ``<entity id>/<local id>``.
        """
        records = []
        for eid in self.graph.entity_order:
            entity = self.entities[eid]
            for local in local_assumptions(entity):
                records.append({"scoped_id": f"{eid}/{local['id']}", "local_id": local["id"],
                                "definition_id": definition_of(entity).get("id"),
                                "applies_to": {"semantic_id": eid, "tag": self.tags[eid]},
                                "category": local["category"], "description": local["description"],
                                "scope": "local to the equipment definition embedded in this item"})
        return records

    def _applies(self, ids):
        return [{"semantic_id": i, "tag": self.tags[i]} for i in ids]

    def _unknowns(self):
        order = self.graph.entity_order
        process = [e for e in order if self.graph.ports_of[e]]
        lines = list(self.graph.connection_order)
        by_kind = {}
        for eid in order:
            by_kind.setdefault(self.entities[eid]["kind"], []).append(eid)
        catalogue = [
            ("unknown/process-conditions", "Process conditions", process + lines,
             "Fluid identity, duty, flow rate, pressure and temperature are not modeled. Ports and lines record "
             "liquid service only."),
            ("unknown/materials-ratings", "Materials and ratings", process + lines,
             "Materials of construction and pressure and temperature ratings are not specified."),
            ("unknown/valve-positions", "Valve operation", by_kind.get("isolation_valve", []),
             "Valve operators, normal (open or closed) positions and fail actions are not modeled."),
            ("unknown/check-valve-details", "Check valve details", by_kind.get("check_valve", []),
             "Check valve type, rating and cracking pressure are not specified."),
            ("unknown/pump-duty", "Pump duty", by_kind.get("centrifugal_pump", []),
             "Pump duty point (flow and head), speed, driver rating and sealing are not specified."),
            ("unknown/tank-details", "Tank details", by_kind.get("tank", []),
             "Tank working volume, operating and alarm levels, venting and design conditions are not specified."),
            ("unknown/instrumentation", "Instrumentation", by_kind.get("indicator", []),
             "Indicator ranges, mounting locations, process hookups, alarms and any control functions are not "
             "modeled; observation relations are conceptual."),
            ("unknown/downstream", "Beyond the scope boundary", by_kind.get("boundary", []),
             "The destination and conditions beyond the scope boundary are outside the model."),
            ("unknown/composed-equipment", "Composed equipment", [e for e in order if is_composed(self.entities[e])],
             "Composed equipment is an illustrative assembly of reusable geometry parts from its definition. Its "
             "declared name and function are authored text, not verified behavior; duty, internals, operating "
             "conditions, materials and ratings are not modeled unless recorded as parameters. Whether its CAD "
             "solids build and verify is decided by the geometry output, not by this description."),
            *self._declared_unknowns(order),
            ("unknown/control-behavior", "Control and safety functions (none modeled)", list(order),
             "No control loops, interlocks or safety functions are modeled. Nothing in this description is a control "
             "or safety claim."),
            ("unknown/physical-details", "Physical details", list(order) + lines,
             "Supports, fittings, slope, drainage, access and clash checks are not modeled; physical positions and "
             "routes are illustrative."),
        ]
        return [{"id": uid, "topic": topic, "text": text, "applies_to": self._applies(ids)}
                for uid, topic, ids, text in catalogue if ids]

    def _declared_unknowns(self, order):
        """One register entry per distinct definition's declared unknowns, quoted verbatim."""
        groups = {}
        for eid in order:
            entity = self.entities[eid]
            declared = declared_unknowns(entity)
            if declared:
                key = (str(definition_of(entity).get("id")), tuple(declared))
                groups.setdefault(key, (definition_name(entity), []))[1].append(eid)
        entries, used = [], set()
        for (definition_id, declared), (name, ids) in groups.items():
            slug = re.sub(r"[^a-z0-9]+", "-", definition_id.lower()).strip("-") or "definition"
            uid, n = f"unknown/definition-{slug}", 2
            while uid in used:
                uid, n = f"unknown/definition-{slug}-{n}", n + 1
            used.add(uid)
            entries.append((uid, f"Declared in the definition “{name}”", ids,
                            "Authored in the equipment definition: " + " ".join(declared)))
        return entries

    def _open_items(self):
        items = []
        for diagnostic in self.model_diagnostics:
            if diagnostic["severity"] != "warning":
                continue
            ids = [i for i in diagnostic["entity_ids"] if i in self.tags]
            items.append({"code": diagnostic["code"], "semantic_ids": ids, "tags": [self.tags[i] for i in ids],
                          "message": diagnostic["message"]})
        return items


# --- Manual notes ------------------------------------------------------------------------------


def _note_problem(note):
    if not isinstance(note, dict):
        return "is not an object"
    unknown = sorted(str(k) for k in note if k not in NOTE_FIELDS)
    if unknown:
        return f"has unsupported fields {unknown}"
    for field in ("id", "target_id"):
        if not isinstance(note.get(field), str) or not note[field].strip():
            return f"{field} must be a nonempty string"
    if not isinstance(note.get("text"), str):
        return "text must be a string"
    revision = note.get("reviewed_revision")
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        return "reviewed_revision must be a nonnegative integer"
    if "reviewed_fingerprint" in note and not isinstance(note["reviewed_fingerprint"], str):
        return "reviewed_fingerprint must be a string"
    if "author" in note and not isinstance(note["author"], str):
        return "author must be a string"
    return None


def reconcile_notes(manual_notes, project, fingerprints, label):
    """Return ``(notes, conflicts)``: verbatim note records with their state, and the conflicts found."""
    if manual_notes is None:
        return [], []
    conflicts = []
    notes = manual_notes
    if isinstance(manual_notes, dict):
        extra = sorted(str(k) for k in manual_notes if k not in ("schema_version", "project_id", "notes"))
        if extra or manual_notes.get("schema_version", 1) != 1:
            conflicts.append({"note_id": None, "note_index": None, "code": "invalid_sidecar", "target_id": None,
                              "message": f"manual-note sidecar has unsupported schema or fields {extra}"})
        owner = manual_notes.get("project_id")
        if owner is not None and owner != project["id"]:
            conflicts.append({"note_id": None, "note_index": None, "code": "project_mismatch", "target_id": None,
                              "message": f"manual notes belong to project {owner!r}, not {project['id']!r}; "
                                         "every note is kept but treated as unreviewed"})
        notes = manual_notes.get("notes", [])
    if not isinstance(notes, list):
        conflicts.append({"note_id": None, "note_index": None, "code": "invalid_sidecar", "target_id": None,
                          "message": "manual notes must be a list of note records"})
        return [{"note": copy.deepcopy(notes), "state": "invalid"}], conflicts
    mismatch = any(c["code"] == "project_mismatch" for c in conflicts)
    records, seen = [], set()
    for index, note in enumerate(notes):
        record = {"note": copy.deepcopy(note), "note_index": index}
        problem = _note_problem(note)
        note_id = note.get("id") if isinstance(note, dict) and isinstance(note.get("id"), str) else None
        if problem is not None:
            record["state"] = "invalid"
            conflicts.append({"note_id": note_id, "note_index": index, "code": "invalid_note", "target_id": None,
                              "message": f"manual note {index} {problem}; its content is kept unchanged"})
            records.append(record)
            continue
        if note_id in seen:
            conflicts.append({"note_id": note_id, "note_index": index, "code": "duplicate_note_id",
                              "target_id": note["target_id"], "message": f"manual note id {note_id!r} is repeated"})
        seen.add(note_id)
        target = note["target_id"]
        tag, kind = label(target)
        record["target_kind"] = kind
        if kind is None:
            record["state"] = "orphan"
            conflicts.append({"note_id": note_id, "note_index": index, "code": "orphan_reference", "target_id": target,
                              "message": f"manual note {note_id!r} refers to {target!r}, which is not in revision "
                                         f"{project['revision']}; the note text is kept unchanged"})
            records.append(record)
            continue
        record["target_tag"] = tag
        record["current_fingerprint"] = fingerprints.get(target)
        if "reviewed_fingerprint" in note:
            stale = note["reviewed_fingerprint"] != fingerprints.get(target)
            basis = "the generated content of its target changed since review"
        else:
            stale = note["reviewed_revision"] != project["revision"]
            basis = f"it was reviewed at revision {note['reviewed_revision']}, not {project['revision']}"
        if stale or mismatch:
            record["state"] = "stale_review"
            conflicts.append({"note_id": note_id, "note_index": index, "code": "stale_review", "target_id": target,
                              "message": f"manual note {note_id!r} on {tag} needs review: "
                                         f"{basis if stale else 'it belongs to another project'}"})
        else:
            record["state"] = "current"
        records.append(record)
    return records, conflicts


def conflict_diagnostics(conflicts):
    diagnostics = []
    for conflict in conflicts:
        ids = [conflict["target_id"]] if conflict.get("target_id") else []
        diagnostics.append({"code": f"manual_note_{conflict['code']}", "severity": "warning", "entity_ids": ids,
                            "message": conflict["message"]})
    return diagnostics


# --- HTML ------------------------------------------------------------------------------------------

_INVALID_HTML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _e(text) -> str:
    return html.escape(_INVALID_HTML.sub("�", xml_text(str(text))), quote=True)


def anchor(semantic_id) -> str:
    return "item-" + re.sub(r"[^A-Za-z0-9_-]", lambda m: f"_{ord(m.group(0)):x}_", semantic_id)


def _nowrap_tags_html(text, tags) -> str:
    """Escaped ``text`` with each whole occurrence of a known tag kept on one line (no break at its hyphen)."""
    tags = sorted((t for t in tags if t), key=len, reverse=True)
    if not tags:
        return _e(text)
    pattern = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, tags)) + r")(?![\w-])")
    pieces = pattern.split(text)
    return "".join(f'<span class="nw">{_e(p)}</span>' if i % 2 else _e(p) for i, p in enumerate(pieces))


def _parts_html(record, known):
    out = []
    for part in record["parts"]:
        if "text" in part:
            out.append(_e(part["text"]))
        elif part["ref"] in known:
            out.append(f'<a class="tag" href="#{_e(anchor(part["ref"]))}" data-ref="{_e(part["ref"])}">{_e(part["tag"])}</a>')
        else:
            out.append(f'<span class="tag">{_e(part["tag"])}</span>')
    return "".join(out)


_CSS = """
:root { color-scheme: light; --ink:#1c2229; --ink-2:#414953; --muted:#5f6873; --faint:#747d88; --rule:#e2e5e9;
  --rule-2:#cfd4da; --ground:#eef0f3; --sheet:#ffffff; --tint:#f6f7f9; --accent:#1d5bb4; --accent-wash:#edf3fc;
  --accent-line:#bed0ec; --amber-ink:#7a5000; --amber-wash:#fdf4dd; --amber-line:#e8cd8b; --warn:#b3261e;
  --warn-wash:#fcebe9; --ok:#2f7d55;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }
* { box-sizing: border-box; }
html { background: var(--ground); }
body { margin:0; background: var(--ground); color: var(--ink); -webkit-font-smoothing: antialiased;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "Helvetica Neue", Helvetica, Arial,
  "Liberation Sans", sans-serif; font-variant-numeric: tabular-nums; }
main { max-width: 1040px; min-height: 100vh; margin: 0 auto; background: var(--sheet);
  padding: clamp(22px, 4vw, 44px) clamp(18px, 5vw, 56px) 64px; }
header { border-bottom: 2px solid var(--ink); padding-bottom: 16px; margin-bottom: 22px; }
h1 { font-size: 26px; line-height: 1.22; font-weight: 650; letter-spacing: -.015em; margin: 0 0 12px;
  overflow-wrap: break-word; }
h1 .nw { white-space: nowrap; }
h2 { font-size: 19px; line-height: 1.3; font-weight: 650; letter-spacing: -.01em; margin: 46px 0 12px; padding-bottom: 7px;
  border-bottom: 1px solid var(--rule-2); break-after: avoid; }
h3 { font-size: 17px; line-height: 1.3; font-weight: 650; margin: 0; }
p { margin: 0 0 12px; max-width: 72ch; text-wrap: pretty; }
.meta { display:flex; flex-wrap:wrap; align-items: center; gap: 6px 22px; color: var(--muted); font-size: 13.5px; }
.meta > span { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }
.meta b { color: var(--ink); font-weight: 600; }
.meta code { font-size: 12.5px; }
.status { display:inline-block; padding: 2px 9px; border-radius: 4px; font-weight: 650; font-size: 12px; line-height: 1.5;
  letter-spacing:.03em; border:1px solid currentColor; }
.status.complete { color: var(--ok); background: #eaf5ef; border-color: #b9dcc8; }
.status.pending { color: var(--amber-ink); background: var(--amber-wash); border-color: var(--amber-line); }
.notice { background: var(--tint); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 16px;
  margin: 16px 0; font-size: 14.5px; color: var(--ink-2); max-width: 86ch; }
.notice.warn { background: var(--amber-wash); border-color: var(--amber-line); color: var(--ink); }
.narrative h2 { margin-top: 30px; }
.narrative p { font-size: 16px; line-height: 1.7; max-width: 74ch; margin-bottom: 14px; }
.narrative p[data-narrative-role="scope"] { color: var(--ink-2); font-size: 14.5px; }
details.item-details { margin-top: 36px; border-top: 1px solid var(--rule-2); }
details.item-details > summary { cursor: pointer; padding: 14px 0 12px; font-size: 19px; line-height: 1.3; font-weight: 650;
  letter-spacing: -.01em; border-bottom: 1px solid var(--rule); }
details.item-details > summary .count { color: var(--muted); font-size: 14px; font-weight: 500; letter-spacing: 0;
  margin-left: 8px; }
details.item-details > summary .count.warn { color: var(--amber-ink); font-weight: 600; }
details.item-details[open] > summary { margin-bottom: 4px; }
details.item-details > h2:first-of-type { margin-top: 26px; }
.flow { display:flex; flex-wrap:wrap; align-items:center; gap: 8px 4px; margin: 10px 0 4px; }
.flow + .flow { margin-top: 6px; }
.flow .node { background: var(--sheet); border:1px solid var(--rule-2); border-radius: 6px; padding: 3px 10px; font-weight: 650;
  box-shadow: 0 1px 1px rgba(28, 38, 52, .05); }
.flow .node a.tag { border-bottom: 0; }
.flow .line { color: var(--muted); font-size: 13px; white-space: nowrap; }
.flow .line::before { content: "\\2192\\00a0"; } .flow .line::after { content: "\\00a0\\2192"; }
.flow + table { margin-top: 16px; }
a { color: var(--accent); }
a.tag { color: var(--accent); font-weight: 650; text-decoration: none; border-bottom: 1px solid var(--accent-line); }
a.tag:hover { border-bottom-color: var(--accent); }
.tag { font-weight: 650; white-space: nowrap; }
section.item { background: var(--sheet); border:1px solid var(--rule-2); border-radius: 8px; padding: 16px 20px 16px;
  margin: 14px 0; scroll-margin-top: 16px; break-inside: avoid; }
section.item:target, tr:target > td { background: var(--amber-wash); }
section.item:target { border-color: var(--amber-line); box-shadow: 0 0 0 3px var(--amber-wash); }
.item-head { display:flex; justify-content: space-between; align-items: baseline; gap: 4px 16px; flex-wrap: wrap;
  margin-bottom: 6px; }
.kind { color: var(--muted); font-size: 14px; font-weight: 500; }
h3 .kind { margin-left: 6px; }
.refs { font-size: 12.5px; color: var(--muted); }
.item-head .refs { font-family: var(--mono); font-size: 12px; }
.basis { color: var(--muted); font-size: 13px; margin: 4px 0 10px; max-width: 80ch; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; line-height: 1.5; margin: 8px 0 6px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--rule); vertical-align: top;
  overflow-wrap: break-word; }
th { font-size: 12.5px; font-weight: 600; color: var(--ink-2); background: var(--tint); border-bottom: 1px solid var(--rule-2);
  white-space: nowrap; }
table.evidence { font-size: 13px; margin: 10px 0 8px; }
table.evidence th { background: none; color: var(--muted); font-size: 12px; padding-top: 4px; padding-bottom: 4px; }
table.evidence td { padding-top: 5px; padding-bottom: 5px; }
table.evidence td:last-child { color: var(--muted); width: 38%; }
code { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); }
a code { color: inherit; }
td code { overflow-wrap: anywhere; }
td a code, .chips a, .note a { text-decoration: none; }
td a { text-decoration: none; }
td a:hover { text-decoration: underline; text-underline-offset: 2px; }
.chips { display:flex; flex-wrap:wrap; align-items: center; gap: 6px; margin: 12px 0 2px; font-size: 12.5px; }
.chips .kind { font-size: 12.5px; font-weight: 600; color: var(--muted); margin-right: 4px; }
.chip { display: inline-block; border:1px solid var(--rule-2); border-radius: 4px; padding: 1px 8px; background: var(--tint);
  color: var(--ink-2); line-height: 1.6; }
a.chip[href^="#asm-"] { font-family: var(--mono); font-size: 12px; }
a.chip[href^="#item-"] { background: var(--amber-wash); border-color: var(--amber-line); color: var(--amber-ink); }
.chip.local { border-style: dashed; background: var(--sheet); }
a.chip:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-wash); }
.note { border: 1px solid var(--accent-line); border-radius: 6px; padding: 9px 12px 10px; margin: 10px 0;
  background: #f5f8fd; }
.note pre { white-space: pre-wrap; margin: 4px 0 0; font: inherit; max-width: 80ch; overflow-wrap: anywhere; }
.note .state { display: inline-block; font-size: 12px; font-weight: 650; letter-spacing: .02em; color: var(--accent); }
.note .state::first-letter { text-transform: uppercase; }
.note.stale_review, .note.orphan, .note.invalid { border-color: #efc1bc; background: var(--warn-wash); }
.note.stale_review .state, .note.orphan .state, .note.invalid .state { color: var(--warn); }
.warn { color: var(--warn); }
footer { margin-top: 48px; color: var(--muted); font-size: 12.5px; line-height: 1.6; border-top: 1px solid var(--rule-2);
  padding-top: 12px; }
footer code { font-size: 12px; overflow-wrap: anywhere; }
@media (min-width: 900px) {
  tr[id^="asm-"] > td:first-child code, tr[id^="dasm-"] > td:nth-child(2) code { white-space: nowrap; }
  tr[id^="asm-"] > td:last-child { width: 12em; }
}
@media (max-width: 720px) {
  body { font-size: 14.5px; }
  h1 { font-size: 22px; }
  h2 { font-size: 18px; margin-top: 38px; }
  section.item { padding: 14px 14px; }
  table { font-size: 12.5px; }
  th, td { padding: 6px 7px; overflow-wrap: anywhere; }
  th { white-space: normal; }
  table.evidence td:last-child { width: auto; }
}
@media print {
  html, body { background: #fff; }
  main { max-width: none; min-height: 0; padding: 0; border: 0; }
  section.item { box-shadow: none; }
  details.item-details > summary { border-bottom: 0; }
  tr { break-inside: avoid; }
  a.tag, a.chip, td a { color: inherit; }
}
"""


def render_html(description, status, diagnostics) -> str:
    known = {item["semantic_id"] for item in description["items"]}
    name = description.get("project_name")
    title = _e(description["title"])
    heading = f"{_e(name)}: {title}" if name else title
    tags = {item["tag"] for item in description["items"]}
    title_html = _nowrap_tags_html(description["title"], tags)
    heading_html = f"{_nowrap_tags_html(name, tags)}: {title_html}" if name else title_html
    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Process description — {heading}</title>",
        f"<style>{_CSS}</style></head>",
        f'<body data-project-id="{_e(description["project_id"])}" data-source-revision="{description["source_revision"]}" '
        f'data-input-digest="{_e(description["input_digest"])}" data-status="{_e(status)}"><main>',
        "<header>",
        f"<h1>Process description — {heading_html}</h1>",
        '<div class="meta">',
        (f'<span>Project <b data-role="project-name">{_e(name)}</b> (<code>{_e(description["project_id"])}</code>)'
         '</span>' if name else f'<span>Project <b>{_e(description["project_id"])}</b></span>'),
        f'<span>Model revision <b>{description["source_revision"]}</b></span>',
        f'<span>Drawing <b>{_e(description["sheet_id"])}</b>, legend <b>{_e(description["legend_sheet_id"])}</b></span>',
        f'<span class="status {_e(status)}">{_e(status.upper())}</span>',
        "</div></header>",
        f'<div class="notice">{_e(description["basis"])}</div>',
    ]
    if status != "complete":
        out.append(f'<div class="notice warn">This description is <b>{_e(status)}</b>: '
                   f"{len(diagnostics)} open item(s) remain; see Open items and Manual notes below.</div>")
    out.append('<section class="narrative" aria-labelledby="narrative-heading">'
               '<h2 id="narrative-heading">Process narrative</h2>')
    for number, paragraph in enumerate(description.get("process_narrative", []), 1):
        out.append(f'<p data-role="narrative" data-paragraph="{number}" data-narrative-role="{_e(paragraph["role"])}">'
                   f'{_parts_html(paragraph, known)}</p>')
    out.append("</section>")
    count = f'{len(description["items"])} item{"" if len(description["items"]) == 1 else "s"}'
    if status != "complete":
        count = (f'<span class="count">{count}</span> <span class="count warn">{_e(status)}: '
                 f"{len(diagnostics)} open item(s)</span>")
    else:
        count = f'<span class="count">{count}</span>'
    out.append(f'<details id="item-details" class="item-details"><summary>Item details {count}</summary>')
    out.append("<h2>Purpose</h2>")
    out.append(f'<p data-role="purpose">{_parts_html(description["purpose"], known)}</p>')
    out.append("<h2>Flow sequence</h2>")
    runs = {}
    for step in description["flow_sequence"]:
        runs.setdefault(step["run"], []).append(step)
    for number in sorted(runs):
        out.append(f'<div class="flow" data-run="{number}">')
        for step in runs[number]:
            link = f'<a class="tag" href="#{_e(anchor(step["semantic_id"]))}">{_e(step["tag"])}</a>'
            cls = "node" if step["item_kind"] == "entity" else "line"
            out.append(f'<span class="{cls}" data-step="{step["step"]}" data-semantic-id="{_e(step["semantic_id"])}" '
                       f'data-occurrence-id="flow/{step["step"]}">{link}</span>')
        out.append("</div>")
    out.append("<table><thead><tr><th>Step</th><th>Tag</th><th>Item</th><th>Connects</th><th>Drawing reference</th>"
               "</tr></thead><tbody>")
    kind_names = {item["semantic_id"]: item["kind_name"] for item in description["items"]}
    for step in description["flow_sequence"]:
        connects = ""
        if step["item_kind"] == "connection":
            connects = f'{_e(step["from_tag"])} → {_e(step["to_tag"])}'
        kind = "process line" if step["item_kind"] == "connection" else kind_names.get(step["semantic_id"], step["kind"])
        out.append(f'<tr><td>{step["step"]}</td><td><a class="tag" href="#{_e(anchor(step["semantic_id"]))}">'
                   f'{_e(step["tag"])}</a></td><td>{_e(kind)}</td><td>{connects}</td>'
                   f'<td>{_e(", ".join(step["zone_refs"]) or "not drawn")}</td></tr>')
    out.append("</tbody></table>")
    notes_by_target = {}
    for note in description["manual_notes"]:
        target = note["note"].get("target_id") if isinstance(note.get("note"), dict) else None
        if note.get("state") in ("current", "stale_review") and target in known:
            notes_by_target.setdefault(target, []).append(note)
    out.append("<h2>Items</h2>")
    for item in description["items"]:
        out.append(f'<section class="item" id="{_e(anchor(item["semantic_id"]))}" data-semantic-id="{_e(item["semantic_id"])}" '
                   f'data-occurrence-id="item/{_e(item["semantic_id"])}" data-fingerprint="{item["fingerprint"]}">')
        refs = ", ".join(item["zone_refs"]) or "not drawn on the sheet"
        out.append(f'<div class="item-head"><h3>{_e(item["tag"])} <span class="kind">{_e(item["kind_name"])}</span></h3>'
                   f'<span class="refs">{_e(refs)}</span></div>')
        out.append(f'<p data-role="function">{_parts_html(item["function"], known)}</p>')
        out.append(f'<div class="basis">{_e(item["intent_basis"])}</div>')
        out.append(_evidence_html(item))
        chips = []
        for aid in item["assumption_ids"]:
            chips.append(f'<a class="chip" href="#asm-{_e(anchor(aid))}">{_e(aid)}</a>')
        for scoped in item.get("definition_assumption_ids", []):
            chips.append(f'<a class="chip local" href="#dasm-{_e(anchor(scoped))}">{_e(scoped.split("/", 1)[1])} '
                         "(definition-local)</a>")
        for uid in item["unknown_ids"]:
            chips.append(f'<a class="chip" href="#{_e(anchor(uid))}">{_e(uid.split("/", 1)[1].replace("-", " "))}</a>')
        if chips:
            out.append(f'<div class="chips"><span class="kind">Assumptions and unknowns</span>{"".join(chips)}</div>')
        for note in notes_by_target.get(item["semantic_id"], []):
            out.append(_note_html(note))
        out.append("</section>")
    out.append("<h2>Unknowns and unresolved decisions</h2><table><thead><tr><th>Topic</th><th>Statement</th>"
               "<th>Applies to</th></tr></thead><tbody>")
    for unknown in description["unknowns"]:
        applies = ", ".join(_e(a["tag"]) for a in unknown["applies_to"])
        out.append(f'<tr id="{_e(anchor(unknown["id"]))}"><td>{_e(unknown["topic"])}</td><td>{_e(unknown["text"])}</td>'
                   f"<td>{applies}</td></tr>")
    out.append("</tbody></table>")
    if description["open_items"]:
        out.append('<h2 class="warn">Open items</h2><table><thead><tr><th>Item</th><th>Finding</th></tr></thead><tbody>')
        for item in description["open_items"]:
            out.append(f'<tr><td>{_e(", ".join(item["tags"]) or "project")}</td><td>{_e(item["message"])}</td></tr>')
        out.append("</tbody></table>")
    out.append("<h2>Assumption register</h2><table><thead><tr><th>ID</th><th>Assumption</th><th>Source</th>"
               "<th>Used by</th></tr></thead><tbody>")
    for record in description["assumptions"]:
        used = sorted({u["tag"] for u in record["used_by"]})
        out.append(f'<tr id="asm-{_e(anchor(record["id"]))}"><td><code>{_e(record["id"])}</code></td>'
                   f'<td>{_e(record["description"])}</td><td>{_e(record["source"])}</td>'
                   f'<td>{_e(", ".join(used) or "not referenced")}</td></tr>')
    out.append("</tbody></table>")
    if description.get("definition_assumptions"):
        out.append("<h2>Definition-local assumptions</h2>")
        out.append('<p class="basis">Recorded inside each composed item\'s equipment definition (its geometry recipe). '
                   "They apply only to that item, are not project model assumptions, and are quoted as authored.</p>")
        out.append("<table><thead><tr><th>Item</th><th>Local ID</th><th>Category</th><th>Assumption</th></tr></thead>"
                   "<tbody>")
        for record in description["definition_assumptions"]:
            target = record["applies_to"]
            out.append(f'<tr id="dasm-{_e(anchor(record["scoped_id"]))}" data-semantic-id="{_e(target["semantic_id"])}">'
                       f'<td><a class="tag" href="#{_e(anchor(target["semantic_id"]))}">{_e(target["tag"])}</a></td>'
                       f'<td><code>{_e(record["local_id"])}</code></td><td>{_e(record["category"].replace("_", " "))}</td>'
                       f'<td>{_e(record["description"])}</td></tr>')
        out.append("</tbody></table>")
    loose = [n for n in description["manual_notes"] if n not in sum(notes_by_target.values(), [])]
    if description["manual_notes"]:
        out.append("<h2>Manual notes</h2>")
        out.append('<p class="basis">Manual notes are kept exactly as written. Notes shown with their items are listed '
                   "there; the notes below target the project, an assumption, a port or a missing item, or are "
                   "malformed.</p>")
        for note in loose:
            out.append(_note_html(note))
    if description["conflicts"]:
        out.append('<h2 class="warn">Conflicts to reconcile</h2><table><thead><tr><th>Note</th><th>Code</th>'
                   "<th>Message</th></tr></thead><tbody>")
        for conflict in description["conflicts"]:
            out.append(f'<tr><td>{_e(conflict["note_id"] or "(sidecar)")}</td><td>{_e(conflict["code"])}</td>'
                       f'<td>{_e(conflict["message"])}</td></tr>')
        out.append("</tbody></table>")
    out.append("</details>")
    out.append(f'<footer>Generated by skid.drafting description {ADAPTER_VERSION} from project '
               f'{_e(description["project_id"])} revision {description["source_revision"]}. Input digest '
               f'<code>{_e(description["input_digest"])}</code>. Original text; no standards conformance or as-built '
               "status is claimed.</footer>")
    out.append("</main></body></html>")
    return "\n".join(out) + "\n"


def _evidence_html(item):
    rows = []
    for evidence in item["evidence"]:
        kind = evidence["kind"]
        if kind == "parameter":
            value = f'{_e(evidence["name"].replace("_", " "))}: {_e(_num(evidence["value"]))} {_e(evidence["unit"])}'
        elif kind == "port":
            value = (f'port {_e(evidence["name"])} ({_e(evidence["direction"])}), DN{_e(_num(evidence["nominal_diameter_mm"]))}, '
                     f'{_e(evidence["service"])} service')
        elif kind == "line":
            value = (f'DN{_e(_num(evidence["nominal_diameter_mm"]))}, outside diameter '
                     f'{_e(_num(evidence["outside_diameter_m"]))} m')
        elif kind == "observes":
            value = f'observes {_e(evidence["variable"].replace("_", " "))} of {_e(evidence["tag"])}'
        elif kind in ("physical_placement", "physical_route"):
            value = f'{"3D placement" if kind == "physical_placement" else "3D route"}: {_e(evidence["status"])}'
            if evidence.get("reason"):
                value += f' — {_e(evidence["reason"])}'
        elif kind == "diagram_edge":
            value = f'P&amp;ID path {_e(evidence["edge_id"])}: {_e(evidence["status"].replace("_", " "))}'
            if evidence.get("manual"):
                value += " (manual)"
        elif kind == "diagram_reference":
            value = f'drawn at {_e(evidence["ref"])}'
        elif kind == "definition":
            value = (f'definition {_e(evidence["name"])} (<code>{_e(evidence["id"])}</code>, {_e(evidence["schema"])}), '
                     f'generic symbol {_e(str(evidence["symbol_shape"]).replace("_", " "))}')
        elif kind == "declared_function":
            value = f'declared function: “{_e(evidence["text"])}”'
        elif kind == "definition_assumption":
            value = f'{_e(evidence["category"].replace("_", " "))}: {_e(evidence["description"])}'
        else:
            value = _e(json.dumps(evidence, sort_keys=True))
        if kind == "definition":
            basis = f'content digest <code>{_e(evidence["digest"][:16])}…</code> (identifies content, not its author)'
        elif kind == "declared_function":
            basis = "authored definition text, not verified"
        elif kind == "definition_assumption":
            basis = (f'definition-local <a href="#dasm-{_e(anchor(evidence["scoped_id"]))}">'
                     f'<code>{_e(evidence["local_id"])}</code></a>')
        elif evidence.get("assumption_id"):
            basis = f'<a href="#asm-{_e(anchor(evidence["assumption_id"]))}"><code>{_e(evidence["assumption_id"])}</code></a>'
        elif evidence.get("source"):
            basis = f'source: {_e(evidence["source"])}'
        else:
            basis = "model record"
        rows.append(f"<tr><td>{value}</td><td>{basis}</td></tr>")
    return '<table class="evidence"><thead><tr><th>Evidence</th><th>Basis</th></tr></thead><tbody>' + "".join(rows) + \
        "</tbody></table>"


def _note_html(note):
    raw = note.get("note")
    state = note.get("state", "invalid")
    if isinstance(raw, dict) and isinstance(raw.get("text"), str):
        body = _e(raw["text"])
        head = f'{_e(raw.get("id", "(no id)"))} on {_e(note.get("target_tag") or raw.get("target_id", "?"))}'
        if isinstance(raw.get("author"), str):
            head += f' by {_e(raw["author"])}'
    else:
        body = _e(json.dumps(raw, ensure_ascii=False, sort_keys=True, default=repr))
        head = "malformed note"
    labels = {"current": "manual note", "stale_review": "manual note — needs review",
              "orphan": "manual note — target missing", "invalid": "manual note — malformed"}
    return (f'<div class="note {_e(state)}" data-note-state="{_e(state)}"><span class="state">{_e(labels.get(state, state))}'
            f'</span> <span class="refs">{head}</span><pre>{body}</pre></div>')
