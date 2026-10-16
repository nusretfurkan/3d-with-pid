"""Process narrative: flow-ordered readable paragraphs with current tags, grounded physical words and honest gaps.

Build tests run the real drafting pipeline on fixture copies changed through
the command layer. Topology tests (branch, merge, loop, separate groups) call
the narrative on hand-edited models directly, because the built-in templates
cannot express those shapes through validated commands.
"""

from __future__ import annotations

import copy
import re
import unittest
from html.parser import HTMLParser

from skid.drafting.description import DescriptionBuilder, anchor
from skid.drafting.graph import FlowGraph
from skid.drafting.narrative import process_narrative
from tests.test_drafting_support import DraftingCase, fixture_document, insert_hv103, run, undo

FLOW_IDS = ["ent-tank", "conn-suction-1", "ent-suction-valve", "conn-suction-2", "ent-pump", "conn-discharge-1",
            "ent-check-valve", "conn-discharge-2", "ent-discharge-valve", "conn-discharge-3", "ent-outlet-boundary"]


def mention_order(paragraphs):
    return list(dict.fromkeys(ref for p in paragraphs for ref in p["refs"]))


def narrative_text(description):
    return " ".join(p["text"] for p in description["process_narrative"])


def assert_told_in_order(case, text, phrases):
    """Each phrase occurs (case-insensitively), first occurrences in the given order."""
    positions = [text.lower().find(phrase.lower()) for phrase in phrases]
    case.assertNotIn(-1, positions, dict(zip(phrases, positions)))
    case.assertEqual(positions, sorted(positions), dict(zip(phrases, positions)))


def placement(rotation, status="placed"):
    if status != "placed":
        return {"status": "unresolved", "position_m": None, "rotation_deg": None, "manual": True,
                "assumption_id": "asm-skid-layout", "reason": "test: placement withdrawn"}
    return {"status": "placed", "position_m": [0.0, 0.0, 0.0], "rotation_deg": rotation, "manual": True,
            "assumption_id": "asm-skid-layout"}


class _Body(HTMLParser):
    """Top-level structure of description.html: narrative paragraphs, details, ids and the elements before details."""

    def __init__(self):
        super().__init__()
        self.ids, self.hrefs, self.order, self.details, self.depth = [], [], [], [], 0
        self.inside_details = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details.append(attrs)
            self.inside_details = True
        if attrs.get("id"):
            self.ids.append((attrs["id"], bool(self.inside_details)))
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.hrefs.append(attrs["href"][1:])
        role = attrs.get("data-role") or ("warn" if "warn" in (attrs.get("class") or "").split() else None)
        if role:
            self.order.append((role, bool(self.inside_details)))

    def handle_endtag(self, tag):
        if tag == "details":
            self.inside_details = False


def structure(html):
    parser = _Body()
    parser.feed(html)
    return parser


