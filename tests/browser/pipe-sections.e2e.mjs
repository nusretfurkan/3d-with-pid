// Browser check of straight pipe-section selection and movement through the real app, the real
// service from this source tree and real CAD builds. Fresh sample copy + own service on a free port;
// never touches demo files or the main service. Read-only hooks only (window.skidWorkspace.snapshot /
// viewerInfo / screenPointOfWorld); every edit goes through the visible controls or the gizmo, except
// the setup route and the "someone else changed the model" step, which use the service's validated
// command API like another client would. Network failures are simulated on this page's own requests.
// No Ask or planner request is made (asserted).
//
// Usage: node tests/browser/pipe-sections.e2e.mjs [step-filter-regex]
// Output: artifacts/ui-review/pipe-sections-<stamp>/{*.png, report.json}; exit code 1 on any failure.

import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const dir = path.join(REVIEW_DIR, `pipe-sections-${stamp}`);
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
  return { status: r.status, body: await r.json() };
};
const state = async () => (await api('/api/state')).body;
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
const posts = [];
page.on('request', (req) => {
  if (req.method() !== 'POST') return;
  let body = null;
  try { body = req.postDataJSON(); } catch { /* none */ }
  posts.push({ path: new URL(req.url()).pathname, body, at: Date.now() });
});
const commandsSince = (n) => posts.slice(n).filter((p) => p.path === '/api/command');
const mutationsSince = (n) => posts.slice(n).filter((p) => p.path !== '/api/selection');

const snap = () => page.evaluate(() => window.skidWorkspace.snapshot());
const info = (pane) => page.evaluate((i) => window.skidWorkspace.viewerInfo(i), pane);
const toScreen = (point, pane = 0) => page.evaluate(([p, i]) => window.skidWorkspace.screenPointOfWorld(p, i), [point, pane]);
const frame = () => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(r)))));
const shot = (name) => page.screenshot({ path: path.join(dir, `${name}.png`) });
const ROOTS = ['#view-3d', '#view-sheet', '#view-p2', '#view-p3'];
const panel = (pane) => page.locator(`${ROOTS[pane]} .section-panel`);
const panelButton = (pane, act) => page.locator(`${ROOTS[pane]} .section-panel button[data-act="${act}"]`);
const offsetField = (pane) => page.locator(`${ROOTS[pane]} .section-panel input.sp-offset`);

async function step(name, fn) {
  if (only && !only.test(name)) return;
  const t0 = Date.now();
  try {
    const detail = await fn();
    report.steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
    console.log(`ok   ${name}`);
  } catch (error) {
    report.steps.push({ name, ok: false, ms: Date.now() - t0, error: String(error && error.stack ? error.stack : error) });
    console.log(`FAIL ${name}\n     ${String(error && error.message ? error.message : error).slice(0, 900)}`);
    await shot(`failed-${report.steps.length}`).catch(() => {});
    await page.mouse.up().catch(() => {});
  }
}

/** Pane 0 shows a complete synchronized final scene of revision rev (or the newest), no edit record left. */
async function complete(rev = null, timeout = 240000) {
  return waitFor(async () => {
    const s = await snap();
    const p0 = s.panes[0];
    const ok = s.sync === 'synchronized' && (rev === null || s.revision === rev) && p0.installed && p0.installed.revision === s.revision
      && p0.installed.kind === 'output' && !s.equipmentMove.record && !(s.pipeMove.record && s.pipeMove.record.phase !== 'preview');
    return ok ? s : null;
  }, { timeout, interval: 250, message: `complete synchronized r${rev ?? 'newest'}` });
}

async function setMove(pane, on) {
  const button = page.locator(`#p${pane}-move`);
  if ((await button.getAttribute('aria-pressed')) !== String(on)) await button.click();
  await frame();
}

async function iso(pane) {
  await page.locator(`${ROOTS[pane]} .view-head button[data-view="iso"]`).click();
  await new Promise((r) => setTimeout(r, 600));
  await frame();
}

/** Click a world point of a pane (a point on a pipe's centreline projects onto its visible surface). */
async function clickWorld(point, pane = 0) {
  const p = await toScreen(point, pane);
  assert.ok(p, `point ${point} not on screen`);
  await page.mouse.click(p.x, p.y);
  await frame();
  return p;
}

/** The section panel's facts of a pane. */
async function sectionPanel(pane = 0) {
  return (await snap()).panes[pane].sectionPanel;
}

