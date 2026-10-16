# Capabilities and limits

This page lists what the code in this repository does, what it does not do, and how each claim can be checked. It describes software behaviour only. **No independent engineering approval of any output is recorded** ([engineering review](ENGINEERING_REVIEW.md)).

Every capability marked *tested* has automated tests under `tests/` that you can run from a clean checkout ([getting started](GETTING_STARTED.md#5-run-the-checks)). The current assessment is [REPOSITORY_ASSESSMENT.md](REPOSITORY_ASSESSMENT.md). Unless it records otherwise, a fresh-clone rerun of the full suite and independent software review are still pending ([evidence status](REFERENCE_EXAMPLE.md#evidence-status)). Development-workspace baselines are recorded only there, labelled as such, and are not fresh-clone results.

## Status words

| Word | Meaning |
| --- | --- |
| **complete** | Every output was generated from the current revision, its manifest verified, and no open item remains. |
| **pending** | The model is structurally valid, but at least one item is deliberately open: unplaced equipment, a route or diagram path that is `needs_reroute`/`unresolved`, or a required port marked `connection_status: "unresolved"`. Pending outputs are inspectable drafts, never "synchronized". |
| **failed** | A generator, verification or integrity check failed. The previous complete publication stays in place. |
| **stale** | The newest published outputs come from an older revision or a different input digest than the current source. |
| **synchronized** | Interactive service only: the verified five-output publication matches the current revision and digest, and the manual-notes sidecar (if present) is readable. |

## Three ways to generate outputs

They differ in what they produce and in what "success" means. Do not treat one as evidence for another.

| Path | Command | Produces | Requires | Success means |
| --- | --- | --- | --- | --- |
| Offline bundle (version 0.1) | `python -m skid.cli build PROJECT` | `pid.svg` (legacy SVG), `model.blend`, `lists.json`, manifests, under `<stem>.skid/bundles/…`, plus pointer `<stem>.skid/current.json` | Python 3.11+ standard library and a Blender executable | Exit `0` = both views complete for this revision; exit `3` = published as **pending**. Composed equipment is shown as an explicit placeholder/omission and keeps the bundle pending. |
| Interactive five-output publication (version 0.2+) | `python -m skid.interactive --project PROJECT` | 3D display scene, STEP CAD, P&ID sheet, legend sheet, item index and process description in one immutable `builds/<build_id>/`, published by `<stem>.workflow/current.json` | Python 3.12 with the pinned CAD requirements | `sync.state: "synchronized"`. Only a build whose five artifacts are all `complete` for the same project, revision and digest moves the pointer. |
| Standalone generators | `python -m skid.geometry build PROJECT OUT`, `python -m skid.drafting --project PROJECT --out OUT` | The geometry or drafting files in `OUT` | CAD requirements (geometry); standard library (drafting) | Nothing is published or compared with a source. `skid.geometry` exits `0` for **pending as well as complete** manifests; `skid.drafting` exits `0` complete, `2` pending, `1` failed. Read the printed statuses. |

## Engineering model and commands

| Capability | Status | Where |
| --- | --- | --- |
| One schema-1 JSON document with separate `model`, `pid` and `physical` sections, undo/redo history, strict loading (duplicate keys, NaN, unknown fields rejected) | tested | `skid/core/`, [input contracts](INPUT_CONTRACTS.md) |
| Permanent IDs separate from editable tags; tags must be unique across equipment and lines after collapsing whitespace runs and ignoring case | tested | `skid/core/validation.py` |
| Validated command envelope with `expected_revision` compare-and-swap; atomic same-directory save under a POSIX file lock; stale or racing writers rejected without writing | tested | `skid/core/commands.py`, `skid/core/storage.py` |
| Commands: `rename_entity`, `rename_project`, `set_parameter`, `set_port_size`, `insert_valve`, `insert_equipment`, `add_equipment`, `connect_ports`, `move_equipment`, `place_equipment`, `move_symbol`, `set_route`, `move_route_segment`, `set_diagram_route`, `batch` (1–12 operations), `undo`, `redo` | tested | [command reference](INPUT_CONTRACTS.md#commands) |
| Undo and redo are new, higher revisions; revisions never decrease | tested | `apply_command` |
| Equipment moves invalidate incident physical routes (old points kept); symbol moves never move equipment and flag, rather than rewrite, manual diagram paths | tested | [reference walkthrough](REFERENCE_EXAMPLE.md#hands-on-edits-standard-library-only) |
| Detection of duplicate tags and IDs, dangling references, wrong port directions, port reuse, DN/service mismatch, missing provenance, invalid current route endpoints | tested | `skid/core/validation.py` |
| Blank projects (`skid.cli new`), adding built-in or composed equipment and connecting named ports | tested | [blank-project walkthrough](INPUT_CONTRACTS.md#worked-example-blank-project-to-complete-model) |

## Equipment

| Capability | Status | Limit |
| --- | --- | --- |
| Built-in templates `tank`, `centrifugal_pump`, `isolation_valve`, `check_valve`, `indicator`, `boundary` (version 1) | tested | DN50 liquid ports only; other sizes are rejected, not drawn wrongly. Indicators are conceptual `observes` relations, not instrument hookups. |
| `composed_equipment` with a self-contained definition built from a fixed part vocabulary and four P&ID symbol families (`horizontal_vessel`, `vertical_vessel`, `box_equipment`, `heat_exchanger`) | tested | At most 8 ports, DN50 liquid, bounded expressions and sizes (`skid.equipment.LIMITS`). No arbitrary CAD, scripts or SVG. |
| Example definitions: horizontal drum, inline box housing, shell-and-tube exchanger | tested | Illustrative shapes. No thermal, hydraulic, mechanical or vendor verification. |
| Inline insertion into an existing line | tested | Two-port items only (`inlet`/`outlet`). |

## Outputs (interactive publication)

| Output | Files | Notes |
| --- | --- | --- |
| 3D display | `scene.json`, `display.manifest.json` | Tessellated from the same solids as the CAD; world metres, right-handed, Z up. Pending routes appear as display-only marked components. |
| CAD | `model.step`, `geometry.recipe.json`, `cad.verification.json`, `cad.manifest.json` | STEP in millimetres, reimported and checked (solid validity, counts, bounds, port faces) before it counts as complete. STEP bytes can differ between identical builds (kernel metadata); compare geometry and mappings, not bytes. |
| P&ID | `pid.svg`, `drawing.inventory.json`, `pid.manifest.json` | One landscape sheet, A–I / 1–10 zones. Original artwork; **no ISA-5.1 or ISO 10628 conformance is claimed**. |
| Legend | `legend.svg`, `legend.manifest.json` | Lists exactly the symbol keys used on the sheet. |
| Item index and process description | `item-index.json`, `description.json`, `description.html`, `description.manifest.json` | Tags, zones, connectivity-derived narrative, assumptions and unknowns. Narrative statements derive from topology and declared definitions, not from process calculations. |

## Interactive workspace

| Capability | Status | Limit |
| --- | --- | --- |
| Loopback-only service with Host/Origin checks, one writer per document, separate build worker process, cancellation, supersession, integrity sealing of every build | tested | POSIX only (`flock`, process groups). |
| Browser workspace (vendored Three.js 0.170.0, no build step): 3D and P&ID views, two- or four-pane layouts, selection across views, inspector, typed edits, structured command form, undo/redo/save/reopen, downloads | tested (Node unit tests, on Node.js 22; browser scenarios need Playwright) | Pane layout lasts for the page session only. |
| Quick edit: a deterministic local parser for rename, one dimension and simple moves | tested | It is not a language model and refuses compound or unknown requests. |
| Guided rerouting of one non-current DN50 route; conservative equipment-body screen for new routes; straight-section moves | tested | Not automatic plant routing. No pipe-to-pipe, support, stress, access or fabrication checks. The body screen uses boxes and can refuse routes that would clear the real CAD. |
| Ask (local deterministic answers about tags, connectivity and paths) | tested | Broader questions use the optional planner below. |
| Engineering plan and optional equipment research | tested with labelled fakes only | Needs your own Claude Code CLI with a Claude Max login; see below. |

## Optional AI planning and research

Engineering plan sends the request text **and a snapshot of the current project** to the exact model `claude-opus-5-5` at requested effort `medium`, through the Claude Code CLI on the operator's own Claude Max subscription. The CLI runs tool-free, with no session persistence, and with `ANTHROPIC_API_KEY` removed from the child environment. The bridge checks the CLI's authentication method and subscription type before trusting any output. It also checks the model and API-key source the CLI reports. Any mismatch refuses the plan; no other model is substituted. Every plan is untrusted input. It is translated into one `batch`, dry-run through the same validator, and shown for review. Nothing is applied without an explicit Apply.

**Research equipment** sends only the request text to a separate web-search step. The CLI's search tool performs retrieval with `claude-haiku-5-5`; the application records and discloses this. Research reads public web text only, with no images, drawings or vendor CAD. Its summaries are model paraphrases tied to observed URLs, not verified facts. **A cited URL grants no rights to the linked content**, and research output is not a licence, a standard or a specification.

If the CLI is missing, logged out, on another subscription or not returning the exact model, plans and research refuse explicitly. Every deterministic capability above keeps working.

## Not supported

- Deleting or duplicating equipment or lines: there is no delete or duplicate command. Use undo, or start from a new document.
- Process design, line or equipment sizing, duty, pressure, temperature, material or rating selection; hydraulic, thermal, stress or safety calculations.
- Fabrication detailing, spools, supports, isometrics, bills of materials, vendor data.
- Automatic whole-plant layout or routing; clash detection beyond the conservative equipment-body screen.
- Instrument hookups, control loops, interlocks; new instrument types.
- Pipe sizes other than DN50; services other than liquid.
- DEXPI or other exchange-format import/export; PDF or drawing ingestion.
- A live Blender add-on (the Blender path is an offline generator).
- Windows (persistence and the service rely on POSIX locking and process groups).
- Multi-project management and multi-user editing.
