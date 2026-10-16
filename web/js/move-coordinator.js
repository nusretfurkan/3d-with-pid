// Application side of direct equipment movement (pure; unit-tested in Node).
//
// One global move record {paneIndex, entityId, sourceRevision, inputDigest, basePosition,
// position, rotation, phase} (docs/ASSISTANT_3D_CONTRACTS.md). Viewers only report intent
// (begin / preview / cancel / commit, from a gizmo drag or the pane's typed XYZ); this module
// decides whether a move may start, keeps the record, mirrors it to the panes through
// `publish`, and turns one commit into exactly one existing move_equipment envelope sent
// through the existing validated command transport. It never writes model data itself.
//
// - A move starts only online, on a complete final Live scene of exactly the current revision
//   and document digest, for an item whose authoritative placement is the captured base.
// - Before sending, the base is checked again against freshly fetched state; a no-op or a
//   stale base sends nothing (the no-op test comes before the envelope allocates its fresh
//   manual-layout assumption).
// - A submitted command cannot be cancelled. An answer that never arrives is not resent. Only
//   authoritative evidence settles it: the command log, or the fresh assumption ID the envelope
//   wrote into the placement (accepted), or the service's own answer (rejected / no change). A
//   state with no trace proves nothing (the request may still be in flight or blocked), so the
//   move stays *unconfirmed* and keeps blocking new moves until it is confirmed or the user hides
//   its preview; a hidden unconfirmed command is still watched (bounded) and reported if it lands.
// - After acceptance the preview stays on the exact scene it was drawn on. A Live pane showing
//   that scene keeps it (with the preview) while the regenerated revision is still progressive,
//   and installs the regenerated scene once a final one is offered, so geometry never shows
//   the delta twice and unreached parts never fall back to the base.

import { MAX_DELTA_M, isFiniteVec3, isNoOp, placementOf, selectionOwner } from './equipment-move.js';

export const RECORD_FIELDS = Object.freeze(['paneIndex', 'entityId', 'sourceRevision', 'inputDigest', 'basePosition', 'position', 'rotation', 'phase']);
/** Phases after the commit: the command is (or may be) on the service and cannot be cancelled. */
export const SUBMITTED = new Set(['submitting', 'accepted', 'uncertain']);
const ACTIVE_BUILD = new Set(['queued', 'running']);
/** Hidden unconfirmed commands still watched for evidence; the oldest is dropped beyond this. */
export const MAX_UNRESOLVED = 4;

export const START_REASONS = Object.freeze({
  busy: 'Another equipment move is in progress.',
  'pipe-busy': 'A pipe-section move is in progress; one edit at a time.',
  unconfirmed: 'A submitted move is still unconfirmed by the service; hide its preview to start another.',
  offline: 'The local service is not reachable.',
  'no-state': 'The project state has not been received yet.',
  'not-3d': 'Moves start from a 3D pane.',
  hidden: 'The pane is not shown.',
  published: 'Published views are read-only; show this pane as Live to move equipment.',
  'no-scene': 'No 3D scene is installed in this pane.',
  progressive: 'A working preview is shown; wait for the final scene.',
  incomplete: 'A move starts only on a complete output; this scene is pending, failed or unverified. Reroute or rebuild first.',
  'digest-unknown': 'The current model digest is unknown.',
  stale: 'The installed scene is not the current model revision.',
  'invalid-intent': 'The move request is incomplete.',
  missing: 'The item does not exist in the current model.',
  unplaced: 'The item has no placed physical position.',
  'base-changed': 'The item\'s placement changed since the move started.',
  'rotation-changed': 'The item\'s rotation changed since the move started.',
});

const has = (obj, key) => !!obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key);
/** The current document digest of a state payload (the service's document record, not the DOM). */
function digestOf(state) {
  const { document: doc } = state || {};
  return doc && typeof doc.input_digest === 'string' && doc.input_digest ? doc.input_digest : null;
}
const sameVec = (a, b) => isFiniteVec3(a) && isFiniteVec3(b) && a.every((v, i) => v === b[i]);
const fmt = (v) => (Number.isFinite(v) ? String(Number(v.toFixed(6))) : String(v));

