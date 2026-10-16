// Workspace controller. One source of truth: the local service's /api/state.
// Polling and every mutation response pass the same freshness gate (status.js).
// The views are independent panes (panes.js): each pane has its own latest-wins
// loader (loader.js) that separates the file the service offers from the file
// actually installed on screen, and its badge describes the installed file as the
// service describes it now (view-source.js). Panes share the semantic selection and
// the immutable artifact caches; layout, pane choices and focus are presentation
// only and never touch the project. The assistant (assistant.js) is a nonmodal bubble
// opened from a draggable floating Ask launcher (floating-assistant.js), with read-only
// Ask and the one Change composer; it reserves no space beside the panes. Direct
// equipment movement in 3D panes is coordinated by one global record (move-coordinator.js)
// that ends in at most one existing move_equipment command through submitCommand. A straight
// section of one selected line (a local selection under that line, app.section) is moved through a
// separate coordinator (pipe-move-coordinator.js) ending in one move_route_segment; equipment and
// section edits block each other.

import { getJSON, postJSON, getText, ApiError } from './api.js';
import {
  ACTIVE_STATUSES, appliedMarker, buildElapsed, formatSeconds, shortDigest, shouldApplyState, syncBadge, viewSource,
} from './status.js';
import { createLoader } from './loader.js';
import { checkIdentity } from './view-source.js';
import { buildPlacement, buildRouteSegmentMove, envelope } from './commands-model.js';
import { createMoveCoordinator } from './move-coordinator.js';
import { createPipeMoveCoordinator, pipeCapable } from './pipe-move-coordinator.js';
import { routeKey } from './pipe-sections.js';
import { $, $$, h, icon, replace } from './dom.js';
import { createInspector } from './inspector.js';
import { createActivity } from './activity.js';
import { createComposer } from './composer.js';
import { createAssistant } from './assistant.js';
import { createFloatingLauncher } from './floating-assistant.js';
import { createCommandPanel } from './command-panel.js';
import { Pane } from './panes.js';
import {
  createLayout, enterFocus, escapeAction, exitFocus, layoutIndices, setLayout, setPaneMode, setPaneType, toggleMaximized,
  visibleIndices,
} from './pane-model.js';
import { createNameEditor } from './name-editor.js';
import { rerouteBuildNote, rerouteOffer, rerouteOutcome, rerouteRequest } from './reroute-model.js';
import { researchOffered } from './research-model.js';

const POLL_ACTIVE_MS = 300;
const POLL_IDLE_MS = 1500;
const ENGINEERING_TIMEOUT_MS = 15 * 60 * 1000;
const NOTICE_CODES = new Set(['external_change', 'reopened', 'build_not_needed', 'generator_unavailable',
  'interrupted_builds', 'disk_invalid', 'manual_notes_unreadable']);

const app = {
  state: null,
  applied: null,
  reqSeq: 0,
  online: null,
  failures: 0,
  pending: null,          // optimistic selection {ids, token}
  selectionKey: null,
  selectionIds: [],
  selectionResolved: null,
  seenNotices: new Set(),
  stats: { applied: 0, discardedStates: 0 },
  layout: createLayout(),
  panes: [],
  hoveredPane: null,
  workedPane: null,       // pane of the last pointer press, wheel or keyboard focus
  loaders: {},            // inspector companions: item index and description data (Live)
  docData: { index: null, descriptionJson: null },
  reroutes: new Map(),    // connection id -> {status: 'busy'|'error'|'done', message, toRevision, fromRevision}
  rerouteVersion: 0,
  section: null,          // local straight-section selection under the one selected line (never sent)
};
// read-only facts for automated checks; nothing here mutates state
window.skidWorkspace = {
  snapshot: () => snapshot(),
  /** Client point of a component's centre in the given pane (index or root ID), else the primary 3D pane. */
  screenPointOf: (componentId, pane) => {
    const viewer = pane == null ? app.viewer : paneOf(pane)?.content?.viewer;
    return viewer ? viewer.screenPointOf(componentId) : null;
  },
  /** Client point of a world point (metres) in the given pane, else the primary 3D pane. */
  screenPointOfWorld: (point, pane) => {
    const viewer = pane == null ? app.viewer : paneOf(pane)?.content?.viewer;
    return viewer ? viewer.screenPointOfWorld(point) : null;
  },
  /** A pane's viewer facts as plain data, including coordinates and move offsets/handles (read-only). */
  viewerInfo: (pane) => {
    const viewer = paneOf(pane)?.content?.viewer;
    if (!viewer) return null;
    const info = viewer.debugInfo();
    return JSON.parse(JSON.stringify({ ...info, coordinates: info.coordinates, move: info.move }));
  },
};

function paneOf(ref) {
  if (typeof ref === 'number') return app.panes[ref] || null;
  return app.panes.find((p) => p.el.id === ref) || null;
}

/** First pane of a type in screen order (visible panes first, then the rest of the layout). */
function primaryPane(type) {
  const order = [...visibleIndices(app.layout), ...layoutIndices(app.layout)];
  for (const i of order) {
    const p = app.panes[i];
    if (p && p.type === type && p.content) return p;
  }
  return null;
}
app.primaryPane = primaryPane;
Object.defineProperty(app, 'viewer', {
  get() {
    const order = [...visibleIndices(app.layout), ...layoutIndices(app.layout)];
    for (const i of order) {
      const p = app.panes[i];
      if (p && p.type === '3d' && p.content && p.content.viewer) return p.content.viewer;
    }
    return null;
  },
});

// ---------------------------------------------------------------- utilities

export function toast(kind, title, lines = [], actions = []) {
  const el = h('div', { class: `toast ${kind}` }, h('b', null, title));
  const list = (Array.isArray(lines) ? lines : [lines]).filter(Boolean);
  if (list.length === 1) el.append(h('div', null, list[0]));
  else if (list.length) el.append(h('ul', null, list.slice(0, 6).map((l) => h('li', null, l))));
  if (actions.length) {
    el.append(h('div', { class: 'btn-row' }, actions.map((a) => h('button', {
      class: 'btn small', onclick: () => { el.remove(); a.run(); },
    }, a.label))));
  }
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 12000 : actions.length ? 10000 : 5200);
}

function diagnosticLines(diagnostics) {
  return (diagnostics || []).map((d) => `[${d.code}] ${d.message}`);
}

function reportError(error, label) {
  if (!(error instanceof ApiError)) {
    toast('err', label, [String(error && error.message ? error.message : error)]);
    return;
  }
  if (error.network) {
    toast('err', label, [error.error]);
    return;
  }
  const kind = error.status === 409 ? 'warn' : 'err';
  const head = error.status === 409 ? `${label}: conflict` : `${label}: rejected`;
  toast(kind, head, [error.error, ...diagnosticLines(error.diagnostics)]);
}

