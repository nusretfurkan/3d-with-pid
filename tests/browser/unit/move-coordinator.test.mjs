// Application move coordinator (move-coordinator.js): start gate, one global record, cancel and
// stale handling, no-op before the envelope, exactly one submission, rejection, uncertain
// outcomes reconciled from state without resending, and the display hold after acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPlacement } from '../../../web/js/commands-model.js';
import {
  MAX_UNRESOLVED, RECORD_FIELDS, acceptedClearReason, commandEvidence, createMoveCoordinator, isFinalOffer, makeRecord, moveHold,
  parseMetres, placementGate, readTyped, startGate,
} from '../../../web/js/move-coordinator.js';

const D0 = 'a'.repeat(64);
const D1 = 'b'.repeat(64);

function state({ revision = 0, digest = D0, position = [3.2, 0.9, 0], rotation = [0, 0, -90], recent = [], last = null, build = null, started = 's1', assumption = 'asm-0' } = {}) {
  return {
    project: {
      id: 'proj', revision,
      model: {
        entities: { 'ent-valve': { tag: 'HV-102' }, 'ent-tank': { tag: 'T-101' } },
        ports: { 'port-valve-in': { id: 'port-valve-in', entity_id: 'ent-valve' }, 'port-valve-out': { id: 'port-valve-out', entity_id: 'ent-valve' } },
        connections: { 'conn-1': { tag: 'L-1', from_port_id: 'port-valve-out', to_port_id: 'port-x' } },
        assumptions: { [assumption]: { description: 'base' } },
      },
      physical: {
        placements: {
          'ent-valve': { status: 'placed', position_m: position, rotation_deg: rotation, manual: true, assumption_id: assumption },
          'ent-tank': { status: 'unresolved', position_m: null, rotation_deg: null, manual: false, assumption_id: 'asm-t', reason: 'not placed' },
        },
        routes: { 'conn-1': { status: 'current', points_m: [[0, 0, 0], [1, 0, 0]] } },
      },
    },
    document: { input_digest: digest },
    history: { recent, undo: 0, redo: 0 },
    last_command: last,
    build,
    service: { started_at: started },
  };
}

const installed = (over = {}, url = '/artifacts/builds/b0/display/scene.json') => ({
  url, src: { kind: 'output', origin: 'published', build_id: 'b0', build_status: 'complete', artifact_status: 'complete', source_revision: 0, input_digest: D0, ...over },
});
/** A pane description; unless given, the viewer has loaded exactly the installed file. */
const pane = (index, over = {}) => {
  const p = { index, type: '3d', mode: 'live', active: true, installed: installed(), ...over };
  if (!('viewerSource' in over)) {
    p.viewerSource = p.installed ? { url: p.installed.url, source_revision: p.installed.src.source_revision, input_digest: p.installed.src.input_digest, final: true } : null;
  }
  return p;
};
const intent = (over = {}) => ({ entityId: 'ent-valve', position: [3.2, 0.9, 0], sourceRevision: 0, inputDigest: D0, ...over });

/** The service accepting one envelope: its placement, revision + 1, a command log entry. */
function accepted(cmd, from = state()) {
  const s = JSON.parse(JSON.stringify(from));
  s.project.revision += 1;
  s.document.input_digest = D1;
  s.project.physical.placements[cmd.payload.entity_id] = cmd.payload.placement;
  Object.assign(s.project.model.assumptions, cmd.assumptions);
  s.project.physical.routes['conn-1'].status = 'needs_reroute';
  const entry = { command_id: cmd.id, type: cmd.type, status: 'accepted', from_revision: from.project.revision, to_revision: s.project.revision };
  s.history.recent.push(entry);
  s.last_command = entry;
  return s;
}

