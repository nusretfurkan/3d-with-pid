// Exact incremental scene updates (review finding 2: no reuse inferred from samples or bounds).
import test from 'node:test';
import assert from 'node:assert/strict';
import { appearanceKey, geometryProblem, isFinalScene, planSceneUpdate } from '../../../web/js/scene-diff.js';

/** A strip of n quads: enough vertices that any sampling scheme skips most of them. */
function component(id, n = 400, tweak = () => {}) {
  const positions = [];
  const normals = [];
  const indices = [];
  for (let i = 0; i <= n; i += 1) {
    positions.push(i * 0.01, 0, 0, i * 0.01, 0.05, 0);
    normals.push(0, 0, 1, 0, 0, 1);
  }
  for (let i = 0; i < n; i += 1) {
    const a = 2 * i;
    indices.push(a, a + 2, a + 1, a + 1, a + 2, a + 3);
  }
  const c = {
    id, semantic_id: id.split('#')[0], component_key: id.split('#')[1] || 'body', tag: 'P-101', status: 'current', display_only: false,
    positions, normals, indices, material: { color: '#1f4f8f', metalness: 0.15, roughness: 0.38 },
    bounds_m: { min: [0, 0, 0], max: [n * 0.01, 0.05, 0] },
  };
  tweak(c);
  return c;
}

function installedFrom(components) {
  return new Map(components.map((c) => [c.id, { geometry: { positions: c.positions, normals: c.normals, indices: c.indices }, appearance: appearanceKey(c) }]));
}

test('identical components are reused (fresh arrays, equal content)', () => {
  const before = [component('ent-pump#body'), component('ent-pump#motor')];
  const plan = planSceneUpdate(installedFrom(before), [component('ent-pump#body'), component('ent-pump#motor')], true);
  assert.deepEqual(plan.keep.map((c) => c.id), ['ent-pump#body', 'ent-pump#motor']);
  assert.equal(plan.replace.length + plan.add.length + plan.restyle.length + plan.remove.length, 0);
});

test('a sub-10-micrometre move of one interior vertex with equal bounds and lengths is a replacement', () => {
  const before = [component('ent-pump#body')];
  const after = [component('ent-pump#body', 400, (c) => { c.positions[3 * 401 + 1] += 3e-6; })]; // interior vertex, y only
  assert.deepEqual(after[0].bounds_m, before[0].bounds_m);
  assert.equal(after[0].positions.length, before[0].positions.length);
  const plan = planSceneUpdate(installedFrom(before), after, true);
  assert.deepEqual(plan.replace.map((c) => c.id), ['ent-pump#body']);
});

test('normals-only and index-only changes are replacements', () => {
  const before = [component('a#x'), component('b#x')];
  const after = [
    component('a#x', 400, (c) => { c.normals[500] = 0.5; }),
    component('b#x', 400, (c) => { [c.indices[10], c.indices[11]] = [c.indices[11], c.indices[10]]; }), // winding flip, same length
  ];
  const plan = planSceneUpdate(installedFrom(before), after, true);
  assert.deepEqual(plan.replace.map((c) => c.id).sort(), ['a#x', 'b#x']);
});

test('appearance-only changes restyle without rebuilding geometry', () => {
  const before = [component('a#x'), component('b#x'), component('c#x')];
  const after = [
    component('a#x', 400, (c) => { c.material = { ...c.material, color: '#6b7378' }; }),
    component('b#x', 400, (c) => { c.status = 'needs_reroute'; }),
    component('c#x', 400, (c) => { c.display_only = true; }),
  ];
  const plan = planSceneUpdate(installedFrom(before), after, true);
  assert.deepEqual(plan.restyle.map((c) => c.id), ['a#x', 'b#x', 'c#x']);
  assert.equal(plan.replace.length, 0);
});

test('previews ghost unreached components; final scenes remove them', () => {
  const installed = installedFrom([component('a#x'), component('b#x')]);
  const preview = planSceneUpdate(installed, [component('a#x')], false);
  assert.deepEqual(preview.ghost, ['b#x']);
  assert.deepEqual(preview.remove, []);
  const final = planSceneUpdate(installed, [component('a#x'), component('n#x')], true);
  assert.deepEqual(final.remove, ['b#x']);
  assert.deepEqual(final.add.map((c) => c.id), ['n#x']);
});

test('unusable components are reported and not treated as present', () => {
  const installed = installedFrom([component('a#x')]);
  const bad = [
    component('a#x', 4, (c) => { c.indices[0] = 999; }),
    component('b#x', 4, (c) => { c.positions[2] = Number.NaN; }),
    component('c#x', 4, (c) => { c.normals = c.normals.slice(1); }),
    component('d#x', 4),
    component('d#x', 4),
    { positions: [] },
  ];
  const plan = planSceneUpdate(installed, bad, true);
  assert.deepEqual(plan.add.map((c) => c.id), ['d#x']);
  assert.deepEqual(plan.remove, ['a#x']);
  const reasons = Object.fromEntries(plan.invalid.map((i) => [i.id, i.reason]));
  assert.match(reasons['a#x'], /index/);
  assert.match(reasons['b#x'], /non-finite/);
  assert.match(reasons['c#x'], /normals/);
  assert.match(reasons['d#x'], /duplicate/);
  assert.match(reasons.null, /without an id/);
  assert.equal(geometryProblem(component('ok#x', 2)), null);
});

test('final versus working scenes', () => {
  assert.equal(isFinalScene({ status: 'complete' }), true);
  assert.equal(isFinalScene({ status: 'pending' }), true);
  assert.equal(isFinalScene({ status: 'working', preview: { final: false } }), false);
  assert.equal(isFinalScene({ status: 'complete', preview: { final: false } }), false);
  assert.equal(isFinalScene(null), false);
});
