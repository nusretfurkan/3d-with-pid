// Application side of straight pipe-section movement (pure; unit-tested in Node).
//
// One global record {paneIndex, connectionId, segmentIndex, sourceRevision, inputDigest, routeKey,
// axis, frames, offset, basePoints, points, problem, phase}. A viewer (gizmo drag) or a pane's typed
// offset reports intent; this module decides whether an edit may start, keeps the record, mirrors
// it to the panes through `publish`, and turns one explicit Apply into exactly one
// move_route_segment envelope through the existing validated command transport. A drag ends in a
// reviewable preview, never in a hidden apply. The preview points come from the ported transform
// (pipe-sections.moveSection) and are tentative: the envelope carries only the section index and
// offset, and the service derives and validates the committed route.
//
// The gates match the equipment move (move-coordinator.js): a complete final Live scene of exactly
// the current revision and digest, the service's capabilities.pipe_segment_move, and the exact
// route the section index was taken from. Equipment and pipe edits block each other (one edit at a
// time). A submitted command is never resent: only the command log, or the fresh route assumption
// the envelope wrote, settles it as accepted; no trace keeps it unconfirmed.

import { MAX_UNRESOLVED, SUBMITTED, isFinalOffer, moveHold, startGate } from './move-coordinator.js';
import { RouteEditError, checkOffset, isPoint, moveSection, routeKey, routeOf, straightSections } from './pipe-sections.js';
// The body-clearance screen is service only: a preview without a problem may still be refused on Apply.

export { SUBMITTED, isFinalOffer };
export const PIPE_RECORD_FIELDS = Object.freeze(['paneIndex', 'connectionId', 'segmentIndex', 'sourceRevision', 'inputDigest', 'routeKey', 'axis', 'frames', 'offset', 'basePoints', 'points', 'problem', 'phase']);

export const PIPE_START_REASONS = Object.freeze({
  busy: 'Another pipe-section edit is in progress.',
  'equipment-busy': 'An equipment move is in progress; one edit at a time.',
  unconfirmed: 'A submitted section move is still unconfirmed by the service; hide its preview to start another.',
  unsupported: 'This workspace session runs an older service without section moves; reload once the updated workspace service is running.',
  offline: 'The local service is not reachable.',
  'no-state': 'The project state has not been received yet.',
  'not-3d': 'Section moves start from a 3D pane.',
  hidden: 'The pane is not shown.',
  published: 'Published views are read-only; show this pane as Live to move a pipe section.',
  'no-scene': 'No 3D scene is installed in this pane.',
  progressive: 'A working preview is shown; wait for the final scene.',
  incomplete: 'A section move starts only on a complete output; this scene is pending, failed or unverified.',
  'digest-unknown': 'The current model digest is unknown.',
  stale: 'The installed scene is not the current model revision.',
  'invalid-intent': 'The section request is incomplete.',
  'no-route': 'The line has no physical route.',
  'route-not-current': 'The route is not current (for example it needs rerouting); reroute it first.',
  'route-changed': 'The route changed since the section was selected.',
  'no-section': 'That section does not exist on the current route.',
  'no-axis': 'This section is not parallel to a world axis.',
  'no-frames': 'The installed scene has no axis-parallel port frames for this line.',
});

const fmt = (v) => String(Number(v.toFixed(6)));
function digestOf(state) {
  const doc = state && state.document;
  return doc && typeof doc.input_digest === 'string' && doc.input_digest ? doc.input_digest : null;
}
const has = (obj, key) => !!obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key);

/** Whether the service advertises the section-move command (absent on older services). */
export function pipeCapable(state) {
  return !!(state && state.capabilities && state.capabilities.pipe_segment_move === true);
}

function freezeDeep(v) {
  if (Array.isArray(v)) return Object.freeze(v.map(freezeDeep));
  if (v && typeof v === 'object') return Object.freeze(Object.fromEntries(Object.entries(v).map(([k, x]) => [k, freezeDeep(x)])));
  return v;
}

