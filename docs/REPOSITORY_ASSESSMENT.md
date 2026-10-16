# Repository preparation assessment

Prepared 2026-10-16. **Private review candidate; no GitHub remote or publication, and no project license selected.** These are software results, not professional engineering approval.

## Clean-checkout reproduction

A separate exact Opus 5.5 executor, requested `medium`, started without the conversation context in a real local Git clone of commit `6026f62a67da55ed9589549bf297cdcb4ef6ff1f`. Safe mode disabled customizations; project Python and Node path overrides were removed. It used only repository instructions and files, created a new `.venv-cad` with **no system site-packages**, installed the documented requirements, and left tracked files unchanged. It did not reuse the development virtual environment.

Observed host: macOS 27.0.1 arm64; Python 3.12.15 / pip 26.2.1; Node 22.23.1 / npm 12.0.2; Playwright 1.58.2 / Chromium 145.0.7632.6; Blender 5.1.1. All **39 installed CAD package versions matched the audited constraints** (including CadQuery 2.6.1, cadquery-ocp 7.8.1.1.post1, VTK 9.3.1). Python, Node and Blender were installed machine prerequisites. Chromium was already in the user-level browser cache; the documented browser-install command succeeded without downloading it.

| Lane at `6026f62a` | Demonstrated result |
| --- | --- |
| Documented quick start, blank/add/connect, insertion and edit walkthroughs | Reproduced their stated revisions and exit statuses; overwrite/stale refusals remained refusals |
| Core reference | 90/90 checks, exit 0 |
| Real five-output CAD reference | 267/267 checks, exit 0; real STEP reimport and identical source identities |
| Full Python suite, real CadQuery and Blender enabled | 1,027 tests: **1,015 passed, 12 skipped**, zero failures/errors; 235.406 s (verbose coverage run: 210.549 s, same result) |
| `npm ci` and Node UI units | 197/197 passed, no skips |
| Real-service Chromium workflow | 9/9 steps; no console/page problems; real generators and local deterministic proposals |
| Real Blender smoke | 1,789 checks passed |
| Offline Blender build and status | Complete, synchronized bundle at revision 0 |
| Standalone geometry and authored drum recipe | Complete; drum 5 parts / 31 solids, STEP reimport verified |

The reference verifies 8 entities, 10 ports, 5 lines, 12 initial assumptions; 67 STEP parts / 350 solids / 68 display components; 10 port flange faces / 12 envelopes; 16 legend keys / 13 index items. It verifies stable IDs, rename and new-revision undo, save/reload, manual layout/routing preservation, unchanged regeneration, stale/conflict refusals, and a pending nozzle edit that retains the prior complete publication. Standalone geometry exit 0 with pending manifests is explicitly tested as incomplete.

## Final post-review verification

A new, separate executor with fresh context used only the repository instructions and files at **`827d6b7fa642197f35e175b7d9c975ba1d2b6762`**. It reused the environment independently cold-installed above, confirmed that all 39 pins still matched, installed nothing, and left every tracked file and HEAD unchanged. No earlier conversation or development environment was used.

| Final lane | Result |
| --- | --- |
| Full Python suite, real CadQuery and Blender enabled | **1,032 tests: 1,021 passed, 11 skipped**, zero failures/errors; 216.036 s |
| Real core + five-output CAD reference | **288/288 checks**, all 18 commands returned their expected status; no errors |
| Node UI units | **197/197 passed**, no skips |
| Real-service Chromium workflow | **9/9 steps**, zero console/page problems |
| Real Blender lane | **1,789 smoke checks and 5/5 integration tests passed** |
| Public source check and self-re-export | **287 files**, byte-identical to the tracked tree; inventory SHA-256 `520a28c7aba81eb7898eb298c6652b65caaee8a432e7f7cd664abb3cc373a12e` at the tested commit |
| New committed fixture regressions | Both ran and passed; the real mode-000 directory test also ran |

The final sample publication is revision 4, all five outputs complete under one verified pointer. Rename, undo and the nozzle edit preserve or explicitly invalidate the intended data; undo restores model, P&ID and physical records exactly. The output includes a real 5,819,000-byte STEP assembly and its successful reimport verification. The test/output folders and private logs are kept outside the public tracked tree; rerun the commands to produce recipient-owned evidence.

The **11 skips** are exactly the three private-workspace manifest checks and eight excluded recorded-provider replay cases described below. The public-manifest and real self-export positive checks ran. Blender was not skipped. The 1,001-test / zero-skip development baseline and initial 1,027-test result are historical; neither is substituted for this final result.

The delivered repository adds only assessment/handover evidence text after the tested commit. Its runtime, examples/fixtures, tests, tools, dependency pins and CI (251 files) retain source fingerprint SHA-256 **`9c932b05011bc01fae33578277cf3761283e4040d0eb4bbc994b809fd460fcae`**. Final Git-tree inventory is recorded with the local review artifact. No application, generator, test or dependency version changed after final verification.

## Review findings and corrections

An independent exact Opus 5.5 reviewer at requested `medium` assessed export privacy/completeness, reference checks, manifests, dependencies and instructions. It found no blocker in the actual initial candidate, and identified concrete corrections. Agent review is software review only.