/** A frozen record with exactly the contract fields; arrays are frozen copies. */
export function makeRecord(fields) {
  const out = {};
  for (const key of RECORD_FIELDS) {
    const v = fields[key];
    out[key] = Array.isArray(v) ? Object.freeze(v.slice()) : v;
  }
  return Object.freeze(out);
}

/**
 * Whether the scene the viewer has actually loaded is the loader's installed file: same URL,
 * revision and digest, and final. viewerSource: {url, source_revision, input_digest, final} | null.
 * A pane description without viewerSource (older pure callers) falls back to its viewerFinal flag.
 */
function viewerShows(pane) {
  if (pane.viewerSource === undefined) return pane.viewerFinal !== false;
  const v = pane.viewerSource;
  const src = pane.installed.src;
  return !!v && v.final === true && v.url === pane.installed.url && v.source_revision === src.source_revision && v.input_digest === src.input_digest;
}

/**
 * Whether a pane may start a move: online, Live, active 3D, and its installed scene, as the viewer
 * has loaded it, is a complete final output of exactly the current revision and document digest.
 * {ok, reason}. pane: {type, mode, active, installed: {url, src} | null, viewerSource}.
 */
export function startGate({ state = null, online = false, pane = null } = {}) {
  const fail = (reason) => ({ ok: false, reason });
  if (online !== true) return fail('offline');
  if (!state || !state.project) return fail('no-state');
  if (!pane || pane.type !== '3d') return fail('not-3d');
  if (!pane.active) return fail('hidden');
  if (pane.mode !== 'live') return fail('published');
  const installed = pane.installed;
  if (!installed || !installed.src) return fail('no-scene');
  const src = installed.src;
  if (src.kind !== 'output' || !viewerShows(pane)) return fail('progressive');
  if (src.build_status !== 'complete' || src.artifact_status !== 'complete' || src.integrity === 'rejected') return fail('incomplete');
  const digest = digestOf(state);
  if (!digest) return fail('digest-unknown');
  if (src.source_revision !== state.project.revision || src.input_digest !== digest) return fail('stale');
  return { ok: true, reason: null };
}

/** The item's authoritative placement still equals the captured base (and rotation). {ok, reason, placement}. */
export function placementGate(project, entityId, basePosition, rotation = null) {
  const placement = placementOf(project, entityId);
  if (placement.state === 'missing' || placement.state === 'unknown' || placement.state === 'connection') return { ok: false, reason: 'missing', placement };
  if (placement.state !== 'placed') return { ok: false, reason: 'unplaced', placement };
  if (!sameVec(placement.position, basePosition)) return { ok: false, reason: 'base-changed', placement };
  if (rotation && !sameVec(placement.rotation, rotation)) return { ok: false, reason: 'rotation-changed', placement };
  return { ok: true, reason: null, placement };
}

const NUMBER = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$/;

/** One typed coordinate in metres: {ok, value} or {ok: false, problem, message}. Nothing is rounded or clamped. */
export function parseMetres(text) {
  const t = String(text ?? '').trim();
  if (!t) return { ok: false, problem: 'empty', message: 'Enter a number of metres.' };
  if (/^[-+]?\d*,\d+$/.test(t)) return { ok: false, problem: 'comma', message: 'Use a decimal point (1.5), not a comma.' };
  if (!NUMBER.test(t)) return { ok: false, problem: 'not-a-number', message: `“${t.slice(0, 24)}” is not a number of metres.` };
  const value = Number(t);
  if (!Number.isFinite(value)) return { ok: false, problem: 'not-a-number', message: `“${t.slice(0, 24)}” is not a finite number.` };
  return { ok: true, value: Object.is(value, -0) ? 0 : value };
}

/**
 * Typed X/Y/Z texts against the base origin: {ok, position, fields: [{ok, message}], message}. A
 * move of more than MAX_DELTA_M on an axis is refused with a reason (the same limit as a drag),
 * never clamped.
 */
