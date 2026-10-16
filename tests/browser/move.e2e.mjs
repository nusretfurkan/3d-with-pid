// Browser check of per-pane Coordinates and direct equipment movement through the real app,
// real service and real CAD builds. Fresh sample copy + own service on a free port; never touches
// demo files or services 8765–8770. Read-only hooks only (window.skidWorkspace.snapshot /
// viewerInfo); every change goes through the visible controls or the gizmo, and network failures
// are simulated by intercepting this page's own requests.
//
// Usage: node tests/browser/move.e2e.mjs [step-filter-regex]
// Output: artifacts/ui-review/move-<stamp>/{*.png, report.json}; exit code 1 on any failure.

import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const dir = path.join(REVIEW_DIR, `move-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const projectPath = path.join(dir, 'project.json');
const report = { started_at: new Date().toISOString(), dir: path.relative(ROOT, dir), steps: [] };
const only = process.argv[2] ? new RegExp(process.argv[2]) : null;

const sample = spawnSync(PY, ['-m', 'skid.cli', 'sample', projectPath], { cwd: ROOT, encoding: 'utf8' });
if (sample.status !== 0) throw new Error(`sample failed: ${sample.stderr}`);
const service = spawn(PY, ['-m', 'skid.interactive', '--project', projectPath, '--port', '0', '--quiet'], { cwd: ROOT });
const serviceLog = fs.createWriteStream(path.join(dir, 'service.log'));
service.stdout.pipe(serviceLog);
service.stderr.pipe(serviceLog);
const url = await new Promise((resolve, reject) => {
  let out = '';
  const timer = setTimeout(() => reject(new Error(`service did not start: ${out}`)), 60000);
  service.stdout.on('data', (c) => { out += c; const m = /at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(out); if (m) { clearTimeout(timer); resolve(m[1]); } });
  service.on('exit', (code) => reject(new Error(`service exited ${code}: ${out}`)));
});
report.url = url;

const api = async (p, body) => {
  const r = await fetch(new URL(p, url), { method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  return r.json();
};
const fileHash = () => crypto.createHash('sha256').update(fs.readFileSync(projectPath)).digest('hex');
const near = (a, b, eps = 1e-6) => a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) <= eps);

const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const problems = watch(page);
const expectedProblems = [];
async function expecting(pattern, fn) {
  const n = problems.length;
  try {
    return await fn();
  } finally {
    await new Promise((r) => setTimeout(r, 300));
    for (const p of problems.splice(n)) (pattern.test(p) ? expectedProblems : problems).push(p);
  }
}
const NETWORK = /ERR_CONNECTION_REFUSED|ERR_CONNECTION_RESET|ERR_FAILED|ERR_EMPTY_RESPONSE|Failed to load resource/;
const posts = []; // every POST the page sends: {path, body}
page.on('request', (req) => {
  if (req.method() !== 'POST') return;
  const u = new URL(req.url());
  let body = null;
  try { body = req.postDataJSON(); } catch { /* none */ }
  posts.push({ path: u.pathname, body, at: Date.now() });
});
const commandsSince = (n) => posts.slice(n).filter((p) => p.path === '/api/command');
const mutationsSince = (n) => posts.slice(n).filter((p) => p.path !== '/api/selection');

const snap = () => page.evaluate(() => window.skidWorkspace.snapshot());
const info = (pane) => page.evaluate((i) => window.skidWorkspace.viewerInfo(i), pane);
const frame = () => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(r)))));
const shot = (name) => page.screenshot({ path: path.join(dir, `${name}.png`) });
const ROOTS = ['#view-3d', '#view-sheet', '#view-p2', '#view-p3'];
const field = (pane, axis) => page.locator(`${ROOTS[pane]} .move-panel input[data-axis="${axis}"]`);
const panelButton = (pane, act) => page.locator(`${ROOTS[pane]} .move-panel button[data-act="${act}"]`);

async function step(name, fn) {
  if (only && !only.test(name)) return;
  const t0 = Date.now();
  try {
    const detail = await fn();
    report.steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
    console.log(`ok   ${name}`);
  } catch (error) {
    report.steps.push({ name, ok: false, ms: Date.now() - t0, error: String(error && error.stack ? error.stack : error) });
    console.log(`FAIL ${name}\n     ${String(error && error.message ? error.message : error).slice(0, 700)}`);
    await shot(`failed-${report.steps.length}`).catch(() => {});
    await page.mouse.up().catch(() => {});
  }
}

/** The service and pane 0 show a complete, synchronized final scene of revision rev (or the newest). */
async function complete(rev = null, timeout = 240000) {
  return waitFor(async () => {
    const s = await snap();
    const p0 = s.panes[0];
    const ok = s.sync === 'synchronized' && (rev === null || s.revision === rev) && p0.installed && p0.installed.revision === s.revision
      && p0.installed.kind === 'output' && !s.equipmentMove.record;
    return ok ? s : null;
  }, { timeout, interval: 250, message: `complete synchronized r${rev ?? 'newest'}` });
}

/** Final scene of the given revision installed in pane 0 (complete or pending), no move record left. */
async function regenerated(rev, timeout = 240000) {
  return waitFor(async () => {
    const s = await snap();
    const p0 = s.panes[0];
    return s.revision === rev && p0.installed && p0.installed.revision === rev && p0.installed.kind === 'output' && !s.equipmentMove.record ? s : null;
  }, { timeout, interval: 100, message: `regenerated final r${rev} installed` });
}

/**
 * Select one item by clicking it like a user: a painted point of its P&ID occurrence (pane 1), else
 * its 3D label in a Live pane. Waits until it is the shared selection.
 */
async function selectOnSheet(id) {
  const point = await page.evaluate((sid) => {
    const hits = (x, y) => { const el = document.elementFromPoint(x, y); const owner = el && el.closest('[data-semantic-id]'); return !!owner && owner.getAttribute('data-semantic-id') === sid; };
    const host = document.querySelector('#pane-pid');
    if (host) {
      const hr = host.getBoundingClientRect();
      for (const el of host.querySelectorAll(`[data-semantic-id="${sid}"]`)) {
        if (el.getAttribute('data-occurrence-kind') === 'port-anchor') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2 || r.left < hr.left || r.right > hr.right || r.top < hr.top + 40 || r.bottom > hr.bottom) continue;
        for (let i = 1; i < 12; i += 1) for (let j = 1; j < 12; j += 1) {
          const x = r.left + (r.width * i) / 12;
          const y = r.top + (r.height * j) / 12;
          if (hits(x, y)) return { x, y, via: 'pid' };
        }
      }
    }
    for (const root of ['#view-3d', '#view-p2']) {
      const label = document.querySelector(`${root} .lbl[data-semantic-id="${sid}"]`);
      if (!label || label.style.visibility !== 'visible') continue;
      const r = label.getBoundingClientRect();
      if (hits(r.left + r.width / 2, r.top + r.height / 2)) return { x: r.left + r.width / 2, y: r.top + r.height / 2, via: 'label' };
    }
    return null;
  }, id);
  assert.ok(point, `no clickable occurrence or label of ${id}`);
  await page.mouse.click(point.x, point.y);
  return waitFor(async () => { const s = await snap(); return s.selection.length === 1 && s.selection[0] === id ? s : null; }, { message: `selection ${id} (clicked ${point.via})` });
}

async function setMove(pane, on) {
  const button = page.locator(`#p${pane}-move`);
  if ((await button.getAttribute('aria-pressed')) !== String(on)) await button.click();
  await frame();
}

