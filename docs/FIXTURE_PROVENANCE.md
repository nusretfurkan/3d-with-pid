# Fixture provenance

This note records where the committed test and example fixtures came from. The data values in them are frozen regression data. Only their descriptive metadata fields (`generated_by`, `_source`) were rewritten to remove references to private workspace files. No value, case, expected output or assertion was changed.

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
