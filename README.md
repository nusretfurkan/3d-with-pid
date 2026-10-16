# Equipment 3D and P&ID

One authoritative engineering model of a small equipment package, with five generated outputs that are kept consistent with it:

```text
project.json (model · P&ID layout · physical layout · history)
        │  validated commands, revision checked
        ▼
   3D view · STEP CAD · P&ID sheet · legend sheet · item index + process description
```

Equipment, ports and lines have permanent IDs, separate from editable tags. Renaming, editing a dimension, inserting a valve or moving equipment changes the model through validated commands. Each output then states which revision it came from. Outputs that cannot be brought up to date honestly, for example because a moved pump leaves a route unresolved, are marked **pending** instead of being shown as current.

> **Status: private review candidate.** External publication has not been approved, and no project licence has been chosen. The current preparation status is in [docs/REPOSITORY_ASSESSMENT.md](docs/REPOSITORY_ASSESSMENT.md). Every dimension in the examples is an illustrative assumption. No independent engineering review is recorded ([criteria](docs/ENGINEERING_REVIEW.md)). Nothing here is a process design, a fabrication drawing, or a claim of ISA-5.1, ISO 10628 or other standards conformance.

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

## Documentation

- [Getting started](docs/GETTING_STARTED.md): install, run, check
- [Capabilities and limits](docs/CAPABILITIES.md)
- [Input contracts](docs/INPUT_CONTRACTS.md): document, units, provenance, commands, runnable examples
- [Reference example](docs/REFERENCE_EXAMPLE.md): the pump skid, its assumptions, a walkthrough, `scripts/reference.py`, and output files
- [Troubleshooting](docs/TROUBLESHOOTING.md): status badges and refusals
- [Working with AI agents](docs/AGENT_WORKFLOWS.md) and [agent instructions](AGENTS.md)
- [Engineering review criteria](docs/ENGINEERING_REVIEW.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md) and [public-contents audit](docs/PUBLIC_CONTENTS_AUDIT.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Handover](HANDOVER.md)

## Optional AI assistance

The workspace can send a change request, with a project snapshot, to `claude-opus-5-5` through **your own** Claude Code CLI on a Claude Max subscription. The reply comes back as a reviewable, validated command batch; nothing is applied without review. Optional web research sends only the request text. Both are off unless requested, and everything else works without them. See [what leaves your machine](docs/CAPABILITIES.md#optional-ai-planning-and-research).

## Licence

Not yet chosen. The repository contains no `LICENSE` file, and no licence is offered until the owner adds one. Third-party components keep their own licences ([notices](THIRD_PARTY_NOTICES.md)).
