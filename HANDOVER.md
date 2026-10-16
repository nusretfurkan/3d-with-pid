# Handover

For a new orchestrator or maintainer starting from this repository alone. It describes the state of the code as prepared in this repository, without reference to any private workspace, running service or local evidence. Read [AGENTS.md](AGENTS.md) first.

## Goal

An open-source tool in which equipment, or a group of equipment, has a P&ID and a 3D/CAD representation that stay consistent because both derive from one authoritative engineering model. Simultaneous drawing is optional; persistent consistency is the goal. The first target is a small pump skid. The current code also supports blank projects and bounded composed equipment.

## Architecture in one paragraph

A schema-1 JSON document (`skid/core`) holds engineering semantics (`model`), P&ID presentation (`pid`) and physical geometry (`physical`), plus undo/redo history. Every change is a command checked by `apply_command` against the current revision and saved atomically under a POSIX lock. Equipment comes from six built-in templates (`skid/templates`) or self-contained composed definitions (`skid/equipment`). Generators read a frozen snapshot. `skid/geometry` builds CadQuery/OCCT solids, STEP and a tessellated display scene. `skid/drafting` builds the P&ID sheet, an exact legend, an item index and a process description. `skid/interactive` runs a loopback service and a separate build worker. It publishes those five outputs only when all of them verify for one revision, and serves the browser workspace in `web/`. The original offline path (`skid.cli build`: legacy SVG plus a Blender `.blend`) remains. Optional planning and research (`skid/planning`, `skid/research`, `skid/assistant`) call the operator's Claude Code CLI and produce reviewable command batches only.

## Where to start

1. [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md): clean install, core, workspace, checks.
2. [docs/CAPABILITIES.md](docs/CAPABILITIES.md): what is supported, and the three output paths.
3. [docs/INPUT_CONTRACTS.md](docs/INPUT_CONTRACTS.md): document, records, units, commands, invalidation.
4. [docs/REFERENCE_EXAMPLE.md](docs/REFERENCE_EXAMPLE.md): the example skid, a runnable walkthrough, `scripts/reference.py`, and output files.
5. [docs/AGENT_WORKFLOWS.md](docs/AGENT_WORKFLOWS.md): operating and developing with agents.
6. [docs/REPOSITORY_ASSESSMENT.md](docs/REPOSITORY_ASSESSMENT.md): the current assessment, with the release decisions and the verification evidence recorded so far.
7. [docs/LICENSING.md](docs/LICENSING.md): the MIT default, the GPL-3.0-or-later Blender exceptions and the generated-deliverables permission.
8. Detailed module contracts are in the source and its tests. The technical topic pages under `docs/` (for example `CONTRACTS.md`, `INTERACTIVE.md`, `PUMP_SKID.md`) are concise current summaries of each slice's decisions.

## State

- Implemented and covered by automated tests: the core, commands and persistence; built-in and composed equipment; five-output generation and publication; the interactive service and browser workspace; guided rerouting and the equipment-body screen; straight-section moves; blank projects with add/connect; local Ask; the planner and research bridges (tested with labelled fakes).
- **Local test baseline:** the [pre-release verification](docs/REPOSITORY_ASSESSMENT.md#final-post-review-verification) at commit `827d6b7` passed a clean install and a separate fresh-context reproduction: 1,021 Python tests passed and 11 were explicitly skipped; real CAD/reference 288 checks, Node 197, browser workflow 9/9, Blender smoke 1,789 and integration 5/5. That result predates the release changes below and is not a result for them. Agent review is software review only.
- **Release preparation (approved 2026-10-16):** the owner approved publishing this repository as `nusretfurkan/3d-with-pid` after final checks. The changes since that baseline are the licence files and per-file GPL headers, the import-boundary fix in `skid/build.py` and its test, the README screenshots and examples, and documentation updates. **Local release verification passed:** 1,047 Python tests (1,036 passed / 11 explicit context skips), 288 CAD/reference checks, 197 Node tests, and real Blender smoke/integration checks. See the [repository assessment](docs/REPOSITORY_ASSESSMENT.md#local-release-verification-2026-10-16) for scope, historical fresh-agent evidence and the link to GitHub execution results.
- **Tooling targets:** Python 3.12; Node.js 22 for the optional UI and browser checks (the only Node version these checks were run on).
- **Engineering review is outstanding** for every discipline ([docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md)).

## Release decisions (owner, 2026-10-16)

- **Licence:** MIT by default, with seven GPL-3.0-or-later Blender integration files. The exact mapping is in [AGENTS.md](AGENTS.md#repository-status) and [docs/LICENSING.md](docs/LICENSING.md). This is a reasoned assessment, not a legal opinion.
- **Fixtures:** the retained, audited regression fixtures are approved for publication, with their origins disclosed in [docs/FIXTURE_PROVENANCE.md](docs/FIXTURE_PROVENANCE.md). This approval is not engineering approval of their values.
- **Media:** the README screenshots and P&ID are original project output from the synthetic example ([provenance](docs/media/PROVENANCE.md)), under the MIT default.
- **Security reports:** GitHub private vulnerability reporting ([SECURITY.md](SECURITY.md)).
- **External publication** happens only with the owner's explicit authorisation. The tool's normal local output publication is unaffected.

## Known limits

DN50 liquid service only. Composed equipment has at most eight ports, and inline insertion is two-port only. There are no delete or duplicate commands. Routing is explicit or bounded per-connection guidance, not plant routing. The body screen is conservative and checks nothing beyond equipment boxes. There is no process sizing, fabrication or standards conformance. Research is text-only and unverified. The software runs on POSIX only. The Blender path is offline, not a live add-on. See [docs/CAPABILITIES.md](docs/CAPABILITIES.md#not-supported).

## Suggested next work

Follow the owner's priorities. Do not restart completed slices. Open items that are visible in this repository: independent engineering review; the recorded coverage gaps in the [assessment](docs/REPOSITORY_ASSESSMENT.md#coverage-and-limits); and, if requested, broader equipment vocabulary, other DN sizes, junctions for branches, and delete/duplicate commands, each as a bounded, contract-first slice.
