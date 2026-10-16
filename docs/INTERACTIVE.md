# Interactive command and build service (`skid.interactive`)

The local service that owns one project document, applies validated commands, runs the two generators in a separate worker and publishes the five coordinated outputs. Contract: [workflow contracts](WORKFLOW_CONTRACTS.md). Setup: [getting started](GETTING_STARTED.md#4-interactive-five-output-workspace). Source is authoritative (`skid/interactive/`, tests `tests/test_interactive*.py`).

## Run

```sh
python -m skid.cli sample work/demo.json            # or: python -m skid.cli new work/blank.json
python -m skid.interactive --project work/demo.json --port 0 --keep-builds 12
# prints http://127.0.0.1:<port>/ ; Ctrl-C or SIGTERM stops; a running build is cancelled, never published
```

| Option | Meaning |
| --- | --- |
| `--project PATH` | Existing project; never created or replaced; anything under `examples/` is refused |
| `--port N` | Loopback port (default 8765; `0` picks a free port) |
| `--workflow-dir DIR` | Output root (default `<project stem>.workflow` beside the project) |
| `--notes FILE` | Manual-notes sidecar (default `<project stem>.notes.json`) |
| `--timeout S` | Per-build limit (default 900 s) |
| `--keep-builds N` | Delete the oldest sealed, unpublished builds beyond N (default 0 keeps all) |
| `--no-initial-build`, `--quiet` | Skip the start-up build; do not log requests |
| `--planner-cli PATH`, `--planner-timeout S`, `--max-plans N` | Optional engineering planner ([planning](PLANNING.md)); default executable `claude` on `PATH` |
| `--research-timeout S`, `--no-research` | Optional research stage ([equipment research](EQUIPMENT_RESEARCH.md)) |

Use an interpreter with the CAD requirements installed. Under another interpreter the service runs, but every build fails with an explicit generator diagnostic. Planning and research are optional; if the CLI is absent they report unavailable and everything else works.

## Architecture

- **One writer.** Every mutation is a command envelope applied by `apply_command` and saved atomically with a revision check. Selection, builds and proposals never enter the document or its history.
- **External writers.** The file is checked on every request. A valid external change (for example the CLI) is adopted, reported as `external_change` and rebuilt; a command based on the old revision gets 409. An invalid file blocks writes until repaired and reopened.
- **Worker process.** Each accepted change queues a build of exactly that revision. The worker runs `build_geometry` and `build_drafting` concurrently on a frozen, digest-checked `snapshot.json`, in its own process group.
- **Queue.** At most one running and one queued build. A newer revision supersedes older work; identical requests coalesce.
- **Cancel.** `SIGTERM` to the worker group, then `SIGKILL` after a grace period. A late result can never publish.
- **Publication.** Only when all five artifacts verify `complete` for the same project, revision and digest with no open items. The service rechecks every file, hashes the build, takes the document storage lock, rereads the source and compares digest and notes digest, writes the seal `build.json`, then atomically replaces `current.json`.

Output layout and the pointer format: [reference example](REFERENCE_EXAMPLE.md#output-files). A real build occupies tens of megabytes, mostly progressive previews; `--keep-builds` never removes the published, current, running, queued or an unsealed build.

## Integrity checks

Run by the worker and again by the service before publication, on pointer or file changes of the published build, and fully on start-up and reopen: manifest fields, adapter version `0.2.0`, identity equal to the snapshot, diagnostics shape; complete mappings for every entity, port and connection; safe relative file paths and required files; strict JSON and identity inside `scene.json` and `description.json`; STEP header; SVG and HTML free of scripts, event handlers, external references and similar active content; event order and immutable previews; and process outcome. Any failure fails the affected artifact or build.

## HTTP API

Errors are `{error, diagnostics}` with 400, 409 or 500, plus transport refusals (403/404/405/411/413/415).

| Endpoint | Body | Result |
| --- | --- | --- |
| `GET /api/state` | | full state |
| `GET /api/events?build_id=B&after=N` | | ordered events after N |
| `GET /api/project`, `/api/history`, `/api/selection` | | state subsets |
| `POST /api/command` | command envelope | state; starts a build unless `no_op` |
| `POST /api/build` | `{}` or `{force?, expected_revision?}` | state, or `build_not_needed` |
| `POST /api/cancel` | `{build_id}` | state |
| `POST /api/selection` | `{semantic_ids, origin: pid|display|inspector}` | state |
| `POST /api/save` / `/api/reopen` | `{expected_revision}` / `{}` | state |
| `POST /api/propose` | `{text, expected_revision, mode?, research?}` | proposal; never writes |
| `POST /api/apply-proposal` / `/api/cancel-proposal` | `{proposal_id, expected_revision}` / `{proposal_id}` | state / cancellation |
| `POST /api/reroute` | `{connection_id, expected_revision}` | state after one guided `set_route` |
| `POST /api/ask` / `/api/cancel-ask` | see [Ask](ASK.md) | answer / acknowledgement; read-only |

State includes `project`, `diagnostics`, `selection`, `build`, `published`, `artifacts`, `artifact_sources`, `sync`, `document`, `history`, `last_command`, `planning`, `capabilities` and `service`.

- `sync.state`: `synchronized` only when the verified publication matches the current digest and readable notes; otherwise `building`, `pending`, `failed`, `cancelled`, `blocked` (notes sidecar unreadable), `invalid` (published set no longer verifies), `stale` or `unpublished`.
- `artifacts` offers the current build's working output or preview when present, else the verified published file; `artifact_sources` states origin, kind, build and source identity for each. Clients must label anything whose revision differs from the project's.
- A `complete` build whose files no longer verify is marked `integrity: rejected` and offers nothing.
- Selection results give `display_components: null` when the mapping is unknown or unverified; only an explicit `[]` is a known absence, with a reason.

## Quick proposals

`skid/interactive/proposals.py` is a deterministic parser, not a language model, and says so. One proposal is one command: rename (`rename P-101 to P-201`), one template dimension in mm/cm/m (`set the tank height to 1.5 m`), a physical move (`move P-101 0.5 m in +x`) or a P&ID move (`move the pump symbol right by 40`). Requests are limited to 500 characters. Compound, ambiguous or unknown requests, line renames, insertion, deletion and rotation return `supported: false` with no commands. Every supported proposal is dry-run first.

## Guided rerouting

`POST /api/reroute` recomputes one non-current DN50 route from the authoritative port frames and commits it as one `set_route` (`mode: "guided"`) with a fresh assumption. Owners must be placed with quarter-turn rotations and axis-aligned ports. A bounded candidate set of straight runs, stubs and detours is filtered by the pipe compiler's rules (port tangency, no reversal, bend room, spacing) and the [equipment-body screen](PIPE_SEGMENTS.md#equipment-body-screen), then ranked by fewest short flange runs, bends and length. Refusals (`reroute_unplaced_endpoint`, `reroute_unsupported_rotation`, `reroute_unsupported_direction`, `reroute_unsupported_size`, `reroute_impossible`, `reroute_clearance_unavailable`, `route_current`, `reroute_busy`, `stale_route`, `stale_revision`) change nothing. Only the selected route changes; other routes and P&ID data are untouched. This is not automatic plant routing.

## Security model

Binds `127.0.0.1` only. Requests must carry a loopback `Host`; `POST` requires JSON content type, ≤ 1 MiB body, a same-origin `Origin` when present and `Sec-Fetch-Site` `same-origin` or `none` when present. No CORS headers. No endpoint accepts a file path, program name or shell text; the planner CLI and generator suite are fixed at launch. Responses send `nosniff`, `no-referrer` and restrictive CSP; artifacts are served only from their own build directory with a sandboxing CSP.

## Manual notes

An existing notes sidecar is copied into each build, parsed strictly and passed unchanged to drafting; the service never edits it. Its digest is part of the publication check. A sidecar that exists but cannot be read is never treated as absent: `sync` is `blocked`, drafting fails with `manual_notes_unreadable`, and the last complete publication stays.

## Limits

POSIX only (`flock`, process groups, signals). One project per service. Progress is only what the generators emit. Tests use a labelled fake generator suite except where marked real; they check identity, integrity, sequencing and propagation, not visual or engineering quality.
