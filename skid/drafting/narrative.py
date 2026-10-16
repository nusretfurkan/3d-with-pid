"""Readable process narrative composed from flow order, kinds and recorded geometry evidence.

One paragraph per run of the flow graph: the opening item, then each line and
the item it reaches, in port-connectivity order (never tags or layout).
Branches, merges, loops and groups not connected to earlier ones are named as
such; a run is called a loop only when its first item reaches itself. Indicators
are mentioned with the item they observe. Physical words (vertical, high, above,
side, top, end-suction) are used only when a built-in template's geometry and
the item's placed 3D rotation establish them; an unplaced or tilted item gets
no orientation claim. Composed equipment gets no top, bottom or side words,
because a definition port's direction does not prove where it sits on an
arbitrary body. Isolation valves are never called manual or automatic,
because no operator is modeled. Composed equipment is introduced by its
declared definition name and function, quoted as authored text.

Every reference is a stable ID resolved to the current tag, so the text
follows renames, insertions and parameter changes in the same build as every
other output.

Flow is read between ``FlowGraph`` nodes. An item whose definition declares
``flow_paths`` is entered and left through named ports, and flow continues
only along the declared path of the port it entered (quoted as an authored
claim, not verified). A second circuit through the same item is a separate
flow circuit (role ``circuit``), never a merge, branch or loop. Its ports
without a line are stated, as are unconnected required ports of other items
(an open item). Narratives of projects without declared flow paths or
unconnected required ports are unchanged.
"""

from __future__ import annotations

import math

from ..templates import entity_primitives
from .definitions import definition_name, definition_of, is_composed

CLOSING = ("Fluid, duty, operating conditions, valve operators and normal valve positions are not modeled, so this "
           "narrative states none of them. The item details below list the recorded evidence, assumptions and unknowns.")


