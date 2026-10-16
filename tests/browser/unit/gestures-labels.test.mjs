// Pointer release decisions (review finding 1) and screen-space label placement.
import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyRelease, movedBeyond } from '../../../web/js/gestures.js';
import { layoutLabels, leaderEnd } from '../../../web/js/labels-layout.js';

const symbol = { id: 'sym-pump' };
const port = { id: 'sym-pump#port-pump-inlet' };

test('a click selects the element hit at release, not the capturing host', () => {
  assert.deepEqual(classifyRelease({ moved: false, downTarget: symbol, upTarget: symbol }), { action: 'select', target: symbol });
  assert.deepEqual(classifyRelease({ moved: false, downTarget: symbol, upTarget: port }), { action: 'select', target: port });
});

test('the press target is used when the release hit test finds nothing semantic', () => {
  assert.deepEqual(classifyRelease({ moved: false, downTarget: symbol, upTarget: null }), { action: 'select', target: symbol });
});

test('a click on blank paper clears; a drag never selects; pick mode reports a point', () => {
  assert.deepEqual(classifyRelease({ moved: false, downTarget: null, upTarget: null }), { action: 'clear' });
  assert.deepEqual(classifyRelease({ moved: true, downTarget: symbol, upTarget: symbol }), { action: 'pan-end' });
  assert.deepEqual(classifyRelease({ moved: false, pickMode: true, downTarget: symbol, upTarget: symbol }), { action: 'pick-point' });
  assert.deepEqual(classifyRelease({ cancelled: true, downTarget: symbol }), { action: 'none' });
  assert.equal(movedBeyond({ x: 10, y: 10 }, 12, 12), false);
  assert.equal(movedBeyond({ x: 10, y: 10 }, 14, 10), true);
});

function overlap(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

test('placed labels never overlap each other and stay inside the viewport', () => {
  const items = [];
  for (let i = 0; i < 24; i += 1) {
    items.push({ id: `L${i}`, ax: 200 + (i % 6) * 18, ay: 150 + Math.floor(i / 6) * 14, w: 52, h: 18, priority: i % 3, depth: i });
  }
  const placed = layoutLabels(items, { w: 640, h: 400 });
  const visible = placed.filter((p) => p.visible);
  assert.ok(visible.length >= 4, 'some labels are placed');
  assert.ok(visible.length < items.length, 'crowded labels are culled');
  for (const p of visible) {
    assert.ok(p.x >= 0 && p.y >= 0 && p.x + p.w <= 640 && p.y + p.h <= 400);
  }
  for (let i = 0; i < visible.length; i += 1) {
    for (let j = i + 1; j < visible.length; j += 1) assert.equal(overlap(visible[i], visible[j]), false);
  }
});

test('higher priority labels win a collision; off-screen anchors are hidden', () => {
  const items = [
    { id: 'conn', ax: 300, ay: 200, w: 60, h: 18, priority: 1, depth: 1 },
    { id: 'selected', ax: 302, ay: 201, w: 60, h: 18, priority: 6, depth: 9 },
    { id: 'away', ax: -50, ay: 10, w: 40, h: 18, priority: 9, depth: 0, onScreen: false },
  ];
  const placed = Object.fromEntries(layoutLabels(items, { w: 800, h: 600 }).map((p) => [p.id, p]));
  assert.equal(placed.selected.visible, true);
  assert.equal(placed.away.visible, false);
  assert.equal(placed.away.reason, 'off-screen');
  if (placed.conn.visible) assert.equal(overlap(placed.conn, placed.selected), false);
});

test('leaders end on the label border nearest the anchor', () => {
  assert.deepEqual(leaderEnd({ x: 100, y: 50, w: 40, h: 20 }, 90, 100), [100, 70]);
  assert.deepEqual(leaderEnd({ x: 100, y: 50, w: 40, h: 20 }, 120, 30), [120, 50]);
});
