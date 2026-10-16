// Read-only contract check of the UI's pure logic against a running service (no browser).
// Usage: node tests/browser/live-contract.mjs [http://127.0.0.1:8765]
// Only GET requests are made. It checks that the state fields and artifact identities the
// UI depends on are present, that the badges derived from the live state are consistent
// with the service's sync verdict, and that the exact scene diff reuses an identical scene.

import assert from 'node:assert/strict';
import { offeredSources, slotBadge } from '../../web/js/view-source.js';
import { syncBadge, viewSource, sourceBadge } from '../../web/js/status.js';
import { planSceneUpdate, appearanceKey, isFinalScene } from '../../web/js/scene-diff.js';

const base = (process.argv[2] || 'http://127.0.0.1:8765').replace(/\/$/, '');
const get = async (path, kind = 'json') => {
  const r = await fetch(base + path);
  assert.equal(r.status, 200, `${path} -> HTTP ${r.status}`);
  return kind === 'json' ? r.json() : r.text();
};
const results = [];
const check = async (name, fn) => {
  try {
    await fn();
    results.push(['ok', name]);
  } catch (error) {
    results.push(['FAIL', `${name}: ${error.message}`]);
  }
};

const state = await get('/api/state');
const rev = state.project.revision;

await check('state carries the fields the UI reads', () => {
  for (const key of ['project', 'diagnostics', 'selection', 'build', 'published', 'artifacts', 'history', 'artifact_sources', 'sync', 'document', 'service']) {
    assert.ok(key in state, key);
  }
  assert.ok('notes' in state.document && 'readable' in state.document.notes, 'document.notes.readable');
  if (state.build) for (const key of ['id', 'status', 'last_sequence', 'events', 'previews', 'artifacts', 'artifact_status', 'integrity']) assert.ok(key in state.build, `build.${key}`);
});

await check('per-view badges agree with the service sync verdict', () => {
  const sync = syncBadge(state);
  for (const key of ['display', 'pid', 'legend', 'index', 'description']) {
    const src = viewSource(state, key, 'live');
    if (!src) continue;
    const installed = { url: src.url, src };
    const b = slotBadge({ desired: installed, installed, pending: null, error: null }, state);
    if (b.tone === 'current') assert.equal(state.sync.state, 'synchronized', `${key} current only when synchronized`);
    if (state.sync.state === 'synchronized') assert.equal(b.tone, 'current', `${key} should be current when synchronized`);
    assert.equal(b.tone, sourceBadge(offeredSources(state).get(src.url), rev, state.published).tone);
  }
  assert.equal(sync.synchronized, state.sync.state === 'synchronized' && state.sync.published_current === true);
});

await check('every offered artifact URL resolves and carries its offered identity', async () => {
  for (const [url, src] of offeredSources(state)) {
    if (/\.step$/.test(url)) continue;
    if (/\.json$/.test(url)) {
      const data = await get(url);
      const id = data.identity || data;
      if ('source_revision' in id) {
        assert.equal(id.source_revision, src.source_revision, url);
        assert.equal(id.input_digest, src.input_digest, url);
      }
    } else if (/\.svg$/.test(url)) {
      const text = await get(url, 'text');
      assert.match(text, new RegExp(`data-source-revision="${src.source_revision}"`), url);
      assert.match(text, new RegExp(`data-input-digest="${src.input_digest}"`), url);
    } else if (/\.html$/.test(url)) {
      const text = await get(url, 'text');
      assert.match(text, new RegExp(`<body[^>]*data-source-revision="${src.source_revision}"`), url);
    }
  }
});

await check('an identical real scene is fully reused by the exact diff', async () => {
  const src = viewSource(state, 'display', 'live');
  assert.ok(src, 'a display source is offered');
  const scene = await get(src.url);
  assert.equal(scene.units, 'm');
  assert.equal(scene.up_axis, 'Z');
  const again = JSON.parse(JSON.stringify(scene));
  const installed = new Map(scene.components.map((c) => [c.id, { geometry: { positions: c.positions, normals: c.normals, indices: c.indices }, appearance: appearanceKey(c) }]));
  const t0 = performance.now();
  const plan = planSceneUpdate(installed, again.components, isFinalScene(again));
  const ms = performance.now() - t0;
  assert.equal(plan.keep.length, scene.components.length);
  assert.equal(plan.invalid.length, 0, JSON.stringify(plan.invalid.slice(0, 3)));
  results.push(['info', `exact diff of ${scene.components.length} components / ${scene.triangle_count} triangles took ${ms.toFixed(1)} ms`]);
});

await check('selection resolution uses null for unknown and [] only for known absence', () => {
  for (const item of state.selection.resolved || []) {
    const dc = item.display_components;
    assert.ok(dc === null || Array.isArray(dc), `${item.id} display_components`);
    if (Array.isArray(dc) && dc.length === 0) assert.ok(item.absent_3d_reason, `${item.id} known absence has a reason`);
    if (dc === null) assert.equal(item.display_build_id, null);
  }
});

for (const [status, line] of results) console.log(`${status.padEnd(4)} ${line}`);
console.log(`revision ${rev} · sync ${state.sync.state} · build ${state.build ? `${state.build.id} ${state.build.status}/${state.build.integrity}` : 'none'}`);
process.exitCode = results.some(([s]) => s === 'FAIL') ? 1 : 0;