class NarrativeBuildTests(DraftingCase):
    def setUp(self):
        self.document = fixture_document()

    def describe(self, project, **kwargs):
        out, manifests, _ = self.build(project, **kwargs)
        return out, manifests, self.read_json(out / "description.json")

    def test_paragraphs_follow_flow_order_with_indicators_beside_their_items(self):
        _, _, description = self.describe(self.document["current"])
        paragraphs = description["process_narrative"]
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "scope"])
        self.assertEqual(set(mention_order(paragraphs)), set(FLOW_IDS) | {"ent-level-indicator", "ent-pressure-indicator"})
        assert_told_in_order(self, paragraphs[0]["text"], [
            "tank TK-101", "level indicator LI-101", "line L-101", "isolation valve HV-101", "line L-102",
            "centrifugal pump P-101", "pressure indicator PI-101", "line L-103", "check valve NRV-101", "line L-104",
            "isolation valve HV-102", "line L-105", "scope boundary OUT-101"])
        for paragraph in paragraphs:
            self.assertEqual(paragraph["text"],
                             "".join(p["text"] if "text" in p else p["tag"] for p in paragraph["parts"]))
            self.assertEqual(set(paragraph), {"text", "parts", "refs", "role", "run"})
        text = narrative_text(description)
        self.assertTrue(text.startswith("The design starts with a vertical cylindrical tank TK-101, 0.8 m in diameter "
                                        "and 1.2 m high (assumed dimensions)."), text)
        for phrase in ("Level indicator LI-101 observes the level of TK-101",
                       "Flow leaves TK-101 through line L-101 from its side outlet, 0.2 m above the shell base",
                       "to isolation valve HV-101, on the suction side of P-101",
                       "Line L-102 continues from HV-101 to the end-suction inlet of centrifugal pump P-101",
                       "P-101 discharges from its top outlet through line L-103 to check valve NRV-101",
                       "isolation valve HV-102, on the discharge side of P-101",
                       "to scope boundary OUT-101, where the modeled scope ends"):
            self.assertIn(phrase, text)
        self.assertNotRegex(text.lower(), r"\bbottom\b|\bmanual\b|\bcontrols?\b|\bregulat|normally|\bfail")
        self.assertNotRegex(text, r"\d\s*(bar|kPa|MPa|psi|°C|m3/h|rpm|kW)\b")
        self.assertIn("valve operators and normal valve positions are not modeled", text)

    def test_rename_and_undo_follow_stable_ids(self):
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-121"})
        out, _, description = self.describe(renamed["current"])
        text = narrative_text(description)
        self.assertIn("centrifugal pump P-121", text)
        self.assertNotIn("P-101", text)
        self.assertNotIn("P-101", (out / "description.html").read_text(encoding="utf-8"))
        refs = [p for para in description["process_narrative"] for p in para["parts"] if p.get("ref") == "ent-pump"]
        self.assertTrue(refs and all(p["tag"] == "P-121" for p in refs))
        _, _, restored = self.describe(undo(renamed)["current"])
        self.assertIn("centrifugal pump P-101", narrative_text(restored))
        self.assertNotIn("P-121", narrative_text(restored))

    def test_inserted_valve_is_told_in_place_and_its_open_placement_is_stated(self):
        inserted = insert_hv103(self.document)["current"]
        out, manifests, description = self.describe(inserted)
        text = narrative_text(description)
        assert_told_in_order(self, text, ["check valve NRV-101", "line L-104", "isolation valve HV-103", "line L-106",
                                          "isolation valve HV-102", "line L-105", "scope boundary OUT-101"])
        self.assertIn("Line L-104 continues from NRV-101 to isolation valve HV-103 (its 3D placement is not yet "
                      "resolved), on the discharge side of P-101.", text)
        self.assertEqual(manifests["description"]["status"], "pending")
        html = (out / "description.html").read_text(encoding="utf-8")
        page = structure(html)
        self.assertIn(("warn", False), page.order, "the pending warning stays outside the collapsed details")
        self.assertRegex(html, r"<summary>Item details <span class=\"count\">\d+ items</span> "
                               r"<span class=\"count warn\">pending: \d+ open item\(s\)</span></summary>")

    def test_physical_words_follow_parameters_rotation_and_placement(self):
        changed = run(self.document, "set_parameter", {"entity_id": "ent-tank", "name": "outlet_height",
                                                       "quantity": {"value": 0.35, "unit": "m",
                                                                    "assumption_id": "asm-tank-dimensions"}})
        _, _, description = self.describe(changed["current"])
        self.assertIn("from its side outlet, 0.35 m above the shell base", narrative_text(description))

        # Pump turned upside down: its discharge no longer faces up, so "top" is not claimed.
        flipped = run(self.document, "move_equipment", {"entity_id": "ent-pump", "placement": placement([180.0, 0.0, 0.0])})
        _, _, description = self.describe(flipped["current"])
        self.assertIn("P-101 discharges from its bottom outlet through line L-103", narrative_text(description))

    def test_tank_dimensions_and_outlet_height_follow_the_shell_attitude(self):
        def told(rotation):
            moved = run(self.document, "move_equipment", {"entity_id": "ent-tank", "placement": placement(rotation)})
            return narrative_text(self.describe(moved["current"])[2])

        # Turned 90 degrees about y: the shell axis lies horizontal, so the template height is a length,
        # and the side nozzle now points down.
        text = told([0.0, 90.0, 0.0])
        self.assertIn("a horizontal cylindrical tank TK-101, 0.8 m in diameter and 1.2 m long (assumed dimensions).",
                      text)
        self.assertIn("through line L-101 from its bottom outlet to isolation valve HV-101", text)
        self.assertNotRegex(text, r"vertical|\d m high|above the shell base")

        # Upside down: still 1.2 m high, but the shell base is on top, so the outlet is not "above" it.
        text = told([180.0, 0.0, 0.0])
        self.assertIn("a vertical cylindrical tank TK-101, 0.8 m in diameter and 1.2 m high (assumed dimensions).",
                      text)
        self.assertIn("Flow leaves TK-101 through line L-101 from its side outlet to isolation valve HV-101", text)
        self.assertNotRegex(text, r"m (above|below)|shell base")

        # Tilted 45 degrees: neither vertical nor horizontal, and the nozzle has no top, bottom or side facing.
        text = told([0.0, 45.0, 0.0])
        self.assertIn("a cylindrical tank TK-101, 0.8 m in diameter with a 1.2 m shell length along its axis "
                      "(assumed dimensions).", text)
        self.assertIn("Flow leaves TK-101 through line L-101 to isolation valve HV-101", text)
        self.assertNotRegex(text, r"vertical|horizontal|\d m high|\d m long|shell base|(side|bottom) outlet")

    def test_unplaced_items_get_no_orientation_claim(self):
        project = copy.deepcopy(self.document["current"])
        project["physical"]["placements"]["ent-tank"] = placement(None, status="unresolved")
        project["physical"]["routes"]["conn-suction-1"].update(status="unresolved", reason="test: tank unplaced")
        _, _, description = self.describe(project)
        text = narrative_text(description)
        self.assertIn("The design starts with a cylindrical tank TK-101, 0.8 m in diameter with a 1.2 m shell length "
                      "along its axis (assumed dimensions) (its 3D placement is not yet resolved).", text)
        self.assertIn("Flow leaves TK-101 through line L-101 to isolation valve HV-101", text)
        self.assertNotRegex(text, r"vertical|horizontal|\d m high|above the shell base|side outlet")

    def test_unassumed_dimensions_are_not_labelled_assumed(self):
        project = copy.deepcopy(self.document["current"])
        for name in ("diameter", "height", "outlet_height"):
            del project["model"]["entities"]["ent-tank"]["parameters"][name]["assumption_id"]
            project["model"]["entities"]["ent-tank"]["parameters"][name]["source"] = "test datasheet"
        _, _, description = self.describe(project)
        text = narrative_text(description)
        self.assertIn("0.8 m in diameter and 1.2 m high.", text)
        self.assertIn("0.2 m above the shell base, to", text)

    def test_unrelated_edit_keeps_wording_and_advances_source_identity(self):
        _, _, before = self.describe(self.document["current"])
        moved = run(self.document, "move_symbol", {"symbol_id": "sym-level-indicator", "x": 60.0, "y": 140.0,
                                                   "rotation_deg": 0.0})
        out, manifests, after = self.describe(moved["current"])
        self.assertEqual(after["process_narrative"], before["process_narrative"])
        self.assertEqual((after["source_revision"], before["source_revision"]), (1, 0))
        self.assertNotEqual(after["input_digest"], before["input_digest"])
        self.assertEqual(manifests["description"]["input_digest"], after["input_digest"])
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertIn(f'data-source-revision="1" data-input-digest="{after["input_digest"]}"', html)

    def test_html_leads_with_narrative_and_keeps_details_closed_and_linked(self):
        notes = [{"id": "note-1", "target_id": "ent-pump", "text": "Keep <b>me</b>.", "reviewed_revision": 0}]
        out, _, description = self.describe(self.document["current"], manual_notes=notes)
        html = (out / "description.html").read_text(encoding="utf-8")
        page = structure(html)
        self.assertEqual(len(page.details), 1)
        self.assertEqual(page.details[0].get("id"), "item-details")
        self.assertEqual(page.details[0].get("class"), "item-details")
        self.assertNotIn("open", page.details[0], "details start closed")
        roles = [role for role, _ in page.order]
        self.assertEqual(roles[:len(description["process_narrative"])], ["narrative"] * len(description["process_narrative"]))
        self.assertTrue(all(not inside for role, inside in page.order if role == "narrative"))
        self.assertTrue(all(inside for role, inside in page.order if role in ("purpose", "function")))
        self.assertLess(html.index('<div class="notice">'), html.index('data-role="narrative"'))
        self.assertLess(html.index('data-role="narrative"'), html.index('<details id="item-details"'))
        self.assertIn(f'<summary>Item details <span class="count">{len(description["items"])} items</span></summary>',
                      html)
        ids = dict(page.ids)
        for item in description["items"]:
            self.assertTrue(ids.get(anchor(item["semantic_id"])), f"{item['tag']} anchor stays inside the details")
        narrative_links = set()
        for paragraph in description["process_narrative"]:
            narrative_links |= {anchor(r) for r in paragraph["refs"]}
        self.assertLessEqual(narrative_links, set(ids))
        self.assertLessEqual(narrative_links, set(page.hrefs))
        self.assertIn("Keep &lt;b&gt;me&lt;/b&gt;.", html)
        self.assertNotRegex(html, r"https?://|<script|\bsrc=|@import")

    def test_exported_styles_have_no_decorative_vertical_bars(self):
        out, _, _ = self.describe(self.document["current"])
        css = re.search(r"<style>(.*)</style>", (out / "description.html").read_text(encoding="utf-8"), re.S).group(1)
        self.assertNotRegex(css, r"border-(left|right|inline)[\w-]*\s*:")
        self.assertNotRegex(css, r"box-shadow:[^;}]*inset")

    def test_tags_are_escaped_in_the_narrative(self):
        renamed = run(self.document, "rename_entity", {"entity_id": "ent-pump", "tag": "P-1 <A&B>"})
        out, _, description = self.describe(renamed["current"])
        self.assertIn("centrifugal pump P-1 <A&B>", narrative_text(description))
        html = (out / "description.html").read_text(encoding="utf-8")
        narrative = html[html.index('data-role="narrative"'):html.index("<details")]
        self.assertIn("P-1 &lt;A&amp;B&gt;", narrative)
        self.assertNotIn("<A&B>", narrative)