/** Small shared cache of immutable artifacts; concurrent requests for one URL share a fetch. */
function immutableCache(limit, load) {
  const cache = new Map();
  return (url) => {
    if (cache.has(url)) {
      const hit = cache.get(url);
      cache.delete(url);
      cache.set(url, hit); // most recently used last
      return hit;
    }
    const promise = load(url);
    cache.set(url, promise);
    promise.catch(() => { if (cache.get(url) === promise) cache.delete(url); });
    while (cache.size > limit) cache.delete(cache.keys().next().value);
    return promise;
  };
}
app.cachedJSON = immutableCache(12, (url) => getJSON(url, 30000));
app.cachedText = immutableCache(8, (url) => getText(url));
// Scenes are large (MBs): keep the few most recent, enough for Live/Published in several panes.
app.sceneJSON = immutableCache(3, (url) => getJSON(url, 60000));

// ---------------------------------------------------------------- transport

let pollTimer = null;
/** Fetch /api/state through the freshness gate; {ok} says whether the service answered. */
async function poll() {
  clearTimeout(pollTimer);
  const seq = ++app.reqSeq;
  let ok = false;
  try {
    const state = await getJSON('/api/state', 8000);
    setOnline(true);
    receive(state, seq);
    ok = true;
  } catch (error) {
    setOnline(false, error);
  }
  schedule();
  return { ok };
}

function schedule() {
  clearTimeout(pollTimer);
  let delay;
  if (!app.online) delay = Math.min(5000, 1000 * 2 ** Math.min(app.failures - 1, 3));
  else {
    const s = app.state;
    const active = s && ((s.build && ACTIVE_STATUSES.has(s.build.status)) || s.active_build);
    delay = active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  }
  pollTimer = setTimeout(poll, delay);
}

function setOnline(online, error) {
  const was = app.online;
  app.online = online;
  app.failures = online ? 0 : app.failures + 1;
  const conn = $('#tb-conn');
  conn.className = `conn ${online ? 'online' : 'offline'}`;
  conn.title = online ? 'Connected to the local service' : `Not connected: ${error ? error.error || error.message : ''}`;
  const exit = $('#btn-exit-focus');
  exit.classList.toggle('offline', !online);
  exit.title = online ? 'Exit focus (Esc)' : 'Exit focus (Esc) · the local service is not reachable; views show the last received state';
  const banner = $('#conn-banner');
  if (online) {
    banner.hidden = true;
    if (was === false) toast('ok', 'Reconnected', ['Full state reloaded from the service.']);
  } else {
    banner.hidden = false;
    banner.textContent = app.state
      ? `Service unreachable — showing revision ${app.state.project.revision} as last received; retrying…`
      : 'Waiting for the local service at this address…';
  }
  if (was !== online && app.assistant) app.assistant.refresh();
  if (was !== online && app.moves) {
    app.moves.onlineChanged(online); // an unsubmitted move preview ends when the service is lost
    app.pipeMoves.onlineChanged(online);
    for (const pane of app.panes) if (pane.content && pane.content.pushContext) pane.content.pushContext();
  }
}

/** Apply a state payload if it is not older than the one on screen. */
function receive(state, seq) {
  if (!shouldApplyState(app.applied, state, seq)) {
    app.stats.discardedStates += 1;
    return false;
  }
  const previous = app.state;
  const restarted = previous && previous.service && state.service && previous.service.started_at !== state.service.started_at;
  app.state = state;
  app.applied = appliedMarker(state, seq);
  app.stats.applied += 1;
  if (restarted) {
    app.selectionKey = null;
    toast('warn', 'Service restarted', [`The service was restarted at ${state.service.started_at}; state reloaded.`]);
  }
  render(previous, state);
  return true;
}

async function mutate(path, body, label, { quiet = false } = {}) {
  const seq = ++app.reqSeq;
  try {
    const state = await postJSON(path, body);
    setOnline(true);
    receive(state, seq);
    schedule();
    return { ok: true, state };
  } catch (error) {
    if (error instanceof ApiError && error.network) setOnline(false, error);
    if (!quiet) reportError(error, label);
    if (error instanceof ApiError && (error.status === 409 || error.network)) poll();
    return { ok: false, error };
  }
}

/** Send one command envelope through the validated command path. */
app.submitCommand = async (command, label, options = {}) => {
  const result = await mutate('/api/command', command, label || command.type, options);
  if (result.ok) {
    const last = result.state.last_command;
    if (last && last.command_id === command.id) {
      if (last.status === 'no_op') toast('warn', `${command.type}: no change`, ['The command changed nothing; the revision was kept.']);
      else {
        const warnings = (last.warnings || []).map((d) => d.message);
        toast(warnings.length ? 'warn' : 'ok', `${label || command.type}: revision ${last.from_revision} → ${last.to_revision}`,
          warnings.length ? warnings : ['Accepted and saved; regeneration queued.']);
      }
    }
  }
  return result;
};

/**
 * Ask for a reviewable proposal. Quick mode (the default) sends the original request body; engineering
 * mode adds mode: "engineering" and waits longer, because the service runs the local planner (whose own
 * limit is 600 s) before it answers. Polling, selection and navigation continue meanwhile; nothing is
 * changed until a reviewed proposal is applied. `signal` stops waiting without touching the service.
 */
/**
 * Wait at least as long as the service says its planner (and, for a researched plan, its researcher
 * before it) may run, plus margin for its checks.
 */
function modelWaitMs(research = false) {
  const planning = app.state && app.state.planning;
  const plannerLimit = planning && planning.planner ? Number(planning.planner.timeout_s) : NaN;
  const researcher = research && planning && planning.research && planning.research.researcher;
  const researchLimit = researcher ? Number(researcher.timeout_s) : NaN;
  const limit = (Number.isFinite(plannerLimit) ? plannerLimit : 0) + (research ? (Number.isFinite(researchLimit) ? researchLimit : 600) : 0);
  return Math.max(ENGINEERING_TIMEOUT_MS, limit ? (limit + 180) * 1000 : 0);
}

/**
 * `research: true` asks the service to research the equipment first (engineering mode only). It is sent
 * only when requested and only to a service that advertises it: an older service rejects unknown fields,
 * so `research` is never sent as false.
 */
app.propose = async (text, mode = 'quick', { signal, research = false } = {}) => {
  const revision = app.state.project.revision;
  const body = { text, expected_revision: revision };
  if (mode && mode !== 'quick') body.mode = mode;
  const researched = research === true && mode === 'engineering' && researchOffered(app.state);
  if (researched) body.research = true;
  const label = mode === 'engineering' ? (researched ? 'Researched engineering plan' : 'Engineering plan') : 'Proposal';
  const timeout = mode !== 'engineering' ? 30000 : modelWaitMs(researched);
  try {
    return { ok: true, proposal: await postJSON('/api/propose', body, timeout, { signal }) };
  } catch (error) {
    if (error && error.aborted) return { ok: false, aborted: true, error };
    reportError(error, label);
    if (error instanceof ApiError && error.status === 409) poll();
    return { ok: false, error };
  }
};

app.applyProposal = async (proposal) => mutate('/api/apply-proposal', { proposal_id: proposal.id, expected_revision: proposal.expected_revision }, 'Apply proposal');

