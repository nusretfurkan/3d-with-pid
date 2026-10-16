"""Exact legend inventory, tagged-item index and A-I / 1-10 zone references."""

from __future__ import annotations

import copy
import math
import unittest
from fractions import Fraction

from skid.drafting import paper
from skid.drafting.inventory import parse_inventory
from tests.test_drafting_support import (
    FIXTURE_TAGS,
    DraftingCase,
    errors,
    fixture_document,
    insert_hv103,
    occurrence_groups,
    parse,
    remove_check_valve,
    remove_indicator,
    run,
    undo,
    visible_text,
)

FIXTURE_KEYS = {
    "convention/zone-grid",
    "symbol/tank",
    "symbol/centrifugal_pump",
    "symbol/isolation_valve",
    "symbol/check_valve",
    "symbol/boundary",
    "symbol/indicator",
    "line/process",
    "line/observation",
    "marker/flow-arrow",
    "marker/crossing-gap",
    "convention/line-designation",
    "convention/equipment-data",
    "convention/assumed-value",
    "abbr/DN",
    "abbr/dia",
}
FIXTURE_ZONES = {
    "TK-101": ["E2"], "HV-101": ["E3"], "P-101": ["E4"], "NRV-101": ["F5"], "HV-102": ["F6"], "OUT-101": ["F7"],
    "LI-101": ["G2", "F2"], "PI-101": ["G5", "F5"],
    "L-101": ["E3"], "L-102": ["E4"], "L-103": ["F5"], "L-104": ["F6"], "L-105": ["F7"],
}


class LegendInventoryTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def keys(self, out):
        return set(self.inventory(out)["keys"])

    def test_fixture_legend_lists_exactly_the_drawn_conventions(self):
        out, manifests, _ = self.build(self.project)
        emitted = {k for e in parse(out / "pid.svg").iter() for k in (e.get("data-legend-keys") or "").split()}
        self.assertEqual(emitted, FIXTURE_KEYS)
        self.assertEqual(self.keys(out), FIXTURE_KEYS)
        self.assertEqual(self.legend_entries(out), sorted(FIXTURE_KEYS))
        self.assertEqual(set(manifests["pid"]["legend_keys"]), FIXTURE_KEYS)
        for unused in ("symbol/unsupported", "line/process/needs-reroute", "line/process/unresolved",
                       "marker/physical-placement-pending", "marker/physical-route-pending",
                       "convention/label-leader"):
            self.assertNotIn(unused, self.legend_entries(out))

    def test_inventory_file_is_read_back_from_the_emitted_svg(self):
        out, _, _ = self.build(self.project)
        recorded = self.inventory(out)
        parsed = parse_inventory((out / "pid.svg").read_text(encoding="utf-8"))
        for key in ("keys", "uses", "occurrences", "tagged_ids", "legend_samples_found", "sheet_id"):
            self.assertEqual(recorded[key], parsed[key], key)
        self.assertEqual(recorded["project_id"], "project-pump-skid")
        self.assertEqual(recorded["paper"]["row_labels_top_to_bottom"], "IHGFEDCBA")

    def test_repeated_symbols_appear_once_with_every_user(self):
        out, _, _ = self.build(self.project)
        entries = [e for e in parse(out / "legend.svg").iter() if e.get("data-legend-entry") == "symbol/isolation_valve"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get("data-used-by").split(), ["HV-101", "HV-102"])
        process = [e for e in parse(out / "legend.svg").iter() if e.get("data-legend-entry") == "line/process"][0]
        self.assertEqual(process.get("data-used-by").split(), ["L-101", "L-102", "L-103", "L-104", "L-105"])

    def test_legend_samples_never_enter_an_inventory(self):
        out, _, _ = self.build(self.project)
        legend = parse(out / "legend.svg")
        self.assertFalse([e for e in legend.iter() if e.get("data-legend-keys") is not None])
        samples = sorted(e.get("data-legend-sample") for e in legend.iter() if e.get("data-legend-sample"))
        self.assertEqual(samples, sorted(FIXTURE_KEYS))
        self.assertFalse([e for e in parse(out / "pid.svg").iter() if e.get("data-legend-sample") is not None])
        self.assertEqual(self.inventory(out)["legend_samples_found"], 0)
        recursive = parse_inventory((out / "legend.svg").read_text(encoding="utf-8"))
        self.assertEqual(recursive["keys"], [])  # a legend sheet contributes no drawn conventions

    def test_new_variant_from_a_command_appears_and_undo_removes_it(self):
        moved_doc = run(self.document, "move_symbol", {"symbol_id": "sym-check-valve", "x": 500.0, "y": 220.0,
                                                       "rotation_deg": 0.0})
        out, _, _ = self.build(moved_doc["current"])
        added = {"line/process/needs-reroute", "line/process/detached-end"}
        self.assertEqual(self.keys(out) - FIXTURE_KEYS, added)
        self.assertEqual(set(self.legend_entries(out)), self.keys(out))
        restored = undo(moved_doc)
        out2, manifests, _ = self.build(restored["current"])
        self.assertEqual(self.keys(out2), FIXTURE_KEYS)
        self.assertEqual(set(self.legend_entries(out2)), FIXTURE_KEYS)
        self.assertEqual(manifests["legend"]["status"], "complete")

    def test_insertion_reuses_the_valve_entry_and_adds_pending_markers(self):
        inserted = insert_hv103(self.document)
        out, manifests, _ = self.build(inserted["current"])
        keys = self.keys(out)
        self.assertEqual(keys - FIXTURE_KEYS, {"marker/physical-placement-pending", "marker/physical-route-pending",
                                               "convention/label-leader"})
        self.assertEqual(set(self.legend_entries(out)), keys)
        entry = [e for e in parse(out / "legend.svg").iter() if e.get("data-legend-entry") == "symbol/isolation_valve"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0].get("data-used-by").split(), ["HV-101", "HV-103", "HV-102"])
        index = self.index_by_tag(out)
        self.assertEqual(set(index), set(FIXTURE_TAGS) | {"HV-103", "L-106"})
        self.assertEqual(index["HV-103"]["occurrences"][0]["ref"], "P&ID-001 / F6")
        self.assertEqual(manifests["legend"]["index_items"], 15)
        restored = undo(inserted)
        out2, _, _ = self.build(restored["current"])
        self.assertEqual(self.keys(out2), FIXTURE_KEYS)
        self.assertEqual(set(self.index_by_tag(out2)), set(FIXTURE_TAGS))

    def test_unresolved_connection_without_a_path_is_its_own_variant(self):
        project = copy.deepcopy(insert_hv103(self.document)["current"])
        edge = project["pid"]["edges"]["edge-hv103-out"]
        edge.update({"points": [], "status": "unresolved", "reason": "diagram path not yet chosen"})
        self.assertEqual(errors(project), [])
        out, manifests, _ = self.build(project)
        self.assertEqual(manifests["pid"]["status"], "pending")
        self.assertIn("line/process/topology-hint", self.keys(out))
        self.assertIn("line/process/topology-hint", self.legend_entries(out))
        group = occurrence_groups(parse(out / "pid.svg"))["edge-hv103-out"]
        hint = [e for e in group.iter() if e.get("data-role") == "topology-hint"]
        self.assertEqual(len(hint), 1)
        self.assertIn("NO DRAWABLE P&ID PATH", visible_text(group))
        self.assertEqual(self.index_by_tag(out)["L-106"]["occurrences"][0]["occurrence_id"], "edge-hv103-out")

    def test_removing_the_last_use_removes_its_entry(self):
        removed = remove_check_valve(self.project)
        self.assertEqual(errors(removed), [])  # a structurally valid removal, not a broken model
        out, manifests, _ = self.build(removed)
        self.assertEqual(manifests["pid"]["status"], "complete")
        self.assertNotIn("symbol/check_valve", self.keys(out))
        self.assertNotIn("symbol/check_valve", self.legend_entries(out))
        self.assertIn("symbol/isolation_valve", self.legend_entries(out))
        self.assertEqual(set(self.index_by_tag(out)), set(FIXTURE_TAGS) - {"NRV-101", "L-104"})

    def test_a_symbol_still_used_elsewhere_keeps_its_entry(self):
        one = remove_indicator(self.project, "ent-pressure-indicator")
        self.assertEqual(errors(one), [])
        out, _, _ = self.build(one)
        keys = self.keys(out)
        self.assertIn("symbol/indicator", keys)
        self.assertIn("line/observation", keys)
        self.assertNotIn("marker/crossing-gap", keys)  # only the PI-101 relation crossed a line
        entry = [e for e in parse(out / "legend.svg").iter() if e.get("data-legend-entry") == "symbol/indicator"][0]
        self.assertEqual(entry.get("data-used-by"), "LI-101")
        both = remove_indicator(one, "ent-level-indicator")
        self.assertEqual(errors(both), [])
        out2, _, _ = self.build(both)
        self.assertFalse({"symbol/indicator", "line/observation"} & self.keys(out2))
        self.assertEqual(set(self.legend_entries(out2)), self.keys(out2))

    def test_structurally_invalid_removal_is_rejected_not_drawn(self):
        broken = copy.deepcopy(self.project)
        del broken["model"]["entities"]["ent-check-valve"]
        self.assertTrue(errors(broken))
        out, manifests, _ = self.build(broken)
        self.assertEqual({m["status"] for m in manifests.values()}, {"failed"})
        for name in ("pid.svg", "legend.svg", "item-index.json", "drawing.inventory.json"):
            self.assertNoFile(out / name)

    def test_rename_updates_every_output_and_undo_restores_it(self):
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-201"})
        out, _, _ = self.build(renamed["current"])
        pid_text = visible_text(parse(out / "pid.svg"))
        legend_text = visible_text(parse(out / "legend.svg"))
        self.assertIn("P-201", pid_text)
        self.assertNotIn("P-101", pid_text)
        self.assertNotIn("P-101", legend_text)
        index = self.index_by_tag(out)
        self.assertEqual(index["P-201"]["semantic_id"], "ent-pump")
        self.assertEqual(index["L-102"]["description"], "LINE HV-101 TO P-201, DN50")
        restored = undo(renamed)
        out2, _, _ = self.build(restored["current"])
        self.assertIn("P-101", visible_text(parse(out2 / "pid.svg")))
        self.assertNotIn("P-201", visible_text(parse(out2 / "pid.svg")))
        self.assertEqual(set(self.index_by_tag(out2)), set(FIXTURE_TAGS))


class ItemIndexTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()
        self.project = self.document["current"]

    def test_index_equals_drawn_tagged_items_with_every_occurrence(self):
        out, _, _ = self.build(self.project)
        index = self.index(out)
        inventory = self.inventory(out)
        model = self.project["model"]
        self.assertEqual({i["semantic_id"] for i in index["items"]}, set(inventory["tagged_ids"]))
        self.assertEqual(set(inventory["tagged_ids"]), set(model["entities"]) | set(model["connections"]))
        groups = occurrence_groups(parse(out / "pid.svg"))
        for item in index["items"]:
            emitted = {k for k, g in groups.items() if g.get("data-semantic-id") == item["semantic_id"]
                       and g.get("data-occurrence-kind") in ("symbol", "edge", "observation")}
            self.assertEqual({o["occurrence_id"] for o in item["occurrences"]}, emitted, item["tag"])
            for occurrence in item["occurrences"]:
                group = groups[occurrence["occurrence_id"]]
                self.assertEqual(occurrence["sheet_id"], "P&ID-001")
                self.assertEqual(occurrence["ref"], f"P&ID-001 / {occurrence['zone']}")
                self.assertEqual(group.get("data-zone"), occurrence["zone"])
                self.assertEqual(occurrence["position"], [float(group.get("data-paper-x")),
                                                          float(group.get("data-paper-y"))])
                self.assertEqual(paper.zone_of(*occurrence["position"]), occurrence["zone"])
        legend_rows = {e.get("data-semantic-id"): e for e in parse(out / "legend.svg").iter()
                       if e.get("data-index-row")}
        self.assertEqual(set(legend_rows), {i["semantic_id"] for i in index["items"]})
        self.assertEqual([i["tag"] for i in index["items"]],
                         ["TK-101", "P-101", "HV-101", "NRV-101", "HV-102", "LI-101", "PI-101", "OUT-101",
                          "L-101", "L-102", "L-103", "L-104", "L-105"])

    def test_fixture_zone_references(self):
        out, _, _ = self.build(self.project)
        zones = {tag: [o["zone"] for o in item["occurrences"]] for tag, item in self.index_by_tag(out).items()}
        self.assertEqual(zones, FIXTURE_ZONES)

    def test_zone_grid_corners_outside_points_and_label_order(self):
        self.assertEqual(paper.zone_of(30, 30), "I1")
        self.assertEqual(paper.zone_of(1170, 810), "A10")
        self.assertEqual(paper.zone_of(1170, 30), "I10")
        self.assertEqual(paper.zone_of(30, 810), "A1")
        self.assertEqual(paper.zone_of(144, 400), "E2")  # internal column line belongs to the right cell
        self.assertEqual(paper.zone_of(486, 320), "F5")  # the L-103 midpoint lies exactly on a column line
        for x, y in ((29.999, 100), (1170.0001, 100), (100, 29.999), (100, 810.0001), (-5, -5),
                     (math.nan, 100), (100, math.inf), (-math.inf, 100)):
            self.assertIsNone(paper.zone_of(x, y))
        self.assertEqual(paper.zone_ref(None), "P&ID-001 / outside sheet")
        rows = [paper.row_of(paper.row_centre(letter)) for letter in "ABCDEFGHI"]
        self.assertEqual(rows, list("ABCDEFGHI"))
        self.assertLess(paper.row_centre("I"), paper.row_centre("A"))  # I at the top, A at the bottom
        self.assertEqual([paper.column_of(paper.column_centre(n)) for n in range(1, 11)], list(range(1, 11)))

    def test_canonical_boundaries_are_the_correctly_rounded_grid_positions(self):
        self.assertEqual(paper.COLUMN_BOUNDARIES, tuple(float(30 + 114 * k) for k in range(11)))
        self.assertEqual(paper.ROW_BOUNDARIES, tuple(float(Fraction(30) + Fraction(780, 9) * k) for k in range(10)))
        self.assertEqual((paper.ROW_BOUNDARIES[0], paper.ROW_BOUNDARIES[-1]), (30.0, 810.0))
        self.assertEqual(paper.column_lines(), list(paper.COLUMN_BOUNDARIES[1:-1]))
        self.assertEqual(paper.row_lines(), list(paper.ROW_BOUNDARIES[1:-1]))
        # Lines 4 and 6 are doubles just below their exact rationals; the doubles are the boundaries.
        self.assertLess(Fraction(paper.row_lines()[4]), Fraction(30) + Fraction(780, 9) * 5)
        self.assertLess(Fraction(paper.row_lines()[6]), Fraction(30) + Fraction(780, 9) * 7)

    def test_every_internal_line_belongs_right_or_below_with_exact_neighbours(self):
        columns, rows = paper.column_lines(), paper.row_lines()
        self.assertEqual((len(columns), len(rows)), (9, 8))
        for index, x in enumerate(columns):  # the line between columns index+1 and index+2
            self.assertEqual(paper.column_of(x), index + 2, x)
            self.assertEqual(paper.column_of(math.nextafter(x, -math.inf)), index + 1, x)
            self.assertEqual(paper.column_of(math.nextafter(x, math.inf)), index + 2, x)
        for index, y in enumerate(rows):  # the line between rows ROW_LETTERS[index] (above) and [index+1] (below)
            above, below = paper.ROW_LETTERS[index], paper.ROW_LETTERS[index + 1]
            self.assertEqual(paper.row_of(y), below, y)
            self.assertEqual(paper.row_of(math.nextafter(y, -math.inf)), above, y)
            self.assertEqual(paper.row_of(math.nextafter(y, math.inf)), below, y)
        self.assertEqual(paper.row_of(rows[4]), "D")  # reported defect: was E
        self.assertEqual(paper.row_of(rows[6]), "B")  # reported defect: was C
        self.assertEqual(paper.zone_of(columns[4], rows[4]), "D6")
        self.assertEqual(paper.zone_of(math.nextafter(columns[4], 0.0), math.nextafter(rows[4], 0.0)), "E5")

    def test_outer_border_ownership_and_its_neighbours(self):
        for value, column, letter in ((30.0, 1, "I"), (math.nextafter(30.0, math.inf), 1, "I")):
            self.assertEqual((paper.column_of(value), paper.row_of(value)), (column, letter))
        self.assertEqual((paper.column_of(1170.0), paper.column_of(math.nextafter(1170.0, 0.0))), (10, 10))
        self.assertEqual((paper.row_of(810.0), paper.row_of(math.nextafter(810.0, 0.0))), ("A", "A"))
        for value in (math.nextafter(30.0, -math.inf),):
            self.assertIsNone(paper.column_of(value))
            self.assertIsNone(paper.row_of(value))
        self.assertIsNone(paper.column_of(math.nextafter(1170.0, math.inf)))
        self.assertIsNone(paper.row_of(math.nextafter(810.0, math.inf)))

    def test_non_float_coordinates_follow_their_serialized_binary64_value(self):
        exact = Fraction(30) + Fraction(780, 9) * 5  # E/D line; its binary64 rounding is the boundary itself
        self.assertEqual(float(exact), paper.row_lines()[4])
        self.assertEqual(paper.row_of(exact), "D")
        self.assertEqual(paper.row_of(Fraction(paper.row_lines()[4])), "D")
        self.assertEqual(paper.column_of(144), 2)
        self.assertEqual(paper.column_of(Fraction(287, 2)), 1)  # 143.5

    def test_drawn_and_exported_grid_lines_are_the_lookup_boundaries(self):
        out, _, _ = self.build(self.project)
        for sheet in ("pid.svg", "legend.svg"):
            root = parse(out / sheet)
            for kind, lines, a, b in (("column", paper.column_lines(), "x1", "x2"), ("row", paper.row_lines(), "y1", "y2")):
                ticks = [e for e in root.iter() if e.get("data-grid-line") == kind]
                self.assertEqual(len(ticks), 2 * len(lines), sheet)  # both margins
                for tick in ticks:
                    self.assertEqual(tick.get(a), tick.get(b))
                    self.assertEqual(float(tick.get(a)), lines[int(tick.get("data-boundary-index")) - 1], sheet)
        for record in (self.inventory(out)["paper"], self.index(out)["paper"]):
            self.assertEqual(record["column_boundaries"], list(paper.COLUMN_BOUNDARIES))
            self.assertEqual(record["row_boundaries_top_to_bottom"], list(paper.ROW_BOUNDARIES))

    def test_an_occurrence_exactly_on_a_row_line_takes_the_row_below(self):
        line = paper.row_lines()[4]  # E above, D below
        for paper_y, expected in ((line, "D2"), (math.nextafter(line, -math.inf), "E2"),
                                  (math.nextafter(line, math.inf), "D2")):
            stored_y = paper_y - 100.0
            self.assertEqual(paper.to_paper(120.0, stored_y), (220.0, paper_y))
            moved = run(self.document, "move_symbol", {"symbol_id": "sym-level-indicator", "x": 120.0, "y": stored_y,
                                                       "rotation_deg": 0.0})["current"]
            out, _, _ = self.build(moved)
            symbol = [o for o in self.index_by_tag(out)["LI-101"]["occurrences"] if o["occurrence_kind"] == "symbol"][0]
            self.assertEqual((symbol["zone"], symbol["position"]), (expected, [220.0, paper_y]))
            group = occurrence_groups(parse(out / "pid.svg"))["sym-level-indicator"]
            self.assertEqual((group.get("data-zone"), float(group.get("data-paper-y"))), (expected, paper_y))

    def test_diagram_move_updates_zones_and_physical_move_does_not(self):
        moved = run(self.document, "move_symbol", {"symbol_id": "sym-pump", "x": 430.0, "y": 300.0,
                                                   "rotation_deg": 0.0})["current"]
        out, _, _ = self.build(moved)
        index = self.index_by_tag(out)
        self.assertEqual(index["P-101"]["occurrences"][0]["zone"], "E5")
        self.assertEqual(index["P-101"]["occurrences"][0]["position"], [530.0, 400.0])
        self.assertEqual(index["TK-101"]["occurrences"][0]["zone"], "E2")
        physical = run(self.document, "move_equipment", {"entity_id": "ent-pump", "placement": {
            "position_m": [1.6, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0], "status": "placed", "manual": True,
            "assumption_id": "asm-skid-layout"}})["current"]
        out2, manifests, _ = self.build(physical)
        zones = {tag: [o["zone"] for o in item["occurrences"]] for tag, item in self.index_by_tag(out2).items()}
        self.assertEqual(zones, FIXTURE_ZONES)
        self.assertEqual(manifests["pid"]["status"], "pending")  # incident 3D routes became stale

    def test_multiple_occurrences_each_have_their_own_reference(self):
        project = copy.deepcopy(self.project)
        project["pid"]["symbols"]["sym-tank-2"] = {"id": "sym-tank-2", "entity_id": "ent-tank", "x": 40.0,
                                                   "y": 480.0, "rotation_deg": 0.0, "manual": True}
        self.assertEqual(errors(project), [])
        out, manifests, _ = self.build(project)
        self.assertEqual(manifests["pid"]["status"], "complete")
        tank = self.index_by_tag(out)["TK-101"]
        self.assertEqual([(o["occurrence_id"], o["zone"]) for o in tank["occurrences"]],
                         [("sym-tank", "E2"), ("sym-tank-2", "C1")])
        self.assertEqual(manifests["pid"]["representations"]["ent-tank"], ["sym-tank", "sym-tank-2"])
        tank_entries = [e for e in parse(out / "legend.svg").iter() if e.get("data-legend-entry") == "symbol/tank"]
        self.assertEqual(len(tank_entries), 1)
        row = [e for e in parse(out / "legend.svg").iter() if e.get("data-index-row") == "ent-tank"][0]
        self.assertEqual(row.get("data-refs"), "P&ID-001/E2 P&ID-001/C1")
        moved = copy.deepcopy(project)
        moved["pid"]["symbols"]["sym-tank-2"]["x"] = 300.0  # paper x 400 -> column 4
        out2, _, _ = self.build(moved)
        tank2 = self.index_by_tag(out2)["TK-101"]
        self.assertEqual([o["zone"] for o in tank2["occurrences"]], ["E2", "C4"])


if __name__ == "__main__":
    unittest.main()
