// Pipe-section coordinator (pipe-move-coordinator.js): capability and start gates, cross-blocking
// with the equipment coordinator, tentative previews, no-op and stale gates, exactly one
// move_route_segment submission, rejection, unconfirmed outcomes settled only by evidence (no
// resend), and clearing an accepted preview once a regenerated scene is installed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildRouteSegmentMove } from '../../../web/js/commands-model.js';
import { createMoveCoordinator } from '../../../web/js/move-coordinator.js';
import {
  createPipeMoveCoordinator, pipeAcceptedClearReason, pipeCapable, pipeEvidence, sectionGate,
} from '../../../web/js/pipe-move-coordinator.js';
import { routeKey } from '../../../web/js/pipe-sections.js';

const D0 = 'a'.repeat(64);
const D1 = 'b'.repeat(64);
const POINTS = [[0, 0, 0.5], [1, 0, 0.5], [1, 0, 1.5], [3, 0, 1.5], [3, 0, 0.5]];

function state({ revision = 5, digest = D0, points = POINTS, status = 'current', capable = true, recent = [], last = null, started = 's1', assumption = 'asm-r', build = null, extraAssumptions = {} } = {}) {
  return {
    capabilities: capable ? { pipe_segment_move: true } : undefined,
    project: {
      id: 'proj', revision,
      model: {
        entities: { 'ent-p': { tag: 'P-101' } },
        ports: {},
        connections: { 'conn-1': { tag: 'L-1', nominal_diameter_mm: 50, outside_diameter_m: 0.0603 } },
        assumptions: { [assumption]: { description: 'base' }, ...extraAssumptions },
      },
      physical: {
        placements: { 'ent-p': { status: 'placed', position_m: [0, 0, 0], rotation_deg: [0, 0, 0], manual: false } },
        routes: { 'conn-1': { points_m: points, mode: 'guided', status, assumption_id: assumption } },
      },
    },
    document: { input_digest: digest },
    service: { started_at: started },
    history: { recent },
    last_command: last,
    build,
  };
}

const installed = (revision = 5, digest = D0, url = '/a/scene.json') => ({
  url, src: { kind: 'output', build_status: 'complete', artifact_status: 'complete', source_revision: revision, input_digest: digest },
});
const pane3d = (over = {}) => ({ index: 0, type: '3d', mode: 'live', active: true, installed: installed(), ...over });

function harness({ st = state(), submit = null, refetch = null, equipment = null } = {}) {
  const h = { state: st, online: true, panes: [pane3d()], submitted: [], published: [], cancelled: [], notes: [] };
  h.c = createPipeMoveCoordinator({
    getState: () => h.state,
    isOnline: () => h.online,
    pane: (i) => h.panes[i] || null,
    panes: () => h.panes,
    refetch: refetch || (async () => ({ ok: true })),
    submit: submit || (async (cmd) => { h.submitted.push(cmd); return { ok: false, error: { network: true, error: 'lost' } }; }),
    buildCommand: buildRouteSegmentMove,
    publish: (r) => h.published.push(r),
    cancelViewer: (i, reason) => h.cancelled.push(reason),
    notify: (kind, title) => h.notes.push([kind, title]),
    otherActive: () => equipment,
  });
  return h;
}

const FRAMES = { start: [1, 0, 0], end: [0, 0, 1] }; // outward port directions: leaves along +X, enters downward
const intent = (over = {}) => ({ connectionId: 'conn-1', segmentIndex: 1, sourceRevision: 5, inputDigest: D0, routeKey: routeKey(state().project, 'conn-1'), frames: FRAMES, ...over });

test('capability: an old service without pipe_segment_move refuses to start', () => {
  assert.equal(pipeCapable(state()), true);
  assert.equal(pipeCapable(state({ capable: false })), false);
  const h = harness({ st: state({ capable: false }) });
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'unsupported');
  assert.equal(h.c.record(), null);
});

test('start gates: published, progressive, stale, route changed, route not current, missing section', () => {
  let h = harness();
  h.panes = [pane3d({ mode: 'published' })];
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'published');
  h = harness();
  h.panes = [pane3d({ installed: { ...installed(), src: { ...installed().src, kind: 'preview' } } })];
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'progressive');
  h = harness();
  assert.equal(h.c.begin(0, intent({ sourceRevision: 4 })), false);
  assert.equal(h.c.refusal().reason, 'stale');
  assert.equal(h.c.begin(0, intent({ routeKey: 'other' })), false);
  assert.equal(h.c.refusal().reason, 'route-changed');
  h = harness({ st: state({ status: 'needs_reroute' }) });
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'route-not-current');
  h = harness();
  assert.equal(h.c.begin(0, intent({ segmentIndex: 9 })), false);
  assert.equal(h.c.refusal().reason, 'no-section');
  assert.equal(sectionGate(state().project, 'conn-1', 1).section.axis, 2);
});

