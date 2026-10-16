// Direct translation of one placed equipment item in the 3D view (pure; unit-tested in Node).
//
// The view only reports intent. It previews a world delta on the scene it has installed and
// hands the proposed origin to the application, which owns the one global move record and the
// validated move_equipment command. Scene vertices are already world metres (the generator
// applied each placement), so a preview translates every owner mesh, edge, label and port by
// the same delta from the captured base origin; the placement and its rotation are never
// applied again. A delta is only ever drawn on the exact scene identity it was captured on, so
// regenerated geometry cannot receive it a second time. A move starts only on a complete final
// Live scene of exactly the current revision and document digest (the digest is supplied
// separately as context.inputDigest; the domain project carries none). No DOM, Three.js,
// network or project mutation here.

export const DEFAULT_SNAP_M = 0.05;
/** Largest per-axis preview delta; a pointer ray grazing a drag plane cannot propose a far jump. */
export const MAX_DELTA_M = 100;
export const MAX_SNAP_M = 10;
export const PHASES = Object.freeze(['preview', 'submitting', 'accepted', 'uncertain']);
const MICROMETRE = 1e6; // moved coordinates are rounded to the micrometre; unmoved axes keep the base exactly
const NO_OP_M = 1e-9;

export const MOVE_REASONS = Object.freeze({
  'no-context': 'The 3D view has no current model context.',
  none: 'Select one placed equipment item to move it.',
  multiple: 'Select only one equipment item; moving several items is not supported.',
  connection: 'A line moves by its straight sections: pick a straight run (or use the Section list) with Move on.',
  missing: 'The selected item does not exist in the current model.',
  unplaced: 'The item has no placed physical position; place it in the inspector first.',
  offline: 'The local service is not reachable.',
  published: 'Published views are read-only; show this pane as Live to move equipment.',
  'no-scene': 'No 3D scene is installed in this pane.',
  'source-mismatch': 'This pane is still installing its scene.',
  progressive: 'A working preview is shown; wait for the final scene.',
  incomplete: 'The installed scene is not a complete output (failed, cancelled, superseded or rejected build).',
  pending: 'The outputs are pending (for example routes need rerouting); move again once they are complete.',
  'digest-unknown': 'The current model digest is unknown, so the scene cannot be matched to it.',
  stale: 'The installed scene does not show the current model revision.',
  'not-in-scene': 'The item has no geometry in the installed scene.',
  'display-only': 'Display-only markers are not moved directly.',
  busy: 'Another equipment move is in progress.',
});

const has = (obj, key) => !!obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key);

export function isFiniteVec3(v) {
  return Array.isArray(v) && v.length === 3 && v.every((x) => typeof x === 'number' && Number.isFinite(x));
}

/** A snap step in metres, or null for off (null, 0 or false); anything else is rejected. */
export function normalizeSnap(step) {
  if (step === null || step === undefined || step === false || step === 0) return null;
  if (typeof step !== 'number' || !Number.isFinite(step) || step < 0 || step > MAX_SNAP_M) {
    throw new RangeError(`snap step must be null (off) or a number of metres in (0, ${MAX_SNAP_M}]`);
  }
  return step;
}

/**
 * Authoritative placement of an entity in the current model:
 * {state: 'placed', tag, position, rotation, manual} | {state: 'unplaced'|'invalid', tag, reason}
 * | {state: 'connection'|'missing'|'unknown'}. Positions are copies.
 */
export function placementOf(project, entityId) {
  const model = project && project.model;
  if (!model || !model.entities) return { state: 'unknown' };
  if (!has(model.entities, entityId)) return { state: has(model.connections, entityId) ? 'connection' : 'missing' };
  const tag = model.entities[entityId].tag;
  const placements = project.physical && project.physical.placements;
  const p = has(placements, entityId) ? placements[entityId] : null;
  if (!p || p.status !== 'placed') return { state: 'unplaced', tag, reason: p && typeof p.reason === 'string' ? p.reason : null };
  if (!isFiniteVec3(p.position_m) || !isFiniteVec3(p.rotation_deg)) return { state: 'invalid', tag, reason: 'placement is not three finite numbers' };
  return { state: 'placed', tag, position: p.position_m.slice(), rotation: p.rotation_deg.slice(), manual: !!p.manual };
}

