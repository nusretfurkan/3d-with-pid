# Ask: read-only project questions

`POST /api/ask` answers questions about items, connectivity and paths without changing anything. Most answers are local and deterministic; broader questions can optionally go to the same external planner bridge. Package `skid/assistant/` (standard library only, no CAD, drafting or service imports); service hooks in `skid/interactive/service.py` and `server.py`; tests `tests/test_assistant*.py`. UI: [UI](UI.md#assistant-ask-and-change). Source is authoritative.

## Read-only guarantee

Ask never calls the command dispatcher, `apply_command`, `save_document`, history, proposal storage, build or publication paths, and never adopts an external change. The source of every answer is the project file **as persisted now**, read strictly under a shared storage lock (at most 10 s, then `ask_source_locked`), never the service's in-memory copy. Change requests ("rename P-101 to P-201", "undo") are answered with a `change_request` clarification offering to copy the text to Change; nothing is planned or applied.

## Request

| Field | Rule |
| --- | --- |
| `request_id` | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$` |
| `question` | 1–4,000 characters, not blank, no control characters except newline/tab/CR |
| `selected_ids` | 0–16 entity, port or connection IDs that resolve in the snapshot |
| `expected_revision`, `input_digest` | must equal the persisted revision and digest, else 409 `stale_ask` before any work |
| `history` | optional, ≤ 6 `{question, answer}` records of ≤ 2,000 characters; untrusted model context only |
| `displayed_source` | optional `null` or `{source_revision, input_digest, mode: live|published}` |

Unknown fields (for example `html`, `url`, `command`) are 400 `invalid_ask`.

## Response

Exactly `{id, request_id, project_id, source_revision, input_digest, current_revision, stale, status, answer, references, unknowns, limitations, responder, displayed_source}`.

- `status`: `answered`, `clarification`, `unavailable` or `cancelled`. `answer` is inert plain text (≤ 16,000 characters).
- `references` (≤ 32) are built by the service from the snapshot, never from model text: `{id, tag, kind, field_paths, facts}`. Tags are the snapshot's, so a later rename never relabels an old answer.
- After answering, the file is read again: `current_revision` is the persisted revision and `stale` is true if the digest changed. If the file cannot be verified, `current_revision` is `null` and `stale` is true. Nothing is ever resent automatically.
- `responder.kind` is `deterministic` or `language_model`; `responder.query` names the route (`identity`, `connectivity`, `expanded_connectivity`, `relation`, `overview`, clarification kinds, `change_request`, `historical`, `general`). Bridge metadata keeps only safe fields (model, requested effort, auth method/subscription, API-key source, tools, version, timings, hashes); never the prompt, raw output, CLI path or credentials.

HTTP errors: 400 `invalid_ask`; 409 `stale_ask`, `ask_source_invalid`, `ask_source_locked`, `ask_busy` (one model Ask at a time, at most 8 tracked, or a duplicate `request_id`), `ask_cancelled`, `ask_not_running`, `ask_closed`. Model faults are not HTTP errors: they return `status: unavailable` with `responder.error.code` and the local facts.

`POST /api/cancel-ask {request_id}` signals only that Ask and returns `{request_id, cancel_requested: true}`. A stop recorded before the model phase guarantees no model call (409 `ask_cancelled`); a stop during the model run terminates the CLI process group and returns `status: cancelled`.

## Local answers

All facts come from the snapshot's model: stable IDs, current tags, `from_port_id`/`to_port_id`, port directions, parameters with provenance, placements, routes, edge status, assumptions and open items. Never tag prefixes, drawing coordinates, 3D proximity or generated documents.

- **Mentions:** current tags (case and whitespace insensitive), `TAG.port`, stable IDs, symbol/edge IDs. Unknown tag-like tokens are reported with suggestions; standards and sizes such as `DN50` are not tags; duplicate tags are ambiguous.
- **Identity:** kind, template version, composed name and declared function, ports, connections, observations, dimensions with provenance, placement origin (metres, Z up) and what the model does not record (duty, fluid, pressures, temperatures, materials, ratings, control philosophy).
- **Connectivity and direction:** a connection is directed when its from port is `out|both` and its to port `in|both`, but not both `both`; otherwise it is listed as "direction not established" and never followed. Observations are listed separately as not process flow.
- **Traces and relations:** depth-first traces (≤ 32 connections along a path, ≤ 64 listed per target and direction, with explicit incomplete-listing markers), loop and re-join markers, and bounded breadth-first relation searches (≤ 256 examined connections).
- **Historical questions** get a `historical` clarification: only the current revision is available.

### Declared flow paths

For composed definitions with validated `flow_paths`, traversal moves between flow nodes: one node per declared `[inlet, outlet]` pair and one per port outside every pair. Separate circuits through one item (for example an exchanger's tube and shell sides) are never joined, looped or related through the item itself; only a recorded connection joins them. In relations, pair order is enforced: an inlet reaches its own outlet with no external line, while an outlet reaches its inlet only through recorded return lines. A definition that declares `flow_paths` but cannot be validated passes no flow at all. Items without declared paths keep the entity-level reading.

## Model path

Questions about why, sizing, duty, materials, standards, operating behaviour or purpose go to the model path. It reuses the service's planner bridge unchanged: exact `claude-opus-5-5`, requested effort `medium`, tool-free, `ANTHROPIC_API_KEY` removed in the child, through the operator's own Claude Code CLI and Claude Max login ([planning](PLANNING.md#cli-bridge-skidplanningbridgepy)). The context (≤ 256 KiB; compacted or refused as `ask_context_limit`) contains the question, selection, evidence for up to 8 targets, entities, connections, assumptions and the untrusted page-session history. The answer schema has no operation, command, URL, HTML or apply field; every cited ID must exist in the snapshot. Statements are labelled recorded fact, connection-graph conclusion, general explanation (not project data) or unknown. Ask time limit: 300 s (constructor option `ask_timeout`). Local answers send nothing externally.

## Limits

- Deterministic questions are recognised by bounded patterns; unrecognised phrasing goes to the model path, or is `unavailable` without it. Nothing is guessed.
- Answers describe recorded nominal connectivity, not operating flow; checked references do not prove model sentences correct. No design, sizing, safety or standards check is made.
- Pair order in declared flow paths is enforced in relation answers only. Traces treat a declared path as one node, so where bidirectional (`both`) ports are wired against the declared pair order, a trace can continue through the item while the relation answer says no directed path exists. With `in`/`out` ports the two agree.
- Current revision only; no undo-history or old-publication queries; no durable conversation.
