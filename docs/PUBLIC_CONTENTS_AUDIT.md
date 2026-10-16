# Public contents audit and release gate

> **Historical record, kept unchanged in substance.** This is the initial, pre-fix audit from the start of repository preparation. Its "not ready" status, findings and gate items describe that moment, not the current state. In particular, the gate's licence, Blender-terms, fixture and security-contact items were owner decisions then pending; the owner decided them on 2026-10-16 (MIT with GPL-3.0-or-later Blender exceptions per [LICENSING.md](LICENSING.md), retained fixtures approved, GitHub private vulnerability reporting), so they are not current blockers. Statements below such as "the project license is undecided" are historical. The recorded failures are kept as they were. Nothing here records an independent engineering or legal review. The current state, including the final verification, is in [REPOSITORY_ASSESSMENT.md](REPOSITORY_ASSESSMENT.md).

Initial audit: 2026-10-16. It covers the **source paths and proposed boundary** of the curated release candidate. The generated export (`release/github/`) did not exist at audit time. It needs a separate review against this document before any publication. This is not legal advice, and no professional engineering sign-off is implied.

Status: **NOT READY TO PUBLISH EXTERNALLY.** The gate in section 6 fails closed.

**This is the initial audit, kept as a historical record.** Sections 2–4 describe the source as it was *before* corrections; findings F1–F10 are not a list of what is still unfixed. Final reconciliation of each finding is in [REPOSITORY_ASSESSMENT.md](REPOSITORY_ASSESSMENT.md), which is the current assessment. The final scan of the generated export is pending; until it is recorded there, no finding counts as verified closed for the export. One correction to this record: the r47 fixture holds **seven** reviewed engineering-plan assumptions, not six; see [fixture provenance](FIXTURE_PROVENANCE.md).

The gate controls **external** publication only (push, public remote, package or archive). It does not restrict the owner-authorised private preparation and review of the candidate, or the tool's local publication of its own generated outputs.

## 1. Proposed boundary

**Include:** `skid/`, `web/` (including `web/vendor/three/`), `tests/`, `examples/`, `requirements-cad.txt`, new `scripts/`, `.github/`, package files, `THIRD_PARTY_NOTICES.md`, this audit, `docs/DEPENDENCY_INVENTORY.json`, new portable docs, and selected current technical contracts after the fixes in section 4. The portable `README.md`, `AGENTS.md`, `HANDOVER.md` and `.gitignore` templates replace the workspace originals.

**Exclude:**

| Path or class | Reason |
| --- | --- |
| `artifacts/` (all) | Generated builds, user projects and history, raw model and executor logs (including authentication and session metadata), private screenshots, downloaded third-party references. |
| `.agents/`, `.claude/` (14 symlinks), `skills-lock.json`, `docs/vendor/taste-skill-LICENSE.txt` | Third-party development skills that are not distributed. The notice file belongs with them. |
| `.venv-cad/`, `node_modules/`, every `__pycache__/` (16 directories under candidate paths) and `*.pyc` | Local environments and caches. No dependency binaries are redistributed. |
| Workspace `README.md`, `AGENTS.md`, `HANDOVER.md` | Private orchestration history. Replaced by portable templates. |
| Historical planning and acceptance documents: `ACCEPTANCE_2026-10-14.md`, `EXTENSION_ACCEPTANCE_2026-10-14.md`, `WORKSPACE_CONTROLS_ACCEPTANCE_2026-10-14.md`, `WORKSPACE_CONTROLS_PLAN.md`, `EXECUTION_PLAN.md`, `ASSISTANT_AND_3D_INTERACTION_PLAN.md`, `CORE_TASK_BOUNDARY.md` | Session history, local service URLs, process and session identifiers, private evidence paths. |
| `docs/DEPENDENCIES.md` (as it stands) | Contains a developer home-directory path, machine-specific runtime paths and model-access history. Replace it with this inventory and the notices, or scrub it (section 4). |
| `release/` | The export must never include itself. |

## 2. Scan results (candidate paths at audit time)

The scan covered 262 files (5,678,992 bytes): `skid` 89, `tests` 92, `web` 44, `docs` 28 (before exclusions), `examples` 7, and 2 top-level files. Only paths, pattern classes and counts are recorded here, never matched values.