function harness() {
  const env = {
    state: state(), online: true, panes: [pane(0), pane(1)], published: [], cancelled: [], notes: [], submitted: [], built: 0,
    seq: 10, applied: 10, refetches: 0, submitImpl: null, refetchImpl: null, clock: 1000,
  };
  env.c = createMoveCoordinator({
    getState: () => env.state,
    isOnline: () => env.online,
    pane: (i) => env.panes[i] || null,
    panes: () => env.panes,
    refetch: async () => {
      env.refetches += 1;
      if (env.refetchImpl) return env.refetchImpl();
      env.seq += 1;
      if (env.online) env.applied = env.seq;
      return { ok: env.online };
    },
    submit: async (cmd, label) => {
      env.submitted.push({ cmd, label });
      if (env.submitImpl) return env.submitImpl(cmd);
      env.state = accepted(cmd, env.state);
      return { ok: true, state: env.state };
    },
    buildCommand: (project, id, position, rotation) => {
      env.built += 1;
      return buildPlacement(project, id, position, rotation);
    },
    publish: (r) => env.published.push(r),
    cancelViewer: (i, reason) => env.cancelled.push([i, reason]),
    notify: (kind, title, lines) => env.notes.push({ kind, title, lines }),
    now: () => env.clock,
  });
  return env;
}

const apiError = (status, error, network = false) => Object.assign(new Error(error), { status, error, network, diagnostics: [] });

test('start gate: online, Live, shown 3D pane, complete final scene of exactly the current revision and digest', () => {
  const s = state();
  assert.deepEqual(startGate({ state: s, online: true, pane: pane(0) }), { ok: true, reason: null });
  const reason = (args) => startGate({ state: s, online: true, pane: pane(0), ...args }).reason;
  assert.equal(reason({ online: false }), 'offline');
  assert.equal(reason({ pane: pane(0, { mode: 'published' }) }), 'published');
  assert.equal(reason({ pane: pane(0, { active: false }) }), 'hidden');
  assert.equal(reason({ pane: pane(0, { type: 'pid' }) }), 'not-3d');
  assert.equal(reason({ pane: pane(0, { installed: null }) }), 'no-scene');
  assert.equal(reason({ pane: pane(0, { installed: installed({ kind: 'preview', build_status: 'running' }) }) }), 'progressive');
  // the scene the viewer has actually loaded must be the installed file itself, final
  const loaded = { url: installed().url, source_revision: 0, input_digest: D0, final: true };
  assert.equal(reason({ pane: pane(0, { viewerSource: { ...loaded, final: false } }) }), 'progressive');
  assert.equal(reason({ pane: pane(0, { viewerSource: { ...loaded, url: '/artifacts/builds/b9/display/scene.json' } }) }), 'progressive');
  assert.equal(reason({ pane: pane(0, { viewerSource: { ...loaded, source_revision: 1 } }) }), 'progressive');
  assert.equal(reason({ pane: pane(0, { viewerSource: { ...loaded, input_digest: D1 } }) }), 'progressive');
  assert.equal(reason({ pane: pane(0, { viewerSource: null }) }), 'progressive', 'no loaded scene');
  // the frozen gate: a pending (or failed) build is viewable but does not start a move
  assert.equal(reason({ pane: pane(0, { installed: installed({ build_status: 'pending', artifact_status: 'complete' }) }) }), 'incomplete');
  assert.equal(reason({ pane: pane(0, { installed: installed({ integrity: 'rejected' }) }) }), 'incomplete');
  assert.equal(reason({ pane: pane(0, { installed: installed({ source_revision: 1 }) }) }), 'stale');
  assert.equal(reason({ pane: pane(0, { installed: installed({ input_digest: D1 }) }) }), 'stale');
  assert.equal(startGate({ state: { ...s, document: {} }, online: true, pane: pane(0) }).reason, 'digest-unknown');
});

test('begin: one frozen record with the contract fields; base and rotation from the authoritative placement; one move at a time', () => {
  const env = harness();
  assert.equal(env.c.begin(0, intent()), true);
  const r = env.c.record();
  assert.deepEqual(Object.keys(r), RECORD_FIELDS);
  assert.deepEqual(r, { paneIndex: 0, entityId: 'ent-valve', sourceRevision: 0, inputDigest: D0, basePosition: [3.2, 0.9, 0], position: [3.2, 0.9, 0], rotation: [0, 0, -90], phase: 'preview' });
  assert.ok(Object.isFrozen(r) && Object.isFrozen(r.basePosition) && Object.isFrozen(r.rotation));
  assert.equal(env.published.at(-1), r, 'mirrored to every pane');
  assert.equal(env.c.begin(1, intent()), false, 'only one global move');
  assert.equal(env.c.refusal().reason, 'busy');
  assert.equal(env.c.record().paneIndex, 0);
});

