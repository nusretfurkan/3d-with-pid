// Pure rules of the 3D coordinate overlay and direct equipment translation
// (web/js/equipment-move.js, web/js/coordinates.js): gates, deltas, snapping and preview
// reconciliation. The viewer itself is exercised in a real browser by the executor harness.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  DEFAULT_SNAP_M, MAX_DELTA_M, MOVE_REASONS, applyDelta, contextDigest, dragEndReason, effectivePreview, isNoOp,
  moveEligibility, normalizeSnap, placementOf, previewTargets, sameBase, selectionOwner, snapPosition, sourceGate, worldDelta,
} from '../../../web/js/equipment-move.js';
import { coordinateReadout, formatMetres, gridExtent, gridLines } from '../../../web/js/coordinates.js';

const JS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'web', 'js');
const DIGEST = 'd'.repeat(64);

// The domain project exactly as the service returns it: no digest field. The current document
// digest travels separately as the context's top-level inputDigest (state.document.input_digest).
function project(overrides = {}) {
  return {
    id: 'project-test', revision: 7,
    model: {
      entities: {
        'ent-pump': { id: 'ent-pump', tag: 'P-101', kind: 'centrifugal_pump' },
        'ent-valve': { id: 'ent-valve', tag: 'HV-102', kind: 'isolation_valve' },
        'ent-new': { id: 'ent-new', tag: 'HV-103', kind: 'isolation_valve' },
        'ent-boundary': { id: 'ent-boundary', tag: 'OUT-101', kind: 'boundary' },
      },
      ports: {
        'port-pump-out': { id: 'port-pump-out', entity_id: 'ent-pump', name: 'outlet' },
        'port-valve-in': { id: 'port-valve-in', entity_id: 'ent-valve', name: 'inlet' },
      },
      connections: { 'conn-1': { id: 'conn-1', tag: 'L-101', from_port_id: 'port-pump-out', to_port_id: 'port-valve-in' } },
    },
    physical: {
      placements: {
        'ent-pump': { position_m: [1.5, 0, 0], rotation_deg: [0, 0, 0], status: 'placed', manual: false, assumption_id: 'a' },
        'ent-valve': { position_m: [2.7, -0.45, 0.7], rotation_deg: [0, 0, -90], status: 'placed', manual: false, assumption_id: 'a' },
        'ent-new': { position_m: null, rotation_deg: null, status: 'unresolved', manual: false, assumption_id: 'a', reason: 'inserted without a position' },
        'ent-boundary': { position_m: [2.7, -3.35, 0.7], rotation_deg: [0, 0, -90], status: 'placed', manual: true, assumption_id: 'a' },
      },
      routes: {},
    },
    ...overrides,
  };
}

const URL = '/artifacts/b-r7/scene.json';
function src(overrides = {}) {
  return {
    url: URL, origin: 'published', kind: 'output', build_id: 'b-r7', build_status: 'complete', artifact_status: 'complete',
    source_revision: 7, input_digest: DIGEST, ...overrides,
  };
}
function context(overrides = {}) {
  const s = src(overrides.src || {});
  const { src: _ignored, ...rest } = overrides;
  return { project: project(), inputDigest: DIGEST, online: true, mode: 'live', installedSource: { url: s.url, src: s }, move: null, ...rest };
}
function viewer(overrides = {}) {
  return { url: URL, source_revision: 7, input_digest: DIGEST, final: true, ...overrides };
}
const pumpScene = { components: 13, physical: 13 };
const eligible = (overrides = {}) => moveEligibility({
  context: context(overrides.context || {}), viewerSource: viewer(overrides.viewer || {}),
  owner: overrides.owner || { entityId: 'ent-pump', reason: null }, scene: overrides.scene || pumpScene, local: overrides.local || null,
});

