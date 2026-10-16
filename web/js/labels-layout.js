// Greedy screen-space label placement with collision culling (pure; unit-tested in Node).
//
// items: [{id, ax, ay, w, h, priority, depth}] with the anchor (ax, ay) in CSS pixels.
// Higher priority places first; among equals the nearer item (smaller depth) wins.
// Each label tries a fixed set of leader directions; a label that cannot be placed
// without overlapping an earlier label, an anchor dot or the viewport edge is hidden.

const CANDIDATES = [
  (a, w, h, L) => [a.ax + 10, a.ay - L - h],      // up-right
  (a, w, h, L) => [a.ax - 10 - w, a.ay - L - h],  // up-left
  (a, w, h, L) => [a.ax + L, a.ay - h / 2],       // right
  (a, w, h, L) => [a.ax - L - w, a.ay - h / 2],   // left
  (a, w, h, L) => [a.ax + 10, a.ay + L],          // down-right
  (a, w, h, L) => [a.ax - 10 - w, a.ay + L],      // down-left
  (a, w, h, L) => [a.ax + 10, a.ay - 1.9 * L - h],// far up-right
  (a, w, h, L) => [a.ax - 10 - w, a.ay - 1.9 * L - h],
];

function overlaps(r, s, gap) {
  return r.x < s.x + s.w + gap && s.x < r.x + r.w + gap && r.y < s.y + s.h + gap && s.y < r.y + r.h + gap;
}

/** Point on the rectangle border nearest to (px, py): the leader's label end. */
export function leaderEnd(rect, px, py) {
  const x = Math.min(Math.max(px, rect.x), rect.x + rect.w);
  const y = Math.min(Math.max(py, rect.y), rect.y + rect.h);
  if (x > rect.x && x < rect.x + rect.w && y > rect.y && y < rect.y + rect.h) return [rect.x, rect.y + rect.h / 2];
  return [x, y];
}

export function layoutLabels(items, viewport, options = {}) {
  const gap = options.gap ?? 4;
  const leader = options.leader ?? 26;
  const margin = options.margin ?? 4;
  const dot = options.dot ?? 5;
  const order = items
    .map((item, index) => ({ item, index }))
    .sort((p, q) => (q.item.priority - p.item.priority) || (p.item.depth - q.item.depth) || (p.index - q.index));
  const placed = [];
  const anchors = items
    .filter((i) => i.onScreen !== false)
    .map((i) => ({ id: i.id, x: i.ax - dot / 2, y: i.ay - dot / 2, w: dot, h: dot }));
  const result = new Map();
  for (const { item } of order) {
    if (item.onScreen === false) {
      result.set(item.id, { id: item.id, visible: false, reason: 'off-screen' });
      continue;
    }
    let chosen = null;
    for (const candidate of CANDIDATES) {
      const [x, y] = candidate(item, item.w, item.h, leader);
      const rect = { x, y, w: item.w, h: item.h };
      if (x < margin || y < margin || x + item.w > viewport.w - margin || y + item.h > viewport.h - margin) continue;
      if (placed.some((r) => overlaps(rect, r, gap))) continue;
      if (anchors.some((r) => r.id !== item.id && overlaps(rect, r, 1))) continue;
      chosen = rect;
      break;
    }
    if (!chosen) {
      result.set(item.id, { id: item.id, visible: false, reason: 'collision' });
      continue;
    }
    placed.push(chosen);
    const [lx, ly] = leaderEnd(chosen, item.ax, item.ay);
    result.set(item.id, { id: item.id, visible: true, x: chosen.x, y: chosen.y, w: chosen.w, h: chosen.h, lx, ly, ax: item.ax, ay: item.ay });
  }
  return items.map((i) => result.get(i.id));
}
