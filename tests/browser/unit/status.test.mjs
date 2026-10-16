// Pure status rules: what may be called Current/Synchronized, and which state payloads are fresh.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  appliedMarker, artifactProgress, buildElapsed, isVerifiedPublished, shouldApplyState, sourceBadge, syncBadge, viewSource,
} from '../../../web/js/status.js';

const D0 = 'a'.repeat(64);
const D1 = 'b'.repeat(64);
const PUBLISHED = { build_id: 'b20261014T072622-r0-66f3af3f', source_revision: 0, input_digest: D0, artifacts: { display: '/artifacts/b20261014T072622-r0-66f3af3f/scene.json' } };
const pubSrc = (over = {}) => ({
  origin: 'published', kind: 'output', build_id: PUBLISHED.build_id, build_status: 'complete', artifact_status: 'complete',
  source_revision: 0, input_digest: D0, ...over,
});

test('only the verified publication of the current revision is Current', () => {
  const b = sourceBadge(pubSrc(), 0, PUBLISHED);
  assert.equal(b.tone, 'current');
  assert.match(b.label, /^Current · r0/);
});

test('a published-looking source without a verified publication is not Current', () => {
  assert.equal(sourceBadge(pubSrc(), 0, null).tone, 'unverified');
  assert.equal(sourceBadge(pubSrc({ build_id: 'b20261014T090000-r0-00000000' }), 0, PUBLISHED).tone, 'unverified');
  assert.equal(sourceBadge(pubSrc({ input_digest: D1 }), 0, PUBLISHED).tone, 'unverified');
  assert.equal(isVerifiedPublished(pubSrc({ artifact_status: null }), PUBLISHED), false);
});

test('lifecycle completion of a non-published source is never Current (review finding 3)', () => {
  const working = { origin: 'working', kind: 'output', build_id: 'bX', build_status: 'complete', artifact_status: 'complete', source_revision: 0, input_digest: D0 };
  const b = sourceBadge(working, 0, PUBLISHED);
  assert.notEqual(b.tone, 'current');
  assert.equal(b.tone, 'unverified');
});

test('rejected integrity is a failure, whatever the lifecycle status says', () => {
  assert.equal(sourceBadge(pubSrc({ artifact_status: 'rejected' }), 0, PUBLISHED).tone, 'failed');
  assert.equal(sourceBadge(pubSrc({ integrity: 'rejected' }), 0, PUBLISHED).tone, 'failed');
});

test('an older verified publication is stale once the project advances', () => {
  const b = sourceBadge(pubSrc(), 3, PUBLISHED);
  assert.equal(b.tone, 'stale');
  assert.equal(b.behind, true);
  assert.match(b.detail, /revision 3/);
});

test('working, pending, failed, cancelled and superseded sources keep their own tone', () => {
  const w = (build_status, kind = 'preview', artifact_status = 'running') => ({ origin: 'working', kind, build_id: 'bW', build_status, artifact_status, source_revision: 1, input_digest: D1 });
  assert.equal(sourceBadge(w('running'), 1, PUBLISHED).tone, 'working');
  assert.match(sourceBadge(w('running', 'output'), 1, PUBLISHED).label, /awaiting verification/);
  assert.equal(sourceBadge(w('pending', 'output', 'pending'), 1, PUBLISHED).tone, 'pending');
  assert.equal(sourceBadge(w('failed', 'preview', 'failed'), 1, PUBLISHED).tone, 'failed');
  assert.equal(sourceBadge(w('cancelled'), 1, PUBLISHED).tone, 'cancelled');
  assert.equal(sourceBadge(w('superseded'), 1, PUBLISHED).tone, 'stale');
  assert.match(sourceBadge(w('running'), 2, PUBLISHED).label, /project r2/);
});