test('pure helpers stay free of DOM and Three.js', () => {
  for (const file of ['equipment-move.js', 'coordinates.js']) {
    const source = fs.readFileSync(path.join(JS, file), 'utf8').replace(/\/\/.*$/gm, '');
    assert.ok(!/from\s+'three/.test(source), `${file} imports three`);
    assert.ok(!/\b(document|window)\./.test(source), `${file} touches the DOM`);
    assert.ok(!/\bfetch\(/.test(source), `${file} uses the network`);
  }
});

test('snap step: default 0.05 m, explicit off, invalid values rejected', () => {
  assert.equal(DEFAULT_SNAP_M, 0.05);
  assert.equal(normalizeSnap(null), null);
  assert.equal(normalizeSnap(0), null);
  assert.equal(normalizeSnap(false), null);
  assert.equal(normalizeSnap(0.1), 0.1);
  for (const bad of [-0.05, Number.NaN, Infinity, '0.05', 11, {}]) assert.throws(() => normalizeSnap(bad), RangeError, String(bad));
});

test('authoritative placement: placed, unplaced, missing, connection and prototype keys', () => {
  const p = project();
  const placed = placementOf(p, 'ent-valve');
  assert.deepEqual(placed, { state: 'placed', tag: 'HV-102', position: [2.7, -0.45, 0.7], rotation: [0, 0, -90], manual: false });
  placed.position[0] = 99; // a copy: the project is never touched
  assert.equal(p.physical.placements['ent-valve'].position_m[0], 2.7);
  assert.deepEqual(placementOf(p, 'ent-new'), { state: 'unplaced', tag: 'HV-103', reason: 'inserted without a position' });
  assert.equal(placementOf(p, 'ent-gone').state, 'missing');
  assert.equal(placementOf(p, 'conn-1').state, 'connection');
  assert.equal(placementOf(p, 'constructor').state, 'missing');
  assert.equal(placementOf(null, 'ent-pump').state, 'unknown');
  const broken = project();
  broken.physical.placements['ent-pump'].position_m = [1, Number.NaN, 0];
  assert.equal(placementOf(broken, 'ent-pump').state, 'invalid');
});

test('selection owner: parts and ports resolve to their equipment item; pipes, several or unknown items do not', () => {
  const p = project();
  assert.deepEqual(selectionOwner({ ids: ['ent-pump'] }, p), { entityId: 'ent-pump', reason: null });
  assert.deepEqual(selectionOwner({ ids: ['port-pump-out'] }, p), { entityId: 'ent-pump', reason: null });
  assert.deepEqual(selectionOwner({ ids: ['ent-pump', 'port-pump-out'] }, p), { entityId: 'ent-pump', reason: null });
  assert.deepEqual(selectionOwner({ ids: ['conn-1'] }, p), { entityId: null, reason: 'connection' });
  assert.deepEqual(selectionOwner({ ids: ['ent-pump', 'ent-valve'] }, p), { entityId: null, reason: 'multiple' });
  assert.deepEqual(selectionOwner({ ids: ['ent-pump', 'conn-1'] }, p), { entityId: null, reason: 'multiple' });
  assert.deepEqual(selectionOwner({ ids: ['ent-deleted'] }, p), { entityId: 'ent-deleted', reason: 'missing' });
  assert.deepEqual(selectionOwner({ ids: [] }, p), { entityId: null, reason: 'none' });
  // without explicit IDs: a motor/nozzle component's semantic ID and a selected port's entity
  assert.deepEqual(selectionOwner({ componentOwners: ['ent-pump', 'ent-pump'], portOwners: ['ent-pump'] }, p), { entityId: 'ent-pump', reason: null });
  assert.deepEqual(selectionOwner({ componentOwners: ['conn-1'] }, p), { entityId: null, reason: 'connection' });
  assert.deepEqual(selectionOwner({}, p), { entityId: null, reason: 'none' });
});

test('eligible: online Live final complete scene of exactly the current revision and digest', () => {
  const r = eligible();
  assert.equal(r.ok, true);
  assert.equal(r.reason, null);
  assert.deepEqual(r.base, [1.5, 0, 0]);
  assert.deepEqual(r.rotation, [0, 0, 0]);
  assert.deepEqual(r.intent, { entityId: 'ent-pump', position: [1.5, 0, 0], sourceRevision: 7, inputDigest: DIGEST });
  assert.equal(r.tag, 'P-101');
  assert.equal(r.inputDigest, DIGEST);
  assert.equal(r.sourceRevision, 7);
});

test('current digest: only the explicit context.inputDigest counts; missing fails closed', () => {
  assert.equal(contextDigest(context()), DIGEST);
  for (const bad of [undefined, null, '', 7, {}]) {
    assert.equal(contextDigest(context({ inputDigest: bad })), null, String(bad));
    assert.equal(eligible({ context: { inputDigest: bad } }).reason, 'digest-unknown', String(bad));
    assert.equal(sourceGate(context({ inputDigest: bad }), viewer()).reason, 'digest-unknown', String(bad));
  }
  // the frozen context shape is accepted as is; an input_digest invented inside the domain project is ignored
  const frozen = { ...context(), project: project() };
  assert.ok(!('input_digest' in frozen.project));
  assert.deepEqual(sourceGate(frozen, viewer()), { ok: true, reason: null });
  const invented = context({ project: project({ input_digest: DIGEST }), inputDigest: undefined });
  assert.equal(sourceGate(invented, viewer()).reason, 'digest-unknown');
  // a different digest at the same revision is another document
  assert.equal(eligible({ context: { inputDigest: 'e'.repeat(64) } }).reason, 'stale');
  // eligibility never writes a digest into the project
  const ctx = context();
  moveEligibility({ context: ctx, viewerSource: viewer(), owner: { entityId: 'ent-pump', reason: null }, scene: pumpScene });
  assert.ok(!('input_digest' in ctx.project));
});

test('a move starts only on complete output: pending stays viewable but is not a start point', () => {
  const pending = { origin: 'working', build_status: 'pending', artifact_status: 'pending' };
  for (const s of [pending, { ...pending, artifact_status: 'complete' }, { ...pending, build_status: 'complete' }]) {
    const r = eligible({ context: { src: s } });
    assert.equal(r.ok, false, JSON.stringify(s));
    assert.equal(r.reason, 'pending', JSON.stringify(s));
    assert.equal(r.message, MOVE_REASONS.pending);
    assert.equal(r.intent, null);
  }
  // the probe case recorded by the orchestrator: pending source with the old invented project digest shape
  const oldShape = context({ project: project({ input_digest: DIGEST }), inputDigest: undefined, src: pending });
  assert.equal(sourceGate(oldShape, viewer()).ok, false);
  // an older pending scene is reported stale first; failed builds stay incomplete
  assert.equal(eligible({ context: { src: { ...pending, source_revision: 6 } }, viewer: { source_revision: 6 } }).reason, 'stale');
  // honest coordinates on the pending current scene: it does show the current revision and digest
  const readout = coordinateReadout({ context: context({ src: pending }), viewerSource: viewer(), owner: { entityId: 'ent-pump', reason: null } });
  assert.equal(readout.lines[0], 'Current model r7 · matches this view');
  assert.deepEqual(readout.xyz, [1.5, 0, 0]);
  // the published complete source of the current revision is a start point (a complete build is offered as published)
  assert.equal(eligible({ context: { src: { origin: 'published' } } }).ok, true);
});

test('an accepted preview is drawn on its captured base scene even after the model moved on', () => {
  const record = { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, basePosition: [1.5, 0, 0], position: [2.45, 0, 0], phase: 'accepted' };
  // the context is already r8 (accepted): no new move on the old scene, but the record keeps its delta there
  const ctx = context({ project: project({ revision: 8 }), inputDigest: 'e'.repeat(64), move: record });
  assert.equal(moveEligibility({ context: ctx, viewerSource: viewer(), owner: { entityId: 'ent-pump', reason: null }, scene: pumpScene }).reason, 'stale');
  for (const phase of ['submitting', 'accepted', 'uncertain']) {
    const p = effectivePreview({ move: { ...record, phase }, viewerSource: viewer(), mode: 'live' });
    assert.equal(p.phase, phase);
    assert.equal(p.origin, 'mirror');
    assert.deepEqual(p.delta.map((v) => Number(v.toFixed(12))), [0.95, 0, 0]);
    assert.equal(p.sourceRevision, 7);
  }
  // the regenerated r8 scene (pending or complete) never receives it
  for (const status of ['pending', 'complete']) {
    assert.equal(effectivePreview({ move: record, viewerSource: viewer({ url: `/r8-${status}`, source_revision: 8, input_digest: 'e'.repeat(64) }), mode: 'live' }), null);
  }
});

test('every gate fails closed with its reason', () => {
  const cases = {
    'no-context': moveEligibility({ context: null, viewerSource: viewer(), owner: { entityId: 'ent-pump', reason: null }, scene: pumpScene }),
    offline: eligible({ context: { online: false } }),
    published: eligible({ context: { mode: 'published' } }),
    'no-scene': eligible({ context: { installedSource: null } }),
    'source-mismatch': eligible({ viewer: { url: '/artifacts/other/scene.json' } }),
    progressive: eligible({ viewer: { final: false } }),
    stale: eligible({ context: { project: project({ revision: 8 }) } }),
    'digest-unknown': eligible({ context: { inputDigest: undefined } }),
    pending: eligible({ context: { src: { origin: 'working', build_status: 'pending', artifact_status: 'pending' } } }),
    unplaced: eligible({ owner: { entityId: 'ent-new', reason: null } }),
    missing: eligible({ owner: { entityId: 'ent-deleted', reason: 'missing' } }),
    connection: eligible({ owner: { entityId: null, reason: 'connection' } }),
    multiple: eligible({ owner: { entityId: null, reason: 'multiple' } }),
    none: eligible({ owner: { entityId: null, reason: 'none' } }),
    'display-only': eligible({ owner: { entityId: 'ent-boundary', reason: null }, scene: { components: 1, physical: 0 } }),
    'not-in-scene': eligible({ scene: { components: 0, physical: 0 } }),
    busy: eligible({ context: { move: { entityId: 'ent-valve', sourceRevision: 7, inputDigest: DIGEST, basePosition: [2.7, -0.45, 0.7], position: [2.8, -0.45, 0.7], phase: 'preview' } } }),
  };
  for (const [reason, r] of Object.entries(cases)) {
    assert.equal(r.ok, false, reason);
    assert.equal(r.reason, reason, reason);
    assert.equal(r.message, MOVE_REASONS[reason], reason);
    assert.equal(r.intent, null, reason);
  }
  // stale also when the digest differs at the same revision, or the scene is older than the model
  assert.equal(eligible({ context: { inputDigest: 'e'.repeat(64) } }).reason, 'stale');
  assert.equal(eligible({ viewer: { input_digest: 'e'.repeat(64) }, context: { src: { input_digest: 'e'.repeat(64) } } }).reason, 'stale');
  assert.equal(eligible({ viewer: { source_revision: 6 }, context: { src: { source_revision: 6 } } }).reason, 'stale');
  for (const [build_status, artifact_status] of [['failed', 'failed'], ['cancelled', 'complete'], ['superseded', 'complete'], ['running', 'running'], ['pending', 'failed'], ['complete', null]]) {
    assert.equal(eligible({ context: { src: { origin: 'working', build_status, artifact_status } } }).reason, 'incomplete', build_status);
  }
  assert.equal(eligible({ context: { src: { integrity: 'rejected' } } }).reason, 'incomplete');
  assert.equal(eligible({ context: { src: { kind: 'preview' } } }).reason, 'progressive');
  // the view's own gesture is not "another move"; a submitted one is busy until the record arrives
  const local = { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, stage: 'drag' };
  const record = { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, basePosition: [1.5, 0, 0], position: [1.6, 0, 0], phase: 'preview' };
  assert.equal(eligible({ context: { move: record }, local }).ok, true);
  assert.equal(eligible({ local: { ...local, stage: 'committed' } }).reason, 'busy');
  assert.equal(sourceGate(context(), viewer()).ok, true);
});

test('an unsubmitted drag ends when its base, digest, source, connection or record no longer holds', () => {
  const local = { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, url: URL, recordSeen: false };
  const record = { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, basePosition: [1.5, 0, 0], position: [1.6, 0, 0], phase: 'preview' };
  const end = (ctx, l = local, v = viewer()) => dragEndReason(ctx, l, v);
  assert.equal(end(context()), null);
  assert.equal(end(context({ move: record })), null);
  assert.equal(end(context(), null), null);
  const cases = [
    ['no-context', null],
    ['offline', context({ online: false })],
    ['published', context({ mode: 'published' })], // also the equal-revision Published source of the same build
    ['digest-unknown', context({ inputDigest: undefined })],
    ['digest-unknown', context({ inputDigest: undefined, project: project({ input_digest: DIGEST }) })],
    ['stale', context({ project: project({ revision: 8 }) })],
    ['stale', context({ inputDigest: 'e'.repeat(64) })],
    ['source-changed', context({ installedSource: null })],
    ['source-changed', context({ src: { url: '/artifacts/b-r7-copy/scene.json' } })], // equal identity, another file
    ['superseded', context({ move: { ...record, entityId: 'ent-valve' } })],
    ['superseded', context({ move: { ...record, sourceRevision: 6 } })],
  ];
  for (const [reason, ctx] of cases) assert.equal(end(ctx), reason, `${reason} ${JSON.stringify(ctx && { ...ctx, project: undefined })}`);
  assert.equal(end(context(), local, viewer({ url: '/artifacts/b-r7-copy/scene.json' })), 'source-changed');
  assert.equal(end(context(), local, null), 'source-changed');
  assert.equal(end(context(), local, viewer({ final: false })), 'progressive');
  assert.equal(end(context(), { ...local, recordSeen: true }), 'app-cancelled');
});

test('world delta and snapping: only dragged axes move, from the base, by whole snap steps', () => {
  const base = [2.7, -1.875, 0]; // off the 0.05 m grid on Y
  assert.deepEqual(snapPosition(base, [2.83, -1.875, 0], 0.05, 'X'), [2.85, -1.875, 0]);
  assert.deepEqual(snapPosition(base, [2.71, -1.79, 0.3], 0.05, 'Y'), [2.7, -1.775, 0]); // only Y moves; Y keeps its own sub-grid offset
  assert.deepEqual(snapPosition(base, [2.7, -1.851, 0], 0.05, 'XY'), base); // 0.024 m: below half a step
  assert.deepEqual(snapPosition(base, [2.66, -1.84, 0], 0.05, 'XY'), [2.65, -1.825, 0]);
  assert.deepEqual(snapPosition(base, [2.71, -1.87, 0.02], 0.05, 'XYZ'), base); // jitter below half a step: no-op
  assert.ok(isNoOp(base, snapPosition(base, [2.71, -1.87, 0.02], 0.05, 'XYZ')));
  assert.deepEqual(snapPosition(base, [2.7123456789, -1.875, 0], null, 'X'), [2.712346, -1.875, 0]); // snap off: micrometre rounding
  assert.deepEqual(snapPosition([0.8, 0, 0.2], [0.95, 0, 0.2], 0.05, 'X'), [0.95, 0, 0.2]); // no float noise
  assert.deepEqual(snapPosition(base, [1e6, -1.875, 0], 0.05, 'X'), [2.7 + MAX_DELTA_M, -1.875, 0]);
  assert.equal(snapPosition(base, [Number.NaN, 0, 0], 0.05, 'X'), null);
  assert.equal(snapPosition([1, 2], [1, 2, 3]), null);
  assert.deepEqual(worldDelta([1.5, 0, 0], [1.65, 0.1, 0]).map((v) => Number(v.toFixed(12))), [0.15, 0.1, 0]);
  assert.equal(isNoOp([1, 2, 3], [1, 2, 3]), true);
  assert.equal(isNoOp([1, 2, 3], [1, 2, 3.001]), false);
});

test('a rotated item moves by the same world delta on every vertex: rotation is never applied again', () => {
  // valve body corners in its local frame, placed at base with rotation Z -90° (as the generator does)
  const local = [[0.1, 0.04, 0], [-0.1, 0.04, 0.05], [0.08, -0.04, 0.1]];
  const rotZ = (deg, [x, y, z]) => {
    const a = (deg * Math.PI) / 180;
    return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a), z];
  };
  const place = (origin, v) => applyDelta(rotZ(-90, v), origin);
  const base = [2.7, -0.45, 0.7];
  const target = snapPosition(base, [2.93, -0.2, 0.7], 0.05, 'XY');
  assert.deepEqual(target, [2.95, -0.2, 0.7]);
  const delta = worldDelta(base, target);
  for (const v of local) {
    const previewed = applyDelta(place(base, v), delta); // what the view draws from immutable world vertices
    const regenerated = place(target, v); // what the generator produces for the moved placement, same rotation
    previewed.forEach((c, i) => assert.ok(Math.abs(c - regenerated[i]) < 1e-12, `${c} vs ${regenerated[i]}`));
  }
});

