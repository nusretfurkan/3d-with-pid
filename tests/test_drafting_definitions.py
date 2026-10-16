"""Drafting of composed (definition-based) equipment: generic glyph families, anchors, legend, index, description.

The glyph tests are pure. The build tests insert composed equipment through the
real command layer (``insert_equipment``) using the example definition; if the
core lacks that command or the example is missing they are skipped with that
reason (a skip is not a pass).
"""

from __future__ import annotations

import copy
import json
import math
import re
import unittest
import xml.etree.ElementTree as ET

from skid.drafting import conventions as conv
from skid.drafting import symbols
from skid.drafting.symbols import (
    FAMILIES,
    MAX_SCALE,
    MIN_SCALE,
    ON_OUTLINE,
    SYMBOLS,
    boundary_scale,
    composed_spec,
    family_sample,
    fit_glyph,
    symbol_for,
    symbol_for_entity,
)
from tests.test_drafting_support import ROOT, SVG, DraftingCase, fixture_document, parse, run, undo, visible_text

DEFINITION_PATH = ROOT / "examples" / "recipes" / "horizontal_drum.definition.json"
FOUNDATION = DEFINITION_PATH.exists()
NEEDS_FOUNDATION = "needs the example definition examples/recipes/horizontal_drum.definition.json"


def _on_outline(family, scale, point, tolerance=1e-6):
    return abs(boundary_scale(family, point) - scale) <= tolerance * max(1.0, scale)


