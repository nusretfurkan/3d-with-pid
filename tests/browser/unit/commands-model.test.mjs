// Command builders and review against the real fixture (read only). The service remains the
// validator; these tests check that the UI sends complete envelopes with fresh identities,
// explicit assumption records, and the topology rules documented for insert_valve.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  buildInsertValve, buildPlacement, buildRename, buildSetParameter, buildSetRoute, describeCommand, diagramPath, envelope,
  nextFreeTag, orthogonalPath, reviewCommandText, simplifyPath, tagInUse,
} from '../../../web/js/commands-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const fixture = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;

function allIds(project) {
  const m = project.model;
  return new Set([...Object.keys(m.entities), ...Object.keys(m.ports), ...Object.keys(m.connections), ...Object.keys(m.assumptions),
    ...Object.keys(project.pid.symbols), ...Object.keys(project.pid.edges)]);
}

test('envelopes carry the current revision and a fresh command ID every time', () => {
  const p = fixture();
  const a = envelope('undo', {}, p.revision);
  const b = envelope('undo', {}, p.revision);
  assert.deepEqual(Object.keys(a).sort(), ['expected_revision', 'id', 'payload', 'type']);
  assert.notEqual(a.id, b.id);
  assert.equal(a.expected_revision, p.revision);
  const r = buildRename(p, 'ent-pump', 'P-201');
  assert.deepEqual(r.payload, { entity_id: 'ent-pump', tag: 'P-201' });
});

test('set_parameter adds exactly one new assumption and references it', () => {
  const p = fixture();
  const cmd = buildSetParameter(p, 'ent-pump', 'motor_length', 0.42);
  const added = Object.keys(cmd.assumptions);
  assert.equal(added.length, 1);
  assert.equal(cmd.payload.quantity.assumption_id, added[0]);
  assert.deepEqual(cmd.payload.quantity, { value: 0.42, unit: 'm', assumption_id: added[0] });
  assert.ok(!allIds(p).has(added[0]));
  assert.deepEqual(Object.keys(cmd.assumptions[added[0]]).sort(), ['description', 'source']);
  assert.match(cmd.assumptions[added[0]].description, /not a vendor, design or verified value/);
});

test('placement chooses move_equipment for placed items and place_equipment for unresolved ones', () => {
  const p = fixture();
  assert.equal(buildPlacement(p, 'ent-pump', [1.6, 0, 0], [0, 0, 0]).type, 'move_equipment');
  p.physical.placements['ent-pump'] = { position_m: null, rotation_deg: null, status: 'unresolved', manual: false, assumption_id: 'asm-skid-layout', reason: 'test' };
  const cmd = buildPlacement(p, 'ent-pump', [1.6, 0, 0], [0, 0, 0]);
  assert.equal(cmd.type, 'place_equipment');
  assert.deepEqual(Object.keys(cmd.payload.placement).sort(), ['assumption_id', 'manual', 'position_m', 'rotation_deg', 'status']);
  assert.equal(cmd.payload.placement.status, 'placed');
  assert.ok(cmd.assumptions[cmd.payload.placement.assumption_id]);
});

test('insert_valve rewires A -> valve -> B with fresh, unique IDs and free tags', () => {
  const p = fixture();
  const conn = p.model.connections['conn-discharge-2'];
  const cmd = buildInsertValve(p, 'conn-discharge-2', { kind: 'isolation_valve' });
  const pl = cmd.payload;
  assert.equal(cmd.type, 'insert_valve');
  assert.deepEqual(Object.keys(pl).sort(), ['connection_id', 'downstream_connection', 'downstream_edge', 'entity', 'ports', 'symbol']);
  const inlet = pl.ports.find((x) => x.name === 'inlet');
  const outlet = pl.ports.find((x) => x.name === 'outlet');
  assert.equal(inlet.direction, 'in');
  assert.equal(outlet.direction, 'out');
  assert.equal(pl.downstream_connection.from_port_id, outlet.id);
  assert.equal(pl.downstream_connection.to_port_id, conn.to_port_id);
  const upEdge = Object.values(p.pid.edges).find((e) => e.connection_id === 'conn-discharge-2');
  assert.equal(pl.downstream_edge.from_symbol_id, pl.symbol.id);
  assert.equal(pl.downstream_edge.to_symbol_id, upEdge.to_symbol_id);
  assert.equal(pl.downstream_edge.connection_id, pl.downstream_connection.id);
  assert.equal(pl.symbol.entity_id, pl.entity.id);
  const fresh = [pl.entity.id, inlet.id, outlet.id, pl.downstream_connection.id, pl.symbol.id, pl.downstream_edge.id];
  assert.equal(new Set(fresh).size, fresh.length);
  const existing = allIds(p);
  for (const id of fresh) assert.ok(!existing.has(id), `${id} must be new`);
  assert.equal(tagInUse(p, pl.entity.tag), null);
  assert.equal(tagInUse(p, pl.downstream_connection.tag), null);
  assert.notEqual(pl.entity.tag.toLowerCase(), pl.downstream_connection.tag.toLowerCase());
  assert.ok(!('placement' in pl), 'no invented placement: the valve is saved as explicitly unresolved');
  for (const [name, q] of Object.entries(pl.entity.parameters)) {
    assert.ok(p.model.assumptions[q.assumption_id], `${name} provenance resolves`);
  }
  const lines = describeCommand(cmd, p);
  assert.ok(lines.some((l) => /NRV-101 → HV-103 → HV-102/.test(l)), lines.join('\n'));
});