class ComposedNarrativeTests(DraftingCase):
    def setUp(self):
        from tests import test_drafting_definitions as defs
        if not defs.FOUNDATION:
            self.skipTest(defs.NEEDS_FOUNDATION)
        self.defs = defs

    def test_declared_name_and_function_are_quoted_as_authored(self):
        definition = self.defs._definition()
        document = self.defs.insert_composed(fixture_document(), definition, suffix="drum", tag="D-101",
                                             line_tag="L-106")
        out, _, _ = self.build(document["current"])
        text = " ".join(p["text"] for p in self.read_json(out / "description.json")["process_narrative"])
        self.assertIn(f"to D-101, composed equipment from the definition “{definition['name']}” with the declared "
                      f"function “{definition['function']}” (authored, not verified) (its 3D placement is not yet "
                      "resolved).", text)
        self.assertIn("Flow leaves D-101 through line L-106 to isolation valve HV-102", text)
        self.assertNotRegex(text, r"D-101[^.]*(top|side|bottom) (inlet|outlet)")

        # Placed: a definition port's direction does not locate it on the body, so no face words are given.
        placed = run(document, "place_equipment", {"entity_id": "ent-drum", "placement": placement([0.0, 0.0, 0.0])})
        out, _, _ = self.build(placed["current"])
        text = " ".join(p["text"] for p in self.read_json(out / "description.json")["process_narrative"])
        self.assertIn(f"function “{definition['function']}” (authored, not verified). Flow leaves D-101 through line "
                      "L-106 to isolation valve HV-102", text)
        self.assertNotRegex(text, r"D-101[^.]*(top|side|bottom|end) (inlet|outlet)")

    def test_an_upward_port_on_the_shell_side_is_not_called_top(self):
        definition = self.defs._definition()
        # The authored inlet still faces up, but sits beside the shell at its centreline height.
        side = ["=x_in", "=R + 150", "=Hc"]
        definition["geometry"]["ports"]["inlet"]["position"] = side
        nozzle = next(c for c in definition["geometry"]["components"] if c["key"] == "nozzle/inlet")
        nozzle["parts"][0]["port"] = side
        document = self.defs.insert_composed(fixture_document(), definition, suffix="drum", tag="D-101",
                                             line_tag="L-106", placement=placement([0.0, 0.0, 0.0]))
        self.assertEqual(document["current"]["physical"]["placements"]["ent-drum"]["status"], "placed")
        out, _, _ = self.build(document["current"])
        text = " ".join(p["text"] for p in self.read_json(out / "description.json")["process_narrative"])
        self.assertIn("Line L-104 continues from NRV-101 to D-101, composed equipment", text)
        self.assertNotRegex(text, r"D-101[^.]*\b(top|side|bottom|end) (inlet|outlet)")
        self.assertNotIn("entering at", text)