/**
 * The single engineering owner of a 3D selection: {entityId, reason}. `ids` are the selected
 * semantic IDs when the caller knows them (entities, ports or connections); otherwise owners come
 * from the selected scene components' semantic IDs and the selected ports' entities. A nozzle,
 * motor or support resolves to its equipment item. reason: null | 'none' | 'multiple' |
 * 'connection' | 'missing' (entityId then names the unknown ID).
 */
export function selectionOwner({ ids = null, componentOwners = [], portOwners = [] } = {}, project = null) {
  const model = project && project.model;
  const owners = new Set();
  let connections = 0;
  let missing = null;
  const add = (id) => {
    if (typeof id !== 'string' || !id) return;
    if (!model) owners.add(id);
    else if (has(model.entities, id)) owners.add(id);
    else if (has(model.ports, id)) owners.add(model.ports[id].entity_id);
    else if (has(model.connections, id)) connections += 1;
    else missing = missing || id;
  };
  if (Array.isArray(ids)) ids.forEach(add);
  else {
    componentOwners.forEach(add);
    portOwners.forEach(add);
  }
  if (owners.size + connections + (missing ? 1 : 0) === 0) return { entityId: null, reason: 'none' };
  if (owners.size > 1 || (owners.size && (connections || missing))) return { entityId: null, reason: 'multiple' };
  if (owners.size === 1) return { entityId: [...owners][0], reason: null };
  if (connections && !missing) return { entityId: null, reason: 'connection' };
  if (missing && !connections) return { entityId: missing, reason: 'missing' };
  return { entityId: null, reason: 'multiple' };
}

/** Scene identity of an installed source: {revision, digest} or null. */
export function identityOf(src) {
  if (!src) return null;
  return { revision: src.source_revision, digest: src.input_digest };
}

/**
 * The current document digest the application supplied with the context (the service state's
 * document input_digest), or null. Only the explicit top-level context.inputDigest counts: a digest
 * is never read from, or expected in, the domain project.
 */
export function contextDigest(context) {
  const digest = context ? context.inputDigest : null;
  return typeof digest === 'string' && digest ? digest : null;
}

const DONE = 'complete';
const OPEN = 'pending';

/**
 * Whether a move may start on the scene a view shows: online, Live, a final complete build whose
 * revision and digest are exactly the current model's, and the same file the pane's loader reports
 * installed. A pending build (every output generated, open items such as routes needing reroute) stays
 * viewable but is not a start point until the reroute/publication workflow yields complete output.
 * {ok, reason}. context: {project, inputDigest, online, mode, installedSource: {url, src}};
 * viewerSource: what the view itself has installed ({url, source_revision, input_digest, final}).
 */
export function sourceGate(context, viewerSource) {
  const fail = (reason) => ({ ok: false, reason });
  if (!context || !context.project) return fail('no-context');
  if (context.online !== true) return fail('offline');
  if (context.mode !== 'live') return fail('published');
  const installed = context.installedSource;
  if (!installed || !installed.src || !viewerSource) return fail('no-scene');
  if (installed.url !== viewerSource.url) return fail('source-mismatch');
  const src = installed.src;
  if (!viewerSource.final || src.kind === 'preview') return fail('progressive');
  const status = [src.build_status, src.artifact_status];
  if (src.integrity === 'rejected' || !status.every((s) => s === DONE || s === OPEN)) return fail('incomplete');
  const digest = contextDigest(context);
  if (!digest) return fail('digest-unknown');
  const revision = context.project.revision;
  if (src.source_revision !== revision || src.input_digest !== digest
    || viewerSource.source_revision !== revision || viewerSource.input_digest !== digest) return fail('stale');
  if (!status.every((s) => s === DONE)) return fail('pending');
  return { ok: true, reason: null };
}