/** Stop a running engineering plan on the service (its planner process is terminated; no command results). */
app.cancelPlan = async (proposalId) => mutate('/api/cancel-proposal', { proposal_id: proposalId }, 'Cancel plan');

/**
 * Read-only question (POST /api/ask, docs/ASSISTANT_3D_CONTRACTS.md). The reply is never a
 * state and never passes through mutate(); errors are returned for the assistant to show
 * inline. A broader question may run the model, so the wait matches an engineering plan.
 */
app.ask = async (body, { signal } = {}) => {
  try {
    return { ok: true, answer: await postJSON('/api/ask', body, modelWaitMs(), { signal }) };
  } catch (error) {
    if (error && error.aborted) return { ok: false, aborted: true, error };
    if (error instanceof ApiError && error.status === 409) poll();
    return { ok: false, error };
  }
};

/**
 * Signal only that Ask on the service; its reply then reports status "cancelled". When the
 * page is going away the signal is sent with keepalive and nothing waits for it.
 */
app.cancelAsk = async (requestId, { leaving = false } = {}) => {
  if (leaving) {
    try {
      fetch('/api/cancel-ask', {
        method: 'POST', keepalive: true, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: requestId }),
      }).catch(() => {});
    } catch { /* best effort while unloading */ }
    return { ok: true };
  }
  try {
    await postJSON('/api/cancel-ask', { request_id: requestId }, 8000);
    return { ok: true };
  } catch (error) {
    // an empty 2xx body is still an accepted cancellation
    return error instanceof ApiError && error.status >= 200 && error.status < 300 ? { ok: true } : { ok: false, error };
  }
};

// ---------------------------------------------------------------- rerouting

function rerouteChanged() {
  app.rerouteVersion += 1;
  app.inspector.update(app.state);
  app.assistant.routesChanged();
}

/**
 * Ask the service to compute and commit a guided route for one connection (POST /api/reroute,
 * current expected revision). One request at a time; the reply's project decides success and
 * the build note reports the five outputs honestly until they are published.
 */
app.reroute = async (connectionId) => {
  const s = app.state;
  if (!s || [...app.reroutes.values()].some((r) => r.status === 'busy')) return;
  const offer = rerouteOffer(s.project, connectionId);
  if (!offer.actionable) return;
  const conn = s.project.model.connections[connectionId];
  const tag = conn ? conn.tag : connectionId;
  const fromRevision = s.project.revision;
  app.reroutes.set(connectionId, { status: 'busy', fromRevision, startedAt: Date.now() });
  rerouteChanged();
  const result = await mutate('/api/reroute', rerouteRequest(connectionId, fromRevision), `Reroute ${tag}`, { quiet: true });
  if (!result.ok) {
    const e = result.error;
    const detail = e instanceof ApiError
      ? [e.error, ...(e.diagnostics || []).map((d) => d.message)].filter(Boolean).join(' — ')
      : String(e && e.message ? e.message : e);
    const conflict = e instanceof ApiError && e.status === 409;
    const missing = e instanceof ApiError && (e.status === 404 || e.status === 405);
    const message = missing ? `The service does not offer rerouting (HTTP ${e.status}).` : conflict ? `Conflict: ${detail}` : detail;
    app.reroutes.set(connectionId, { status: 'error', fromRevision, message });
    toast(conflict ? 'warn' : 'err', `Reroute ${tag}: ${conflict ? 'conflict' : 'not routed'}`,
      [message, 'Nothing was changed by this request; the route stays pending.']);
    rerouteChanged();
    return;
  }
  const outcome = rerouteOutcome(fromRevision, result.state, connectionId);
  if (outcome.routed) {
    app.reroutes.set(connectionId, { status: 'done', fromRevision, toRevision: outcome.toRevision, mode: outcome.mode });
    const note = rerouteBuildNote(app.state, outcome.toRevision);
    toast('ok', `${tag} rerouted (${outcome.mode || 'guided'}): revision ${fromRevision} → ${outcome.toRevision}`,
      [note ? note.text : '', pipeCapable(result.state)
        ? 'Prototype route, screened only against conservative equipment-body boxes. Other pipes, supports, stress and fabrication are not checked.'
        : 'Prototype route from an older service; see the route assumption for what it checked. Other pipes, supports, stress and fabrication are not checked.']);
  } else {
    app.reroutes.set(connectionId, { status: 'error', fromRevision, message: `The service answered, but ${tag} is still ${outcome.status || 'not routed'} at revision ${outcome.toRevision}.` });
    toast('warn', `Reroute ${tag}: no current route`, [`Route status ${outcome.status || 'none'} at revision ${outcome.toRevision}.`]);
  }
  rerouteChanged();
};

/** UI state of one connection's Reroute control (busy/error/done), for the inspector. */
app.rerouteState = (connectionId) => {
  const rec = app.reroutes.get(connectionId) || null;
  const anyBusy = [...app.reroutes.values()].some((r) => r.status === 'busy');
  const note = rec && rec.status === 'done' && app.state ? rerouteBuildNote(app.state, rec.toRevision) : null;
  return { record: rec, anyBusy, note, online: app.online !== false };
};

// ---------------------------------------------------------------- direct equipment movement

/** What the move coordinator may know about a pane (read-only). */
function moveFacts(pane) {
  if (!pane || !pane.content) return null;
  const c = pane.content;
  const viewer = c.type === '3d' ? c.viewer : null;
  const shown = viewer && viewer.source ? viewer.source : null; // the scene the viewer has actually loaded
  return {
    index: pane.index, type: pane.type, mode: pane.mode, active: !!pane.active && !pane.el.hidden,
    installed: c.installedNow ? c.installedNow() : c.loader ? c.loader.state().installed : null,
    viewerSource: shown ? { url: shown.url, source_revision: shown.source_revision, input_digest: shown.input_digest, final: shown.final === true } : null,
  };
}

app.moves = createMoveCoordinator({
  getState: () => app.state,
  isOnline: () => app.online === true,
  pane: (index) => moveFacts(app.panes[index]),
  panes: () => app.panes.map(moveFacts),
  refetch: () => poll(),
  submit: (command, label) => app.submitCommand(command, label, { quiet: true }),
  buildCommand: buildPlacement,
  publish: () => {
    for (const pane of app.panes) if (pane.content && pane.content.moveChanged) pane.content.moveChanged();
  },
  cancelViewer: (index, reason) => {
    const viewer = app.panes[index] && app.panes[index].content ? app.panes[index].content.viewer : null;
    if (viewer) viewer.cancelMove(reason);
  },
  notify: (kind, title, lines) => toast(kind, title, lines),
  otherActive: () => (app.pipeMoves ? app.pipeMoves.record() : null),
});
/** The one global move record {paneIndex, entityId, sourceRevision, inputDigest, basePosition, position, rotation, phase} or null. */
Object.defineProperty(app, 'equipmentMove', { get: () => app.moves.record() });