test('preview reconciliation: drawn only on the exact base scene of a Live view, never twice', () => {
  const move = { paneIndex: 0, entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, basePosition: [1.5, 0, 0], position: [1.65, 0, 0], rotation: [0, 0, 0], phase: 'preview' };
  const live = { viewerSource: viewer(), mode: 'live' };
  const mirrored = effectivePreview({ move, ...live });
  assert.equal(mirrored.origin, 'mirror');
  assert.equal(mirrored.entityId, 'ent-pump');
  assert.deepEqual(mirrored.delta.map((v) => Number(v.toFixed(12))), [0.15, 0, 0]);
  for (const phase of ['submitting', 'accepted', 'uncertain']) assert.equal(effectivePreview({ move: { ...move, phase }, ...live }).phase, phase);
  // published panes keep their own source; regenerated geometry (another identity) never gets the delta
  assert.equal(effectivePreview({ move, viewerSource: viewer(), mode: 'published' }), null);
  assert.equal(effectivePreview({ move, viewerSource: viewer({ source_revision: 8, input_digest: 'e'.repeat(64), url: '/r8' }), mode: 'live' }), null);
  assert.equal(effectivePreview({ move, viewerSource: viewer({ final: false }), mode: 'live' }), null);
  assert.equal(effectivePreview({ move: { ...move, phase: 'bogus' }, ...live }), null);
  assert.equal(effectivePreview({ move: { ...move, position: [Number.NaN, 0, 0] }, ...live }), null);
  assert.equal(effectivePreview({ move: null, ...live }), null);
  // the view's own drag, and its commit bridged until the application's record arrives
  const local = { entityId: 'ent-pump', base: [1.5, 0, 0], position: [1.7, 0, 0], sourceRevision: 7, inputDigest: DIGEST, stage: 'drag' };
  assert.deepEqual(effectivePreview({ local, ...live }).position, [1.7, 0, 0]);
  assert.equal(effectivePreview({ local, ...live }).origin, 'local');
  assert.equal(effectivePreview({ local: { ...local, stage: 'committed' }, ...live }).phase, 'submitting');
  assert.equal(effectivePreview({ local: { ...local, stage: 'committed' }, move: { ...move, position: [1.7, 0, 0], phase: 'submitting' }, ...live }).origin, 'mirror');
  assert.equal(effectivePreview({ local, viewerSource: viewer({ source_revision: 8 }), mode: 'live' }), null);
  assert.equal(effectivePreview({ local: { ...local, stage: 'begin' }, ...live }), null);
  assert.ok(sameBase(move, local));
  // targets: every owner component, its label and ports, nothing else
  const targets = previewTargets({
    components: [{ id: 'ent-pump#body', semanticId: 'ent-pump' }, { id: 'ent-pump#motor', semanticId: 'ent-pump' }, { id: 'conn-1#pipe', semanticId: 'conn-1' }],
    labels: ['ent-pump', 'conn-1'],
    ports: [{ id: 'port-pump-out', entity_id: 'ent-pump', position_m: [1.5, 0, 0.5] }, { id: 'port-valve-in', entity_id: 'ent-valve', position_m: [2.7, -0.37, 0.7] }],
  }, 'ent-pump');
  assert.deepEqual(targets, { components: ['ent-pump#body', 'ent-pump#motor'], labels: ['ent-pump'], ports: ['port-pump-out'] });
});