test('begin refuses a stale intent, a moved base, unplaced or unknown items and pending scenes', () => {
  const refused = (fn) => { const env = harness(); fn(env); return env.c.record() === null ? env.c.refusal().reason : 'started'; };
  assert.equal(refused((e) => e.c.begin(0, intent({ sourceRevision: 1 }))), 'stale');
  assert.equal(refused((e) => e.c.begin(0, intent({ inputDigest: D1 }))), 'stale');
  assert.equal(refused((e) => e.c.begin(0, intent({ position: [3.25, 0.9, 0] }))), 'base-changed');
  assert.equal(refused((e) => e.c.begin(0, intent({ entityId: 'ent-tank', position: [0, 0, 0] }))), 'unplaced');
  assert.equal(refused((e) => e.c.begin(0, intent({ entityId: 'ent-gone' }))), 'missing');
  assert.equal(refused((e) => { e.panes[0] = pane(0, { installed: installed({ build_status: 'pending' }) }); e.c.begin(0, intent()); }), 'incomplete');
  assert.equal(refused((e) => { e.online = false; e.c.begin(0, intent()); }), 'offline');
  assert.equal(refused((e) => e.c.begin(0, { entityId: 'ent-valve', position: [NaN, 0, 0], sourceRevision: 0, inputDigest: D0 })), 'invalid-intent');
});

test('preview updates only the origin pane\'s record; cancel restores and sends nothing', () => {
  const env = harness();
  env.c.begin(0, intent());
  assert.equal(env.c.preview([3.5, 0.9, 0], 1), false, 'another pane cannot drive it');
  assert.equal(env.c.preview([3.5, 0.9, 0], 0), true);
  assert.equal(env.c.preview([Infinity, 0, 0], 0), false);
  assert.deepEqual(env.c.record().position, [3.5, 0.9, 0]);
  assert.equal(env.c.cancel('escape', 1), false, 'not the origin pane');
  assert.equal(env.c.cancel('escape'), true);
  assert.equal(env.c.record(), null);
  assert.equal(env.published.at(-1), null);
  assert.deepEqual(env.cancelled, [[0, 'escape']]);
  assert.equal(env.c.outcome().kind, 'cancelled');
  assert.equal(env.built, 0);
  assert.equal(env.submitted.length, 0);
});

test('a no-op commit sends nothing and never builds the envelope (no fresh assumption)', async () => {
  const env = harness();
  env.c.begin(0, intent());
  env.c.preview([3.25, 0.9, 0]);
  const result = await env.c.commit([3.2, 0.9, 0]);
  assert.equal(result.reason, 'no-op');
  assert.equal(env.built, 0, 'buildPlacement was not called');
  assert.equal(env.submitted.length, 0);
  assert.equal(env.refetches, 0);
  assert.equal(env.c.record(), null);
  assert.equal(env.c.outcome().kind, 'no-op');
});