/** A pane's viewer (or its typed X/Y/Z) asks to start a move; synchronous yes/no. */
app.beginEquipmentMove = (paneIndex, intent, options) => app.moves.begin(paneIndex, intent, options);
app.previewEquipmentMove = (position, paneIndex = null) => app.moves.preview(position, paneIndex);
/** Ends an unsubmitted preview only; a submitted command cannot be revoked. */
app.cancelEquipmentMove = (reason, paneIndex = null) => app.moves.cancel(reason, paneIndex);
/** One existing move_equipment envelope through the validated command transport (or nothing). */
app.commitEquipmentMove = (position, paneIndex = null) => app.moves.commit(position, paneIndex);
/** Hide the preview of an unconfirmed move; nothing is cancelled or resent, and it stays watched. */
app.dismissEquipmentMove = (paneIndex = null) => app.moves.dismiss(paneIndex);
app.movePaneChanged = () => panesChanged();

function panesChanged() {
  app.moves.paneChanged();
  app.pipeMoves.paneChanged();
}

// ---------------------------------------------------------------- straight pipe sections

function repaintMoves() {
  for (const pane of app.panes) if (pane.content && pane.content.moveChanged) pane.content.moveChanged();
}

app.pipeMoves = createPipeMoveCoordinator({
  getState: () => app.state,
  isOnline: () => app.online === true,
  pane: (index) => moveFacts(app.panes[index]),
  panes: () => app.panes.map(moveFacts),
  refetch: () => poll(),
  submit: (command, label) => app.submitCommand(command, label, { quiet: true }),
  buildCommand: buildRouteSegmentMove,
  publish: () => repaintMoves(),
  cancelViewer: (index, reason) => {
    const viewer = app.panes[index] && app.panes[index].content ? app.panes[index].content.viewer : null;
    if (viewer) viewer.cancelPipeMove(reason);
  },
  notify: (kind, title, lines) => toast(kind, title, lines),
  otherActive: () => app.moves.record(),
});
/** The one global section record {paneIndex, connectionId, segmentIndex, ..., offset, points, phase} or null. */
Object.defineProperty(app, 'pipeSectionMove', { get: () => app.pipeMoves.record() });
app.beginPipeMove = (paneIndex, intent, options) => app.pipeMoves.begin(paneIndex, intent, options);
app.previewPipeMove = (offset, paneIndex = null) => app.pipeMoves.preview(offset, paneIndex);
app.cancelPipeMove = (reason, paneIndex = null) => app.pipeMoves.cancel(reason, paneIndex);
app.commitPipeMove = (paneIndex = null) => app.pipeMoves.commit(paneIndex);
app.dismissPipeMove = (paneIndex = null) => app.pipeMoves.dismiss(paneIndex);

/** Set (or clear) the local section selection; a preview of another section ends. */
function setSection(section) {
  const same = (a, b) => (!a && !b) || (!!a && !!b && a.connectionId === b.connectionId && a.segmentIndex === b.segmentIndex
    && a.sourceRevision === b.sourceRevision && a.inputDigest === b.inputDigest && a.routeKey === b.routeKey);
  if (same(app.section, section)) return false;
  app.section = section ? Object.freeze({ ...section }) : null;
  app.pipeMoves.sectionChanged(app.section);
  return true;
}

/** Select one straight section of a line (the list in a pane's section panel); the line becomes the selection. */
app.selectSection = (connectionId, segmentIndex) => {
  const s = app.state;
  if (!s || !(connectionId in s.project.model.connections)) return;
  const key = routeKey(s.project, connectionId);
  if (!key) return;
  if (!(app.selectionIds.length === 1 && app.selectionIds[0] === connectionId)) app.select([connectionId], 'display');
  const changed = setSection({
    connectionId, segmentIndex, sourceRevision: s.project.revision, inputDigest: s.document ? s.document.input_digest : null, routeKey: key,
  });
  if (changed) repaintMoves();
};

/** A section index is scoped to the exact route points: it survives other edits and ends when the route changes. */
function revalidateSection(state) {
  const sec = app.section;
  if (!sec) return;
  const key = routeKey(state.project, sec.connectionId);
  if (key !== sec.routeKey) setSection(null);
  else setSection({ ...sec, sourceRevision: state.project.revision, inputDigest: state.document ? state.document.input_digest : null });
}

// ---------------------------------------------------------------- selection

function knownId(project, id) {
  const m = project.model;
  return id in m.entities || id in m.ports || id in m.connections;
}

/**
 * Local mapping of semantic IDs to the P&ID occurrences and the 3D components of one viewer.
 * The service's verified manifest mapping is used when it describes the scene that viewer
 * shows (an explicit [] is a known absence); otherwise (null = unknown, or another build is
 * shown) the viewer's installed scene's own component semantic IDs are used.
 */
function highlightFor(ids, resolved, viewer) {
  const project = app.state.project;
  const occurrences = new Set();
  const components = new Set();
  const ports = [];
  const byId = new Map((resolved || []).map((r) => [r.id, r]));
  const shownBuild = viewer && viewer.source ? viewer.source.build_id : null;
  for (const id of ids) {
    const r = byId.get(id);
    let entityId = null;
    if (id in project.model.entities) entityId = id;
    else if (id in project.model.ports) {
      entityId = project.model.ports[id].entity_id;
      ports.push(id);
    }
    if (r && Array.isArray(r.pid_occurrences)) r.pid_occurrences.forEach((o) => occurrences.add(o));
    else if (entityId) Object.values(project.pid.symbols).filter((s) => s.entity_id === entityId).forEach((s) => occurrences.add(s.id));
    else Object.values(project.pid.edges).filter((e) => e.connection_id === id).forEach((e) => occurrences.add(e.id));
    if (!viewer) continue;
    if (r && Array.isArray(r.display_components) && r.display_build_id && r.display_build_id === shownBuild) {
      r.display_components.forEach((c) => { if (viewer.hasComponent(c)) components.add(c); });
    } else {
      viewer.componentsFor(id).forEach((c) => components.add(c));
    }
  }
  return { occurrences, components, ports };
}
app.highlightFor = highlightFor;

function paintSelection(ids, resolved) {
  app.selectionIds = ids;
  app.selectionResolved = resolved;
  app.moves.selectionChanged(ids); // a typed preview of an item no longer selected ends
  if (app.section && !(ids.length === 1 && ids[0] === app.section.connectionId)) setSection(null);
  for (const pane of app.panes) pane.setSelection(ids, resolved);
  app.inspector.setSelection(ids);
}

/** Repaint the shared selection into one pane after its content changed (no request is sent). */
app.paintPane = (pane) => {
  if (app.state) pane.setSelection(app.selectionIds, app.selectionResolved);
  if (app.assistant) app.assistant.sourcesChanged();
};

/** A pane installed a new scene: the inspector's 3D mapping chips follow the primary 3D pane. */
app.sceneChanged = (pane) => {
  if (primaryPane('3d') === pane) app.inspector.sceneChanged();
};

