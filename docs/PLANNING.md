# Engineering plans (`skid.planning`)

Optional AI planning: the request text and a frozen project context go to an external model through the operator's own Claude Code CLI, and the reply comes back as a reviewable, validated batch. Nothing applies without an explicit Apply. Contract context: [extension contracts](EXTENSION_CONTRACTS.md#engineering-planning). Creation operations and research citations: [creation and research](CREATION_AND_RESEARCH_UI.md). What leaves the machine: [capabilities](CAPABILITIES.md#optional-ai-planning-and-research). Source is authoritative.

Everything deterministic (core, CLI, generators, Quick edit, local Ask, rerouting) works without this feature or any Claude installation.

## Flow

```text
POST /api/propose {text, expected_revision, mode: "engineering", research?: bool}
  -> frozen snapshot of revision R / digest D (no service lock held while the model runs)
  -> optional research stage (request text only; see EQUIPMENT_RESEARCH.md)
  -> context + instructions -> auth check + `claude -p` (bridge.py) -> untrusted structured JSON
  -> strict plan schema -> translation into existing operations -> ONE batch envelope
  -> apply_command dry run against R/D -> stored proposal (stale if the source changed meanwhile)
POST /api/apply-proposal {proposal_id, expected_revision}
  -> status ready, not applied/stale/expired, same revision
  -> under the write lock: reread from disk, digest still D -> apply + save
  -> one revision, one undo snapshot, one build
```

## CLI bridge (`skid/planning/bridge.py`)

Two child processes from argument lists (never a shell), each in its own process group and fresh private working directory; the prompt goes on stdin.

1. `<cli> auth status --json` must report `authMethod: "claude.ai"` and `subscriptionType: "max"`. Only those fields are kept; account details are never stored.
2. `<cli> -p --model claude-opus-5-5 --effort medium --output-format stream-json --verbose --json-schema <schema> --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence`

Both children get the service environment with only `ANTHROPIC_API_KEY` removed, so the subscription login is used rather than API billing. Model and effort are constants; the CLI does not echo applied effort, so metadata records `effort.requested: "medium"` and `reported: null`. There is no `--bare`, fallback model, API client, MCP server or session file, and nothing is installed at runtime.

The default executable is `claude` found on `PATH`; override it with `--planner-cli PATH` at launch (HTTP cannot choose it). A missing executable gives `planner_unavailable`; an environment that hides the CLI's credential store gives `planner_auth`.

The stream is verified line by line, and any violation stops the child: strict JSON; exactly one init before any assistant message, with model `claude-opus-5-5`, `apiKeySource: "none"`, no MCP servers and no tools except the structured-output return channel; every assistant message from that exact model; a final successful `result` with a `structured_output` object and no other model in `modelUsage`; plan ≤ 256 KiB.

| Error code | Status |
| --- | --- |
| `planner_unavailable`, `planner_auth`, `planner_timeout`, `planner_cancelled`, `planner_exit_status`, `planner_error_result` | `unavailable` |
| `planner_model_mismatch`, `planner_api_key_source`, `planner_tools_enabled`, `planner_tool_use`, `planner_malformed_output`, `planner_output_missing`, `planner_output_limit`, `planner_input_limit` | `rejected` |

Time-out, `POST /api/cancel-proposal` and shutdown send `SIGTERM` to the process group, then `SIGKILL` after 3 s.

| Limit | Value |
| --- | --- |
| Request text | 2,000 characters; control characters refused |
| Prompt | 1 MiB |
| Plan time-out (`--planner-timeout`) | 600 s; auth check 30 s |
| CLI stdout / one line / plan | 16 MiB / 8 MiB / 256 KiB |
| Concurrent plans (`--max-plans`) | 1; further requests get 409 `planner_busy` |
| Operations per plan | 12 (the core batch limit) |
| `definition_json` | 65,536 characters, nesting ≤ 40, then `skid.equipment.LIMITS` |
| Proposal lifetime | 1 hour (`proposal_expired`) |

## Context and plan schema

`build_context(document, text, examples=None, research=None)` gives revision, digest, conventions (metres, Z up, XYZ rotation; P&ID +y down and paper offset), every entity with parameters, provenance, bounds, ports, placements and symbols, every connection with routes and edges, assumptions, open items, unknowns, the operation vocabulary with derived-ID rules, the creation vocabulary (present even for an empty model) and the composition vocabulary with the example definitions. User text and equipment metadata are JSON data, never prompt structure.

The CLI receives a limit-free schema; every limit is rendered from `PLAN_SCHEMA` into an "Output limits" block in the prompt and enforced locally. Any violation rejects the whole output; nothing is truncated, repaired or retried.

Plan fields: `outcome` (`proposal|questions|unsupported`), `summary`, `reasoning`, `operations`, `assumptions [{key, description, source_ids}]`, `unknowns`, `questions [{id, question, reason}]` (at most 8), `impacts`. Operations translate to core commands: `rename_entity`, `set_parameter` (declared parameters, m or mm), `move_equipment` (or `place_equipment` when unresolved), `move_symbol`, `set_route` (manual), `set_diagram_route`, `insert_valve`, `insert_equipment` (`example:<name>`, `entity:<id>` or `definition_json`), `add_equipment`, `connect_ports`.

Translation rules: references must exist or be created earlier in the same plan; derived IDs follow fixed patterns (for example `ent-<stem>` → `port-<stem>-inlet`, `sym-<stem>`) and collisions are rejected; every new value needs an assumption (an existing ID or a plan key that becomes `asm-<proposal id>-<key>` with a service-written source stating it is a proposed, reviewed value, not a vendor or design value); unresolved placements stay unresolved and no route is invented.

## Proposal record

Proposals keep the quick shape (`id, text, expected_revision, summary, commands, assumptions, unknowns, supported, validation, applied`) and add `mode`, `status` (`ready | questions | unsupported | rejected | invalid | unavailable`), `reason`, `command_count` (0 or 1), `operation_count`, described `operations`, `proposed_assumptions`, `questions`, `reasoning`, `impacts` (planner or validator diff), `base_digest`, `planner` metadata (CLI version, models, requested effort, auth fields, API-key source, token counts, elapsed time, prompt/output hashes; no prompt or raw output), `stale`, `created_at`, `expires_at`, `limitation` and `research`. `validation.geometry_checked` is always false: CAD runs only in the build after Apply, and a CAD failure there keeps the previous publication.

`POST /api/apply-proposal` checks, in order: status `ready` and validation accepted (400 otherwise), not applied, not stale, not expired, same revision (409), then commits only if the file digest is still the base digest (409 `stale_proposal`).

## Limits

- The planner proposes; validators check consistency, not engineering adequacy. No sizing, safety, control or standards check is made.
- Proposals live in memory and are forgotten on restart. Stale proposals are refused, never rebased. Questions need a new self-contained request; there is no conversation state.
- The whole project context is sent with every plan, bounded by the 1 MiB prompt limit.
- Recorded assumptions carry planner-written descriptions; their accuracy is the reviewer's responsibility.
- No deletion, duplication, branching, new instruments or control functions.
- Tests use labelled fake CLIs and planners (`tests/test_planning*.py`, `tests/test_interactive_planning*.py`); they exercise the application path, not a real model.
