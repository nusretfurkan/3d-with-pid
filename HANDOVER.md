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
6. [docs/REPOSITORY_ASSESSMENT.md](docs/REPOSITORY_ASSESSMENT.md): the current preparation assessment, with the evidence collected so far and what is still pending.
7. Detailed module contracts are in the source and its tests. The technical topic pages under `docs/` (for example `CONTRACTS.md`, `INTERACTIVE.md`, `PUMP_SKID.md`) are concise current summaries of each slice's decisions.

## State

- Implemented and covered by automated tests: the core, commands and persistence; built-in and composed equipment; five-output generation and publication; the interactive service and browser workspace; guided rerouting and the equipment-body screen; straight-section moves; blank projects with add/connect; local Ask; the planner and research bridges (tested with labelled fakes).
- **Evidence:** [final preparation assessment](docs/REPOSITORY_ASSESSMENT.md#final-post-review-verification): clean install and separate final-version reproduction passed. Final suite: 1,021 passed, 11 explicitly skipped; real CAD/reference 288 checks, Node 197, browser workflow 9/9, Blender smoke 1,789 and integration 5/5. Tested commit and runtime/source fingerprints are recorded there; agent review is software review only.
- **Tooling targets:** Python 3.12; Node.js 22 for the optional UI and browser checks (the only Node version these checks were run on).
- **Engineering review is outstanding** for every discipline ([docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md)).

## Open decisions (owner)

- Project licence. None has been chosen, and no `LICENSE` file is present.
- Distribution terms for the Blender adapter, which uses Blender's GPL-licensed Python API. Running Blender as a subprocess does not by itself settle this.
- Publication of any retained test fixture derived from interactive editing or captured model output (see `docs/PUBLIC_CONTENTS_AUDIT.md`).
- A private vulnerability-reporting contact, to be set up before any external release (see `SECURITY.md`).
- External publication of any kind (remote, public repository, package or archive) is forbidden until the owner approves it, under the release gate in `docs/PUBLIC_CONTENTS_AUDIT.md`. These decisions block external publication; they do not block preparing or reviewing the source.

## Known limits

DN50 liquid service only. Composed equipment has at most eight ports, and inline insertion is two-port only. There are no delete or duplicate commands. Routing is explicit or bounded per-connection guidance, not plant routing. The body screen is conservative and checks nothing beyond equipment boxes. There is no process sizing, fabrication or standards conformance. Research is text-only and unverified. The software runs on POSIX only. The Blender path is offline, not a live add-on. See [docs/CAPABILITIES.md](docs/CAPABILITIES.md#not-supported).

## Suggested next work

Follow the owner's priorities. Do not restart completed slices. Open items that are visible in this repository: the owner decisions above; independent engineering review; and, if requested, broader equipment vocabulary, other DN sizes, junctions for branches, and delete/duplicate commands, each as a bounded, contract-first slice.
