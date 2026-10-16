"""Process-flow order derived from port connectivity (never from tags or layout).

A run starts at a process entity without incoming connections, ties broken by
stable ID, and follows outgoing connections in template port order. Further
outgoing connections of one entity start new runs (branches); a connection
into an already visited entity ends its run and is marked ``closes_loop``.
An item with ports but no line at all (an unconnected draft item) is its own
run, walked in stable ID order after the runs that begin at a start node and
their branches, so the joined flow is read first; runs reached only through
a closed loop without a start node follow, as before. A project without such
items walks exactly as before.
Entities without process ports (indicators) are ordered after the flow by
the position of the entity they observe.

Flow moves between *nodes*. An entity whose definition declares no
``flow_paths`` (every built-in kind and every legacy definition) is one node
holding all its ports, so every incident line belongs to it, exactly as
before. A composed entity that declares ``flow_paths`` has one node per
declared ``[inlet, outlet]`` pair and one node per port outside every pair:
flow arriving at a path's inlet continues only from that path's outlet, so
separate circuits through one item (shell and tube sides of an exchanger)
are never joined by the item's identity. Its entity steps carry
``flow_path`` (the pair's port names) or ``flow_port`` (an unpaired port's
name). A node without any connection is not walked while another node of
its entity is connected; its ports are reported as unconnected instead.
"""

from __future__ import annotations

from .definitions import entity_template