export function readTyped(texts, base) {
  const fields = texts.map((t) => parseMetres(t));
  const bad = fields.findIndex((f) => !f.ok);
  if (bad >= 0) return { ok: false, position: null, fields, message: `${'XYZ'[bad]}: ${fields[bad].message}` };
  const position = fields.map((f) => f.value);
  if (isFiniteVec3(base)) {
    for (let i = 0; i < 3; i += 1) {
      const d = position[i] - base[i];
      if (Math.abs(d) > MAX_DELTA_M) {
        fields[i] = { ok: false, problem: 'too-far', message: `${fmt(d)} m from the current origin` };
        return { ok: false, position, fields, message: `${'XYZ'[i]} is ${fmt(d)} m from the current origin; a direct move is limited to ±${MAX_DELTA_M} m per axis. Nothing was changed.` };
      }
    }
  }
  return { ok: true, position, fields, message: null };
}

/**
 * What authoritative state says about a submitted envelope. submitted: {commandId, assumptionId,
 * entityId, baseRevision, baseDigest}. {outcome, toRevision, via, atBase}: 'accepted' (the command
 * log records it, or the fresh assumption it wrote is present), 'no-op' (the log says the service
 * changed nothing), or 'no-trace' (atBase: still the base revision and digest). No trace is not a
 * failure: a request still in flight or blocked on the service may yet be applied.
 */
export function commandEvidence(state, submitted) {
  if (!state || !state.project || !submitted) return { outcome: 'unknown', toRevision: null, via: null };
  const log = [...(state.history && Array.isArray(state.history.recent) ? state.history.recent : []), state.last_command]
    .filter((e) => e && e.command_id === submitted.commandId);
  const accepted = log.find((e) => e.status === 'accepted');
  if (accepted) return { outcome: 'accepted', toRevision: Number.isInteger(accepted.to_revision) ? accepted.to_revision : null, via: 'command-log' };
  if (log.some((e) => e.status === 'no_op')) return { outcome: 'no-op', toRevision: null, via: 'command-log' };
  const placements = state.project.physical && state.project.physical.placements;
  const placement = has(placements, submitted.entityId) ? placements[submitted.entityId] : null;
  if (placement && submitted.assumptionId && placement.assumption_id === submitted.assumptionId) return { outcome: 'accepted', toRevision: null, via: 'placement' };
  const assumptions = state.project.model && state.project.model.assumptions;
  if (submitted.assumptionId && has(assumptions, submitted.assumptionId)) return { outcome: 'accepted', toRevision: null, via: 'assumption' };
  const atBase = state.project.revision === submitted.baseRevision && digestOf(state) === submitted.baseDigest;
  return { outcome: 'no-trace', toRevision: null, via: 'state', atBase };
}

/** Connections at an item's ports whose physical route is not current (e.g. needs_reroute after a move). */
export function pendingIncidentRoutes(state, entityId) {
  if (!state || !state.project || !entityId) return 0;
  const { model, physical } = state.project;
  const ports = new Set(Object.entries(model.ports || {}).filter(([, p]) => p && p.entity_id === entityId).map(([id]) => id));
  return Object.entries(model.connections || {}).filter(([id, c]) => (ports.has(c.from_port_id) || ports.has(c.to_port_id))
    && physical.routes && physical.routes[id] && physical.routes[id].status !== 'current').length;
}

/** A final scene: an output file of a build that has finished (complete, pending, failed, ...). */
export function isFinalOffer(src) {
  return !!src && src.kind === 'output' && !ACTIVE_BUILD.has(src.build_status);
}

/**
 * Display hold of one Live 3D pane during a submitted move: the installed {url, src} to keep, or
 * null to follow the service as usual. A pane that shows the record's exact base scene keeps it
 * (and its labelled preview) while the service offers only progressive or unfinished files of a
 * newer build; a final scene is installed as soon as it is offered. Published panes never hold.
 */
export function moveHold({ record = null, mode = null, installed = null, offered = null } = {}) {
  if (!record || !SUBMITTED.has(record.phase) || mode !== 'live') return null;
  if (!installed || !installed.src) return null;
  if (installed.src.source_revision !== record.sourceRevision || installed.src.input_digest !== record.inputDigest) return null;
  if (!offered || offered.url === installed.url || isFinalOffer(offered)) return null;
  return installed;
}

/** Whether an installed {url, src} shows exactly the record's base scene identity. */
export function showsBase(record, installed) {
  return !!record && !!installed && !!installed.src && installed.src.source_revision === record.sourceRevision
    && installed.src.input_digest === record.inputDigest;
}