test('one edit at a time: equipment and pipe coordinators block each other', () => {
  const h = harness({ equipment: { phase: 'preview' } });
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'equipment-busy');
  const p = harness();
  assert.equal(p.c.begin(0, intent()), true);
  const eq = createMoveCoordinator({
    getState: () => p.state, isOnline: () => true, pane: () => pane3d(), panes: () => [pane3d()], refetch: async () => ({ ok: true }),
    submit: async () => ({ ok: true }), buildCommand: () => ({}), publish: () => {}, cancelViewer: () => {}, notify: () => {},
    otherActive: () => p.c.record(),
  });
  assert.equal(eq.begin(0, { entityId: 'ent-p', position: [0, 0, 0], sourceRevision: 5, inputDigest: D0 }), false);
  assert.equal(eq.refusal().reason, 'pipe-busy');
  p.c.cancel('escape');
  assert.equal(eq.begin(0, { entityId: 'ent-p', position: [0, 0, 0], sourceRevision: 5, inputDigest: D0 }), true);
});

test('preview keeps the base points and a zero offset sends nothing (no assumption, no command)', async () => {
  const h = harness();
  assert.equal(h.c.begin(0, intent()), true);
  const r = h.c.record();
  assert.deepEqual(r.basePoints, POINTS);
  assert.equal(r.axis, 2);
  assert.equal(r.phase, 'preview');
  assert.ok(Object.isFrozen(r));
  const out = await h.c.commit(0);
  assert.equal(out.reason, 'no-op');
  assert.equal(h.submitted.length, 0);
  assert.equal(h.c.record(), null);
  assert.equal(h.c.outcome().kind, 'no-op');
});

test('a parallel or two-axis offset is refused before sending and the preview stays for correction', async () => {
  const h = harness();
  h.c.begin(0, intent());
  h.c.preview([0, 0, 0.3], 0); // along the leg's own axis (Z)
  const out = await h.c.commit(0);
  assert.equal(out.reason, 'invalid-offset');
  assert.equal(h.submitted.length, 0);
  assert.equal(h.c.record().phase, 'preview');
});

test('a stale model at Apply cancels the preview and sends nothing', async () => {
  const h = harness();
  h.c.begin(0, intent());
  h.state = state({ revision: 6, digest: D1 });
  h.c.stateChanged(h.state);
  assert.equal(h.c.record(), null);
  assert.equal(h.c.outcome().reason, 'stale');
  assert.deepEqual(h.cancelled, ['stale']);
});

test('apply sends exactly one move_route_segment with the source revision and a fresh assumption; accepted from the answer', async () => {
  let sent = null;
  const h = harness({
    submit: async (cmd) => {
      sent = cmd;
      return { ok: true, state: state({ revision: 6, digest: D1, last: { command_id: cmd.id, status: 'accepted', from_revision: 5, to_revision: 6 } }) };
    },
  });
  h.c.begin(0, intent());
  assert.equal(h.c.record().offset.join(), '0,0,0');
  h.c.preview([0.25, 0, 0], 0); // perpendicular X on the Z leg: both neighbouring X legs stretch
  const rec = h.c.record();
  assert.equal(rec.problem, null);
  assert.deepEqual(rec.points, [[0, 0, 0.5], [1.25, 0, 0.5], [1.25, 0, 1.5], [3, 0, 1.5], [3, 0, 0.5]]);
  assert.deepEqual(rec.basePoints, POINTS, 'base kept for the overlay');
  const out = await h.c.commit(0);
  assert.equal(out.ok, true);
  assert.equal(sent.type, 'move_route_segment');
  assert.equal(sent.expected_revision, 5);
  assert.deepEqual(Object.keys(sent.payload).sort(), ['assumption_id', 'connection_id', 'offset_m', 'segment_index']);
  assert.deepEqual(sent.payload.offset_m, [0.25, 0, 0]);
  assert.equal(sent.payload.segment_index, 1);
  assert.ok(sent.assumptions[sent.payload.assumption_id]);
  assert.equal(h.c.record().phase, 'accepted');
});

test('pipeEvidence: command log, the route\'s fresh assumption, the assumption record, or no trace', () => {
  const sub = { commandId: 'cmd-1', assumptionId: 'asm-new', connectionId: 'conn-1', baseRevision: 5, baseDigest: D0 };
  assert.equal(pipeEvidence(state({ recent: [{ command_id: 'cmd-1', status: 'accepted', to_revision: 6 }] }), sub).via, 'command-log');
  assert.equal(pipeEvidence(state({ recent: [{ command_id: 'cmd-1', status: 'no_op' }] }), sub).outcome, 'no-op');
  assert.equal(pipeEvidence(state({ revision: 6, assumption: 'asm-new' }), sub).via, 'route');
  assert.equal(pipeEvidence(state({ revision: 6, extraAssumptions: { 'asm-new': {} } }), sub).via, 'assumption');
  const none = pipeEvidence(state(), sub);
  assert.equal(none.outcome, 'no-trace');
  assert.equal(none.atBase, true);
});