export function makePipeRecord(fields) {
  const out = {};
  for (const key of PIPE_RECORD_FIELDS) out[key] = freezeDeep(fields[key] ?? null);
  return Object.freeze(out);
}

/** The connection, route and section an intent names on a project: {ok, reason, section, sections, route}. */
export function sectionGate(project, connectionId, segmentIndex, key = null) {
  const fail = (reason) => ({ ok: false, reason, section: null, sections: [], route: null });
  const r = routeOf(project, connectionId);
  if (!r) return fail('no-route');
  if (r.route.status !== 'current') return fail('route-not-current');
  if (key !== null && routeKey(project, connectionId) !== key) return fail('route-changed');
  const sections = straightSections(project, connectionId);
  const section = sections.find((s) => s.index === segmentIndex);
  if (!section) return fail('no-section');
  if (section.axis === null) return fail('no-axis');
  return { ok: true, reason: null, section, sections, route: r.route };
}

/** The tentative preview of one offset: {points, problem}. Never throws. frames: {start, end} port directions. */
export function previewPoints(project, connectionId, segmentIndex, offset, frames) {
  try {
    return { points: moveSection(project, connectionId, segmentIndex, offset, frames), problem: null };
  } catch (error) {
    if (error instanceof RouteEditError) return { points: null, problem: { code: error.code, message: error.message } };
    return { points: null, problem: { code: 'error', message: String(error && error.message ? error.message : error) } };
  }
}

/**
 * What authoritative state says about a submitted envelope: 'accepted' (the command log records it,
 * or the route or model carries the envelope's fresh assumption), 'no-op' (the log says nothing
 * changed), or 'no-trace' (atBase: still the base revision and digest).
 */
export function pipeEvidence(state, submitted) {
  if (!state || !state.project || !submitted) return { outcome: 'unknown', toRevision: null, via: null };
  const log = [...(state.history && Array.isArray(state.history.recent) ? state.history.recent : []), state.last_command]
    .filter((e) => e && e.command_id === submitted.commandId);
  const accepted = log.find((e) => e.status === 'accepted');
  if (accepted) return { outcome: 'accepted', toRevision: Number.isInteger(accepted.to_revision) ? accepted.to_revision : null, via: 'command-log' };
  if (log.some((e) => e.status === 'no_op')) return { outcome: 'no-op', toRevision: null, via: 'command-log' };
  const routes = state.project.physical && state.project.physical.routes;
  const route = has(routes, submitted.connectionId) ? routes[submitted.connectionId] : null;
  if (route && submitted.assumptionId && route.assumption_id === submitted.assumptionId) return { outcome: 'accepted', toRevision: null, via: 'route' };
  const assumptions = state.project.model && state.project.model.assumptions;
  if (submitted.assumptionId && has(assumptions, submitted.assumptionId)) return { outcome: 'accepted', toRevision: null, via: 'assumption' };
  const atBase = state.project.revision === submitted.baseRevision && digestOf(state) === submitted.baseDigest;
  return { outcome: 'no-trace', toRevision: null, via: 'state', atBase };
}

function showsBase(record, installed) {
  return !!record && !!installed && !!installed.src && installed.src.source_revision === record.sourceRevision
    && installed.src.input_digest === record.inputDigest;
}

/** Reason to drop an accepted record, or null to keep it (as move-coordinator.acceptedClearReason). */
export function pipeAcceptedClearReason({ record, state, submitted, toRevision, livePanes = [] }) {
  if (!record || record.phase !== 'accepted' || !state || !state.project) return null;
  const routes = state.project.physical && state.project.physical.routes;
  const route = has(routes, record.connectionId) ? routes[record.connectionId] : null;
  if (!route || !submitted || route.assumption_id !== submitted.assumptionId) return 'superseded';
  const b = state.build;
  if (b && Number.isInteger(toRevision) && b.source_revision >= toRevision && (b.status === 'failed' || b.status === 'cancelled')) return `build-${b.status}`;
  if (!livePanes.some((p) => p && p.active && p.mode === 'live' && showsBase(record, p.installed))) return 'installed';
  return null;
}