const LINE = 'conn-discharge-1'; // L-103: pump discharge nozzle -> check valve inlet
// Setup route (validated, body-screened set_route like any client): four straight legs, both nozzle frames kept.
const SETUP_POINTS = [[1.5, 0, 0.5], [1.5, 0, 1.0], [2.0, 0, 1.0], [2.0, 0, 0.7], [2.21, 0, 0.7]];
const RAISED = [[1.5, 0, 0.5], [1.5, 0, 1.15], [2.0, 0, 1.15], [2.0, 0, 0.7], [2.21, 0, 0.7]]; // section 2 +0.15 m Z
const ASK_PATHS = new Set(['/api/ask', '/api/propose', '/api/cancel-ask', '/api/cancel-proposal', '/api/apply-proposal']);

/** Type an offset for the selected section of pane 0 along axis (0|1|2) through the visible controls. */
async function typeOffset(axis, text, pane = 0) {
  await page.locator(`${ROOTS[pane]} .section-panel .sp-axis button[data-axis="${axis}"]`).click();
  await offsetField(pane).click();
  await offsetField(pane).fill('');
  await page.keyboard.type(text);
  await frame();
}

async function chooseSection(index, pane = 0) {
  await page.locator(`${ROOTS[pane]} .section-panel .sp-item[data-index="${index}"]`).click();
  return waitFor(async () => { const x = await snap(); return x.section && x.section.segmentIndex === index && x.panes[pane].pipe.eligible.ok ? x : null; }, { message: `section ${index + 1} selected and eligible` });
}

/** A visible, hittable section-gizmo handle and the gizmo origin (client px). */
async function pipeHandle(pane, which) {
  const v = await info(pane);
  const h = v.move.pipeHandles.find((x) => x.handle === which);
  if (!h || !h.visible || !h.hittable) throw new Error(`section handle ${which} not hittable: ${JSON.stringify(v.move.pipeHandles)}`);
  return { h, origin: await toScreen(v.move.pipe.gizmo.origin, pane) };
}