test('coordinate grid: whole metres, contains the origin, restrained line count', () => {
  const g = gridExtent({ min: [-0.41, -3.32, -0.002], max: [4.05, 0.41, 1.32] });
  assert.deepEqual(g, { min: [-2, -5], max: [6, 2], minor: 0.25, major: 1, zTop: 2 });
  const far = gridExtent({ min: [100, 100, 0], max: [180, 140, 3] });
  assert.ok(far.min[0] <= 0 && far.min[1] <= 0, 'origin included');
  assert.ok((far.max[0] - far.min[0]) / far.minor <= 240);
  assert.deepEqual(gridExtent(null), { min: [-2, -2], max: [2, 2], minor: 0.25, major: 1, zTop: 1 });
  const lines = gridLines(-2, 2, 0.25, 1);
  assert.deepEqual(lines.major, [-2, -1, 1, 2]);
  assert.equal(lines.minor.length, 12);
  assert.ok(!lines.minor.includes(0) && !lines.major.includes(0), 'the axis line is drawn separately');
  assert.equal(formatMetres(-0.0001), '0.000');
  assert.equal(formatMetres(1.5), '1.500');
  assert.equal(formatMetres(Number.NaN), '—');
});

test('coordinate readout: authoritative origin with its source; unplaced is explicit; preview is labelled', () => {
  const ctx = { project: project(), inputDigest: DIGEST };
  const pump = { entityId: 'ent-pump', reason: null };
  const here = coordinateReadout({ context: ctx, viewerSource: viewer(), owner: pump });
  assert.equal(here.kind, 'origin');
  assert.deepEqual(here.xyz, [1.5, 0, 0]);
  assert.equal(here.title, 'P-101 · equipment origin');
  assert.equal(here.lines[0], 'Current model r7 · matches this view');
  // without the explicit digest the view cannot be matched: still the current-model origin, never "matches"
  for (const unknown of [{ project: project() }, { project: project({ input_digest: DIGEST }) }]) {
    const r = coordinateReadout({ context: unknown, viewerSource: viewer(), owner: pump });
    assert.equal(r.title, 'P-101 · origin in current model r7');
    assert.equal(r.lines[0], 'Current model r7 · digest unknown, not matched to this view (r7)');
    assert.deepEqual(r.xyz, [1.5, 0, 0]);
  }
  const older = coordinateReadout({ context: ctx, viewerSource: viewer({ source_revision: 5 }), owner: pump });
  assert.equal(older.title, 'P-101 · origin in current model r7'); // never presented as the older mesh's coordinates
  assert.equal(older.lines[0], 'Current model r7 · this view shows r5');
  assert.equal(coordinateReadout({ context: ctx, viewerSource: viewer({ input_digest: 'e'.repeat(64) }), owner: pump }).lines[0],
    'Current model r7 · this view shows r7 with another digest');
  const unplaced = coordinateReadout({ context: ctx, viewerSource: viewer(), owner: { entityId: 'ent-new', reason: null } });
  assert.equal(unplaced.kind, 'unplaced');
  assert.equal(unplaced.xyz, null); // never zeroes
  assert.match(unplaced.title, /HV-103 · Unplaced/);
  assert.equal(coordinateReadout({ context: ctx, viewerSource: viewer(), owner: { entityId: 'ent-x', reason: 'missing' } }).kind, 'missing');
  assert.equal(coordinateReadout({ context: ctx, owner: { entityId: null, reason: 'connection' } }).kind, 'connection');
  assert.equal(coordinateReadout({ context: ctx, owner: { entityId: null, reason: 'multiple' } }).kind, 'multiple');
  assert.equal(coordinateReadout({ context: ctx, owner: null }).kind, 'none');
  assert.equal(coordinateReadout({ context: null, owner: pump }).kind, 'no-context');
  const preview = effectivePreview({
    move: { entityId: 'ent-pump', sourceRevision: 7, inputDigest: DIGEST, basePosition: [1.5, 0, 0], position: [1.65, 0, 0], phase: 'accepted' },
    viewerSource: viewer(), mode: 'live',
  });
  const p = coordinateReadout({ context: ctx, viewerSource: viewer(), owner: pump, preview });
  assert.equal(p.kind, 'preview');
  assert.equal(p.title, 'P-101 · Preview origin');
  assert.deepEqual(p.xyz, [1.65, 0, 0]);
  assert.match(p.lines.join('\n'), /Δ \+0\.150, 0\.000, 0\.000 m from 1\.500, 0\.000, 0\.000/);
  assert.match(p.lines.join('\n'), /Accepted · waiting for regenerated geometry · base r7/);
  const tool = coordinateReadout({ context: ctx, viewerSource: viewer(), owner: pump, move: { enabled: true, snap: 0.05, eligible: { ok: false, reason: 'published', message: MOVE_REASONS.published } } });
  assert.match(tool.lines.join('\n'), /Move unavailable: Published views are read-only/);
});