test('many previews and one commit: exactly one existing move_equipment envelope, rotation and base revision kept', async () => {
  const env = harness();
  env.c.begin(0, intent());
  for (let i = 1; i <= 30; i += 1) env.c.preview([3.2 + i * 0.01, 0.9 + i * 0.005, 0]);
  const target = [3.55, 1.05, 0];
  const first = env.c.commit(target);
  const second = env.c.commit([9, 9, 9]); // a repeated commit while submitting is refused
  assert.equal(env.c.record().phase, 'submitting');
  assert.equal(env.c.cancel('escape'), false, 'a submitted move cannot be cancelled');
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.ok, true);
  assert.equal(b.reason, 'submitted');
  assert.equal(env.submitted.length, 1);
  assert.equal(env.built, 1);
  assert.equal(env.refetches, 1, 'state rechecked once before sending');
  const cmd = env.submitted[0].cmd;
  assert.equal(cmd.type, 'move_equipment');
  assert.equal(cmd.expected_revision, 0);
  assert.deepEqual(cmd.payload.placement.position_m, target);
  assert.deepEqual(cmd.payload.placement.rotation_deg, [0, 0, -90], 'rotation preserved');
  assert.equal(cmd.payload.placement.manual, true);
  assert.equal(Object.keys(cmd.assumptions).length, 1);
  assert.equal(env.submitted[0].label, 'Move HV-102');
  const r = env.c.record();
  assert.equal(r.phase, 'accepted');
  assert.deepEqual(r.basePosition, [3.2, 0.9, 0], 'the base identity of the old scene is kept for its preview');
  assert.equal(r.sourceRevision, 0);
  assert.equal(env.c.snapshot().toRevision, 1);
  assert.match(env.c.outcome().text, /r0 → r1\. 1 incident route needs rerouting/);
  assert.equal(env.c.begin(1, intent({ sourceRevision: 1, inputDigest: D1, position: target })), false, 'no second move meanwhile');
});

test('a base that changed before sending (stale state, moved item, refetch) sends nothing', async () => {
  let env = harness();
  env.c.begin(0, intent());
  env.state = state({ revision: 1, digest: D1 });
  assert.equal((await env.c.commit([4, 0.9, 0])).reason, 'stale');
  assert.equal(env.built + env.submitted.length, 0);

  env = harness();
  env.c.begin(0, intent());
  env.refetchImpl = () => { env.state = state({ position: [3.3, 0.9, 0] }); env.seq += 1; env.applied = env.seq; return { ok: true }; };
  const r = await env.c.commit([4, 0.9, 0]);
  assert.equal(r.reason, 'base-changed', 'the fresh state shows the item moved elsewhere');
  assert.equal(env.built + env.submitted.length, 0);
  assert.equal(env.c.record(), null);
  assert.equal(env.c.outcome().kind, 'not-sent');

  env = harness();
  env.c.begin(0, intent());
  env.refetchImpl = () => { env.online = false; return { ok: false }; };
  assert.equal((await env.c.commit([4, 0.9, 0])).reason, 'offline');
  assert.equal(env.built + env.submitted.length, 0);
});

test('an envelope that cannot be prepared sends nothing and leaves no stuck record', async () => {
  const notes = [];
  const published = [];
  let submitted = 0;
  const s = state();
  const coordinator = createMoveCoordinator({
    getState: () => s, isOnline: () => true, pane: (i) => pane(i), panes: () => [pane(0)],
    refetch: async () => ({ ok: true }),
    submit: async () => { submitted += 1; return { ok: true, state: s }; },
    buildCommand: () => { throw new Error('no secure random source for fresh IDs'); },
    publish: (r) => published.push(r), cancelViewer: () => {}, notify: (kind, title) => notes.push({ kind, title }), now: () => 1,
  });
  coordinator.begin(0, intent());
  const result = await coordinator.commit([4, 0.9, 0]);
  assert.equal(result.reason, 'not-sent');
  assert.equal(submitted, 0);
  assert.equal(coordinator.record(), null);
  assert.equal(published.at(-1), null, 'every view restored');
  assert.match(coordinator.outcome().text, /could not be prepared/);
  assert.equal(notes.at(-1).kind, 'err');
});

test('a definitive rejection (4xx) removes the preview and is not resent', async () => {
  const env = harness();
  env.submitImpl = () => ({ ok: false, error: apiError(409, 'move_equipment rejected; revision 0 is unchanged') });
  env.c.begin(0, intent());
  const r = await env.c.commit([4, 0.9, 0]);
  assert.equal(r.reason, 'rejected');
  assert.equal(env.submitted.length, 1);
  assert.equal(env.c.record(), null);
  assert.equal(env.c.outcome().kind, 'rejected');
  assert.equal(env.notes.at(-1).kind, 'warn');
});