function withValidPreview(h) {
  assert.equal(h.c.begin(0, intent()), true);
  h.c.preview([0.25, 0, 0], 0);
  assert.equal(h.c.record().problem, null);
}

test('lost answer: unconfirmed, never resent, settled by the fresh route assumption', async () => {
  const h = harness();
  withValidPreview(h);
  const out = await h.c.commit(0);
  assert.equal(out.reason, 'uncertain');
  assert.equal(h.submitted.length, 1);
  assert.equal(h.c.record().phase, 'uncertain');
  assert.equal(h.c.begin(0, intent()), false);
  assert.equal(h.c.refusal().reason, 'unconfirmed');
  assert.equal(h.c.cancel('escape'), false); // a submitted command cannot be cancelled
  h.state = state(); // no trace yet: still unconfirmed
  h.c.stateChanged(h.state);
  assert.equal(h.c.record().phase, 'uncertain');
  const asm = h.submitted[0].payload.assumption_id;
  h.state = state({ revision: 6, digest: D1, assumption: asm });
  h.c.stateChanged(h.state);
  assert.equal(h.c.record().phase, 'accepted');
  assert.equal(h.c.outcome().via, 'route');
  assert.equal(h.submitted.length, 1);
});

test('rejection (collision) removes the preview and reports the service reason; nothing resent', async () => {
  const h = harness({
    submit: async (cmd) => { h.submitted.push(cmd); return { ok: false, error: { status: 400, error: 'route enters C-101', diagnostics: [{ code: 'route_equipment_clearance', message: 'L-1 crosses C-101' }] } }; },
  });
  withValidPreview(h);
  const out = await h.c.commit(0);
  assert.equal(out.reason, 'rejected');
  assert.equal(h.submitted.length, 1);
  assert.equal(h.c.record(), null);
  assert.match(h.c.outcome().text, /L-1 crosses C-101/, 'the service diagnostic is shown');
});

test('hide an unconfirmed preview: watched, reported if applied later, never resent', async () => {
  const h = harness();
  withValidPreview(h);
  await h.c.commit(0);
  assert.equal(h.c.dismiss(0), true);
  assert.equal(h.c.unresolved().length, 1);
  const asm = h.submitted[0].payload.assumption_id;
  h.c.stateChanged(state({ revision: 6, digest: D1, assumption: asm }));
  assert.equal(h.c.unresolved().length, 0);
  assert.equal(h.c.outcome().kind, 'confirmed-late');
});

test('accepted preview stays on the base scene, then clears when a regenerated scene is installed', () => {
  const record = { phase: 'accepted', connectionId: 'conn-1', sourceRevision: 5, inputDigest: D0 };
  const sub = { assumptionId: 'asm-new' };
  const st = state({ revision: 6, digest: D1, assumption: 'asm-new' });
  assert.equal(pipeAcceptedClearReason({ record, state: st, submitted: sub, toRevision: 6, livePanes: [pane3d()] }), null);
  assert.equal(pipeAcceptedClearReason({ record, state: st, submitted: sub, toRevision: 6, livePanes: [pane3d({ installed: installed(6, D1, '/b/scene.json') })] }), 'installed');
  assert.equal(pipeAcceptedClearReason({ record, state: state({ revision: 7 }), submitted: sub, toRevision: 6, livePanes: [pane3d()] }), 'superseded');
});

test('a refused preview (short end section) cannot be applied; nothing is sent', async () => {
  const short = [[0, 0, 0.5], [0.15, 0, 0.5], [0.15, 0, 1.5], [3, 0, 1.5], [3, 0, 0.5]]; // 0.15 m end leg < 0.16 m stub
  const g = harness({ st: state({ points: short }) });
  assert.equal(g.c.begin(0, intent({ segmentIndex: 0, routeKey: routeKey(state({ points: short }).project, 'conn-1') })), true);
  g.c.preview([0, 0.2, 0], 0);
  assert.equal(g.c.record().problem.code, 'route_edit_stub_too_short');
  const out = await g.c.commit(0);
  assert.equal(out.reason, 'preview-refused');
  assert.equal(g.submitted.length, 0);
});

test('an intent without port frames is refused', () => {
  const h = harness();
  assert.equal(h.c.begin(0, intent({ frames: null })), false);
  assert.equal(h.c.refusal().reason, 'no-frames');
});

test('selection change and Escape end an unsent preview only', () => {
  const h = harness();
  h.c.begin(0, intent());
  h.c.sectionChanged({ connectionId: 'conn-1', segmentIndex: 1 });
  assert.ok(h.c.record());
  h.c.sectionChanged({ connectionId: 'conn-1', segmentIndex: 2 });
  assert.equal(h.c.record(), null);
  assert.equal(h.c.outcome().reason, 'selection-changed');
});
