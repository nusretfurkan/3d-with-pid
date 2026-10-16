# Empty-start and research contract

Current summary of the interface decisions behind blank-project creation and optional equipment research. This page states the rules that the three implementing areas share; the detailed behaviour is in [empty start](EMPTY_START.md) (core, templates, drafting, CAD), [equipment research](EQUIPMENT_RESEARCH.md) (researcher) and [creation and research](CREATION_AND_RESEARCH_UI.md) (planner, service, UI). Source is authoritative.

## Scope

Start from a genuinely empty document, add equipment and connect named ports through validated commands, and generate the existing five outputs. Optionally research an equipment type before the tool-free planner runs. The first researched example is a shell-and-tube exchanger with four ports and two separate flow paths.

Existing projects, identities, history, manual P&ID and physical layout, definition replay, clearance checks, revision guards and current/pending publication are preserved. No destructive reset or project-switching UI is part of this scope.

## Core rules

- `new_document(project_id, name=None)` and `python -m skid.cli new` create revision 0 with no hidden seed content and refuse existing targets.
- `add_equipment {entity, ports, symbol, placement?}` accepts any supported built-in template or validated composed definition with its full declared port set.
- An unconnected required port remains a structural error unless it carries the explicit marker `connection_status: "unresolved"`, which makes it a warning and keeps outputs pending. Connected ports never keep the marker; contradictory or unknown markers are errors.
- `connect_ports {connection, edge, route?}` joins two existing unused ports by stable ID only, never by proximity. Missing paths stay explicitly unresolved.
- Add and connect run in one `batch` with one revision and undo step; a failure leaves document and history unchanged; new routes are screened initially and in the final batch result.
- Empty CAD, display and drafting outputs are truthful and empty: no dummy geometry.
- Composed definitions may declare `flow_paths`, disjoint `[inlet, outlet]` pairs. Shell and tube circuits are never mixed through shared equipment identity; disconnected auxiliary ports stay explicit.
- The `heat_exchanger` glyph is original artwork; the example definition uses only existing parts. Manufacturer pages may support general characteristics, never assumed dimensions, and no proprietary images, symbols or vendor CAD are copied.

## Research rules

- Public API `ClaudeCliResearcher`, `ResearchConfig`, `ResearchError`; brief schema `skid.equipment.research/1`.
- Exact `claude-opus-5-5`, requested effort `medium`, through the operator's own Claude Code CLI on a verified Claude Max login (`claude.ai`/`max`, `apiKeySource: none`), with `ANTHROPIC_API_KEY` removed in the child only. No fallback model, API client, MCP, shell, file or fetch tools.
- Only built-in WebSearch plus the structured-output return channel. The CLI's internal retrieval with `claude-haiku-5-5` is accepted only alongside matched WebSearch evidence and is always disclosed in metadata; any other model is refused. The planner remains tool-free with its own exact-model rules.
- Request text only; no project data. Search queries should be generic (no tags, clients or site values); this is a prompt instruction, not a filter.
- Source URLs must be public http(s) URLs actually observed in search results before the final output. Search failure or missing evidence is never a successful brief. No image inspection is claimed.
- Failures are explicit refusals. There is no silent research-free fallback.

## Integration rules

- `POST /api/propose` accepts an optional strict boolean `research`, true only in engineering mode, default false.
- Research then planning run on the same frozen snapshot without service locks, in one plan slot, with shared cancellation and shutdown. A source change in either stage makes the whole proposal stale.
- The verified brief appears in the proposal and in the planner context as untrusted evidence. Applied assumptions retain the research identity and source URLs, so references survive save, reload and regeneration. Proposed dimensions remain assumptions.
- New capabilities are advertised additively (`equipment_creation`, `equipment_research`); the shared frontend gates its controls on them and never sends new fields to older services.

## Acceptance expectations

Genuine blank start; staged creation with deliberately open ports and no false complete state; a recognisable four-port exchanger with separate circuits; real CAD and drawing output; identity and provenance preserved through undo, save and reload; negative checks for invalid IDs, ports, directions, stale or cancelled research, invented URLs and hostile source text. Automated tests use labelled fakes for the external model. Real-model behaviour and engineering adequacy are not established by those tests ([engineering review](ENGINEERING_REVIEW.md)).