test('sync badge: invalid and blocked are errors; synchronized needs published_current', () => {
  const state = (sync) => ({ project: { revision: 4 }, sync });
  assert.equal(syncBadge(state({ state: 'invalid', message: 'x' })).tone, 'failed');
  assert.match(syncBadge(state({ state: 'invalid' })).label, /invalid/i);
  assert.equal(syncBadge(state({ state: 'blocked' })).tone, 'failed');
  assert.match(syncBadge(state({ state: 'blocked' })).label, /notes unreadable/i);
  assert.equal(syncBadge(state({ state: 'synchronized', published_current: true })).synchronized, true);
  assert.equal(syncBadge(state({ state: 'synchronized', published_current: false })).synchronized, false);
  const unknown = syncBadge(state({ state: 'something-new' }));
  assert.notEqual(unknown.tone, 'current');
  assert.match(unknown.label, /Unknown state/);
});

test('viewSource published mode yields a source that verifies against the same publication', () => {
  const state = { published: PUBLISHED, artifacts: {}, artifact_sources: {} };
  const src = viewSource(state, 'display', 'published');
  assert.equal(src.url, PUBLISHED.artifacts.display);
  assert.equal(isVerifiedPublished(src, PUBLISHED), true);
  assert.equal(viewSource(state, 'display', 'live'), null);
});

function payload({ started = 's1', revision = 0, build = null, seq = 0, sel = 0 } = {}) {
  return { project: { revision }, service: { started_at: started }, build: build ? { id: build, last_sequence: seq } : null, selection: { sequence: sel } };
}

test('freshness gate discards older revisions and older event sequences', () => {
  const applied = appliedMarker(payload({ revision: 2, build: 'bA', seq: 10, sel: 1 }), 5);
  assert.equal(shouldApplyState(applied, payload({ revision: 1, build: 'bA', seq: 99 }), 9), false);
  assert.equal(shouldApplyState(applied, payload({ revision: 3, build: 'bB', seq: 1 }), 4), true);
  assert.equal(shouldApplyState(applied, payload({ revision: 2, build: 'bA', seq: 9 }), 9), false);
  assert.equal(shouldApplyState(applied, payload({ revision: 2, build: 'bA', seq: 11 }), 3), true);
  // same revision/build/sequence: a newer request or a newer selection wins
  assert.equal(shouldApplyState(applied, payload({ revision: 2, build: 'bA', seq: 10, sel: 1 }), 4), false);
  assert.equal(shouldApplyState(applied, payload({ revision: 2, build: 'bA', seq: 10, sel: 2 }), 4), true);
  assert.equal(shouldApplyState(applied, payload({ revision: 2, build: 'bA', seq: 10, sel: 1 }), 6), true);
});

test('after a service restart the newest request wins even at a lower sequence', () => {
  const applied = appliedMarker(payload({ started: 's1', revision: 5, build: 'bA', seq: 40, sel: 9 }), 7);
  assert.equal(shouldApplyState(applied, payload({ started: 's2', revision: 5, build: null, sel: 0 }), 8), true);
  assert.equal(shouldApplyState(applied, payload({ started: 's2', revision: 5 }), 6), false);
  assert.equal(shouldApplyState(null, payload(), 1), true);
  assert.equal(shouldApplyState(applied, { nonsense: true }, 99), false);
});

test('artifact progress and elapsed time come from real event data only', () => {
  const build = {
    status: 'complete', artifact_status: { display: 'complete', cad: 'complete', pid: 'complete', legend: 'complete', description: 'complete' },
    started_at: '2026-10-14T07:26:22.000+00:00', finished_at: '2026-10-14T07:26:26.100+00:00',
    events: [
      { sequence: 1, kind: 'preview', artifact: 'display', completed: 1, total: 13, message: 'TK-101 ready', elapsed_s: 0.8 },
      { sequence: 2, kind: 'preview', artifact: 'display', completed: 2, total: 13, message: 'HV-101 ready', elapsed_s: 0.9 },
      { sequence: 3, kind: 'activity', artifact: 'cad', message: 'Exporting STEP', elapsed_s: 1.7 },
    ],
  };
  const p = artifactProgress(build);
  assert.deepEqual([p.display.completed, p.display.total, p.display.previews], [2, 13, 2]);
  assert.equal(p.cad.completed, null);
  assert.equal(p.cad.message, 'Exporting STEP');
  assert.equal(Math.round(buildElapsed(build) * 10) / 10, 4.1);
  assert.equal(buildElapsed({ status: 'cancelled', events: [{ elapsed_s: 1.25 }] }), 1.25);
});