function applyServerSelection(force = false) {
  const s = app.state;
  if (!s || app.pending) return;
  const key = `${s.service ? s.service.started_at : ''}|${s.selection.sequence}|${s.project.revision}`;
  if (!force && key === app.selectionKey) return;
  app.selectionKey = key;
  paintSelection(s.selection.semantic_ids || [], s.selection.resolved);
}

let selectionToken = 0;
app.select = async (ids, origin, additive = false) => {
  if (!app.state) return;
  const project = app.state.project;
  let next = ids.filter((id) => knownId(project, id));
  if (ids.length && !next.length) {
    toast('warn', 'Not in the current revision', ['That item comes from an older view and does not exist at the current revision.']);
    return;
  }
  if (additive) {
    const current = new Set(app.selectionIds);
    for (const id of next) {
      if (current.has(id)) current.delete(id);
      else current.add(id);
    }
    next = [...current];
  }
  const token = ++selectionToken;
  app.pending = { ids: next, token };
  paintSelection(next, null); // immediate local feedback; the service's answer follows
  const seq = ++app.reqSeq;
  try {
    const state = await postJSON('/api/selection', { semantic_ids: next, origin });
    if (token !== selectionToken) return;
    app.pending = null;
    receive(state, seq);
    applyServerSelection(true);
  } catch (error) {
    if (token !== selectionToken) return;
    app.pending = null;
    reportError(error, 'Selection');
    applyServerSelection(true);
  }
};

/** A click in any pane's 3D view or sheet. */
app.pickFromView = (hit, origin) => {
  if (!hit) {
    setSection(null);
    if (app.selectionIds.length) app.select([], origin);
    return;
  }
  app.select([hit.semanticId], origin, hit.additive); // paints synchronously; the line stays one semantic selection
  // a straight run of a line's pipe also names its section (a bend, flange or bolting names none)
  if (!hit.additive && app.selectionIds.length === 1 && app.selectionIds[0] === hit.semanticId && setSection(hit.section || null)) repaintMoves();
};

// ---------------------------------------------------------------- rendering

function renderToolbar(state) {
  const project = state.project;
  app.nameEditor.update(project);
  $('#tb-revision').textContent = `r${project.revision}`;
  $('#tb-revision').title = `Project revision ${project.revision}`;
  const doc = state.document || {};
  const saved = doc.saved;
  const notes = doc.notes || {};
  const docEl = $('#tb-doc');
  if (doc.disk_valid === false) {
    docEl.textContent = 'File on disk invalid';
    docEl.className = 'tb-doc bad';
  } else if (notes.present && notes.readable === false) {
    docEl.textContent = 'Notes sidecar unreadable';
    docEl.className = 'tb-doc bad';
  } else if (saved && saved.revision === project.revision) {
    docEl.textContent = `Saved r${saved.revision} · verified`;
    docEl.className = 'tb-doc saved';
  } else {
    docEl.textContent = 'Auto-saved per change';
    docEl.className = 'tb-doc';
  }
  docEl.title = `${doc.path || ''}${saved ? `\nSave confirmed at ${saved.verified_at}` : '\nEvery accepted command is saved atomically; Save confirms durability on disk.'}` +
    `${notes.present ? `\nNotes ${notes.path}: ${notes.readable === false ? `unreadable (${notes.error})` : 'readable'}` : ''}`;
  const sync = syncBadge(state);
  const syncEl = $('#tb-sync');
  syncEl.className = `badge tone-${sync.tone}`;
  syncEl.title = sync.detail || '';
  replace(syncEl, h('i', { class: 'dot' }), sync.label);
  const build = state.build;
  const buildEl = $('#tb-build');
  const active = build && ACTIVE_STATUSES.has(build.status);
  if (build) {
    const elapsed = buildElapsed(build);
    const integrity = build.integrity === 'rejected' ? ' · integrity rejected' : '';
    buildEl.textContent = `${active ? (build.status === 'queued' ? 'Queued' : 'Generating') : 'Last build'} r${build.source_revision} · ${build.status}${integrity} · ${formatSeconds(elapsed)}`;
    buildEl.title = `${build.id}\n${build.message || ''}`;
    buildEl.classList.toggle('bad', build.integrity === 'rejected' || build.status === 'failed');
  } else {
    buildEl.textContent = '';
  }
  const cancellable = (build && ACTIVE_STATUSES.has(build.status)) || state.active_build;
  $('#btn-cancel').hidden = !cancellable;
  $('#btn-undo').disabled = !state.history.undo;
  $('#btn-redo').disabled = !state.history.redo;
  $('#undo-count').textContent = state.history.undo || '';
  $('#redo-count').textContent = state.history.redo || '';
  renderDownloads(state);
}

const DOWNLOAD_LABELS = {
  cad: ['STEP assembly', 'model.step'], recipe: ['Geometry recipe (rebuildable)', 'geometry.recipe.json'],
  verification: ['CAD reimport verification', 'cad.verification.json'], display: ['3D scene (mesh JSON)', 'scene.json'],
  pid: ['P&ID sheet', 'pid.svg'], legend: ['Legend sheet', 'legend.svg'], index: ['Item index', 'item-index.json'],
  inventory: ['Drawing inventory', 'drawing.inventory.json'], description: ['Process description', 'description.html'],
};

function renderDownloads(state) {
  const menu = $('#menu-downloads');
  const pub = state.published;
  const items = [];
  if (pub) {
    const behind = pub.source_revision !== state.project.revision;
    const synchronized = state.sync && state.sync.state === 'synchronized';
    items.push(h('h4', null, `Verified published set · r${pub.source_revision}${behind ? ' · stale' : synchronized ? ' · current' : ''}`));
    for (const key of ['cad', 'recipe', 'verification', 'pid', 'legend', 'index', 'inventory', 'description', 'display']) {
      const url = pub.artifacts[key];
      if (!url) continue;
      items.push(h('a', { href: url, download: `r${pub.source_revision}-${DOWNLOAD_LABELS[key][1]}`, role: 'menuitem' },
        h('span', null, DOWNLOAD_LABELS[key][0]), h('small', null, DOWNLOAD_LABELS[key][1])));
    }
    items.push(h('div', { class: 'note' }, `Build ${pub.build_id} · digest ${shortDigest(pub.input_digest)}. ` +
      (behind ? `The project is at revision ${state.project.revision}; these files are not current.`
        : synchronized ? 'All five outputs verified for this revision.' : (state.sync ? state.sync.message : ''))));
  } else {
    items.push(h('div', { class: 'note' }, state.sync && state.sync.state === 'invalid'
      ? 'The last published set no longer verifies and is not offered. Rebuild to publish a verified set.'
      : 'No verified output set is published yet.'));
  }
  const b = state.build;
  if (b && b.status === 'pending' && b.integrity !== 'rejected' && b.artifacts && Object.keys(b.artifacts).length) {
    items.push(h('h4', null, `Unpublished pending outputs · r${b.source_revision}`));
    for (const key of ['cad', 'recipe', 'verification', 'pid', 'legend']) {
      if (!b.artifacts[key] || !DOWNLOAD_LABELS[key]) continue;
      items.push(h('a', { href: b.artifacts[key], download: `r${b.source_revision}-pending-${DOWNLOAD_LABELS[key][1]}`, role: 'menuitem' },
        h('span', null, `${DOWNLOAD_LABELS[key][0]} (pending)`), h('small', null, DOWNLOAD_LABELS[key][1])));
    }
  }
  replace(menu, items);
}

