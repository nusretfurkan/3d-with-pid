// Browser check of the shared assistant (Ask / Change) and the Index cleanup.
// Fresh fixture copy + own service on a free port; never touches demo files or services 8765–8770.
//
// Ask transport: when the service implements POST /api/ask (docs/ASSISTANT_3D_CONTRACTS.md) the
// real endpoint is used, with identity/topology questions only; the run fails if any of them
// reached the language model. When the service answers 404/405, or ASK_TRANSPORT=stub is set,
// /api/ask and /api/cancel-ask are served inside this browser context by a contract-shaped stub
// built from the real service's current /api/state; only that mode exercises a deliberately slow
// answer, cancellation, a change during an answer and hostile model text. The report records
// which mode ran; a stub run says nothing about the backend. State, commands and builds are real.
//
// Usage: [ASK_TRANSPORT=stub] node tests/browser/assistant.e2e.mjs [step-filter-regex]
// Output: artifacts/ui-review/assistant-<stamp>/ (or $UI_REVIEW_DIR/assistant-<stamp>/) {*.png, report.json}; exit code 1 on any failure.

import { spawn, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const dir = path.join(REVIEW_DIR, `assistant-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const projectPath = path.join(dir, 'project.json');
const report = { started_at: new Date().toISOString(), dir, steps: [] };
const sample = spawnSync(PY, ['-m', 'skid.cli', 'sample', projectPath], { cwd: ROOT, encoding: 'utf8' });
if (sample.status !== 0) throw new Error(`sample failed: ${sample.stderr}`);

let service = null;
function startService(port = 0) {
  service = spawn(PY, ['-m', 'skid.interactive', '--project', projectPath, '--port', String(port), '--quiet'], { cwd: ROOT });
  return new Promise((resolve, reject) => {
    let out = '';
    const timer = setTimeout(() => reject(new Error(`service did not start: ${out}`)), 30000);
    service.stdout.on('data', (c) => { out += c; const m = /at (http:\/\/127\.0\.0\.1:(\d+)\/)/.exec(out); if (m) { clearTimeout(timer); resolve(m[1]); } });
    service.on('exit', (code) => reject(new Error(`service exited ${code}: ${out}`)));
  });
}
const url = await startService();
report.url = url;

const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const problems = watch(page);
const expectedProblems = []; // network noise this script causes on purpose (offline, restart, 409)
/** Run fn; browser problems matching `pattern` that appear meanwhile are expected, others stay problems. */
async function expecting(pattern, fn) {
  const n = problems.length;
  try {
    return await fn();
  } finally {
    for (const p of problems.splice(n)) (pattern.test(p) ? expectedProblems : problems).push(p);
  }
}
const NETWORK_DOWN = /ERR_CONNECTION_REFUSED|ERR_FAILED|ERR_EMPTY_RESPONSE|ERR_CONNECTION_RESET/;
const warnings = [];
page.on('console', (msg) => { if (msg.type() === 'warning') warnings.push(msg.text()); });
const snap = () => snapshot(page);
const shot = (name) => page.screenshot({ path: path.join(dir, `${name}.png`) });
const only = process.argv[2] ? new RegExp(process.argv[2]) : null;
async function step(name, fn) {
  if (only && !only.test(name)) return;
  const t0 = Date.now();
  try {
    const detail = await fn();
    report.steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
    console.log(`ok   ${name}`);
  } catch (error) {
    report.steps.push({ name, ok: false, ms: Date.now() - t0, error: String(error && error.stack ? error.stack : error) });
    console.log(`FAIL ${name}\n     ${String(error && error.message ? error.message : error).slice(0, 500)}`);
    await shot(`failed-${report.steps.length}`).catch(() => {});
  }
}
const api = async (p, body) => {
  const r = await fetch(new URL(p, url), { method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  let json = null;
  try { json = await r.json(); } catch { /* empty */ }
  return { status: r.status, body: json };
};
const settled = (rev) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === rev && s.sync === 'synchronized' && s.viewer && s.viewer.source && s.viewer.source.source_revision === rev ? s : null;
}, { timeout: 240000, message: `synchronized r${rev}` });
const stableCamera = async (i) => {
  let last = null;
  return waitFor(async () => {
    const x = await snap();
    const cam = x.panes[i].navigation && JSON.stringify(x.panes[i].navigation.camera);
    const ok = cam && cam === last;
    last = cam;
    return ok ? x : null;
  }, { interval: 300, message: `pane ${i} camera at rest` });
};
const camDelta = (a, b) => Math.max(...['position', 'target'].flatMap((k) => a[k].map((v, i) => Math.abs(v - b[k][i]))));
const fileHash = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const persisted = () => ({ project: fileHash(projectPath) });
const glCount = () => page.evaluate(() => document.querySelectorAll('canvas.gl').length);
const geometryOk = async () => page.evaluate(() => {
  const out = { overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth, clipped: [] };
  const sc = document.scrollingElement;
  if (sc.scrollTop || sc.scrollLeft) out.clipped.push(`document scrolled ${sc.scrollLeft},${sc.scrollTop}`);
  const vw = innerWidth;
  const vh = innerHeight;
  for (const el of document.querySelectorAll('.toolbar button, .toolbar .badge, .view-head button, .source-plate .badge, .source-plate button, .focus-bar button, #btn-ask, #assistant button, #assistant textarea')) {
    if (!el.getClientRects().length) continue;
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    const pane = el.closest('.pane');
    const asst = el.closest('#assistant');
    const box = pane ? pane.getBoundingClientRect() : asst ? asst.getBoundingClientRect() : { left: 0, right: vw, top: 0, bottom: vh };
    // controls inside the assistant's scrolling thread may be scrolled out of view; that is not clipping
    if (asst && el.closest('.asst-thread, .asst-composer')) continue;
    if (r.left < box.left - 0.5 || r.right > box.right + 0.5 || r.left < -0.5 || r.top < -0.5 || r.right > vw + 0.5 || r.bottom > vh + 0.5) {
      out.clipped.push(`${el.id || el.dataset.doc || el.dataset.act || el.dataset.mode || el.textContent.trim().slice(0, 20)} [${Math.round(r.left)}-${Math.round(r.right)} in ${Math.round(box.left)}-${Math.round(box.right)}; y ${r.top.toFixed(1)}-${r.bottom.toFixed(1)}]`);
    }
  }
  const items = [...document.querySelectorAll('.toolbar > *')].filter((e) => e.getClientRects().length).map((e) => e.getBoundingClientRect());
  for (let i = 1; i < items.length; i += 1) if (items[i].left < items[i - 1].right - 1 && items[i].width && items[i - 1].width) out.clipped.push(`toolbar overlap ${i}`);
  // the floating bubble lies inside the viewport
  const bubble = document.getElementById('assistant');
  if (!bubble.hidden) {
    const r = bubble.getBoundingClientRect();
    if (r.left < -0.5 || r.top < -0.5 || r.right > vw + 0.5 || r.bottom > vh + 0.5) out.clipped.push(`bubble ${Math.round(r.left)},${Math.round(r.top)}-${Math.round(r.right)},${Math.round(r.bottom)}`);
  }
  // the focus controls never cover a pane's or the assistant's header controls
  const bar = document.querySelector('#focus-bar');
  if (bar && bar.getClientRects().length) {
    const b = bar.getBoundingClientRect();
    for (const el of document.querySelectorAll('.view-head button, .asst-head button')) {
      if (!el.getClientRects().length) continue;
      const r = el.getBoundingClientRect();
      if (r.width && r.right > b.left + 0.5 && r.left < b.right - 0.5 && r.bottom > b.top + 0.5 && r.top < b.bottom - 0.5) out.clipped.push(`under focus bar: ${el.id || el.dataset.doc || el.dataset.act || el.dataset.mode || el.getAttribute('aria-label')}`);
    }
  }
  return out;
});

// ---------------------------------------------------------------- Ask transport (real or stub)

const asks = [];       // request bodies as sent by the page
const cancels = [];    // /api/cancel-ask bodies
const proposes = [];   // /api/propose bodies (copy-to-Change must not send any)
page.on('request', (req) => {
  const u = req.url();
  if (u.endsWith('/api/ask')) asks.push(req.postDataJSON());
  else if (u.endsWith('/api/cancel-ask')) cancels.push(req.postDataJSON());
  else if (u.endsWith('/api/propose')) proposes.push(req.postDataJSON());
});
const probe = await api('/api/ask', {});
const forced = process.env.ASK_TRANSPORT === 'stub';
const mode = forced || probe.status === 404 || probe.status === 405 ? 'stub' : 'real';
report.askTransport = { mode, probeStatus: probe.status, forced };
console.log(`Ask transport: ${mode}${forced ? ' (forced by ASK_TRANSPORT=stub)' : ''} (probe HTTP ${probe.status})`);
// Real mode asks only identity/topology questions; none may reach the language model (no external call).
const modelCalls = [];
page.on('response', async (res) => {
  if (mode !== 'real' || !res.url().endsWith('/api/ask') || res.status() !== 200) return;
  const body = await res.json().catch(() => null);
  if (body && body.responder && (body.responder.model_called || body.responder.language_model)) modelCalls.push(body.request_id);
});

const stubCancelled = new Set();
const ASK_KEYS = new Set(['request_id', 'question', 'selected_ids', 'expected_revision', 'input_digest', 'history', 'displayed_source']);
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
function stubFacts(project, ids, question) {
  const m = project.model;
  const tagged = new Map();
  for (const [id, e] of Object.entries(m.entities)) tagged.set(e.tag.toUpperCase(), id);
  for (const [id, c] of Object.entries(m.connections)) tagged.set(c.tag.toUpperCase(), id);
  const found = [...new Set([...ids.map((id) => (m.ports[id] ? m.ports[id].entity_id : id)), ...(question.toUpperCase().match(/[A-Z]{1,4}-\d{2,4}/g) || []).map((t) => tagged.get(t)).filter(Boolean)])];
  const refs = [];
  const lines = [];
  const ref = (id) => {
    const e = m.entities[id];
    const c = m.connections[id];
    if (!refs.some((r) => r.id === id)) refs.push({ id, tag: (e || c).tag, kind: e ? e.kind : 'connection', field_paths: [`model.${e ? 'entities' : 'connections'}.${id}.tag`], facts: [`tag ${(e || c).tag}`] });
  };
  for (const id of found) {
    const e = m.entities[id];
    if (!e) { if (m.connections[id]) { ref(id); lines.push(`${m.connections[id].tag} is a process connection.`); } continue; }
    ref(id);
    const own = new Set(Object.keys(m.ports).filter((p) => m.ports[p].entity_id === id));
    const into = Object.entries(m.connections).filter(([, c]) => own.has(c.to_port_id));
    const out = Object.entries(m.connections).filter(([, c]) => own.has(c.from_port_id));
    const other = (pid) => m.entities[m.ports[pid].entity_id];
    lines.push(`${e.tag} is a ${e.kind.replace(/_/g, ' ')}.`);
    for (const [cid, c] of into) { ref(cid); ref(m.ports[c.from_port_id].entity_id); lines.push(`Upstream: ${other(c.from_port_id).tag} via ${c.tag}.`); }
    for (const [cid, c] of out) { ref(cid); ref(m.ports[c.to_port_id].entity_id); lines.push(`Downstream: ${other(c.to_port_id).tag} via ${c.tag}.`); }
  }
  return { refs, lines, found };
}
async function stubAsk(route) {
  const body = route.request().postDataJSON() || {};
  const fulfil = (status, json) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
  const unknown = Object.keys(body).filter((k) => !ASK_KEYS.has(k));
  if (unknown.length || typeof body.request_id !== 'string' || typeof body.question !== 'string') {
    return fulfil(400, { error: `invalid Ask request (${unknown.join(', ') || 'shape'})`, diagnostics: [{ code: 'invalid_ask', message: 'invalid', severity: 'error' }] });
  }
  const before = (await api('/api/state')).body;
  if (body.expected_revision !== before.project.revision || body.input_digest !== before.document.input_digest || /stale-test/.test(body.question)) {
    return fulfil(409, { error: 'the project changed', diagnostics: [{ code: 'stale_ask', message: 'expected revision/digest do not match', severity: 'error' }] });
  }
  if (/slow/.test(body.question)) {
    for (let t = 0; t < 60 && !stubCancelled.has(body.request_id); t += 1) await delay(100);
  }
  const after = (await api('/api/state')).body;
  const base = {
    id: `ans-${body.request_id.slice(-8)}`, request_id: body.request_id, project_id: before.project.id, source_revision: before.project.revision,
    input_digest: before.document.input_digest, current_revision: after.project.revision, stale: after.project.revision !== before.project.revision,
    displayed_source: body.displayed_source || null, unknowns: [], limitations: ['Stub reply served by the browser test; not the backend.'],
  };
  if (stubCancelled.has(body.request_id)) return fulfil(200, { ...base, status: 'cancelled', answer: 'Cancelled before an answer was produced.', references: [], responder: { kind: 'local', language_model: null } });
  if (/explain/.test(body.question)) {
    return fulfil(200, {
      ...base, status: 'answered', answer: '<img src=x onerror="window.__pwned=1"> Model text with <b>markup</b> and https://example.invalid/x',
      references: [{ id: 'ent-pump', tag: '<script>window.__pwned=2</script>P-101', kind: 'centrifugal_pump', field_paths: ['model.entities.ent-pump.tag'], facts: ['<i>fact</i>'] }],
      operations: [{ type: 'move_equipment', payload: { entity_id: 'ent-pump' } }], commands: [{ type: 'undo' }],
      responder: { kind: 'language_model', language_model: 'claude-opus-5-5', effort: { requested: 'xhigh' }, api_key: 'must-not-show' },
    });
  }
  const { refs, lines, found } = stubFacts(before.project, body.selected_ids || [], body.question);
  if (!found.length) {
    return fulfil(200, { ...base, status: 'clarification', answer: 'Which item do you mean? No current tag or selection identifies it.', references: [], unknowns: ['target item'], responder: { kind: 'local', language_model: null } });
  }
  return fulfil(200, { ...base, status: 'answered', answer: lines.join('\n'), references: refs, unknowns: ['operating flow is not modelled'], responder: { kind: 'local', language_model: null } });
}
if (mode === 'stub') {
  await page.route('**/api/ask', stubAsk);
  await page.route('**/api/cancel-ask', async (route) => {
    const body = route.request().postDataJSON() || {};
    stubCancelled.add(body.request_id);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ request_id: body.request_id, cancelled: true }) });
  });
}

const turns = async () => (await snap()).assistant.turns;
const lastTurn = async () => { const t = await turns(); return t[t.length - 1]; };
/** Send one question; the new turn is recognised by its request ID (the thread is bounded, so counts stop growing). */
async function ask(question, { waitDone = true } = {}) {
  await page.fill('#asst-input', question);
  const prev = await lastTurn();
  const prevId = prev ? prev.request_id : null;
  await page.click('#asst-send');
  if (!waitDone) return waitFor(async () => { const l = await lastTurn(); return l && l.request_id !== prevId ? l : null; }, { message: 'turn started' });
  return waitFor(async () => { const l = await lastTurn(); return l && l.request_id !== prevId && l.phase !== 'running' ? l : null; }, { timeout: 120000, message: `answer to ${question}` });
}
const pumpSymbol = () => page.locator('#pane-pid [data-occurrence-id="sym-pump"] [data-role="symbol-body"] circle').first();

try {
  await page.goto(url);

  await step('initial: synchronized r0; assistant closed; one composer moved into the assistant; dock Feedback is an entry', async () => {
    const s = await settled(0);
    if (s.assistant.open || s.assistant.composerHost !== 'assistant') throw new Error(JSON.stringify(s.assistant));
    const counts = await page.evaluate(() => ({
      textareas: document.querySelectorAll('#panel-feedback textarea').length,
      composers: document.querySelectorAll('.composer textarea[aria-label="Change request"]').length,
      inDock: !!document.querySelector('#dock #panel-feedback'),
      entry: !!document.querySelector('#dock #panel-feedback-entry #btn-open-change'),
    }));
    if (counts.textareas !== 1 || counts.composers !== 1 || counts.inDock || !counts.entry) throw new Error(JSON.stringify(counts));
    await page.evaluate(() => { document.querySelector('#panel-feedback textarea').dataset.probe = 'one-instance'; });
    await shot('01-initial');
    return counts;
  });

  await step('Index pages show Tag / Item / Sheet-zone in duplicate Index panes; legend keys stay in the data and legend', async () => {
    await page.click('#layout-seg button[data-layout="4"]');
    await page.click('#view-p2 .pane-tabs button[data-doc="index"]');
    const s = await waitFor(async () => { const x = await snap(); return x.panes[2].installed && x.panes[3].installed && x.panes[2].type === 'index' ? x : null; }, { message: 'two index panes' });
    const tables = await page.evaluate(() => [...document.querySelectorAll('.pane:not([hidden]) .doc-pane table.table')].map((t) => ({
      head: [...t.querySelectorAll('thead th')].map((th) => th.textContent),
      cells: [...t.querySelectorAll('tbody tr')].map((tr) => tr.children.length),
    })));
    const index = tables.filter((t) => t.head[0] === 'Tag');
    if (index.length !== 2) throw new Error(`index tables ${JSON.stringify(tables).slice(0, 300)}`);
    for (const t of index) {
      if (JSON.stringify(t.head) !== JSON.stringify(['Tag', 'Item', 'Sheet / zone'])) throw new Error(JSON.stringify(t.head));
      if (!t.cells.length || t.cells.some((n) => n !== 3)) throw new Error(`row cells ${t.cells}`);
    }
    const data = (await api(s.panes[2].installed.url)).body;
    const withKeys = data.items.filter((i) => Array.isArray(i.legend_keys) && i.legend_keys.length).length;
    if (!withKeys) throw new Error('legend_keys missing from item-index.json');
    const intro = await page.textContent('#view-p2 .doc-intro');
    // row click still selects, both index panes highlight
    await page.click('#view-p2 tr[data-id="ent-pump"]');
    await waitFor(async () => { const n = await page.locator('.pane:not([hidden]) tr.sel[data-id="ent-pump"]').count(); return n === 2; }, { message: 'row selected in both index panes' });
    await page.click('#view-p2 .pane-tabs button[data-doc="legend"]');
    await shot('02-index-columns');
    return { rows: index[0].cells.length, itemsWithLegendKeys: withKeys, intro };
  });

  let cams;
  const paneRects = () => page.evaluate(() => [...document.querySelectorAll('.pane')].map((p) => { const r = p.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round).join(','); }));
  await step('Ask opens as a floating bubble over four mounted panes: no remount, no new WebGL context, pane sizes, cameras and paper views unchanged', async () => {
    const before = await stableCamera(0);
    const canvases = await glCount();
    const rects = await paneRects();
    await page.click('#btn-ask');
    const s = await waitFor(async () => { const x = await snap(); return x.assistant.open ? x : null; }, { message: 'open' });
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (focused !== 'asst-input') throw new Error(`focus on ${focused}`);
    if (s.assistant.layout !== 'floating' || s.assistant.mode !== 'ask') throw new Error(JSON.stringify(s.assistant));
    await page.waitForTimeout(500);
    if (JSON.stringify(await paneRects()) !== JSON.stringify(rects)) throw new Error(`pane rectangles changed: ${rects} -> ${await paneRects()}`);
    const after = await stableCamera(0);
    for (let i = 0; i < 4; i += 1) {
      if (after.panes[i].generation !== before.panes[i].generation || after.panes[i].type !== before.panes[i].type) throw new Error(`pane ${i} remounted`);
      if (!after.panes[i].visible) throw new Error(`pane ${i} hidden`);
    }
    const d = camDelta(after.panes[0].navigation.camera, before.panes[0].navigation.camera);
    if (d > 1e-9) throw new Error(`camera moved ${d}`);
    if (JSON.stringify(after.panes[1].navigation) !== JSON.stringify(before.panes[1].navigation)) throw new Error('paper view moved');
    if ((await glCount()) !== canvases) throw new Error('canvas count changed');
    if (after.panes[0].frames < before.panes[0].frames) throw new Error('viewer restarted');
    const g = await geometryOk();
    if (g.overflowX || g.clipped.length) throw new Error(JSON.stringify(g));
    cams = after;
    await shot('03-ask-open-four');
    return { canvases, cameraDelta: d, layout: s.assistant.layout, placement: s.assistant.placement };
  });

  let hashes;
  await step('Ask about the selected pump: exact request DTO, captured context, references, read-only', async () => {
    hashes = persisted();
    const before = await snap();
    const box = await pumpSymbol().boundingBox();
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
    await waitFor(async () => (await snap()).selection.join() === 'ent-pump', { message: 'pump selected' });
    // a click outside the bubble selects on the sheet and closes the bubble; the Ask button reopens it
    if ((await snap()).assistant.open) throw new Error('an outside click did not close the bubble');
    await page.click('#btn-ask');
    await waitFor(async () => (await snap()).assistant.context.join() === 'ent-pump', { message: 'context chip' });
    const chip = await page.textContent('#assistant .asst-context .chips');
    const t = await ask('What is upstream and downstream of it?');
    const body = asks[asks.length - 1];
    const keys = Object.keys(body).sort();
    const allowed = ['displayed_source', 'expected_revision', 'history', 'input_digest', 'question', 'request_id', 'selected_ids'];
    if (keys.some((k) => !allowed.includes(k)) || !keys.includes('request_id') || !keys.includes('input_digest')) throw new Error(`request keys ${keys}`);
    if (JSON.stringify(body.selected_ids) !== '["ent-pump"]' || body.expected_revision !== 0) throw new Error(JSON.stringify(body));
    if (!/^ask-[0-9a-f]{32}$/.test(body.request_id)) throw new Error(body.request_id);
    if (t.phase !== 'done' || !['answered', 'clarification'].includes(t.status)) throw new Error(JSON.stringify(t));
    if (mode === 'stub' && !t.references.includes('ent-pump')) throw new Error(JSON.stringify(t.references));
    const after = await snap();
    if (after.revision !== before.revision || after.build.id !== before.build.id) throw new Error('Ask changed revision or started a build');
    const st = (await api('/api/state')).body;
    if (st.history.undo !== 0 || st.history.redo !== 0) throw new Error('history changed');
    if (JSON.stringify(persisted()) !== JSON.stringify(hashes)) throw new Error('project file changed');
    const stamp = await page.textContent('#assistant .asst-turn:last-child .stamp');
    await shot('04-ask-answer');
    return { chip, request: body, turn: t, stamp };
  });

  await step('clicking a reference selects its current counterpart without moving any camera or paper view', async () => {
    const before = await stableCamera(0);
    const refs = await page.locator('#assistant .asst-turn:last-child .turn-refs .tchip[data-ref]').evaluateAll((els) => els.map((e) => e.dataset.ref));
    const target = refs.find((id) => id !== 'ent-pump');
    if (!target) throw new Error(`no other reference: ${refs}`);
    await page.click(`#assistant .asst-turn:last-child .turn-refs .tchip[data-ref="${target}"]`);
    const s = await waitFor(async () => { const x = await snap(); return x.selection.join() === target ? x : null; }, { message: `selected ${target}` });
    await page.waitForTimeout(400);
    const after = await stableCamera(0);
    for (const i of [0, 2]) {
      if (after.panes[i].type === '3d') {
        const d = camDelta(after.panes[i].navigation.camera, before.panes[i].navigation.camera);
        if (d > 1e-9) throw new Error(`pane ${i} camera moved ${d}`);
      }
    }
    if (JSON.stringify(after.panes[1].navigation) !== JSON.stringify(before.panes[1].navigation)) throw new Error('paper view moved');
    if (!s.pidSelected.length) throw new Error('nothing highlighted on the P&ID');
    return { target, pid: s.pidSelected };
  });

  await step('model-style reply is inert: markup stays text, operations are ignored, secrets hidden', async () => {
    if (mode !== 'stub') return { skipped: 'real backend: covered by service tests (no stub injection)' };
    const t = await ask('Please explain the pump in general terms');
    await page.waitForTimeout(300);
    const dom = await page.evaluate(() => {
      const turn = document.querySelector('#assistant .asst-turn:last-child');
      const handlers = [...turn.querySelectorAll('*')].filter((el) => [...el.attributes].some((a) => /^on/i.test(a.name) || /javascript:/i.test(a.value)));
      return {
        parsed: turn.querySelectorAll('img, script, a, iframe, object, embed, svg image, style').length,
        handlers: handlers.length,
        textChildren: turn.querySelector('.a-text').children.length,
        chipChildren: [...turn.querySelector('.turn-refs .tchip').children].map((c) => c.tagName),
        text: turn.querySelector('.a-text').textContent,
        chip: turn.querySelector('.turn-refs .tchip').textContent,
        pwned: window.__pwned || null,
        who: turn.querySelector('.stamp .who').textContent,
        allText: turn.textContent,
      };
    });
    if (dom.parsed || dom.handlers || dom.textChildren || dom.pwned || dom.chipChildren.some((t) => t !== 'SMALL')) throw new Error(JSON.stringify(dom));
    if (!dom.text.startsWith('<img src=x')) throw new Error(dom.text);
    if (/move_equipment|must-not-show/.test(dom.allText)) throw new Error('operation or secret rendered');
    if (!/Claude \(Max\)/.test(dom.who)) throw new Error(dom.who);
    return { status: t.status, who: dom.who };
  });

  await step('ordinary shortcuts do not fire while typing in Ask; Escape closes and restores focus; reopen keeps draft and thread', async () => {
    const before = await stableCamera(0);
    const turnsBefore = (await turns()).length;
    await page.focus('#asst-input');
    await page.keyboard.type('m10fle');
    const s1 = await snap();
    if (s1.layout.maximized !== null) throw new Error('M maximized a pane while typing');
    if (await page.inputValue('#asst-input') !== 'm10fle') throw new Error('typing lost');
    await page.waitForTimeout(400);
    const mid = await stableCamera(0);
    if (camDelta(mid.panes[0].navigation.camera, before.panes[0].navigation.camera) > 1e-9) throw new Error('a view shortcut moved the camera');
    await page.keyboard.press('Escape');
    const s2 = await snap();
    if (s2.assistant.open) throw new Error('Escape did not close');
    if (s2.layout.focus || s2.selection.length === 0) throw new Error('Escape did more than close the assistant');
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (focused !== 'btn-ask') throw new Error(`focus returned to ${focused}`);
    await page.click('#btn-ask');
    if (await page.inputValue('#asst-input') !== 'm10fle') throw new Error('draft lost on reopen');
    if ((await turns()).length !== turnsBefore) throw new Error('thread lost on reopen');
    await page.fill('#asst-input', '');
    return { focused };
  });

  await step('copy an edit request to Change: draft only, no proposal request, explicit Review remains', async () => {
    const n = proposes.length;
    await page.fill('#asst-input', 'Rename P-101 to P-201');
    await page.waitForSelector('#assistant .asst-edit-hint:not([hidden]) button');
    await page.click('#assistant .asst-edit-hint button');
    const s = await waitFor(async () => { const x = await snap(); return x.assistant.mode === 'change' ? x : null; }, { message: 'change mode' });
    const draft = await page.inputValue('#panel-feedback textarea');
    if (draft !== 'Rename P-101 to P-201') throw new Error(draft);
    await page.waitForTimeout(500);
    if (proposes.length !== n) throw new Error('a proposal was requested');
    if (s.composer.proposal || s.composer.busy) throw new Error(JSON.stringify(s.composer));
    await shot('05-copied-to-change');
    await page.click('#asst-tab-ask');
    await page.fill('#asst-input', '');
    return { draft };
  });

  await step('Change in focus with four panes: proposal and draft survive switch/close/reopen/focus; Apply and Undo through the assistant', async () => {
    await page.click('#asst-tab-change');
    await page.fill('#panel-feedback textarea', 'Set motor length of P-101 to 0.42 m');
    await page.click('#panel-feedback .composer > button');
    await page.waitForSelector('#panel-feedback .proposal .verdict.yes');
    const proposal = (await snap()).composer.proposal;
    await page.click('#asst-tab-ask');
    await page.keyboard.press('Escape');
    await page.click('#btn-focus');
    await page.click('#btn-ask');
    await page.click('#asst-tab-change');
    let s = await snap();
    if (!s.layout.focus || !s.assistant.open || s.composer.proposal?.id !== proposal.id) throw new Error(JSON.stringify({ focus: s.layout.focus, a: s.assistant.open, p: s.composer.proposal }));
    const same = await page.evaluate(() => document.querySelector('#panel-feedback textarea').dataset.probe);
    if (same !== 'one-instance') throw new Error('composer textarea was recreated');
    const vis = await page.evaluate(() => [...document.querySelectorAll('.pane:not([hidden])')].filter((p) => p.getClientRects().length).length);
    if (vis !== 4) throw new Error(`${vis} panes visible`);
    await shot('06-focus-four-change');
    await page.click('#panel-feedback .proposal button:has-text("Apply")');
    s = await waitFor(async () => { const x = await snap(); return x.revision === 1 ? x : null; }, { message: 'applied r1' });
    if (s.layout.focus !== true) throw new Error('left focus');
    await page.waitForSelector('#asst-undo:not([disabled])');
    await page.click('#asst-undo');
    s = await waitFor(async () => { const x = await snap(); return x.revision === 2 ? x : null; }, { message: 'undo r2' });
    const st = (await api('/api/state')).body;
    if (st.history.undo !== 0 || st.history.redo !== 1) throw new Error(JSON.stringify(st.history));
    return { proposal: proposal.id, history: st.history };
  });

  await step('four panes stay usable while Ask runs in focus: 3D orbit responds; cancel is distinct from close', async () => {
    await page.click('#asst-tab-ask');
    if (mode !== 'stub') return { skipped: 'needs a deliberately slow answer (stub only); backend cancellation is covered by service tests' };
    const before = await stableCamera(0);
    const t = await ask('slow: what is upstream of P-101?', { waitDone: false });
    if (t.phase !== 'running') throw new Error(JSON.stringify(t));
    const box = await page.locator('#view-3d .pane-content').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 100, box.y + box.height / 2 + 20, { steps: 6 });
    await page.mouse.up();
    const after = await stableCamera(0);
    if (camDelta(after.panes[0].navigation.camera, before.panes[0].navigation.camera) < 1e-6) throw new Error('3D pane did not respond while Ask ran');
    if (!after.assistant.open) throw new Error('orbiting a view (a drag, not a click) closed the bubble');
    // close: the request keeps running; reopen shows it; then cancel explicitly
    await page.click('#assistant .asst-close');
    let s = await snap();
    if (s.assistant.open || !s.assistant.pending) throw new Error(`close cancelled the request: ${JSON.stringify(s.assistant.pending)}`);
    if (!s.layout.focus) throw new Error('close left focus');
    await page.click('#btn-ask');
    s = await snap();
    if (!s.assistant.pending) throw new Error('request lost on reopen');
    await page.click('#assistant .asst-turn:last-child button:has-text("Cancel question")');
    const done = await waitFor(async () => { const l = await lastTurn(); return l.phase !== 'running' ? l : null; }, { timeout: 30000, message: 'cancelled' });
    if (!cancels.some((c) => c.request_id === t.request_id)) throw new Error('no /api/cancel-ask for the request');
    if (done.status !== 'cancelled' && done.phase !== 'failed') throw new Error(JSON.stringify(done));
    if ((await snap()).revision !== 2) throw new Error('cancel changed the project');
    await shot('07-cancelled');
    return { done };
  });

  await step('a project change during a slow answer gives a source-labelled stale answer and an explicit refresh', async () => {
    if (mode !== 'stub') return { skipped: 'needs a deliberately slow answer (stub only)' };
    const asked = asks.length;
    const started = ask('slow: what is P-101?', { waitDone: false });
    await started;
    const r = await api('/api/command', { id: `cmd-assistant-test-${Date.now()}`, expected_revision: 2, type: 'rename_project', payload: { name: 'Assistant test skid' } });
    if (r.status !== 200) throw new Error(JSON.stringify(r));
    const t = await waitFor(async () => { const l = await lastTurn(); return l.phase !== 'running' ? l : null; }, { timeout: 120000, message: 'slow answer' });
    if (t.source_revision !== 2 || t.stale !== true) throw new Error(JSON.stringify(t));
    const label = await page.textContent('#assistant .asst-turn:last-child .asst-stale');
    if (!/Answer based on r2; current r3/.test(label)) throw new Error(label);
    await page.waitForTimeout(800);
    if (asks.length !== asked + 1) throw new Error('an automatic resend happened');
    await shot('08-stale-answer');
    await page.click('#assistant .asst-turn:last-child .asst-stale button');
    const fresh = await waitFor(async () => { const l = await lastTurn(); return l.phase !== 'running' && l.request_id !== t.request_id ? l : null; }, { timeout: 120000, message: 'refreshed answer' });
    if (fresh.source_revision !== 3 || fresh.stale) throw new Error(JSON.stringify(fresh));
    return { label: label.trim(), fresh: fresh.source_revision };
  });

  await step('older Published pane is named separately from the current-model answer', async () => {
    // the floating bubble may lie over pane 4's header: close it first, as a user would (or move the launcher)
    if ((await snap()).assistant.open) await page.click('#assistant .asst-close');
    await page.click('#view-p3 .pane-tabs button[data-doc="pid"]').catch(() => {});
    await page.click('#view-p3 [role="radiogroup"] button[data-mode="published"]');
    await waitFor(async () => { const x = await snap(); return x.panes[3].installed && x.panes[3].installed.origin === 'published' ? x : null; }, { message: 'published pane' });
    await page.click('#view-p3 .pane-content', { position: { x: 30, y: 60 } });
    if (!(await snap()).assistant.open) await page.click('#btn-ask'); // the pane clicks were outside clicks
    const src = await page.textContent('#assistant .asst-source');
    if (!/Visible pane 4 · P&ID · published r\d+/.test(src)) throw new Error(`source line did not follow the worked pane: ${src}`);
    const t = await ask('What is P-101?');
    const body = asks[asks.length - 1];
    const s = await snap();
    const pub = s.panes[3].installed;
    if (!body.displayed_source || body.displayed_source.mode !== 'published' || body.displayed_source.source_revision !== pub.revision) throw new Error(JSON.stringify(body.displayed_source));
    const note = await page.locator('#assistant .asst-turn:last-child .asst-visible').count() ? await page.textContent('#assistant .asst-turn:last-child .asst-visible') : null;
    if (pub.revision !== s.revision && !/visible publication r\d+/.test(note || '')) throw new Error(`no visible-source note: ${note}`);
    return { visibleLine: src, note, published: pub.revision, current: s.revision, status: t.status };
  });

  await step('stale preflight (409 stale_ask) and offline: nothing asked, draft kept, explicit retry', async () => {
    if (mode === 'stub') {
      const t = await expecting(/status of 409/, () => ask('stale-test: what is P-101?'));
      if (t.failure !== 'Not asked: the project changed') throw new Error(JSON.stringify(t));
      if (!(await page.locator('#assistant .asst-turn:last-child button:has-text("Ask again")').count())) throw new Error('no explicit retry');
    }
    let disabled;
    let note;
    await expecting(NETWORK_DOWN, async () => {
      await page.route('**/api/state', (route) => route.abort('connectionrefused'));
      await waitFor(async () => (await snap()).online === false, { timeout: 15000, message: 'offline' });
      await page.fill('#asst-input', 'What is HV-101?');
      disabled = await page.isDisabled('#asst-send');
      note = await page.textContent('#assistant .budget-note');
      await shot('09-offline');
      await page.unroute('**/api/state');
      await waitFor(async () => (await snap()).online === true, { timeout: 15000, message: 'online again' });
    });
    if (!disabled || !/not reachable/.test(note)) throw new Error(`${disabled} ${note}`);
    if (await page.inputValue('#asst-input') !== 'What is HV-101?') throw new Error('draft lost offline');
    await page.fill('#asst-input', '');
    return { note };
  });

  await step('responsive: 900/1280/1600 × two/four panes × normal/focus with the assistant open fit without overflow or covered controls', async () => {
    const results = [];
    for (const w of [900, 1280, 1600]) {
      await page.setViewportSize({ width: w, height: 900 });
      for (const n of [2, 4]) {
        for (const focus of [false, true]) {
          const s0 = await snap();
          if (s0.layout.focus !== focus) await page.evaluate((on) => { const b = document.querySelector(on ? '#btn-focus' : '#btn-exit-focus'); b.click(); }, focus);
          if (!focus) await page.evaluate((n) => document.querySelector(`#layout-seg button[data-layout="${n}"]`).click(), n);
          if (!(await snap()).assistant.open) await page.click('#btn-ask');
          await page.waitForTimeout(400);
          const g = await geometryOk();
          const s = await snap();
          results.push({ w, n, focus, layout: s.assistant.layout, visible: s.layout.visible.length, ...g });
          await shot(`10-${w}-${n}${focus ? '-focus' : ''}`);
        }
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    const bad = results.filter((r) => r.overflowX || r.clipped.length || r.layout !== 'floating');
    if (bad.length) throw new Error(JSON.stringify(bad).slice(0, 2000));
    return results.map((r) => `${r.w}/${r.n}/${r.focus ? 'focus' : 'normal'}/${r.layout}`);
  });

  await step('repeated open/close and mode switches keep one canvas per 3D pane and no WebGL warnings', async () => {
    const s0 = await snap();
    const canvases = await glCount();
    const gens = s0.panes.map((p) => p.generation);
    for (let k = 0; k < 12; k += 1) {
      await page.click('#btn-ask');
      await page.evaluate((tab) => { const t = document.querySelector(tab); if (t && t.getClientRects().length) t.click(); }, k % 2 ? '#asst-tab-change' : '#asst-tab-ask');
      await page.waitForTimeout(60);
    }
    const s = await snap();
    if ((await glCount()) !== canvases) throw new Error('canvas count changed');
    if (JSON.stringify(s.panes.map((p) => p.generation)) !== JSON.stringify(gens)) throw new Error('a pane remounted');
    const ctx = warnings.filter((w) => /WebGL|context/i.test(w));
    if (ctx.length) throw new Error(ctx.slice(0, 3).join(' | '));
    return { canvases, threePanes: s.panes.filter((p) => p.type === '3d').length };
  });

  await step('dock Feedback entry opens Change; Command JSON tab still works', async () => {
    const s0 = await snap();
    if (s0.layout.focus) await page.click('#btn-exit-focus');
    if ((await snap()).assistant.open) await page.click('#assistant .asst-close');
    await page.click('#change-tabs button[data-tab="command"]');
    if (!(await page.isVisible('#panel-command'))) throw new Error('command panel hidden');
    if ((await snap()).assistant.open) throw new Error('command tab opened the assistant');
    await page.click('#change-tabs button[data-tab="feedback"]');
    const s = await waitFor(async () => { const x = await snap(); return x.assistant.open && x.assistant.mode === 'change' ? x : null; }, { message: 'change opened' });
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.closest('#panel-feedback') !== null);
    if (!focused) throw new Error('composer not focused');
    await page.keyboard.press('Escape');
    const back = await page.evaluate(() => document.activeElement && document.activeElement.dataset.tab);
    return { mode: s.assistant.mode, focusReturnedTo: back };
  });

  await step('the thread keeps the last 24 turns; polling never moves its scroll position', async () => {
    const s0 = await snap();
    if (s0.layout.focus) await page.click('#btn-exit-focus');
    if (!(await snap()).assistant.open) await page.click('#btn-ask');
    await page.click('#asst-tab-ask');
    const tags = ['P-101', 'TK-101', 'HV-101', 'NRV-101', 'HV-102'];
    for (let k = (await turns()).length; k < 27; k += 1) await ask(`What is ${tags[k % tags.length]}?`);
    const s = await snap();
    if (s.assistant.turns.length !== 24 || s.assistant.dropped < 3) throw new Error(`${s.assistant.turns.length} turns, ${s.assistant.dropped} dropped`);
    const sep = await page.textContent('#assistant .asst-sep');
    await page.evaluate(() => { document.querySelector('#assistant .asst-thread').scrollTop = 300; });
    const before = await page.evaluate(() => document.querySelector('#assistant .asst-thread').scrollTop);
    await page.waitForTimeout(3500); // several polls and ticks
    const after = await page.evaluate(() => document.querySelector('#assistant .asst-thread').scrollTop);
    if (Math.abs(after - before) > 1) throw new Error(`thread scrolled ${before} -> ${after}`);
    const history = asks[asks.length - 1].history || [];
    if (history.length !== 6) throw new Error(`history ${history.length}`);
    return { dropped: s.assistant.dropped, sep, scrollTop: after, historySent: history.length };
  });

  await step('a restarted service starts a new conversation: Ask thread/draft and the Change draft/proposal are cleared', async () => {
    if (!(await snap()).assistant.open) await page.click('#btn-ask');
    await page.click('#asst-tab-ask');
    await page.fill('#asst-input', 'draft before restart');
    await page.click('#asst-tab-change');
    await page.fill('#panel-feedback textarea', 'Set motor length of P-101 to 0.40 m');
    const port = new URL(url).port;
    const s = await expecting(NETWORK_DOWN, async () => {
      const old = service;
      const exited = new Promise((r) => old.once('exit', r));
      old.kill('SIGTERM');
      await exited;
      await startService(port);
      return waitFor(async () => { const x = await snap(); return x.online && x.assistant.identity && !x.assistant.turns.length ? x : null; }, { timeout: 60000, message: 'reset after restart' });
    });
    const change = await page.inputValue('#panel-feedback textarea');
    await page.click('#asst-tab-ask');
    const askDraft = await page.inputValue('#asst-input');
    const notice = await page.textContent('#assistant .asst-notice');
    if (change || askDraft || !/changed/.test(notice) || s.composer.proposal) throw new Error(JSON.stringify({ change, askDraft, notice }));
    await shot('11-after-restart');
    return { notice };
  });
} finally {
  report.problems = problems;
  report.expectedProblems = expectedProblems;
  report.modelCalls = modelCalls;
  if (modelCalls.length) problems.push(`unexpected language-model Ask calls: ${modelCalls.join(', ')}`);
  report.passed = report.steps.every((s) => s.ok) && problems.length === 0;
  report.finished_at = new Date().toISOString();
  fs.writeFileSync(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  console.log(`problems: ${problems.length}${problems.length ? `\n${problems.slice(0, 10).join('\n')}` : ''}`);
  console.log(`passed: ${report.passed} · ${dir}`);
  await browser.close();
  if (service) service.kill();
}
process.exit(report.passed ? 0 : 1);