def _num(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _rotate(rotation_deg, vector):
    """``R = Rz @ Ry @ Rx`` applied to ``vector``: the template and placement convention."""
    x, y, z = vector
    rx, ry, rz = (math.radians(a) for a in rotation_deg)
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return x, y, z


def _vertical(component):
    if component > 0.98:
        return "up"
    if component < -0.98:
        return "down"
    return "level" if abs(component) < 0.02 else None


class _Geometry:
    """Local axes from template or definition geometry, mapped through the placed rotation."""

    def __init__(self, builder):
        self.b = builder

    def rotation(self, eid):
        placement = self.b.placements.get(eid)
        if not placement or placement.get("status") != "placed":
            return None
        return placement.get("rotation_deg")

    def local_ports(self, eid):
        """Built-in port name -> local outward unit vector of its template nozzle, else {}.

        Composed definitions are excluded: their port points and directions are
        authored freely, so an outward direction does not locate the port on the body.
        """
        entity = self.b.entities[eid]
        if is_composed(entity):
            return {}
        try:
            out = {}
            for primitive in entity_primitives(entity):
                if primitive["key"].startswith("nozzle/"):
                    out[primitive["key"][7:]] = _rotate(primitive["rotation_deg"], (0.0, 0.0, 1.0))
            return out
        except (KeyError, ValueError, TypeError):
            return {}

    def primitive(self, eid, key):
        if is_composed(self.b.entities[eid]):
            return None
        try:
            return next((p for p in entity_primitives(self.b.entities[eid]) if p["key"] == key), None)
        except (KeyError, ValueError, TypeError):
            return None

    def facing(self, port_id):
        """``top``, ``bottom`` or ``side`` of a placed built-in item's port in the world frame, else None."""
        port = self.b.ports[port_id]
        rotation = self.rotation(port["entity_id"])
        local = self.local_ports(port["entity_id"]).get(port["name"])
        if rotation is None or local is None:
            return None
        return {"up": "top", "down": "bottom", "level": "side"}.get(_vertical(_rotate(rotation, local)[2]))

    def shell_frame(self, eid):
        """World attitude of a placed built-in cylindrical body: ``upright`` (base end down), ``inverted``,
        ``horizontal``, or None when unplaced, tilted or not a cylinder."""
        body, rotation = self.primitive(eid, "body"), self.rotation(eid)
        if body is None or body["shape"] != "cylinder" or rotation is None:
            return None
        axis = _rotate(rotation, _rotate(body["rotation_deg"], (0.0, 0.0, 1.0)))
        return {"up": "upright", "down": "inverted", "level": "horizontal"}.get(_vertical(axis[2]))

    def end_suction(self, eid):
        """A built-in pump inlet that faces along the shaft, away from the motor (template geometry)."""
        motor, inlet = self.primitive(eid, "motor"), self.local_ports(eid).get("inlet")
        if motor is None or inlet is None:
            return False
        shaft = _rotate(motor["rotation_deg"], (0.0, 0.0, 1.0))
        along = sum(a * b for a, b in zip(shaft, inlet))
        return along < -0.98 and sum(a * b for a, b in zip(shaft, motor["center_m"])) > 0


def _components(keys, pairs):
    parent = {key: key for key in keys}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for a, b in pairs:
        parent[find(a)] = find(b)
    return {key: find(key) for key in keys}


def _words(name):
    return str(name).replace("_", " ")


class _Narrative:
    def __init__(self, builder):
        self.b = builder
        self.graph = builder.graph
        self.entities = builder.entities
        self.geometry = _Geometry(builder)
        self.mentioned = set()
        self.qualified = False  # the observation qualifier is stated in full once
        # Entities joined by any line, and flow nodes joined by lines: they differ only where an item declares
        # separate flow paths, so a second circuit through it shares the item but no fluid.
        self.component = _components(self.entities, self.graph.ends.values())
        self.flow_component = _components(self.graph.node_ports, self.graph.node_ends.values())
        self.told_paths = {}  # entity -> declared paths already described
        self.stated_unconnected = set()  # port IDs already called unconnected
        self.observers = {}
        for oid in self.graph.entity_order:
            observes = self.entities[oid].get("observes")
            subject = observes.get("entity_id") if isinstance(observes, dict) else None
            if subject in self.entities:
                self.observers.setdefault(subject, []).append(oid)

    def on_loop(self, node):
        """True when a path of one or more lines leads from flow node ``node`` back to itself."""
        return any(self.graph.reaches(self.graph.node_ends[cid][1], node) for cid in self.graph.node_out[node])

    def unconnected(self, port_id):
        return not any(port_id in (c["from_port_id"], c["to_port_id"]) for c in self.b.connections.values())

    def path_statement(self, text, node):
        """What happens inside an item with declared flow paths after flow arrives at ``node``."""
        eid = node[0]
        label = self.graph.node_label[node] or {}
        tag = self.entities[eid]["tag"]
        if "flow_port" in label:
            text.add(f" That port lies outside the declared flow paths of {tag}, so the modeled flow ends there.")
            return
        inlet, outlet = label["flow_path"]
        told = self.told_paths.setdefault(eid, [])
        others = [p for p in self.graph.ports_of[eid] if self.b.ports[p]["name"] not in (inlet, outlet)]
        text.add(f" Inside {tag} it follows the declared {_words(inlet)} to {_words(outlet)} flow path")
        if told:
            earlier = " and ".join(f"{_words(a)} to {_words(b)}" for a, b in told)
            text.add(f", separate from the {earlier} path described above, so the two streams do not mix in the "
                     f"model")
        elif others:
            text.add(", which the definition keeps separate from its other ports")
        text.add(" (an authored definition claim, not verified).")
        told.append([inlet, outlet])
        by_name = {self.b.ports[p]["name"]: p for p in self.graph.ports_of[eid]}
        out_port = by_name.get(outlet)
        if out_port is None or self.unconnected(out_port):
            if out_port is not None:
                self.stated_unconnected.add(out_port)
            text.add(f" Its {_words(outlet)} has no connection in the model, so this flow path ends there.")

    # --- Phrases ------------------------------------------------------------------------------

    def kind(self, eid):
        return self.entities[eid]["kind"]

    def parameter(self, eid, name):
        quantity = self.entities[eid]["parameters"].get(name)
        return quantity if isinstance(quantity, dict) and quantity.get("unit") == "m" else None

    def unplaced(self, text, eid):
        placement = self.b.placements.get(eid)
        if placement is None or placement.get("status") != "placed":
            text.add(" (its 3D placement is not yet resolved)")

    def describe(self, text, eid, first=False, capital=False):
        """``<kind phrase> <TAG>`` with the evidence the model supports; full detail on first mention only.

        ``first`` adds an indefinite article; ``capital`` starts a sentence.
        """
        kind = self.kind(eid)
        full = eid not in self.mentioned
        self.mentioned.add(eid)
        entity = self.entities[eid]
        composed = is_composed(entity)
        frame = self.geometry.shell_frame(eid) if kind == "tank" and full else None
        if kind == "tank":
            axis = {"upright": "vertical", "inverted": "vertical", "horizontal": "horizontal"}.get(frame)
            noun = f"{axis + ' ' if axis else ''}cylindrical tank" if full else "tank"
        elif composed:
            noun = ""
        else:
            noun = {"centrifugal_pump": "centrifugal pump", "isolation_valve": "isolation valve",
                    "check_valve": "check valve", "boundary": "scope boundary"}.get(kind)
            noun = noun or f"item of kind {str(kind).replace('_', ' ')}"
        if noun:
            if first:
                noun = ("an " if noun[0] in "aeiou" else "a ") + noun
            text.add((noun[0].upper() + noun[1:] if capital else noun) + " ")
        text.ref(eid)
        if not full:
            return
        if kind == "tank":
            diameter, height = self.parameter(eid, "diameter"), self.parameter(eid, "height")
            if diameter and height:
                assumed = any("assumption_id" in q for q in (diameter, height))
                # The template "height" is the shell length along its axis; it is a world height only when vertical.
                length = {"upright": "{} m high", "inverted": "{} m high", "horizontal": "{} m long"}.get(
                    frame, "with a {} m shell length along its axis")
                joiner = " and " if frame else " "
                text.add(f", {_num(diameter['value'])} m in diameter{joiner}{length.format(_num(height['value']))}"
                         f"{' (assumed dimensions)' if assumed else ''}")
        elif composed:
            text.add(f", composed equipment from the definition “{definition_name(entity)}”")
            declared = definition_of(entity).get("function")
            if isinstance(declared, str) and declared.strip():
                text.add(f" with the declared function “{' '.join(declared.split())}” (authored, not verified)")
            else:
                text.add(" that declares no function")
        self.unplaced(text, eid)

    def observations(self, text, eid):
        for oid in self.observers.get(eid, []):
            if oid in self.mentioned:
                continue
            self.mentioned.add(oid)
            variable = str(self.entities[oid]["observes"].get("variable", "")).replace("_", " ").strip()
            label = f"{variable} indicator" if variable else "indicator"
            text.add(f" {label[0].upper()}{label[1:]} ").ref(oid).add(" observes ")
            text.add(f"the {variable} of " if variable else "").ref(eid)
            if self.qualified:
                text.add(", also as a conceptual observation.")
            else:
                text.add("; this is a conceptual observation, and its hookup point and range are not modeled.")
                self.qualified = True

    def leaving(self, text, source, cid, opening, branch=False):
        """Clause from ``source`` into line ``cid``."""
        kind = self.kind(source)
        inline = kind in ("isolation_valve", "check_valve")  # an inline body's end faces carry no orientation meaning
        facing = None if inline else self.geometry.facing(self.b.connections[cid]["from_port_id"])
        if self.graph.flow_paths(source):
            # An item with declared flow paths is left through a named port, never "the" outlet of the item.
            port = _words(self.b.ports[self.b.connections[cid]["from_port_id"]]["name"])
            text.add("A branch leaves " if branch else "Flow leaves ").ref(source)
            text.add(f" from its {port} through line ").ref(cid)
        elif branch:
            text.add("A branch leaves ").ref(source).add(" through line ").ref(cid)
            if facing:
                text.add(f" from its {facing} outlet")
        elif kind == "centrifugal_pump":
            text.ref(source).add(" discharges")
            if facing:
                text.add(f" from its {facing} outlet")
            text.add(" through line ").ref(cid)
        elif opening or kind == "tank" or is_composed(self.entities[source]):
            text.add("Flow leaves ").ref(source).add(" through line ").ref(cid)
            if facing:
                text.add(f" from its {facing} outlet")
                # outlet_height is measured up the shell axis from its base: "above" only while that base is down.
                upright = kind == "tank" and facing == "side" and self.geometry.shell_frame(source) == "upright"
                height = self.parameter(source, "outlet_height") if upright else None
                if height:
                    text.add(f", {_num(height['value'])} m above the shell base"
                             f"{' (assumed)' if 'assumption_id' in height else ''},")
        else:
            text.add("Line ").ref(cid).add(" continues from ").ref(source)

    def arriving(self, text, target, cid, closes):
        """Clause from line ``cid`` into ``target``, ending the sentence."""
        source_node, target_node = self.graph.node_ends[cid]
        port = _words(self.b.ports[self.b.connections[cid]["to_port_id"]]["name"])
        if closes:
            at = f" at its {port}" if self.graph.flow_paths(target) else ""
            if self.graph.reaches(target_node, source_node):
                text.add(" back to ").ref(target).add(f"{at}, closing a loop in the modeled flow.")
            else:
                text.add(" to ").ref(target).add(f"{at}, where it joins the flow path described above.")
            return
        kind = self.kind(target)
        if self.graph.flow_paths(target):
            text.add(f" to the {port} of ")
            self.describe(text, target)
            text.add(".")
            self.path_statement(text, target_node)
        elif kind == "centrifugal_pump":
            inlet = " end-suction inlet" if self.geometry.end_suction(target) else " suction"
            text.add(f" to the{inlet} of ")
            self.describe(text, target)
            text.add(".")
        elif kind == "isolation_valve":
            text.add(" to ")
            self.describe(text, target)
            pumps_down = self.b._reach(target, "down", "centrifugal_pump")
            pumps_up = self.b._reach(target, "up", "centrifugal_pump")
            if pumps_down:
                text.add(", on the suction side of ").refs(pumps_down)
            elif pumps_up:
                text.add(", on the discharge side of ").refs(pumps_up)
            text.add(".")
        elif kind == "check_valve":
            text.add(" to ")
            self.describe(text, target)
            text.add(", which permits flow in this direction and is intended to prevent reverse flow.")
        elif kind == "boundary":
            text.add(" to ")
            self.describe(text, target)
            text.add(", where the modeled scope ends; the destination beyond it is not modeled.")
        else:
            text.add(" to ")
            self.describe(text, target)
            port_id = self.b.connections[cid]["to_port_id"]
            facing = self.geometry.facing(port_id)
            if facing and kind != "tank":
                text.add(f", entering at its {facing} inlet")
            text.add(".")
        self.observations(text, target)

    # --- Paragraphs ----------------------------------------------------------------------------

    def relation(self, node, seen):
        """``joined`` (shares a line with earlier flow), ``circuit`` (shares only an item with declared flow
        paths, through a separate path) or ``separate``."""
        if ("flow", self.flow_component[node]) in seen:
            return "joined"
        return "circuit" if ("entity", self.component[node[0]]) in seen else "separate"

    def opening(self, text, node, index, seen):
        start = node[0]
        relation = self.relation(node, seen)
        if not (self.graph.incoming[start] or self.graph.outgoing[start]):
            self.describe(text, start, first=True, capital=True)
            text.add(" is not connected to any process line.")
        elif not self.graph.node_in[node]:
            if index == 0:
                text.add("The design starts with ")
            elif relation == "joined":
                text.add("A second flow path starts with ")
            elif relation == "circuit":
                text.add("A separate flow circuit, sharing no line with the flow above, starts with ")
            else:
                text.add("A separate group, not connected to the items above, starts with ")
            self.describe(text, start, first=True)
            text.add(".")
            self.unpaired_start(text, node)
        else:
            # FlowGraph starts a fed item only after every reachable item is told, so its feeds are told later.
            # It is called a loop only when it reaches itself; a tail fed from a loop is not.
            if index == 0:
                text.add("The reading starts at ")
            elif relation == "joined":
                text.add("Another part of the same connected flow is read from ")
            elif relation == "circuit":
                text.add("A separate flow circuit, sharing no line with the flow above, is read from ")
            else:
                text.add("A separate group, not connected to the items above, is read from ")
            self.describe(text, start, first=True)
            text.add(", which lies on a closed loop in the modeled flow." if self.on_loop(node)
                     else ", which is fed only by lines described below.")
        self.observations(text, start)

    def unpaired_start(self, text, node):
        """A run starting inside an item with declared flow paths: name the path and its unconnected inlet."""
        label = self.graph.node_label[node]
        if not label:
            return
        tag = self.entities[node[0]]["tag"]
        if "flow_port" in label:
            text.add(f" Flow starts at its {_words(label['flow_port'])}, a port outside the declared flow paths of "
                     f"{tag}.")
            return
        inlet, outlet = label["flow_path"]
        text.add(f" This reading follows its declared {_words(inlet)} to {_words(outlet)} flow path (an authored "
                 f"definition claim, not verified).")
        self.told_paths.setdefault(node[0], []).append([inlet, outlet])
        by_name = {self.b.ports[p]["name"]: p for p in self.graph.ports_of[node[0]]}
        if by_name.get(inlet) is None or self.unconnected(by_name[inlet]):
            if by_name.get(inlet) is not None:
                self.stated_unconnected.add(by_name[inlet])
            text.add(f" Its {_words(inlet)} has no connection in the model.")

    def run(self, steps, index, seen):
        text = self.b.text()
        first = steps[0]
        if first["item_kind"] == "entity":
            node = self.graph.step_node(first)
            relation = self.relation(node, seen)
            role = ("flow" if index == 0 else "loop" if self.graph.node_in[node] and self.on_loop(node) else
                    "path" if relation == "joined" else relation)
            self.opening(text, node, index, seen)
            current, rest, opening = first["id"], steps[1:], True
        else:
            role, current, rest, opening = "branch", first["branch_from"], steps, False
            node = self.graph.node_ends[first["id"]][0]
        seen.add(("flow", self.flow_component[node]))
        seen.add(("entity", self.component[current]))
        position = 0
        while position < len(rest):
            step = rest[position]
            target = self.graph.ends[step["id"]][1]
            text.add(" ")
            self.leaving(text, current, step["id"], opening and position == 0, branch=role == "branch" and not position)
            self.arriving(text, target, step["id"], step["closes_loop"])
            current, opening = target, False
            position += 1 if step["closes_loop"] else 2
        record = text.record()
        record["text"] = record["text"].strip()
        for end in (0, -1):
            if record["parts"] and "text" in record["parts"][end]:
                record["parts"][end]["text"] = (record["parts"][end]["text"].lstrip() if end == 0
                                                else record["parts"][end]["text"].rstrip())
        record.update({"role": role, "run": first["run"]})
        return record

    def open_ports(self, text):
        """Unconnected ports stay explicit: every port of an item with declared flow paths, and required ports
        of other items (an open item that keeps outputs pending), unless already stated in a run."""
        for eid in self.graph.entity_order:
            paths = self.graph.flow_paths(eid)
            ports = [p for p in self.graph.ports_of[eid] if p not in self.stated_unconnected and self.unconnected(p)
                     and (paths or self.b.ports[p]["required"])]
            if not ports or not (self.graph.incoming[eid] or self.graph.outgoing[eid]):
                continue  # an item without any line is already told as not connected
            names = [_words(self.b.ports[p]["name"]) for p in ports]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            plural = len(names) > 1
            marked = any("connection_status" in self.b.ports[p] for p in ports)
            text.add(f"{'Ports' if plural else 'Port'} {listed} of ").ref(eid)
            if paths:
                text.add(f" {'have' if plural else 'has'} no process connection in the model")
                text.add("; open required ports are marked unresolved, which keeps the outputs pending. " if marked
                         else ". ")
            else:
                text.add(f" {'are' if plural else 'is'} required and explicitly unresolved (not connected yet), an "
                         "open item that keeps the outputs pending. ")
            self.stated_unconnected.update(ports)

    def closing(self):
        text = self.b.text()
        for eid in self.graph.entity_order:
            if eid in self.mentioned:
                continue
            entity = self.entities[eid]
            observes = entity.get("observes")
            if isinstance(observes, dict):
                subject = observes.get("entity_id")
                variable = str(observes.get("variable", "")).replace("_", " ").strip()
                text.add("Indicator ").ref(eid)
                if subject in self.entities:
                    text.add(f" observes {'the ' + variable + ' of ' if variable else ''}").ref(subject)
                    text.add(", which has no process connection; this is a conceptual observation. ")
                else:
                    text.add(" has no observed item in the model. ")
            else:
                self.describe(text, eid, first=True, capital=True)
                text.add(" has no process ports, so it is outside the flow path. ")
            self.mentioned.add(eid)
        sinks = [s["id"] for s in self.graph.steps if s["item_kind"] == "entity"
                 and not self.graph.outgoing[s["id"]] and self.kind(s["id"]) != "boundary"
                 and self.graph.incoming[s["id"]]]
        if sinks:
            text.add("The modeled flow ends at ").refs(list(dict.fromkeys(sinks)))
            text.add(" with no scope boundary after it, so what lies downstream is not recorded. ")
        self.open_ports(text)
        if not any(self.kind(e) == "boundary" for e in self.entities):
            text.add("No scope boundary is modeled. ")
        text.add(CLOSING)
        record = text.record()
        record.update({"role": "scope", "run": None})
        return record


def process_narrative(builder) -> list[dict]:
    """Paragraph records ``{text, parts, refs, role, run}`` in flow order, ending with a scope paragraph."""
    narrative = _Narrative(builder)
    seen_components = set()
    paragraphs = [narrative.run(steps, index, seen_components)
                  for index, steps in enumerate(narrative.graph.runs_in_order())]
    if not paragraphs:
        text = builder.text().add("The model contains no process items with ports, so no flow can be described.")
        paragraphs.append(dict(text.record(), role="flow", run=None))
    paragraphs.append(narrative.closing())
    return paragraphs
