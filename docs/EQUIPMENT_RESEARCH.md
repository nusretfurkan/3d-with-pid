# Equipment research (`skid.research`)

An optional, opt-in, search-only step that can run before an [engineering plan](PLANNING.md). It turns a short equipment request (for example "shell-and-tube heat exchanger with separate shell and tube circuits") into a verified brief, `skid.equipment.research/1`, in which every source URL was actually observed in a successful web-search result in the CLI's own output stream. The planner itself stays tool-free. Service and UI integration: [creation and research](CREATION_AND_RESEARCH_UI.md). Source is authoritative.

**What leaves the machine:** only the request text, sent through the operator's own Claude Code CLI on a Claude Max login. No project data is sent. The research assistant is exact `claude-opus-5-5` at requested effort `medium` (applied effort is not echoed). The CLI's WebSearch tool performs its retrieval with `claude-haiku-5-5`; this is recorded and disclosed in the result metadata as `internal_retrieval`, and is the only other model accepted. Research reads public web text only: no images, drawings or CAD are inspected. A cited URL grants no rights to the linked content, and a brief is not a design basis, standard or specification.

## Public API

```python
from skid.research import ClaudeCliResearcher, ResearchConfig, ResearchError, validate_brief

researcher = ClaudeCliResearcher(ResearchConfig())     # cli_path defaults to `claude` on PATH
researcher.describe()                                   # static description; starts no process
reply = researcher.run("shell-and-tube heat exchanger, four nozzles", cancel=threading.Event())
reply["brief"], reply["metadata"]                       # validated brief and safe metadata
researcher.shutdown()                                   # stops running children; later runs refused
```

- `run(request, cancel=None)`: request text only (1–2,000 characters). Returns a verified reply or raises `ResearchError(code, message, metadata)`. It never returns a partial or research-free result.
- `ResearchConfig(cli_path, timeout_s=480, auth_timeout_s=30, grace_s=3, max_stdout_bytes=16 MiB)`. The service passes its `--planner-cli` executable and `--research-timeout`.
- Integrators should re-validate replies with `validate_brief(brief, observed_urls)` and `validate_research_result(reply, request)`; both return problem lists and never raise. They apply equally to injected test researchers.

## Brief

```json
{"schema": "skid.equipment.research/1", "query": "<exact request>", "equipment_type": "Shell-and-tube heat exchanger",
 "summary": "Concise paraphrase.",
 "sources": [{"id": "S1", "url": "https://www.example-manufacturer.com/...", "title": "<observed title>",
              "observations": ["Paraphrase of what the search result says."]}],
 "features": [{"text": "Tube-side and shell-side fluids are separated by the tube walls.", "source_ids": ["S1"]}],
 "assumptions": ["Illustrative overall length 3 m; not from a source."],
 "unknowns": ["Nozzle sizes and ratings."],
 "warnings": ["<text-evidence warning>"]}
```

Bounds: summary ≤ 1,500 characters (the prompt targets 2–4 sentences, ≤ 700); 1–8 sources with unique `S<n>` IDs and public http(s) URLs ≤ 600 characters; 1–4 observations of ≤ 300 characters per source; 1–16 features citing 1–4 listed sources; ≤ 12 assumptions and unknowns; 1–8 warnings, starting with the standard text-evidence warning; ≤ 64 KiB total. Titles come from the search result, not the model. Text fields refuse control, bidirectional, private-use and similar characters. All brief text is untrusted web-derived data: escape it for display and pass it to the planner only as evidence.

`features` are characteristics a cited result supports; `assumptions` are illustrative choices no source established; `unknowns` are what the search did not establish. Observations are model paraphrases tied to observed URLs; their wording is not mechanically verifiable.

The URL grammar accepts only ASCII public http(s) URLs with well-formed escapes, no credentials or explicit port, and a public host; local, special-use, private and reserved hosts and addresses are refused. URLs are never fetched.

## How a run is verified

1. `<cli> auth status --json` must report `claude.ai` / `max`; nothing else is read.
2. `<cli> -p --model claude-opus-5-5 --effort medium --output-format stream-json --verbose --json-schema <schema> --tools WebSearch --allowedTools WebSearch --permission-mode dontAsk --strict-mcp-config --setting-sources "" --no-session-persistence`, prompt on stdin, private temporary directory, own process group, `ANTHROPIC_API_KEY` removed.
3. The stream is checked line by line: exact init model, `apiKeySource: none`, tools exactly `StructuredOutput` and `WebSearch`, no MCP servers; every assistant message from the exact model; at most 8 WebSearch calls (the prompt asks for at most 5) and 3 structured-output calls. A search result counts as evidence only in its exact expected form, and only its leading `Links` array; URLs elsewhere in result text are ignored.
4. The final result must succeed; `modelUsage` may contain only the exact model plus `claude-haiku-5-5` with at least one web-search request; no web fetches.
5. Every cited URL must equal a URL observed before the final output (`research_unverified_source` otherwise; never repaired or encoded). At least one successful search is required (`research_no_evidence`).
6. If the only problem is a summary over 1,500 characters, it is replaced by a clearly labelled overview built from whole sourced features; `metadata.overview_replaced` discloses this. Any other problem refuses the run (`research_invalid_brief`). No retry or extra call is made.

Error codes: `research_input_invalid`, `research_unavailable`, `research_auth`, `research_cancelled`, `research_timeout`, `research_output_limit`, `research_malformed_output`, `research_model_mismatch`, `research_api_key_source`, `research_tools_enabled`, `research_tool_use`, `research_tool_limit`, `research_error_result`, `research_exit_status`, `research_output_missing`, `research_no_evidence`, `research_unverified_source`, `research_invalid_brief`. Time-out, cancellation and shutdown terminate the whole child process group. A failure must never become an ordinary unresearched plan.

Safe metadata includes model, requested effort, auth fields, API-key source, CLI version, tools, response and usage models, `internal_retrieval`, search counts and the recorded queries (clipped), observed and cited URLs, hashes of request, prompt, raw output and final brief, timings and `image_inspection: false`. No credentials or raw authentication output are stored.

## Prompt

The prompt asks for generic public queries about the equipment type, omitting tags, client, site and project values; manufacturer product pages first; one or two strong sources; arrangement, named connections and circuits, subtypes and unknowns; and a short summary. URLs must be copied exactly from search results. This generic-query rule is an instruction to the model, not a privacy filter: the request text itself is sent unchanged, so do not put confidential information in it. The queries actually made are recorded for review.

## Limits

- The stream format is the one the code was written against; a changed format fails closed (`research_no_evidence`).
- An observed URL does not prove the page states each observation; treat features as leads, not engineering data.
- No cache: each run searches again.
- Tests use labelled fake CLI streams (`tests/test_research*.py`); they do not exercise a real model or real search.