| Check | Result |
| --- | --- |
| Symlinks inside candidate paths | 0 |
| Binary or non-UTF-8 files | 0 |
| Files over 300 KB | 1: the vendored `three.module.min.js` (691,648 bytes, expected) |
| Unusual file names | 0 |
| API keys, tokens, private keys, bearer strings | 0 |
| Real e-mail addresses | 0. All 14 matches are synthetic test values (`example.invalid`, `example.org`, credential-in-URL negative tests on fictitious `fake-*` domains). |
| Organization/account IDs | 0 real. Session keys in `tests/test_planning_fakes.py` and `tests/test_research_fakes.py` are fake constants. |
| Developer home-directory path | **4 files**: `skid/planning/bridge.py` (default CLI path), `skid/research/researcher.py` (default CLI path), `docs/DEPENDENCIES.md`, `docs/PLANNING.md` |
| Machine-specific runtime paths | Blender `.app` path in 2 source docstrings and 4 docs; a Homebrew path in `docs/DRAFTING.md`. These are examples, not personal data. Making them portable is recommended. |
| Local service URLs | Only generic `127.0.0.1` defaults in source and tests; excluded or scrubbed docs carry the session-specific ports |
| References to excluded `artifacts/` | 166 occurrences in 50 files. Runtime `/artifacts/<build>/` URL routes are product API paths and fine. Problems are listed in section 4. |

## 3. Provenance of examples and fixtures

| File | Origin evidence | Status |
| --- | --- | --- |
| `examples/pump_skid.json`, `examples/make_fixture.py` | Authored synthetic prototype. Assumptions cite `docs/PUMP_SKID.md` prototype values. | Original. Keep the cited document public, or provide a replacement it can cite. |
| `examples/recipes/*.json` | Authored composed definitions. The exchanger cites one public vendor overview page for the general arrangement only and states that no vendor geometry or dimensions were copied. DN50 flange proportions are labelled "no rating or standard selected". | Original. An engineering reviewer should confirm that the proportions are not presented as standard data. |
| `tests/fixtures/pipe-segments.json` | Generated parity cases. `generated_by` names generator scripts in the private evidence tree. | Synthetic, but its generator is not published (see fix F6). |
| `tests/fixtures/pipe-segments-r47.json` | **A snapshot of the owner's interactive workspace project at revision 47** (empty history), byte-equal in model, P&ID and physical data to the private pre-edit snapshot. It derives from the synthetic pump skid, plus the owner's own web edits (2 `asm-ui-*` entries with timestamps), 4 guided reroutes and reviewed engineering-plan assumptions generated by an external model (recorded here at audit time as 6; the fixture actually has 7, from 3 proposals, per [fixture provenance](FIXTURE_PROVENANCE.md)). No personal identifiers were found. | **Owner confirmation required** to publish (user-edited content and work timestamps), or replace it with a reproducible synthetic derivative (fix F7). |
| `tests/browser/unit/fixtures/real-questions-8.json` | A trimmed copy of an actual planner reply (external model output) about the synthetic fixture. `_source` names a private evidence path. | Owner confirmation for model-output content; reword `_source` (F6). |
| P&ID symbols and drawing furniture | Code-generated from SVG primitives. Source headers state they are not copied or traced from ISA-5.1, ISO 10628 or any library. | Original. No standards conformance claimed. No standards artwork bundled. |

## 4. Required fixes before export

Each fix belongs to the path's owner. This audit does not edit them.