/**
 * Whether the selected item can be moved in this view: {ok, reason, message, entityId, tag, base,
 * rotation, sourceRevision, inputDigest, intent}. context as for sourceGate (inputDigest is the
 * intent's digest); owner from selectionOwner; scene: {components,
 * physical} counts of the owner's installed (non-ghost) components and those not display-only;
 * local: the view's own gesture (its record is not "another move").
 */
export function moveEligibility({ context = null, viewerSource = null, owner = null, scene = null, local = null } = {}) {
  const result = (reason, extra = {}) => ({
    ok: !reason, reason, message: reason ? MOVE_REASONS[reason] || reason : null, entityId: null, tag: null,
    base: null, rotation: null, sourceRevision: null, inputDigest: null, intent: null, ...extra,
  });
  if (!context || !context.project) return result('no-context');
  const who = owner || { entityId: null, reason: 'none' };
  if (who.reason) return result(who.reason, { entityId: who.entityId || null });
  const placement = placementOf(context.project, who.entityId);
  const named = { entityId: who.entityId, tag: placement.tag || null };
  if (placement.state === 'connection') return result('connection', named);
  if (placement.state === 'missing' || placement.state === 'unknown') return result('missing', named);
  if (placement.state !== 'placed') return result('unplaced', named);
  const gate = sourceGate(context, viewerSource);
  if (!gate.ok) return result(gate.reason, named);
  const counts = scene || { components: 0, physical: 0 };
  if (!counts.components) return result('not-in-scene', named);
  if (!counts.physical) return result('display-only', named);
  if (local && local.stage === 'committed') return result('busy', named); // submitted, awaiting the application
  const move = context.move;
  const ours = local && move && move.entityId === local.entityId && sameBase(move, local);
  if (move && !ours) return result('busy', named);
  const sourceRevision = context.project.revision;
  const inputDigest = contextDigest(context);
  return result(null, {
    ...named, base: placement.position, rotation: placement.rotation, sourceRevision, inputDigest,
    intent: { entityId: who.entityId, position: placement.position.slice(), sourceRevision, inputDigest },
  });
}

/**
 * Why a view's unsubmitted drag must end now, or null to continue. local: the gesture {entityId,
 * sourceRevision, inputDigest, url, recordSeen}; viewerSource as for sourceGate. The current model
 * must still be exactly the drag's base (a missing digest fails closed), on the same installed file,
 * with the application's record (once seen) still describing this drag.
 */
export function dragEndReason(context, local, viewerSource) {
  if (!local) return null;
  if (!context || !context.project) return 'no-context';
  if (context.online !== true) return 'offline';
  if (context.mode !== 'live') return 'published';
  const digest = contextDigest(context);
  if (!digest) return 'digest-unknown';
  if (context.project.revision !== local.sourceRevision || digest !== local.inputDigest) return 'stale';
  const installed = context.installedSource;
  if (!installed || installed.url !== local.url || !viewerSource || viewerSource.url !== local.url) return 'source-changed';
  const gate = sourceGate(context, viewerSource);
  if (!gate.ok) return gate.reason;
  const move = context.move;
  if (move && !(move.entityId === local.entityId && sameBase(move, local))) return 'superseded';
  if (!move && local.recordSeen) return 'app-cancelled';
  return null;
}

export function worldDelta(base, position) {
  return [position[0] - base[0], position[1] - base[1], position[2] - base[2]];
}

export function applyDelta(point, delta) {
  return [point[0] + delta[0], point[1] + delta[1], point[2] + delta[2]];
}

/** True when the proposed origin equals the base: release sends no command. */
export function isNoOp(base, position) {
  if (!isFiniteVec3(base) || !isFiniteVec3(position)) return true;
  return worldDelta(base, position).every((d) => Math.abs(d) < NO_OP_M);
}

