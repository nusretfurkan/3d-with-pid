# 3d-with-pid

One authoritative engineering model of a small equipment package, with five generated outputs that are kept consistent with it:

```text
project.json (model · P&ID layout · physical layout · history)
        │  validated commands, revision checked
        ▼
   3D view · STEP CAD · P&ID sheet · legend sheet · item index + process description
```

Equipment, ports and lines have permanent IDs, separate from editable tags. Renaming, editing a dimension, inserting a valve or moving equipment changes the model through validated commands. Each output then states which revision it came from. Outputs that cannot be brought up to date honestly, for example because a moved pump leaves a route unresolved, are marked **pending** instead of being shown as current.

> **Status: engineering prototype.** MIT licence by default, with a few GPL-3.0-or-later Blender integration files ([licensing](docs/LICENSING.md)). Every dimension in the examples is an illustrative assumption. No independent engineering review or sign-off is recorded ([criteria](docs/ENGINEERING_REVIEW.md)). Nothing here is a process design, a fabrication drawing, or a claim of ISA-5.1, ISO 10628 or other standards conformance. The verification record is in [docs/REPOSITORY_ASSESSMENT.md](docs/REPOSITORY_ASSESSMENT.md), and the origins of the test fixtures are in [fixture provenance](docs/FIXTURE_PROVENANCE.md).

## Screenshots

![The workspace with the example pump skid: a 3D view of a tank, valves, a centrifugal pump on a baseplate and an outlet, beside the generated P&ID sheet and an item list, all marked current at revision 0](docs/media/workspace.png)

*The workspace on the repository's synthetic pump-skid example. The 3D view and the P&ID sheet come from the same verified build of revision 0. The sheet is too small to read here; open the [full P&ID sheet (SVG)](docs/media/pid.svg).*

![Isometric 3D view of the example pump skid: tank TK-101, isolation valves HV-101 and HV-102, pump P-101, check valve NRV-101, indicators LI-101 and PI-101, and outlet boundary OUT-101, with line tags](docs/media/pump-skid.png)

*Isometric view of the same model. All dimensions are illustrative assumptions, and the images are unedited captures of generated geometry, not of a real plant. Some tags near the outlet overlap. How the images were made: [provenance](docs/media/PROVENANCE.md).*

## Who it is for

Engineers, drafters, engineering and project managers, and software agents who want to explore keeping a P&ID and a 3D/CAD model of the same equipment consistent: by stable identity, explicit assumptions and honest status rather than by visual overlap.

## Quick start

POSIX (Linux or macOS), Python 3.12:

```sh
python3.12 -m venv .venv-cad && . .venv-cad/bin/activate
mkdir -p work
python -m skid.cli sample work/skid.json        # copy of the example pump skid
python -m skid.cli validate work/skid.json      # standard library only, no installs

python -m pip install -r requirements-cad.txt   # CadQuery, for the five-output workspace
python -m skid.interactive --project work/skid.json --port 0    # opens a loopback-only workspace
```

The full guide is [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md). The web UI needs no build step; Three.js is vendored. Blender, Node.js 22 with Playwright (for UI and browser checks), and the Claude Code CLI are optional.

## Three ways to generate outputs

| Path | What you get | Success means |
| --- | --- | --- |
| `python -m skid.interactive` (CAD environment) | All five outputs in one immutable build, published by a single verified pointer, and a browser workspace for editing | `synchronized`: all five outputs verified for the current revision |
| `python -m skid.cli build` (needs Blender) | The original offline bundle: legacy P&ID SVG plus a Blender `.blend` | `complete` (exit 0). `pending` (exit 3) is published but not complete. |
| `python -m skid.geometry` / `python -m skid.drafting` | Standalone geometry or drawings in a directory | Nothing is published; `skid.geometry` exits 0 even when pending, so read its statuses |

Details: [capabilities and limits](docs/CAPABILITIES.md).

## Examples

There are two ways to make a change, and both end in the same validated commands:

- **Local and deterministic.** `skid.cli` commands, JSON command files, and the workspace's inspector and **Quick edit** (a local parser for renames, one dimension and simple moves). No model is involved and nothing leaves your machine.
- **AI planning (optional).** In the workspace, **Change → Engineering plan** sends your request and a project snapshot to `claude-opus-5-5` through your own Claude Code CLI. The reply is shown as a dry-run command batch with its assumptions and questions, and nothing is applied until you click Apply. An external coding agent can play the same role by writing command files and following [agent workflows](docs/AGENT_WORKFLOWS.md#operating-the-tool).

Example requests for a coding agent working in this repository. Each one stays within supported commands, and the linked walkthroughs show the exact JSON. Requests 1–3 use one fresh project, `work/readme-pump/project.json`, separate from the quick start's `work/skid.json`, and are meant to run in order. `sample`, `new` and `scripts/reference.py` refuse to overwrite existing files, so the expected results below hold only for a fresh directory:

1. **Reference pump skid.** "If `work/readme-pump/` already exists, list what is in it and stop; do not delete or overwrite anything. Otherwise run `mkdir -p work/readme-pump`, `python -m skid.cli sample work/readme-pump/project.json` and `python -m skid.cli validate work/readme-pump/project.json` (expect VALID, exit 0). Then run `python scripts/reference.py --output work/readme-pump/reference --cad`, which works on its own isolated copy of the example. Report its exit status and any failed check from `work/readme-pump/reference/evidence.json`. Do not edit generated files." ([reference script](docs/REFERENCE_EXAMPLE.md#reference-script))
2. **Rename and edit without losing layout.** "Continue with `work/readme-pump/project.json` from request 1. Read its revision with `python -m skid.cli validate work/readme-pump/project.json --json`. Try the edits first on a copy that does not yet exist (`cp -n` to `work/readme-pump/trial.json`). Then, on the project, run `python -m skid.cli rename work/readme-pump/project.json ent-pump P-201 --expected-revision <N>`, and `apply` a `set_parameter` command that sets `motor_length` to 0.42 m, with a new assumption labelled illustrative. Show that the IDs, manual placements and routes are unchanged." ([hands-on edits](docs/REFERENCE_EXAMPLE.md#hands-on-edits-standard-library-only))
3. **Move the pump, reroute, undo.** "Continue with the same project after request 2. Move `ent-pump` 0.2 m along +x (from `position_m` [1.5, 0, 0] to [1.7, 0, 0]) with a `move_equipment` command at the current `expected_revision`. Confirm that `validate` exits 3 because L-102 and L-103 need rerouting. Propose both `set_route` operations as one `batch`, and apply it after my review. Then run `undo` with `--expected-revision` and report that `validate` exits 3 again because the routes are pending." ([walkthrough](docs/REFERENCE_EXAMPLE.md#hands-on-edits-standard-library-only))
4. **Composed exchanger, illustrative draft only.** "Run `python -m skid.cli new work/readme-exchanger/project.json --name 'Exchanger sketch'`; if the file already exists, stop and report. Add one `composed_equipment` entity with `add_equipment`, whose `definition` is the content of `examples/recipes/shell_and_tube_exchanger.definition.json`. Its four required ports stay marked `unresolved` until connected. Report the `validate` exit status as it is (3 means valid but incomplete). Do not state duty, sizing, ratings or a thermal or mechanical design." ([composed equipment](docs/INPUT_CONTRACTS.md#composed-equipment-definitions), [exchanger example](docs/EMPTY_START.md#shell-and-tube-example))

Composed equipment is built from a fixed part vocabulary, so an agent can sketch other illustrative shapes within the [equipment limits](docs/CAPABILITIES.md#equipment). The tool does not do whole-plant layout, automatic routing, or process design, including distillation or other column design (stages, internals, duty, sizing). For such a request, an agent should say which parts are unsupported and do only the supported, illustrative part, never approximate the rest. See [not supported](docs/CAPABILITIES.md#not-supported). Some of these could become future extensions; none is implemented now.

**Starting a fresh development agent** (copy as is):

```text
Read AGENTS.md, then HANDOVER.md, then docs/GETTING_STARTED.md. Do not push, publish externally
or change licence terms. Set up a new Python 3.12 environment (.venv-cad) with requirements-cad.txt. If Node.js 22
is available, run `npm ci` and then `npx playwright install chromium`; npm ci alone does not
download the browser. Before changing anything, run these and report the exact counts and every
skip with its reason. Run the full Python suite first without SKID_BLENDER set:
  python -m unittest discover -s tests -p 'test_*.py'
  python scripts/reference.py --output work/agent-check/reference-cad --cad
  node --test "tests/browser/unit/*.test.mjs"
  node tests/browser/workflow.e2e.mjs
The reference output directory must be new or empty; if it exists, report that instead of
deleting it. If Blender is installed, also run
SKID_BLENDER=/path/to/blender sh scripts/check_blender.sh; otherwise report the Blender lane as
not run. Never weaken a test or report a skipped lane as passed. Then wait for a bounded task.
```

## Documentation

- [Getting started](docs/GETTING_STARTED.md): install, run, check
- [Capabilities and limits](docs/CAPABILITIES.md)
- [Input contracts](docs/INPUT_CONTRACTS.md): document, units, provenance, commands, runnable examples
- [Reference example](docs/REFERENCE_EXAMPLE.md): the pump skid, its assumptions, a walkthrough, `scripts/reference.py`, and output files
- [Troubleshooting](docs/TROUBLESHOOTING.md): status badges and refusals
- [Working with AI agents](docs/AGENT_WORKFLOWS.md) and [agent instructions](AGENTS.md)
- [Engineering review criteria](docs/ENGINEERING_REVIEW.md)
- [Licensing](docs/LICENSING.md), [third-party notices](THIRD_PARTY_NOTICES.md), [repository assessment](docs/REPOSITORY_ASSESSMENT.md) and the historical [public-contents audit](docs/PUBLIC_CONTENTS_AUDIT.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Handover](HANDOVER.md)

## Optional AI assistance

The workspace can send a change request, with a project snapshot, to `claude-opus-5-5` through **your own** Claude Code CLI on a Claude Max subscription. The reply comes back as a reviewable, validated command batch; nothing is applied without review. Optional web research sends only the request text. Both are off unless requested, and everything else works without them. See [what leaves your machine](docs/CAPABILITIES.md#optional-ai-planning-and-research).

## Licence

Project-authored material is licensed under the [MIT License](LICENSE). That includes the documentation, examples, test fixtures and the images in `docs/media/`. The exceptions are seven Blender integration files that call Blender's Python API or load the modules that do. They are licensed under [GPL-3.0-or-later](LICENSES/GPL-3.0-or-later.txt) and are listed in [docs/LICENSING.md](docs/LICENSING.md#gpl-exceptions-blender-integration), which also describes an additional permission for generated deliverables. The vendored Three.js files keep their own MIT licence and copyright. Blender and the other dependencies are installed separately under their own terms ([NOTICE](NOTICE), [third-party notices](THIRD_PARTY_NOTICES.md)). This is a reasoned licensing assessment, not a legal opinion.