| ID | Path | Fix |
| --- | --- | --- |
| F1 | `skid/planning/bridge.py` `DEFAULT_CLI` | Use a portable default (`claude` on `PATH`), keep the `--planner-cli` override, and keep the strict auth/model validators unchanged. `tests/test_planning_bridge.py::test_default_executable_is_the_verified_native_binary` asserts the old suffix and needs a meaningful replacement, not deletion. |
| F2 | `skid/research/researcher.py` `DEFAULT_CLI` | The same personal path. **No executor currently owns this file.** Apply the same portable default, or share a single constant with F1, and update any research test that pins it. |
| F3 | `docs/DEPENDENCIES.md`, `docs/PLANNING.md` | Remove the home-directory CLI path and session history, or exclude the document. `DEPENDENCIES.md` also links to the excluded skill notice and the old acceptance document. |
| F4 | Public technical docs that link to excluded docs: `CONTRACTS`, `EXTENSION_CONTRACTS`, `PLANNING`, `INTERACTIVE`, `UI` (to the extension acceptance), `ASK` and `UI` (to the assistant plan/contracts), `PROJECT_NAMING`, `INTERACTIVE` and `UI` (to the workspace-controls plan), `DRAFTING` (to `PUMP_SKID`) | Either include the target (scrubbed) or replace each link with a portable summary. `ASSISTANT_3D_CONTRACTS.md` is a current contract that tests cite; including it is recommended. |
| F5 | Docs that mention private evidence paths (`UI` 30, `ASK` 10, `INTERACTIVE` 10, `CAD_SPIKE` 10 plus 1 link, `DRAFTING` 4, and others) | Remove or neutralize the `artifacts/...` evidence references. They are not available in a clean clone. |
| F6 | Comments and data naming private evidence: `tests/fixtures/pipe-segments.json` `generated_by`; `tests/browser/unit/fixtures/real-questions-8.json` `_source`; comments in `tests/test_assistant.py`, `tests/test_assistant_service.py`, `tests/blender_smoke.py`, `web/js/pipe-sections.js` | Publish the parity generator under `scripts/`, or describe provenance without a private path. Do not alter fixture values. |
| F7 | `tests/fixtures/pipe-segments-r47.json` | Owner confirms publication, **or** it is regenerated from `examples/pump_skid.json` by a published script that reproduces the geometry the tests need (column, L-105/L-106 routes). In the second case, keep the original privately as evidence. |
| F8 | `tests/browser/lib.mjs` | Must load Playwright from a public `npm ci` install rather than the private runtime directory (owned by the portability executor). |
| F9 | `requirements-cad.txt` | Pins only `cadquery`. A clean install can resolve different transitive versions from the audited snapshot. Publish a constraints/lock file with the 39 audited pins, or state that the clean resolution is unaudited. |
| F10 | Blender adapter files (see `THIRD_PARTY_NOTICES.md` section 4) | Owner/legal decision on distribution terms, or exclude these files from the candidate. Until then, publication is blocked. |

Hygiene notes: the third-party `typish` package installs a top-level `tests` package into site-packages. Running from the repository root keeps the project `tests` first on `sys.path`; CI should keep doing that.

## 5. Dependencies and licensing

See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) and [DEPENDENCY_INVENTORY.json](DEPENDENCY_INVENTORY.json). Explicit uncertainties:

- cadquery-ocp declares no license metadata, and its wheel bundles 75 shared libraries without their notices.
- NLopt's declared MIT may not cover every compiled component.
- CasADi declares LGPL-3.0-or-later with only a generic classifier.

None of these binaries is redistributed. Research links grant no rights. The project license is undecided, and the absence of `LICENSE` is deliberate.

## 6. Fail-closed release gate

External publication (push, public remote, package or archive) is permitted only when **every** item has recorded evidence. Any missing, failed or unknown item blocks external publication. These items, including the licence, Blender adapter and fixture decisions, do not block the authorised private preparation, review or local testing of the candidate.

1. **Owner approval:** the owner has explicitly approved the exact export hash inventory, chosen a project license (a `LICENSE` file is added only by the owner), and decided the Blender adapter terms (F10).
2. **Licensing review:** this audit has been re-run on the generated export. `THIRD_PARTY_NOTICES.md` matches the actual vendored hashes, the inventory matches the pinned dependencies, and no excluded third-party notice or reference file is present.
3. **Secrets and personal-data scan:** a scan of the full export (every file, including dotfiles) finds zero tokens/keys, zero real e-mail addresses, zero home-directory or personal paths, and zero organization/account/session IDs. Fixes F1–F3 and F5–F6 are verified. Test-only synthetic values are listed as allowed.
4. **Boundary check:** the export has no symlinks, caches, virtual environments, `node_modules`, `artifacts/`, `.agents/`, `.claude/`, `skills-lock.json` or `release/`. The export's own hash inventory matches the files.
5. **Clean-clone acceptance:** a fresh clone on a clean POSIX machine with Python 3.12 (no pre-existing venv) installs from the documented commands, passes the core suite and the documented reference run, and has no doc link pointing at a missing or private path. CAD, Blender and browser lanes are reported separately as passed or untested; untested lanes are never reported as passed.
6. **Engineering review status:** [ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md) records the status of each discipline and its acceptance criteria. An **outstanding** status satisfies this item when it is stated plainly in the docs, the criteria are published, and every public claim stays within an honest prototype description (illustrative dimensions, no sizing, fabrication or standards conformance, no implied approval). A completed review is not a prerequisite for preparing or reviewing the source. No automated agent counts as professional sign-off.
7. **Fixture provenance:** F7 is resolved, and the owner has confirmed publication of any retained model-output fixture.
8. **Security contact:** the owner has set up the private vulnerability-reporting route described in [SECURITY.md](../SECURITY.md).