/** Inspector companions follow the Live sources, independent of what the panes show. */
function requestCompanions(state) {
  const index = viewSource(state, 'index', 'live');
  app.loaders.index.want(index ? index.url : null, index);
  // description.json (inspector explanations) lives beside a final description.html of the same build
  const live = viewSource(state, 'description', 'live');
  if (live && live.kind === 'output' && /\/description\.html$/.test(live.url)) {
    app.loaders.descriptionJson.want(live.url.replace(/description\.html$/, 'description.json'), live);
  } else {
    app.loaders.descriptionJson.want(null, null);
  }
}

function render(previous, state) {
  renderToolbar(state);
  requestCompanions(state);
  app.moves.stateChanged(state); // before the panes decide what to install
  revalidateSection(state);
  app.pipeMoves.stateChanged(state);
  for (const pane of app.panes) pane.update(state);
  app.moves.sweep();
  app.pipeMoves.sweep();
  applyServerSelection(!previous || previous.project.revision !== state.project.revision);
  app.inspector.update(state, previous);
  app.activity.update(state);
  app.composer.update(state);
  app.assistant.update(state);
  app.commandPanel.update(state);
  for (const d of state.diagnostics || []) {
    if (!NOTICE_CODES.has(d.code) && !String(d.code).startsWith('published_')) continue;
    const key = `${d.code}|${d.message}`;
    if (app.seenNotices.has(key)) continue;
    app.seenNotices.add(key);
    if (d.code === 'build_not_needed') {
      toast('ok', 'Already current', [d.message], [{ label: 'Force rebuild', run: () => mutate('/api/build', { force: true }, 'Force rebuild') }]);
    } else {
      toast(d.severity === 'error' ? 'err' : 'warn', d.code.replace(/_/g, ' '), [d.message]);
    }
  }
}

// ---------------------------------------------------------------- panes and layout

function setupCompanions() {
  const companion = (label, fetch, install) => createLoader({ fetch, install, onChange: () => {} });
  app.loaders.index = companion('Item index', (url) => app.cachedJSON(url), (data, target) => {
    checkIdentity('Item index', { revision: data.source_revision, digest: data.input_digest }, target.src);
    app.docData.index = { data, src: target.src, url: target.url };
    app.inspector.docsChanged();
  });
  app.loaders.descriptionJson = companion('Description data', (url) => app.cachedJSON(url), (data, target) => {
    checkIdentity('Description data', { revision: data.source_revision, digest: data.input_digest }, target.src);
    app.docData.descriptionJson = { data, src: target.src, url: target.url };
    app.inspector.docsChanged();
  });
}

function setupPanes() {
  const views = $('#views');
  const splitter = h('div', { class: 'splitter', id: 'splitter', role: 'separator', 'aria-orientation': 'vertical', tabindex: '0', title: 'Drag to resize the columns (← →)' });
  const splitterH = h('div', { class: 'splitter-h', id: 'splitter-h', role: 'separator', 'aria-orientation': 'horizontal', tabindex: '0', title: 'Drag to resize the rows (↑ ↓)' });
  app.panes = app.layout.types.map((type, i) => new Pane(app, i, { type, mode: app.layout.modes[i] }));
  views.append(app.panes[0].el, splitter, app.panes[1].el, splitterH, app.panes[2].el, app.panes[3].el);
  bindSplitter(splitter, 'x', '--split', 54);
  bindSplitter(splitterH, 'y', '--split-y', 50);
  applyLayout();
}

