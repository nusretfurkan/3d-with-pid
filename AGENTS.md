# Agent instructions

Read this file, then `HANDOVER.md`, before planning or changing anything. Detailed procedures are in [docs/AGENT_WORKFLOWS.md](docs/AGENT_WORKFLOWS.md).

## Division of work

- **A GPT model orchestrates.** It scopes tasks, decides architecture and interfaces, assigns exclusive file ownership, coordinates dependencies, independently assesses results and reports to the owner.
- **Claude Opus 5.5 (`claude-opus-5-5`) at `medium` effort executes** bounded implementation tasks.
- **Claude Opus 5.5 at `medium` performs selective reviews** when the orchestrator judges an additional review worthwhile. Review is selective, not mandatory.

Run executors and reviewers through your **own existing Claude Code CLI with a Claude Max login**. Before the first delegation in a session, verify:

1. the CLI is installed and runnable;
2. `env -u ANTHROPIC_API_KEY claude auth status --json` reports `authMethod: "claude.ai"` and `subscriptionType: "max"` (do not print or store the rest of the output);
3. a short probe with `--model claude-opus-5-5 --effort medium` reports exactly `claude-opus-5-5` and `apiKeySource: "none"`.

Remove `ANTHROPIC_API_KEY` only from child processes. Do not use API-key billing, `--bare`, global configuration changes, or reinstalls. Never substitute another model or effort, and never claim a model ran when it did not. The CLI does not echo applied effort, so say "requested `medium`". If the exact model or effort is unavailable, report that specific limitation and continue with orchestration, planning and deterministic work. The tool itself does not need any model to build, validate, publish its local outputs or test. Do not commit personal CLI paths, account e-mails, organisation IDs or credentials.

## Delegation

Give each executor a bounded task with context, the interface contracts it must respect, exclusive file ownership, acceptance criteria and focused validation commands. Parallelise only independent tasks, after agreeing shared interfaces. Executors do not delegate further. Each writes a result note containing actual commands and results, failures included. Review requests name the code or artifacts and the concrete risks to assess. The orchestrator reconciles findings, runs the full suite and owns the final assessment.

## Project principles

- One authoritative engineering model drives every output. Stable IDs are separate from editable tags.
- Engineering semantics (`model`), P&ID presentation (`pid`) and physical geometry (`physical`) are separate data concerns.
- Every edit is a validated command with `expected_revision`. Regeneration preserves manual layout and explicit routing choices.
- Revisions, unresolved placement, pending and stale outputs are explicit. Never report incomplete synchronization as success.
- Deterministic generators and validators produce geometry and drawings. AI translates intent into structured operations, and its output is untrusted until validated and reviewed.
- Assumed dimensions and data stay identifiable, with provenance. Nothing implies process design, sizing, fabrication readiness or standards conformance.
- **Never weaken tests, validators or integrity checks, and never hide failures.** Label every fake. Do not count a fake pass as real acceptance.
- No automated agent provides professional engineering sign-off ([docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md)).

## Repository status

This is an engineering prototype. **The project licence is chosen** ([docs/LICENSING.md](docs/LICENSING.md)): project-authored material is MIT ([`LICENSE`](LICENSE)), except these seven Blender integration files, which are GPL-3.0-or-later ([`LICENSES/GPL-3.0-or-later.txt`](LICENSES/GPL-3.0-or-later.txt)):

- `skid/adapters/blender.py`
- `skid/adapters/blender_runner.py`
- `skid/adapters/blender_support/scene.py`
- `skid/adapters/blender_support/preview.py`
- `skid/adapters/blender_support/run_build.py`
- `tests/blender_smoke.py`
- `tests/test_geometry_contract.py`

`skid/adapters/blender_support/{__init__,plan,geometry}.py`, `skid/build.py` and `scripts/check_blender.sh` stay MIT. `skid.build` reads its adapter constants from the MIT `blender_support.plan` and starts Blender as a separate program, so the normal CLI and service do not import a GPL file; `tests/test_blender_license_boundary.py` checks this. Do not move code across that boundary, add `bpy`/`mathutils` imports to MIT files, or change licence files, headers or the mapping without the owner's decision. Vendored Three.js keeps its own MIT licence and copyright.

**External publication needs the owner's explicit authorisation.** Without it, do not push to a remote, create a repository, release a package or archive, or otherwise distribute this repository or its contents. This boundary is separate from the tool's own *local* publication: the interactive service publishing a verified five-output build through `<stem>.workflow/current.json`, and `skid.cli build` publishing an offline bundle through `<stem>.skid/current.json`, are normal, authorised workflow steps on the user's own machine. Do not refuse or skip them because of this rule, and do not treat them as permission to publish anything externally.

Keep dependency and asset licences documented when adding anything third-party ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)), and do not add standards-derived symbol artwork. Blender is a separately installed tool, not a contributor or endorser; do not use its logo or imply affiliation.

Keep updates concise. Do not repeat settled decisions or ask for confirmation of routine, reversible work inside an assigned task.