test('held command, lost answer: fresh base states with no trace keep it unconfirmed and blocking; it lands once', async () => {
  const env = harness();
  let held = null;
  env.submitImpl = (cmd) => {
    held = cmd; // the service has not processed it yet (blocked or in flight); the client loses the answer
    return { ok: false, error: apiError(0, 'no answer from the local service within 30 s', true) };
  };
  env.c.begin(0, intent());
  const r = await env.c.commit([4, 0.9, 0]);
  assert.equal(r.reason, 'uncertain');
  assert.equal(env.c.record().phase, 'uncertain');
  assert.equal(env.notes.at(-1).title, 'Move HV-102: unconfirmed');
  // fresh states requested after the loss, still the base revision with no trace: no conclusion
  for (let i = 0; i < 5; i += 1) {
    env.state = state();
    env.c.stateChanged(env.state);
  }
  const o = env.c.outcome();
  assert.equal(env.c.record().phase, 'uncertain', 'still unconfirmed');
  assert.equal(o.kind, 'uncertain');
  assert.match(o.text, /^Unconfirmed: .*No record of it yet at r0\. It may still be applied; nothing is resent\.$/);
  assert.doesNotMatch(o.text, /not applied|move again|failed/i, 'no failure claim, no invitation to retry');
  assert.equal(env.c.begin(1, intent()), false, 'no new move while it may still land');
  assert.equal(env.c.refusal().reason, 'unconfirmed');
  assert.equal(env.c.cancel('escape'), false, 'Escape cannot cancel it');
  assert.equal(env.submitted.length, 1);
  // the held request is processed: the next state shows it, once
  env.state = accepted(held, state());
  env.c.stateChanged(env.state);
  assert.equal(env.c.record().phase, 'accepted');
  assert.equal(env.c.outcome().how, 'reconciled');
  env.c.stateChanged(env.state);
  assert.equal(env.notes.filter((n) => n.title === 'Move HV-102: accepted').length, 1, 'confirmed once');
  assert.equal(env.submitted.length, 1, 'never resent');
});

test('request dropped before the service: unconfirmed until the user hides it; hidden, it stays watched and reported', async () => {
  const env = harness();
  let dropped = null;
  env.submitImpl = (cmd) => { dropped = cmd; env.online = false; return { ok: false, error: apiError(0, 'the local service is not reachable', true) }; };
  env.c.begin(0, intent());
  await env.c.commit([4, 0.9, 0]);
  env.online = true;
  for (let i = 0; i < 3; i += 1) env.c.stateChanged(state());
  assert.equal(env.c.record().phase, 'uncertain', 'no state without a trace settles it');
  assert.equal(env.c.dismiss(1), false, 'only its own pane hides it');
  assert.equal(env.c.dismiss(0), true);
  assert.equal(env.c.record(), null, 'preview hidden, views restored');
  assert.equal(env.published.at(-1), null);
  assert.equal(env.c.outcome().kind, 'unconfirmed');
  assert.match(env.c.outcome().text, /still unconfirmed.*Nothing was cancelled, undone or resent/);
  assert.deepEqual(env.c.unresolved().map((u) => u.commandId), [dropped.id]);
  // a new, explicitly requested move starts from freshly verified state and is a new command
  assert.equal(env.c.begin(0, intent()), true);
  env.submitImpl = null;
  env.online = true;
  await env.c.commit([3.6, 0.9, 0]);
  assert.equal(env.submitted.length, 2);
  assert.notEqual(env.submitted[1].cmd.id, dropped.id, 'nothing reused or resent');
  assert.deepEqual(env.submitted[1].cmd.payload.placement.position_m, [3.6, 0.9, 0], 'not the old delta');
  // the old command shows up after all (e.g. a long-delayed delivery): reported once, then no longer watched
  const late = accepted(dropped, state());
  env.c.stateChanged(late);
  assert.equal(env.notes.filter((n) => n.title === 'Move HV-102: applied').length, 1);
  assert.deepEqual(env.c.unresolved(), []);
});

