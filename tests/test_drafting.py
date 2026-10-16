"""Drafting contract: outputs, manifests, fixed paper sheet, stored geometry, previews, cancel and failure."""

from __future__ import annotations

import copy
import math
import os
import re
import unittest
from pathlib import Path

from skid.core.validation import canonical_digest
from skid.drafting import OUTPUT_FILES, build_drafting
from skid.drafting import paper
from skid.drafting.graph import FlowGraph
from skid.drafting.sheet import PidSheet
from skid.drafting.svgkit import contains, overlaps, segment_hits_rect, text_width
from skid.templates import diagram_port_position
from tests.test_drafting_support import (
    EVENT_FIELDS,
    MANIFEST_FIELDS,
    SVG,
    DraftingCase,
    fixture_document,
    insert_hv103,
    occurrence_groups,
    parse,
    run,
    visible_text,
)


def polyline_points(element):
    return [tuple(float(v) for v in pair.split(",")) for pair in element.get("points").split()]


class OutputContractTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def test_fixture_build_writes_every_contracted_file_and_manifest(self):
        out, manifests, _ = self.build(self.project)
        self.assertEqual(set(manifests), {"pid", "legend", "description"})
        for name in OUTPUT_FILES:
            self.assertTrue((out / name).is_file(), name)
            self.assertGreater((out / name).stat().st_size, 0, name)
        digest = canonical_digest(self.project)
        model = self.project["model"]
        semantic = set(model["entities"]) | set(model["ports"]) | set(model["connections"])
        for adapter, manifest in manifests.items():
            self.assertTrue(MANIFEST_FIELDS <= set(manifest), adapter)
            self.assertEqual(manifest["adapter"], adapter)
            self.assertEqual(manifest["adapter_version"], "0.2.0")
            self.assertEqual(manifest["project_id"], "project-pump-skid")
            self.assertEqual(manifest["source_revision"], 0)
            self.assertEqual(manifest["input_digest"], digest)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["diagnostics"], [])
            self.assertEqual(set(manifest["representations"]), semantic)
            self.assertEqual(self.read_json(out / f"{adapter}.manifest.json"), manifest)
            for relative in manifest["files"] + manifest["previews"]:
                self.assertTrue((out / relative).is_file(), relative)
        self.assertEqual(manifests["pid"]["files"], ["pid.svg", "drawing.inventory.json"])
        self.assertEqual(manifests["legend"]["files"], ["legend.svg", "item-index.json"])
        self.assertEqual(manifests["description"]["files"], ["description.json", "description.html"])
        self.assertEqual(manifests["pid"]["layout_findings"], [])
        pid = parse(out / "pid.svg")
        self.assertEqual(pid.get("data-input-digest"), digest)
        self.assertEqual(pid.get("data-source-revision"), "0")
        self.assertEqual(pid.get("data-status"), "complete")
        self.assertEqual(pid.get("data-preview"), "false")
        description = self.read_json(out / "description.json")
        for key in ("project_id", "source_revision", "input_digest", "purpose", "flow_sequence", "items",
                    "assumptions", "unknowns", "manual_notes", "conflicts"):
            self.assertIn(key, description)
        self.assertEqual(description["input_digest"], digest)

    def test_pid_representations_map_every_drawn_occurrence(self):
        out, manifests, _ = self.build(self.project)
        groups = occurrence_groups(parse(out / "pid.svg"))
        mapped = {o for occurrences in manifests["pid"]["representations"].values() for o in occurrences}
        self.assertEqual(mapped, set(groups))
        for semantic_id, occurrences in manifests["pid"]["representations"].items():
            for occurrence in occurrences:
                self.assertEqual(groups[occurrence].get("data-semantic-id"), semantic_id)
        self.assertEqual(manifests["pid"]["representations"]["ent-level-indicator"],
                         ["sym-level-indicator", "sym-level-indicator#observes"])

    def test_input_snapshot_is_not_mutated(self):
        before = copy.deepcopy(self.project)
        self.build(self.project, manual_notes=[{"id": "n", "target_id": "ent-pump", "text": "x",
                                                "reviewed_revision": 0}])
        self.assertEqual(self.project, before)

    def test_repeated_builds_are_byte_identical(self):
        first, _, _ = self.build(self.project)
        second, _, _ = self.build(copy.deepcopy(self.project))
        names = sorted(p.relative_to(first).as_posix() for p in first.rglob("*") if p.is_file())
        self.assertEqual(names, sorted(p.relative_to(second).as_posix() for p in second.rglob("*") if p.is_file()))
        for name in names:
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_build_works_without_callbacks(self):
        out = self.output_dir()
        manifests = build_drafting(self.project, str(out))
        self.assertEqual({m["status"] for m in manifests.values()}, {"complete"})

    def test_generator_contains_no_synthetic_delay(self):
        source_dir = Path(__file__).resolve().parent.parent / "skid" / "drafting"
        for path in source_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\bsleep\s*\(", path.name)
            self.assertNotIn("import time", text, path.name)


class SheetTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def test_fixed_paper_frame_and_zone_margins(self):
        out, _, _ = self.build(self.project)
        root = parse(out / "pid.svg")
        self.assertEqual(root.get("viewBox"), "0 0 1200 840")
        border = [e for e in root.iter(f"{SVG}rect") if e.get("data-role") == "inner-border"]
        self.assertEqual(len(border), 1)
        self.assertEqual([border[0].get(k) for k in ("x", "y", "width", "height")], ["30", "30", "1140", "780"])
        columns = [e for e in root.iter(f"{SVG}text") if e.get("data-zone-column")]
        rows = [e for e in root.iter(f"{SVG}text") if e.get("data-zone-row")]
        for margin in ("top", "bottom"):
            labels = sorted((float(e.get("x")), e.text) for e in columns if e.get("data-margin") == margin)
            self.assertEqual([text for _, text in labels], [str(n) for n in range(1, 11)])
        for margin in ("left", "right"):
            labels = sorted((float(e.get("y")), e.text) for e in rows if e.get("data-margin") == margin)
            self.assertEqual("".join(text for _, text in labels), "IHGFEDCBA")  # I at the top, A at the bottom
        letter_y = {e.text: float(e.get("y")) for e in rows if e.get("data-margin") == "left"}
        self.assertLess(letter_y["I"], letter_y["A"])
        self.assertAlmostEqual(letter_y["A"], paper.row_centre("A") + 8.5 * 0.36, places=2)

    def test_stored_geometry_rotation_anchors_and_manual_points_under_fixed_transform(self):
        out, _, _ = self.build(self.project)
        groups = occurrence_groups(parse(out / "pid.svg"))
        for sid, symbol in self.project["pid"]["symbols"].items():
            group = groups[sid]
            body = [e for e in group.iter() if e.get("data-role") == "symbol-body"][0]
            x, y = symbol["x"] + 100, symbol["y"] + 100
            self.assertEqual(body.get("transform"), f"translate({x:g} {y:g}) rotate({symbol['rotation_deg']:g})")
            self.assertEqual(float(group.get("data-paper-x")), x)
            self.assertEqual(float(group.get("data-paper-y")), y)
        for port_id, port in self.project["model"]["ports"].items():
            sid = next(s for s, r in self.project["pid"]["symbols"].items() if r["entity_id"] == port["entity_id"])
            anchor = groups[f"{sid}#{port_id}"]
            ax, ay = diagram_port_position(self.project, port_id, sid)
            self.assertEqual((float(anchor.get("data-paper-x")), float(anchor.get("data-paper-y"))), (ax + 100, ay + 100))
        for edge_id, edge in self.project["pid"]["edges"].items():
            path = [e for e in groups[edge_id].iter(f"{SVG}polyline") if e.get("data-role") == "path"][0]
            self.assertEqual(polyline_points(path), [(px + 100, py + 100) for px, py in edge["points"]])
        manual = [e for e in groups["edge-discharge-1"].iter(f"{SVG}polyline") if e.get("data-role") == "path"][0]
        self.assertEqual(polyline_points(manual), [(460.0, 372.0), (460.0, 320.0), (564.0, 320.0)])

    def test_rotation_is_preserved_and_anchors_follow_it(self):
        document = run(self.document, "move_symbol", {"symbol_id": "sym-discharge-valve", "x": 600.0, "y": 220.0,
                                                      "rotation_deg": 180.0})
        project = document["current"]
        out, _, _ = self.build(project)
        groups = occurrence_groups(parse(out / "pid.svg"))
        body = [e for e in groups["sym-discharge-valve"].iter() if e.get("data-role") == "symbol-body"][0]
        self.assertEqual(body.get("transform"), "translate(700 320) rotate(180)")
        anchor = groups["sym-discharge-valve#port-discharge-valve-inlet"]
        self.assertEqual((float(anchor.get("data-paper-x")), float(anchor.get("data-paper-y"))), (716.0, 320.0))

    def test_editing_one_symbol_never_refits_or_moves_the_rest(self):
        base, _, _ = self.build(self.project)
        moved = run(self.document, "move_symbol", {"symbol_id": "sym-level-indicator", "x": 40.0, "y": 140.0,
                                                   "rotation_deg": 0.0})["current"]
        out, _, _ = self.build(moved)
        before, after = occurrence_groups(parse(base / "pid.svg")), occurrence_groups(parse(out / "pid.svg"))
        self.assertEqual(parse(out / "pid.svg").get("viewBox"), "0 0 1200 840")
        for sid in self.project["pid"]["symbols"]:
            if sid == "sym-level-indicator":
                continue
            self.assertEqual(before[sid].get("data-paper-x"), after[sid].get("data-paper-x"))
            self.assertEqual(before[sid].get("data-paper-y"), after[sid].get("data-paper-y"))
        self.assertEqual(after["sym-level-indicator"].get("data-paper-x"), "140")

    def test_drawing_body_has_no_debug_hashes_ids_or_raw_parameter_names(self):
        out, manifests, _ = self.build(self.project)
        text = visible_text(parse(out / "pid.svg")) + " " + visible_text(parse(out / "legend.svg"))
        self.assertNotIn(manifests["pid"]["input_digest"], text)
        self.assertNotRegex(text, r"[0-9a-f]{32}")
        for raw in ("asm-", "ent-", "conn-", "sym-", "edge-", "port-", "body_length", "outlet_height",
                    "motor_diameter", "template_version"):
            self.assertNotIn(raw, text)

    def test_sheet_states_limits_and_invents_no_operating_data(self):
        out, _, _ = self.build(self.project)
        text = visible_text(parse(out / "pid.svg"))
        self.assertIn("NO DRAFTING-STANDARD CONFORMANCE IS CLAIMED", text)
        self.assertIn("NOT AN AS-BUILT RECORD", text)
        self.assertIn("NOT FOR DESIGN, PROCUREMENT OR CONSTRUCTION", text)
        self.assertIn("NORMAL POSITIONS AND FAIL ACTIONS ARE NOT MODELED", text)
        root = parse(out / "pid.svg")
        drawing = visible_text([e for e in root.iter() if e.get("data-layer") == "drawing"][0])
        self.assertNotRegex(drawing, r"\b(NO|NC|FO|FC|FL|LO|LC|LCO|LCC)\b")  # no position or fail annotations
        groups = occurrence_groups(root)
        for sid in ("sym-suction-valve", "sym-check-valve", "sym-discharge-valve"):
            tag = self.project["model"]["entities"][self.project["pid"]["symbols"][sid]["entity_id"]]["tag"]
            self.assertEqual(visible_text(groups[sid]), tag)
        self.assertNotRegex(text, r"\d\s*(bar|kPa|MPa|psi|°C|degC|m3/h|m³/h|rpm|kW)\b")
        self.assertNotRegex(text.upper(), r"NORMALLY (OPEN|CLOSED)|FAIL (OPEN|CLOSED?)|LOCKED")

    def test_title_and_revision_block_reflect_the_snapshot(self):
        out, _, _ = self.build(self.project)
        root = parse(out / "pid.svg")
        block = [e for e in root.iter() if e.get("data-layer") == "title-block"][0]
        text = visible_text(block)
        self.assertIn("PIPING AND INSTRUMENTATION DIAGRAM", text)
        self.assertIn("TK-101 TO OUT-101 VIA P-101", text)
        self.assertIn("P&ID-001", text)
        self.assertIn("GENERATED FROM ENGINEERING MODEL REVISION 0", text)
        self.assertIn("COMPLETE", text)
        self.assertIn("NOT CHECKED", text)
        self.assertIn("NOT APPROVED", text)

    def test_equipment_data_and_notes_use_current_tags_and_modeled_values(self):
        out, _, _ = self.build(self.project)
        root = parse(out / "pid.svg")
        data = {e.get("data-annotation-of"): visible_text(e) for e in root.iter()
                if e.get("data-annotation-kind") == "equipment-data"}
        self.assertEqual(set(data), {"ent-tank", "ent-pump"})
        self.assertIn("Ø 0.8 m, HEIGHT 1.2 m *", data["ent-tank"])
        self.assertIn("DESIGN DATA: NOT SPECIFIED", data["ent-tank"])
        self.assertIn("FLOW AND HEAD: NOT SPECIFIED", data["ent-pump"])
        changed = run(self.document, "set_parameter", {"entity_id": "ent-tank", "name": "height", "quantity": {
            "value": 1.5, "unit": "m", "assumption_id": "asm-tank-dimensions"}})["current"]
        out2, _, _ = self.build(changed)
        data2 = {e.get("data-annotation-of"): visible_text(e) for e in parse(out2 / "pid.svg").iter()
                 if e.get("data-annotation-kind") == "equipment-data"}
        self.assertIn("HEIGHT 1.5 m", data2["ent-tank"])
        notes = visible_text([e for e in root.iter() if e.get("data-layer") == "notes"][0])
        self.assertIn("LI-101 AND PI-101 ARE CONCEPTUAL INDICATIONS", notes)

    def test_observation_relations_are_distinct_from_piping(self):
        out, _, _ = self.build(self.project)
        root = parse(out / "pid.svg")
        groups = occurrence_groups(root)
        relations = {k: g for k, g in groups.items() if g.get("data-occurrence-kind") == "observation"}
        edges = {k: g for k, g in groups.items() if g.get("data-occurrence-kind") == "edge"}
        self.assertEqual(set(relations), {"sym-level-indicator#observes", "sym-pressure-indicator#observes"})
        self.assertEqual(len(edges), 5)
        anchors = {(float(g.get("data-paper-x")), float(g.get("data-paper-y"))) for g in groups.values()
                   if g.get("data-occurrence-kind") == "port-anchor"}
        for group in relations.values():
            self.assertEqual(group.get("data-semantic-kind"), "entity")
            line = [e for e in group.iter(f"{SVG}polyline")][0]
            self.assertEqual(line.get("data-legend-keys"), "line/observation")
            self.assertIn("stroke-dasharray", line.attrib)
            self.assertNotEqual(line.get("stroke"), "#141414")
            points = polyline_points(line)
            self.assertNotIn(points[-1], anchors)
            for a, b in zip(points, points[1:]):  # orthogonal relation legs
                self.assertTrue(a[0] == b[0] or a[1] == b[1])
        level = polyline_points([e for e in relations["sym-level-indicator#observes"].iter(f"{SVG}polyline")][0])
        self.assertEqual(level, [(220.0, 256.0), (220.0, 328.0)])

    def test_crossing_without_connection_is_drawn_as_a_gap(self):
        out, _, _ = self.build(self.project)
        groups = occurrence_groups(parse(out / "pid.svg"))
        gaps = [e for e in groups["edge-discharge-1"].iter() if e.get("data-role") == "crossing-gap"]
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap.get("data-legend-keys"), "marker/crossing-gap")
        self.assertEqual((float(gap.get("x1")) + float(gap.get("x2"))) / 2, 520.0)
        self.assertEqual(float(gap.get("y1")), 320 - 4.5)
        children = list(groups["edge-discharge-1"])
        roles = [c.get("data-role") for c in children]
        self.assertLess(roles.index("crossing-gap"), roles.index("path"))  # knockout lies under the continuous line

    def test_every_line_has_a_flow_arrow_in_flow_direction(self):
        out, _, _ = self.build(self.project)
        groups = occurrence_groups(parse(out / "pid.svg"))
        for edge_id, edge in self.project["pid"]["edges"].items():
            arrows = [e for e in groups[edge_id].iter() if e.get("data-role") == "flow-arrow"]
            self.assertEqual(len(arrows), 1, edge_id)
            numbers = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", arrows[0].get("d"))]
            tip, left, right = numbers[0:2], numbers[2:4], numbers[4:6]
            base = ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
            direction = (tip[0] - base[0], tip[1] - base[1])
            points = [(x + 100, y + 100) for x, y in edge["points"]]
            longest = max(zip(points, points[1:]), key=lambda s: math.dist(*s))
            flow = (longest[1][0] - longest[0][0], longest[1][1] - longest[0][1])
            self.assertGreater(direction[0] * flow[0] + direction[1] * flow[1], 0, edge_id)

    def test_fixture_layout_has_no_label_collisions(self):
        project = self.project
        sheet = PidSheet(project, FlowGraph(project))
        for unit in sheet.units:
            sheet.run_unit(unit)
        self.assertEqual(sheet.layout_findings, [])
        boxes = {item["id"]: item["box"] for item in sheet.symbol_items}
        segments = [s for item in sheet.edge_items for s in item["segments"]]
        segments += [s for r in sheet.relation_items for s in zip(r["points"], r["points"][1:])]
        labels = [(item["id"], item["label"]["rect"]) for item in sheet.symbol_items + sheet.edge_items if item["label"]]
        area = paper.BORDER
        for owner, rect in labels:
            self.assertTrue(contains(area, rect), owner)
            self.assertFalse(overlaps(rect, paper.TITLE_BLOCK), owner)
            for sid, box in boxes.items():
                self.assertFalse(overlaps(rect, box), f"{owner} label overlaps {sid}")
            for a, b in segments:
                self.assertFalse(segment_hits_rect(a, b, rect), f"{owner} label touches a line")
            for other, rect2 in labels:
                if other != owner:
                    self.assertFalse(overlaps(rect, rect2), f"{owner} overlaps {other}")
        panels = [c["rect"] for c in sheet.callouts] + [sheet.notes["rect"]]
        for panel in panels:
            self.assertTrue(contains(area, panel))
            self.assertFalse(overlaps(panel, paper.TITLE_BLOCK))
            for content in sheet.content_rects():
                self.assertFalse(overlaps(panel, content))

    def test_crowded_insertion_keeps_labels_clear_with_leaders(self):
        project = insert_hv103(self.document)["current"]
        out, manifests, _ = self.build(project)
        self.assertEqual(manifests["pid"]["layout_findings"], [])
        leaders = [e for e in parse(out / "pid.svg").iter() if e.get("data-role") == "leader"]
        self.assertTrue(leaders)
        self.assertTrue(all(e.get("data-legend-keys") == "convention/label-leader" for e in leaders))
        self.assertIn("convention/label-leader", self.legend_entries(out))

    def test_mixed_weight_text_has_explicit_renderer_independent_spacing(self):
        # CairoSVG anchors each tspan separately and loses a leading space ("L-101DN50"); mixed runs must be
        # start-anchored at their measured left edge with the tag/size gap as an explicit dx.
        project = insert_hv103(self.document)["current"]  # also exercises leader-placed designations
        out, _, _ = self.build(project)
        sheet = PidSheet(project, FlowGraph(project))
        for unit in sheet.units:
            sheet.run_unit(unit)
        lefts = {}
        for item in sheet.edge_items:
            label = item["label"]
            x0, _, x1, _ = label["rect"]
            designation = label["lines"][0]
            centre = {"start": x0, "middle": (x0 + x1) / 2, "end": x1}[label["anchor"]]
            lefts[item["connection"]["tag"]] = {"start": centre, "middle": centre - designation.width / 2,
                                                "end": centre - designation.width}[label["anchor"]]
        space = text_width(" ", 8.0)
        found = {}
        for name in ("pid.svg", "legend.svg"):
            for text in parse(out / name).iter(f"{SVG}text"):
                spans = list(text.iter(f"{SVG}tspan"))
                if not spans:
                    continue
                self.assertIn(text.get("text-anchor", "start"), ("start",), name)
                self.assertFalse(text.text and text.text.strip(), name)
                for index, span in enumerate(spans):
                    self.assertEqual(span.text, span.text.strip(), name)  # no whitespace left to collapse
                    self.assertFalse(span.tail and span.tail.strip(), name)
                    if index:
                        self.assertGreater(float(span.get("dx")), 0, name)
                if name == "pid.svg":
                    tag = spans[0].text
                    self.assertEqual([s.text for s in spans], [tag, "DN50"])
                    self.assertAlmostEqual(float(spans[1].get("dx")), space, places=2)
                    self.assertAlmostEqual(float(text.get("x")), lefts[tag], places=2)
                    found[tag] = True
                else:
                    self.assertEqual([s.text for s in spans], ["TAG", "DN"])
                    found["legend"] = True
        self.assertEqual(set(found), {"L-101", "L-102", "L-103", "L-104", "L-106", "L-105", "legend"})

    def test_text_is_escaped_in_every_output(self):
        document = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-1 <A&B>"})
        out, manifests, _ = self.build(document["current"])
        self.assertEqual(manifests["pid"]["status"], "complete")
        pid = parse(out / "pid.svg")
        self.assertIn("P-1 <A&B>", visible_text(pid))
        self.assertIn("P-1 <A&B>", visible_text(parse(out / "legend.svg")))
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertIn("P-1 &lt;A&amp;B&gt;", html)
        self.assertNotIn("<A&B>", html)

    def test_characters_xml_cannot_carry_are_replaced_and_reported(self):
        project = copy.deepcopy(self.project)
        project["model"]["entities"]["ent-pump"]["tag"] = "P-\x01101"
        out, manifests, _ = self.build(project)
        self.assertEqual(manifests["pid"]["status"], "pending")
        self.assertIn("xml_text_replaced", {d["code"] for d in manifests["pid"]["diagnostics"]})
        self.assertIn("P-�101", visible_text(parse(out / "pid.svg")))
        self.assertEqual(self.read_json(out / "description.json")["items"][4]["tag"], "P-\x01101")
        self.assertNotIn("\x01", (out / "description.html").read_text(encoding="utf-8"))


class PreviewAndEventTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def test_previews_follow_real_drawing_units_and_exist_before_each_callback(self):
        out = self.output_dir()
        seen = []

        def emit(event):
            self.assertLessEqual(set(event), EVENT_FIELDS)
            self.assertIn(event["kind"], ("activity", "preview", "artifact"))
            self.assertIn(event["artifact"], ("pid", "legend", "description"))
            self.assertTrue(event["message"])
            if "path" in event:
                path = out / event["path"]
                self.assertTrue(path.is_file(), event["path"])
                if path.suffix == ".svg":
                    parse(path)  # complete, parseable file at callback time
                self.assertFalse(os.path.isabs(event["path"]))
            seen.append(event)

        manifests = build_drafting(self.project, str(out), emit=emit)
        previews = [e for e in seen if e["kind"] == "preview"]
        pid_previews = [e for e in previews if e["artifact"] == "pid"]
        legend_previews = [e for e in previews if e["artifact"] == "legend"]
        total = pid_previews[0]["total"]
        self.assertEqual(total, 1 + 8 + 5 + 2 + 1 + 1)  # frame, symbols, edges, relations, labels, panels
        self.assertEqual([e["completed"] for e in pid_previews], list(range(1, total + 1)))
        self.assertEqual([e["path"] for e in pid_previews], [f"preview/pid-{n:04d}.svg" for n in range(1, total + 1)])
        self.assertEqual([e["path"] for e in legend_previews],
                         [f"preview/legend-{n:04d}.svg" for n in range(1, total + 1)])
        self.assertEqual(manifests["pid"]["previews"], [e["path"] for e in pid_previews])
        counts = []
        for event, legend in zip(pid_previews, legend_previews):
            root = parse(out / event["path"])
            self.assertEqual(root.get("data-preview"), "true")
            self.assertTrue([e for e in root.iter() if e.get("data-role") == "progress"])
            groups = occurrence_groups(root)
            counts.append(sum(1 for g in groups.values() if g.get("data-occurrence-kind") in ("symbol", "edge",
                                                                                               "observation")))
            keys = {k for e in root.iter() for k in (e.get("data-legend-keys") or "").split()}
            entries = {e.get("data-legend-entry") for e in parse(out / legend["path"]).iter()
                       if e.get("data-legend-entry")}
            self.assertEqual(entries, keys, legend["path"])  # each legend preview follows its drawing preview
        # Frame, then one symbol per unit, one edge per unit, one relation per unit, then labels and panels.
        self.assertEqual(counts, list(range(0, 16)) + [15, 15])
        tags_at_symbols = visible_text(parse(out / "preview/pid-0009.svg"))
        self.assertNotIn("L-101 DN50", tags_at_symbols)  # line designations arrive with the label unit
        self.assertIn("L-101", visible_text(parse(out / "preview/pid-0017.svg")))
        artifacts = [e for e in seen if e["kind"] == "artifact"]
        self.assertEqual([(e["artifact"], e["path"]) for e in artifacts],
                         [("pid", "pid.svg"), ("legend", "legend.svg"), ("description", "description.html")])
        self.assertLess(seen.index(pid_previews[-1]), seen.index(artifacts[0]))
        final = parse(out / "pid.svg")
        self.assertEqual(final.get("data-preview"), "false")
        self.assertFalse([e for e in final.iter() if e.get("data-role") == "progress"])

    def test_previews_are_immutable_and_build_directories_are_not_reused(self):
        out, _, _ = self.build(self.project)
        before = (out / "preview" / "pid-0001.svg").read_bytes()
        with self.assertRaises(FileExistsError):
            build_drafting(self.project, str(out))
        self.assertEqual((out / "preview" / "pid-0001.svg").read_bytes(), before)

    def test_shared_build_directory_with_another_generator(self):
        out = self.output_dir()
        (out / "preview").mkdir()
        (out / "snapshot.json").write_text("{}", encoding="utf-8")
        (out / "preview" / "display-0001.json").write_text('{"a": 1}', encoding="utf-8")

        def emit(event):  # the geometry generator keeps writing its own previews meanwhile
            if event["kind"] == "preview" and event["artifact"] == "pid" and event["completed"] == 2:
                (out / "preview" / "display-0002.json").write_text('{"b": 2}', encoding="utf-8")

        manifests = build_drafting(self.project, str(out), emit=emit)
        self.assertEqual({m["status"] for m in manifests.values()}, {"complete"})
        self.assertEqual((out / "preview" / "display-0001.json").read_text(encoding="utf-8"), '{"a": 1}')
        self.assertEqual((out / "preview" / "display-0002.json").read_text(encoding="utf-8"), '{"b": 2}')
        self.assertEqual((out / "snapshot.json").read_text(encoding="utf-8"), "{}")
        self.assertFalse([p for p in out.rglob(".*.tmp")])

    def test_cancel_between_units_stops_without_final_outputs(self):
        out = self.output_dir()
        previews = []

        def emit(event):
            if event["kind"] == "preview" and event["artifact"] == "pid":
                previews.append(event["path"])

        with self.assertRaises(InterruptedError):
            build_drafting(self.project, str(out), emit=emit, cancelled=lambda: len(previews) >= 3)
        self.assertEqual(previews, ["preview/pid-0001.svg", "preview/pid-0002.svg", "preview/pid-0003.svg"])
        self.assertEqual(sorted(p.name for p in (out / "preview").iterdir()),
                         ["legend-0001.svg", "legend-0002.svg", "legend-0003.svg",
                          "pid-0001.svg", "pid-0002.svg", "pid-0003.svg"])
        for name in OUTPUT_FILES:
            self.assertNoFile(out / name)

    def test_cancel_before_any_work_writes_nothing(self):
        out = self.output_dir()
        with self.assertRaises(InterruptedError):
            build_drafting(self.project, str(out), cancelled=lambda: True)
        self.assertEqual(list((out / "preview").iterdir()), [])
        for name in OUTPUT_FILES:
            self.assertNoFile(out / name)

    def test_cancel_after_drawing_but_before_publication_writes_no_artifact(self):
        out = self.output_dir()
        state = {"previews": 0}

        def emit(event):
            if event["kind"] == "preview" and event["artifact"] == "legend":
                state["previews"] += 1

        with self.assertRaises(InterruptedError):
            build_drafting(self.project, str(out), emit=emit, cancelled=lambda: state["previews"] >= 18)
        self.assertEqual(state["previews"], 18)
        for name in OUTPUT_FILES:
            self.assertNoFile(out / name)


class FailureAndPendingTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def test_invalid_input_fails_every_artifact_without_drawings(self):
        for project in ("not a project", {"id": "x"}, None):
            out, manifests, _ = self.build(project)
            for adapter, manifest in manifests.items():
                self.assertEqual(manifest["status"], "failed", adapter)
                self.assertTrue(any(d["severity"] == "error" for d in manifest["diagnostics"]))
                self.assertEqual(manifest["representations"], {})
                self.assertEqual(manifest["files"], [])
                self.assertEqual(self.read_json(out / f"{adapter}.manifest.json"), manifest)
            for name in ("pid.svg", "legend.svg", "description.json", "description.html", "item-index.json",
                         "drawing.inventory.json"):
                self.assertNoFile(out / name)
            self.assertEqual(list((out / "preview").iterdir()), [])

    def test_structural_error_is_reported_with_identity(self):
        broken = copy.deepcopy(self.project)
        broken["model"]["connections"]["conn-suction-1"]["to_port_id"] = "port-missing"
        out, manifests, events = self.build(broken)
        self.assertEqual(manifests["pid"]["status"], "failed")
        self.assertEqual(manifests["pid"]["project_id"], "project-pump-skid")
        self.assertEqual(manifests["pid"]["input_digest"], canonical_digest(broken))
        self.assertIn("dangling_reference", {d["code"] for d in manifests["pid"]["diagnostics"]})
        self.assertFalse([e for e in events if e["kind"] in ("preview", "artifact")])

    def test_missing_output_directory_is_rejected(self):
        out = self.output_dir() / "absent"
        with self.assertRaises(NotADirectoryError):
            build_drafting(self.project, str(out))

    def test_pending_diagram_condition_is_never_complete(self):
        moved = run(self.document, "move_symbol", {"symbol_id": "sym-check-valve", "x": 500.0, "y": 220.0,
                                                   "rotation_deg": 0.0})["current"]
        self.assertEqual(moved["pid"]["edges"]["edge-discharge-1"]["status"], "needs_reroute")
        out, manifests, _ = self.build(moved)
        for manifest in manifests.values():
            self.assertEqual(manifest["status"], "pending")
            self.assertIn("edge_not_current", {d["code"] for d in manifest["diagnostics"]})
        root = parse(out / "pid.svg")
        self.assertEqual(root.get("data-status"), "pending")
        self.assertIn("PENDING: 1 OPEN ITEM", visible_text(root))
        groups = occurrence_groups(root)
        retained = [e for e in groups["edge-discharge-1"].iter() if e.get("data-role") == "retained-path"]
        self.assertEqual(polyline_points(retained[0]), [(460.0, 372.0), (460.0, 320.0), (564.0, 320.0)])
        self.assertIn("P&ID PATH NEEDS REROUTE", visible_text(groups["edge-discharge-1"]))
        self.assertIn("L-103 P&ID PATH NOT CURRENT", visible_text(root))

    def test_pending_physical_condition_is_never_complete(self):
        project = insert_hv103(self.document)["current"]
        out, manifests, _ = self.build(project)
        for manifest in manifests.values():
            self.assertEqual(manifest["status"], "pending")
            self.assertIn("placement_unresolved", {d["code"] for d in manifest["diagnostics"]})
        root = parse(out / "pid.svg")
        groups = occurrence_groups(root)
        self.assertIn("3D PLACEMENT PENDING", visible_text(groups["sym-hv103"]))
        self.assertIn("3D ROUTE PENDING", visible_text(groups["edge-hv103-out"]))
        self.assertIn("HV-103 3D PLACEMENT PENDING", visible_text(root))
        description = self.read_json(out / "description.json")
        self.assertEqual(description["status"], "pending")
        self.assertIn("HV-103", {tag for item in description["open_items"] for tag in item["tags"]})

    def test_out_of_sheet_symbol_is_a_diagnostic_not_a_clamp(self):
        moved = run(self.document, "move_symbol", {"symbol_id": "sym-level-indicator", "x": 1200.0, "y": 140.0,
                                                   "rotation_deg": 0.0})["current"]
        out, manifests, _ = self.build(moved)
        self.assertEqual(manifests["pid"]["status"], "pending")
        codes = {d["code"] for d in manifests["pid"]["diagnostics"]}
        self.assertIn("out_of_sheet", codes)
        item = self.index_by_tag(out)["LI-101"]
        symbol = [o for o in item["occurrences"] if o["occurrence_kind"] == "symbol"][0]
        self.assertIsNone(symbol["zone"])
        self.assertEqual(symbol["ref"], "P&ID-001 / outside sheet")
        self.assertEqual(symbol["position"], [1300.0, 240.0])


if __name__ == "__main__":
    unittest.main()