test('nextFreeTag skips used tags case-insensitively', () => {
  const p = fixture();
  assert.equal(nextFreeTag(p, 'HV-'), 'HV-103');
  assert.equal(nextFreeTag(p, 'L-'), 'L-106');
  p.model.entities['ent-pump'].tag = 'hv-103';
  assert.equal(nextFreeTag(p, 'HV-'), 'HV-104');
});

test('orthogonal route example starts and ends exactly at the port frames and is axis-aligned', () => {
  const from = { position_m: [1.725, 0, 0.5], direction: [0, 0, 1] };
  const to = { position_m: [2.21, 0, 0.7], direction: [-1, 0, 0] };
  const pts = orthogonalPath(from, to);
  assert.deepEqual(pts[0], from.position_m);
  assert.deepEqual(pts[pts.length - 1], to.position_m);
  for (let i = 0; i + 1 < pts.length; i += 1) {
    const moving = [0, 1, 2].filter((k) => Math.abs(pts[i + 1][k] - pts[i][k]) > 1e-9);
    assert.equal(moving.length, 1, `segment ${i} must move along exactly one axis`);
  }
  const cmd = buildSetRoute(fixture(), 'conn-discharge-1', pts);
  assert.equal(cmd.payload.mode, 'manual');
  assert.ok(cmd.assumptions[cmd.payload.assumption_id]);
  assert.deepEqual(simplifyPath([[0, 0, 0], [0, 0, 0], [1, 0, 0], [2, 0, 0], [2, 1, 0]]), [[0, 0, 0], [2, 0, 0], [2, 1, 0]]);
  assert.deepEqual(diagramPath([0, 0], [10, 0]), [[0, 0], [10, 0]]);
  assert.deepEqual(diagramPath([0, 0], [10, 6]), [[0, 0], [5, 0], [5, 6], [10, 6]]);
});

test('route examples keep non-round port endpoints exactly and stay strictly axis-aligned', () => {
  // scene ports carry the core's unrounded world positions; the example must not round them
  const from = { position_m: [1.7251234567891, -0.0000003141592, 0.5000004321987], direction: [0, 0, 1] };
  const to = { position_m: [2.2109876543219, 0.123456789012, 0.7000001234567], direction: [-1, 0, 0] };
  const pts = orthogonalPath(from, to);
  assert.deepEqual(pts[0], from.position_m);
  assert.deepEqual(pts[pts.length - 1], to.position_m);
  assert.notEqual(pts[0], from.position_m, 'a copy, so editing the JSON cannot alter the scene record');
  for (let i = 0; i + 1 < pts.length; i += 1) {
    const changed = [0, 1, 2].filter((k) => pts[i + 1][k] !== pts[i][k]); // exact, no tolerance
    assert.equal(changed.length, 1, `segment ${i} ${JSON.stringify(pts[i])} -> ${JSON.stringify(pts[i + 1])}`);
  }
  // float-noise differences (<= 1 nm) are absorbed into an interior move, never into an endpoint
  const noisy = orthogonalPath({ position_m: [0.1, 0, 0.2], direction: [1, 0, 0] }, { position_m: [0.9, 4e-13, 0.6], direction: [-1, 0, 0] });
  assert.deepEqual(noisy[0], [0.1, 0, 0.2]);
  assert.deepEqual(noisy[noisy.length - 1], [0.9, 4e-13, 0.6]);
});

/** Index pairs of consecutive segments that move along the same axis in opposite directions. */
function reversals(points) {
  const segs = [];
  for (let i = 0; i + 1 < points.length; i += 1) {
    const d = points[i + 1].map((v, k) => v - points[i][k]);
    const axis = d.findIndex((v) => v !== 0);
    segs.push({ axis, sign: Math.sign(d[axis]) });
  }
  const out = [];
  for (let i = 0; i + 1 < segs.length; i += 1) if (segs[i].axis === segs[i + 1].axis && segs[i].sign !== segs[i + 1].sign) out.push(i);
  return out;
}

