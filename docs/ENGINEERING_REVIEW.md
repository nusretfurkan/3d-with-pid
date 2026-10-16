# Engineering review: criteria and status

**Status: outstanding.** No independent process, mechanical, piping or drafting review of this software or its outputs is recorded for this release candidate. The automated tests, the reference script, maintainers' checks and reviews by AI agents are **software verification**. None of them is professional engineering review or sign-off, and none may be presented as one.

*Outstanding* is a legitimate, honestly recorded status. It does not prevent the owner or contributors from preparing, reviewing or testing the source, or from running independent software review. It does mean the software and examples may claim only what they are: an illustrative prototype whose outputs have no recorded engineering approval. This review is needed before anyone relies on outputs for an engineering purpose, not before the source can be prepared.

This page defines what a qualified reviewer should assess before anyone relies on the software for a stated purpose, and how to record the outcome. Passing every item does not make the example skid a design. The example is an illustrative prototype ([reference example](REFERENCE_EXAMPLE.md)).

## Who reviews

A reviewer should be a competent, independent practitioner in the discipline, as recognised by the organisation that intends to use the tool. Independent means not an author of the code or examples under review. Where a jurisdiction or client requires licensed or chartered sign-off, that requirement governs; this page does not replace it. Automated agents may help prepare a review, but the reviewer of record must be a person.

## What is in scope

The question is whether the tool represents the engineering information it is given **faithfully, consistently and with honest status**, and whether its drawings and models are fit for the stated purpose. Typical purposes are a concept study, a discussion drawing or a training aid. Process design adequacy of any particular plant is out of scope unless a reviewer explicitly takes it on for a specific project.

## Acceptance criteria by discipline

Record each item as **met**, **not met** (with evidence) or **not assessed**. Unassessed items remain outstanding.

### Process

| ID | Criterion |
| --- | --- |
| PR-1 | Flow order, line-to-port connectivity and direction in the P&ID, item index and process description match the model for the example and for at least one reviewer-built project (for example a blank project built with add/connect). |
| PR-2 | The description states only what follows from topology and declared definitions. It does not imply duty, sizing, control philosophy, normal or fail positions, or safety functions that were never entered. |
| PR-3 | Unknowns (duty, pressure, temperature, material, ratings, operators) are visible wherever a reader would otherwise assume them. |
| PR-4 | Indications drawn with `observes` are not mistakable for control loops or process connections. |
| PR-5 | Equipment with declared internal flow paths (for example the shell-and-tube example's two circuits) is described without mixing circuits. |
| PR-6 | Optional research output is presented as unverified, cited background. It is never presented as a design basis, and the cited sources are traceable. |

### Mechanical (equipment geometry)

| ID | Criterion |
| --- | --- |
| ME-1 | Port positions and outward directions in the STEP model match the model's port frames after placement, including rotated equipment. |
| ME-2 | Dimensional edits, such as a pump motor length or a vessel shell length, change the STEP solids and the 3D display consistently, and only where expected. |
| ME-3 | Shapes are labelled illustrative, and no output suggests vendor accuracy, pressure-vessel design or fabrication readiness. |
| ME-4 | Out-of-range or unsupported parameters are refused or fail explicitly. They are never silently clamped. |
| ME-5 | STEP units (millimetres), axes (Z up) and assembly structure import as described into the reviewer's CAD tool. |

### Piping and layout

| ID | Criterion |
| --- | --- |
| PI-1 | Moving equipment or changing a dimension that moves a port flags every affected route. No route is shown as current with stale endpoints. |
| PI-2 | Manual routes and diagram paths are preserved across regeneration, undo/redo and save/reload. |
| PI-3 | Guided reroutes and section moves respect the stated rules (orthogonal runs, bend room, nozzle tangents). Their refusals are understandable. |
| PI-4 | The conservative equipment-body screen is described accurately. It is not presented as clash detection, and the unchecked aspects (pipe to pipe, supports, stress, access, drainage, slope, fabrication) are clearly stated. |
| PI-5 | DN and outside-diameter values are presented as nominal/assumed, never as a selected pipe specification. |

### Drafting

| ID | Criterion |
| --- | --- |
| DR-1 | Tags are readable and unambiguous, and lines do not visually merge or terminate ambiguously. |
| DR-2 | The legend contains exactly the symbols used on the sheet. Symbols are recognisable for their intended meaning. |
| DR-3 | Sheet zones (A–I / 1–10) in the item index match the drawn positions. |
| DR-4 | The title block identifies the project, revision and status. A pending or stale sheet cannot be mistaken for a current issue. |
| DR-5 | The artwork does not claim ISA-5.1, ISO 10628 or any company standard. If a standard is required, the gaps are listed. |
| DR-6 | Manual notes stay attached to the right items and are flagged when stale or orphaned. |

### Cross-cutting

| ID | Criterion |
| --- | --- |
| X-1 | Every output identifies its source revision. "Synchronized" appears only when all five verified outputs match the current revision. |
| X-2 | Failed, pending and cancelled builds never replace the last complete publication. |
| X-3 | Renaming changes tags only. IDs, connectivity and manual layout are preserved in all outputs. |
| X-4 | Each assumed value can be traced to its assumption record and source. |

## How to review

1. Follow [getting started](GETTING_STARTED.md) on a clean machine, including the CAD requirements.
2. Run `python scripts/reference.py --output <dir> --cad` and keep `evidence.json`.
3. Open the workspace on a copy of the example and on one project you build yourself. Exercise rename, a dimension edit, a valve insertion, an equipment move with rerouting, undo, save and reopen.
4. Inspect the published `pid.svg`, `legend.svg`, `description.html`, `item-index.json` and `model.step` (in your CAD tool) against the criteria above.
5. Record the result as described below.

## Recording a review

Open an issue with the **Engineering review** template, or add a dated file to the repository, with:

- the reviewer's name, discipline, qualification basis and independence statement;
- the commit reviewed, platform and dependency versions (`pip freeze`), and the reference `evidence.json` hash;
- each criterion ID with met / not met / not assessed and its evidence (file, zone, ID or screenshot of a shareable project);
- the stated purpose for which the result applies, and any restrictions.

A review applies only to the commit and purpose it names. Later changes to generators, templates or symbols require a fresh review of the affected criteria.

## Current record

| Discipline | Reviewer | Commit | Result |
| --- | --- | --- | --- |
| Process | none | — | outstanding |
| Mechanical | none | — | outstanding |
| Piping and layout | none | — | outstanding |
| Drafting | none | — | outstanding |