| Finding | Resolution |
| --- | --- |
| Symlinked input directory could escape the source root | Reproduced independently, fixed, and independently rechecked: refuses before copying and writes no candidate |
| Scan/copy race, changed/staged bytes, dishonest Git setup success | Exact read bytes are scanned/copied; source and staged copies are verified; Git failure or incomplete staging reports **partial, exit 3**, with the exported tree disclosed |
| Unreadable directory silently omitted | Listing errors now refuse; injected and real mode-000 negative controls fail on the prior code and pass after the fix |
| Missing/malformed reference expectations and unused expected values | Usage errors return 2 before output creation; index count and nozzle origin/target expectations are enforced; wrong expectations still fail |
| Two regressions depended on excluded development artifacts | Audited exact JSON fixtures are committed; the original legacy-recipe assertion and hash-pinned creation-r3 assertions run unconditionally. No values, expected hash or assertions were weakened |
| Reference edit reused the old assumption for a new value | The changed parameter now cites a fresh, explicitly illustrative assumption that supersedes the old value for that parameter; original provenance and undo fidelity are preserved |
| Minor walkthrough/Blender logging issues | Directory typo corrected; failed Blender version queries propagate their real failure |

The corrected executor reference runs passed **99/99 core checks and 288/288 core+CAD checks**. These are implementation-executor checks; the separate final reproduction above independently reran the 288-check CAD/reference lane. Existing engineering generators and validators were unchanged by repository preparation.

## Coverage and limits

The 12 initial skips were 3 private-workspace manifest checks, 8 replay tests needing excluded real-model stream/reply recordings, and 1 creation-r3 snapshot test. The creation snapshot is now committed and its regression mandatory. The three workspace-only cases are replaced in a public checkout by actual shipped-manifest and self-re-export checks. The eight recorded-provider replay cases remain explicitly skipped: raw transcripts and authentication/session metadata are excluded. This is a disclosed coverage gap, not eight passes. Set `SKID_BLENDER` to run its real integration; otherwise that is an additional stated skip. A permission test also reports a skip on root/file systems that permit listing mode-000 directories.

No fresh real app planning, broad Ask or equipment-research model call is claimed. Their synthetic/fake-provider unit checks are labelled; the reproduction used local deterministic edits. No GitHub Actions or Linux/bare-machine run occurred. Dependency wheel availability is not OS execution evidence. Optional npm `fsevents` install-script warning under npm 12 did not affect the checks; no global permission setting was changed.

Exporter use is documented in [getting started](GETTING_STARTED.md). Use `release/` or a sibling output outside every included source tree. An earlier export left inside an included tree could be swept into a later export; that unsupported placement is documented. The exporter is for a trusted local workspace, not an adversarial same-user file system. Pattern scans are supplemented by review and do not certify the absence of every possible secret.

## Public-contents audit reconciliation

The [initial audit](PUBLIC_CONTENTS_AUDIT.md) records findings at the start of preparation; this table is the current reconciliation.

| Initial finding | Current state |
| --- | --- |
| F1/F2 personal CLI defaults | Both use the operator's `claude` on PATH; explicit override and strict model/auth/tool checks retained |
| F3–F5 private paths, history and unavailable doc links | Portable top-level instructions and 19 current technical summaries replace private originals; all candidate Markdown links/anchors checked with zero problems |
| F6 private fixture provenance paths | Metadata/comments made self-contained; numerical regression data preserved; origins documented in [fixture provenance](FIXTURE_PROVENANCE.md) |
| F7 owner-edited/model-output fixtures | Origins and timestamps disclosed; publication remains an owner decision. Added legacy and creation snapshots were scanned before byte-exact copying; captured research summaries/URLs are not vendor-data redistribution permission |
| F8 private browser-tool install | Public `npm ci` with exact Playwright 1.58.2 and integrity-pinned lock; used by the cold reproduction |
| F9 unaudited transitive resolution | One 39-pin constraints file, matching the [license inventory](DEPENDENCY_INVENTORY.json), actually cold-installed on macOS arm64 / Python 3.12 |
| F10 Blender API distribution | **Open owner/legal decision**; a subprocess does not resolve the API-script licensing question |

The candidate scan found no credentials, real account IDs or personal home paths in tracked content. The only home-path/e-mail pattern matches are identified synthetic negative-test strings. Vendored Three.js hashes and its MIT notice are preserved. No dependency binaries, downloaded reference assets, local skill bundles, raw transcripts, environments or user workspaces are tracked. The project `LICENSE` is deliberately absent pending the owner. Dependency binary notice uncertainties remain relevant to any future binary release ([third-party notices](../THIRD_PARTY_NOTICES.md)).

The local Git commit was made by a separate orchestration step after export, using a neutral automation identity. The initial export report describes export-time state, including then-uncommitted files; the final Git tree/review inventory identifies the reviewed source. No remote is configured.

## Engineering status and owner review

One authoritative engineering document, permanent IDs, and separate semantics, P&ID presentation and physical placement/routing remain intact. Original sample dimensions and operating data are explicit prototype assumptions. Duty, flow, pressure, temperature, material, thickness, ratings, operating/safety design and instrument hookups remain unknown. Valid solids and consistent drawings establish neither design adequacy nor fabrication/vendor accuracy ([capability limits](CAPABILITIES.md)).

Qualified independent process, mechanical, piping and drafting review is **outstanding**. No connected professional reviewer was available and no external review request was sent. [Engineering review criteria](ENGINEERING_REVIEW.md) define acceptance and the required reviewer-of-record evidence. No automated agent counts as that signoff.

Owner decisions before external publication: project license; Blender adapter distribution terms; retained owner-edited/model-output fixtures; final source inventory/publication approval; a private vulnerability reporting contact. Preparing this private candidate does not approve those decisions. The four existing user project/history documents remained byte-identical, and existing preview services were not restarted.

All implementation/review executors were verified as exact `claude-opus-5-5` through Claude Max with API-key source `none`, requested `medium`; applied effort is not echoed. Audit WebSearch also accounted for the CLI-internal Haiku retrieval helper, disclosed separately from the Opus assistant. No API-key billing, fallback assistant or global tool/configuration change was used.
