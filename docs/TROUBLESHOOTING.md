# Troubleshooting

The software reports incomplete work rather than hiding it. Most "problems" are deliberate states: something is open and needs an explicit decision. This page explains how to read them.

## Status badges and what to do

| State (`sync.state` in the service) | Meaning | What to do |
| --- | --- | --- |
| `synchronized` | The verified five-output publication matches the current revision and digest. | Nothing. |
| `building` | A build of the current revision is queued or running. Displayed outputs are working previews or the previous publication, each labelled with its own revision. | Wait, or cancel. Navigation and edits stay available. A newer edit supersedes the running build. |
| `pending` | The current revision is valid, but has open items: unplaced equipment, routes or diagram paths that are `needs_reroute`/`unresolved`, deliberately open required ports, or a route that enters an equipment body. The last complete publication stays current for its own older revision. | Resolve the listed items: place equipment, supply or reroute routes, set diagram paths, connect ports. A pending set never becomes synchronized by waiting. |
| `failed` | A generator or verification failed for the current revision. The diagnostics name the artifact and the cause. A typical cause is a dimension outside a recipe's supported range, which fails CAD explicitly. | Read the diagnostics, change the input or undo. The previous publication is untouched. |
| `cancelled` | The current revision's build was cancelled. | Start a build again. |
| `stale` | The newest publication is from another revision or digest, for example after an external edit or a changed notes sidecar. | Build. |
| `unpublished` | Nothing complete has been published yet, for example for a new project with open items. | Resolve open items and build. |
| `invalid` | A published file or the pointer no longer verifies (changed, missing or corrupted). That build is withdrawn from the displayed outputs. | Restore the file or rebuild. The service never rewrites a corrupted publication. |
| `blocked` | The manual-notes sidecar exists but cannot be read (permissions, a directory, a dangling link). | Fix or remove `<stem>.notes.json`. A sidecar that is present but unreadable is never treated as absent. |

Each pane badge describes the content actually installed in that pane, not the file the page wants next. A pane can therefore show an older revision while a download is in progress. Anything whose source revision differs from the project revision is labelled.

## Command refusals

| Diagnostic | Cause | Resolution |
| --- | --- | --- |
| `stale_revision` (CLI exit 4, HTTP 409) | `expected_revision` is not the current revision. | Re-read the document (`skid.cli validate --json`, or `/api/state`), check what changed, and resubmit with the new revision. Do not blindly increment the revision. |
| `revision_conflict` (exit 4, 409) | Another writer saved between your read and your save. | As above. Nothing was written. |
| `duplicate_tag`, `duplicate_id` | The tag collides after whitespace collapsing and case folding, or an ID is already used anywhere in the project. | Choose another tag or a fresh ID. |
| `unsupported_nominal_diameter`, `dn_incompatible`, `dn_mismatch` | Only DN50 is supported, and a whole connection must agree. | Use DN50. |
| `wrong_port_direction`, `duplicate_port_use`, `self_connection` | A line must go from an `out`/`both` port to an `in`/`both` port, and each port takes one line. Branches need explicit junction equipment, which is not yet available. | Pick other ports. |
| `missing_provenance`, `unreferenced_assumption`, `assumption_conflict` | Each quantity needs an `assumption_id` or `source`. Assumptions added by a command must be used, and existing assumptions cannot be overwritten. | Add or reference a fresh assumption. |
| `route_intrusion`, `route_clearance_unavailable` | The route's conservative envelope enters a placed equipment body, or a body cannot be bounded. | Choose other points or reroute. The screen is deliberately conservative. |
| `invalid_route` | Route endpoints are not on the port frames (within 1e-6 m), or a diagram path does not end on the symbol anchors. | Recompute endpoints from the current placement. |
| `required_port_unconnected` (error) | A required port is unconnected and carries no `connection_status: "unresolved"` marker. | Connect it. Ports added with `add_equipment` are marked automatically. |
| `no_op` | Nothing changed. | None. No revision, history or build was created. |

## Undo looks like it went "forward"

Undo and redo restore content at a **new, higher** revision. After revision 6, undo creates revision 7 with the content of revision 5. This is intended: revisions identify states in time and never decrease, so generated outputs and external writers can always tell what is newer.

## Generated files and pointers

- Edit only the project JSON (through commands). Generated directories (`*.workflow/`, `*.skid/`) are disposable and ignored by Git. Never hand-edit a published build. The integrity check will mark it `invalid`.
- `current.json` is the only publication pointer. A build directory that exists is not necessarily published. Unsealed directories from an interrupted service are reported and never published.
- The standalone `python -m skid.geometry build` exits `0` for pending manifests too. Read its printed statuses. That exit code does not mean "synchronized".

## Environment

### Python and CAD

- `ModuleNotFoundError: cadquery`: the CAD requirements are not installed in the interpreter that started the service. The service still runs, but every build fails with `generator_unavailable`/`generator_fault`. Install `requirements-cad.txt` in that environment.
- `pip` cannot resolve `constraints-cad.txt` on your platform: the constraints record one audited macOS arm64 / Python 3.12 environment. Report the platform and the error; do not silently drop the constraints for evidence runs.
- Tests that report `skipped`: CAD and Blender tests skip with a reason when their runtime is missing. A run with skips is not a full run.
- The Node unit test `pipe-sections` cross-checks against Python via `SKID_PYTHON` (default `.venv-cad/bin/python`) and skips that case if the interpreter is absent.

### Service

- `address already in use`: choose another `--port`, or `--port 0` for a free one.
- Refuses to start on a project under `examples/`: copy it first with `skid.cli sample`.
- Refuses a workflow directory: one service per output root (`.service.lock`), and a non-empty directory owned by another project is refused.
- A request from another host name or origin gets `403`: the service accepts only `127.0.0.1` / `localhost` on its own port.
- Disk usage grows: start the service with `--keep-builds N`.

### Blender

- `no Blender executable`: pass `--blender PATH` or set `SKID_BLENDER`.
- Blender crashes at background start-up inside a sandbox or a GPU-less container: run it where it can initialise its graphics backend. An exit code of zero alone does not count as success; both manifests must verify.

### Engineering plan and research

| Code | Meaning |
| --- | --- |
| `planner_auth` | `claude auth status --json` did not report `authMethod: claude.ai` and `subscriptionType: max`. |
| `planner_api_key_source` | The CLI reported an API-key source other than `none`, so it was not using the subscription login. |
| `planner_model_mismatch` | A reply came from a model other than `claude-opus-5-5`. Nothing is substituted. |
| time-out, cancelled or malformed | No partial output is used. The plan has no Apply action. |

If `claude` is not found, pass `--planner-cli /path/to/claude`. A refused plan never changes the project. Quick edit, local Ask, typed edits and structured commands keep working.

### Browser checks

- Use Node.js 22, the verified target (`node --version`). Support for the quoted glob in `node --test "tests/browser/unit/*.test.mjs"` differs between Node versions, and older versions are untested.
- `playwright is not installed`: run `npm ci` in the repository root, then `npx playwright install chromium`.
- No WebGL in headless Chromium: set `SKID_BROWSER_ARGS` to a JSON array of flags suitable for your machine. The defaults are ANGLE/Metal on macOS and SwiftShader elsewhere.

## Reporting a problem

Include the commit, OS, Python version, `pip freeze` (for CAD issues), the exact command, and its full output. Use a project you are allowed to share. Remove account details from any CLI output. See [CONTRIBUTING.md](../CONTRIBUTING.md) and, for vulnerabilities, [SECURITY.md](../SECURITY.md).