test('route example between opposing frames closer than two stubs never reverses (browser QA finding)', () => {
  // Inserted valve placed 0.07 m after NRV-101: full 0.1 m stubs used to give 2.39 -> 2.49 -> 2.36 -> 2.46,
  // which the core accepts (axis-aligned, exact ends) but the pipe compiler rejects as reversing.
  const from = { position_m: [2.39, 0, 0.7], direction: [1, 0, 0] };
  const to = { position_m: [2.46, 0, 0.7], direction: [-1, 0, 0] };
  const straight = orthogonalPath(from, to);
  assert.deepEqual(straight, [[2.39, 0, 0.7], [2.46, 0, 0.7]]);
  assert.notEqual(straight[0], from.position_m, 'copies, not the scene records');
  // the same scenario's downstream line: HV-103 outlet 2.62 (+x) to HV-102 inlet facing +y at x 2.70 used to go
  // out to 2.72 and back; the stub now stops at the other port's x, giving the single elbow of the fixture route
  const down = orthogonalPath({ position_m: [2.62, 0, 0.7], direction: [1, 0, 0] }, { position_m: [2.7, -0.37, 0.7], direction: [0, 1, 0] });
  assert.deepEqual(down, [[2.62, 0, 0.7], [2.7, 0, 0.7], [2.7, -0.37, 0.7]]);
  const cases = [
    [from, to],
    [from, { position_m: [2.46, 0.2, 0.7], direction: [-1, 0, 0] }], // offset sideways: meet at the mid gap
    [{ position_m: [1, -0.3, 0.5], direction: [0, 1, 0] }, { position_m: [1.25, -0.2123456789, 0.5], direction: [0, -1, 0] }],
    [{ position_m: [0, 0, 1.2], direction: [0, 0, -1] }, { position_m: [0, 0, 1.0000001], direction: [0, 0, 1] }],
    [{ position_m: [2.62, 0, 0.7], direction: [1, 0, 0] }, { position_m: [2.7, -0.37, 0.7], direction: [0, 1, 0] }],
    [{ position_m: [0, 0, 0], direction: [1, 0, 0] }, { position_m: [0.05, -0.05, 0], direction: [0, 1, 0] }],
  ];
  for (const [a, b] of cases) {
    const pts = orthogonalPath(a, b);
    const label = JSON.stringify(pts);
    assert.deepEqual(pts[0], a.position_m, label);
    assert.deepEqual(pts[pts.length - 1], b.position_m, label);
    for (let i = 0; i + 1 < pts.length; i += 1) {
      assert.equal([0, 1, 2].filter((k) => pts[i + 1][k] !== pts[i][k]).length, 1, `segment ${i} of ${label}`);
    }
    assert.deepEqual(reversals(pts), [], label);
    // never back-tracks along either stub axis: forward along the start's outward direction, and
    // toward the end port along the end's axis (against its outward direction)
    for (const [frame, sense] of [[a, 1], [b, -1]]) {
      const k = frame.direction.findIndex((v) => v !== 0);
      const sign = Math.sign(frame.direction[k]) * sense;
      for (let i = 0; i + 1 < pts.length; i += 1) assert.ok((pts[i + 1][k] - pts[i][k]) * sign >= 0, `monotonic along axis ${k}: ${label}`);
    }
  }
  // frames at least two stubs apart keep the full 0.1 m stubs (previous behavior)
  const far = orthogonalPath({ position_m: [0, 0, 0], direction: [1, 0, 0] }, { position_m: [0.5, 0.3, 0], direction: [-1, 0, 0] });
  assert.deepEqual(far, [[0, 0, 0], [0.1, 0, 0], [0.1, 0.3, 0], [0.5, 0.3, 0]]); // collinear final run merged
});

test('command review refuses malformed or stale envelopes before anything is sent', () => {
  const p = fixture();
  assert.equal(reviewCommandText('{not json', p).ok, false);
  const missing = reviewCommandText(JSON.stringify({ type: 'rename_entity', payload: {} }), p);
  assert.equal(missing.ok, false);
  assert.ok(missing.errors.some((e) => /id/.test(e)));
  const unsupported = reviewCommandText(JSON.stringify({ id: 'c', expected_revision: 0, type: 'delete_entity', payload: {} }), p);
  assert.ok(unsupported.errors.some((e) => /Unsupported type/.test(e)));
  const stale = reviewCommandText(JSON.stringify({ ...buildRename(p, 'ent-pump', 'P-9'), expected_revision: p.revision + 3 }), p);
  assert.equal(stale.ok, true);
  assert.ok(stale.warnings.some((w) => /stale/.test(w)));
  const good = reviewCommandText(JSON.stringify(buildRename(p, 'ent-pump', 'P-201')), p);
  assert.deepEqual([good.ok, good.warnings.length], [true, 0]);
  assert.match(good.lines[0], /Rename P-101 \(ent-pump\) to "P-201"/);
});