/**
 * Reason to drop an accepted record, or null to keep it: the model no longer has the moved
 * placement ('superseded'), the regeneration ended without a scene ('build-failed' /
 * 'build-cancelled'), or no shown Live 3D pane still displays the base scene ('installed').
 */
export function acceptedClearReason({ record, state, toRevision, livePanes = [] }) {
  if (!record || record.phase !== 'accepted' || !state || !state.project) return null;
  const placement = placementOf(state.project, record.entityId);
  if (placement.state !== 'placed' || !sameVec(placement.position, record.position) || !sameVec(placement.rotation, record.rotation)) return 'superseded';
  const b = state.build;
  if (b && Number.isInteger(toRevision) && b.source_revision >= toRevision && (b.status === 'failed' || b.status === 'cancelled')) return `build-${b.status}`;
  if (!livePanes.some((p) => p && p.active && p.mode === 'live' && showsBase(record, p.installed))) return 'installed';
  return null;
}

/**
 * The stateful coordinator. deps:
 * - getState(): the newest applied /api/state payload; isOnline(): boolean
 * - pane(index): {index, type, mode, active, installed, viewerSource} or null; panes(): all of them
 * - refetch(): Promise<{ok}> through the usual freshness gate (app.state is then updated)
 * - submit(command, label): Promise<{ok, state} | {ok: false, error}> (existing command transport)
 * - buildCommand(project, entityId, position, rotation): the existing buildPlacement envelope
 * - publish(record | null): mirror the record into every pane; cancelViewer(index, reason)
 * - notify(kind, title, lines, actions?); now(): ms
 * - otherActive() (optional): the pipe-section edit record, which blocks a new equipment move
 */