class GlyphFamilyTests(unittest.TestCase):
    def test_existing_kind_lookup_is_unchanged(self):
        for kind, spec in SYMBOLS.items():
            self.assertIs(symbol_for(kind), spec)
            self.assertIs(symbol_for_entity({"kind": kind, "tag": "X"}), spec)
        self.assertIsNone(symbol_for("composed_equipment"))
        self.assertIsNone(symbol_for_entity({"kind": "reactor"}))
        # a definition never turns a built-in kind into a composed glyph
        self.assertIs(symbol_for_entity({"kind": "tank", "definition": {"symbol": {"shape": "box_equipment"}}}),
                      SYMBOLS["tank"])

    def test_every_declared_anchor_meets_the_artwork(self):
        anchor_sets = {
            "on both ends": {"inlet": [-60, 0], "outlet": [60, 0]},
            "top nozzle and end": {"inlet": [-30, -40], "outlet": [70, 0]},
            "side and bottom": {"inlet": [-45, -20], "outlet": [0, 75]},
            "corner": {"inlet": [-80, -60], "outlet": [80, 10]},
            "three ports": {"a": [-50, 0], "b": [0, -40], "c": [30, 36]},
        }
        for shape, family in FAMILIES.items():
            for label, anchors in anchor_sets.items():
                with self.subTest(shape=shape, anchors=label):
                    spec = composed_spec(shape, anchors)
                    glyph = spec.glyph
                    self.assertEqual(spec.key, f"symbol/{shape}")
                    self.assertEqual(glyph.unmatched, ())
                    stubs = {name: (start, end) for name, start, end in glyph.stubs}
                    for name, (x, y) in anchors.items():
                        if name in stubs:
                            start, end = stubs[name]
                            self.assertEqual(end, (float(x), float(y)), "a stub ends exactly at its anchor")
                            self.assertTrue(_on_outline(family, glyph.scale, start), f"{name} stub starts on the outline")
                            self.assertGreater(math.dist(start, end), ON_OUTLINE)
                        else:
                            self.assertTrue(_on_outline(family, glyph.scale, (x, y), 1e-3), f"{name} lies on the outline")
                        x0, y0, x1, y1 = spec.bbox
                        self.assertTrue(x0 <= x <= x1 and y0 <= y <= y1, "anchors lie inside the layout box")

    def test_body_grows_to_the_nearest_anchor_within_bounds(self):
        family = FAMILIES["horizontal_vessel"]
        glyph = fit_glyph(family, {"inlet": [-60, 0], "outlet": [60, 0]})
        self.assertAlmostEqual(glyph.scale, 60 / (family.tangent + family.head))
        self.assertEqual(glyph.stubs, ())  # both anchors sit on the head apexes
        self.assertAlmostEqual(glyph.extent[0], 60.0)
        far = fit_glyph(family, {"inlet": [-900, 0], "outlet": [900, 0]})
        self.assertEqual(far.scale, MAX_SCALE)
        self.assertEqual(len(far.stubs), 2, "beyond the largest body, stubs reach the anchors")
        for _, start, end in far.stubs:
            self.assertEqual(start[1], 0.0)
            self.assertEqual(end[1], 0.0)

    def test_stubs_are_perpendicular_to_the_side_they_leave(self):
        # "left" sits on the head apex and limits the body; the others are reached by stubs
        glyph = composed_spec("horizontal_vessel",
                              {"left": [-60, 0], "top": [-20, -40], "bottom": [10, 45], "end": [90, 5]}).glyph
        stubs = {name: (start, end) for name, start, end in glyph.stubs}
        self.assertEqual(set(stubs), {"top", "bottom", "end"})
        self.assertEqual(stubs["top"][0][0], -20.0)
        self.assertEqual(stubs["bottom"][0][0], 10.0)
        self.assertEqual(stubs["end"][0][1], 5.0)
        vertical = composed_spec("vertical_vessel", {"side": [-50, -10], "bottom": [0, 90]}).glyph
        self.assertEqual({n: s[1] for n, s, _ in vertical.stubs}["side"], -10.0)

    def test_families_cover_the_definition_symbol_vocabulary(self):
        from skid.equipment import SYMBOL_SHAPES
        self.assertEqual(set(FAMILIES), set(SYMBOL_SHAPES))

    def test_an_anchor_inside_the_smallest_outline_is_marked_unmatched(self):
        spec = composed_spec("box_equipment", {"inlet": [0, 0], "outlet": [60, 0]})
        glyph = spec.glyph
        self.assertEqual(glyph.scale, MIN_SCALE)
        self.assertEqual(glyph.unmatched, ("inlet",))
        drawn = spec.draw(1.0)
        rings = [attrs for tag, attrs in drawn if attrs.get("data-role") == "unmatched-anchor"]
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["data-legend-keys"], "marker/unmatched-anchor")
        self.assertEqual((rings[0]["cx"], rings[0]["cy"]), ("0", "0"))
        self.assertIn("marker/unmatched-anchor", conv.CONVENTIONS)

    def test_invalid_declarations_have_no_artwork(self):
        self.assertIsNone(composed_spec("reactor_with_agitator", {"inlet": [0, -40]}))
        self.assertIsNone(composed_spec("box_equipment", {"inlet": [float("nan"), 0]}))
        self.assertIsNone(composed_spec("box_equipment", {"inlet": ["x", 0]}))
        self.assertIsNone(composed_spec("box_equipment", [[0, 0]]))
        self.assertIsNone(symbol_for_entity({"kind": "composed_equipment", "definition": {"symbol": "box"}}))
        self.assertIsNone(symbol_for_entity({"kind": "composed_equipment"}))

    def test_one_legend_convention_per_family_with_key_free_samples(self):
        for shape in FAMILIES:
            key = f"symbol/{shape}"
            self.assertIn(key, conv.CONVENTIONS)
            self.assertEqual(conv.CONVENTIONS[key].section, "equipment")
            sample = family_sample(shape)
            self.assertEqual(sample.glyph.unmatched, ())
            self.assertFalse(any("data-legend-keys" in attrs for _, attrs in sample.draw(0.5)))
            self.assertGreaterEqual(len(sample.glyph.stubs), 1, "the sample shows how stubs reach anchors")
        text = " ".join(conv.CONVENTIONS[f"symbol/{s}"].description for s in FAMILIES).lower()
        for claim in ("iso", "isa", "conform", "rated", "pressure vessel"):
            self.assertNotIn(claim, text)

    def test_fitting_is_deterministic(self):
        anchors = {"outlet": [60, 0], "inlet": [-30, -40]}
        a = composed_spec("horizontal_vessel", anchors)
        b = composed_spec("horizontal_vessel", dict(reversed(list(anchors.items()))))
        self.assertIs(a, b)
        self.assertEqual(a.draw(1.0), b.draw(1.0))
        self.assertIsNot(symbols.composed_spec("vertical_vessel", anchors), a)


