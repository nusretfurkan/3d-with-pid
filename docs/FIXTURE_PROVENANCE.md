# Fixture provenance

This note records where the committed test and example fixtures came from. The data values in them are frozen regression data. In the fixtures that have them, only the descriptive metadata fields (`generated_by`, `_source`) were rewritten to remove references to private workspace files. `tests/fixtures/legacy_geometry_recipe.json` and `tests/fixtures/creation-r3.json` are unmodified byte-for-byte copies. No value, case, expected output or assertion was changed.

## `examples/pump_skid.json`: synthetic pump skid

The prototype pump-skid document. Process order: TK-101 → HV-101 → P-101 → NRV-101 → HV-102 → OUT-101, with LI-101 and PI-101 as conceptual indications. `examples/make_fixture.py` builds it deterministically and is included; regenerate it with `python3 -m examples.make_fixture`.

Every dimension, placement and route is an illustrative prototype assumption, recorded with an ID in the document's assumption map. Several assumption `source` strings refer to `docs/PUMP_SKID.md` prototype assumptions. None of it is vendor, sizing or design data.

## `tests/fixtures/pipe-segments.json`: route-edit parity cases

These are 34 fixed cases (schema `skid.route_edit.parity/1`) shared by the Python tests (`tests/test_route_edit.py`) and the browser unit test (`tests/browser/unit/pipe-sections.test.mjs`). They prove that the browser's straight-section move gives exactly the same result as `skid/route_edit.py` and `skid/clearance.py`: points compare with `===`, and refusals compare by error code.

- **How they were made:** a development script computed them from the Python implementation; it produced 25 cases. A later review step added 9 regression cases (minimum-offset boundaries and tiny offsets) and the `min_offset_m` constant, after first confirming that the original 25 still recomputed unchanged.
- **Generator not included:** neither generator script is part of this repository. Instead, `tests/test_route_edit.py` checks that the committed fixture still equals what the current Python module computes.
- **Cases from real route geometry:** the cases named `current47_*` and `r48_*` use the L-105 (`conn-discharge-3`) route and C-101 body boxes from the revision-47 and revision-48 project states described below. All other cases use small synthetic geometry.

## `tests/fixtures/pipe-segments-r47.json`: owner-edited revision 47

This is a complete project document at revision 47, with empty undo/redo history. It is the project state in which the L-105 pipe entered the C-101 vessel before reaching its flange; the pipe-section work was tested and repaired against it. It is kept byte for byte (SHA-256 `16e9ad103dc3332480326eba5c2f1e7dca432304fcf39abdafe1c8b5d3c4d8f2`), and its values are not altered.

**It is not purely synthetic.** It started from the synthetic pump skid above and was then edited through the application by its owner:

- Two web-inspector placements of C-101, recorded as `local web workspace edit` at 2026-10-14T23:31:34Z (against revision 41) and 2026-10-15T14:57:33Z (against revision 44). Both are described as manual prototype layout choices, not verified installation positions.
- Seven assumptions from three engineering-plan proposals: the pump motor length, the C-101 column envelope, layout and inlet, the boundary relocation, the L-105 riser route and the L-106 straight route. Each proposal was produced by `claude-opus-5-5` through the local Claude Code CLI and reviewed before apply. Their `source` strings say: "proposed value, reviewed before apply; not a vendor or design value".
- Four guided reroutes of L-105 and L-106 by the local deterministic rerouting helper (`skid.interactive.rerouting`), timestamped between 2026-10-14T23:32Z and 2026-10-15T14:58Z.

The document contains no names, e-mail addresses or file-system paths. Including it in this candidate is an explicit publication decision for the owner, not a claim that it is synthetic. If it is not approved, replace it with a reproducible synthetic derivative and keep the tests' assertions.

## `tests/browser/unit/fixtures/real-questions-8.json`: captured planner questions

This holds the fields the follow-up UI uses, copied unchanged from one real engineering-planner reply captured on 2026-10-14:

- **The request:** the planner was asked what information is missing to size TK-101 and P-101.
- **What it ran against:** revision 2 of the synthetic pump-skid example, with the same entities and assumptions as `examples/pump_skid.json`.
- **The reply:** status `questions` with 8 questions. It was produced by `claude-opus-5-5` through the local Claude Code CLI on a Claude Max subscription. Effort was requested as `xhigh`, which was the setting at that time; the app now requests `medium`.