class NarrativeTopologyTests(unittest.TestCase):
    """Branches, merges, loops and separate groups, on hand-edited models."""

    def setUp(self):
        self.project = copy.deepcopy(fixture_document()["current"])
        self.model = self.project["model"]

    def entity(self, eid, kind, tag, ports, **parameters):
        values = parameters or {"length": 0.16, "body_diameter": 0.1}
        self.model["entities"][eid] = {"id": eid, "kind": kind, "tag": tag, "template_version": 1,
                                       "parameters": {k: {"value": v, "unit": "m"} for k, v in values.items()}}
        for name, direction in ports:
            pid = f"port-{eid}-{name}"
            self.model["ports"][pid] = {"id": pid, "entity_id": eid, "name": name, "direction": direction,
                                        "required": True, "nominal_diameter_mm": 50, "service": "liquid",
                                        "assumption_id": "asm-dn50-liquid-service"}

    def connect(self, cid, tag, from_port, to_port):
        self.model["connections"][cid] = {"id": cid, "tag": tag, "from_port_id": from_port, "to_port_id": to_port,
                                          "nominal_diameter_mm": 50, "outside_diameter_m": 0.0603,
                                          "assumption_id": "asm-dn50-pipe"}

    def narrate(self):
        graph = FlowGraph(self.project)
        builder = DescriptionBuilder(self.project, graph, {"items": []}, [])
        paragraphs = process_narrative(builder)
        return paragraphs, "\n".join(p["text"] for p in paragraphs)

    def test_branch_and_merge_are_named(self):
        self.entity("ent-out-b", "boundary", "OUT-102", [("inlet", "in")], size=0.2)
        self.connect("conn-z-branch", "L-201", "port-tank-outlet", "port-ent-out-b-inlet")
        self.entity("ent-tank-b", "tank", "TK-102", [("outlet", "out")], diameter=0.5, height=1.0, outlet_height=0.1)
        self.connect("conn-merge", "L-202", "port-ent-tank-b-outlet", "port-suction-valve-inlet")
        paragraphs, text = self.narrate()
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "path", "branch", "scope"])
        self.assertIn("A second flow path starts with a cylindrical tank TK-102", text)
        self.assertIn("(its 3D placement is not yet resolved)", text)
        self.assertIn("Flow leaves TK-102 through line L-202 to HV-101, where it joins the flow path described "
                      "above.", text)
        self.assertIn("A branch leaves TK-101 through line L-201 from its side outlet to scope boundary OUT-102 (its 3D "
                      "placement is not yet resolved), where the modeled scope ends", text)
        self.assertEqual(mention_order(paragraphs[:1])[:3], ["ent-tank", "ent-level-indicator", "conn-suction-1"])

    def test_recycle_closes_a_loop(self):
        self.connect("conn-recycle", "L-301", "port-check-valve-outlet", "port-suction-valve-inlet")
        paragraphs, text = self.narrate()
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "branch", "scope"])
        self.assertIn("A branch leaves NRV-101 through line L-301 back to HV-101, closing a loop in the modeled flow.",
                      text)

    def test_separate_groups_closed_loops_and_loose_items_are_reported(self):
        self.entity("ent-x-tank", "tank", "TK-900", [("outlet", "out")], diameter=0.5, height=1.0, outlet_height=0.1)
        self.entity("ent-x-out", "boundary", "OUT-900", [("inlet", "in")], size=0.2)
        self.connect("conn-x", "L-900", "port-ent-x-tank-outlet", "port-ent-x-out-inlet")
        self.entity("ent-y1", "isolation_valve", "HV-901", [("inlet", "in"), ("outlet", "out")])
        self.entity("ent-y2", "isolation_valve", "HV-902", [("inlet", "in"), ("outlet", "out")])
        self.connect("conn-y1", "L-901", "port-ent-y1-outlet", "port-ent-y2-inlet")
        self.connect("conn-y2", "L-902", "port-ent-y2-outlet", "port-ent-y1-inlet")
        self.entity("ent-z", "check_valve", "NRV-999", [("inlet", "in"), ("outlet", "out")])
        self.model["entities"]["ent-pressure-indicator"]["observes"]["entity_id"] = "ent-missing"
        paragraphs, text = self.narrate()
        roles = [p["role"] for p in paragraphs]
        self.assertEqual(roles, ["flow", "separate", "separate", "loop", "scope"])
        self.assertIn("A separate group, not connected to the items above, starts with a cylindrical tank TK-900", text)
        self.assertIn("A separate group, not connected to the items above, is read from an isolation valve HV-901 (its "
                      "3D placement is not yet resolved), which lies on a closed loop in the modeled flow.", text)
        self.assertIn("Line L-902 continues from HV-902 back to HV-901, closing a loop in the modeled flow.", text)
        self.assertIn("A check valve NRV-999 (its 3D placement is not yet resolved) is not connected to any process "
                      "line.", text)
        self.assertIn("Indicator PI-101 has no observed item in the model.", text)
        self.assertEqual(paragraphs[0]["role"], "flow")
        self.assertEqual(set(mention_order(paragraphs)),
                         set(self.model["entities"]) | set(self.model["connections"]))

    def test_a_tail_fed_from_a_loop_is_not_called_a_loop(self):
        for eid, tag in (("ent-a-tail", "HV-901"), ("ent-y-cycle", "HV-902"), ("ent-z-cycle", "HV-903")):
            self.entity(eid, "isolation_valve", tag, [("inlet", "in"), ("outlet", "out")])
        # The y->a ID sorts before y->z, so the walk reads the tail first, then the tail line, then the loop.
        self.connect("conn-cycle-1", "L-901", "port-ent-y-cycle-outlet", "port-ent-z-cycle-inlet")
        self.connect("conn-cycle-2", "L-902", "port-ent-z-cycle-outlet", "port-ent-y-cycle-inlet")
        self.connect("conn-cycle-0", "L-903", "port-ent-y-cycle-outlet", "port-ent-a-tail-inlet")
        paragraphs, _ = self.narrate()
        self.assertEqual([p["role"] for p in paragraphs], ["flow", "separate", "loop", "branch", "scope"])
        tail, entry, branch = (p["text"] for p in paragraphs[1:4])
        self.assertEqual(tail, "A separate group, not connected to the items above, is read from an isolation valve "
                               "HV-901 (its 3D placement is not yet resolved), which is fed only by lines described "
                               "below.")
        self.assertEqual(entry, "Another part of the same connected flow is read from an isolation valve HV-902 (its "
                                "3D placement is not yet resolved), which lies on a closed loop in the modeled flow. "
                                "Flow leaves HV-902 through line L-903 to HV-901, where it joins the flow path "
                                "described above.")
        self.assertIn("A branch leaves HV-902 through line L-901 to isolation valve HV-903 (its 3D placement is not "
                      "yet resolved). Line L-902 continues from HV-903 back to HV-902, closing a loop in the modeled "
                      "flow.", branch)
        for paragraph in paragraphs:
            self.assertNotIn("closed loop with no inlet", paragraph["text"])
            self.assertNotIn("following items form", paragraph["text"])

    def test_a_flow_without_a_boundary_says_where_it_ends(self):
        model = self.model
        for cid in ("conn-discharge-3",):
            del model["connections"][cid]
        del model["entities"]["ent-outlet-boundary"]
        del model["ports"]["port-boundary-inlet"]
        _, text = self.narrate()
        self.assertIn("The modeled flow ends at HV-102 with no scope boundary after it", text)
        self.assertIn("No scope boundary is modeled.", text)


if __name__ == "__main__":
    unittest.main()