test('unconfirmed watch is bounded and ends with the project or service session', async () => {
  const env = harness();
  env.submitImpl = () => ({ ok: false, error: apiError(0, 'the local service is not reachable', true) });
  for (let i = 0; i < 6; i += 1) {
    env.state = state();
    assert.equal(env.c.begin(0, intent()), true, `move ${i}`);
    await env.c.commit([4 + i, 0.9, 0]);
    assert.equal(env.c.dismiss(0), true);
  }
  assert.equal(env.c.unresolved().length, MAX_UNRESOLVED, 'oldest dropped beyond the bound');
  env.c.stateChanged(state({ started: 's2' }));
  assert.deepEqual(env.c.unresolved(), [], 'a restarted service ends the watch');
  // an unconfirmed (not hidden) move also ends with the session, saying what the model shows
  env.state = state();
  env.c.begin(0, intent());
  await env.c.commit([5, 0.9, 0]);
  assert.equal(env.c.record().phase, 'uncertain');
  env.c.stateChanged(state({ started: 's3' }));
  assert.equal(env.c.record(), null);
  assert.equal(env.c.outcome().kind, 'session-changed');
  assert.match(env.c.outcome().text, /service was restarted; the unconfirmed move of HV-102 is no longer watched/);
});

test('uncertain after a service restart: the envelope\'s fresh placement assumption proves acceptance', async () => {
  const env = harness();
  env.submitImpl = (cmd) => {
    const s = accepted(cmd, env.state);
    s.history.recent = [];
    s.last_command = null;
    s.service.started_at = 's2';
    env.state = s;
    return { ok: false, error: apiError(502, 'HTTP 502') };
  };
  env.c.begin(0, intent());
  await env.c.commit([4, 0.9, 0]);
  assert.equal(env.c.record().phase, 'accepted', 'resolved from the placement right away');
  assert.equal(env.c.snapshot().toRevision, 1);
  const ev = commandEvidence(env.state, env.c.snapshot().submitted);
  assert.equal(ev.via, 'placement');
});

test('an answer that does not mention the command is unconfirmed until the placement shows its assumption', async () => {
  const env = harness();
  let sent = null;
  env.submitImpl = (cmd) => { sent = cmd; return { ok: true, state: state() }; };
  env.c.begin(0, intent());
  const r = await env.c.commit([4, 0.9, 0]);
  assert.equal(r.reason, 'uncertain');
  env.c.stateChanged(state());
  assert.equal(env.c.record().phase, 'uncertain');
  const s = accepted(sent, state());
  s.history.recent = [];
  s.last_command = null;
  env.c.stateChanged(s);
  assert.equal(env.c.record().phase, 'accepted');
  assert.equal(env.c.outcome().via, 'placement');
});

test('preview ends on stale state, service restart, offline, hidden/destroyed/published/changed origin pane, other selection', () => {
  const cases = [
    ['stale', (e) => { e.state = state({ revision: 1, digest: D1 }); e.c.stateChanged(e.state); }],
    ['base-changed', (e) => { e.state = state({ position: [0, 0, 0] }); e.c.stateChanged(e.state); }],
    ['rotation-changed', (e) => { e.state = state({ rotation: [0, 0, 0] }); e.c.stateChanged(e.state); }],
    ['service-changed', (e) => { e.state = state({ started: 's2' }); e.c.stateChanged(e.state); }],
    ['offline', (e) => { e.online = false; e.c.onlineChanged(false); }],
    ['hidden', (e) => { e.panes[0] = pane(0, { active: false }); e.c.paneChanged(); }],
    ['destroyed', (e) => { e.panes[0] = pane(0, { type: 'pid', installed: null }); e.c.paneChanged(); }],
    ['published', (e) => { e.panes[0] = pane(0, { mode: 'published' }); e.c.paneChanged(); }],
    ['source-changed', (e) => { e.panes[0] = pane(0, { installed: installed({}, '/artifacts/builds/b0-again/display/scene.json') }); e.c.paneChanged(); }],
    ['selection-changed', (e) => e.c.selectionChanged(['ent-tank'])],
  ];
  for (const [reason, fire] of cases) {
    const env = harness();
    env.c.begin(0, intent());
    env.c.preview([4, 0.9, 0]);
    fire(env);
    assert.equal(env.c.record(), null, reason);
    assert.equal(env.c.outcome().reason, reason);
    assert.deepEqual(env.cancelled, [[0, reason]], reason);
    assert.equal(env.submitted.length, 0);
  }
  const env = harness();
  env.c.begin(0, intent());
  env.c.selectionChanged(['port-valve-in']); // a nozzle of the same item keeps the preview
  env.c.paneChanged(); // nothing changed
  env.c.stateChanged(env.state);
  assert.equal(env.c.record().phase, 'preview');
});

