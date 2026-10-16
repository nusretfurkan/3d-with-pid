// Pure rules of the floating Ask launcher and its bubble: stored position, clamping, drag
// threshold, keyboard placement and where the bubble goes beside the launcher.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_POSITION, LAUNCHER_MARGIN, LAUNCHER_STORAGE_KEY, fromNormalized, isDrag, nudge, placeBubble, readStoredPosition,
  toNormalized, writeStoredPosition,
} from '../../../web/js/floating-assistant.js';

const SIZE = { width: 80, height: 42 };
const inside = (pos, vp, size = SIZE) => pos.left >= 0 && pos.top >= 0 && pos.left + size.width <= vp.width && pos.top + size.height <= vp.height;

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: (k) => { delete data[k]; }, data };
}
const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('denied'); } };

test('the default position is the bottom-right corner, inside the margins', () => {
  const vp = { width: 1600, height: 1000 };
  const p = fromNormalized(DEFAULT_POSITION, SIZE, vp);
  assert.deepEqual(p, { left: 1600 - 80 - LAUNCHER_MARGIN, top: 1000 - 42 - LAUNCHER_MARGIN });
  assert.deepEqual(fromNormalized(null, SIZE, vp), p, 'no stored position gives the default');
  assert.deepEqual(fromNormalized({ x: 'a', y: 2 }, SIZE, vp), p, 'an invalid position gives the default');
});

test('a normalized position follows a resize and is clamped back into a smaller window', () => {
  const big = { width: 1600, height: 1000 };
  const norm = toNormalized({ left: 400, top: 300 }, SIZE, big);
  assert.deepEqual(fromNormalized(norm, SIZE, big), { left: 400, top: 300 }, 'round trip');
  for (const vp of [{ width: 900, height: 760 }, { width: 1280, height: 800 }, { width: 120, height: 60 }]) {
    const p = fromNormalized(norm, SIZE, vp);
    assert.ok(inside(p, vp) || vp.width < SIZE.width + 2 * LAUNCHER_MARGIN, `${JSON.stringify(p)} in ${JSON.stringify(vp)}`);
    assert.ok(p.left >= LAUNCHER_MARGIN && p.top >= LAUNCHER_MARGIN);
  }
  // back to the original size: the original pixel position returns (nothing was lost to clamping)
  assert.deepEqual(fromNormalized(norm, SIZE, big), { left: 400, top: 300 });
  // a position dragged past an edge is stored as that edge
  assert.deepEqual(toNormalized({ left: -500, top: 5000 }, SIZE, big), { x: 0, y: 1 });
});

test('stored positions: read and write never throw; anything invalid reads as none', () => {
  const s = memoryStorage();
  assert.equal(readStoredPosition(s), null);
  assert.equal(writeStoredPosition(s, { x: 0.25, y: 0.5 }), true);
  assert.deepEqual(readStoredPosition(s), { x: 0.25, y: 0.5 });
  assert.deepEqual(JSON.parse(s.data[LAUNCHER_STORAGE_KEY]), { x: 0.25, y: 0.5 });
  for (const raw of ['{', '[]', '{"x":2,"y":0}', '{"x":0.1}', 'null', '{"x":"0.1","y":0.2}']) {
    assert.equal(readStoredPosition(memoryStorage({ [LAUNCHER_STORAGE_KEY]: raw })), null, raw);
  }
  assert.equal(readStoredPosition(broken), null);
  assert.equal(writeStoredPosition(broken, { x: 0.1, y: 0.1 }), false, 'a refusing storage reports false');
  assert.equal(writeStoredPosition(s, { x: Number.NaN, y: 0 }), false, 'never stores an invalid position');
  assert.equal(readStoredPosition(null), null);
});

test('a press that moves less than the threshold is a click; more is a drag', () => {
  assert.equal(isDrag(0, 0), false);
  assert.equal(isDrag(3, 3), false);
  assert.equal(isDrag(4, 3), true);
  assert.equal(isDrag(-6, 0), true);
});

test('Shift+Arrow placement moves by one step; other keys do nothing', () => {
  const p = { left: 100, top: 200 };
  assert.deepEqual(nudge(p, 'ArrowLeft', 24), { left: 76, top: 200 });
  assert.deepEqual(nudge(p, 'ArrowDown', 24), { left: 100, top: 224 });
  assert.equal(nudge(p, 'Enter'), null);
  assert.equal(nudge(p, 'a'), null);
});

test('the bubble opens above a bottom-right launcher, right-aligned, bounded and clear of it', () => {
  const vp = { width: 1600, height: 1000 };
  const anchor = { left: 1504, right: 1584, top: 942, bottom: 984 };
  const b = placeBubble(anchor, vp);
  assert.equal(b.side, 'above');
  assert.equal(b.width, 680);
  assert.equal(b.left + b.width, anchor.right, 'right edges line up');
  assert.equal(b.top, null);
  assert.ok(vp.height - b.bottom < anchor.top, 'its bottom is above the launcher');
  assert.ok(vp.height - b.bottom - b.maxHeight >= 12, 'its top stays inside the margin');
});

test('the bubble opens below a top-left launcher, left-aligned', () => {
  const vp = { width: 1280, height: 800 };
  const anchor = { left: 16, right: 96, top: 16, bottom: 58 };
  const b = placeBubble(anchor, vp);
  assert.equal(b.side, 'below');
  assert.equal(b.left, 16);
  assert.ok(b.top > anchor.bottom);
  assert.ok(b.top + b.maxHeight <= vp.height - 12);
});

test('narrow and short windows: the bubble is clamped inside the viewport', () => {
  const narrow = placeBubble({ left: 400, right: 480, top: 700, bottom: 742 }, { width: 500, height: 760 });
  assert.equal(narrow.width, 476, 'viewport minus both margins');
  assert.equal(narrow.left, 12);
  const short = placeBubble({ left: 200, right: 280, top: 180, bottom: 222 }, { width: 900, height: 420 });
  assert.equal(short.side, 'over', 'neither side has room');
  assert.equal(short.top, 12);
  assert.equal(short.maxHeight, 396);
  // a launcher dragged to mid-height uses the larger side
  const mid = placeBubble({ left: 600, right: 680, top: 500, bottom: 542 }, { width: 1600, height: 900 });
  assert.equal(mid.side, 'above');
  const midLow = placeBubble({ left: 600, right: 680, top: 300, bottom: 342 }, { width: 1600, height: 900 });
  assert.equal(midLow.side, 'below');
});
