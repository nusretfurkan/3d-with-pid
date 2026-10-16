"""Deterministic, read-only queries over one frozen project revision.

Every fact comes from the authoritative engineering model of the revision the
query was built from: stable IDs, current tags, recorded port semantics and
connections (``from_port_id`` -> ``to_port_id``), parameters with their
provenance, placements, routes, assumptions and open validation items. Never
from tag spelling, P&ID coordinates, 3D proximity or a generated document.

Directed process connectivity is the model's nominal intent. A connection runs
from its ``from`` port to its ``to`` port; one whose two ports are both
bidirectional (``both``), whose port directions contradict that order, or
whose ports are missing has no established direction. Such connections are
reported separately and never followed by traversal. Instrument observations
(``observes``) are not process connections, and the model has no signal or
control connections. Expanded traversal lists every branch, marks re-joins and
loops, and stops at fixed bounds with an explicit note.

Flow continues between *flow nodes*, never between bare entity IDs. An entity
whose template declares no ``flow_paths`` (every built-in kind and every
legacy definition) is one node holding all its ports, which is exactly the
entity-level reading. A composed entity whose validated definition declares
``flow_paths`` has one node per declared ``[inlet, outlet]`` pair and one per
port outside every pair, so flow arriving at one declared path continues only
from that path's ports: separate circuits through one item (the tube and shell
sides of an exchanger) are never joined, looped or related through the item's
identity, and an unpaired (auxiliary) port is no bridge. Only a recorded
connection joins them. Each pair is directional: relations pass through it only
from its inlet to its outlet, whatever the port directions, so an outlet reaches
its own inlet only through recorded connections. A definition that declares
``flow_paths`` but cannot be validated passes no flow at all (one node per
port). Traces and relations name the declared path of every such node;
``skid.drafting.graph.FlowGraph`` reads circuits the same way, but nothing here
imports it.

Mentions are resolved against the current tags and stable IDs only, so a
renamed item is found by its new tag and an unknown or ambiguous one is
reported instead of guessed. Nothing here mutates its input, imports drafting
or CAD code, or calls a language model.
"""

from __future__ import annotations

import difflib
import math
import re

from ..core.validation import normalize_tag, validate_project
from ..equipment import entity_template
from ..planning.context import STANDING_UNKNOWNS
from ..templates import world_port_position

__all__ = ["DIRECTION_NOTE", "MAX_DEPTH", "MAX_EDGES", "Mentions", "ProjectQuery", "SIGNALS_NOTE", "STANDING_UNKNOWNS",
           "num", "plain", "vec"]

MAX_DEPTH = 32  # connections along one traced path
MAX_EDGES = 64  # connections listed by one trace (one target, one direction)
MAX_FACTS = 24
MAX_FIELD_PATHS = 24
MAX_FACT_CHARS = 400
MAX_LISTED = 64  # items listed by an overview
SIGNALS_NOTE = ("The model records no signal, control or interlock connections; instrument observations are not "
                "process flow.")
DIRECTION_NOTE = ("Direction is the model's nominal connectivity (from_port_id to to_port_id with the recorded port "
                  "directions), not a simulation or measurement of actual flow.")
COMPOSED_KIND = "composed_equipment"
_TAG_CHARS = r"A-Za-z0-9_./#-"
_EXCLUDED_PREFIXES = frozenset(("dn", "pn", "nps", "iso", "isa", "iec", "en", "din", "api", "asme", "ansi", "bs",
                                "jis", "sch", "cl", "class", "rev", "r", "utf", "sha", "step", "q", "e", "x"))
_TAGLIKE = re.compile(r"(?<![A-Za-z0-9_./#-])([A-Za-z]{1,6}-\d{1,6}[A-Za-z]?)(?:\.([A-Za-z][A-Za-z0-9_]{0,40}))?"
                      r"(?![A-Za-z0-9_/#-]|\.[A-Za-z0-9])")
_IDLIKE = re.compile(r"(?<![A-Za-z0-9_.:-])((?:ent|port|conn|sym|edge)-[A-Za-z0-9][A-Za-z0-9_.:-]{0,120})")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=MAX_FACT_CHARS):
    """Plain single-paragraph text: control characters become spaces, then clipped."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


plain = _text


def num(value):
    """A compact number (six significant digits); anything else as text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _text(value, 40)
    try:
        if not math.isfinite(value):
            return str(value)
    except OverflowError:
        return str(value)
    text = f"{value:.6g}"
    return "0" if text == "-0" else text


def vec(values, sep=" / "):
    if not isinstance(values, (list, tuple)):
        return _text(values, 60)
    return sep.join(num(v) for v in values)


def _article(phrase):
    return "an" if phrase[:1].lower() in "aeiou" else "a"


def _template(entity):
    try:
        return entity_template(entity)
    except Exception:  # noqa: BLE001 - an unresolvable template is reported as unknown, never guessed
        return None


class Mentions:
    """Targets named in a question: resolved stable IDs, unknown tokens and ambiguous tokens, plus masked text."""

    def __init__(self, targets, unknown, ambiguous, masked):
        self.targets = targets  # semantic IDs in order of first mention
        self.unknown = unknown  # [token]
        self.ambiguous = ambiguous  # [(token, [semantic IDs])]
        self.masked = masked  # the question with every resolved mention replaced by " X "