export function createMoveCoordinator(deps) {
  const now = deps.now || (() => Date.now());
  let record = null;
  let meta = null;          // {origin, baseUrl, tag, startedAt, projectId, service, submitted, toRevision}
  let outcome = null;       // last result shown by the origin pane
  const unresolved = [];    // hidden unconfirmed commands still watched: {submitted, tag, paneIndex, projectId, service, since}
  const stats = { begins: 0, refused: 0, previews: 0, cancels: 0, commits: 0, submissions: 0, accepted: 0, rejected: 0, uncertain: 0 };
  let lastRefusal = null;

  function set(next) {
    record = next ? makeRecord(next) : null;
    deps.publish(record);
  }

  function identity(state) {
    return { projectId: state && state.project ? state.project.id : null, service: state && state.service ? state.service.started_at ?? null : null };
  }

  function refuse(reason) {
    stats.refused += 1;
    lastRefusal = { reason, message: START_REASONS[reason] || reason, at: now() };
    return false;
  }

  function finish(kind, text, extra = {}) {
    outcome = { kind, text, at: now(), entityId: record ? record.entityId : extra.entityId ?? null, paneIndex: record ? record.paneIndex : extra.paneIndex ?? null, ...extra };
  }

  /** Start a preview (synchronous; a viewer needs the answer during its pointer press). */
  function begin(paneIndex, intent, { origin = 'pointer' } = {}) {
    if (record) return refuse(record.phase === 'uncertain' ? 'unconfirmed' : 'busy');
    if (deps.otherActive && deps.otherActive()) return refuse('pipe-busy'); // one equipment or pipe-section edit at a time
    const state = deps.getState();
    const pane = deps.pane(paneIndex);
    const gate = startGate({ state, online: deps.isOnline(), pane });
    if (!gate.ok) return refuse(gate.reason);
    if (!intent || typeof intent.entityId !== 'string' || !isFiniteVec3(intent.position)) return refuse('invalid-intent');
    if (intent.sourceRevision !== state.project.revision || intent.inputDigest !== digestOf(state)) return refuse('stale');
    const placed = placementGate(state.project, intent.entityId, intent.position);
    if (!placed.ok) return refuse(placed.reason);
    stats.begins += 1;
    lastRefusal = null;
    outcome = null;
    meta = {
      origin, baseUrl: pane.installed.url, tag: placed.placement.tag || intent.entityId, startedAt: now(),
      ...identity(state), submitted: null, toRevision: null,
    };
    set({
      paneIndex, entityId: intent.entityId, sourceRevision: state.project.revision, inputDigest: digestOf(state),
      basePosition: placed.placement.position, position: placed.placement.position, rotation: placed.placement.rotation, phase: 'preview',
    });
    return true;
  }

  function preview(position, paneIndex = null) {
    if (!record || record.phase !== 'preview') return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    if (!isFiniteVec3(position)) return false;
    stats.previews += 1;
    if (sameVec(position, record.position)) return true;
    set({ ...record, position });
    return true;
  }

  /** Cancel an unsubmitted preview and restore every view; a submitted move is not revoked. */
  function cancel(reason = 'cancelled', paneIndex = null) {
    if (!record || record.phase !== 'preview') return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    const r = record;
    const tag = meta ? meta.tag : r.entityId;
    record = null; // first: the viewer's own cancel callback below finds nothing left to cancel
    stats.cancels += 1;
    outcome = {
      kind: reason === 'no-op' ? 'no-op' : 'cancelled', reason, at: now(), entityId: r.entityId, paneIndex: r.paneIndex,
      text: reason === 'no-op' ? `${tag} is already at that position; nothing was sent.` : `Move of ${tag} cancelled (${String(reason).replace(/-/g, ' ')}); nothing was sent.`,
    };
    meta = null;
    deps.cancelViewer(r.paneIndex, reason);
    deps.publish(null);
    return true;
  }

  /** The origin pane and the model still hold the captured base: {ok, reason}. */
  function freshGate(r, { requireOnline = true } = {}) {
    const state = deps.getState();
    if (requireOnline && !deps.isOnline()) return { ok: false, reason: 'offline' };
    if (!state || !state.project) return { ok: false, reason: 'no-state' };
    const id = identity(state);
    if (meta && (id.projectId !== meta.projectId || id.service !== meta.service)) return { ok: false, reason: 'service-changed' };
    if (state.project.revision !== r.sourceRevision || digestOf(state) !== r.inputDigest) return { ok: false, reason: 'stale' };
    const placed = placementGate(state.project, r.entityId, r.basePosition, r.rotation);
    if (!placed.ok) return { ok: false, reason: placed.reason };
    const pane = deps.pane(r.paneIndex);
    if (!pane || pane.type !== '3d') return { ok: false, reason: 'destroyed' };
    if (!pane.active) return { ok: false, reason: 'hidden' };
    if (pane.mode !== 'live') return { ok: false, reason: 'published' };
    if (!pane.installed || (meta && pane.installed.url !== meta.baseUrl)) return { ok: false, reason: 'source-changed' };
    if (!viewerShows(pane)) return { ok: false, reason: 'source-changed' }; // the viewer's loaded scene is another one
    return { ok: true, reason: null };
  }

  function acceptedText(tag, from, to) {
    const routes = pendingIncidentRoutes(deps.getState(), record ? record.entityId : null);
    return `${tag} moved: r${from} → r${to ?? '?'}.${routes ? ` ${routes} incident route${routes === 1 ? '' : 's'} need${routes === 1 ? 's' : ''} rerouting (points kept).` : ''}`;
  }

  /** Settle a submitted move on definitive evidence only: accepted, or the service's "no change". */
  function resolve(evidence, how) {
    const tag = meta ? meta.tag : record.entityId;
    const sub = meta.submitted;
    if (evidence.outcome === 'accepted') {
      meta.toRevision = evidence.toRevision ?? sub.baseRevision + 1;
      stats.accepted += 1;
      set({ ...record, phase: 'accepted' });
      finish('accepted', acceptedText(tag, sub.baseRevision, meta.toRevision), { commandId: sub.commandId, via: evidence.via, how });
      if (how === 'reconciled') deps.notify('ok', `Move ${tag}: accepted`, [`Confirmed from the service state (${evidence.via}); nothing was resent.`]);
      return;
    }
    const r = record;
    record = null;
    finish('no-op', `${tag}: the service reported no change; the revision was kept.`, { entityId: r.entityId, paneIndex: r.paneIndex, commandId: sub.commandId });
    meta = null;
    deps.publish(null);
  }

  /** Text of a still unconfirmed command, from what the newest state shows (never a failure claim). */
  function unconfirmedText(tag, sub, evidence, state) {
    const at = state && state.project ? state.project.revision : null;
    const moved = evidence && evidence.outcome === 'no-trace' && !evidence.atBase && at !== null
      ? ` The model is now r${at}; this command was prepared for r${sub.baseRevision}, and the service applies it only at that revision.` : '';
    const checked = at !== null ? ` No record of it yet at r${at}.` : '';
    return `Unconfirmed: the service has not confirmed the move of ${tag}.${checked} It may still be applied; nothing is resent.${moved}`;
  }

  /** Commit the preview: at most one validated move_equipment envelope through the existing transport. */
  async function commit(position, paneIndex = null) {
    if (!record || record.phase !== 'preview') return { ok: false, reason: record ? 'submitted' : 'no-preview' };
    if (paneIndex != null && paneIndex !== record.paneIndex) return { ok: false, reason: 'other-pane' };
    if (!isFiniteVec3(position)) return { ok: false, reason: 'invalid' };
    stats.commits += 1;
    if (!sameVec(position, record.position)) set({ ...record, position });
    // no-op first: buildCommand would allocate a fresh assumption for an unchanged placement
    if (isNoOp(record.basePosition, position)) {
      cancel('no-op');
      return { ok: false, reason: 'no-op' };
    }
    let gate = freshGate(record);
    if (!gate.ok) {
      cancel(gate.reason);
      return { ok: false, reason: gate.reason };
    }
    const tag = meta.tag;
    set({ ...record, phase: 'submitting' }); // locks further commits and new moves
    finish('submitting', `Submitting the move of ${tag}…`);
    const fresh = await deps.refetch(); // recheck revision and digest on the newest state before sending
    gate = fresh && fresh.ok ? freshGate(record) : { ok: false, reason: 'offline' };
    if (!gate.ok) {
      const r = record;
      record = null;
      meta = null;
      finish('not-sent', `Nothing was sent: ${START_REASONS[gate.reason] || gate.reason.replace(/-/g, ' ')}`, { entityId: r.entityId, paneIndex: r.paneIndex });
      deps.notify('warn', `Move ${tag}: not sent`, [outcome.text]);
      deps.publish(null);
      return { ok: false, reason: gate.reason };
    }
    const state = deps.getState();
    let command;
    try {
      command = deps.buildCommand(state.project, record.entityId, record.position.slice(), record.rotation.slice());
    } catch (error) {
      const r = record;
      record = null;
      meta = null;
      finish('not-sent', `Nothing was sent: the move command could not be prepared (${error && error.message ? error.message : error}).`, { entityId: r.entityId, paneIndex: r.paneIndex });
      deps.notify('err', `Move ${tag}: not sent`, [outcome.text]);
      deps.publish(null);
      return { ok: false, reason: 'not-sent' };
    }
    meta.submitted = {
      commandId: command.id, assumptionId: command.payload && command.payload.placement ? command.payload.placement.assumption_id : null,
      entityId: record.entityId, baseRevision: record.sourceRevision, baseDigest: record.inputDigest, sentAt: now(),
    };
    stats.submissions += 1;
    const result = await deps.submit(command, `Move ${tag}`);
    if (!record || !meta || !meta.submitted || meta.submitted.commandId !== command.id) return { ok: false, reason: 'superseded' };
    if (result && result.ok) {
      const evidence = commandEvidence(result.state, meta.submitted);
      if (evidence.outcome === 'accepted' || evidence.outcome === 'no-op') {
        resolve(evidence, 'answer');
        return { ok: evidence.outcome === 'accepted', reason: evidence.outcome, commandId: command.id };
      }
    }
    const error = result && result.error;
    const definitive = !!error && !error.network && error.status >= 400 && error.status < 500;
    if (definitive) {
      const r = record;
      record = null;
      meta = null;
      stats.rejected += 1;
      const detail = [error.error, ...(error.diagnostics || []).map((d) => `[${d.code}] ${d.message}`)].filter(Boolean);
      finish('rejected', `Rejected by the service (HTTP ${error.status}): ${detail[0] || 'no reason given'}. Nothing was changed.`, { entityId: r.entityId, paneIndex: r.paneIndex, commandId: command.id });
      deps.notify(error.status === 409 ? 'warn' : 'err', `Move ${tag}: rejected`, [...detail, 'The revision is unchanged; the preview was removed.']);
      deps.publish(null);
      return { ok: false, reason: 'rejected', status: error.status, commandId: command.id };
    }
    // no answer, a server error or an answer without this command: unconfirmed until evidence, never resent
    stats.uncertain += 1;
    meta.submitted.reason = error ? error.error || `HTTP ${error.status}` : 'the answer does not mention this command';
    set({ ...record, phase: 'uncertain' });
    finish('uncertain', unconfirmedText(tag, meta.submitted, null, null), { commandId: command.id, cause: meta.submitted.reason });
    deps.notify('warn', `Move ${tag}: unconfirmed`, [`No confirmation from the service (${meta.submitted.reason}). The move may still be applied; it is not sent again.`]);
    reconcile();
    if (record && record.phase === 'uncertain') deps.refetch();
    return { ok: false, reason: record && record.phase === 'accepted' ? 'accepted' : 'uncertain', commandId: command.id };
  }

  /**
   * An unconfirmed move against the newest state: settled only by definitive evidence (the command
   * log or the envelope's fresh assumption: accepted; the log's "no change"). No trace keeps it
   * unconfirmed, because the request may still be in flight or blocked on the service. A changed
   * project or service run (the old request can no longer land in this session) ends the watch.
   */
  function reconcile(state = deps.getState()) {
    if (!record || record.phase !== 'uncertain' || !meta || !meta.submitted || !state || !state.project) return;
    const evidence = commandEvidence(state, meta.submitted);
    if (evidence.outcome === 'accepted' || evidence.outcome === 'no-op') {
      resolve(evidence, 'reconciled');
      return;
    }
    const id = identity(state);
    if (id.projectId !== meta.projectId || id.service !== meta.service) {
      const r = record;
      const tag = meta.tag;
      record = null;
      finish('session-changed', `The ${id.service !== meta.service ? 'service was restarted' : 'project changed'}; the unconfirmed move of ${tag} is no longer watched. The model now (r${state.project.revision}) shows no trace of it.`, { entityId: r.entityId, paneIndex: r.paneIndex, commandId: meta.submitted.commandId });
      meta = null;
      deps.publish(null);
      return;
    }
    const text = unconfirmedText(meta.tag, meta.submitted, evidence, state);
    if (!outcome || outcome.text !== text) finish('uncertain', text, { commandId: meta.submitted.commandId, cause: meta.submitted.reason, checkedRevision: state.project.revision });
  }

  /**
   * Hide the preview of an unconfirmed move (an explicit user action). This neither cancels nor
   * undoes anything: the request may still land. The command stays watched (at most MAX_UNRESOLVED)
   * and is reported if it is applied; a new move may now start from freshly verified state.
   */
  function dismiss(paneIndex = null) {
    if (!record || record.phase !== 'uncertain' || !meta || !meta.submitted) return false;
    if (paneIndex != null && paneIndex !== record.paneIndex) return false;
    const r = record;
    unresolved.push({ submitted: { ...meta.submitted }, tag: meta.tag, paneIndex: r.paneIndex, projectId: meta.projectId, service: meta.service, since: now() });
    while (unresolved.length > MAX_UNRESOLVED) unresolved.shift();
    record = null;
    finish('unconfirmed', `Preview hidden. The move of ${meta.tag} is still unconfirmed: it may yet be applied, and you will be told if it is. Nothing was cancelled, undone or resent.`, { entityId: r.entityId, paneIndex: r.paneIndex, commandId: meta.submitted.commandId });
    meta = null;
    deps.publish(null);
    return true;
  }

  /** Hidden unconfirmed commands: report any that land; drop them when the session changes. */
  function watchUnresolved(state) {
    if (!unresolved.length || !state || !state.project) return;
    const id = identity(state);
    for (const entry of unresolved.slice()) {
      const evidence = commandEvidence(state, entry.submitted);
      const settled = evidence.outcome === 'accepted' || evidence.outcome === 'no-op';
      const ended = id.projectId !== entry.projectId || id.service !== entry.service;
      if (!settled && !ended) continue;
      unresolved.splice(unresolved.indexOf(entry), 1);
      if (evidence.outcome === 'accepted') {
        deps.notify('warn', `Move ${entry.tag}: applied`, [`The move whose preview was hidden while unconfirmed has been applied (model r${state.project.revision}; ${evidence.via}). It was not resent.`]);
        if (!record) finish('confirmed-late', `The hidden unconfirmed move of ${entry.tag} was applied (model r${state.project.revision}). It was not resent.`, { entityId: entry.submitted.entityId, paneIndex: entry.paneIndex, commandId: entry.submitted.commandId });
      } else if (evidence.outcome === 'no-op' && !record) {
        finish('no-op', `${entry.tag}: the service reported no change for the hidden unconfirmed move.`, { entityId: entry.submitted.entityId, paneIndex: entry.paneIndex, commandId: entry.submitted.commandId });
      }
    }
  }

  function livePanes() {
    return (deps.panes() || []).filter((p) => p && p.type === '3d');
  }

  /** Drop an accepted record once the shown Live panes have moved on (or the model no longer has it). */
  function sweep() {
    if (!record || record.phase !== 'accepted') return;
    const reason = acceptedClearReason({ record, state: deps.getState(), toRevision: meta ? meta.toRevision : null, livePanes: livePanes() });
    if (!reason) return;
    const r = record;
    record = null;
    if (outcome && outcome.kind === 'accepted') outcome = { ...outcome, cleared: reason, clearedAt: now() };
    if (reason === 'superseded') finish('superseded', `The model no longer has this move of ${meta ? meta.tag : r.entityId} (undone or changed again); the preview was removed.`, { entityId: r.entityId, paneIndex: r.paneIndex });
    else if (reason !== 'installed') finish(reason, `${meta ? meta.tag : r.entityId} moved, but regeneration ended ${reason.replace('build-', '')}; the preview was removed and the views show what the service offers.`, { entityId: r.entityId, paneIndex: r.paneIndex });
    meta = null;
    deps.publish(null);
  }

  /** The origin pane or the model changed: an unsubmitted preview whose base no longer holds ends. */
  function check() {
    if (!record || record.phase !== 'preview') return;
    const gate = freshGate(record);
    if (!gate.ok) cancel(gate.reason);
  }

  function stateChanged(state) {
    watchUnresolved(state);
    if (!record) return;
    if (record.phase === 'preview') check();
    else if (record.phase === 'uncertain') reconcile(state);
    else if (record.phase === 'accepted') sweep();
  }

  return {
    record: () => record,
    begin,
    preview,
    cancel,
    commit,
    /** Escape and the Cancel button apply only to an unsubmitted preview. */
    cancellable: () => !!record && record.phase === 'preview',
    dismiss,
    /** Hidden unconfirmed commands still watched (copies). */
    unresolved: () => unresolved.map((u) => ({ commandId: u.submitted.commandId, entityId: u.submitted.entityId, tag: u.tag, paneIndex: u.paneIndex, baseRevision: u.submitted.baseRevision, since: u.since })),
    stateChanged,
    /** Lost connection ends an unsubmitted preview; reconnecting is answered by the next applied state. */
    onlineChanged(online) {
      if (!online) cancel('offline');
    },
    /** A pane changed type, mode, visibility or installed scene. */
    paneChanged() {
      check();
      sweep();
    },
    /** The shared selection changed: a preview of an item no longer selected in its pane ends. */
    selectionChanged(ids) {
      if (!record || record.phase !== 'preview') return;
      const state = deps.getState();
      const owner = selectionOwner({ ids }, state ? state.project : null);
      if (owner.entityId !== record.entityId || owner.reason) cancel('selection-changed');
    },
    sweep,
    hold: (args) => moveHold({ record, ...args }),
    outcome: () => outcome,
    refusal: () => lastRefusal,
    tag: () => (meta ? meta.tag : null),
    snapshot: () => ({
      record: record ? { ...record, basePosition: record.basePosition.slice(), position: record.position.slice(), rotation: record.rotation.slice() } : null,
      origin: meta ? meta.origin : null,
      baseUrl: meta ? meta.baseUrl : null,
      submitted: meta && meta.submitted ? { ...meta.submitted } : null,
      toRevision: meta ? meta.toRevision : null,
      outcome: outcome ? { ...outcome } : null,
      refusal: lastRefusal ? { ...lastRefusal } : null,
      unresolved: unresolved.map((u) => ({ commandId: u.submitted.commandId, entityId: u.submitted.entityId, tag: u.tag, paneIndex: u.paneIndex, baseRevision: u.submitted.baseRevision, since: u.since })),
      stats: { ...stats },
    }),
  };
}
