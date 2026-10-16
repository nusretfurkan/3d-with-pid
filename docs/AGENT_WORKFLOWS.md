# Working with AI agents

There are two kinds of agent work: **operating** the tool, which turns engineering intent into validated commands, and **developing** it, which changes code, tests and docs. The rules differ. In both, deterministic code and its validators are the authority, and an agent's output is a proposal.

## Operating the tool

An agent (or a script) that edits projects must follow the same path as a person:

1. **Read the current state.** Use `python -m skid.cli validate PROJECT --json` (revision, undo/redo depth, open items) and `python -m skid.cli lists PROJECT --json`, or `GET /api/state` on a running service. Use IDs, not tags, to address things.
2. **Express the change as commands.** Use one envelope, or one `batch` of 1–12 operations for anything that must happen together ([input contracts](INPUT_CONTRACTS.md#commands)). Set `expected_revision` to the revision you read. Choose fresh IDs for new records.
3. **Record provenance.** Every new quantity, placement, route and line cites an assumption added by the same command, with a truthful `description` and `source`. Do not invent duty, pressure, temperature, material, ratings, vendor data or standards compliance. Leave unknowns unknown, or ask.
4. **Dry-run.** Apply to a copy first (`cp PROJECT /tmp/x.json && python -m skid.cli apply /tmp/x.json CMD.json`), or use the service's proposal review. Read every diagnostic.
5. **Get review, then apply once.** Apply to the real document only after the reviewer accepts the exact command. On `stale_revision`, start again from step 1. Never bump the revision and resubmit blindly.
6. **Report the actual state.** Report *synchronized* only when the service says so, or *complete* for the offline bundle. Report pending items, failures and refusals as they are.

Never edit generated files, `current.json` pointers or the history arrays by hand. Never "fix" an open item by fabricating a placement or route. Supply real choices, or leave it pending.

### Built-in engineering plan

In the workspace, **Change → Engineering plan** sends the request and a project snapshot to `claude-opus-5-5` (requested effort `medium`) through the operator's own Claude Code CLI on a Claude Max subscription. The reply is validated as untrusted input, translated to one `batch`, dry-run, and shown with its assumptions, unknowns and questions. A plan with questions, or one that is rejected, stale or expired, cannot be applied. **Research equipment** adds a separate web-search step that receives only the request text. Treat research as cited background, not as verified data or a licence to use the linked material ([capabilities](CAPABILITIES.md#optional-ai-planning-and-research)).

Do not send client, vendor or otherwise confidential project data to an external model without authorisation. Quick edit, local Ask and structured commands run entirely locally.

## Developing the tool

### Division of work

The maintainers use this division, and agents contributing to this repository should follow it:

- **A GPT model orchestrates.** It scopes tasks, decides interfaces and file ownership, coordinates parallel work, independently assesses results and reports to the owner.
- **Claude Opus 5.5 at `medium` effort implements** bounded tasks. Opus 5.5 at `medium` also performs selective reviews when the orchestrator judges a risk worth one, for example identity, topology, revision handling, persistence, publication or cross-view integration. Review is selective, not mandatory for every task.
- Executors run through the contributor's **own existing Claude Code CLI and Claude Max login**. Do not use API-key billing, a different model, or a different effort while claiming the requested ones.

### Before delegating

Verify, do not assume:

```sh
claude --version
env -u ANTHROPIC_API_KEY claude auth status --json      # expect authMethod "claude.ai", subscriptionType "max"
env -u ANTHROPIC_API_KEY claude -p --model claude-opus-5-5 --effort medium \
    --output-format stream-json --verbose "Reply with READY only."
```

In the stream, the `system`/`init` event should name `claude-opus-5-5` and report `apiKeySource` `none`, and the assistant messages should come from the same model. The CLI does not echo the applied effort, so say "requested `medium`". Remove `ANTHROPIC_API_KEY` only in the child process; do not change global shell settings. Do not use `--bare`, which skips the subscription login. Do not print or commit credentials, account e-mails or organisation IDs.

If the exact model or effort is unavailable, say so, name the limitation, and continue with work that does not need it. Do not substitute another model or describe work as done by a model that did not run. The deterministic tool itself does not depend on any model: building, validating, publishing and testing all work without one.

### Task contract for an executor

Give each executor:

- the goal and acceptance criteria;
- the interfaces it must not change, or must propose changes to;
- **exclusive file ownership**, so parallel executors never edit the same file;
- the focused tests to run, and whether a full suite is the orchestrator's job;
- a result file to write: what changed, exact commands with their **actual** results (including failures and skips), and remaining limits.

### Rules that protect the engineering model

- One authoritative model. Keep semantics, P&ID layout and physical geometry as separate concerns, and keep IDs separate from tags.
- All edits go through validated commands. Preserve manual layout and routing choices during regeneration.
- Revisions, unresolved placement and stale outputs must stay explicit. Never report partial synchronization as success.
- Equipment geometry and drawings come from deterministic generators and bounded definitions. AI translates intent into structured operations; it does not draw or mesh directly.
- **Never weaken a test, validator or integrity check to make something pass.** If a test is wrong, show why, fix it with a meaningful negative control, and say so. A flaky failure is recorded with its values, not hidden.
- Fake planners, fake CLIs and fake Blender processes are labelled in their file names and outputs. A fake pass is never reported as real acceptance.
- Keep assumed dimensions and data identifiable, and never present research or model output as verified engineering.

### Finishing a change

- Run the focused tests for what you touched and record the exact commands and counts. The orchestrator runs the full suite (`python -m unittest discover -s tests -p 'test_*.py'`, with CAD and `SKID_BLENDER`), the Node unit tests, and relevant browser scenarios.
- Update the user-facing docs that describe changed behaviour, using only behaviour you have actually observed.
- Do not push, create a repository, release a package or archive, or otherwise publish externally without the owner's explicit authorisation ([agent instructions](../AGENTS.md#repository-status)). Do not change licence files, SPDX headers or the [licence mapping](LICENSING.md) without the owner's decision. The tool's local publication of verified outputs (`<stem>.workflow/current.json`, `<stem>.skid/current.json`) is normal workflow and is not affected.
- Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for the pull-request checklist. An AI review is not engineering sign-off ([engineering review](ENGINEERING_REVIEW.md)).
