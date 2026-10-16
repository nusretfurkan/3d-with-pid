# Assistant and direct 3D interaction contracts

Current summary of the interfaces shared by the read-only Ask transport, the workspace assistant, per-pane coordinates and direct equipment movement. Behaviour details: [Ask](ASK.md), [UI](UI.md#assistant-ask-and-change), [pipe sections](PIPE_SEGMENTS.md). Source is authoritative (`skid/assistant/`, `skid/interactive/service.py`, `web/js/assistant*.js`, `web/js/move-coordinator.js`, `web/js/viewer3d.js`).

## Read-only Ask transport

- `POST /api/ask` accepts exactly `request_id`, `question`, `selected_ids`, `expected_revision`, `input_digest`, and optional `history` and `displayed_source`. IDs must resolve in the frozen snapshot; no HTML, URL or command fields are accepted.
- The response is exactly `{id, request_id, project_id, source_revision, input_digest, current_revision, stale, status, answer, references, unknowns, limitations, responder, displayed_source}`. `answer` is inert text. `references` are verified snapshot records built by the service, never by the model. Answers never carry operations, commands or apply fields.
- Currentness is checked against the **persisted** project file before execution and again after answering, without adopting, saving or building. An initially mismatched source is 409 `stale_ask`; an unreadable source fails closed. A changed source after answering is labelled `stale`, with `current_revision` `null` if it cannot be verified. Nothing is resent automatically.
- One model Ask at a time; local deterministic answers do not depend on model availability. No project lock is held during a model run. `POST /api/cancel-ask {request_id}` signals only that Ask and returns a narrow acknowledgement, never the full state.
- The model path reuses the existing planner bridge object unchanged (exact `claude-opus-5-5`, requested effort `medium`, tool-free) with an Ask-specific strict schema. Its configuration is not changeable through Ask.

## Assistant ownership (browser)

- `createAssistant(app, root, composerRoot)` owns one page-session Ask thread, draft and running request, and the Ask/Change switch. The existing composer is created once and moved into Change, so its controller, draft, proposal and pending request survive opening, closing, mode switches and focus.
- The assistant is nonmodal and never replaces a pane or remounts a viewer. Closing preserves state. The thread and drafts reset when the project ID or service run changes.
- Escape priority: active move or pick cancellation, then closing the assistant, then existing focus, maximize and selection actions. No shortcuts fire while typing.

## Per-pane coordinates and the move adapter

- `pane.nav['3d'].coordinatesOn` defaults to false; `Viewer3D.setCoordinates(on)` and `viewState().coordinatesOn` manage it. Coordinates are display only.
- The viewer receives authoritative context through `viewer.setEngineeringContext({project, inputDigest, online, mode, installedSource, move})`, where `installedSource` is the content actually installed in that pane, not the desired file.
- A move may start only when online, in a Live pane, on the complete final scene of exactly the current revision and digest, for an independently placed equipment item whose authoritative placement equals the captured base.
- Viewer callbacks: `onMoveBegin({entityId, position, sourceRevision, inputDigest})` (returns a boolean), `onMovePreview(position)`, `onMoveCancel(reason)`, `onMoveCommit(position)`. The app exposes `beginEquipmentMove`, `previewEquipmentMove`, `cancelEquipmentMove` and `commitEquipmentMove` and owns one frozen record `app.equipmentMove = {paneIndex, entityId, sourceRevision, inputDigest, basePosition, position, rotation, phase}` with phase `preview | submitting | accepted | uncertain`.
- Viewer tools: `setMoveEnabled(on)`, `setMoveSnap(stepMetresOrNull)` (default 0.05 m), `cancelMove(reason)`, `moveSnapshot()`. World translation only; existing rotation is preserved; every owned mesh, edge, label and port moves by one world delta from immutable geometry. The vendored Three.js version is not upgraded for this.
- An unsubmitted preview is cancelled by a pane or source change, hidden origin pane, stale revision, disconnect, pointer cancel or Escape. Escape cannot revoke a submitted command.

## Commit rules

The app commits through the existing transport (`POST /api/command`), not a new endpoint: one `move_equipment` envelope with the captured rotation, `expected_revision` equal to the base revision, and a fresh manual-layout assumption. A no-op or cancel sends nothing; a submission locks further moves. An uncertain transport outcome refetches state and reconciles by command ID or fresh assumption ID, never by resending. The existing command keeps topology and P&ID layout, marks incident routes `needs_reroute`, and goes through the normal publication, reroute, history, save and reopen paths. Undo, Redo and Reroute stay reachable in focus through the assistant.

## Boundaries

No project-schema, core-command or planner-contract changes are part of this slice. Ask writes nothing and starts no build.
