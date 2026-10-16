# Pump-skid prototype basis and milestone behaviours

This page is the current, portable statement of the prototype basis behind the reference fixture `examples/pump_skid.json` (written by `examples/make_fixture.py`). It is cited by name in some assumption `source` strings. The fixture is unchanged; this page explains those citations. For a hands-on walkthrough of the same fixture, see the [reference example](REFERENCE_EXAMPLE.md).

**Nothing here is a design.** Every value below is an illustrative prototype assumption chosen to exercise the software. No vendor, vessel, pump, piping or plant-layout data was used. Qualified engineering review is outstanding ([engineering review](ENGINEERING_REVIEW.md)).

## What the source strings mean

Assumption `source` strings in the fixture use two labels:

| Label | Meaning |
| --- | --- |
| `docs/PUMP_SKID.md prototype value` / `prototype assumption` | A value from the original prototype basis listed on this page. |
| `E1a fixture choice` | A value the fixture's original author chose when first building it (positions, routes, body diameters, outlet height). "E1a" names that initial authoring step. It is **not** external engineering evidence. |

Each assumption's `description` states its values in full, so the fixture is self-contained without either label.

## Prototype basis

Process order: `TK-101 → HV-101 → P-101 → NRV-101 → HV-102 → OUT-101`, with lines L-101 to L-105 between them. LI-101 and PI-101 are conceptual indications (`observes` relations), not process connections.

| Item | Prototype value (metres) | Basis |
| --- | --- | --- |
| TK-101 vertical cylindrical tank | diameter 0.8, height 1.2; outlet nozzle centreline 0.2 above the base, aligned with pump suction | diameter/height: prototype value; outlet height: E1a |
| P-101 centrifugal pump | body 0.45 long (x) × 0.30 wide (y) × 0.40 high (z); motor 0.35 long × 0.22 diameter on the shaft axis; end suction, top discharge (template `centrifugal_pump` v1) | prototype value |
| HV-101, HV-102 isolation valves | face to face 0.16; body diameter 0.10 | length: prototype value; body diameter: E1a |
| NRV-101 check valve | face to face 0.18; body diameter 0.11; inlet-to-outlet follows flow | length: prototype value; body diameter: E1a |
| LI-101, PI-101 indications | conceptual 0.08 dial; not an instrument specification or hookup | prototype value |
| OUT-101 scope boundary | 0.2 marker; downstream plant outside the modelled scope | size: E1a; role: prototype basis |
| All ports | DN50 (nominal designation, not a diameter), liquid service | prototype assumption |
| All five lines | DN50, assumed outside diameter 0.0603; not a pipe schedule or fabrication specification | prototype assumption |

Fixture layout (E1a choices, in template-v1 local frames, deck top at z = 0, flow along +x at y = 0 until the discharge turns toward −y):

- placement origins: tank (0, 0, 0); suction valve (0.8, 0, 0.2); pump (1.5, 0, 0); check valve (2.3, 0, 0.7); discharge valve (2.7, −0.45, 0.7) and boundary (2.7, −0.85, 0.7), both rotated −90° about z; tank and pump nozzles project 0.10 from their bodies;
- indication mounts: tank top centre (0, 0, 1.2); top of the pump discharge line (1.9, 0, 0.73);
- guided orthogonal routes: L-101 (0.5, 0, 0.2)→(0.72, 0, 0.2); L-102 (0.88, 0, 0.2)→(1.175, 0, 0.2); L-104 (2.39, 0, 0.7)→(2.7, 0, 0.7)→(2.7, −0.37, 0.7); L-105 (2.7, −0.53, 0.7)→(2.7, −0.75, 0.7); no fittings, supports, slope or clash checks;
- one **manual** route, L-103: (1.5, 0, 0.5)→(1.5, 0, 0.7)→(2.21, 0, 0.7), above the motor, and one manual P&ID choice (symbol `sym-check-valve`, edge `edge-discharge-1`), so preservation of manual work is exercised from the start.

## Explicit unknowns

Duty, flow, pressure, temperature, material and wall thickness are **unknown**. Operating and safety design are omitted. Valve operators, normal and fail positions are not modelled. No instrument tapping, hookup or support is modelled. Generated outputs must keep these unknowns visible rather than fill them in.

## Original milestone behaviours

The project began with eight behaviours for this skid. They remain the demonstration and acceptance reference. The source tests named below exercise them; running them is software verification only, not engineering sign-off, and this page records no new results ([evidence status](REFERENCE_EXAMPLE.md#evidence-status)).

| # | Behaviour | Where it is exercised |
| --- | --- | --- |
| 1 | Generate a coherent P&ID and 3D model from one persisted project | `tests/test_integration.py`, `tests/test_interactive_real.py`, `scripts/reference.py --cad` |
| 2 | Rename equipment and change a shared property through supported controls | `tests/test_core.py`, `tests/test_interactive_service.py`, `tests/browser/workflow.e2e.mjs` |
| 3 | Insert a valve, preserving order and identity in both views | `tests/test_core.py`, `tests/test_pid.py`, `tests/test_drafting_inventory.py` |
| 4 | Move equipment and update or explicitly invalidate affected routes | `tests/test_core.py`, `tests/test_rerouting.py`, `tests/test_route_edit.py` |
| 5 | Select in one view and highlight the corresponding item in the other | `tests/test_interactive_service.py`, `tests/test_interactive_http.py`, browser scenarios |
| 6 | Undo and save/reload without losing identities, connectivity or manual layout | `tests/test_core.py`, `tests/test_storage.py`, `tests/test_transactions.py` |
| 7 | Detect duplicate tags, disconnected required ports and incompatible connections | `tests/test_validation.py`, `tests/test_empty_start.py` |
| 8 | Rebuild unchanged input without duplicate geometry or altered layout | `tests/test_cad_geometry.py`, `tests/test_drafting.py`, `tests/test_integration.py` |

Assess semantics, geometry and visual quality separately. A passing consistency check does not establish process or fabrication adequacy. Whole-plant routing, fabrication detail, stress analysis and process calculations are outside this milestone ([capabilities](CAPABILITIES.md#not-supported)).