class ProjectQuery:
    """Indexes one project revision. Construction never mutates ``project``."""

    def __init__(self, project):
        self.project = project if isinstance(project, dict) else {}
        model = _mapping(self.project.get("model"))
        physical = _mapping(self.project.get("physical"))
        pid = _mapping(self.project.get("pid"))
        self.entities = _mapping(model.get("entities"))
        self.ports = _mapping(model.get("ports"))
        self.connections = _mapping(model.get("connections"))
        self.assumptions = _mapping(model.get("assumptions"))
        self.placements = _mapping(physical.get("placements"))
        self.routes = _mapping(physical.get("routes"))
        self.symbols = _mapping(pid.get("symbols"))
        self.edges = _mapping(pid.get("edges"))
        self.revision = self.project.get("revision")
        self.templates = {eid: _template(entity) for eid, entity in self.entities.items()}
        self.ports_of = {eid: [] for eid in self.entities}
        for port_id, port in sorted(self.ports.items()):
            owner = port.get("entity_id") if isinstance(port, dict) else None
            if owner in self.ports_of:
                self.ports_of[owner].append(port_id)
        for eid, port_ids in self.ports_of.items():
            order = list((self.templates[eid] or {}).get("ports") or ())
            port_ids.sort(key=lambda p: (order.index(self.ports[p].get("name")) if self.ports[p].get("name") in order
                                         else len(order), str(self.ports[p].get("name")), p))
        self.connections_of_port = {}
        self.incoming = {eid: [] for eid in self.entities}
        self.outgoing = {eid: [] for eid in self.entities}
        self.undirected = {eid: [] for eid in self.entities}  # connections without an established direction
        for cid in sorted(self.connections):
            connection = self.connections[cid]
            ends = self.ends(cid)
            for port_id in (connection.get("from_port_id"), connection.get("to_port_id")):
                if isinstance(port_id, str):
                    self.connections_of_port.setdefault(port_id, []).append(cid)
            source, target = ends
            if self.direction(cid)[0] == "directed":
                self.outgoing[source].append(cid)
                self.incoming[target].append(cid)
            else:
                for eid in dict.fromkeys(e for e in ends if e in self.entities):
                    self.undirected[eid].append(cid)
        self.observers = {eid: [] for eid in self.entities}
        for eid in sorted(self.entities):
            observes = self.entities[eid].get("observes")
            subject = observes.get("entity_id") if isinstance(observes, dict) else None
            if subject in self.observers and subject != eid:
                self.observers[subject].append(eid)
        self._build_flow_nodes()
        self._patterns = None
        self._open_items = None

    # --- Flow nodes ------------------------------------------------------------------

    def _build_flow_nodes(self):
        """``(entity id, rank)`` nodes: ``nodes_of``, ``node_label``, ``node_ports``, ``node_of_port`` and the
        directed (``node_in``/``node_out``) and undirected connections of each node, in connection-ID order."""
        self.flow_paths = {}  # entity -> its validated declared [inlet, outlet] pairs
        self.unverified = set()  # entities declaring flow_paths whose definition cannot be validated
        self.nodes_of, self.node_label, self.node_ports, self.node_of_port = {}, {}, {}, {}
        for eid in sorted(self.entities):
            template = self.templates.get(eid)
            definition = self.entities[eid].get("definition")
            paths = template.get("flow_paths") if isinstance(template, dict) else None
            port_ids = self.ports_of.get(eid, [])
            if paths:
                self.flow_paths[eid] = [list(pair) for pair in paths]
                paired = [name for pair in paths for name in pair]
                groups = [({"flow_path": list(pair)},
                           [p for name in pair for p in port_ids if self.ports[p].get("name") == name])
                          for pair in paths]
                groups += [({"flow_port": self.ports[p].get("name")}, [p]) for p in port_ids
                           if self.ports[p].get("name") not in paired]
            elif template is None and isinstance(definition, dict) and "flow_paths" in definition:
                self.unverified.add(eid)
                groups = [({"flow_port": self.ports[p].get("name")}, [p]) for p in port_ids]
            else:
                groups = [(None, list(port_ids))]
            self.nodes_of[eid] = []
            for rank, (label, members) in enumerate(groups):
                node = (eid, rank)
                self.nodes_of[eid].append(node)
                self.node_label[node] = label
                self.node_ports[node] = members
                for port_id in members:
                    self.node_of_port[port_id] = node
        self.node_in = {node: [] for node in self.node_label}
        self.node_out = {node: [] for node in self.node_label}
        self.node_undirected = {node: [] for node in self.node_label}
        for cid in sorted(self.connections):
            connection = self.connections[cid]
            if self.direction(cid)[0] == "directed":
                self.node_out[self.node_of_port[connection["from_port_id"]]].append(cid)
                self.node_in[self.node_of_port[connection["to_port_id"]]].append(cid)
                continue
            for key in ("from_port_id", "to_port_id"):
                port_id = connection.get(key)
                node = self.node_of_port.get(port_id) if isinstance(port_id, str) else None
                if node is not None and cid not in self.node_undirected[node]:
                    self.node_undirected[node].append(cid)

    def split(self, eid):
        """True when flow through ``eid`` follows separate nodes (declared or unverifiable flow paths)."""
        return eid in self.flow_paths or eid in self.unverified

    def _order(self, port_id):
        """A port's position along its declared ``[inlet, outlet]`` pair: 1 for the outlet, 0 for the inlet and for
        every port outside a validated pair. Relations pass through a declared path only from a lower position to a
        higher one, whatever the recorded port directions (``both`` included)."""
        label = self.node_label.get(self.node_of_port.get(port_id)) or {}
        pair = label.get("flow_path")
        name = self.ports[port_id].get("name") if pair else None
        return pair.index(name) if pair and name in pair else 0

    def _port_names(self, port_ids):
        return ", ".join(_text(self.ports[p].get("name"), 40) for p in port_ids)

    def path_phrase(self, pair):
        return f"{_text(pair[0], 40)} to {_text(pair[1], 40)}"

    def label_phrase(self, label):
        """``flow path tube_inlet to tube_outlet`` / ``port vent, outside its declared flow paths``."""
        if "flow_path" in label:
            return f"flow path {self.path_phrase(label['flow_path'])}"
        return f"port {_text(label.get('flow_port'), 40)}, outside its declared flow paths"

    def node_text(self, eid, label=None):
        """The tag of an entity, plus its declared path or port when flow through it is split."""
        if not label:
            return self.tag(eid)
        if eid in self.unverified:
            return f"{self.tag(eid)} (port {_text(label.get('flow_port'), 40)}; flow paths not validated)"
        return f"{self.tag(eid)} ({self.label_phrase(label)})"

    def _item_text(self, item):
        if not item.get("entity"):
            return "?"
        return self.node_text(item["entity"], {k: item[k] for k in ("flow_path", "flow_port") if k in item})

    def _label_of(self, node):
        label = self.node_label.get(node)
        return {key: list(value) if isinstance(value, list) else value for key, value in label.items()} \
            if label else {}

    def circuit_summary(self, eid):
        """One sentence naming the separate declared flow paths of ``eid`` (empty when flow is not split)."""
        tag = self.tag(eid)
        if eid in self.unverified:
            return (f"{tag}'s definition declares flow paths that could not be validated, so no flow is passed "
                    f"through {tag}: each of its ports is treated on its own.")
        if eid not in self.flow_paths:
            return ""
        paths = "; ".join(self.path_phrase(pair) for pair in self.flow_paths[eid])
        text = (f"Declared flow paths of {tag}: {paths}. They are separate circuits through {tag}: flow entering "
                "one path continues only from that path's ports, never into another path through the item itself; "
                "only a recorded connection can join them.")
        outside = [p for node in self.nodes_of[eid] if "flow_port" in (self.node_label[node] or {})
                   for p in self.node_ports[node]]
        if outside:
            text += f" Port(s) outside every declared path ({self._port_names(outside)}) pass no flow through {tag}."
        return text

    def port_circuit(self, port_id):
        """How a port of a split entity takes part in its circuits (empty for an entity-level port)."""
        owner = self.ports[port_id].get("entity_id")
        node = self.node_of_port.get(port_id)
        if node is None or not self.split(owner):
            return ""
        tag, label = self.tag(owner), self.node_label[node]
        if owner in self.unverified:
            return (f"{self.tag(port_id)}: {tag}'s declared flow paths could not be validated, so no flow passes "
                    f"through {tag} from this port.")
        if "flow_port" in label:
            return f"{self.tag(port_id)} is outside every declared flow path of {tag}, so no flow passes through " \
                   f"{tag} from it."
        others = [self.tag(p) for p in self.node_ports[node] if p != port_id]
        linked = (f"through {tag} it is linked only with {', '.join(others)}" if others else
                  "no other port of this path is recorded")
        return (f"{self.tag(port_id)} is in the declared flow path {self.path_phrase(label['flow_path'])} of {tag}; "
                f"{linked}, never with another declared path.")

    def _end_circuits(self, cid):
        """``[(note, on_path)]`` for the ends of a connection that lie on split entities; ``on_path`` is true when
        the end is a port of a validated declared path."""
        connection = self.connections[cid]
        notes = []
        for key, verb in (("to_port_id", "enters"), ("from_port_id", "leaves")):
            port_id = connection.get(key)
            node = self.node_of_port.get(port_id) if isinstance(port_id, str) else None
            if node is None or not self.split(node[0]):
                continue
            label = self.node_label[node]
            if node[0] in self.unverified:
                notes.append((f"it {verb} {self.tag(node[0])} at {self.tag(port_id)}; the declared flow paths of "
                              f"{self.tag(node[0])} could not be validated, so no flow is passed through it", False))
            elif "flow_path" in label:
                notes.append((f"it {verb} {self.tag(node[0])} through its declared flow path "
                              f"{self.path_phrase(label['flow_path'])} (at {self.tag(port_id)})", True))
            else:
                notes.append((f"it {verb} {self.tag(node[0])} at {self.tag(port_id)}, outside every declared flow "
                              "path, so no flow is passed through the item from there", False))
        return notes

    def flow_note(self, limit=4):
        """A note on the split items of the project for relation answers (empty when there are none)."""
        declared = sorted(self.flow_paths, key=lambda e: (self.tag(e).casefold(), e))
        parts = []
        if declared:
            named = [f"{self.tag(e)} ({'; '.join(self.path_phrase(p) for p in self.flow_paths[e])})"
                     for e in declared[:limit]]
            more = f" and {len(declared) - limit} further item(s)" if len(declared) > limit else ""
            parts.append(f"Declared flow paths were followed one at a time: {', '.join(named)}{more}. A path through "
                         "such an item continues only from the same declared path, never into another one through "
                         "the item itself; only a recorded connection can join them.")
        unverified = sorted(self.unverified, key=lambda e: (self.tag(e).casefold(), e))
        if unverified:
            more = f" and {len(unverified) - limit} further item(s)" if len(unverified) > limit else ""
            parts.append(f"No path was followed through {', '.join(self.tag(e) for e in unverified[:limit])}{more}: "
                         "its declared flow paths could not be validated.")
        return " ".join(parts)

    # --- Records ---------------------------------------------------------------------

    def kind_of(self, record_id):
        if record_id in self.entities:
            return "entity"
        if record_id in self.ports:
            return "port"
        if record_id in self.connections:
            return "connection"
        return None

    def semantic_ids(self):
        return sorted(set(self.entities) | set(self.ports) | set(self.connections))

    def resolve_id(self, value):
        """A semantic ID (entity, port or connection) for an ID, a P&ID symbol ID or a P&ID edge ID; else None."""
        if self.kind_of(value):
            return value
        symbol = self.symbols.get(value)
        if isinstance(symbol, dict) and symbol.get("entity_id") in self.entities:
            return symbol["entity_id"]
        edge = self.edges.get(value)
        if isinstance(edge, dict) and edge.get("connection_id") in self.connections:
            return edge["connection_id"]
        return None

    def tag(self, record_id):
        if record_id in self.entities:
            return _text(self.entities[record_id].get("tag") or record_id, 80)
        if record_id in self.ports:
            port = self.ports[record_id]
            owner = self.entities.get(port.get("entity_id"))
            owner_tag = _text(owner.get("tag"), 80) if isinstance(owner, dict) else "?"
            return f"{owner_tag}.{_text(port.get('name'), 40)}"
        if record_id in self.connections:
            return _text(self.connections[record_id].get("tag") or record_id, 80)
        return _text(record_id, 80)

    def ends(self, cid):
        connection = self.connections.get(cid) or {}
        owners = []
        for key in ("from_port_id", "to_port_id"):
            port = self.ports.get(connection.get(key))
            owners.append(port.get("entity_id") if isinstance(port, dict) else None)
        return tuple(owners)

    def port_end(self, port_id):
        """``TAG.port`` for a port, with an explicit note when the port record is missing."""
        if port_id in self.ports:
            return self.tag(port_id)
        return f"missing port {_text(port_id, 80)!s}"

    def direction(self, cid):
        """``(state, note)``; state is directed, undetermined or broken."""
        connection = self.connections.get(cid) or {}
        source = self.ports.get(connection.get("from_port_id"))
        target = self.ports.get(connection.get("to_port_id"))
        if not isinstance(source, dict) or not isinstance(target, dict):
            return "broken", "a port record of this connection is missing, so its direction is unknown"
        if source.get("entity_id") not in self.entities or target.get("entity_id") not in self.entities:
            return "broken", "a port of this connection belongs to no recorded entity, so its direction is unknown"
        out, into = source.get("direction"), target.get("direction")
        if out == "both" and into == "both":
            return "undetermined", (f"both ports are bidirectional; the recorded order (from "
                                    f"{self.port_end(connection.get('from_port_id'))} to "
                                    f"{self.port_end(connection.get('to_port_id'))}) does not establish a nominal "
                                    "flow direction")
        if out in ("out", "both") and into in ("in", "both"):
            return "directed", f"nominal direction from {self.port_end(connection.get('from_port_id'))} to " \
                               f"{self.port_end(connection.get('to_port_id'))} (port directions {out} to {into})"
        return "undetermined", (f"the port directions ({_text(out, 20)} at the from end, {_text(into, 20)} at the to "
                                "end) contradict the recorded order, so the direction is not established")

    def open_items(self):
        """Open (warning) validation items of this revision; computed once."""
        if self._open_items is None:
            try:
                self._open_items = [d for d in validate_project(self.project) if d.get("severity") == "warning"]
            except Exception:  # noqa: BLE001 - an unvalidatable snapshot simply reports no open items here
                self._open_items = []
        return self._open_items

    def open_items_for(self, record_id):
        return [d for d in self.open_items() if record_id in (d.get("entity_ids") or ())]

    # --- Mentions --------------------------------------------------------------------

    def _mention_patterns(self):
        if self._patterns is None:
            owners = {}
            for eid, entity in self.entities.items():
                tag = entity.get("tag")
                if isinstance(tag, str) and tag.strip():
                    owners.setdefault(normalize_tag(tag), []).append(eid)
            for cid, connection in self.connections.items():
                tag = connection.get("tag")
                if isinstance(tag, str) and tag.strip():
                    owners.setdefault(normalize_tag(tag), []).append(cid)
            for port_id in self.ports:
                owners.setdefault(normalize_tag(self.tag(port_id)), []).append(port_id)
            patterns = []
            for key, ids in owners.items():
                parts = key.split()
                body = r"\s+".join(re.escape(part) for part in parts)
                flags = re.IGNORECASE
                if len(key) <= 3 and key.isalpha():
                    original = self.tag(ids[0])
                    body, flags = re.escape(original), 0  # short plain words match only with their exact case
                patterns.append((len(key), re.compile(rf"(?<![{_TAG_CHARS}]){body}(?![A-Za-z0-9_/#-]|\.[A-Za-z0-9])",
                                                      flags), sorted(ids)))
            ids = {}
            for record_id in list(self.entities) + list(self.ports) + list(self.connections) + list(
                    self.symbols) + list(self.edges):
                semantic = self.resolve_id(record_id)
                if semantic is not None and isinstance(record_id, str) and record_id.strip():
                    ids.setdefault(record_id.casefold(), set()).add(semantic)
            for key, targets in ids.items():
                patterns.append((len(key), re.compile(rf"(?<![A-Za-z0-9_.:-]){re.escape(key)}"
                                                      rf"(?![A-Za-z0-9_:-]|\.[A-Za-z0-9])", re.IGNORECASE),
                                 sorted(targets)))
            patterns.sort(key=lambda item: -item[0])
            self._patterns = patterns
        return self._patterns

    def mentions(self, text):
        """Resolve tags, port tags (``TAG.port``) and stable/symbol/edge IDs named in ``text``."""
        claims = []
        for _, pattern, ids in self._mention_patterns():
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(start < c_end and c_start < end for c_start, c_end, _, _ in claims):
                    continue
                claims.append((start, end, match.group(0), ids))
        claims.sort()
        targets, ambiguous = [], []
        for _, _, token, ids in claims:
            unique = list(dict.fromkeys(ids))
            if len(unique) == 1:
                if unique[0] not in targets:
                    targets.append(unique[0])
            elif all(token != seen for seen, _ in ambiguous):
                ambiguous.append((_text(token, 80), unique))
        masked, cursor = [], 0
        for start, end, _, _ in claims:
            masked.append(text[cursor:start])
            masked.append(" X ")
            cursor = end
        masked.append(text[cursor:])
        masked = "".join(masked)
        unknown = []
        for match in _TAGLIKE.finditer(masked):
            prefix = match.group(1).split("-", 1)[0].casefold()
            if prefix not in _EXCLUDED_PREFIXES:
                unknown.append(match.group(0))
        for match in _IDLIKE.finditer(masked):
            unknown.append(match.group(1).rstrip(".:"))
        return Mentions(targets, list(dict.fromkeys(_text(u, 80) for u in unknown)), ambiguous, masked)

    def suggestions(self, token, limit=4):
        """Current tags spelled similarly to ``token`` (a spelling suggestion, not a claim of identity)."""
        tags = {}
        for record_id in list(self.entities) + list(self.connections):
            tags.setdefault(normalize_tag(self.tag(record_id)), self.tag(record_id))
        found = difflib.get_close_matches(normalize_tag(token), list(tags), n=limit, cutoff=0.6)
        return [tags[key] for key in found]

    # --- Facts -----------------------------------------------------------------------

    def _assumption_fact(self, aid):
        record = self.assumptions.get(aid)
        if not isinstance(record, dict):
            return f"assumption {_text(aid, 80)}: not recorded in this revision"
        return f"assumption {_text(aid, 80)}: {_text(record.get('description'), 300)}"

    def _quantity(self, quantity):
        if not isinstance(quantity, dict):
            return _text(quantity, 60), None
        provenance = quantity.get("assumption_id")
        source = f"assumption {provenance}" if provenance else (
            f"source {_text(quantity.get('source'), 80)}" if quantity.get("source") else "no recorded provenance")
        return f"{num(quantity.get('value'))} {_text(quantity.get('unit'), 12)} ({source})", provenance

    def placement_fact(self, eid):
        placement = self.placements.get(eid)
        if not isinstance(placement, dict):
            return "placement: no physical placement record (unplaced)", None
        if placement.get("status") == "placed":
            layout = "manual layout choice" if placement.get("manual") else "automatic layout"
            aid = placement.get("assumption_id")
            return (f"placement: equipment origin X / Y / Z = {vec(placement.get('position_m'))} m (world, Z up), "
                    f"rotation {vec(placement.get('rotation_deg'))} deg about fixed X, Y, Z; {layout}"
                    + (f"; assumption {aid}" if aid else "")), aid
        reason = placement.get("reason")
        return f"placement: unresolved (unplaced){': ' + _text(reason, 200) if reason else ''}", None

    def _port_summary(self, port_id):
        port = self.ports[port_id]
        flags = [_text(port.get("direction"), 12), f"DN{num(port.get('nominal_diameter_mm'))}",
                 _text(port.get("service"), 20), "required" if port.get("required") else "optional"]
        return ", ".join(flags)

    def _port_link(self, port_id):
        """How a port is connected: ``L-102 from HV-101.outlet`` / ``L-103 to NRV-101.inlet`` / not connected."""
        links = []
        for cid in self.connections_of_port.get(port_id, ()):
            connection = self.connections[cid]
            if connection.get("to_port_id") == port_id:
                links.append(f"{self.tag(cid)} from {self.port_end(connection.get('from_port_id'))}")
            else:
                links.append(f"{self.tag(cid)} to {self.port_end(connection.get('to_port_id'))}")
        return "; ".join(links) if links else "not connected"

    def entity_facts(self, eid):
        """``(facts, field_paths, assumption IDs)`` for one entity."""
        entity = self.entities[eid]
        base = f"model.entities[{eid}]"
        template = self.templates.get(eid)
        facts = [f"tag {self.tag(eid)} (stable ID {eid})",
                 f"kind {_text(entity.get('kind'), 60)}, template version {num(entity.get('template_version'))}"
                 + ("" if template else "; its template could not be resolved")]
        paths = [f"{base}.tag", f"{base}.kind"]
        aids = []
        definition = entity.get("definition")
        if entity.get("kind") == COMPOSED_KIND and isinstance(definition, dict):
            facts.append(f"composed definition {_text(definition.get('id'), 80)} \"{_text(definition.get('name'), 80)}\"; "
                         f"declared function: {_text(definition.get('function'), 240)}")
            paths.append(f"{base}.definition")
        if self.split(eid):
            facts.append(self.circuit_summary(eid))
            paths.append(f"{base}.definition.flow_paths")
        for name, quantity in sorted(_mapping(entity.get("parameters")).items()):
            text, aid = self._quantity(quantity)
            facts.append(f"parameter {_text(name, 48)} = {text}")
            paths.append(f"{base}.parameters[{name}]")
            if aid:
                aids.append(aid)
        for port_id in self.ports_of.get(eid, ()):
            port = self.ports[port_id]
            facts.append(f"port {_text(port.get('name'), 40)} ({port_id}): {self._port_summary(port_id)}; "
                         f"{self._port_link(port_id)}")
            paths.append(f"model.ports[{port_id}]")
            if port.get("assumption_id"):
                aids.append(port["assumption_id"])
        observes = entity.get("observes")
        if isinstance(observes, dict):
            subject = observes.get("entity_id")
            facts.append(f"observes {self.tag(subject) if subject in self.entities else _text(subject, 80)} "
                         f"{_text(observes.get('variable'), 60)} (an instrument observation, not a process connection)")
            paths.append(f"{base}.observes")
        for observer in self.observers.get(eid, ()):
            variable = self.entities[observer]["observes"].get("variable")
            facts.append(f"observed by {self.tag(observer)} ({_text(variable, 60)}; observation, not process flow)")
            paths.append(f"model.entities[{observer}].observes")
        placement, aid = self.placement_fact(eid)
        facts.append(placement)
        paths.append(f"physical.placements[{eid}]")
        if aid:
            aids.append(aid)
        for item in self.open_items_for(eid)[:4]:
            facts.append(f"open item {_text(item.get('code'), 40)}: {_text(item.get('message'), 240)}")
        return facts, paths, list(dict.fromkeys(aids))

    def connection_facts(self, cid):
        connection = self.connections[cid]
        base = f"model.connections[{cid}]"
        state, note = self.direction(cid)
        facts = [f"line tag {self.tag(cid)} (stable ID {cid})",
                 f"from {self.port_end(connection.get('from_port_id'))} to {self.port_end(connection.get('to_port_id'))}",
                 ("direction: " if state == "directed" else "direction not established: ") + note,
                 f"DN{num(connection.get('nominal_diameter_mm'))}, outside diameter "
                 f"{num(connection.get('outside_diameter_m'))} m"
                 + (f" (assumption {connection['assumption_id']})" if connection.get("assumption_id") else "")]
        paths = [f"{base}.tag", f"{base}.from_port_id", f"{base}.to_port_id", f"{base}.nominal_diameter_mm"]
        aids = [connection["assumption_id"]] if connection.get("assumption_id") else []
        route = self.routes.get(cid)
        if isinstance(route, dict):
            points = route.get("points_m") if isinstance(route.get("points_m"), list) else []
            reason = route.get("reason")
            facts.append(f"physical route: {_text(route.get('mode'), 12)}, {_text(route.get('status'), 20)}, "
                         f"{len(points)} point(s){': ' + _text(reason, 200) if reason else ''}")
            if route.get("assumption_id"):
                aids.append(route["assumption_id"])
        else:
            facts.append("physical route: no route record")
        paths.append(f"physical.routes[{cid}]")
        for edge_id, edge in sorted(self.edges.items()):
            if isinstance(edge, dict) and edge.get("connection_id") == cid:
                facts.append(f"P&ID edge {edge_id}: {'manual' if edge.get('manual') else 'guided'}, "
                             f"{_text(edge.get('status'), 20)}")
                paths.append(f"pid.edges[{edge_id}]")
        for item in self.open_items_for(cid)[:4]:
            facts.append(f"open item {_text(item.get('code'), 40)}: {_text(item.get('message'), 240)}")
        facts[3:3] = [f"flow path: {note}" for note, _ in self._end_circuits(cid)]  # after the direction fact
        return facts, paths, list(dict.fromkeys(aids))

    def port_position(self, port_id):
        try:
            value = world_port_position(self.project, port_id)
        except Exception:  # noqa: BLE001 - unresolved geometry is reported as unknown, never guessed
            return None
        return list(value) if isinstance(value, (list, tuple)) and len(value) == 3 else None

    def port_facts(self, port_id):
        port = self.ports[port_id]
        owner = port.get("entity_id")
        facts = [f"port {self.tag(port_id)} (stable ID {port_id}) of "
                 f"{self.tag(owner) if owner in self.entities else 'no recorded entity'}",
                 f"{self._port_summary(port_id)}"
                 + (f" (assumption {port['assumption_id']})" if port.get("assumption_id") else ""),
                 f"connection: {self._port_link(port_id)}"]
        circuit = self.port_circuit(port_id)
        if circuit:
            facts.append(f"flow path: {circuit}")
        position = self.port_position(port_id)
        facts.append(f"world position X / Y / Z = {vec(position)} m (current model, Z up)" if position else
                     "world position: unknown (its entity is unplaced or its geometry is unresolved)")
        paths = [f"model.ports[{port_id}]"]
        aids = [port["assumption_id"]] if port.get("assumption_id") else []
        return facts, paths, aids

    def reference(self, record_id):
        """The verified reference record ``{id, tag, kind, field_paths, facts}`` of one semantic ID."""
        kind = self.kind_of(record_id)
        if kind == "entity":
            facts, paths, aids = self.entity_facts(record_id)
        elif kind == "port":
            facts, paths, aids = self.port_facts(record_id)
        elif kind == "connection":
            facts, paths, aids = self.connection_facts(record_id)
        else:
            raise KeyError(record_id)
        for aid in aids:
            facts.append(self._assumption_fact(aid))
            paths.append(f"model.assumptions[{aid}]")
        facts = [_text(f) for f in facts]
        if len(facts) > MAX_FACTS:
            facts = facts[:MAX_FACTS - 1] + [f"{len(facts) - MAX_FACTS + 1} further fact(s) not listed"]
        paths = list(dict.fromkeys(_text(p, 240) for p in paths))[:MAX_FIELD_PATHS]
        return {"id": record_id, "tag": self.tag(record_id), "kind": kind, "field_paths": paths, "facts": facts}

    def unknowns_for(self, record_id):
        """What the model does not record for an item (standing unknowns, definition unknowns, open items)."""
        found = list(STANDING_UNKNOWNS)
        entity = self.entities.get(record_id)
        definition = entity.get("definition") if isinstance(entity, dict) else None
        if isinstance(definition, dict) and isinstance(definition.get("unknowns"), list):
            found += [f"{self.tag(record_id)} definition: {_text(u, 300)}" for u in definition["unknowns"][:6]]
        placement = self.placements.get(record_id) if record_id in self.entities else None
        if record_id in self.entities and (not isinstance(placement, dict) or placement.get("status") != "placed"):
            found.append(f"{self.tag(record_id)} has no resolved physical placement, so its coordinates are unknown.")
        return found

    # --- Answers ---------------------------------------------------------------------

    def _kind_phrase(self, eid):
        entity = self.entities[eid]
        definition = entity.get("definition")
        if entity.get("kind") == COMPOSED_KIND and isinstance(definition, dict):
            return f"composed equipment item \"{_text(definition.get('name'), 80)}\""
        return f"{_text(entity.get('kind'), 60).replace('_', ' ')}"

    def identity_lines(self, record_id):
        kind = self.kind_of(record_id)
        if kind == "entity":
            return self._entity_identity(record_id)
        if kind == "connection":
            return self._connection_identity(record_id)
        return self._port_identity(record_id)

    def _entity_identity(self, eid):
        entity = self.entities[eid]
        phrase = self._kind_phrase(eid)
        lines = [f"{self.tag(eid)} ({eid}) is {_article(phrase)} {phrase} (kind {_text(entity.get('kind'), 60)}, "
                 f"template version {num(entity.get('template_version'))}) in the current model."]
        definition = entity.get("definition")
        if entity.get("kind") == COMPOSED_KIND and isinstance(definition, dict):
            lines.append(f"Declared function (from its definition {_text(definition.get('id'), 80)}): "
                         f"{_text(definition.get('function'), 300)}")
        port_ids = self.ports_of.get(eid, [])
        if port_ids:
            parts = [f"{_text(self.ports[p].get('name'), 40)} ({self._port_summary(p)}): {self._port_link(p)}"
                     for p in port_ids]
            lines.append("Ports: " + "; ".join(parts) + ".")
        else:
            lines.append("It has no process ports, so it is not part of the process flow.")
        if self.split(eid):
            lines.append(self.circuit_summary(eid))
        observes = entity.get("observes")
        if isinstance(observes, dict):
            subject = observes.get("entity_id")
            lines.append(f"It observes {self.tag(subject) if subject in self.entities else _text(subject, 80)} "
                         f"{_text(observes.get('variable'), 60)}. This is an instrument observation, not a process "
                         "connection.")
        if self.observers.get(eid):
            lines.append("Observed by " + ", ".join(
                f"{self.tag(o)} ({_text(self.entities[o]['observes'].get('variable'), 60)})"
                for o in self.observers[eid]) + "; observations are not process flow.")
        groups = {}
        for name, quantity in sorted(_mapping(entity.get("parameters")).items()):
            if isinstance(quantity, dict):
                where = quantity.get("assumption_id") or (f"source {_text(quantity.get('source'), 60)}"
                                                          if quantity.get("source") else "no recorded provenance")
                groups.setdefault(where, []).append(f"{_text(name, 48)} {num(quantity.get('value'))} "
                                                    f"{_text(quantity.get('unit'), 12)}")
        if groups:
            lines.append("Recorded dimensions: " + "; ".join(f"{', '.join(values)} ({where})"
                                                             for where, values in groups.items()) + ".")
        else:
            lines.append("No dimensions are recorded for it.")
        placement, _ = self.placement_fact(eid)
        lines.append(placement[0].upper() + placement[1:] + ".")
        aids = self.entity_facts(eid)[2]
        if aids:
            lines.append("Assumptions: " + " ".join(
                f"{aid}: {_text((self.assumptions.get(aid) or {}).get('description') or 'not recorded', 200)}"
                for aid in aids))
        items = self.open_items_for(eid)
        if items:
            lines.append("Open items: " + "; ".join(_text(d.get("message"), 240) for d in items[:4]) + ".")
        return lines

    def _connection_identity(self, cid):
        connection = self.connections[cid]
        state, note = self.direction(cid)
        lines = [f"{self.tag(cid)} ({cid}) is a process connection (line) from "
                 f"{self.port_end(connection.get('from_port_id'))} to {self.port_end(connection.get('to_port_id'))}, "
                 f"DN{num(connection.get('nominal_diameter_mm'))}, outside diameter "
                 f"{num(connection.get('outside_diameter_m'))} m, in the current model.",
                 ("Direction: " if state == "directed" else "Direction not established: ") + note + "."]
        lines += [f"Flow path: {note}." for note, _ in self._end_circuits(cid)]
        facts = self.connection_facts(cid)[0]
        lines += [f[0].upper() + f[1:] + "." for f in facts if f.startswith(("physical route", "P&ID edge"))]
        aids = self.connection_facts(cid)[2]
        if aids:
            lines.append("Assumptions: " + " ".join(
                f"{aid}: {_text((self.assumptions.get(aid) or {}).get('description') or 'not recorded', 200)}"
                for aid in aids))
        return lines

    def _port_identity(self, port_id):
        port = self.ports[port_id]
        owner = port.get("entity_id")
        position = self.port_position(port_id)
        circuit = self.port_circuit(port_id)
        return [f"{self.tag(port_id)} ({port_id}) is the {_text(port.get('name'), 40)} port of "
                f"{self.tag(owner) if owner in self.entities else 'no recorded entity'}: {self._port_summary(port_id)}"
                + (f" (assumption {port['assumption_id']})" if port.get("assumption_id") else "") + ".",
                f"Connection: {self._port_link(port_id)}."] + ([circuit] if circuit else []) + [
                (f"World position (current model): X / Y / Z = {vec(position)} m, Z up." if position else
                 "World position: unknown (its entity is unplaced or its geometry is unresolved).")]

    def neighbours(self, record_id):
        """Immediate directed connectivity of an entity, port or connection.

        ``{"subject", "upstream": [cid], "downstream": [cid], "undirected": [cid]}``; for a connection the
        lists hold the connection itself and its ends are the neighbours.
        """
        kind = self.kind_of(record_id)
        if kind == "entity":
            return {"subject": record_id, "upstream": list(self.incoming[record_id]),
                    "downstream": list(self.outgoing[record_id]), "undirected": list(self.undirected[record_id])}
        if kind == "port":
            owner = self.ports[record_id].get("entity_id")
            mine = self.connections_of_port.get(record_id, [])
            return {"subject": record_id,
                    "upstream": [c for c in mine if c in self.incoming.get(owner, ())],
                    "downstream": [c for c in mine if c in self.outgoing.get(owner, ())],
                    "undirected": [c for c in mine if c in self.undirected.get(owner, ())]}
        directed = self.direction(record_id)[0] == "directed"
        return {"subject": record_id, "upstream": [record_id] if directed else [],
                "downstream": [record_id] if directed else [], "undirected": [] if directed else [record_id]}

    def connectivity_lines(self, record_id):
        """Immediate upstream/downstream lines, observations and direction notes."""
        near = self.neighbours(record_id)
        kind = self.kind_of(record_id)
        lines = [f"Directed process connectivity of {self.tag(record_id)} ({record_id}) in the current model:"]
        if kind == "connection":
            connection = self.connections[record_id]
            source, target = self.ends(record_id)
            if near["upstream"]:
                lines.append(f"Upstream end: {self.tag(source)} (via {self.port_end(connection.get('from_port_id'))}).")
                lines.append(f"Downstream end: {self.tag(target)} (via {self.port_end(connection.get('to_port_id'))}).")
                lines += [f"Flow path: {note}" + ("; traversal continues only within it." if on_path else ".")
                          for note, on_path in self._end_circuits(record_id)]
            else:
                lines.append(f"Direction not established: {self.direction(record_id)[1]}. It joins "
                             f"{self.port_end(connection.get('from_port_id'))} and "
                             f"{self.port_end(connection.get('to_port_id'))}.")
        else:
            owner = record_id if kind == "entity" else self.ports[record_id].get("entity_id")
            split = kind == "entity" and self.split(owner)
            for label, key, verb in (("Upstream (incoming)", "upstream", "from"),
                                     ("Downstream (outgoing)", "downstream", "to")):
                items = near[key]
                if not items:
                    none = "no incoming" if key == "upstream" else "no outgoing"
                    lines.append(f"{label}: none; {none} process connection is recorded.")
                    continue
                parts = []
                for cid in items:
                    connection = self.connections[cid]
                    if key == "upstream":
                        parts.append(f"{self.tag(cid)} from {self.port_end(connection.get('from_port_id'))} into "
                                     f"{self.port_end(connection.get('to_port_id'))}")
                    else:
                        parts.append(f"{self.tag(cid)} from {self.port_end(connection.get('from_port_id'))} to "
                                     f"{self.port_end(connection.get('to_port_id'))}")
                    if split:  # name the separate circuit each line belongs to; they are not branches of one flow
                        node = self.node_of_port[connection["to_port_id" if key == "upstream" else "from_port_id"]]
                        parts[-1] += f" ({self.label_phrase(self.node_label[node])})" \
                            if owner in self.flow_paths else " (flow paths not validated)"
                if len(parts) > 1:
                    noun = "lines" if split else "branches"
                    lines.append(f"{label}, {len(parts)} {noun}: " + "; ".join(parts) + ".")
                else:
                    lines.append(f"{label}: {parts[0]}.")
            for cid in near["undirected"]:
                lines.append(f"Direction not established for {self.tag(cid)}: {self.direction(cid)[1]}.")
            circuit = self.circuit_summary(owner) if split else (
                self.port_circuit(record_id) if kind == "port" else "")
            if circuit:
                lines.append(circuit)
        if kind == "entity":
            if not self.ports_of.get(record_id):
                lines.append(f"{self.tag(record_id)} has no process ports, so it has no upstream or downstream "
                             "process items.")
            observes = self.entities[record_id].get("observes")
            if isinstance(observes, dict):
                subject = observes.get("entity_id")
                lines.append(f"Observation (not process flow): {self.tag(record_id)} observes "
                             f"{self.tag(subject) if subject in self.entities else _text(subject, 80)} "
                             f"{_text(observes.get('variable'), 60)}.")
            for observer in self.observers.get(record_id, ()):
                lines.append(f"Observation (not process flow): {self.tag(observer)} observes {self.tag(record_id)} "
                             f"{_text(self.entities[observer]['observes'].get('variable'), 60)}.")
        lines.append(SIGNALS_NOTE)
        lines.append(DIRECTION_NOTE)
        return lines

    def connectivity_ids(self, record_id):
        """Records named by the immediate connectivity answer, in order."""
        near = self.neighbours(record_id)
        ids = [record_id]
        for cid in near["upstream"] + near["downstream"] + near["undirected"]:
            ids.append(cid)
            ids.extend(e for e in self.ends(cid) if e in self.entities)
        if record_id in self.entities:
            observes = self.entities[record_id].get("observes")
            if isinstance(observes, dict) and observes.get("entity_id") in self.entities:
                ids.append(observes["entity_id"])
            ids.extend(self.observers.get(record_id, ()))
        return list(dict.fromkeys(ids))

    # --- Expanded traversal ----------------------------------------------------------

    def trace(self, record_id, direction, max_depth=MAX_DEPTH, max_edges=MAX_EDGES):
        """Bounded expanded traversal from an entity, port or connection in one direction.

        Returns ``{"start", "direction", "via", "branches", "edges", "truncated", "ids"}``. ``branches`` is a
        tree of ``{"connection", "entity", "end", "next"}`` nodes; ``end`` is ``None`` (continues), ``"end"`` (no
        further process connection in this direction), ``"loop"`` (returns to an item on the same path; stops),
        ``"joins"`` (reaches an item already listed by another branch; listed once), ``"depth"`` / ``"limit"``
        (a bound was reached) or ``"undirected"`` (a connection without established direction; not followed).
        ``edges`` counts every listed connection, directed or undirected; at ``max_edges`` one ``"limit"`` node
        ends the listing and ``truncated`` is true.

        Traversal moves between flow nodes (see the module notes), so loops and re-joins are those of nodes. A
        connection start continues from the node of the exact port it reaches. A port start of an entity-level node
        follows only that port's own directed connections first (and lists no undirected one there), as before; a
        port of a declared flow path stands for that path, so it continues exactly like flow arriving at it, and
        never into another path. A node of an entity whose flow is split also carries its ``flow_path`` (port-name
        pair) or ``flow_port`` (a port outside every declared path) in the tree and, for a port or connection
        start, in the result. An entity start whose flow is split has ``circuits`` (their number) and one top-level node
        ``{"connection": None, "entity": start, "flow_path"|"flow_port", "end", "next"}`` per declared path and per
        port outside them that has a connection in this direction or an undirected one, each traced on its own
        (``end`` ``"joins"`` when an earlier one reached it through a recorded connection).
        """
        downstream = direction == "downstream"
        kind = self.kind_of(record_id)
        via = None
        own = None  # an entity-level port start: only that port's own connections leave the start node
        if kind == "connection":
            source, target = self.ends(record_id)
            via = record_id
            start = target if downstream else source
            if self.direction(record_id)[0] != "directed" or start not in self.entities:
                return {"start": None, "direction": direction, "via": via, "branches": [], "edges": 0,
                        "truncated": False, "ids": [record_id]}
            starts = [self.node_of_port[self.connections[record_id]["to_port_id" if downstream else "from_port_id"]]]
        elif kind == "port":
            start = self.ports[record_id].get("entity_id")
            node = self.node_of_port.get(record_id)
            starts = [node] if node is not None else []
            if node is None or self.node_label[node] is None:  # an entity-level port: its own connections only
                own = set(self.connections_of_port.get(record_id, ()))
        else:
            start = record_id
            starts = list(self.nodes_of.get(record_id, ()))
            if self.split(record_id):  # every declared path; a port outside them only with a line to list here
                starts = [n for n in starts if "flow_path" in (self.node_label[n] or {})
                          or (self.node_out[n] if downstream else self.node_in[n]) or self.node_undirected[n]]
        circuits = kind == "entity" and self.split(record_id) and bool(starts)
        state = {"edges": 0, "truncated": False}
        expanded = set() if circuits else set(starts)
        ids = [record_id, start] + ([via] if via else [])

        def onward(node):
            return self.node_out[node] if downstream else self.node_in[node]

        def full(branches):
            """Count one more listed connection (directed or undirected), or mark the listing limit once."""
            if state["edges"] < max_edges:
                state["edges"] += 1
                return False
            state["truncated"] = True
            if not branches or branches[-1]["end"] != "limit":
                branches.append({"connection": None, "entity": None, "end": "limit", "next": []})
            return True

        def walk(current, path, depth, first=False):
            branches = []
            candidates = list(onward(current))
            restricted = first and own is not None
            if restricted:
                candidates = [c for c in candidates if c in own]
            for cid in candidates:
                if full(branches):
                    break
                connection = self.connections[cid]
                nxt = self.node_of_port[connection["to_port_id" if downstream else "from_port_id"]]
                ids.extend((cid, nxt[0]))
                node = {"connection": cid, "entity": nxt[0], "end": None, "next": []}
                node.update(self._label_of(nxt))
                if nxt in path:
                    node["end"] = "loop"
                elif nxt in expanded:
                    node["end"] = "joins"
                elif depth + 1 >= max_depth:
                    node["end"] = "depth"
                    state["truncated"] = True
                else:
                    expanded.add(nxt)
                    node["next"] = walk(nxt, path | {nxt}, depth + 1)
                    if not node["next"]:
                        node["end"] = "end"
                branches.append(node)
            if not restricted:
                for cid in self.node_undirected.get(current, ()):
                    if full(branches):
                        break
                    other = [e for e in self.ends(cid) if e != current[0]]
                    ids.append(cid)
                    node = {"connection": cid, "entity": other[0] if other else None, "end": "undirected", "next": []}
                    far = self._far_node(cid, current)
                    if far is not None and self.node_label.get(far) and node["entity"] in (None, far[0]):
                        node["entity"] = far[0]
                        node.update(self._label_of(far))
                    branches.append(node)
            return branches

        result = {"start": start, "direction": direction, "via": via}
        if circuits:
            branches = []
            for node in starts:
                item = {"connection": None, "entity": start, "end": None, "next": []}
                item.update(self._label_of(node))
                if node in expanded:  # an earlier circuit reached it through a recorded connection
                    item["end"] = "joins"
                else:
                    expanded.add(node)
                    item["next"] = walk(node, frozenset((node,)), 0, first=True)
                    if not item["next"]:
                        item["end"] = "end"
                branches.append(item)
            result["circuits"] = len(starts)
        elif starts:
            branches = walk(starts[0], frozenset(starts), 0, first=True)
            result.update(self._label_of(starts[0]))
        else:  # a port of no recorded entity, or a split entity without any port
            branches = []
        result.update(branches=branches, edges=state["edges"], truncated=state["truncated"],
                      ids=list(dict.fromkeys(i for i in ids if self.kind_of(i))))
        return result

    def _far_node(self, cid, node):
        """The node at the other end of ``cid`` from ``node`` (None when missing or the same node)."""
        connection = self.connections[cid]
        for key in ("from_port_id", "to_port_id"):
            port_id = connection.get(key)
            other = self.node_of_port.get(port_id) if isinstance(port_id, str) else None
            if other is not None and other != node:
                return other
        return None

    def trace_lines(self, record_id, direction, max_depth=MAX_DEPTH, max_edges=MAX_EDGES):
        result = self.trace(record_id, direction, max_depth, max_edges)
        arrow = "→" if direction == "downstream" else "←"
        label = "Downstream" if direction == "downstream" else "Upstream"
        if result["start"] is None:
            return [f"{label} path from {self.tag(record_id)}: not traced, because its direction is not established "
                    f"({self.direction(record_id)[1]})."], result
        endings = {"end": "[end: no further " + ("outgoing" if direction == "downstream" else "incoming")
                          + " process connection]",
                   "loop": "[loop: returns to {tag} on this path; traversal stops]",
                   "joins": "[joins {tag}, listed above; not repeated]",
                   "depth": f"[depth limit of {max_depth} connections reached; the path continues]",
                   "limit": f"[listing limit of {max_edges} connections reached; further items are not listed]",
                   "undirected": "[direction not established; not followed]"}
        here = self.node_text(result["start"], {k: result[k] for k in ("flow_path", "flow_port") if k in result})
        start_text = here
        if result["via"]:
            start_text = f"{self.tag(result['via'])} {arrow} {start_text}"
        header = f"{label} path from {self.tag(record_id)} (bounded expansion, every branch listed):"
        if not result["branches"]:
            return [header, f"{start_text} {endings['end']}"], result
        lines = [header]

        def ending(node):
            text = endings[node["end"]]
            return text.replace("{tag}", self._item_text(node))

        def render(prefix, here, branches, indent):
            if len(branches) == 1:
                node = branches[0]
                if node["end"] == "limit":
                    lines.append(" " * indent + f"{prefix} {ending(node)}")
                    return
                there = self._item_text(node)
                text = f"{prefix} {arrow} {self.tag(node['connection'])} {arrow} {there}"
                if node["end"]:
                    lines.append(" " * indent + f"{text} {ending(node)}")
                else:
                    render(text, there, node["next"], indent)
                return
            lines.append(" " * indent + f"{prefix}, which has {len(branches)} branches:")
            for index, node in enumerate(branches, 1):
                if node["end"] == "limit":
                    lines.append(" " * (indent + 2) + f"branch {index}: {ending(node)}")
                    continue
                there = self._item_text(node)
                text = f"branch {index}: {here} {arrow} {self.tag(node['connection'])} {arrow} {there}"
                if node["end"]:
                    lines.append(" " * (indent + 2) + f"{text} {ending(node)}")
                else:
                    render(text, there, node["next"], indent + 2)

        if result.get("circuits"):
            tag = self.tag(result["start"])
            if result["start"] in self.unverified:
                lines.append(f"{tag}'s declared flow paths could not be validated, so no flow is passed through {tag}: "
                             "each of its ports with a line in this direction is traced on its own:")
            else:
                count = len(self.flow_paths[result["start"]])
                outside = sum(1 for item in result["branches"] if "flow_port" in item)
                lines.append(f"{tag} declares {count} separate flow path{'' if count == 1 else 's'}"
                             + (f" and has {outside} port{'' if outside == 1 else 's'} outside them with a line in "
                                "this direction" if outside else "")
                             + f"; each is traced on its own, and none continues into another through {tag} itself "
                               "(only a recorded connection can join them):")
            for item in result["branches"]:
                text = self._item_text(item)
                if item["end"] == "joins":
                    lines.append(f"{text} [reached above through a recorded connection; listed there, not repeated]")
                elif item["end"]:
                    lines.append(f"{text} {ending(item)}")
                else:
                    render(text, text, item["next"], 0)
        else:
            render(start_text, here, result["branches"], 0)
        if result["truncated"]:
            lines.append("The traversal stopped at a bound; the listing above is incomplete beyond the marked point.")
        return lines, result

    # --- Relations -------------------------------------------------------------------

    def _anchor(self, record_id, role):
        """``(states, prefix, suffix)``: where a path starts (role source) or ends (role target).

        A state is ``(node, position)``, the position along the node's declared pair (``_order``). An entity anchors
        at every one of its nodes, from their start as a source and at any position as a target; a port at its own
        node and position; a directed connection at the port it reaches (source) or leaves (target), plus the
        connection itself. A target is reached at its node from any position up to its own, so a path never runs
        from a declared outlet back to its inlet through the item itself."""
        kind = self.kind_of(record_id)
        if kind == "entity":
            position = 0 if role == "source" else math.inf
            return [(node, position) for node in self.nodes_of.get(record_id, ())], [], []
        if kind == "port":
            node = self.node_of_port.get(record_id)
            return ([(node, self._order(record_id))] if node is not None else []), [], []
        if self.direction(record_id)[0] != "directed":
            return [], [], []
        connection = self.connections[record_id]
        source, target = connection["from_port_id"], connection["to_port_id"]
        reached = self.node_of_port[target]
        if role == "source":
            return [(reached, self._order(target))], [(record_id, reached)], []
        return [(self.node_of_port[source], self._order(source))], [], [(record_id, reached)]

    def _node_path(self, source_id, target_id, max_edges):
        """``(path, searched, truncated, origin)``, breadth first over ``(node, position)`` states; ``path`` is
        ``[(connection, node), ...]`` and ``origin`` the node it starts from (``None`` without a path). A connection
        leaves a state only from a port at or after its position, so a declared path is passed from its inlet to its
        outlet only, and returning to an earlier position of it takes recorded connections. Every other node has one
        position, which is exactly the node-level search."""
        starts, prefix, _ = self._anchor(source_id, "source")
        goals, _, suffix = self._anchor(target_id, "target")
        if not starts or not goals:
            return None, 0, False, None
        goals = dict(goals)

        def reached(state):
            return state[0] in goals and state[1] <= goals[state[0]]

        common = [state for state in starts if reached(state)]
        if common:
            path = prefix + suffix
            if self.kind_of(source_id) == "connection" and self.kind_of(target_id) == "connection" and \
                    source_id == target_id:
                path = prefix
            return path, 0, False, common[0][0]
        previous = {state: None for state in starts}
        queue, searched, truncated = list(starts), 0, False
        while queue:
            current = queue.pop(0)
            for cid in self.node_out[current[0]]:
                connection = self.connections[cid]
                if self._order(connection["from_port_id"]) < current[1]:
                    continue  # it leaves before the position already reached on this declared path
                if searched >= max_edges:
                    truncated = True
                    queue = []
                    break
                searched += 1
                nxt = (self.node_of_port[connection["to_port_id"]], self._order(connection["to_port_id"]))
                if nxt in previous:
                    continue
                previous[nxt] = (cid, current)
                if reached(nxt):
                    steps, state = [], nxt
                    while previous[state] is not None:
                        cid_, before = previous[state]
                        steps.append((cid_, state[0]))
                        state = before
                    return prefix + steps[::-1] + suffix, searched, False, state[0]
                queue.append(nxt)
        return None, searched, truncated, None

    def path_between(self, source_id, target_id, max_edges=MAX_EDGES * 4):
        """Shortest directed process path from ``source_id`` to ``target_id`` (breadth first over flow nodes,
        bounded), so it never passes from one declared flow path of an item into another through the item, nor
        from a declared outlet back to its inlet.

        Returns ``(path, searched, truncated)``: ``path`` is ``[(connection, entity), ...]`` (``[]`` when both are
        the same item, or a port and a later port of the same declared path) or ``None`` when no path was found
        within ``max_edges`` connections.
        """
        path, searched, truncated, _ = self._node_path(source_id, target_id, max_edges)
        return (None if path is None else [(cid, node[0]) for cid, node in path]), searched, truncated

    def path_text(self, source_id, path):
        return " → ".join([self.tag(source_id)] + [f"{self.tag(c)} → {self.tag(e)}" for c, e in path])

    def _node_path_text(self, source_id, origin, path, target_id=None):
        """``path_text`` naming the declared flow path of every node of a split entity. A path from one port of a
        declared path to a later port of it, through the item alone, names both ports."""
        head = self.tag(source_id)
        if self.kind_of(source_id) == "entity" and origin is not None and self.node_label.get(origin):
            head = self.node_text(source_id, self.node_label[origin])
        label = self.node_label.get(origin) or {}
        if not path and "flow_path" in label and self.kind_of(source_id) == self.kind_of(target_id) == "port" and \
                source_id != target_id and self.node_of_port.get(target_id) == origin:
            return (f"{head} → {self.tag(target_id)} inside {self.tag(origin[0])}, along its declared flow path "
                    f"{self.path_phrase(label['flow_path'])} with no external line")
        return " → ".join([head] + [f"{self.tag(c)} → {self.node_text(n[0], self.node_label.get(n))}"
                                    for c, n in path])

    def relation_lines(self, first, second, relation):
        """Whether ``first`` is upstream/downstream of ``second``, or what lies between them (directed graph)."""
        lines = []
        forward = self._node_path(first, second, MAX_EDGES * 4)
        backward = self._node_path(second, first, MAX_EDGES * 4)
        a, b = self.tag(first), self.tag(second)
        if relation == "upstream":
            found, other = forward, backward
            yes = f"Yes. {a} is upstream of {b}: following directed process connections downstream from {a} " \
                  f"reaches {b}"
            reverse = f"No. {a} is downstream of {b}, not upstream: following directed process connections " \
                      f"downstream from {b} reaches {a}"
            head, tail = first, second
        elif relation == "downstream":
            found, other = backward, forward
            yes = f"Yes. {a} is downstream of {b}: following directed process connections downstream from {b} " \
                  f"reaches {a}"
            reverse = f"No. {a} is upstream of {b}, not downstream: following directed process connections " \
                      f"downstream from {a} reaches {b}"
            head, tail = second, first
        else:
            found, other = forward, backward
            yes = f"A directed process path runs from {a} to {b}"
            reverse = f"A directed process path runs from {b} to {a}"
            head, tail = first, second
        if found[0] is not None:
            lines.append(f"{yes}: {self._node_path_text(head, found[3], found[0], tail)} ({len(found[0])} "
                         "connection(s)).")
        elif other[0] is not None:
            lines.append(f"{reverse}: {self._node_path_text(tail, other[3], other[0], head)} ({len(other[0])} "
                         "connection(s)).")
        else:
            bound = " The search stopped at its bound, so a longer path may exist." if (found[2] or other[2]) else ""
            lines.append(f"No directed process path connects {a} and {b} in either direction in the current model "
                         f"(searched {found[1] + other[1]} connection(s)).{bound}")
        if any(self.undirected.get(e) for e in self.entities):
            lines.append("Connections without an established direction were not followed.")
        note = self.flow_note()
        if note:
            lines.append(note)
        lines.append(DIRECTION_NOTE)
        ids = [first, second]
        for path in (found[0], other[0]):
            for cid, node in path or ():
                ids += [cid, node[0]]
        return lines, list(dict.fromkeys(i for i in ids if self.kind_of(i)))

    # --- Overview --------------------------------------------------------------------

    def overview_lines(self):
        lines = [f"The current model has {len(self.entities)} item(s) and {len(self.connections)} process "
                 "connection(s)."]
        for eid in sorted(self.entities, key=lambda e: (self.tag(e).casefold(), e))[:MAX_LISTED]:
            lines.append(f"- {self.tag(eid)} ({eid}): {self._kind_phrase(eid)}")
        if len(self.entities) > MAX_LISTED:
            lines.append(f"- {len(self.entities) - MAX_LISTED} further item(s) not listed")
        for cid in sorted(self.connections, key=lambda c: (self.tag(c).casefold(), c))[:MAX_LISTED]:
            connection = self.connections[cid]
            lines.append(f"- line {self.tag(cid)} ({cid}): {self.port_end(connection.get('from_port_id'))} to "
                         f"{self.port_end(connection.get('to_port_id'))}")
        if len(self.connections) > MAX_LISTED:
            lines.append(f"- {len(self.connections) - MAX_LISTED} further connection(s) not listed")
        return lines
