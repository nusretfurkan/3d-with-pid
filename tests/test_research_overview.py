"""Over-limit research overviews: a marked, derived overview replaces a summary whose only problem is its length.

The bridge cases run the real subprocess bridge against the LABELED FAKE ``claude`` executable
(``tests.test_research_fakes.FakeResearchCli``); nothing calls Claude or the web. The bridge cases import only
names that existed before the fix, so against the earlier code they fail on the refused research itself
(``research_invalid_brief``), not on a missing name.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from skid.research import (TEXT_EVIDENCE_WARNING, ResearchError, compose_brief, research_prompt, validate_brief,
                           validate_research_result)
from tests.test_research_fakes import (MAKER_QUERY, MAKER_URL, OTHER_URL, REQUEST, TECH_URL, FakeResearchCli, links,
                                       output, research_events, search, standard_searches)

LIMIT = 1500
NOTICE = "Derived overview: the researcher's "

# Realistic overview prose (FAKE content), the kind of extended answer that overran the limit in practice.
PROSE = [
    "FAKE: a shell-and-tube heat exchanger consists of a long cylindrical shell that encloses a bundle of tubes "
    "held between two tubesheets, with a channel head bolted to each end of the shell.",
    "One fluid flows inside the tubes (the tube side) while the other flows around the tubes inside the shell "
    "(the shell side); the tube walls and tubesheets keep the two circuits separate so the fluids never mix.",
    "Tube-side inlet and outlet nozzles sit on the channel heads, and shell-side inlet and outlet nozzles sit on "
    "the shell itself, usually near opposite ends so that the shell-side fluid crosses the whole bundle.",
    "Transverse baffles inside the shell direct the shell-side flow back and forth across the tubes, and tie rods "
    "with spacers hold the baffles in position along the bundle.",
    "Common subtypes differ mainly in how they accommodate thermal expansion: fixed-tubesheet units are simplest, "
    "U-tube units let the bundle grow freely, and floating-head units allow the bundle to be withdrawn for "
    "cleaning.",
    "Units are commonly mounted horizontally on two saddle supports, although vertical installations on lugs or "
    "a skirt also exist where floor space is limited.",
    "The searches did not establish nozzle sizes, flange ratings, materials, tube counts or design pressures, so "
    "those remain unknown and any dimensions used for the model are illustrative.",
]


def prose(length):
    """Whole FAKE sentences in two-sentence paragraphs, at least ``length`` characters."""
    sentences, index = [], 0
    while len(" ".join(sentences)) < length:
        sentences.append(PROSE[index % len(PROSE)])
        index += 1
    paragraphs = [" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2)]
    return "\n".join(paragraphs)


def long_features(count=16):
    """``count`` complete FAKE features of 280-300 characters, each citing observed sources."""
    features = []
    for n in range(count):
        words = (f"FAKE sourced feature {n + 1}: the cited page describes connection layout, internal arrangement "
                 "and circuit separation for this exchanger type, qualified as typical of common designs only and "
                 "not of every variant offered, with sizes, ratings and materials left to the unknowns and to the "
                 "illustrative assumptions").split()
        while len(" ".join(words)) > 279:
            words.pop()
        features.append({"text": " ".join(words) + ".", "source_ids": ["S1", "S2"] if n % 3 == 0 else ["S1"]})
    return features


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def evidence_for(searches):
    """The observed ``{url: title}`` evidence the FAKE searches yield."""
    observed = {}
    for spec in searches:
        for link in spec["links"]:
            observed.setdefault(link["url"], link["title"])
    return observed


class OverviewCase(unittest.TestCase):
    def research(self, payload, searches=None, request=REQUEST):
        searches = standard_searches() if searches is None else searches
        cli = FakeResearchCli(self, events=research_events(payload, copy.deepcopy(searches)))
        return cli.researcher().run(request), cli

    def refused(self, payload, code="research_invalid_brief", searches=None, request=REQUEST):
        searches = standard_searches() if searches is None else searches
        cli = FakeResearchCli(self, events=research_events(payload, copy.deepcopy(searches)))
        with self.assertRaises(ResearchError) as caught:
            cli.researcher().run(request)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        self.assertIs(caught.exception.metadata.get("verified"), False)
        self.assertNotIn("overview_replaced", caught.exception.metadata)
        self.assertEqual(len(cli.calls()), 2)  # auth check and the one research run: no retry
        return caught.exception

    def assertDerived(self, reply, payload, cli, searches=None, request=REQUEST):
        """The reply keeps every composed field but the summary, which is the marked derived overview."""
        searches = standard_searches() if searches is None else searches
        brief, metadata = reply["brief"], reply["metadata"]
        composed, unverified, problems = compose_brief(request, payload, evidence_for(searches))
        self.assertEqual((unverified, problems), ([], []))
        self.assertGreater(len(composed["summary"]), LIMIT)
        self.assertEqual({k: v for k, v in brief.items() if k != "summary"},
                         {k: v for k, v in composed.items() if k != "summary"})  # sources..warnings unchanged
        summary = brief["summary"]
        self.assertLessEqual(len(summary), LIMIT)
        self.assertTrue(summary.startswith(f"{NOTICE}{len(composed['summary'])}-character overview exceeded the "
                                           f"{LIMIT}-character limit and is not shown"), summary)
        self.assertIn("this is not a shortened copy of it", summary)
        total = len(composed["features"])
        self.assertIn(f"All {total} sourced features, every source observation, the assumptions and the unknowns "
                      "are listed in full in their own sections.", summary)
        notice, *items = summary.split("\n")
        shown = metadata["overview_replaced"]["features_shown"]
        self.assertIn(f"The first {shown} of {total} sourced features follow in full", notice)
        self.assertGreaterEqual(shown, 1)
        self.assertEqual(items, [f"- {f['text']} [{', '.join(f['source_ids'])}]"
                                 for f in composed["features"][:shown]])  # whole features, in order
        for sentence in PROSE:  # the replaced prose is not reused, even partly
            self.assertNotIn(sentence[:60], summary)
        self.assertEqual(metadata["overview_replaced"],
                         {"reason": "summary_over_limit", "original_chars": len(composed["summary"]),
                          "limit": LIMIT, "features_shown": shown, "features_total": total})
        self.assertTrue(metadata["verified"])
        self.assertEqual(validate_brief(brief, metadata["observed_urls"]), [])
        self.assertEqual(validate_research_result(reply, request), [])
        self.assertEqual(metadata["output_sha256"], sha256(canonical(payload)))  # the raw received object
        self.assertEqual(metadata["brief_sha256"], sha256(canonical(brief)))  # the final bounded brief
        self.assertEqual(metadata["source_urls"], [s["url"] for s in composed["sources"]])
        self.assertEqual(metadata["observed_urls"], list(evidence_for(searches)))
        self.assertEqual(len(cli.calls()), 2)  # auth check and the one research run: no retry, no extra search


class AcceptedTests(OverviewCase):
    def test_a_summary_at_the_limit_is_kept_byte_identical(self):
        for length in (1, 700, LIMIT - 1, LIMIT):
            with self.subTest(length=length):
                text = prose(length)[:length].rstrip() if length > 1 else "x"
                text = text + "x" * (length - len(text))
                payload = output(summary=text)
                reply, cli = self.research(payload)
                self.assertEqual(reply["brief"]["summary"], text)
                self.assertNotIn("overview_replaced", reply["metadata"])
                self.assertEqual(validate_research_result(reply, REQUEST), [])

    def test_one_character_over_the_limit_gets_a_marked_derived_overview(self):
        payload = output(summary="x" * (LIMIT + 1))
        reply, cli = self.research(payload)
        self.assertDerived(reply, payload, cli)
        self.assertEqual(reply["metadata"]["overview_replaced"]["features_shown"], 2)  # both short features fit

    def test_realistic_extended_overviews_pass_with_the_first_whole_features_that_fit(self):
        for length in (2000, 3000, 4000):
            for features in (output()["features"], long_features(16)):
                with self.subTest(length=length, features=len(features)):
                    payload = output(summary=prose(length), features=features)
                    reply, cli = self.research(payload)
                    self.assertDerived(reply, payload, cli)
                    shown = reply["metadata"]["overview_replaced"]["features_shown"]
                    if len(features) == 16:
                        self.assertLess(shown, 16)  # only whole features that fit; the rest stay in features
                        self.assertNotIn(features[shown]["text"], reply["brief"]["summary"])

    def test_sources_observations_qualifications_and_warnings_stay_complete(self):
        maker, tech = output()["sources"]
        sources = [dict(maker, observations=["FAKE: " + "o" * 290, "FAKE: shell side only, per S1."]),
                   dict(tech, observations=["FAKE: channels carry the tube-side fluid; not all designs."])]
        assumptions = [f"FAKE assumption {n}: illustrative value, not from a source." for n in range(12)]
        unknowns = [f"FAKE unknown {n}: " + "u" * 270 for n in range(12)]
        for warnings in ([], [f"FAKE warning {n}: " + "w" * 270 for n in range(7)]):
            with self.subTest(warnings=len(warnings)):
                payload = output(summary=prose(3000), sources=sources, features=long_features(16),
                                 assumptions=assumptions, unknowns=unknowns, warnings=warnings)
                reply, cli = self.research(payload)
                self.assertDerived(reply, payload, cli)
                brief = reply["brief"]
                self.assertEqual(brief["warnings"], [TEXT_EVIDENCE_WARNING, *warnings])
                self.assertEqual(brief["unknowns"], unknowns)
                self.assertEqual(brief["assumptions"], assumptions)
                self.assertEqual([s["observations"] for s in brief["sources"]], [s["observations"] for s in sources])
                self.assertEqual(brief["features"], long_features(16))


class RefusedTests(OverviewCase):
    def test_an_over_limit_summary_with_unsafe_text_anywhere_is_refused(self):
        head = prose(2000)
        for name, text in {"control in the excess tail": head + " tail\x07end",
                           "NUL in the excess tail": head + " tail\x00end",
                           "bidi override in the excess tail": head + " tail‮end",
                           "bidi isolate early": "FAKE ⁦" + head,
                           "format character in the excess tail": head + " tail⁠end",
                           "private-use in the excess tail": head + " tailend"}.items():
            with self.subTest(name):
                error = self.refused(output(summary=text))
                self.assertIn("longer than 1500", error.message)
                self.assertIn("control or format character", error.message)

    def test_an_over_limit_summary_with_any_other_problem_is_refused(self):
        maker, tech = output()["sources"]
        long = prose(2500)
        cases = {
            "summary of the wrong type": (output(summary=["FAKE", long]), "research_invalid_brief",
                                          "output.summary must be a string"),
            "another over-limit field": (output(summary=long, features=[{"text": "f" * 301, "source_ids": ["S1"]}]),
                                         "research_invalid_brief", "features[0].text is longer than 300"),
            "over-limit equipment type": (output(summary=long, equipment_type="t" * 81), "research_invalid_brief",
                                          "equipment_type is longer than 80"),
            "unknown source id": (output(summary=long, features=[{"text": "x", "source_ids": ["S9"]}]),
                                  "research_invalid_brief", "unknown source 'S9'"),
            "too many unknowns": (output(summary=long, unknowns=[f"u{n}" for n in range(13)]),
                                  "research_invalid_brief", "0 to 12 items"),
            "bidi in a feature": (output(summary=long, features=[{"text": "a‮b", "source_ids": ["S1"]}]),
                                  "research_invalid_brief", "U+202E"),
            "duplicate source id": (output(summary=long, sources=[maker, dict(tech, id="S1")]),
                                    "research_invalid_brief", "is not unique"),
            "no sources": (output(summary=long, sources=[], features=[{"text": "x", "source_ids": ["S1"]}]),
                           "research_invalid_brief", "1 to 8 items"),
            "extra output field": (dict(output(summary=long), images=["hx.png"]), "research_invalid_brief",
                                   "exactly the fields"),
            "unobserved URL": (output(summary=long, sources=[maker, dict(tech, url="https://www.forged.com/x")]),
                               "research_unverified_source", "forged.com"),
            "private URL": (output(summary=long, sources=[maker, dict(tech, url="http://192.168.1.10/hx")]),
                            "research_unverified_source", "192.168.1.10"),
        }
        for name, (payload, code, fragment) in cases.items():
            with self.subTest(name):
                self.assertIn(fragment, self.refused(payload, code).message)

    def test_an_over_size_composed_brief_is_refused_before_any_replacement(self):
        # Raw output under its 64 KiB cap; observed titles and the request push the composed brief over the
        # 64 KiB brief cap. Without the original size check the derived overview would bring it back under.
        wide = "\U0001d538"
        urls = [f"https://www.fake-maker-{n}.com/hx" for n in range(1, 9)]
        searches = [search("toolu_fake_search_1", MAKER_QUERY, links(*[(url, wide * 200) for url in urls]))]
        payload = output(summary=wide * 4000, sources=[
            {"id": f"S{n}", "url": url, "observations": [wide * 300] * 4} for n, url in enumerate(urls, 1)])
        request = wide * 2000
        composed, unverified, problems = compose_brief(request, payload, evidence_for(searches))
        self.assertEqual((unverified, problems), ([], []))
        self.assertLessEqual(len(canonical(payload)), 64 << 10)
        self.assertGreater(len(json.dumps(composed, ensure_ascii=False, separators=(",", ":")).encode()), 64 << 10)
        self.assertEqual(validate_brief(composed, evidence_for(searches)),
                         ["brief.summary is longer than 1500 characters"])  # the size check is skipped there
        smaller = dict(composed, summary="Short overview.")
        self.assertEqual(validate_brief(smaller, evidence_for(searches)), [])  # a replacement alone would pass
        error = self.refused(payload, searches=searches, request=request)
        self.assertIn("more than 65536 bytes", error.message)
        # The same output with a short request and titles fits, and is accepted with a derived overview.
        small = [search("toolu_fake_search_1", MAKER_QUERY, links(*[(url, "FAKE title") for url in urls]))]
        reply, cli = self.research(payload, searches=small)
        self.assertDerived(reply, payload, cli, searches=small)

    def test_raw_output_over_its_byte_limit_is_still_refused(self):
        self.refused(output(summary="x" * (70 << 10)), "research_output_limit")

    def test_a_lone_surrogate_in_the_raw_output_is_refused_as_malformed(self):
        for summary in (prose(2000) + " tail\ud800end", "short\udc00"):
            with self.subTest(chars=len(summary)):
                self.assertIn("lone surrogate", self.refused(output(summary=summary),
                                                             "research_malformed_output").message)

    def test_model_and_metadata_violations_still_refuse_an_over_limit_summary(self):
        events = research_events(output(summary=prose(3000)))
        events[0]["model"] = "claude-sonnet-5-5"
        cli = FakeResearchCli(self, events=events)
        with self.assertRaises(ResearchError) as caught:
            cli.researcher().run(REQUEST)
        self.assertEqual(caught.exception.code, "research_model_mismatch")
        events = research_events(output(summary=prose(3000)), usage_models=["claude-opus-5-5", "claude-sonnet-5-5"],
                                 retrieval_requests=None)
        cli = FakeResearchCli(self, events=events)
        with self.assertRaises(ResearchError) as caught:
            cli.researcher().run(REQUEST)
        self.assertEqual(caught.exception.code, "research_model_mismatch")


class StrictnessTests(unittest.TestCase):
    """The public composer, validator and result check stay strict; only ``_verify`` replaces an overview."""

    def test_public_compose_and_validate_still_refuse_an_over_limit_summary(self):
        evidence = {MAKER_URL: "maker", TECH_URL: "tech", OTHER_URL: "other"}
        composed, _, problems = compose_brief(REQUEST, output(summary="x" * (LIMIT + 1)), evidence)
        self.assertEqual(problems, [])
        self.assertEqual(len(composed["summary"]), LIMIT + 1)  # never shortened by the composer
        self.assertEqual(validate_brief(composed, evidence), ["brief.summary is longer than 1500 characters"])

    def test_the_helper_refuses_untrimmed_or_unsafe_full_summaries_and_other_problems(self):
        from skid.research.researcher import _bound_overview
        evidence = {MAKER_URL: "maker", TECH_URL: "tech"}
        composed, _, _ = compose_brief(REQUEST, output(), evidence)
        long = prose(2000)
        for name, summary in {"leading whitespace": " " + long, "trailing whitespace": long + "\n",
                              "tab": long.replace(" ", "\t", 1), "surrogate": long + "\ud800",
                              "line separator": long + " x", "bidi": long + "‮"}.items():
            with self.subTest(name):
                value = dict(composed, summary=summary)
                problems = validate_brief(value, evidence)
                self.assertEqual(problems, ["brief.summary is longer than 1500 characters"])
                kept, problems, replacement = _bound_overview(value, evidence, problems)
                self.assertIs(kept, value)
                self.assertIsNone(replacement)
                self.assertEqual(len(problems), 2, problems)
        value = dict(composed, summary=long, unknowns=["u" * 301])
        problems = validate_brief(value, evidence)
        self.assertEqual(_bound_overview(value, evidence, problems), (value, problems, None))
        value = dict(composed, summary=long)
        unobserved = {MAKER_URL: "maker"}
        problems = validate_brief(value, unobserved)
        self.assertEqual(_bound_overview(value, unobserved, problems), (value, problems, None))

    def test_derived_overview_is_deterministic_bounded_and_never_cuts_a_feature(self):
        from skid.research.brief import derived_overview
        for count in (1, 2, 5, 16):
            features = long_features(count)
            text, shown = derived_overview(4000, features)
            self.assertEqual(derived_overview(4000, copy.deepcopy(features)), (text, shown))
            self.assertLessEqual(len(text), LIMIT)
            self.assertGreaterEqual(shown, 1)
            lines = text.split("\n")[1:]
            self.assertEqual(lines, [f"- {f['text']} [{', '.join(f['source_ids'])}]" for f in features[:shown]])
            if shown < count:  # the next whole feature would not have fit
                feature = features[shown]
                longer = (text.replace(f"first {shown} of", f"first {shown + 1} of", 1)
                          + f"\n- {feature['text']} [{', '.join(feature['source_ids'])}]")
                self.assertGreater(len(longer), LIMIT)

    def test_prompt_asks_for_a_short_overview_with_details_in_sourced_fields(self):
        flat = " ".join(research_prompt("shell-and-tube heat exchanger").split())
        self.assertIn("summary: 2 to 4 sentences, at most 700 characters (hard limit 1500)", flat)
        self.assertIn("Keep the summary a short overview: 2 to 4 sentences, at most 700 characters. Put every detail "
                      "in the sourced features and observations, and every gap in unknowns, not in the summary.", flat)


if __name__ == "__main__":
    unittest.main()