test('submitted moves survive pane, selection and connection changes (cancel cannot revoke them)', async () => {
  const env = harness();
  env.c.begin(0, intent());
  await env.c.commit([4, 0.9, 0]);
  assert.equal(env.c.record().phase, 'accepted');
  env.c.selectionChanged([]);
  env.c.onlineChanged(false);
  assert.equal(env.c.cancel('escape'), false);
  assert.equal(env.c.cancellable(), false);
  assert.equal(env.c.record().phase, 'accepted');
});

test('display hold: a Live pane on the base scene keeps it while only unfinished files of the new build are offered', () => {
  const rec = makeRecord({ paneIndex: 0, entityId: 'ent-valve', sourceRevision: 0, inputDigest: D0, basePosition: [3.2, 0.9, 0], position: [4, 0.9, 0], rotation: [0, 0, -90], phase: 'accepted' });
  const base = installed();
  const progressive = { url: '/artifacts/builds/b1/preview/display-0003.json', kind: 'preview', build_status: 'running', source_revision: 1, input_digest: D1 };
  const written = { url: '/artifacts/builds/b1/display/scene.json', kind: 'output', build_status: 'running', source_revision: 1, input_digest: D1 };
  const pendingFinal = { ...written, build_status: 'pending' };
  assert.equal(moveHold({ record: rec, mode: 'live', installed: base, offered: progressive }), base);
  assert.equal(moveHold({ record: rec, mode: 'live', installed: base, offered: written }), base, 'written but the build still runs');
  assert.equal(moveHold({ record: rec, mode: 'live', installed: base, offered: pendingFinal }), null, 'a final scene (pending counts) is installed');
  assert.equal(moveHold({ record: { ...rec, phase: 'uncertain' }, mode: 'live', installed: base, offered: progressive }), base);
  assert.equal(moveHold({ record: { ...rec, phase: 'submitting' }, mode: 'live', installed: base, offered: progressive }), base);
  assert.equal(moveHold({ record: { ...rec, phase: 'preview' }, mode: 'live', installed: base, offered: progressive }), null, 'an unsubmitted preview is cancelled by a source change, not held');
  assert.equal(moveHold({ record: rec, mode: 'published', installed: base, offered: progressive }), null);
  assert.equal(moveHold({ record: rec, mode: 'live', installed: installed({ source_revision: 1, input_digest: D1 }), offered: progressive }), null, 'only the exact base scene is kept');
  assert.equal(moveHold({ record: null, mode: 'live', installed: base, offered: progressive }), null);
  assert.equal(isFinalOffer({ kind: 'output', build_status: 'failed' }), true);
});