The original capture and its run record are not included. `tests/browser/fake_planner_service.py`, `tests/browser/engineering.e2e.mjs` and `tests/browser/unit/plan-model.test.mjs` use it as realistic UI input. **The questions and reasons are model output kept as test data; they are not independent engineering advice.**

## `tests/fixtures/legacy_geometry_recipe.json`: legacy geometry snapshot

This is the `geometry.recipe.json` that the geometry generator (`skid.geometry` 0.2.0, schema `skid.geometry.snapshot-recipe/1`) wrote on 2026-10-14 for revision 0 of the synthetic pump skid above (`project-pump-skid`, input digest `64f83f2f…7a68`). It is kept byte for byte: 95,797 bytes, SHA-256 `e24b43cd70d73c7b0fc65bb411862968e56552344039f61428ee43ea94dcdb15`.

- **Why it is kept:** it predates the `recipe_sha256` content digest. `tests/test_cad_snapshot.py` checks that a rebuild of this real earlier snapshot is refused with the actionable "legacy schema" error.
- **What it contains:** deterministic generator output only. The assumption texts repeat the fixture's own prototype assumptions, and the `rcp-*` texts come from the generator's recipes. No language model was involved. The `@` characters are generator version suffixes (`tank@1`) and the rotation formula `Rz @ Ry @ Rx`.
- **Privacy check (2026-10-16):** no file-system paths, e-mail addresses, user or account names, session data, credentials, URLs or local ports.

## `tests/fixtures/creation-r3.json`: actual empty-start creation, revision 3

This is the sealed `snapshot.json` of the build `b20261016T073800-r3-7dd44c9c`, written on 2026-10-16 during an acceptance run of empty-project creation in the local application. The run used a separate test project, `project-root-actual-creation` ("Water Transfer Skid"); it is not a user's project. The copy is byte for byte: 68,840 bytes, SHA-256 `96b5730778bd4d16019d116d4e74e937a100a6b189298c483682553ab8dc5228`. It is a complete project document at revision 3 with empty undo/redo history.

- **What it shows:** a pump flow (TK-101 → HV-101 → HV-102 → P-101 → OUT-101), plus HX-101 added as a draft shell-and-tube exchanger with all four ports deliberately unconnected. `tests/test_creation_title.py` uses it to check that the drawing title no longer joins HX-101 to the pump flow.
- **It contains captured model output.** Three engineering-plan proposals (`prop-52cd46e497d9`, `prop-a1ae86617e3a`, `prop-9aab196f94c2`) were produced by `claude-opus-5-5` through the local Claude Code CLI, with effort requested as `xhigh` (the setting at that time; the app now requests `medium`). Each was reviewed before it was applied, and its `source` strings say so. The HX-101 definition and its assumption texts are model-written. Route assumptions come from the local deterministic rerouting helper. **Model output kept as test data is not engineering advice, and no dimension in it is a design value.**
- **It cites public web sources.** The HX-101 proposal used the app's optional text research (`res-9aab196f94c2`, 4 retained sources). The assumption `source` strings and one description name and link these pages: alfalaval.com (shell-and-tube product page), bendel.com (fabrication article), wermac.org (heat exchanger part 5) and engineersedge.com (shell-and-tube review). The fixture holds only URLs and short model-written summaries attributed to them. No page content was stored, and the summaries were not compared word for word with the pages.
- **Privacy check (2026-10-16):** no file-system paths, e-mail addresses, user or account names, session or authentication data, credentials or local ports. The only reference to a person is the generic model-written phrase "Water is the user's description only". Raw model transcripts, the run records and the research brief are not included.

**Owner decisions still open.** Publishing either file is the owner's decision. For `creation-r3.json` this covers the captured model output and the third-party URLs and names. Neither file is approved for publication: they are included only in the private candidate being prepared. The project's own license is also undecided. If either file is not approved, replace it with a reproducible derivative and keep the tests' assertions. No professional or engineering review of either file has taken place.

Other tests replay raw, real model streams. Those streams stay private and are not copied into this repository; the tests that need them skip in a clean checkout.