class FlowGraph:
    def __init__(self, project: dict):
        model = project["model"]
        self.entities = model["entities"]
        self.ports = model["ports"]
        self.connections = model["connections"]
        self.ports_of = {eid: [] for eid in self.entities}
        for port_id in sorted(self.ports):
            self.ports_of[self.ports[port_id]["entity_id"]].append(port_id)
        self.templates = {eid: entity_template(entity) for eid, entity in self.entities.items()}
        for eid, port_ids in self.ports_of.items():
            order = list(self.templates[eid]["ports"])
            port_ids.sort(key=lambda p: (order.index(self.ports[p]["name"]), p))
        self.port_rank = {p: i for ids in self.ports_of.values() for i, p in enumerate(ids)}
        self.outgoing = {eid: [] for eid in self.entities}
        self.incoming = {eid: [] for eid in self.entities}
        self.ends = {}
        for cid in sorted(self.connections):
            connection = self.connections[cid]
            source = self.ports[connection["from_port_id"]]["entity_id"]
            target = self.ports[connection["to_port_id"]]["entity_id"]
            self.ends[cid] = (source, target)
            self.outgoing[source].append(cid)
            self.incoming[target].append(cid)
        for eid in self.entities:
            self.outgoing[eid].sort(key=lambda c: (self.port_rank[self.connections[c]["from_port_id"]], c))
            self.incoming[eid].sort(key=lambda c: (self.port_rank[self.connections[c]["to_port_id"]], c))
        self._build_nodes()
        self.steps = self._walk()
        self.entity_rank = {}
        self.connection_rank = {}
        for index, step in enumerate(self.steps):
            target = self.entity_rank if step["item_kind"] == "entity" else self.connection_rank
            target.setdefault(step["id"], index)
        self.entity_order = self._entity_order()
        self.connection_order = sorted(self.connections, key=lambda c: (self.connection_rank.get(c, len(self.steps)), c))

    # --- Flow nodes ------------------------------------------------------------------------------

    def flow_paths(self, eid):
        """The entity's declared ``[inlet, outlet]`` port-name pairs, or None (one node for all its ports)."""
        paths = self.templates[eid].get("flow_paths")
        return [list(pair) for pair in paths] if paths else None

    def _build_nodes(self):
        """Nodes are ``(entity id, rank)``; ``node_ports``, ``node_label``, ``node_of_port`` and node adjacency."""
        self.nodes_of = {eid: [] for eid in self.entities}
        self.node_ports, self.node_label, self.node_of_port = {}, {}, {}
        for eid in sorted(self.entities):
            if not self.ports_of[eid]:
                continue
            paths = self.flow_paths(eid)
            by_name = {self.ports[p]["name"]: p for p in self.ports_of[eid]}
            if paths is None:
                groups = [(None, list(self.ports_of[eid]))]
            else:
                paired = {name for pair in paths for name in pair}
                groups = [({"flow_path": list(pair)}, [by_name[n] for n in pair if n in by_name]) for pair in paths]
                groups += [({"flow_port": self.ports[p]["name"]}, [p]) for p in self.ports_of[eid]
                           if self.ports[p]["name"] not in paired]
            for rank, (label, port_ids) in enumerate(groups):
                node = (eid, rank)
                self.nodes_of[eid].append(node)
                self.node_ports[node] = port_ids
                self.node_label[node] = label
                for port_id in port_ids:
                    self.node_of_port[port_id] = node
        self.node_out = {node: [] for node in self.node_ports}
        self.node_in = {node: [] for node in self.node_ports}
        self.node_ends = {}
        for cid in sorted(self.connections):
            connection = self.connections[cid]
            source = self.node_of_port[connection["from_port_id"]]
            target = self.node_of_port[connection["to_port_id"]]
            self.node_ends[cid] = (source, target)
            self.node_out[source].append(cid)
            self.node_in[target].append(cid)
        for node in self.node_ports:
            self.node_out[node].sort(key=lambda c: (self.port_rank[self.connections[c]["from_port_id"]], c))
            self.node_in[node].sort(key=lambda c: (self.port_rank[self.connections[c]["to_port_id"]], c))

    def connected(self, node):
        return bool(self.node_in[node] or self.node_out[node])

    def walked_nodes(self):
        """Process nodes in walk order: an unconnected node only when its whole entity is unconnected (once)."""
        out = []
        for eid in sorted(self.entities):
            nodes = self.nodes_of[eid]
            if any(self.connected(n) for n in nodes):
                out += [n for n in nodes if self.connected(n)]
            elif nodes:
                out.append(nodes[0])
        return out

    def reaches(self, start, goal):
        """True when a path of zero or more lines leads from node ``start`` to node ``goal``."""
        seen, frontier = {start}, [start]
        while frontier:
            current = frontier.pop()
            if current == goal:
                return True
            for cid in self.node_out[current]:
                other = self.node_ends[cid][1]
                if other not in seen:
                    seen.add(other)
                    frontier.append(other)
        return False

    def nearest(self, eid, direction, accept):
        """Nearest entities (sorted IDs) reachable from ``eid``'s nodes along ``direction`` (``down`` follows
        outgoing lines, ``up`` incoming) whose entity satisfies ``accept``; flow never crosses between the
        separate declared paths of one item, and a found entity is not passed through."""
        start = self.nodes_of.get(eid, [])
        seen, frontier, found = set(start), list(start), set()
        while frontier and not found:
            following = []
            for current in frontier:
                lines = self.node_out[current] if direction == "down" else self.node_in[current]
                for cid in lines:
                    other = self.node_ends[cid][1 if direction == "down" else 0]
                    if other in seen:
                        continue
                    seen.add(other)
                    if accept(other[0]):
                        found.add(other[0])
                    else:
                        following.append(other)
            frontier = following
        return sorted(found)

    # --- Walk ------------------------------------------------------------------------------------

    def _entity_step(self, node, run):
        step = {"item_kind": "entity", "id": node[0], "run": run, "closes_loop": False}
        if self.node_label[node]:
            step.update(self.node_label[node])
        return step

    def _walk(self):
        process = self.walked_nodes()
        # A walked node without any line is a wholly unconnected item: read after the start runs and their
        # branches, still before runs reached only through a closed loop (where it was before).
        starts = [node for node in process if not self.node_in[node] and self.connected(node)]
        loose = [node for node in process if not self.connected(node)]
        steps, visited, used = [], set(), set()
        run = 0
        queue = []  # (node to continue from, connection that begins the branch)

        def follow(current, first_connection=None):
            nonlocal run
            run += 1
            if first_connection is None:
                steps.append(self._entity_step(current, run))
                visited.add(current)
            else:
                branch_from = current[0]
                current = self._step_connection(steps, first_connection, run, visited, used, branch_from)
                if current is None:
                    return
            while True:
                pending = [c for c in self.node_out[current] if c not in used]
                if not pending:
                    return
                for extra in pending[1:]:
                    queue.append((current, extra))
                current = self._step_connection(steps, pending[0], run, visited, used, None)
                if current is None:
                    return

        while True:
            if starts:
                start = starts.pop(0)
                if start not in visited:
                    follow(start)
                continue
            if queue:
                origin, connection = queue.pop(0)
                if connection not in used:
                    follow(origin, connection)
                continue
            if loose:
                follow(loose.pop(0))
                continue
            remaining = [node for node in process if node not in visited]
            if not remaining:
                break
            follow(remaining[0])
        return steps

    def _step_connection(self, steps, cid, run, visited, used, branch_from):
        used.add(cid)
        target = self.node_ends[cid][1]
        closes = target in visited
        record = {"item_kind": "connection", "id": cid, "run": run, "closes_loop": closes}
        if branch_from is not None:
            record["branch_from"] = branch_from
        steps.append(record)
        if closes:
            return None
        steps.append(self._entity_step(target, run))
        visited.add(target)
        return target

    def _entity_order(self):
        flow = sorted(self.entity_rank, key=lambda e: self.entity_rank[e])
        rest = [eid for eid in sorted(self.entities) if eid not in self.entity_rank]
        end = len(self.steps)

        def rest_key(eid):
            observes = self.entities[eid].get("observes")
            subject = observes.get("entity_id") if isinstance(observes, dict) else None
            return (0 if subject in self.entity_rank else 1, self.entity_rank.get(subject, end), eid)

        return flow + sorted(rest, key=rest_key)

    def step_node(self, step):
        """The flow node of an entity step (its declared path or port, else the entity's only node)."""
        nodes = self.nodes_of[step["id"]]
        for node in nodes:
            label = self.node_label[node]
            if label is None or label == {k: step[k] for k in ("flow_path", "flow_port") if k in step}:
                return node
        return nodes[0]

    def runs_in_order(self):
        """Steps grouped by run, in order."""
        runs = {}
        for step in self.steps:
            runs.setdefault(step["run"], []).append(step)
        return [runs[key] for key in sorted(runs)]

    def flow_groups(self):
        """Connected flows for summaries, and the items outside every flow.

        One record per node-level connected component holding a line, in walk order: ``starts`` and ``ends``
        are the entities of its nodes without incoming or outgoing lines, ``entities`` every entity stepped in
        it and ``ports`` the ports of its nodes (entity IDs once each, in walk order). Nodes are joined only by
        lines, so separate declared flow paths through one item are separate flows. The second value lists the
        entities with ports but no line at all; they start and end no flow.
        """
        groups, group_of = [], {}
        for step in self.steps:
            if step["item_kind"] != "entity":
                continue
            node = self.step_node(step)
            if not self.connected(node):
                continue
            if node not in group_of:
                group = {"starts": [], "ends": [], "entities": [], "ports": []}
                groups.append(group)
                group_of[node], frontier = group, [node]
                while frontier:
                    current = frontier.pop()
                    for cid in self.node_in[current] + self.node_out[current]:
                        for other in self.node_ends[cid]:
                            if other not in group_of:
                                group_of[other] = group
                                frontier.append(other)
            group = group_of[node]
            for key, member in (("entities", True), ("starts", not self.node_in[node]),
                                ("ends", not self.node_out[node])):
                if member and step["id"] not in group[key]:
                    group[key].append(step["id"])
            group["ports"] += self.node_ports[node]
        unconnected = [s["id"] for s in self.steps if s["item_kind"] == "entity"
                       and not (self.incoming[s["id"]] or self.outgoing[s["id"]])]
        return groups, list(dict.fromkeys(unconnected))