try {
  await page.goto(url);
  await step('fresh sample: complete r0; setup route of L-103 with four straight legs (validated set_route)', async () => {
    await complete(0);
    const st = await state();
    const before = st.project.physical.routes[LINE].points_m;
    assert.deepEqual(before[0], SETUP_POINTS[0]);
    assert.deepEqual(before[before.length - 1], SETUP_POINTS[SETUP_POINTS.length - 1]);
    const r = await api('/api/command', {
      id: `cmd-test-${crypto.randomUUID()}`, expected_revision: st.project.revision, type: 'set_route',
      payload: { connection_id: LINE, points_m: SETUP_POINTS, mode: 'manual', assumption_id: 'asm-test-setup-route' },
      assumptions: { 'asm-test-setup-route': { description: 'Browser test setup route for L-103 (four straight legs).', source: 'pipe-sections.e2e.mjs' } },
    });
    assert.equal(r.status, 200, JSON.stringify(r.body).slice(0, 400));
    const s = await complete(1);
    assert.equal(s.pipeCapable, true, 'this service advertises capabilities.pipe_segment_move');
    return { revision: s.revision, capabilities: s.capabilities };
  });

  await step('pick the pipe mesh on straight section 2: the line is the one semantic selection, only that section is highlighted; one aggregate pipe mesh', async () => {
    await iso(0);
    const n = posts.length;
    const p = await clickWorld([1.75, 0, 1.0]);
    const s = await waitFor(async () => { const x = await snap(); return x.selection.length === 1 && x.selection[0] === LINE && x.section ? x : null; }, { message: 'line and section selected' });
    assert.equal(s.section.connectionId, LINE);
    assert.equal(s.section.segmentIndex, 1);
    assert.ok(s.pidSelected.length > 0, 'the P&ID highlights the line');
    const v = await info(0);
    assert.ok(v.move.pipe.drawn && v.move.pipe.drawn.highlight, 'sleeve drawn');
    assert.ok(near(v.move.pipe.drawn.highlight.start, [1.575, 0, 1.0]), `sleeve start ${v.move.pipe.drawn.highlight.start}`);
    assert.ok(near(v.move.pipe.drawn.highlight.end, [1.925, 0, 1.0]), `sleeve end ${v.move.pipe.drawn.highlight.end}`);
    const scene = (await api(s.panes[0].installed.url)).body;
    assert.equal(scene.components.filter((c) => c.semantic_id === LINE && c.component_key === 'pipe').length, 1, 'one conn#pipe sweep');
    assert.equal(v.components, scene.components.length, 'no component added or split');
    assert.deepEqual(s.selection, [LINE], 'no section ID in the semantic selection');
    const insp = await page.locator('#insp-body').textContent();
    assert.match(insp, /L-103/, 'the inspector shows the line');
    assert.deepEqual(mutationsSince(n), [], 'selection only');
    await shot('01-section-picked');
    return { clicked: p, section: s.section };
  });

  await step('a pick on a bend or a flange selects the line but names no straight section', async () => {
    await clickWorld([2.0, 0, 1.0]);
    await waitFor(async () => { const x = await snap(); return x.selection[0] === LINE && !x.section ? x : null; }, { message: 'bend: no section' });
    assert.equal((await info(0)).move.pipe.drawn, null, 'nothing highlighted');
    const fp = await page.evaluate(() => window.skidWorkspace.screenPointOf('conn-discharge-1#flange/to', 0));
    assert.ok(fp, 'flange on screen');
    await page.mouse.click(fp.x, fp.y);
    await frame();
    const s = await snap();
    assert.equal(s.section, null, 'a flange is not a straight section');
    return { flange: fp };
  });

  await step('Move on: the section panel lists every straight section and replaces the equipment panel; the list selects short end sections', async () => {
    await clickWorld([1.75, 0, 1.0]);
    await setMove(0, true);
    let sp = await waitFor(async () => { const x = await sectionPanel(0); return x && !x.hidden && x.sections.length === 4 ? x : null; }, { message: 'section panel with four sections' });
    assert.equal((await snap()).panes[0].movePanel.hidden, true, 'the equipment Move panel steps aside');
    assert.equal(sp.selected, 1);
    await chooseSection(3);
    sp = await sectionPanel(0);
    assert.equal(sp.selected, 3);
    assert.deepEqual((await snap()).selection, [LINE]);
    await chooseSection(1);
    const ro = (await info(0)).coordinates.readoutText || '';
    assert.match(ro, /L-103 · section 2 of 4 · midpoint/, ro);
    assert.match(ro, /Section move: drag the Y\/Z handle or type an offset/, ro);
    assert.doesNotMatch(ro, /Move unavailable/);
    return { items: sp.sections.map((x) => x.text), readout: ro };
  });

  await step('the section gizmo offers only the axes perpendicular to the section (Y, Z for an X leg) at its midpoint', async () => {
    const s = await waitFor(async () => { const x = await snap(); return x.panes[0].pipe.gizmo.attached ? x : null; }, { message: 'section gizmo attached', timeout: 10000 });
    assert.deepEqual(s.panes[0].pipe.gizmo.shown, ['Y', 'Z']);
    assert.ok(near(s.panes[0].pipe.gizmo.origin, [1.75, 0, 1.0]), `gizmo origin ${s.panes[0].pipe.gizmo.origin}`);
    assert.deepEqual((await sectionPanel(0)).axes, ['Y', 'Z']);
    assert.equal(s.panes[0].move.gizmo.attached, false, 'no equipment gizmo');
    const v = await info(0);
    assert.deepEqual(v.move.pipeHandles.filter((h) => h.visible).map((h) => h.handle).sort(), ['Y', 'Z'], 'no X or plane handle');
    return { shown: s.panes[0].pipe.gizmo.shown };
  });

  await step('drag the Z handle: tentative preview follows (neighbours stretch, nozzles fixed), orbit does not move; release keeps a reviewable preview; nothing sent', async () => {
    const n = posts.length;
    const cam = (await snap()).panes[0].navigation.camera;
    const { h, origin } = await pipeHandle(0, 'Z');
    const [dx, dy] = [h.x - origin.x, h.y - origin.y];
    const len = Math.hypot(dx, dy);
    await page.mouse.move(h.x, h.y);
    await frame();
    await page.mouse.down();
    await page.mouse.move(h.x + (dx / len) * 50, h.y + (dy / len) * 50, { steps: 14 });
    await frame();
    let s = await snap();
    const rec = s.pipeMove.record;
    assert.ok(rec && rec.phase === 'preview', 'drag began a preview');
    assert.equal(s.panes[0].pipe.active.stage, 'drag');
    assert.ok(rec.offset[2] > 0 && rec.offset[0] === 0 && rec.offset[1] === 0, `one axis ${rec.offset}`);
    assert.ok(Array.isArray(rec.points), 'tentative points');
    assert.deepEqual(rec.points[0], SETUP_POINTS[0], 'start nozzle fixed');
    assert.deepEqual(rec.points[rec.points.length - 1], SETUP_POINTS[4], 'end nozzle fixed');
    assert.ok(Math.abs(rec.points[1][2] - (1.0 + rec.offset[2])) < 1e-9, 'the rising leg stretches');
    assert.deepEqual(s.panes[0].navigation.camera, cam, 'orbit did not move during the gizmo drag');
    await page.mouse.up();
    await frame();
    s = await snap();
    assert.equal(s.pipeMove.record.phase, 'preview', 'release keeps a reviewable preview');
    assert.equal(s.panes[0].pipe.active, null);
    const v = await info(0);
    assert.ok(v.move.pipe.drawn.preview && v.move.pipe.faded.includes(`${LINE}#pipe`), 'preview overlay drawn, installed pipe faded');
    const ro = v.coordinates.readoutText || '';
    assert.match(ro, /L-103 · section 2 of 4 · tentative midpoint/, ro);
    assert.match(ro, /m along Z · both nozzle ends fixed/, ro);
    assert.doesNotMatch(ro, /Move unavailable|World origin/, 'no equipment-only readout in section mode');
    const sp = await sectionPanel(0);
    assert.equal(sp.apply, true, 'Apply offered');
    assert.equal(sp.phase, 'preview');
    assert.deepEqual(commandsSince(n), [], 'no command from a drag');
    assert.deepEqual(s.panes[0].navigation.camera, cam);
    await shot('02-drag-preview');
    return { offset: rec.offset, points: rec.points };
  });

  await step('Escape cancels the unsent preview before closing Ask; a second Escape closes Ask; nothing sent', async () => {
    const n = posts.length;
    await page.locator('#btn-ask').click();
    await waitFor(async () => (await snap()).assistant?.open || (await page.locator('#btn-ask').getAttribute('aria-expanded')) === 'true', { message: 'Ask open' });
    await page.mouse.move(5, 500);
    await page.keyboard.press('Escape');
    await frame();
    let s = await snap();
    assert.equal(s.pipeMove.record, null, 'preview cancelled first');
    assert.equal(s.pipeMove.outcome.reason, 'escape');
    assert.equal(await page.locator('#btn-ask').getAttribute('aria-expanded'), 'true', 'Ask still open');
    await page.keyboard.press('Escape');
    await frame();
    assert.equal(await page.locator('#btn-ask').getAttribute('aria-expanded'), 'false', 'Ask closed by the second Escape');
    s = await snap();
    assert.equal(s.section && s.section.segmentIndex, 1, 'the section stays selected');
    const v = await info(0);
    assert.deepEqual(v.move.pipe.faded, [], 'installed pipe shown again');
    assert.deepEqual(commandsSince(n), []);
    return { outcome: s.pipeMove.outcome.text };
  });

  let accepted;
  await step('typed offset: preview, then Apply sends exactly one move_route_segment; one revision, undo step and complete build; the new scene replaces the preview once', async () => {
    const n = posts.length;
    const st = await state();
    const cam = (await snap()).panes[0].navigation.camera;
    await typeOffset(2, '0.15');
    let s = await snap();
    assert.deepEqual(s.panes[0].navigation.camera, cam, 'typing digits takes no view shortcut');
    assert.equal(s.pipeMove.origin, 'numeric');
    assert.deepEqual(s.pipeMove.record.offset, [0, 0, 0.15]);
    assert.deepEqual(s.pipeMove.record.points, RAISED, 'preview equals the service algorithm for this offset');
    await panelButton(0, 'apply-section').click();
    s = await waitFor(async () => { const x = await snap(); return x.pipeMove.outcome && x.pipeMove.outcome.kind === 'accepted' ? x : null; }, { message: 'accepted', timeout: 30000 });
    const sent = commandsSince(n);
    assert.equal(sent.length, 1, 'exactly one command');
    assert.equal(sent[0].body.type, 'move_route_segment');
    assert.deepEqual(Object.keys(sent[0].body.payload).sort(), ['assumption_id', 'connection_id', 'offset_m', 'segment_index']);
    assert.deepEqual(sent[0].body.payload.offset_m, [0, 0, 0.15]);
    assert.equal(sent[0].body.payload.segment_index, 1);
    assert.equal(sent[0].body.expected_revision, st.project.revision);
    assert.ok(sent[0].body.assumptions[sent[0].body.payload.assumption_id], 'fresh assumption record in assumptions');
    const done = await complete(st.project.revision + 1);
    const after = await state();
    const route = after.project.physical.routes[LINE];
    assert.deepEqual(route.points_m, RAISED);
    assert.equal(route.mode, 'manual');
    assert.equal(route.assumption_id, sent[0].body.payload.assumption_id);
    assert.equal(after.history.undo, st.history.undo + 1, 'one undo step');
    for (const [id, r] of Object.entries(st.project.physical.routes)) if (id !== LINE) assert.deepEqual(after.project.physical.routes[id], r, `${id} unchanged`);
    assert.deepEqual(after.project.pid, st.project.pid, 'P&ID layout and semantics unchanged');
    assert.deepEqual(after.project.model.connections, st.project.model.connections);
    s = await waitFor(async () => { const x = await snap(); return !x.pipeMove.record && x.panes[0].installed.revision === after.project.revision ? x : null; }, { message: 'record cleared after the final scene is installed' });
    const v = await info(0);
    assert.deepEqual(v.move.pipe.faded, [], 'no faded pipe');
    assert.equal(v.move.pipe.drawn, null, 'preview gone; the route changed so the section index ended');
    assert.ok(v.move.components.every((c) => c.offset.every((x) => x === 0)), 'no mesh offset (no double motion)');
    const scene = (await api(s.panes[0].installed.url)).body;
    const pipe = scene.components.find((c) => c.id === `${LINE}#pipe`);
    const zs = []; for (let i = 2; i < pipe.positions.length; i += 3) zs.push(pipe.positions[i]);
    assert.ok(Math.max(...zs) > 1.15 && Math.max(...zs) < 1.15 + 0.0603, `installed sweep reaches the new height ${Math.max(...zs)}`);
    assert.equal(done.sync, 'synchronized', 'all five outputs verified');
    accepted = { revision: after.project.revision, command: sent[0].body.id };
    await shot('03-applied');
    return accepted;
  });

  await step('Undo restores the previous route (one step)', async () => {
    const st = await state();
    await page.locator('#btn-undo').click();
    await complete(st.project.revision + 1);
    const after = await state();
    assert.deepEqual(after.project.physical.routes[LINE].points_m, SETUP_POINTS);
    return { revision: after.project.revision };
  });

  await step('a move into an equipment body: tentative preview, then the service refuses it (intrusion); model and file unchanged; nothing resent', async () => {
    await clickWorld([1.75, 0, 1.0]);
    await waitFor(async () => (await snap()).section?.segmentIndex === 1 && (await snap()).panes[0].pipe.eligible.ok, { message: 'section 2 eligible' });
    const st = await state();
    const hash = fileHash();
    const n = posts.length;
    await typeOffset(2, '-0.15');
    let s = await snap();
    assert.equal(s.pipeMove.record.problem, null, 'the client preview has no body screen');
    s = await expecting(/status of 400/, async () => {
      await panelButton(0, 'apply-section').click();
      return waitFor(async () => { const x = await snap(); return x.pipeMove.outcome && x.pipeMove.outcome.kind === 'rejected' ? x : null; }, { message: 'rejected', timeout: 30000 });
    });
    assert.match(s.pipeMove.outcome.text, /PI-101|enter equipment/);
    assert.ok(s.pipeMove.outcome.detail.some((d) => /route_edit_intrusion/.test(d)), JSON.stringify(s.pipeMove.outcome.detail));
    assert.equal(commandsSince(n).length, 1, 'one command, not resent');
    const after = await state();
    assert.equal(after.project.revision, st.project.revision, 'revision unchanged');
    assert.deepEqual(after.project.physical.routes[LINE], st.project.physical.routes[LINE]);
    assert.equal(fileHash(), hash, 'project file unchanged');
    assert.equal(s.pipeMove.record, null, 'preview removed');
    const sp = await sectionPanel(0);
    assert.ok(sp.status.some((l) => /Rejected/.test(l)), sp.status.join(' | '));
    await shot('04-collision-refused');
    return { outcome: s.pipeMove.outcome.text };
  });

  await step('a short end section: the preview explains why it cannot move; Apply and Enter send nothing', async () => {
    const n = posts.length;
    await chooseSection(3);
    await typeOffset(1, '0.1');
    const s = await snap();
    assert.equal(s.pipeMove.record.problem.code, 'route_edit_bend_room');
    const sp = await sectionPanel(0);
    assert.equal(sp.apply, false, 'Apply disabled');
    assert.ok(sp.status.some((l) => /Not possible/.test(l)), sp.status.join(' | '));
    await offsetField(0).press('Enter');
    await frame();
    assert.deepEqual(commandsSince(n), []);
    await panelButton(0, 'cancel-section').click();
    assert.equal((await snap()).pipeMove.record, null);
    return { problem: s.pipeMove.record.problem };
  });

  await step('a terminal section moves with a fixed nozzle stub and extra bends; both nozzles stay', async () => {
    const st = await state();
    await chooseSection(0);
    await typeOffset(1, '0.2');
    let s = await snap();
    assert.equal(s.pipeMove.record.problem, null);
    assert.deepEqual(s.pipeMove.record.points.slice(0, 3), [[1.5, 0, 0.5], [1.5, 0, 0.66], [1.5, 0.2, 0.66]]);
    await offsetField(0).press('Enter');
    s = await waitFor(async () => { const x = await snap(); return x.pipeMove.outcome?.kind === 'accepted' ? x : null; }, { message: 'terminal accepted', timeout: 30000 });
    await complete(st.project.revision + 1);
    const after = await state();
    const pts = after.project.physical.routes[LINE].points_m;
    assert.deepEqual(pts[0], SETUP_POINTS[0]);
    assert.deepEqual(pts[pts.length - 1], SETUP_POINTS[4]);
    assert.equal(pts.length, 8);
    await page.locator('#btn-undo').click();
    await complete(st.project.revision + 2);
    assert.deepEqual((await state()).project.physical.routes[LINE].points_m, SETUP_POINTS);
    return { points: pts };
  });

  await step('stale: another client changes the model during a preview; the preview ends and nothing is sent', async () => {
    await chooseSection(1);
    await typeOffset(2, '0.1');
    assert.equal((await snap()).pipeMove.record.phase, 'preview');
    const n = posts.length;
    const st = await state();
    const r = await api('/api/command', { id: `cmd-test-${crypto.randomUUID()}`, expected_revision: st.project.revision, type: 'rename_entity', payload: { entity_id: 'ent-tank', tag: 'TK-901' } });
    assert.equal(r.status, 200, JSON.stringify(r.body).slice(0, 300));
    const s = await waitFor(async () => { const x = await snap(); return !x.pipeMove.record && x.pipeMove.outcome ? x : null; }, { message: 'preview ended', timeout: 20000 });
    assert.match(s.pipeMove.outcome.reason, /stale|source-changed/);
    assert.deepEqual(commandsSince(n), [], 'nothing sent');
    await complete(st.project.revision + 1);
    const again = await snap();
    assert.equal(again.section && again.section.segmentIndex, 1, 'the route is unchanged, so the section stays selected');
    return { reason: s.pipeMove.outcome.reason };
  });

  await step('Published is read-only: no section gizmo, Apply unavailable, reason shown; Live again offers it', async () => {
    await page.locator(`${ROOTS[0]} .source-plate button[data-mode="published"]`).click();
    let s = await waitFor(async () => { const x = await snap(); return x.panes[0].mode === 'published' && x.panes[0].pipe.eligible.reason === 'published' ? x : null; }, { message: 'published reason' });
    assert.equal(s.panes[0].pipe.gizmo.attached, false);
    const sp = await sectionPanel(0);
    assert.equal(sp.apply, false);
    assert.ok(sp.status.some((l) => /read-only/.test(l)), sp.status.join(' | '));
    await page.locator(`${ROOTS[0]} .source-plate button[data-mode="live"]`).click();
    s = await waitFor(async () => { const x = await snap(); return x.panes[0].pipe.eligible.ok && x.panes[0].pipe.gizmo.attached ? x : null; }, { message: 'Live again' });
    return { ok: true };
  });

  await step('lost answer after the service applied it: unconfirmed, reconciled from the fresh route assumption, never resent', async () => {
    const st = await state();
    const n = posts.length;
    let forwarded = null;
    await expecting(NETWORK, async () => {
      await page.route('**/api/command', async (route) => {
        const response = await route.fetch();
        forwarded = { status: response.status(), body: await response.json() };
        await route.abort('connectionreset');
      }, { times: 1 });
      await typeOffset(2, '0.1');
      await panelButton(0, 'apply-section').click();
      const s = await waitFor(async () => { const x = await snap(); return x.pipeMove.outcome?.kind === 'accepted' && x.pipeMove.outcome.how ? x : null; }, { timeout: 30000, message: 'reconciled as accepted' });
      assert.equal(s.pipeMove.outcome.how, 'reconciled');
      assert.match(s.pipeMove.outcome.via, /command-log|route|assumption/);
    });
    assert.equal(forwarded.status, 200, 'the service applied it');
    assert.equal(commandsSince(n).length, 1, 'no automatic resend');
    await complete(st.project.revision + 1);
    assert.equal((await state()).project.revision, st.project.revision + 1, 'exactly one revision');
    await page.locator('#btn-undo').click();
    await complete(st.project.revision + 2);
    return { forwarded: forwarded.body.last_command };
  });

  await step('an older service without the capability: no section edit can start and nothing is sent', async () => {
    await page.route('**/api/state', async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      delete body.capabilities;
      await route.fulfill({ response, json: body });
    });
    try {
      await clickWorld([1.75, 0, 1.0]);
      const s = await waitFor(async () => { const x = await snap(); return !x.pipeCapable && x.panes[0].pipe.eligible.reason === 'unsupported' ? x : null; }, { message: 'unsupported', timeout: 10000 });
      assert.equal(s.panes[0].pipe.gizmo.attached, false);
      const n = posts.length;
      await offsetField(0).fill('0.1').catch(() => {});
      const sp = await sectionPanel(0);
      assert.equal(sp.apply, false);
      assert.ok(sp.status.some((l) => /older service|does not offer/.test(l)), sp.status.join(' | '));
      const visible = await page.locator(`${ROOTS[0]} .section-panel`).innerText();
      assert.doesNotMatch(visible, /pipe_segment_move|capabilit/, 'no raw API identifier shown');
      assert.equal((await snap()).pipeMove.record, null);
      assert.deepEqual(commandsSince(n), []);
    } finally {
      await page.unroute('**/api/state');
    }
    await waitFor(async () => (await snap()).pipeCapable, { message: 'capability back' });
    return { ok: true };
  });

  await step('duplicate Live pane mirrors the tentative preview read-only; Published shows none; re-rendering the origin pane ends the unsent preview', async () => {
    await page.locator('#layout-seg button[data-layout="4"]').click();
    await page.locator('#view-p2 .pane-tabs button[data-doc="3d"]').click();
    await page.locator('#view-p3 .pane-tabs button[data-doc="3d"]').click();
    await page.locator('#view-p3 .source-plate button[data-mode="published"]').click();
    await waitFor(async () => { const x = await snap(); return [0, 2, 3].every((i) => x.panes[i].installed && x.panes[i].pipe) ? x : null; }, { message: 'three 3D panes', timeout: 60000 });
    await setMove(2, true);
    await clickWorld([1.75, 0, 1.0]);
    await waitFor(async () => (await snap()).panes[0].pipe.eligible.ok, { message: 'pane 0 eligible' });
    const n = posts.length;
    await typeOffset(2, '0.1');
    let s = await snap();
    assert.equal(s.pipeMove.record.paneIndex, 0);
    assert.ok(s.panes[2].pipe.drawn && s.panes[2].pipe.drawn.preview, 'duplicate Live pane mirrors the preview');
    assert.equal(s.panes[2].pipe.gizmo.attached, false, 'no second editor in the duplicate pane');
    assert.equal(s.panes[2].pipe.eligible.reason, 'busy');
    assert.ok(s.panes[2].sectionPanel.status.some((l) => /in progress in pane 1/.test(l)), s.panes[2].sectionPanel.status.join(' | '));
    assert.ok(!s.panes[3].pipe.drawn || !s.panes[3].pipe.drawn.preview, 'Published pane shows no preview');
    assert.deepEqual(s.panes[3].pipe.faded, []);
    await shot('06-duplicate-live-published');
    await page.locator('#view-3d .pane-tabs button[data-doc="legend"]').click();
    s = await waitFor(async () => { const x = await snap(); return !x.pipeMove.record ? x : null; }, { message: 'preview ended with its pane' });
    assert.equal(s.pipeMove.outcome.kind, 'cancelled');
    assert.ok(!s.panes[2].pipe.drawn || !s.panes[2].pipe.drawn.preview, 'mirror removed');
    await page.locator('#view-3d .pane-tabs button[data-doc="3d"]').click();
    s = await waitFor(async () => { const x = await snap(); return x.panes[0].pipe && x.panes[0].pipe.drawn && x.panes[0].pipe.drawn.highlight ? x : null; }, { message: 'pane 0 3D again with the section sleeve' });
    assert.equal(s.section.segmentIndex, 1);
    assert.deepEqual(commandsSince(n), []);
    await setMove(2, false);
    return { reason: s.pipeMove.outcome.reason };
  });

  await step('layouts: focus, two and four panes at 900, 1280 and 1600 px; the section panel stays inside its pane and clear of the floating Ask', async () => {
    const results = [];
    await setMove(0, true); // the Move tool is per mounted 3D view (off again after the type switch above)
    await clickWorld([1.75, 0, 1.0]);
    for (const width of [1600, 1280, 900]) {
      await page.setViewportSize({ width, height: width === 900 ? 760 : 900 });
      for (const mode of ['focus', '2', '4']) {
        if (mode === 'focus') await page.locator('#btn-focus').click();
        else await page.locator(`#layout-seg button[data-layout="${mode}"]`).click();
        await new Promise((r) => setTimeout(r, 350));
        await frame();
        await waitFor(async () => (await sectionPanel(0)) && !(await sectionPanel(0)).hidden, { message: 'panel shown', timeout: 5000 });
        const geo = await page.evaluate(() => {
          const r = (el) => { const b = el.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom }; };
          const panel = document.querySelector('#view-3d .section-panel');
          const host = panel.parentElement;
          const pr = r(panel);
          // every core control is visible without scrolling, inside the panel, and is what a click there hits
          const controls = [...panel.querySelectorAll('.sp-axis button:not([hidden]), input.sp-offset, button[data-act="apply-section"], button[data-act="cancel-section"]')];
          const blocked = controls.filter((el) => {
            const b = r(el);
            const within = b.l >= pr.l - 0.5 && b.r <= pr.r + 0.5 && b.t >= pr.t - 0.5 && b.b <= pr.b + 0.5 && b.r - b.l > 4 && b.b - b.t > 4;
            const hit = document.elementFromPoint((b.l + b.r) / 2, (b.t + b.b) / 2);
            return !within || !hit || !(hit === el || el.contains(hit));
          }).map((el) => el.dataset.act || el.dataset.axis || el.className);
          return { panel: pr, host: r(host), ask: r(document.getElementById('btn-ask')), overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth, controls: controls.length, blocked, scrolled: panel.scrollHeight > panel.clientHeight + 1, heights: [panel.clientHeight, panel.scrollHeight, host.clientHeight, ...[...panel.children].filter((c) => !c.hidden && getComputedStyle(c).display !== "none").map((c) => Math.round(c.getBoundingClientRect().height))] };
        });
        const inside = geo.panel.l >= geo.host.l - 0.5 && geo.panel.r <= geo.host.r + 0.5 && geo.panel.t >= geo.host.t - 0.5 && geo.panel.b <= geo.host.b + 0.5;
        const overlap = !(geo.panel.r <= geo.ask.l || geo.panel.l >= geo.ask.r || geo.panel.b <= geo.ask.t || geo.panel.t >= geo.ask.b);
        results.push({ width, mode, inside, overlap, overflowX: geo.overflowX, controls: geo.controls, blocked: geo.blocked, scrolled: geo.scrolled, heights: geo.heights });
        await shot(`05-layout-${width}-${mode}`);
        if (mode === 'focus') await page.locator('#btn-exit-focus').click();
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    const bad = results.filter((r) => !r.inside || r.overlap || r.overflowX || r.blocked.length || r.controls < 5 || r.scrolled);
    assert.deepEqual(bad, [], JSON.stringify(results));
    return results;
  });

  await step('no Ask or planner request; no page or console errors', async () => {
    assert.deepEqual(posts.filter((p) => ASK_PATHS.has(p.path)), []);
    assert.deepEqual(problems, []);
    return { expected: expectedProblems.length, posts: posts.length };
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