/** Visible, hittable gizmo handle of a pane plus the gizmo origin (client px). */
async function handle(pane, which) {
  const m = (await info(pane)).move;
  const h = m.handles.find((x) => x.handle === which);
  if (!h || !h.visible || !h.hittable) throw new Error(`handle ${which} not hittable in pane ${pane}: ${JSON.stringify(m.handles)}`);
  return { h, origin: m.gizmoOrigin };
}

/** The pane's Iso view button (a user's way to bring the whole scene, and its gizmo, into view). */
async function iso(pane) {
  await page.locator(`${ROOTS[pane]} .view-head button[data-view="iso"]`).click();
  await new Promise((r) => setTimeout(r, 600)); // camera tween
  await frame();
}

/** Press a handle and drag it px along its screen direction (or dir), in many small pointer moves. */
async function drag(pane, which, px, { steps = 18, release = true, dir = null } = {}) {
  const { h, origin } = await handle(pane, which);
  let [ux, uy] = [h.x - origin.x, h.y - origin.y];
  const len = Math.hypot(ux, uy) || 1;
  [ux, uy] = dir || [ux / len, uy / len];
  await page.mouse.move(h.x, h.y);
  await frame();
  await page.mouse.down();
  await page.mouse.move(h.x + ux * px, h.y + uy * px, { steps });
  await frame();
  if (release) {
    await page.mouse.up();
    await frame();
  }
}

/** Number of components of one semantic owner in the scene file a pane has installed. */
async function ownerCount(pane, id) {
  const url = (await snap()).panes[pane].installed.url;
  const scene = await api(url);
  return scene.components.filter((c) => c.semantic_id === id).length;
}

/** Owner components (mesh and edges) offset by delta, all others at zero. */
function checkOffsets(move, entityId, delta, count) {
  const owned = move.components.filter((c) => c.semanticId === entityId);
  assert.equal(owned.length, count, `owner components listed (${owned.length} of ${count})`);
  for (const c of move.components) assert.ok(near(c.offset, c.semanticId === entityId ? delta : [0, 0, 0]), `${c.id} offset ${c.offset} vs ${delta}`);
  for (const e of move.edges) {
    const c = move.components.find((x) => x.id === e.id);
    assert.ok(near(e.offset, c.semanticId === entityId ? delta : [0, 0, 0]), `${e.id} edge offset`);
  }
}

/** Reroute every pending route through the assistant's focus-accessible Change actions. */
async function rerouteAll() {
  const n = posts.length;
  await page.evaluate(() => {
    const b = document.getElementById('btn-ask');
    if (b.getAttribute('aria-expanded') !== 'true') b.click();
  });
  await page.locator('#asst-tab-change').click();
  for (let guard = 0; guard < 12; guard += 1) {
    const button = page.locator('.asst-routes .reroute:not([disabled])').first();
    if (!(await button.count())) {
      const routes = (await api('/api/state')).project.physical.routes;
      if (!Object.values(routes).some((r) => r.status !== 'current')) break;
      await new Promise((r) => setTimeout(r, 300));
      continue;
    }
    await button.click();
    await waitFor(async () => !(await page.locator('.asst-routes .reroute.busy').count()), { timeout: 60000, message: 'reroute finished' });
  }
  const s = await complete();
  await page.locator('#assistant .asst-close').click();
  return { reroutes: posts.slice(n).filter((p) => p.path === '/api/reroute').length, revision: s.revision };
}