function bindSplitter(el, axis, variable, fallback) {
  const set = (pct) => document.documentElement.style.setProperty(variable, `${Math.min(80, Math.max(20, pct))}%`);
  el.addEventListener('pointerdown', (e) => {
    const rect = $('#views').getBoundingClientRect();
    el.classList.add('drag');
    el.setPointerCapture(e.pointerId);
    const move = (ev) => set(axis === 'x' ? ((ev.clientX - rect.left) / rect.width) * 100 : ((ev.clientY - rect.top) / rect.height) * 100);
    const up = () => {
      el.classList.remove('drag');
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      markCorner();
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  });
  el.addEventListener('keydown', (e) => {
    const keys = axis === 'x' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown'];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(variable)) || fallback;
    set(current + (e.key === keys[0] ? -3 : 3));
  });
}

/** Apply layout, maximize and focus to the DOM; panes outside the visible set are paused. */
function applyLayout() {
  const layout = app.layout;
  const views = $('#views');
  views.dataset.panes = String(layout.panes);
  if (layout.maximized != null) views.dataset.max = String(layout.maximized);
  else delete views.dataset.max;
  const inLayout = new Set(layoutIndices(layout));
  const visible = new Set(visibleIndices(layout));
  for (const pane of app.panes) {
    pane.el.hidden = !inLayout.has(pane.index);
    pane.setActive(visible.has(pane.index));
    const maxed = layout.maximized === pane.index;
    pane.maxButton.replaceChildren(icon(maxed ? 'min' : 'max'));
    pane.maxButton.title = maxed ? 'Restore the panes (Esc)' : 'Maximize this pane (M)';
    pane.maxButton.setAttribute('aria-pressed', String(maxed));
  }
  for (const b of $$('#layout-seg button')) {
    const on = Number(b.dataset.layout) === layout.panes;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  }
  const focus = !!layout.focus;
  document.body.classList.toggle('focus-mode', focus);
  $('#btn-focus').setAttribute('aria-pressed', String(focus));
  $('#focus-bar').hidden = !focus;
  panesChanged(); // a typed preview whose pane is no longer shown ends
  if (app.assistant) app.assistant.place(); // focus changes the room left at the top
  requestAnimationFrame(markCorner);
}

/** In focus, the pane in the top-right corner leaves room in its header for the exit control. */
function markCorner() {
  let corner = null;
  let best = null;
  for (const pane of app.panes) {
    pane.el.classList.remove('corner');
    if (pane.el.hidden || !pane.active) continue;
    const r = pane.el.getBoundingClientRect();
    if (!r.width || r.right < window.innerWidth - 8) continue;
    const score = Math.round(r.top) * 100000 - Math.round(r.right);
    if (best === null || score < best) { best = score; corner = pane; }
  }
  if (corner) corner.el.classList.add('corner');
}

app.setPaneType = (index, type) => {
  app.layout = setPaneType(app.layout, index, type);
  app.panes[index].setType(type);
  panesChanged();
  if (type === '3d' || app.panes.filter((p) => p.type === '3d').length === 0) app.inspector.sceneChanged();
  app.assistant.sourcesChanged();
};

app.setPaneMode = (index, mode) => {
  app.layout = setPaneMode(app.layout, index, mode);
  app.panes[index].setMode(mode);
  panesChanged(); // Published, even of the same revision, ends that pane's unsubmitted preview
  app.assistant.sourcesChanged();
};

app.setLayout = (panes) => {
  app.layout = setLayout(app.layout, panes);
  applyLayout();
};

app.toggleMax = (index) => {
  app.layout = toggleMaximized(app.layout, index);
  applyLayout();
};

function panelState() {
  const ws = $('#workspace');
  return { inspectorHidden: ws.classList.contains('no-inspector'), inspectorOpen: ws.classList.contains('inspector-open'), dockHidden: ws.classList.contains('no-dock') };
}

app.setFocus = (on) => {
  if (on) {
    app.layout = enterFocus(app.layout, panelState());
    $('#menu-downloads').hidden = true;
    applyLayout();
    $('#btn-exit-focus').focus({ preventScroll: true });
    return;
  }
  const { layout, restore } = exitFocus(app.layout);
  app.layout = layout;
  if (restore) {
    const ws = $('#workspace');
    ws.classList.toggle('no-inspector', restore.inspectorHidden);
    ws.classList.toggle('inspector-open', restore.inspectorOpen);
    ws.classList.toggle('no-dock', restore.dockHidden);
  }
  applyLayout();
  $('#btn-focus').focus({ preventScroll: true });
};

/** Pane the keyboard's view shortcuts act on: the hovered pane if it shows that type, else the first visible one. */
function targetPane(predicate) {
  const hovered = app.hoveredPane != null ? app.panes[app.hoveredPane] : null;
  const visible = visibleIndices(app.layout).map((i) => app.panes[i]);
  if (hovered && visible.includes(hovered) && predicate(hovered)) return hovered;
  return visible.find(predicate) || null;
}

app.noteWorkedPane = (index) => {
  if (app.workedPane === index) return;
  app.workedPane = index;
  if (app.assistant) app.assistant.sourcesChanged(); // the Ask source line names this pane's content
};

/** The pane the user last worked in when it is still visible, else the first visible pane. */
app.activePane = () => {
  const visible = visibleIndices(app.layout);
  const i = app.workedPane != null && visible.includes(app.workedPane) ? app.workedPane : visible[0];
  return i != null ? app.panes[i] : null;
};

function focusSelection() {
  for (const i of visibleIndices(app.layout)) {
    const c = app.panes[i].content;
    if (c && c.focusSelection) c.focusSelection();
  }
}
app.focusSelection = focusSelection;

/** Compatibility for callers that switch the document pane: pane 1 shows the requested type. */
app.showDoc = (doc) => app.setPaneType(1, doc);

app.pickOnSheet = (callback) => {
  let pane = targetPane((p) => p.type === 'pid');
  if (!pane) {
    pane = app.panes[1];
    app.setPaneType(1, 'pid');
  }
  toast('ok', 'Pick a point on the P&ID', [`Click the sheet in pane ${pane.index + 1} to use that position; Esc cancels.`]);
  pane.content.sheet.pickPoint(callback);
};

function pickingPane() {
  return app.panes.find((p) => p.content && p.content.sheet && p.content.sheet.pickMode) || null;
}

app.sceneFacts = () => {
  const viewer = app.viewer;
  return viewer ? { ports: viewer.ports, source: viewer.source || null } : { ports: new Map(), source: null };
};
app.sheetFacts = () => {
  const pane = primaryPane('pid');
  const sheet = pane && pane.content.sheet;
  return sheet ? { anchors: sheet.portAnchors(), meta: sheet.meta || null } : { anchors: {}, meta: null };
};
app.toast = toast;
app.reportError = reportError;
app.mutate = mutate;

// ---------------------------------------------------------------- controls

/** Undo/Redo through the validated command path; the toolbar and the assistant share these. */
app.undo = () => (app.state ? app.submitCommand(envelope('undo', {}, app.state.project.revision), 'Undo') : null);
app.redo = () => (app.state ? app.submitCommand(envelope('redo', {}, app.state.project.revision), 'Redo') : null);

function setupControls() {
  const rev = () => app.state.project.revision;
  $('#btn-undo').addEventListener('click', () => app.undo());
  $('#btn-redo').addEventListener('click', () => app.redo());
  $('#btn-save').addEventListener('click', async () => {
    const r = await mutate('/api/save', { expected_revision: rev() }, 'Save');
    if (r.ok) toast('ok', `Saved revision ${r.state.project.revision}`, [`${r.state.document.path}`, 'File content verified on disk and synced.']);
  });
  $('#btn-reopen').addEventListener('click', async () => {
    const r = await mutate('/api/reopen', {}, 'Reopen');
    if (r.ok) toast('ok', `Reopened at revision ${r.state.project.revision}`, [`${r.state.history.undo} undo · ${r.state.history.redo} redo step(s) restored from disk.`]);
  });
  $('#btn-build').addEventListener('click', () => mutate('/api/build', {}, 'Rebuild'));
  $('#btn-cancel').addEventListener('click', () => {
    const s = app.state;
    const target = s.build && ACTIVE_STATUSES.has(s.build.status) ? s.build.id : s.active_build && s.active_build.id;
    if (target) mutate('/api/cancel', { build_id: target }, 'Cancel build');
  });
  const menuButton = $('#btn-downloads');
  const menu = $('#menu-downloads');
  menuButton.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    menuButton.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (e) => { if (!menu.contains(e.target)) menu.hidden = true; });

  const ws = $('#workspace');
  $('#btn-inspector').addEventListener('click', () => {
    if (window.matchMedia('(max-width: 1100px)').matches) ws.classList.toggle('inspector-open');
    else ws.classList.toggle('no-inspector');
    requestAnimationFrame(markCorner);
  });
  $('#btn-dock').addEventListener('click', () => ws.classList.toggle('no-dock'));
  $('#layout-seg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-layout]');
    if (b) app.setLayout(Number(b.dataset.layout));
  });
  $('#btn-focus').addEventListener('click', () => app.setFocus(!app.layout.focus));
  $('#btn-exit-focus').addEventListener('click', () => app.setFocus(false));
  // one floating Ask launcher in normal and focus; a drag moves it and never counts as a click
  const ask = $('#btn-ask');
  app.askLauncher = createFloatingLauncher(ask, {
    onActivate: () => app.assistant.toggleAsk(ask),
    onMove: () => { if (app.assistant) app.assistant.place(); },
  });
  window.addEventListener('resize', () => requestAnimationFrame(markCorner));

  // The Feedback entry opens the assistant's Change section, where the one composer lives.
  const changeTabs = $('#change-tabs');
  changeTabs.addEventListener('click', (e) => {
    const button = e.target.closest('button[data-tab]');
    if (!button) return;
    for (const b of changeTabs.querySelectorAll('button')) {
      b.classList.toggle('on', b === button);
      b.setAttribute('aria-selected', String(b === button));
    }
    $('#panel-feedback-entry').hidden = button.dataset.tab !== 'feedback';
    $('#panel-command').hidden = button.dataset.tab !== 'command';
    if (button.dataset.tab === 'feedback') app.assistant.open('change', button);
  });

  document.addEventListener('keydown', (e) => {
    const typing = e.target.closest && e.target.closest('input, textarea, select, [contenteditable]');
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'z' && !typing) {
      e.preventDefault();
      (e.shiftKey ? $('#btn-redo') : $('#btn-undo')).click();
      return;
    }
    // Escape while typing in the assistant or a pane's Move X/Y/Z still cancels a move preview or a
    // pick, or closes the assistant (drafts stay); no other key is taken from a field
    const field = app.assistant.contains(e.target) || !!(e.target.closest && e.target.closest('.move-panel'));
    if (e.key === 'Escape' && typing && !mod && !e.altKey && !e.isComposing && field) {
      e.preventDefault();
      escape();
      return;
    }
    if (typing || mod || e.altKey) return;
    const k = e.key;
    const three = () => targetPane((p) => p.type === '3d');
    if (k === 'Escape') escape();
    else if (k === 'f' || k === 'F') focusSelection();
    else if (k === '0') three()?.content.command('fit');
    else if (['1', '2', '3', '4'].includes(k)) three()?.content.command(['iso', 'front', 'top', 'right'][Number(k) - 1]);
    else if (k === 'l' || k === 'L') three()?.content.command('labels');
    else if (k === 'e' || k === 'E') three()?.content.command('edges');
    else if (k === 'm' || k === 'M') {
      const hovered = app.hoveredPane != null && visibleIndices(app.layout).includes(app.hoveredPane) ? app.hoveredPane : visibleIndices(app.layout)[0];
      app.toggleMax(hovered);
    }
  });
}