const sameOffset = (a, b) => isPoint(a) && isPoint(b) && a.every((v, i) => v === b[i]);

/**
 * deps: getState(), isOnline(), pane(index), panes(), refetch(), submit(command, label),
 * buildCommand(project, connectionId, segmentIndex, offset), publish(record|null),
 * cancelViewer(index, reason), notify(kind, title, lines), otherActive(): equipment record or null, now().
 */
export function createPipeMoveCoordinator(deps) {
  const now = deps.now || (() => Date.now());
  let record = null;
  let meta = null;     // {origin, baseUrl, tag, projectId, service, submitted, toRevision}
  let outcome = null;
  const unresolved = [];
  const stats = { begins: 0, refused: 0, previews: 0, cancels: 0, commits: 0, submissions: 0, accepted: 0, rejected: 0, uncertain: 0 };
  let lastRefusal = null;

  function set(next) {
    record = next ? makePipeRecord(next) : null;
    deps.publish(record);
  }
  const identity = (state) => ({ projectId: state && state.project ? state.project.id : null, service: state && state.service ? state.service.started_at ?? null : null });
  function refuse(reason) {
    stats.refused += 1;
    lastRefusal = { reason, message: PIPE_START_REASONS[reason] || reason, at: now() };
    return false;
  }
  const label = (r) => `${meta ? meta.tag : r.connectionId} section ${r.segmentIndex + 1}`;
  function finish(kind, text, extra = {}) {
    outcome = {
      kind, text, at: now(), connectionId: record ? record.connectionId : extra.connectionId ?? null,
      segmentIndex: record ? record.segmentIndex : extra.segmentIndex ?? null, paneIndex: record ? record.paneIndex : extra.paneIndex ?? null, ...extra,
    };
  }
  const ended = (r) => ({ connectionId: r.connectionId, segmentIndex: r.segmentIndex, paneIndex: r.paneIndex });

  /** Start an edit of one section (synchronous). intent: {connectionId, segmentIndex, sourceRevision, inputDigest, routeKey}. */
  function begin(paneIndex, intent, { origin = 'pointer' } = {}) {
    if (record) return refuse(record.phase === 'uncertain' ? 'unconfirmed' : 'busy');
    if (deps.otherActive && deps.otherActive()) return refuse('equipment-busy');
    const state = deps.getState();
    if (!pipeCapable(state)) return refuse(state && state.project ? 'unsupported' : 'no-state');
    const gate = startGate({ state, online: deps.isOnline(), pane: deps.pane(paneIndex) });
    if (!gate.ok) return refuse(gate.reason);
    if (!intent || typeof intent.connectionId !== 'string' || !Number.isInteger(intent.segmentIndex)) return refuse('invalid-intent');
    if (!intent.frames || !isPoint(intent.frames.start) || !isPoint(intent.frames.end)) return refuse('no-frames');
    if (intent.sourceRevision !== state.project.revision || intent.inputDigest !== digestOf(state)) return refuse('stale');
    const sec = sectionGate(state.project, intent.connectionId, intent.segmentIndex, intent.routeKey ?? null);
    if (!sec.ok) return refuse(sec.reason);
    stats.begins += 1;
    lastRefusal = null;
    outcome = null;
    const conn = state.project.model.connections[intent.connectionId];
    meta = { origin, baseUrl: deps.pane(paneIndex).installed.url, tag: conn && conn.tag ? conn.tag : intent.connectionId, ...identity(state), submitted: null, toRevision: null };
    set({
      paneIndex, connectionId: intent.connectionId, segmentIndex: intent.segmentIndex, sourceRevision: state.project.revision,
      inputDigest: digestOf(state), routeKey: routeKey(state.project, intent.connectionId), axis: sec.section.axis,
      frames: { start: intent.frames.start.slice(), end: intent.frames.end.slice() }, offset: [0, 0, 0], basePoints: sec.route.points_m.map((p) => p.slice()), points: null, problem: null, phase: 'preview',
    });
    return true;
  }

  /** Preview one offset (exactly one nonzero perpendicular axis, or zero to show the base). */
  function preview(offset, paneIndex = null) {
    if (!record || record.phase !== 'preview') return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    if (!isPoint(offset)) return false;
    stats.previews += 1;
    if (sameOffset(offset, record.offset)) return true;
    const state = deps.getState();
    const zero = offset.every((v) => v === 0);
    const p = zero ? { points: null, problem: null } : previewPoints(state.project, record.connectionId, record.segmentIndex, offset, record.frames);
    set({ ...record, offset: offset.map((v) => (Object.is(v, -0) ? 0 : v)), points: p.points, problem: p.problem });
    return true;
  }

  function cancel(reason = 'cancelled', paneIndex = null) {
    if (!record || record.phase !== 'preview') return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    const r = record;
    const text = reason === 'no-op'
      ? `${label(r)}: the offset is zero; nothing was sent.`
      : `Section move of ${label(r)} cancelled (${String(reason).replace(/-/g, ' ')}); nothing was sent.`;
    record = null;
    stats.cancels += 1;
    outcome = { kind: reason === 'no-op' ? 'no-op' : 'cancelled', reason, at: now(), ...ended(r), text };
    meta = null;
    deps.cancelViewer(r.paneIndex, reason);
    deps.publish(null);
    return true;
  }

  function freshGate(r, { requireOnline = true } = {}) {
    const state = deps.getState();
    if (requireOnline && !deps.isOnline()) return { ok: false, reason: 'offline' };
    if (!state || !state.project) return { ok: false, reason: 'no-state' };
    const id = identity(state);
    if (meta && (id.projectId !== meta.projectId || id.service !== meta.service)) return { ok: false, reason: 'service-changed' };
    if (!pipeCapable(state)) return { ok: false, reason: 'unsupported' };
    if (state.project.revision !== r.sourceRevision || digestOf(state) !== r.inputDigest) return { ok: false, reason: 'stale' };
    const sec = sectionGate(state.project, r.connectionId, r.segmentIndex, r.routeKey);
    if (!sec.ok) return { ok: false, reason: sec.reason };
    const pane = deps.pane(r.paneIndex);
    if (!pane || pane.type !== '3d') return { ok: false, reason: 'destroyed' };
    if (!pane.active) return { ok: false, reason: 'hidden' };
    if (pane.mode !== 'live') return { ok: false, reason: 'published' };
    const gate = startGate({ state, online: true, pane });
    if (!gate.ok || (meta && pane.installed.url !== meta.baseUrl)) return { ok: false, reason: 'source-changed' };
    return { ok: true, reason: null };
  }

  function resolve(evidence, how) {
    const sub = meta.submitted;
    const tag = label(record);
    if (evidence.outcome === 'accepted') {
      meta.toRevision = evidence.toRevision ?? sub.baseRevision + 1;
      stats.accepted += 1;
      set({ ...record, phase: 'accepted' });
      finish('accepted', `${tag} moved: r${sub.baseRevision} → r${meta.toRevision}. The service derived and validated the new route.`, { commandId: sub.commandId, via: evidence.via, how });
      if (how === 'reconciled') deps.notify('ok', `Section move ${tag}: accepted`, [`Confirmed from the service state (${evidence.via}); nothing was resent.`]);
      return;
    }
    const r = record;
    record = null;
    finish('no-op', `${tag}: the service reported no change; the revision was kept.`, { ...ended(r), commandId: sub.commandId });
    meta = null;
    deps.publish(null);
  }

  function unconfirmedText(tag, sub, evidence, state) {
    const at = state && state.project ? state.project.revision : null;
    const moved = evidence && evidence.outcome === 'no-trace' && !evidence.atBase && at !== null
      ? ` The model is now r${at}; this command was prepared for r${sub.baseRevision}, and the service applies it only at that revision.` : '';
    return `Unconfirmed: the service has not confirmed the move of ${tag}.${at !== null ? ` No record of it yet at r${at}.` : ''} It may still be applied; nothing is resent.${moved}`;
  }

  /** Apply: at most one move_route_segment envelope for the previewed offset. */
  async function commit(paneIndex = null) {
    if (!record || record.phase !== 'preview') return { ok: false, reason: record ? 'submitted' : 'no-preview' };
    if (paneIndex != null && paneIndex !== record.paneIndex) return { ok: false, reason: 'other-pane' };
    stats.commits += 1;
    if (record.offset.every((v) => v === 0)) {
      cancel('no-op');
      return { ok: false, reason: 'no-op' };
    }
    const state0 = deps.getState();
    const sec0 = sectionGate(state0.project, record.connectionId, record.segmentIndex, record.routeKey);
    const check = checkOffset(sec0.section, record.offset);
    if (!check.ok) return { ok: false, reason: 'invalid-offset', message: check.message }; // the preview stays for correction
    if (record.problem) return { ok: false, reason: 'preview-refused', message: record.problem.message };
    let gate = freshGate(record);
    if (!gate.ok) {
      cancel(gate.reason);
      return { ok: false, reason: gate.reason };
    }
    const tag = label(record);
    set({ ...record, phase: 'submitting' });
    finish('submitting', `Submitting the move of ${tag}…`);
    const fresh = await deps.refetch();
    gate = fresh && fresh.ok ? freshGate(record) : { ok: false, reason: 'offline' };
    if (!gate.ok) {
      const r = record;
      record = null;
      meta = null;
      finish('not-sent', `Nothing was sent: ${PIPE_START_REASONS[gate.reason] || gate.reason.replace(/-/g, ' ')}`, ended(r));
      deps.notify('warn', `Section move ${tag}: not sent`, [outcome.text]);
      deps.publish(null);
      return { ok: false, reason: gate.reason };
    }
    const state = deps.getState();
    let command;
    try {
      command = deps.buildCommand(state.project, record.connectionId, record.segmentIndex, record.offset.slice());
    } catch (error) {
      const r = record;
      record = null;
      meta = null;
      finish('not-sent', `Nothing was sent: the command could not be prepared (${error && error.message ? error.message : error}).`, ended(r));
      deps.notify('err', `Section move ${tag}: not sent`, [outcome.text]);
      deps.publish(null);
      return { ok: false, reason: 'not-sent' };
    }
    meta.submitted = {
      commandId: command.id, assumptionId: command.payload ? command.payload.assumption_id : null, connectionId: record.connectionId,
      segmentIndex: record.segmentIndex, baseRevision: record.sourceRevision, baseDigest: record.inputDigest, sentAt: now(),
    };
    stats.submissions += 1;
    const result = await deps.submit(command, `Move ${tag}`);
    if (!record || !meta || !meta.submitted || meta.submitted.commandId !== command.id) return { ok: false, reason: 'superseded' };
    if (result && result.ok) {
      const evidence = pipeEvidence(result.state, meta.submitted);
      if (evidence.outcome === 'accepted' || evidence.outcome === 'no-op') {
        resolve(evidence, 'answer');
        return { ok: evidence.outcome === 'accepted', reason: evidence.outcome, commandId: command.id };
      }
    }
    const error = result && result.error;
    if (error && !error.network && error.status >= 400 && error.status < 500) {
      const r = record;
      record = null;
      meta = null;
      stats.rejected += 1;
      const detail = [error.error, ...(error.diagnostics || []).map((d) => `[${d.code}] ${d.message}`)].filter(Boolean);
      const reason = (error.diagnostics || []).map((d) => d.message).filter(Boolean)[0] || error.error || 'no reason given';
      finish('rejected', `Rejected by the service (HTTP ${error.status}): ${reason}. Nothing was changed.`, { ...ended(r), commandId: command.id, detail });
      deps.notify(error.status === 409 ? 'warn' : 'err', `Section move ${tag}: rejected`, [...detail, 'The revision is unchanged; the preview was removed.']);
      deps.publish(null);
      return { ok: false, reason: 'rejected', status: error.status, commandId: command.id };
    }
    stats.uncertain += 1;
    meta.submitted.reason = error ? error.error || `HTTP ${error.status}` : 'the answer does not mention this command';
    set({ ...record, phase: 'uncertain' });
    finish('uncertain', unconfirmedText(tag, meta.submitted, null, null), { commandId: command.id, cause: meta.submitted.reason });
    deps.notify('warn', `Section move ${tag}: unconfirmed`, [`No confirmation from the service (${meta.submitted.reason}). The move may still be applied; it is not sent again.`]);
    reconcile();
    if (record && record.phase === 'uncertain') deps.refetch();
    return { ok: false, reason: record && record.phase === 'accepted' ? 'accepted' : 'uncertain', commandId: command.id };
  }

  function reconcile(state = deps.getState()) {
    if (!record || record.phase !== 'uncertain' || !meta || !meta.submitted || !state || !state.project) return;
    const evidence = pipeEvidence(state, meta.submitted);
    if (evidence.outcome === 'accepted' || evidence.outcome === 'no-op') {
      resolve(evidence, 'reconciled');
      return;
    }
    const id = identity(state);
    if (id.projectId !== meta.projectId || id.service !== meta.service) {
      const r = record;
      const tag = label(r);
      record = null;
      finish('session-changed', `The ${id.service !== meta.service ? 'service was restarted' : 'project changed'}; the unconfirmed move of ${tag} is no longer watched. The model now (r${state.project.revision}) shows no trace of it.`, { ...ended(r), commandId: meta.submitted.commandId });
      meta = null;
      deps.publish(null);
      return;
    }
    const text = unconfirmedText(label(record), meta.submitted, evidence, state);
    if (!outcome || outcome.text !== text) finish('uncertain', text, { commandId: meta.submitted.commandId, cause: meta.submitted.reason, checkedRevision: state.project.revision });
  }

  /** Hide the preview of an unconfirmed move; nothing is cancelled or resent, and it stays watched. */
  function dismiss(paneIndex = null) {
    if (!record || record.phase !== 'uncertain' || !meta || !meta.submitted) return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    const r = record;
    const tag = label(r);
    unresolved.push({ submitted: { ...meta.submitted }, tag, paneIndex: r.paneIndex, projectId: meta.projectId, service: meta.service, since: now() });
    while (unresolved.length > MAX_UNRESOLVED) unresolved.shift();
    record = null;
    finish('unconfirmed', `Preview hidden. The move of ${tag} is still unconfirmed: it may yet be applied, and you will be told if it is. Nothing was cancelled, undone or resent.`, { ...ended(r), commandId: meta.submitted.commandId });
    meta = null;
    deps.publish(null);
    return true;
  }

  function watchUnresolved(state) {
    if (!unresolved.length || !state || !state.project) return;
    const id = identity(state);
    for (const entry of unresolved.slice()) {
      const evidence = pipeEvidence(state, entry.submitted);
      const settled = evidence.outcome === 'accepted' || evidence.outcome === 'no-op';
      const over = id.projectId !== entry.projectId || id.service !== entry.service;
      if (!settled && !over) continue;
      unresolved.splice(unresolved.indexOf(entry), 1);
      const where = { connectionId: entry.submitted.connectionId, segmentIndex: entry.submitted.segmentIndex, paneIndex: entry.paneIndex, commandId: entry.submitted.commandId };
      if (evidence.outcome === 'accepted') {
        deps.notify('warn', `Section move ${entry.tag}: applied`, [`The move whose preview was hidden while unconfirmed has been applied (model r${state.project.revision}; ${evidence.via}). It was not resent.`]);
        if (!record) finish('confirmed-late', `The hidden unconfirmed move of ${entry.tag} was applied (model r${state.project.revision}). It was not resent.`, where);
      } else if (evidence.outcome === 'no-op' && !record) {
        finish('no-op', `${entry.tag}: the service reported no change for the hidden unconfirmed move.`, where);
      }
    }
  }

  function sweep() {
    if (!record || record.phase !== 'accepted') return;
    const reason = pipeAcceptedClearReason({
      record, state: deps.getState(), submitted: meta ? meta.submitted : null, toRevision: meta ? meta.toRevision : null,
      livePanes: (deps.panes() || []).filter((p) => p && p.type === '3d'),
    });
    if (!reason) return;
    const r = record;
    const tag = label(r);
    record = null;
    if (outcome && outcome.kind === 'accepted') outcome = { ...outcome, cleared: reason, clearedAt: now() };
    if (reason === 'superseded') finish('superseded', `The model no longer has this move of ${tag} (undone or changed again); the preview was removed.`, ended(r));
    else if (reason !== 'installed') finish(reason, `${tag} moved, but regeneration ended ${reason.replace('build-', '')}; the preview was removed and the views show what the service offers.`, ended(r));
    meta = null;
    deps.publish(null);
  }

  function check() {
    if (!record || record.phase !== 'preview') return;
    const gate = freshGate(record);
    if (!gate.ok) cancel(gate.reason);
  }

  return {
    record: () => record,
    begin,
    preview,
    cancel,
    commit,
    cancellable: () => !!record && record.phase === 'preview',
    dismiss,
    unresolved: () => unresolved.map((u) => ({ commandId: u.submitted.commandId, connectionId: u.submitted.connectionId, segmentIndex: u.submitted.segmentIndex, tag: u.tag, paneIndex: u.paneIndex, baseRevision: u.submitted.baseRevision, since: u.since })),
    stateChanged(state) {
      watchUnresolved(state);
      if (!record) return;
      if (record.phase === 'preview') check();
      else if (record.phase === 'uncertain') reconcile(state);
      else if (record.phase === 'accepted') sweep();
    },
    onlineChanged(online) { if (!online) cancel('offline'); },
    paneChanged() { check(); sweep(); },
    /** The local section selection changed: a preview of another section (or none) ends. */
    sectionChanged(section) {
      if (!record || record.phase !== 'preview') return;
      if (!section || section.connectionId !== record.connectionId || section.segmentIndex !== record.segmentIndex) cancel('selection-changed');
    },
    sweep,
    hold: (args) => moveHold({ record, ...args }),
    outcome: () => outcome,
    refusal: () => lastRefusal,
    tag: () => (meta ? meta.tag : null),
    snapshot: () => ({
      record: record ? JSON.parse(JSON.stringify(record)) : null,
      origin: meta ? meta.origin : null,
      submitted: meta && meta.submitted ? { ...meta.submitted } : null,
      toRevision: meta ? meta.toRevision : null,
      outcome: outcome ? { ...outcome } : null,
      refusal: lastRefusal ? { ...lastRefusal } : null,
      unresolved: unresolved.map((u) => ({ commandId: u.submitted.commandId, connectionId: u.submitted.connectionId, tag: u.tag, baseRevision: u.submitted.baseRevision })),
      stats: { ...stats },
    }),
  };
}

/** Offset text for one axis: "+0.25 m along Z". */
export function offsetText(offset) {
  const k = isPoint(offset) ? offset.findIndex((v) => v !== 0) : -1;
  return k < 0 ? '0 m' : `${offset[k] > 0 ? '+' : ''}${fmt(offset[k])} m along ${'XYZ'[k]}`;
}