/**
 * Proposed origin for a raw gizmo position. Only the axes of the dragged handle ('X', 'XY', ...)
 * move; the world delta from the base is quantized to the snap step (null = off), so an item keeps
 * its existing sub-step offsets and pointer jitter below half a step is a no-op. Deltas are capped
 * at ±MAX_DELTA_M per axis; moved coordinates are rounded to the micrometre. Null if not finite.
 */
export function snapPosition(base, raw, step = null, axis = 'XYZ') {
  if (!isFiniteVec3(base) || !isFiniteVec3(raw)) return null;
  const letters = typeof axis === 'string' && axis ? axis : 'XYZ';
  return base.map((b, i) => {
    if (!letters.includes('XYZ'[i])) return b;
    let d = raw[i] - b;
    if (step) d = Math.round(d / step) * step;
    d = Math.max(-MAX_DELTA_M, Math.min(MAX_DELTA_M, d));
    if (Math.abs(d) < NO_OP_M) return b;
    return Math.round((b + d) * MICROMETRE) / MICROMETRE;
  });
}

/** The same scene identity: a delta captured on one may be drawn on the other. */
export function sameBase(a, b) {
  return !!a && !!b && a.sourceRevision === b.sourceRevision && a.inputDigest === b.inputDigest;
}

/**
 * The preview a view draws now, or null: {entityId, base, position, delta, phase, origin}.
 * - local: the view's own gesture {entityId, base, position, sourceRevision, inputDigest, stage}.
 *   'drag' draws the pointer's proposal; 'committed' bridges the moment between its commit and the
 *   application's record arriving.
 * - move: the application's global record {entityId, sourceRevision, inputDigest, basePosition,
 *   position, phase}; it is mirrored only into a Live view whose installed final scene is exactly
 *   the record's base identity. Published views keep their own source, and regenerated or other
 *   geometry (another identity) never receives the delta.
 * Display is independent of the start gate: a submitted, accepted or uncertain record keeps its
 * captured delta on its base scene after the model has moved on, until another scene is installed.
 */
export function effectivePreview({ local = null, move = null, viewerSource = null, mode = null } = {}) {
  const shown = viewerSource ? { sourceRevision: viewerSource.source_revision, inputDigest: viewerSource.input_digest } : null;
  const usable = mode === 'live' && !!viewerSource && !!viewerSource.final;
  const make = (entityId, base, position, phase, origin) => (
    isFiniteVec3(base) && isFiniteVec3(position)
      ? {
        entityId, base: base.slice(), position: position.slice(), delta: worldDelta(base, position), phase, origin,
        sourceRevision: shown.sourceRevision, inputDigest: shown.inputDigest,
      }
      : null);
  if (local && (local.stage === 'drag' || local.stage === 'committed')) {
    if (!usable || !sameBase(local, shown)) return null;
    if (local.stage === 'drag') return make(local.entityId, local.base, local.position, 'preview', 'local');
    if (!move) return make(local.entityId, local.base, local.position, 'submitting', 'local');
  }
  if (!move || !usable || !PHASES.includes(move.phase) || !sameBase(move, shown)) return null;
  return make(move.entityId, move.basePosition, move.position, move.phase, 'mirror');
}

/**
 * IDs a preview of entityId translates in one installed scene: its components (by semantic ID),
 * its labels and its ports. components: [{id, semanticId}], labels: [semantic IDs], ports: [{id, entity_id}].
 */
export function previewTargets({ components = [], labels = [], ports = [] } = {}, entityId) {
  if (!entityId) return { components: [], labels: [], ports: [] };
  return {
    components: components.filter((c) => c && c.semanticId === entityId).map((c) => c.id),
    labels: labels.filter((id) => id === entityId),
    ports: ports.filter((p) => p && p.entity_id === entityId && isFiniteVec3(p.position_m)).map((p) => p.id),
  };
}
