# Browser workspace (`web/`)

Current guide to the local engineering workspace served by the [interactive service](INTERACTIVE.md) at `/`. No build step, framework, CDN, web font or network access: Three.js 0.170.0 is vendored under `web/vendor/` with hashes in [web/vendor/README.md](../web/vendor/README.md) and resolved by an inline import map. Start it as in [getting started](GETTING_STARTED.md#4-interactive-five-output-workspace). Source is authoritative.

## Layout

- **Toolbar:** project name (click to rename, [project naming](PROJECT_NAMING.md)), revision, saved and notes state, overall sync badge, newest build with elapsed time, Undo/Redo with stack depths, Save, Reopen, Rebuild (offers *Force rebuild* when already current), Cancel while a build runs, Outputs menu, connection indicator, panel toggles and Focus.
- **Panes:** two side by side or a 2 × 2 grid with draggable splitters. Each pane offers **3D, P&ID, Legend, Index, Description, Outputs**; any type in any pane, duplicates allowed. Each pane owns its loader, source badge, Live/Published choice, tools and navigation; selection is shared. Pane choices last for the page session.
- **Inspector:** stable ID, current tag, P&ID zone chips, 3D mapping, typed editors, ports, observed variable, generated explanation, assumptions, unknowns and diagnostics; without a selection, a project overview.
- **Dock:** Activity (ordered build events, per-artifact status and unit counts) and Command JSON.
- **Focus:** hides global chrome and keeps pane tabs, tools and truthful badges; the exit control or Escape restores the previous panel state.
- **Ask launcher:** one draggable floating button opens the assistant bubble (below).

## Truthful status and identity

- One source of truth: `GET /api/state`, polled faster while a build runs. Every response passes a freshness gate; older revisions, sequences or requests are discarded. Each state carries the newest build's full event list, so nothing is lost on reconnect.
- **Desired versus installed.** Each view records the file the service offers now and the file actually on screen. A newer file is installed only after it loads and its embedded identity (revision and digest in the scene, SVG root, HTML body or JSON) matches what the service attached to that URL. A failed or refused load leaves the old content with a visible note.
- **Badges describe installed content.** *Current · rN* requires the verified publication's identity at the current revision; a completed but unverified build is *Unverified*; an older publication is *Stale*; withdrawn content is labelled as such. *Synchronized* appears only for `sync.state: synchronized`; `invalid` and `blocked` are error badges.
- Rejected builds (`integrity: rejected`) are shown and offer nothing.

## Live generation

Previews are the immutable files the service lists; nothing is synthesized and there are no fabricated percentages or delays. The 3D view applies cumulative previews incrementally, reusing geometry only when buffers are exactly equal; components not yet reached stay as labelled faint ghosts. Camera, paper view and selection survive every replacement.

## Selection

Clicks on 3D meshes or labels, P&ID or legend occurrences (symbols, edges, relations, port anchors) and inspector or index rows post `POST /api/selection`; shift/⌘-click adds or removes. A display mapping of `null` is shown as unknown; only an explicit `[]` is a known absence with its reason. Selected ports get a direction arrow in 3D.

## Edits

All edits are ordinary command envelopes with a fresh ID and the current `expected_revision`; 400/409 diagnostics are shown verbatim and a 409 refreshes.

- **Inspector:** rename; dimensions per declared parameter (built-in or composed, generic from the entity's own data) with a new assumption; physical placement (`move_equipment`, or `place_equipment` when unresolved); P&ID symbol position with *Pick* on the sheet.
- **Command JSON:** `insert_valve`, `insert_equipment`, `batch`, `set_route`, `set_diagram_route`, `place_equipment`, `move_equipment`, `move_symbol`, `set_parameter`, `rename_entity`. *Fill example* builds an editable envelope from the project and selection; route examples copy exact port positions from the installed scene and are starting points, not routing or clearance checks. A stale `expected_revision` disables Apply until deliberately refreshed.
- **Reroute:** a non-current route shows a *Reroute* button that calls `POST /api/reroute`; success requires a newer revision with that route current, and the note stays "outputs regenerating, not yet current" until publication.
- No delete, duplicate or line-tag rename controls exist, because the command set has none.

## Assistant: Ask and Change

One floating, nonmodal assistant with **Ask** and **Change**. Opening it changes no pane size, camera or paper view. Closing (✕, Escape, an outside click or the launcher) only hides it; drafts, proposals and running requests survive. The launcher position is a per-browser preference in `localStorage["skid.askLauncher.position.v1"]`; Shift+Arrow moves it and Shift+Home resets it.

- **Ask** sends the exact [Ask](ASK.md) request with the selected IDs, current revision and digest, and the source shown in the last-used pane. Answers are rendered as text with reference chips, unknowns, limitations, evidence and responder metadata (credential-like keys hidden). Stale answers say which revision they describe and offer an explicit refresh; nothing is resent automatically. *Copy to Change* only fills the Change draft.
- **Change** holds Undo/Redo, Reroute buttons and the composer: *Quick edit* (deterministic local parser) or *Engineering plan* (request and current project data go through the operator's Claude Code CLI to the external model; see [planning](PLANNING.md)), with an optional *Research equipment* checkbox ([creation and research](CREATION_AND_RESEARCH_UI.md)). A budget line counts characters as the service does. The plan review card shows summary, reasoning, affected items, impacts, proposed assumptions, unknowns, questions, every exact operation of the single batch, validation and requested effort. *Geometry not checked* is shown unless the service says otherwise. Apply exists only for a ready, accepted, current-revision proposal; one Undo reverts the whole plan.

## Coordinates and direct movement

- **Coordinates** (per 3D pane, off by default) show world axes, the XY grid at Z = 0 and the selected item's authoritative placement origin with its source revision. Display only.
- **Move** (per 3D pane, off by default) offers a world-axis or plane translation gizmo for one placed equipment item, and typed X/Y/Z fields; snap 0.05 m by default or off; ±100 m drag limit. It is available only online, in a Live pane showing the final scene of a complete build at exactly the current revision and digest. Release previews; Apply sends one `move_equipment` with the captured rotation. The command keeps topology and P&ID layout and marks incident routes `needs_reroute`. An unconfirmed submission is never resent; it is settled only by the command log or its fresh assumption. Escape cancels an unsubmitted preview.
- **Pipe sections:** with Move on and one line selected, a straight section can be picked and offset perpendicular to itself; Apply sends one `move_route_segment` ([pipe sections](PIPE_SEGMENTS.md)). The browser checks geometry rules locally but cannot run the equipment-body screen, so such refusals arrive on Apply.

Escape order: cancel a move preview → cancel a sheet pick → close the assistant → leave focus → restore maximize → clear selection. No letter shortcut fires while typing.

## Documents and downloads

P&ID and legend SVGs are sanitized again on import and identity-checked. The Index shows Tag / Item / Sheet-zone from `item-index.json` and cross-checks the drawing inventory; zones are never computed by the UI. The Description is rendered inert in a sandboxed iframe ([process narrative](PROCESS_NARRATIVE.md#html)). Outputs lists the five required outputs and four supporting files with status, revision, build, digest and download; a non-published file is labelled as a preview. STEP and recipe are illustrative prototype geometry.

## 3D presentation

Engineering geometry is exactly the scene tessellation (world metres, Z up). Lights, environment, floor grid, contact shadow, feature edges and screen-space labels are display-only additions. Labels are not depth-occluded.

## Tests

`node --test "tests/browser/unit/*.test.mjs"` covers status rules, loaders, badges, scene diff, gestures, command builders, plan review, assistant, move coordinators, pipe sections, research rendering and static checks. Browser scenarios in `tests/browser/*.e2e.mjs` need Playwright and start their own service; scenarios with planners or research use labelled fakes ([getting started](GETTING_STARTED.md#5-run-the-checks)).

## Limits

Pane layout is not persisted across reloads. Four 3D panes use four WebGL contexts. Translation of one item only; no rotation, scale or multi-item move. The assistant bubble can cover controls on small screens. Long tags in narrow description tables can wrap at a hyphen.