test('accepted record: kept while a shown Live pane has the base scene; dropped when superseded, failed or installed', () => {
  const rec = makeRecord({ paneIndex: 0, entityId: 'ent-valve', sourceRevision: 0, inputDigest: D0, basePosition: [3.2, 0.9, 0], position: [4, 0.9, 0], rotation: [0, 0, -90], phase: 'accepted' });
  const s1 = state({ revision: 1, digest: D1, position: [4, 0.9, 0], build: { status: 'running', source_revision: 1 } });
  const onBase = [pane(0), pane(1, { installed: installed({ source_revision: 1, input_digest: D1 }, '/b1') })];
  assert.equal(acceptedClearReason({ record: rec, state: s1, toRevision: 1, livePanes: onBase }), null);
  assert.equal(acceptedClearReason({ record: rec, state: s1, toRevision: 1, livePanes: [onBase[1]] }), 'installed');
  assert.equal(acceptedClearReason({ record: rec, state: s1, toRevision: 1, livePanes: [pane(0, { active: false })] }), 'installed', 'a hidden pane does not keep it');
  assert.equal(acceptedClearReason({ record: rec, state: state({ revision: 2, digest: 'c'.repeat(64) }), toRevision: 1, livePanes: onBase }), 'superseded', 'undone');
  assert.equal(acceptedClearReason({ record: rec, state: { ...s1, build: { status: 'failed', source_revision: 1 } }, toRevision: 1, livePanes: onBase }), 'build-failed');

  const env = harness();
  env.c.begin(0, intent());
  return env.c.commit([4, 0.9, 0]).then(() => {
    env.c.sweep();
    assert.equal(env.c.record().phase, 'accepted', 'both panes still show the base scene');
    const regenerated = installed({ source_revision: 1, input_digest: D1, build_status: 'pending' }, '/b1/scene.json');
    env.panes[0] = pane(0, { installed: regenerated });
    env.c.paneChanged();
    assert.equal(env.c.record().phase, 'accepted', 'the duplicate Live pane still draws the preview on the base scene');
    env.panes[1] = pane(1, { installed: regenerated });
    env.c.paneChanged();
    assert.equal(env.c.record(), null, 'regenerated scene installed everywhere: the record (and every preview) ends');
    assert.equal(env.c.outcome().cleared, 'installed');
  });
});

test('typed metres are exact, refused with a reason when invalid or beyond the drag limit, never clamped', () => {
  assert.deepEqual(parseMetres(' 1.25 '), { ok: true, value: 1.25 });
  assert.deepEqual(parseMetres('-0'), { ok: true, value: 0 });
  assert.equal(parseMetres('1e-3').value, 0.001);
  assert.equal(parseMetres('.5').value, 0.5);
  assert.equal(parseMetres('').problem, 'empty');
  assert.equal(parseMetres('1,5').problem, 'comma');
  for (const bad of ['--1', '1.2.3', '1m', 'NaN', 'Infinity', '0x10', '1e999']) assert.equal(parseMetres(bad).ok, false, bad);
  const ok = readTyped(['3.2', '0.9', '0.123456789'], [3.2, 0.9, 0]);
  assert.deepEqual(ok.position, [3.2, 0.9, 0.123456789], 'no rounding');
  const bad = readTyped(['3.2', 'x', '0'], [3.2, 0.9, 0]);
  assert.equal(bad.ok, false);
  assert.match(bad.message, /^Y: /);
  const far = readTyped(['150', '0.9', '0'], [3.2, 0.9, 0]);
  assert.equal(far.ok, false);
  assert.deepEqual(far.position, [150, 0.9, 0], 'the entry is reported, not replaced by a clamped value');
  assert.match(far.message, /limited to ±100 m.*Nothing was changed/);
});

test('placement gate compares exactly; command evidence reads the log, the placement and the assumption', () => {
  const s = state();
  assert.equal(placementGate(s.project, 'ent-valve', [3.2, 0.9, 0], [0, 0, -90]).ok, true);
  assert.equal(placementGate(s.project, 'ent-valve', [3.2, 0.9, 1e-12]).reason, 'base-changed');
  assert.equal(placementGate(s.project, 'conn-1', [0, 0, 0]).reason, 'missing');
  const sub = { commandId: 'cmd-1', assumptionId: 'asm-new', entityId: 'ent-valve', baseRevision: 0, baseDigest: D0 };
  assert.deepEqual(commandEvidence(s, sub), { outcome: 'no-trace', toRevision: null, via: 'state', atBase: true }, 'no trace is not a failure');
  assert.equal(commandEvidence(state({ revision: 1, digest: D1 }), sub).atBase, false);
  assert.equal(commandEvidence(state({ recent: [{ command_id: 'cmd-1', status: 'accepted', to_revision: 1 }] }), sub).toRevision, 1);
  assert.equal(commandEvidence(state({ last: { command_id: 'cmd-1', status: 'no_op' } }), sub).outcome, 'no-op');
  assert.equal(commandEvidence(state({ revision: 3, digest: D1, assumption: 'asm-new' }), sub).via, 'placement');
});