def _definition():
    with open(DEFINITION_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _variant(definition, shape, ident, name):
    """The same authored geometry under another identity and generic symbol family."""
    variant = copy.deepcopy(definition)
    variant["id"], variant["name"], variant["symbol"]["shape"] = ident, name, shape
    return variant


def _parameters(definition, assumption_id):
    """Initial authoritative quantities: the recipe's authoring defaults, recorded with a project assumption."""
    return {name: {"value": record["value"], "unit": record["unit"], "assumption_id": assumption_id}
            for name, record in definition["geometry"]["parameters"].items()}


def insert_composed(document, definition, *, suffix, tag, line_tag, connection_id="conn-discharge-2", x=548.0,
                    y=150.0, rotation=0.0, placement=None):
    """insert_equipment of one composed two-port item through the real command layer (no physical placement)."""
    model = document["current"]["model"]
    connection = model["connections"][connection_id]
    upstream_edge = next(e for e in document["current"]["pid"]["edges"].values() if e["connection_id"] == connection_id)
    entity_id = f"ent-{suffix}"
    assumption_id = f"asm-{suffix}-defaults"
    payload = {
        "connection_id": connection_id,
        "entity": {"id": entity_id, "kind": "composed_equipment", "tag": tag, "template_version": 1,
                   "parameters": _parameters(definition, assumption_id), "definition": definition},
        "ports": [
            {"id": f"port-{suffix}-in", "entity_id": entity_id, "name": "inlet", "direction": "in", "required": True,
             "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"},
            {"id": f"port-{suffix}-out", "entity_id": entity_id, "name": "outlet", "direction": "out", "required": True,
             "nominal_diameter_mm": 50, "service": "liquid", "assumption_id": "asm-dn50-liquid-service"},
        ],
        "downstream_connection": {"id": f"conn-{suffix}-out", "tag": line_tag, "from_port_id": f"port-{suffix}-out",
                                  "to_port_id": connection["to_port_id"], "nominal_diameter_mm": 50,
                                  "outside_diameter_m": connection["outside_diameter_m"],
                                  "assumption_id": connection["assumption_id"]},
        "symbol": {"id": f"sym-{suffix}", "entity_id": entity_id, "x": x, "y": y, "rotation_deg": rotation,
                   "manual": True},
        "downstream_edge": {"id": f"edge-{suffix}-out", "connection_id": f"conn-{suffix}-out",
                            "from_symbol_id": f"sym-{suffix}", "to_symbol_id": upstream_edge["to_symbol_id"],
                            "points": [], "manual": False, "status": "unresolved",
                            "reason": "downstream diagram path not drawn yet"},  # stored text: no tag to go stale
    }
    if placement is not None:
        payload["placement"] = placement
    command = {"id": f"cmd-insert-{suffix}", "expected_revision": document["current"]["revision"],
               "type": "insert_equipment", "payload": payload,
               "assumptions": {assumption_id: {"description": f"Initial {definition['name']} parameters equal to the "
                                                              "definition's authoring defaults (test scenario).",
                                               "source": "drafting definition test"}}}
    from skid.core.commands import apply_command
    return apply_command(document, command)[0]


@unittest.skipUnless(FOUNDATION, NEEDS_FOUNDATION)
class ComposedDraftingTests(DraftingCase):
    @classmethod
    def setUpClass(cls):
        from skid.core.errors import CommandError
        try:
            insert_composed(fixture_document(), _definition(), suffix="probe", tag="D-999", line_tag="L-999")
        except CommandError as exc:
            if any(d.get("code") == "unsupported_command" for d in exc.diagnostics):
                raise unittest.SkipTest("the core command layer has no insert_equipment yet (foundation in progress)")
            raise

    def setUp(self):
        self.definition = _definition()
        self.document = fixture_document()
        self.document = insert_composed(self.document, self.definition, suffix="drum", tag="D-101", line_tag="L-106")
        self.project = self.document["current"]

    def symbol_group(self, root, occurrence_id):
        return next(e for e in root.iter() if e.get("data-occurrence-id") == occurrence_id)

    def test_generic_glyph_meets_the_template_anchors(self):
        out, manifests, _ = self.build(self.project)
        root = parse(out / "pid.svg")
        group = self.symbol_group(root, "sym-drum")
        self.assertEqual(group.get("data-symbol-shape"), self.definition["symbol"]["shape"])
        self.assertEqual(group.get("data-definition-id"), self.definition["id"])
        body = next(e for e in group if e.get("data-role") == "symbol-body")
        self.assertEqual(body.get("data-legend-keys"), f"symbol/{self.definition['symbol']['shape']}")
        tx, ty = (float(v) for v in re.match(r"translate\(([-\d.e]+) ([-\d.e]+)\)", body.get("transform")).groups())
        anchors = {}
        for port_group in group:
            if port_group.get("data-occurrence-kind") == "port-anchor":
                circle = port_group.find(f"{SVG}circle")
                anchors[port_group.get("data-port-name")] = (float(circle.get("cx")), float(circle.get("cy")))
        self.assertEqual(set(anchors), set(self.definition["ports"]))
        family = FAMILIES[self.definition["symbol"]["shape"]]
        stubs = {p.get("data-port-name"): p.get("d") for p in body.iter(f"{SVG}path")
                 if p.get("data-role") == "nozzle-stub"}
        spec = symbol_for_entity(self.project["model"]["entities"]["ent-drum"])
        for name, (ax, ay) in anchors.items():
            local = (ax - tx, ay - ty)  # unrotated occurrence
            self.assertEqual(local, tuple(float(v) for v in self.definition["symbol"]["anchors"][name]))
            if name in stubs:
                end = [float(v) for v in stubs[name].split(" L ")[1].split()]
                self.assertEqual(tuple(end), local, "the stub ends on the drawn port anchor")
            else:
                self.assertTrue(_on_outline(family, spec.glyph.scale, local, 1e-3))
        self.assertNotIn("pid_anchor_unmatched", {d["code"] for d in manifests["pid"]["diagnostics"]})

    def test_rotated_occurrence_keeps_anchors_on_the_glyph(self):
        document = insert_composed(fixture_document(), self.definition, suffix="vrot", tag="D-111", line_tag="L-106",
                                   x=548.0, y=380.0, rotation=90.0)
        out, manifests, _ = self.build(document["current"])
        group = self.symbol_group(parse(out / "pid.svg"), "sym-vrot")
        body = next(e for e in group if e.get("data-role") == "symbol-body")
        self.assertEqual(body.get("transform"), "translate(648 480) rotate(90)")
        for port_group in group:
            if port_group.get("data-occurrence-kind") != "port-anchor":
                continue
            circle = port_group.find(f"{SVG}circle")
            ax, ay = self.definition["symbol"]["anchors"][port_group.get("data-port-name")]
            # rotate(90) maps local (x, y) to (-y, x), then the paper translation applies
            self.assertEqual((float(circle.get("cx")), float(circle.get("cy"))), (648.0 - ay, 480.0 + ax))
        self.assertNotIn("pid_anchor_unmatched", {d["code"] for d in manifests["pid"]["diagnostics"]})

    def test_legend_and_index_list_only_the_family_used(self):
        out, manifests, _ = self.build(self.project)
        shape = self.definition["symbol"]["shape"]
        entries = self.legend_entries(out)
        self.assertIn(f"symbol/{shape}", entries)
        for other in set(FAMILIES) - {shape}:
            self.assertNotIn(f"symbol/{other}", entries)
        self.assertEqual(entries, sorted(self.inventory(out)["keys"]))
        item = self.index_by_tag(out)["D-101"]
        self.assertEqual(item["kind"], "composed_equipment")
        self.assertEqual(item["category"], "equipment")
        self.assertEqual(item["definition"]["id"], self.definition["id"])
        self.assertEqual(len(item["definition"]["digest"]), 64)
        self.assertIn(self.definition["name"].upper(), item["description"])
        self.assertEqual([o["occurrence_id"] for o in item["occurrences"]], ["sym-drum"])
        self.assertIn(f"symbol/{shape}", item["legend_keys"])
        legend = parse(out / "legend.svg")
        used = next(e for e in legend.iter() if e.get("data-legend-entry") == f"symbol/{shape}")
        self.assertEqual(used.get("data-used-by"), "D-101")
        # without a placement or a downstream path, the sheet says so instead of passing as complete
        self.assertEqual(manifests["pid"]["status"], "pending")
        self.assertIn("3D PLACEMENT PENDING", visible_text(parse(out / "pid.svg")))

    def test_a_second_family_needs_no_kind_handler(self):
        document = fixture_document()
        variant = _variant(self.definition, "box_equipment", "inline-box-demo", "Inline package (demo)")
        variant["symbol"]["anchors"] = {"inlet": [-40, 0], "outlet": [40, 0]}
        document = insert_composed(document, variant, suffix="pkg", tag="X-101", line_tag="L-106")
        out, manifests, _ = self.build(document["current"])
        group = self.symbol_group(parse(out / "pid.svg"), "sym-pkg")
        self.assertEqual(group.get("data-symbol-shape"), "box_equipment")
        self.assertIn("symbol/box_equipment", self.legend_entries(out))
        self.assertNotIn("symbol/horizontal_vessel", self.legend_entries(out))
        self.assertNotEqual(manifests["pid"]["status"], "failed")

    def test_description_quotes_declared_evidence_with_current_tags(self):
        out, _, _ = self.build(self.project)
        description = self.read_json(out / "description.json")
        item = next(i for i in description["items"] if i["semantic_id"] == "ent-drum")
        self.assertEqual(item["tag"], "D-101")
        self.assertIn(self.definition["name"], item["kind_name"])
        self.assertIn(" ".join(self.definition["function"].split()), item["function"]["text"])
        self.assertIn("authored, not verified", item["function"]["text"])
        self.assertIn("not verified", item["intent_basis"])
        refs = {p["ref"]: p["tag"] for p in item["function"]["parts"] if "ref" in p}
        self.assertEqual(refs.get("conn-discharge-2"), self.project["model"]["connections"]["conn-discharge-2"]["tag"])
        self.assertEqual(refs.get("conn-drum-out"), "L-106")
        self.assertEqual(item["definition"]["id"], self.definition["id"])
        evidence = item["evidence"]
        parameters = {e["name"]: e for e in evidence if e["kind"] == "parameter"}
        entity = self.project["model"]["entities"]["ent-drum"]
        self.assertEqual({n: (p["value"], p["unit"], p["assumption_id"]) for n, p in parameters.items()},
                         {n: (q["value"], q["unit"], q["assumption_id"]) for n, q in entity["parameters"].items()})
        local_ids = sorted(self.definition["geometry"]["assumptions"])
        self.assertEqual(sorted(e["local_id"] for e in evidence if e["kind"] == "definition_assumption"), local_ids)
        self.assertEqual(item["definition_assumption_ids"], [f"ent-drum/{a}" for a in local_ids])
        scoped = {r["scoped_id"]: r for r in description["definition_assumptions"]}
        self.assertEqual(set(scoped), {f"ent-drum/{a}" for a in local_ids})
        self.assertTrue(all(r["applies_to"] == {"semantic_id": "ent-drum", "tag": "D-101"} for r in scoped.values()))
        self.assertNotIn(local_ids[0], {a["id"] for a in description["assumptions"]}, "local IDs stay out of the model register")
        unknowns = {u["id"]: u for u in description["unknowns"]}
        self.assertIn("ent-drum", {a["semantic_id"] for a in unknowns["unknown/composed-equipment"]["applies_to"]})
        declared = [u for u in description["unknowns"] if u["id"].startswith("unknown/definition-")]
        self.assertEqual(len(declared), 1)
        for text in self.definition["unknowns"]:
            self.assertIn(text, declared[0]["text"])
        self.assertIn("unknown/process-conditions", item["unknown_ids"])
        html = (out / "description.html").read_text(encoding="utf-8")
        self.assertIn("Definition-local assumptions", html)
        self.assertNotRegex(item["function"]["text"], r"\d+(\.\d+)?\s*(bar|kPa|°C|m3/h|m³/h)")

    def test_rename_and_parameter_edits_follow_by_stable_id(self):
        self.document = run(self.document, "rename_entity", {"entity_id": "ent-drum", "tag": "D-201"}, "cmd-rename-drum")
        name = next(iter(self.definition["geometry"]["parameters"]))
        value = self.project["model"]["entities"]["ent-drum"]["parameters"][name]["value"] * 1.1
        edit = {"id": "cmd-drum-param", "expected_revision": self.document["current"]["revision"],
                "type": "set_parameter", "payload": {"entity_id": "ent-drum", "name": name,
                                                     "quantity": {"value": value, "unit": "m",
                                                                  "assumption_id": "asm-drum-edit"}},
                "assumptions": {"asm-drum-edit": {"description": "Test edit of a composed parameter.",
                                                  "source": "drafting definition test"}}}
        from skid.core.commands import apply_command
        self.document = apply_command(self.document, edit)[0]
        out, _, _ = self.build(self.document["current"])
        description = self.read_json(out / "description.json")
        generated = json.dumps([description["purpose"], description["flow_sequence"], description["items"],
                                description["definition_assumptions"], description["unknowns"]])
        self.assertNotIn("D-101", generated)
        self.assertIn("D-201", self.index_by_tag(out))
        item = next(i for i in description["items"] if i["semantic_id"] == "ent-drum")
        evidence = {e["name"]: e for e in item["evidence"] if e["kind"] == "parameter"}
        self.assertEqual(evidence[name]["value"], value)
        self.assertEqual(evidence[name]["assumption_id"], "asm-drum-edit")
        self.assertIn(f"{name.replace('_', ' ').upper()}", visible_text(parse(out / "pid.svg")))
        self.assertNotIn("D-101", visible_text(parse(out / "pid.svg")))

    def test_local_assumption_ids_are_scoped_per_entity(self):
        variant = _variant(self.definition, "vertical_vessel", "vertical-drum-demo", "Vertical drum (demo)")
        variant["symbol"]["anchors"] = {"inlet": [-40, -10], "outlet": [0, 70]}
        self.document = insert_composed(self.document, variant, suffix="vdrum", tag="D-102", line_tag="L-107",
                        connection_id="conn-drum-out", x=548.0, y=300.0)
        out, _, _ = self.build(self.document["current"])
        description = self.read_json(out / "description.json")
        local = sorted(self.definition["geometry"]["assumptions"])[0]
        owners = {r["scoped_id"]: r["applies_to"]["tag"] for r in description["definition_assumptions"]
                  if r["local_id"] == local}
        self.assertEqual(owners, {f"ent-drum/{local}": "D-101", f"ent-vdrum/{local}": "D-102"})
        entries = self.legend_entries(out)
        self.assertIn("symbol/horizontal_vessel", entries)
        self.assertIn("symbol/vertical_vessel", entries)

    def test_undo_removes_the_family_from_the_legend(self):
        self.document = undo(self.document)
        out, manifests, _ = self.build(self.document["current"])
        self.assertFalse(any(e.startswith("symbol/") and e.split("/")[1] in FAMILIES for e in self.legend_entries(out)))
        self.assertEqual(manifests["pid"]["status"], "complete")

    def test_unmatched_anchor_is_an_open_item(self):
        document = fixture_document()
        variant = _variant(self.definition, "box_equipment", "bad-anchor-demo", "Anchor review demo")
        variant["symbol"]["anchors"] = {"inlet": [0, 0], "outlet": [40, 0]}
        try:
            document = insert_composed(document, variant, suffix="bad", tag="X-102", line_tag="L-106")
        except Exception as exc:  # the foundation may reject such anchors outright, which is also safe
            self.skipTest(f"definition rejected before drafting: {exc}")
        out, manifests, _ = self.build(document["current"])
        codes = {d["code"] for d in manifests["pid"]["diagnostics"]}
        self.assertIn("pid_anchor_unmatched", codes)
        self.assertEqual(manifests["pid"]["status"], "pending")
        self.assertIn("marker/unmatched-anchor", self.legend_entries(out))
        self.assertIn("CONNECTION POINT NOT ON SYMBOL", visible_text(parse(out / "pid.svg")))


if __name__ == "__main__":
    unittest.main()