try {
  let s0;
  await page.goto(url);
  await step('fresh sample: complete r0 synchronized in the real app', async () => {
    s0 = await complete(0);
    return { revision: s0.revision, build: s0.build.status, sync: s0.sync };
  });

  await step('four panes: pane 2 duplicate Live 3D, pane 3 Published 3D; tools off by default', async () => {
    await page.locator('#layout-seg button[data-layout="4"]').click();
    await page.locator('#view-p2 .pane-tabs button[data-doc="3d"]').click();
    await page.locator('#view-p3 .pane-tabs button[data-doc="3d"]').click();
    await page.locator('#view-p3 .source-plate button[data-mode="published"]').click();
    const s = await waitFor(async () => { const x = await snap(); return [0, 2, 3].every((i) => x.panes[i].installed && x.panes[i].navigation) ? x : null; }, { message: 'four panes installed', timeout: 60000 });
    for (const i of [0, 2, 3]) {
      assert.equal(await page.locator(`#p${i}-coords`).getAttribute('aria-pressed'), 'false', `pane ${i} coordinates off`);
      assert.equal(await page.locator(`#p${i}-move`).getAttribute('aria-pressed'), 'false', `pane ${i} move off`);
      assert.equal(s.panes[i].navigation.coordinatesOn, false);
      assert.equal(s.panes[i].tools.move, false);
      assert.equal(s.panes[i].move.gizmo.attached, false);
      assert.equal(s.panes[i].movePanel.hidden, true);
    }
    assert.equal(s.panes[3].installed.origin, 'published');
    return { installed: s.panes.map((p) => p.installed && `${p.installed.kind} r${p.installed.revision} ${p.installed.origin}`) };
  });

  await step('coordinates per pane: independent, restored through type, layout and focus; zero writes', async () => {
    const n = posts.length;
    const hash = fileHash();
    const before = await api('/api/state');
    await page.locator('#p0-coords').click();
    await frame();
    let s = await snap();
    assert.equal(await page.locator('#p0-coords').getAttribute('aria-pressed'), 'true');
    assert.equal(s.panes[0].navigation.coordinatesOn, true);
    assert.equal(s.panes[2].navigation.coordinatesOn, false, 'the duplicate 3D pane is independent');
    const c0 = (await info(0)).coordinates;
    assert.equal(c0.on, true);
    assert.deepEqual(c0.axisLabels.map((l) => l.text), ['+X', '+Y', '+Z', '0']);
    assert.equal((await info(2)).coordinates.on, false);
    // type switch away and back restores the pane's own choice
    await page.locator('#view-3d .pane-tabs button[data-doc="legend"]').click();
    await page.locator('#view-3d .pane-tabs button[data-doc="3d"]').click();
    s = await waitFor(async () => { const x = await snap(); return x.panes[0].navigation && x.panes[0].installed ? x : null; }, { message: 'pane 0 3D again' });
    assert.equal(s.panes[0].navigation.coordinatesOn, true, 'restored after a type switch');
    assert.equal(await page.locator('#p0-coords').getAttribute('aria-pressed'), 'true');
    // layout 4 -> 2 -> 4 and focus keep both panes' choices (no remount)
    const gen = s.panes.map((p) => p.generation);
    await page.locator('#layout-seg button[data-layout="2"]').click();
    await page.locator('#layout-seg button[data-layout="4"]').click();
    await page.locator('#btn-focus').click();
    s = await snap();
    assert.equal(s.layout.focus, true);
    assert.equal(s.panes[0].navigation.coordinatesOn, true);
    assert.equal(s.panes[2].navigation.coordinatesOn, false);
    assert.deepEqual(s.panes.map((p) => p.generation), gen, 'no pane remounted');
    await page.locator('#btn-exit-focus').click();
    await page.locator('#p2-coords').click();
    await page.locator('#p2-coords').click();
    await frame();
    const after = await api('/api/state');
    assert.deepEqual(mutationsSince(n), [], 'no POST from coordinates, type, layout or focus');
    assert.equal(fileHash(), hash, 'project file unchanged');
    assert.equal(after.project.revision, before.project.revision);
    assert.equal(after.build.id, before.build.id, 'no build');
    await shot('01-coordinates-pane0');
    return { readout: c0.readoutText };
  });

  // ---------------------------------------------------------------- typed move of the rotated HV-102
  const VALVE = 'ent-discharge-valve';
  let valveTyped = null;
  await step('typed X/Y/Z of rotated HV-102: live preview mirrored in the duplicate Live pane only; shortcuts not taken while typing', async () => {
    const st = await api('/api/state');
    const base = st.project.physical.placements[VALVE];
    assert.deepEqual(base.rotation_deg.map(Math.round), [0, 0, -90], 'already rotated');
    await selectOnSheet(VALVE);
    await setMove(0, true);
    await setMove(2, true);
    let s = await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok && x.panes[0].move.gizmo.attached ? x : null; }, { message: 'HV-102 eligible in pane 0' });
    assert.equal(s.panes[0].movePanel.hidden, false);
    assert.deepEqual(s.panes[0].movePanel.values, base.position_m.map(String), 'fields show the exact current origin');
    assert.match(s.panes[0].movePanel.caption, /^Current origin · model r0 · matches this view$/);
    const cam = s.panes[0].navigation.camera;
    const labels = s.panes[0].navigation.labelMode;
    const edges = s.panes[0].navigation.edgesOn;
    const n = posts.length;
    valveTyped = [Number((base.position_m[0] + 0.4).toFixed(6)), Number((base.position_m[1] - 0.25).toFixed(6)), base.position_m[2]];
    await field(0, 'x').selectText();
    await page.keyboard.type(String(valveTyped[0]));
    await field(0, 'y').selectText();
    await page.keyboard.type(`${valveTyped[1]}`);
    await frame();
    s = await snap();
    const rec = s.equipmentMove.record;
    assert.ok(rec, 'typing began the move record');
    assert.equal(s.equipmentMove.origin, 'numeric');
    assert.equal(rec.phase, 'preview');
    assert.equal(rec.entityId, VALVE);
    assert.deepEqual(rec.rotation, base.rotation_deg);
    const delta = valveTyped.map((v, i) => v - base.position_m[i]);
    assert.ok(near(s.panes[0].move.preview.delta, delta, 1e-9), `pane 0 preview ${s.panes[0].move.preview.delta}`);
    assert.ok(near(s.panes[2].move.preview.delta, delta, 1e-9), 'duplicate Live pane mirrors');
    assert.equal(s.panes[2].move.preview.origin, 'mirror');
    assert.equal(s.panes[3].move.preview, null, 'Published pane keeps its own source');
    assert.equal(s.panes[2].movePanel.apply, false);
    assert.match(s.panes[2].movePanel.status.join(' '), /in progress in pane 1/);
    assert.equal(s.panes[0].move.gizmo.attached, false, 'gizmo detached while the typed preview owns the record');
    const owner = (await info(0)).move;
    checkOffsets(owner, VALVE, delta, await ownerCount(0, VALVE));
    assert.ok(owner.ghosts > 0, 'base position shown');
    assert.ok(owner.ports.every((p) => near(p.preview, p.base.map((v, i) => v + delta[i]), 1e-9)), 'ports carried');
    // characters that are view shortcuts elsewhere (e, l, f, 1, m) only edit the field; every
    // prefix of this entry is invalid, so the last valid preview must stay as it is
    await field(0, 'z').selectText();
    await page.keyboard.type('elf1m');
    s = await snap();
    assert.deepEqual(s.panes[0].navigation.camera, cam, 'no view shortcut moved the camera');
    assert.equal(s.panes[0].navigation.labelMode, labels);
    assert.equal(s.panes[0].navigation.edgesOn, edges);
    assert.equal(s.layout.maximized, null);
    assert.deepEqual(s.panes[0].movePanel.invalid, [false, false, true], 'invalid Z is marked, not clamped');
    assert.equal(s.panes[0].movePanel.apply, false);
    assert.ok(near(s.panes[0].move.preview.delta, delta, 1e-9), 'the last valid preview stays');
    await page.keyboard.press('Enter'); // refused: nothing is sent for an invalid entry
    await field(0, 'z').selectText();
    await page.keyboard.type(String(valveTyped[2]));
    s = await snap();
    assert.deepEqual(s.panes[0].movePanel.invalid, [false, false, false]);
    assert.equal(commandsSince(n).length, 0, 'typing sends nothing');
    await shot('02-typed-preview-mirrored');
    return { base: base.position_m, typed: valveTyped, delta, ghosts: owner.ghosts };
  });

  await step('Apply: one move_equipment; accepted preview kept on the r0 scene (progressive r1 not installed) until final r1, no doubled transform', async () => {
    const before = await api('/api/state');
    const oldScene = await api(before.artifacts.display);
    const n = posts.length;
    const installs0 = (await snap()).panes[0].loaderStats.installs;
    // read-only sampler: what pane 0 shows while the service regenerates
    await page.evaluate(() => {
      window.__moveSamples = [];
      const tick = () => {
        const s = window.skidWorkspace.snapshot();
        const v = window.skidWorkspace.viewerInfo(0);
        window.__moveSamples.push({
          t: performance.now(), phase: s.equipmentMove.record && s.equipmentMove.record.phase, revision: s.revision,
          installed: s.panes[0].installed && { revision: s.panes[0].installed.revision, kind: s.panes[0].installed.kind, url: s.panes[0].installed.url },
          hold: s.panes[0].moveHold, badge: s.panes[0].badge, note: document.getElementById('note-3d').hidden ? null : document.getElementById('note-3d').textContent,
          preview: s.panes[0].move.preview, offsets: v.move.components.filter((c) => c.semanticId === 'ent-discharge-valve').map((c) => c.offset),
          ghosts: v.ghosts, sync: s.sync, build: s.build && s.build.status,
        });
        if (window.__moveSamples.length < 4000) window.__moveSampler = setTimeout(tick, 25);
      };
      tick();
    });
    await panelButton(0, 'apply').click();
    const s1 = await regenerated(1);
    await page.evaluate(() => clearTimeout(window.__moveSampler));
    const samples = await page.evaluate(() => window.__moveSamples);
    const cmds = commandsSince(n);
    assert.equal(cmds.length, 1, 'exactly one command');
    const cmd = cmds[0].body;
    assert.equal(cmd.type, 'move_equipment');
    assert.equal(cmd.expected_revision, 0);
    assert.deepEqual(cmd.payload.placement.position_m, valveTyped, 'typed values sent exactly');
    const after = await api('/api/state');
    const pl = after.project.physical.placements[VALVE];
    assert.deepEqual(pl.rotation_deg, before.project.physical.placements[VALVE].rotation_deg, 'rotation preserved');
    assert.deepEqual(pl.position_m, valveTyped);
    assert.equal(pl.manual, true);
    assert.equal(after.project.revision, 1);
    assert.equal(after.history.undo, before.history.undo + 1, 'one undo step');
    assert.deepEqual(after.project.pid, before.project.pid, 'P&ID layout untouched');
    for (const k of ['entities', 'ports', 'connections']) assert.deepEqual(after.project.model[k], before.project.model[k], `model ${k} unchanged`);
    assert.equal(after.project.id, before.project.id);
    const added = Object.keys(after.project.model.assumptions).filter((k) => !(k in before.project.model.assumptions));
    assert.deepEqual(added, [cmd.payload.placement.assumption_id], 'one new manual-layout assumption');
    const incident = Object.entries(after.project.physical.routes).filter(([, r]) => r.status !== 'current');
    assert.ok(incident.length > 0, 'incident routes explicitly pending');
    for (const [id, r] of incident) {
      assert.equal(r.status, 'needs_reroute');
      assert.equal(JSON.stringify(r.points_m), JSON.stringify(before.project.physical.routes[id].points_m), `${id} points retained`);
    }
    // what pane 0 showed meanwhile
    const typedDelta = valveTyped.map((v, i) => v - before.project.physical.placements[VALVE].position_m[i]);
    const held = samples.filter((x) => x.hold);
    const violations = [];
    for (const x of samples) {
      if (x.installed && x.installed.revision === 1 && x.installed.kind !== 'output') violations.push(`progressive r1 installed at ${x.t}`);
      if (x.phase && x.installed && x.installed.revision === 0 && !(x.preview && near(x.preview.delta, typedDelta, 1e-9))) violations.push(`r0 shown during ${x.phase} without the preview at ${x.t}`);
      if (x.installed && x.installed.revision === 0 && x.offsets.some((o) => !near(o, typedDelta, 1e-9)) && x.phase) violations.push(`owner part back at base at ${x.t}`);
      if (x.installed && x.installed.revision === 1 && x.offsets.some((o) => !near(o, [0, 0, 0]))) violations.push(`regenerated part offset again at ${x.t}`);
      if (x.hold && !(x.note || '').includes('kept for the move')) violations.push(`held without the note at ${x.t}`);
      if (x.hold && /^Current/.test(x.badge)) violations.push(`badge claims current while holding r0: ${x.badge}`);
    }
    assert.deepEqual(violations.slice(0, 5), [], 'display stayed honest');
    assert.ok(samples.some((x) => x.phase === 'accepted'), 'accepted phase observed');
    // regenerated geometry is at base + delta, drawn with zero offset
    const p0 = s1.panes[0];
    assert.equal(p0.loaderStats.installs - installs0, 1, 'exactly one install in pane 0: the final r1 scene');
    assert.equal(p0.move.preview, null);
    const v = (await info(0)).move;
    assert.ok(v.components.every((c) => near(c.offset, [0, 0, 0])), 'no transform on regenerated geometry');
    assert.equal(v.ghosts, 0);
    const newScene = await api(after.artifacts.display);
    let compared = 0;
    for (const c of oldScene.components.filter((x) => x.semantic_id === VALVE)) {
      const nb = newScene.components.find((x) => x.id === c.id).bounds_m;
      assert.ok(near(nb.min, c.bounds_m.min.map((x, i) => x + typedDelta[i]), 2e-5) && near(nb.max, c.bounds_m.max.map((x, i) => x + typedDelta[i]), 2e-5), `${c.id} regenerated at base + delta`);
      compared += 1;
    }
    assert.equal(s1.panes[2].installed.revision, 1, 'the duplicate pane installed r1 too');
    assert.equal(s1.panes[3].installed.revision, 0, 'Published stays the last complete r0');
    assert.equal(after.build.status, 'pending');
    const p0Now = (await snap()).panes[0];
    assert.equal(p0Now.move.eligible.ok, false, 'no new move on a pending scene');
    assert.ok(p0Now.movePanel.reroute, 'the panel offers Reroute for the pending incident routes');
    // the Published pane still shows r0: its origin readouts name the current model and the older view
    await page.locator('#p3-coords').click();
    await setMove(3, true);
    await frame();
    const p3 = (await snap()).panes[3];
    assert.equal(p3.move.eligible.reason, 'published');
    assert.equal(p3.movePanel.caption, 'Current origin in model r1 · this view shows r0');
    assert.deepEqual(p3.movePanel.values, valveTyped.map(String), 'current model values, labelled as such');
    const r3 = (await info(3)).coordinates.readout;
    assert.match(r3.lines.join(' | '), /Current model r1 · this view shows r0/);
    await shot('03-after-typed-move');
    await setMove(3, false);
    await page.locator('#p3-coords').click();
    return {
      command: cmd.id, pending: incident.map(([id]) => id), samples: samples.length, heldSamples: held.length,
      heldNotes: [...new Set(held.map((x) => x.note))], heldBadges: [...new Set(held.map((x) => x.badge))], partsCompared: compared,
      phases: [...new Set(samples.map((x) => x.phase))], installedSeen: [...new Set(samples.map((x) => x.installed && `${x.installed.kind} r${x.installed.revision}`))],
    };
  });

  await step('focus: assistant Reroute (existing transport) brings outputs back to complete; Move available again', async () => {
    await page.locator('#btn-focus').click();
    try {
      const r = await rerouteAll();
      await complete();
      assert.ok(r.reroutes >= 1);
      const s = await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok && x.panes[0].move.gizmo.attached ? x : null; }, { message: 'Move available again on the complete scene' });
      assert.equal(s.panes[0].movePanel.apply, false, 'nothing typed yet: Apply off for the unchanged origin');
      assert.match(s.panes[0].movePanel.caption, new RegExp(`^Current origin · model r${s.revision} · matches this view$`));
      return { ...r, eligible: s.panes[0].move.eligible };
    } finally {
      await page.evaluate(() => { if (document.body.classList.contains('focus-mode')) document.getElementById('btn-exit-focus').click(); });
    }
  });

  // ---------------------------------------------------------------- pointer moves
  const PUMP = 'ent-pump';
  await step('pointer X drag of P-101 (snap 0.05): many moves, one command; duplicate mirrors; regenerated without double offset', async () => {
    await selectOnSheet(PUMP);
    const st = await api('/api/state');
    const base = st.project.physical.placements[PUMP].position_m;
    await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok && x.panes[0].move.gizmo.attached ? x : null; }, { message: 'P-101 eligible' });
    await iso(0);
    const count = (await info(0)).move.components.filter((c) => c.semanticId === PUMP).length || null;
    const n = posts.length;
    const rev = st.project.revision;
    await drag(0, 'X', 150, { release: false, steps: 24 });
    let s = await snap();
    const rec = s.equipmentMove.record;
    assert.equal(rec.phase, 'preview');
    assert.equal(s.equipmentMove.origin, 'pointer');
    const delta = s.panes[0].move.preview.delta;
    assert.ok(Math.abs(delta[0]) > 0.04 && delta[1] === 0 && delta[2] === 0, `X only: ${delta}`);
    assert.ok(Math.abs(delta[0] / 0.05 - Math.round(delta[0] / 0.05)) < 1e-6, `snapped to 0.05: ${delta[0]}`);
    assert.ok(near(s.panes[2].move.preview.delta, delta, 1e-9), 'duplicate Live pane mirrors the drag');
    assert.equal(s.panes[3].move.preview, null);
    assert.ok(near(s.panes[0].movePanel.values.map(Number), base.map((v, i) => v + delta[i]), 1e-9), 'the panel follows the drag');
    const move = (await info(0)).move;
    checkOffsets(move, PUMP, delta, await ownerCount(0, PUMP));
    assert.equal(move.orbitEnabled, false, 'orbit suspended during the drag');
    await shot('04-pointer-x-drag');
    await page.mouse.up();
    s = await regenerated(rev + 1);
    const cmds = commandsSince(n);
    assert.equal(cmds.length, 1, 'many pointer moves, one command');
    assert.equal(cmds[0].body.type, 'move_equipment');
    assert.ok(near(cmds[0].body.payload.placement.position_m, base.map((v, i) => v + delta[i]), 1e-9));
    const v = (await info(0)).move;
    assert.ok(v.components.every((c) => near(c.offset, [0, 0, 0])), 'no doubled transform after rebuild');
    assert.equal(v.orbitEnabled, true);
    const after = await api('/api/state');
    assert.equal(after.history.undo, st.history.undo + 1);
    assert.deepEqual(after.project.physical.placements[PUMP].rotation_deg, st.project.physical.placements[PUMP].rotation_deg);
    return { delta, count, command: cmds[0].body.id };
  });

  await step('reroute again to complete (assistant, normal layout)', async () => rerouteAll());

  await step('pointer XY plane drag of rotated HV-102, snap off: planar delta, one command, rotation and Z kept', async () => {
    await selectOnSheet(VALVE);
    await page.locator(`${ROOTS[0]} .move-panel button[data-snap="off"]`).click();
    await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok && x.panes[0].move.gizmo.attached && x.panes[0].move.snap === null ? x : null; }, { message: 'HV-102 eligible, snap off' });
    await iso(0);
    const st = await api('/api/state');
    const base = st.project.physical.placements[VALVE];
    const n = posts.length;
    await drag(0, 'XY', 83, { release: false, dir: [0.83, -0.56], steps: 20 });
    const s = await snap();
    const delta = s.panes[0].move.preview.delta;
    assert.equal(s.equipmentMove.record.phase, 'preview');
    assert.ok(Math.hypot(delta[0], delta[1]) > 0.02 && delta[2] === 0, `planar XY: ${delta}`);
    assert.ok(near(s.panes[2].move.preview.delta, delta, 1e-9), 'duplicate mirrors');
    const v = (await info(0)).move;
    checkOffsets(v, VALVE, delta, await ownerCount(0, VALVE));
    const unsnapped = delta.slice(0, 2).some((d) => Math.abs(d / 0.05 - Math.round(d / 0.05)) > 1e-3);
    await shot('05-pointer-xy-drag');
    await page.mouse.up();
    await regenerated(st.project.revision + 1);
    const cmds = commandsSince(n);
    assert.equal(cmds.length, 1, 'one command');
    const after = await api('/api/state');
    const pl = after.project.physical.placements[VALVE];
    assert.deepEqual(pl.rotation_deg, base.rotation_deg, 'rotation preserved');
    assert.equal(pl.position_m[2], base.position_m[2], 'Z unchanged by an XY plane drag');
    assert.ok(near(pl.position_m, base.position_m.map((x, i) => x + delta[i]), 1e-9));
    assert.ok((await info(0)).move.components.every((c) => near(c.offset, [0, 0, 0])), 'no doubled transform');
    await page.locator(`${ROOTS[0]} .move-panel button[data-snap="0.05"]`).click();
    return { delta, unsnapped, snap: s.panes[0].move.snap };
  });

  await step('reroute after the XY move', async () => rerouteAll());

  await step('Escape mid-drag (Z axis): preview cleared everywhere, zero commands, orbit back', async () => {
    await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok && x.panes[0].move.gizmo.attached ? x : null; }, { message: 'eligible' });
    await iso(0);
    const n = posts.length;
    await drag(0, 'Z', 60, { release: false });
    let s = await snap();
    assert.equal(s.equipmentMove.record.phase, 'preview');
    const delta = s.panes[0].move.preview.delta;
    assert.ok(delta[0] === 0 && delta[1] === 0 && Math.abs(delta[2]) >= 0.05, `Z only: ${delta}`);
    await page.keyboard.press('Escape');
    await frame();
    s = await snap();
    assert.equal(s.equipmentMove.record, null);
    assert.equal(s.equipmentMove.outcome.reason, 'escape');
    assert.equal(s.panes[0].move.preview, null);
    assert.equal(s.panes[2].move.preview, null, 'mirror cleared');
    assert.ok((await info(0)).move.components.every((c) => near(c.offset, [0, 0, 0])));
    await page.mouse.up();
    await frame();
    assert.equal((await info(0)).move.orbitEnabled, true);
    assert.equal(commandsSince(n).length, 0);
    return { delta };
  });

  await step('pointercancel and a no-op release send nothing', async () => {
    const n = posts.length;
    await iso(0);
    await drag(0, 'Y', 70, { release: false });
    assert.equal((await snap()).equipmentMove.record.phase, 'preview');
    await page.evaluate(() => document.querySelector('#view-3d canvas.gl').dispatchEvent(new PointerEvent('pointercancel', { pointerId: 1, pointerType: 'mouse', bubbles: true })));
    await page.mouse.up();
    await frame();
    let s = await snap();
    assert.equal(s.equipmentMove.record, null);
    assert.equal(s.equipmentMove.outcome.reason, 'pointer-cancel');
    const { h } = await handle(0, 'X');
    await page.mouse.move(h.x, h.y);
    await frame();
    await page.mouse.down();
    await page.mouse.up();
    await frame();
    s = await snap();
    assert.equal(s.equipmentMove.record, null);
    assert.equal(commandsSince(n).length, 0);
    return { last: s.equipmentMove.outcome };
  });

  // ---------------------------------------------------------------- cancellation of typed previews
  async function typedPreview(dx = 0.3) {
    await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok ? x : null; }, { message: 'eligible for a typed preview' });
    const base = (await snap()).panes[0].movePanel.values.map(Number);
    await field(0, 'x').selectText();
    await page.keyboard.type(String(Number((base[0] + dx).toFixed(6))));
    const s = await snap();
    assert.equal(s.equipmentMove.record && s.equipmentMove.record.phase, 'preview');
    return base;
  }

  await step('typed preview ends on Published switch (same revision), pane hidden, type switch; Escape in the field cancels first', async () => {
    const n = posts.length;
    const reasons = [];
    await typedPreview();
    await page.locator('#view-3d .source-plate button[data-mode="published"]').click();
    let s = await snap();
    assert.equal(s.equipmentMove.record, null);
    reasons.push(s.equipmentMove.outcome.reason);
    await page.locator('#view-3d .source-plate button[data-mode="live"]').click();
    await waitFor(async () => { const x = await snap(); return x.panes[0].installed && x.panes[0].installed.origin !== 'published' || x.panes[0].mode === 'live' ? x : null; });
    await typedPreview();
    await page.locator('#view-sheet .vh-tools button[data-act="max"]').click(); // maximize pane 1: pane 0 hidden
    s = await snap();
    assert.equal(s.equipmentMove.record, null);
    reasons.push(s.equipmentMove.outcome.reason);
    await page.keyboard.press('Escape'); // restore the panes
    await waitFor(async () => { const x = await snap(); return x.layout.maximized === null && x.panes[0].active ? x : null; });
    await typedPreview();
    await page.locator('#view-3d .pane-tabs button[data-doc="description"]').click();
    s = await snap();
    assert.equal(s.equipmentMove.record, null);
    reasons.push(s.equipmentMove.outcome.reason);
    await page.locator('#view-3d .pane-tabs button[data-doc="3d"]').click();
    await waitFor(async () => { const x = await snap(); return x.panes[0].navigation && x.panes[0].installed ? x : null; });
    await setMove(0, true);
    await typedPreview();
    await field(0, 'y').focus();
    await page.keyboard.press('Escape');
    s = await snap();
    assert.equal(s.equipmentMove.record, null, 'Escape while typing cancels the preview');
    reasons.push(s.equipmentMove.outcome.reason);
    assert.equal(s.layout.focus, false);
    assert.equal(s.panes[0].movePanel.dirty, false);
    const origin = (await api('/api/state')).project.physical.placements[VALVE].position_m;
    assert.deepEqual(s.panes[0].movePanel.values, origin.map(String), 'fields back on the exact current origin');
    assert.equal(commandsSince(n).length, 0);
    assert.deepEqual(reasons, ['published', 'hidden', 'destroyed', 'escape']);
    return { reasons };
  });

  await step('assistant: closing it keeps the move; Escape order is move, then assistant', async () => {
    const n = posts.length;
    await page.locator('#btn-ask').click();
    await typedPreview(-0.2);
    // typing in the pane's Move panel is an outside click that already closes the floating bubble
    if ((await snap()).assistant.open) await page.locator('#assistant .asst-close').click();
    let s = await snap();
    assert.equal(s.assistant.open, false);
    assert.equal(s.equipmentMove.record && s.equipmentMove.record.phase, 'preview', 'closing the assistant keeps the move');
    assert.equal(s.panes[0].generation, s.panes[0].generation);
    await page.locator('#btn-ask').click();
    await page.locator('#asst-input').focus();
    await page.keyboard.press('Escape');
    s = await snap();
    assert.equal(s.equipmentMove.record, null, 'first Escape cancels the move');
    assert.equal(s.assistant.open, true, 'the assistant stays open');
    await page.keyboard.press('Escape');
    s = await snap();
    assert.equal(s.assistant.open, false, 'second Escape closes the assistant');
    assert.equal(commandsSince(n).length, 0);
  });

  await step('focus: typed preview, Apply and Cancel reachable; Escape cancels the move before leaving focus', async () => {
    const n = posts.length;
    await page.locator('#btn-focus').click();
    try {
      assert.equal((await snap()).layout.focus, true);
      await typedPreview(0.2);
      let s = await snap();
      assert.equal(s.panes[0].movePanel.apply, true);
      assert.equal(s.panes[0].movePanel.cancel, true);
      await panelButton(0, 'cancel').click();
      s = await snap();
      assert.equal(s.equipmentMove.record, null);
      assert.equal(s.equipmentMove.outcome.reason, 'cancel-button');
      await typedPreview(0.2);
      await field(0, 'x').focus();
      await page.keyboard.press('Escape');
      s = await snap();
      assert.equal(s.equipmentMove.record, null);
      assert.equal(s.layout.focus, true, 'the first Escape cancels the move; focus stays');
      await page.keyboard.press('Escape');
      s = await snap();
      assert.equal(s.layout.focus, false, 'the second Escape leaves focus');
      assert.equal(commandsSince(n).length, 0);
    } finally {
      await page.evaluate(() => { if (document.body.classList.contains('focus-mode')) document.getElementById('btn-exit-focus').click(); });
    }
  });

  await step('typed preview ends when the service is unreachable, and on a stale base (external change)', async () => {
    const n = posts.length;
    await typedPreview();
    await expecting(NETWORK, async () => {
      await page.route('**/api/state', (route) => route.abort('connectionrefused'));
      const s = await waitFor(async () => { const x = await snap(); return x.online === false ? x : null; }, { timeout: 15000, message: 'offline' });
      assert.equal(s.equipmentMove.record, null);
      assert.equal(s.equipmentMove.outcome.reason, 'offline');
      assert.equal(s.panes[0].move.preview, null);
      await page.unroute('**/api/state');
      await waitFor(async () => { const x = await snap(); return x.online ? x : null; }, { timeout: 20000, message: 'online again' });
    });
    await typedPreview();
    const typed = (await snap()).panes[0].movePanel.values;
    const st = await api('/api/state');
    const ext = await api('/api/command', { id: `cmd-ext-${Date.now()}`, expected_revision: st.project.revision, type: 'rename_entity', payload: { entity_id: 'ent-tank', tag: 'T-901' } });
    assert.equal(ext.project.revision, st.project.revision + 1);
    let s = await waitFor(async () => { const x = await snap(); return x.revision === st.project.revision + 1 ? x : null; }, { message: 'external change seen' });
    assert.equal(s.equipmentMove.record, null);
    assert.equal(s.equipmentMove.outcome.reason, 'stale');
    assert.deepEqual(s.panes[0].movePanel.draft, typed, 'the typed values survive the stale cancel');
    assert.equal(commandsSince(n).length, 0, 'the page sent no command');
    await complete();
    s = await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok ? x : null; }, { message: 'eligible again' });
    await new Promise((r) => setTimeout(r, 1600)); // more state polls
    s = await snap();
    assert.deepEqual(s.panes[0].movePanel.draft, typed, 'kept across polls');
    assert.deepEqual(s.panes[0].movePanel.values, typed, 'shown again in the fields once Move is available');
    assert.equal(s.panes[0].movePanel.apply, true, 'Apply sends them only on request');
    assert.equal(s.equipmentMove.record, null, 'no preview restarted by itself');
    return { external: ext.last_command, keptDraft: typed };
  });

  // ---------------------------------------------------------------- uncertain outcomes
  await step('answer lost after the service applied it: uncertain, reconciled as accepted from state, never resent', async () => {
    const st = await api('/api/state');
    const n = posts.length;
    let forwarded = null;
    await expecting(NETWORK, async () => {
      await page.route('**/api/command', async (route) => {
        const response = await route.fetch();
        forwarded = { status: response.status(), body: await response.json() };
        await route.abort('connectionreset');
      }, { times: 1 });
      await typedPreview(0.15);
      await panelButton(0, 'apply').click();
      const s = await waitFor(async () => { const x = await snap(); return x.equipmentMove.outcome && x.equipmentMove.outcome.kind === 'accepted' && x.equipmentMove.outcome.how ? x : null; }, { timeout: 30000, message: 'reconciled as accepted' });
      assert.equal(s.equipmentMove.outcome.how, 'reconciled');
    });
    assert.equal(forwarded.status, 200, 'the service applied it');
    assert.equal(commandsSince(n).length, 1, 'no automatic resend');
    const s = await regenerated(st.project.revision + 1);
    const after = await api('/api/state');
    assert.equal(after.project.revision, st.project.revision + 1, 'exactly one revision');
    return { forwarded: forwarded.body.last_command, outcome: s.equipmentMove.outcome };
  });

  await step('reroute again to complete', async () => rerouteAll());

  /** Base-revision states with no trace of a command, observed by the page for at least ms. */
  async function noTraceFor(ms, revision, commandId) {
    const t0 = Date.now();
    const seen = [];
    while (Date.now() - t0 < ms) {
      const x = await snap();
      seen.push({ revision: x.revision, phase: x.equipmentMove.record && x.equipmentMove.record.phase, outcome: x.equipmentMove.outcome && x.equipmentMove.outcome.kind });
      assert.equal(x.revision, revision, 'the service is still at the base revision');
      assert.equal(x.equipmentMove.record && x.equipmentMove.record.phase, 'uncertain', 'still unconfirmed');
      await new Promise((r) => setTimeout(r, 400));
    }
    const s = await api('/api/state');
    assert.ok(!s.history.recent.some((e) => e.command_id === commandId), 'no trace on the service either');
    return seen;
  }

  await step('held command, lost answer: fresh base states keep it unconfirmed and blocking; delivered later, it is confirmed once', async () => {
    const st = await api('/api/state');
    const n = posts.length;
    let held = null;
    await expecting(NETWORK, async () => {
      // the page loses the answer while the request has not reached the service (held)
      await page.route('**/api/command', async (route) => { held = route.request().postData(); await route.abort('connectionreset'); }, { times: 1 });
      await typedPreview(-0.15);
      await panelButton(0, 'apply').click();
      await waitFor(async () => { const x = await snap(); return x.equipmentMove.record && x.equipmentMove.record.phase === 'uncertain' && x.online ? x : null; }, { timeout: 20000, message: 'unconfirmed and online again' });
    });
    const commandId = JSON.parse(held).id;
    const seen = await noTraceFor(4000, st.project.revision, commandId); // ~3 fresh polls at the base revision
    let s = await snap();
    assert.equal(s.equipmentMove.outcome.kind, 'uncertain');
    assert.match(s.equipmentMove.outcome.text, /^Unconfirmed: .* It may still be applied; nothing is resent\./);
    assert.doesNotMatch(s.equipmentMove.outcome.text, /not applied|move again/i);
    assert.equal(s.panes[0].movePanel.dismiss, true, 'Hide preview offered');
    assert.equal(s.panes[0].movePanel.apply, false);
    assert.equal(s.panes[0].move.preview && s.panes[0].move.preview.phase, 'uncertain', 'labelled preview kept');
    assert.equal(s.panes[2].move.preview && s.panes[2].move.preview.phase, 'uncertain', 'mirrored too');
    assert.equal(s.panes[2].movePanel.status.some((l) => /unconfirmed move in pane 1/.test(l)), true, 'other panes say why no new move starts');
    await shot('06-unconfirmed');
    // the held request is finally delivered to the service (same envelope, as a delayed request would be)
    const delivered = await (await fetch(new URL('/api/command', url), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: held })).json();
    assert.equal(delivered.last_command.command_id, commandId);
    s = await waitFor(async () => { const x = await snap(); return x.equipmentMove.outcome && x.equipmentMove.outcome.kind === 'accepted' ? x : null; }, { timeout: 20000, message: 'confirmed' });
    assert.equal(s.equipmentMove.outcome.how, 'reconciled');
    await regenerated(st.project.revision + 1);
    assert.equal(commandsSince(n).length, 1, 'the page sent it once; nothing resent');
    const after = await api('/api/state');
    assert.equal(after.project.revision, st.project.revision + 1, 'one revision');
    assert.equal(after.history.recent.filter((e) => e.command_id === commandId).length, 1, 'applied once');
    return { commandId, polledWhileUnconfirmed: seen.length };
  });

  await step('reroute after the delivered move', async () => rerouteAll());

  await step('request dropped before the service: unconfirmed until Hide preview; then watched, nothing resent or applied', async () => {
    const st = await api('/api/state');
    const n = posts.length;
    let dropped = null;
    await expecting(NETWORK, async () => {
      await page.route('**/api/command', (route) => { dropped = route.request().postData(); return route.abort('connectionreset'); }, { times: 1 });
      await typedPreview(0.1);
      await panelButton(0, 'apply').click();
      await waitFor(async () => { const x = await snap(); return x.equipmentMove.record && x.equipmentMove.record.phase === 'uncertain' && x.online ? x : null; }, { timeout: 20000, message: 'unconfirmed and online again' });
    });
    const commandId = JSON.parse(dropped).id;
    await noTraceFor(3000, st.project.revision, commandId);
    await panelButton(0, 'dismiss').click();
    let s = await snap();
    assert.equal(s.equipmentMove.record, null, 'preview hidden');
    assert.equal(s.panes[0].move.preview, null);
    assert.equal(s.panes[2].move.preview, null);
    assert.equal(s.equipmentMove.outcome.kind, 'unconfirmed');
    assert.deepEqual(s.equipmentMove.unresolved.map((u) => u.commandId), [commandId], 'still watched');
    assert.match(s.panes[0].movePanel.status.join(' '), /still unconfirmed: it may yet be applied/);
    // Move is available again from freshly verified state (new moves would be new commands)
    s = await waitFor(async () => { const x = await snap(); return x.panes[0].move.eligible.ok ? x : null; }, { message: 'eligible again' });
    await new Promise((r) => setTimeout(r, 2000));
    s = await snap();
    assert.deepEqual(s.equipmentMove.unresolved.map((u) => u.commandId), [commandId], 'still watched after more polls');
    assert.equal(commandsSince(n).length, 1, 'no automatic resend');
    const after = await api('/api/state');
    assert.equal(after.project.revision, st.project.revision, 'nothing applied');
    assert.deepEqual(after.project.physical.placements[VALVE], st.project.physical.placements[VALVE]);
    return { outcome: s.equipmentMove.outcome, unresolved: s.equipmentMove.unresolved };
  });

  // ---------------------------------------------------------------- fit
  await step('fit: 900/1280/1600 × two/four panes × assistant open, normal and focus, with Move and Coordinates on', async () => {
    const results = [];
    await selectOnSheet(VALVE);
    await setMove(0, true);
    if ((await page.locator('#p0-coords').getAttribute('aria-pressed')) !== 'true') await page.locator('#p0-coords').click();
    if (await page.locator('#p2-move').count()) await setMove(2, true);
    for (const width of [1600, 1280, 900]) {
      await page.setViewportSize({ width, height: width === 900 ? 760 : 900 });
      for (const panes of [2, 4]) {
        await page.evaluate((p) => document.querySelector(`#layout-seg button[data-layout="${p}"]`).click(), panes);
        for (const focus of [false, true]) {
          if (focus) await page.evaluate(() => document.getElementById('btn-focus').click());
          await page.evaluate(() => { const b = document.getElementById('btn-ask'); if (b.getAttribute('aria-expanded') !== 'true') b.click(); });
          await frame();
          await new Promise((r) => setTimeout(r, 250));
          const g = await page.evaluate(() => {
            const out = { overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth, clipped: [], overlaps: [], wide: [] };
            const vw = innerWidth;
            const vh = innerHeight;
            if (out.overflowX) { // what is wider than the viewport (the closed fixed inspector drawer is off-screen by design)
              out.scroll = [document.documentElement.scrollWidth, document.documentElement.clientWidth];
              for (const el of document.querySelectorAll('body *')) {
                if (!el.getClientRects().length || el.closest('#inspector')) continue;
                const r = el.getBoundingClientRect();
                if (r.right > document.documentElement.clientWidth + 0.5) out.wide.push(`${el.tagName.toLowerCase()}#${el.id}.${String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className).slice(0, 30)} ${Math.round(r.left)}-${Math.round(r.right)}`);
              }
              out.wide = out.wide.slice(0, 12);
            }
            const asst = document.getElementById('assistant').getBoundingClientRect();
            const overlay = getComputedStyle(document.getElementById('assistant')).position === 'fixed';
            for (const pane of document.querySelectorAll('.pane')) {
              if (!pane.getClientRects().length) continue;
              const box = pane.getBoundingClientRect();
              for (const el of pane.querySelectorAll('.view-head button, .move-panel button, .move-panel input, .source-plate .badge, .source-plate button')) {
                if (!el.getClientRects().length) continue;
                const r = el.getBoundingClientRect();
                if (!r.width) continue;
                if (r.left < box.left - 0.5 || r.right > box.right + 0.5 || r.top < box.top - 0.5 || r.bottom > box.bottom + 0.5 || r.right > vw + 0.5 || r.bottom > vh + 0.5) {
                  out.clipped.push(`${pane.id}: ${el.id || el.dataset.act || el.dataset.axis || el.dataset.doc || el.textContent.trim().slice(0, 16)}`);
                }
              }
              const panel = pane.querySelector('.move-panel:not([hidden])');
              const body = pane.querySelector('.view-body').getBoundingClientRect();
              if (panel) { // the panel and its controls lie in the pane body (not under its header), clear of the source plate
                for (const el of [panel, ...panel.querySelectorAll('button, input')]) {
                  if (!el.getClientRects().length) continue; // not rendered (e.g. Reroute with nothing pending)
                  const r = el.getBoundingClientRect();
                  if (el !== panel && (r.top < panel.getBoundingClientRect().top - 0.5 || r.bottom > panel.getBoundingClientRect().bottom + 0.5)) out.clipped.push(`${pane.id}: ${el.dataset.act || el.dataset.axis || el.dataset.snap} scrolled out of the panel`);
                  if (r.top < body.top - 0.5 || r.bottom > body.bottom + 0.5 || r.left < body.left - 0.5 || r.right > body.right + 0.5) out.clipped.push(`${pane.id}: ${el === panel ? 'move panel' : el.dataset.act || el.dataset.axis || el.dataset.snap} outside the pane body`);
                }
                const plate = pane.querySelector('.source-plate').getBoundingClientRect();
                const a = panel.getBoundingClientRect();
                if (a.right > plate.left && a.left < plate.right && a.bottom > plate.top && a.top < plate.bottom) out.overlaps.push(`${pane.id}: panel/source plate`);
                out.panels = (out.panels || []).concat(`${pane.id} ${Math.round(a.width)}×${Math.round(a.height)}${panel.classList.contains('compact') ? ' compact' : ''} in body ${Math.round(body.height)}`);
              }
              const readout = pane.querySelector('.coord-readout:not([hidden])');
              if (panel && readout && readout.getClientRects().length && getComputedStyle(readout).display !== 'none') {
                const a = panel.getBoundingClientRect();
                const b = readout.getBoundingClientRect();
                if (a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom) out.overlaps.push(`${pane.id}: panel/readout`);
              }
              if (panel && !overlay && !document.getElementById('assistant').hidden) {
                const a = panel.getBoundingClientRect();
                if (a.right > asst.left + 0.5 && a.left < asst.right) out.overlaps.push(`${pane.id}: panel under reserved assistant`);
              }
            }
            return out;
          });
          const label = `${width}-${panes}p-${focus ? 'focus' : 'normal'}`;
          await page.screenshot({ path: path.join(dir, `fit-${label}.png`) });
          results.push({ label, ...g });
          if (focus) await page.evaluate(() => document.getElementById('btn-exit-focus').click());
        }
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    assert.ok(results.every((r) => r.panels && r.panels.length), 'a Move panel was measured in every layout');
    const bad = results.filter((r) => r.overflowX || r.clipped.length || r.overlaps.length);
    assert.deepEqual(bad, [], 'no horizontal overflow, no clipped controls, Move panel and readout apart');
    return results;
  });

  await step('no page errors or console errors', async () => {
    assert.deepEqual(problems, []);
    return { expected: expectedProblems.length };
  });
} finally {
  report.finished_at = new Date().toISOString();
  report.passed = report.steps.filter((s) => s.ok).length;
  report.failed = report.steps.filter((s) => !s.ok).length;
  report.problems = problems;
  report.expectedProblems = expectedProblems;
  report.posts = posts.map((p) => ({ path: p.path, type: p.body && p.body.type, id: p.body && p.body.id }));
  fs.writeFileSync(path.join(dir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(`${report.passed} passed, ${report.failed} failed · ${path.relative(ROOT, dir)}`);
  await browser.close().catch(() => {});
  service.kill('SIGTERM');
  if (report.failed) process.exitCode = 1;
}