/** Escape, most local first (pane-model.escapeAction). A gizmo drag takes its own Escape (viewer3d.js). */
function escape() {
  const picking = pickingPane();
  const action = escapeAction({
    moving: app.moves.cancellable() || app.pipeMoves.cancellable(), picking: !!picking, assistant: app.assistant.isOpen(), focus: !!app.layout.focus,
    maximized: app.layout.maximized, selection: app.selectionIds.length,
  });
  if (action === 'cancel-move') {
    if (app.pipeMoves.cancellable()) app.cancelPipeMove('escape');
    else app.cancelEquipmentMove('escape');
  }
  else if (action === 'cancel-pick') picking.content.sheet.cancelPick();
  else if (action === 'close-assistant') app.assistant.close();
  else if (action === 'exit-focus') app.setFocus(false);
  else if (action === 'restore-max') app.toggleMax(null);
  else if (action === 'clear-selection') app.select([], 'inspector');
}

// ---------------------------------------------------------------- read-only snapshot

function snapshot() {
  const s = app.state;
  const slots = {};
  const slot = (l) => {
    const st = l.state();
    return { desired: st.desired && st.desired.url, installed: st.installed && st.installed.url, pending: st.pending && st.pending.url, error: st.error && st.error.message, stats: { ...l.stats } };
  };
  // legacy keys: the primary pane of each type, plus the inspector companions
  for (const [key, type] of [['display', '3d'], ['pid', 'pid'], ['legend', 'legend'], ['index', 'index'], ['description', 'description']]) {
    const pane = primaryPane(type);
    if (pane && pane.content.loader) slots[key] = slot(pane.content.loader);
  }
  slots.inspectorIndex = slot(app.loaders.index);
  slots.descriptionJson = slot(app.loaders.descriptionJson);
  const viewer = app.viewer;
  const pid = primaryPane('pid');
  const sheet = pid ? pid.content.sheet : null;
  const three = primaryPane('3d');
  const docPane = visibleIndices(app.layout).map((i) => app.panes[i]).find((p) => p.type !== '3d') || app.panes[1];
  const ws = $('#workspace');
  return {
    online: app.online,
    revision: s ? s.project.revision : null,
    sync: s ? s.sync.state : null,
    build: s && s.build ? { id: s.build.id, status: s.build.status, last_sequence: s.build.last_sequence, integrity: s.build.integrity } : null,
    selection: app.selectionIds.slice(),
    pidSelected: sheet && sheet.svg ? [...sheet.svg.querySelectorAll('.is-sel')].map((e) => e.getAttribute('data-occurrence-id')).filter(Boolean) : [],
    pidView: sheet ? sheet.viewState() : null,
    pidSource: sheet && sheet.meta ? { build_id: sheet.meta.build_id, revision: sheet.meta.source_revision, url: sheet.meta.url } : null,
    badges: { view3d: three ? three.badge.textContent : '', doc: docPane ? docPane.badge.textContent : '', sync: $('#tb-sync').textContent },
    viewer: viewer ? viewer.debugInfo() : null,
    sceneStats: three && three.content.stats ? { ...three.content.stats, invalid: three.content.stats.invalid.length } : null,
    slots,
    stats: { ...app.stats },
    composer: app.composer ? app.composer.snapshot() : null,
    assistant: app.assistant ? app.assistant.snapshot() : null,
    askLauncher: app.askLauncher ? app.askLauncher.snapshot() : null,
    layout: {
      panes: app.layout.panes,
      types: app.layout.types.slice(),
      modes: app.layout.modes.slice(),
      visible: visibleIndices(app.layout),
      maximized: app.layout.maximized,
      focus: !!app.layout.focus,
      focusSaved: app.layout.focus ? { ...app.layout.focus.saved } : null,
      panels: { ...panelState(), toolbarShown: $('.toolbar').getClientRects().length > 0, dockShown: $('#dock').getClientRects().length > 0, inspectorShown: $('#inspector').getClientRects().length > 0 && getComputedStyle($('#inspector')).display !== 'none' },
      overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    },
    panes: app.panes.map((p) => p.snapshot()),
    projectName: app.nameEditor.snapshot(),
    reroutes: Object.fromEntries([...app.reroutes].map(([id, r]) => [id, { ...r }])),
    equipmentMove: app.moves.snapshot(),
    pipeMove: app.pipeMoves.snapshot(),
    section: app.section ? { ...app.section } : null,
    capabilities: s && s.capabilities ? { ...s.capabilities } : null,
    pipeCapable: pipeCapable(s),
  };
}

function boot() {
  app.nameEditor = createNameEditor(app);
  app.inspector = createInspector(app, $('#insp-body'));
  app.activity = createActivity(app);
  app.composer = createComposer(app, $('#panel-feedback'));
  app.assistant = createAssistant(app, $('#assistant'), $('#panel-feedback'));
  app.commandPanel = createCommandPanel(app, $('#panel-command'));
  setupCompanions();
  setupPanes();
  setupControls();
  setInterval(() => { if (app.state) { renderToolbar(app.state); app.activity.tick(app.state); app.assistant.tick(); } }, 1000);
  poll();
}

boot();
