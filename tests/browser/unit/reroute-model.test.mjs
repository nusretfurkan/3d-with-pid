// Reroute button decisions and honest build feedback (reroute-model.js), on the read-only fixture.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { rerouteBuildNote, rerouteOffer, rerouteOutcome, rerouteRequest } from '../../../web/js/reroute-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const fixture = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;
const firstConnection = (project) => Object.keys(project.model.connections).sort()[0];

test('a current route stays a status chip, not a button', () => {
  const p = fixture();
  const id = firstConnection(p);
  p.physical.routes[id].status = 'current';
  const offer = rerouteOffer(p, id);
  assert.equal(offer.pending, false);
  assert.equal(offer.actionable, false);
  assert.equal(offer.status, 'current');
});

test('needs_reroute between placed items is actionable and keeps its status and reason', () => {
  const p = fixture();
  const id = firstConnection(p);
  Object.assign(p.physical.routes[id], { status: 'needs_reroute', reason: 'equipment ent-pump placement changed' });
  const offer = rerouteOffer(p, id);
  assert.deepEqual({ ...offer }, { status: 'needs_reroute', reason: 'equipment ent-pump placement changed', pending: true, actionable: true, why: null });
});

test('an unplaced endpoint keeps the button visible but disabled, naming the item to place', () => {
  const p = fixture();
  const id = firstConnection(p);
  const conn = p.model.connections[id];
  const entityId = p.model.ports[conn.to_port_id].entity_id;
  p.physical.routes[id].status = 'unresolved';
  p.physical.placements[entityId] = { position_m: null, rotation_deg: null, status: 'unresolved', manual: false, reason: 'test' };
  const offer = rerouteOffer(p, id);
  assert.equal(offer.pending, true);
  assert.equal(offer.actionable, false);
  assert.match(offer.why, new RegExp(`${p.model.entities[entityId].tag} has no physical placement; place it first`));
  delete p.physical.routes[id];
  assert.equal(rerouteOffer(p, id).status, 'none', 'a missing route record is pending too');
  assert.equal(rerouteOffer(p, 'conn-nope').pending, false, 'unknown connections offer nothing');
});

test('request carries exactly the two contracted fields', () => {
  assert.deepEqual(rerouteRequest('conn-a', 7), { connection_id: 'conn-a', expected_revision: 7 });
  assert.deepEqual(Object.keys(rerouteRequest('conn-a', 7)).sort(), ['connection_id', 'expected_revision']);
});

test('success only when the reply has a current route at a newer revision', () => {
  const p = fixture();
  const id = firstConnection(p);
  const state = (rev, status, mode = 'guided') => {
    const q = structuredClone(p);
    q.revision = rev;
    Object.assign(q.physical.routes[id], { status, mode });
    return { project: q };
  };
  assert.deepEqual(rerouteOutcome(4, state(5, 'current'), id), { routed: true, toRevision: 5, mode: 'guided', status: 'current' });
  assert.equal(rerouteOutcome(4, state(4, 'current'), id).routed, false, 'no new revision is no reroute');
  assert.equal(rerouteOutcome(4, state(5, 'needs_reroute'), id).routed, false);
  assert.equal(rerouteOutcome(4, null, id).routed, false);
});

test('build note: never "published" before all five outputs are for that revision', () => {
  const base = (sync, publishedRev, rev = 5) => ({ project: { revision: rev }, sync: { state: sync, message: '' }, published: publishedRev == null ? null : { source_revision: publishedRev } });
  const building = rerouteBuildNote(base('building', 4), 5);
  assert.equal(building.settled, false);
  assert.match(building.text, /r5 saved · outputs regenerating, not yet current/);
  assert.equal(building.tone, 'working');
  assert.match(rerouteBuildNote(base('pending', 4), 5).text, /outputs pending, not yet current/);
  const stale = rerouteBuildNote(base('synchronized', 4), 5);
  assert.notEqual(stale.tone, 'current', 'a synchronized state with an older publication is not current');
  const done = rerouteBuildNote(base('synchronized', 5), 5);
  assert.deepEqual(done, { tone: 'current', text: 'All five outputs published for r5.', settled: true });
  const failed = rerouteBuildNote(base('failed', 4), 5);
  assert.equal(failed.settled, true);
  assert.equal(failed.tone, 'failed');
  assert.match(rerouteBuildNote(base('building', 4, 6), 5).text, /Superseded by revision 6/);
  assert.equal(rerouteBuildNote(null, 5), null);
});
