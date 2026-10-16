// Browser check of the floating Ask launcher and bubble, and of the removal of decorative
// vertical rules. Fresh sample + own service on a free port; never touches demo files or
// services 8765–8770. /api/ask and /api/cancel-ask are always served by an in-page stub (a
// deliberately slow answer is needed for dismissal versus cancellation); no language model is
// called. State, commands and proposals are real.
//
// Usage: [UI_REVIEW_DIR=artifacts/ui-review/<review>] node tests/browser/floating-assistant.e2e.mjs [step-filter-regex]
// Output: <review dir>/floating-<stamp>/{*.png, report.json}; exit code 1 on any failure.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const STORAGE_KEY = 'skid.askLauncher.position.v1';
const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const dir = path.join(REVIEW_DIR, `floating-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const projectPath = path.join(dir, 'project.json');
const report = { started_at: new Date().toISOString(), dir, askTransport: 'stub (in-page)', steps: [] };
const sample = spawnSync(PY, ['-m', 'skid.cli', 'sample', projectPath], { cwd: ROOT, encoding: 'utf8' });
if (sample.status !== 0) throw new Error(`sample failed: ${sample.stderr}`);

const service = spawn(PY, ['-m', 'skid.interactive', '--project', projectPath, '--port', '0', '--quiet'], { cwd: ROOT });
const url = await new Promise((resolve, reject) => {
  let out = '';
  const timer = setTimeout(() => reject(new Error(`service did not start: ${out}`)), 30000);
  service.stdout.on('data', (c) => { out += c; const m = /at (http:\/\/127\.0\.0\.1:(\d+)\/)/.exec(out); if (m) { clearTimeout(timer); resolve(m[1]); } });
  service.on('exit', (code) => reject(new Error(`service exited ${code}: ${out}`)));
});
report.url = url;

const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const problems = watch(page);
const only = process.argv[2] ? new RegExp(process.argv[2]) : null;
const snap = () => snapshot(page);
const shot = (name, p = page) => p.screenshot({ path: path.join(dir, `${name}.png`) });
async function step(name, fn) {
  if (only && !only.test(name)) return;
  const t0 = Date.now();
  try {
    const detail = await fn();
    report.steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
    console.log(`ok   ${name}`);
  } catch (error) {
    report.steps.push({ name, ok: false, ms: Date.now() - t0, error: String(error && error.stack ? error.stack : error) });
    console.log(`FAIL ${name}\n     ${String(error && error.message ? error.message : error).slice(0, 600)}`);
    await shot(`failed-${report.steps.length}`).catch(() => {});
  }
}
const api = async (p) => (await fetch(new URL(p, url))).json();

// ---------------------------------------------------------------- traffic and the Ask stub

const posts = [];
page.on('request', (req) => { if (req.method() === 'POST') posts.push({ path: new URL(req.url()).pathname, body: req.postDataJSON() }); });
const postsSince = (n, p) => posts.slice(n).filter((x) => !p || x.path === p);
const cancelled = new Set();
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function stubAsk(route) {
  const body = route.request().postDataJSON() || {};
  const st = await api('/api/state');
  if (/slow/.test(body.question)) for (let t = 0; t < 80 && !cancelled.has(body.request_id); t += 1) await delay(100);
  const base = {
    id: `ans-${body.request_id.slice(-8)}`, request_id: body.request_id, project_id: st.project.id, source_revision: st.project.revision,
    input_digest: st.document.input_digest, current_revision: st.project.revision, stale: false, displayed_source: body.displayed_source || null,
    unknowns: [], limitations: ['Stub reply served by the browser test; not the backend.'],
  };
  const reply = cancelled.has(body.request_id)
    ? { ...base, status: 'cancelled', answer: 'Cancelled before an answer was produced.', references: [], responder: { kind: 'local', language_model: null } }
    : { ...base, status: 'answered', answer: 'P-101 is a centrifugal pump.\nUpstream: HV-101 via L-101.\nDownstream: NRV-101 via L-102.', references: [{ id: 'ent-pump', tag: 'P-101', kind: 'centrifugal_pump', field_paths: ['model.entities.ent-pump.tag'], facts: ['tag P-101'] }], unknowns: ['operating flow is not modelled'], responder: { kind: 'local', language_model: null } };
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reply) });
}
async function installStub(p) {
  await p.route('**/api/ask', stubAsk);
  await p.route('**/api/cancel-ask', async (route) => {
    const body = route.request().postDataJSON() || {};
    cancelled.add(body.request_id);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ request_id: body.request_id, cancelled: true }) });
  });
}
await installStub(page);

// ---------------------------------------------------------------- probes

const launcherBox = () => page.evaluate(() => { const r = document.getElementById('btn-ask').getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; });
const paneRects = () => page.evaluate(() => [...document.querySelectorAll('.pane')].map((p) => { const r = p.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round).join(','); }));
const navigation = async () => (await snap()).panes.map((p) => JSON.stringify(p.navigation || null));
const settled = (rev) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === rev && s.sync === 'synchronized' && s.viewer && s.viewer.source && s.viewer.source.source_revision === rev ? s : null;
}, { timeout: 240000, message: `synchronized r${rev}` });
const restingNavigation = async () => {
  let last = null;
  return waitFor(async () => { const n = JSON.stringify(await navigation()); const ok = n === last; last = n; return ok ? JSON.parse(n) : null; }, { interval: 300, message: 'views at rest' });
};
/** Element under each point of interest: what the user can actually reach. */
const reach = () => page.evaluate(() => {
  const hit = (el) => {
    if (!el || !el.getClientRects().length) return null;
    const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !!top && (top === el || el.contains(top));
  };
  return {
    launcher: hit(document.getElementById('btn-ask')),
    close: hit(document.querySelector('#assistant .asst-close')),
    input: hit(document.getElementById('asst-input')),
    exitFocus: document.body.classList.contains('focus-mode') ? hit(document.getElementById('btn-exit-focus')) : null,
  };
});
const bubbleGeometry = () => page.evaluate(() => {
  const a = document.getElementById('assistant');
  const r = a.getBoundingClientRect();
  const l = document.getElementById('btn-ask').getBoundingClientRect();
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  return {
    hidden: a.hidden, left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height),
    inside: r.left >= -0.5 && r.top >= -0.5 && r.right <= vw + 0.5 && r.bottom <= vh + 0.5,
    launcherInside: l.left >= -0.5 && l.top >= -0.5 && l.right <= vw + 0.5 && l.bottom <= vh + 0.5,
    overlapsLauncher: r.right > l.left && r.left < l.right && r.bottom > l.top && r.top < l.bottom,
    overflowX: document.documentElement.scrollWidth > vw,
    side: a.dataset.side, radius: getComputedStyle(a).borderTopLeftRadius, background: getComputedStyle(a).backgroundColor,
  };
});

/** Decorative vertical strokes in the rendered UI (drawings, 3D canvases and icons excluded). */
const verticalRules = () => page.evaluate(() => {
  const found = [];
  const px = (v) => parseFloat(v) || 0;
  const name = (el) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).slice(0, 3).join('.')}` : ''}`;
  const insetX = (shadow) => shadow && shadow !== 'none' && shadow.split(/,(?![^(]*\))/).some((s) => /inset/.test(s) && (() => {
    const n = s.replace(/rgba?\([^)]*\)|#[0-9a-f]+|inset/gi, '').trim().split(/\s+/).map(parseFloat);
    return n.length >= 2 && n[0] !== 0 && n[1] === 0 && !(n[3] > 0); // x offset only: a side bar, not a ring
  })());
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg, canvas, .sheet-host, .gl-overlay, .doc-frame')) continue;
    if (!el.getClientRects().length) continue;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const side = (s) => px(cs[`border${s}Width`]) > 0 && cs[`border${s}Style`] !== 'none' && cs[`border${s}Style`] !== 'hidden';
    const [L, R, T, B] = ['Left', 'Right', 'Top', 'Bottom'].map(side);
    if ((L || R) && !(L && R && T && B) && r.height > 8) found.push(`${name(el)} border ${L ? 'L' : ''}${R ? 'R' : ''}${T ? 'T' : ''}${B ? 'B' : ''}`);
    if (insetX(cs.boxShadow)) found.push(`${name(el)} inset side shadow`);
    for (const pseudo of ['::before', '::after']) {
      const ps = getComputedStyle(el, pseudo);
      if (ps.content === 'none' || ps.content === 'normal') continue;
      const w = px(ps.width);
      const h = px(ps.height);
      const bar = h > 8 && w > 0 && w <= 4 && ps.backgroundColor !== 'rgba(0, 0, 0, 0)';
      const pl = px(ps.borderLeftWidth) > 0 && ps.borderLeftStyle !== 'none';
      const pr = px(ps.borderRightWidth) > 0 && ps.borderRightStyle !== 'none';
      if (bar || ((pl || pr) && h > 8)) found.push(`${name(el)}${pseudo} bar`);
    }
  }
  // no visible vertical strip either: side-by-side panes, the inspector and the two dock panes meet directly
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg, canvas, .sheet-host, .gl-overlay, .doc-frame') || !el.getClientRects().length) continue;
    const r = el.getBoundingClientRect();
    const bg = getComputedStyle(el).backgroundColor;
    if (r.width > 0 && r.width <= 6 && r.height > 40 && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') found.push(`${name(el)} vertical strip ${r.width}px`);
  }
  const shown = (el) => el && el.getClientRects().length && getComputedStyle(el).display !== 'none' && !el.hidden;
  const rect = (el) => el.getBoundingClientRect();
  const gap = (a, b, label) => { if (shown(a) && shown(b)) { const g = rect(b).left - rect(a).right; if (Math.abs(g) > 0.5) found.push(`${label} gap ${g.toFixed(1)}px`); } };
  const panes = [...document.querySelectorAll('#views > .pane')].filter(shown);
  for (const a of panes) for (const b of panes) if (a !== b && Math.abs(rect(a).top - rect(b).top) < 1 && rect(b).left > rect(a).left) gap(a, b, `${a.id}|${b.id}`);
  const insp = document.getElementById('inspector');
  if (shown(insp) && getComputedStyle(insp).position !== 'fixed') gap(document.getElementById('views'), insp, 'views|inspector');
  const dock = [...document.querySelectorAll('#dock > .dock-pane')];
  if (dock.length === 2 && Math.abs(rect(dock[0]).top - rect(dock[1]).top) < 1) gap(dock[0], dock[1], 'dock panes');
  return [...new Set(found)];
});

const pumpSymbol = () => page.locator('#pane-pid [data-occurrence-id="sym-pump"] [data-role="symbol-body"] circle').first();
const turns = async () => (await snap()).assistant.turns;
async function ask(question, { waitDone = true } = {}) {
  const prev = (await turns()).at(-1);
  await page.fill('#asst-input', question);
  await page.click('#asst-send');
  return waitFor(async () => { const l = (await turns()).at(-1); return l && (!prev || l.request_id !== prev.request_id) && (!waitDone || l.phase !== 'running') ? l : null; }, { timeout: 30000, message: `turn for ${question}` });
}

try {
  await page.goto(url);
  await page.evaluate((k) => localStorage.removeItem(k), STORAGE_KEY);
  await page.reload();

  await step('one floating Ask launcher in fixed chrome, default bottom-right; no toolbar or focus-bar Ask, no assistant column', async () => {
    await settled(0);
    const s = await snap();
    const dom = await page.evaluate(() => ({
      asks: document.querySelectorAll('[id^="btn-ask"]').length,
      inToolbar: !!document.querySelector('.toolbar #btn-ask'),
      inFocusBar: !!document.querySelector('#focus-bar #btn-ask, #btn-ask-focus'),
      position: getComputedStyle(document.getElementById('btn-ask')).position,
      dialog: ['role', 'aria-modal', 'aria-labelledby'].map((a) => document.getElementById('assistant').getAttribute(a)),
      inWorkspace: !!document.querySelector('#workspace #assistant'),
      columns: getComputedStyle(document.getElementById('workspace')).gridTemplateColumns.split(' ').length,
      aria: ['aria-controls', 'aria-expanded', 'aria-haspopup', 'aria-describedby'].map((a) => document.getElementById('btn-ask').getAttribute(a)),
    }));
    if (dom.asks !== 1 || dom.inToolbar || dom.inFocusBar || dom.position !== 'fixed' || dom.inWorkspace || dom.columns !== 2) throw new Error(JSON.stringify(dom));
    if (dom.dialog.join() !== 'dialog,false,asst-title' || dom.aria.join() !== 'assistant,false,dialog,ask-fab-help') throw new Error(JSON.stringify(dom));
    const b = await launcherBox();
    if (Math.round(1600 - b.right) !== 16 || Math.round(1000 - b.bottom) !== 16) throw new Error(`not bottom-right: ${JSON.stringify(b)}`);
    if (s.askLauncher.stored) throw new Error('a position was stored without a move');
    await shot('01-launcher-default');
    return { launcher: b, dom };
  });

  let nav0;
  await step('click opens a compact input-first bubble beside the launcher; input focused; panes and views unchanged', async () => {
    nav0 = await restingNavigation();
    const rects = await paneRects();
    const canvases = await page.evaluate(() => document.querySelectorAll('canvas.gl').length);
    await page.click('#btn-ask');
    const s = await waitFor(async () => { const x = await snap(); return x.assistant.open ? x : null; }, { message: 'open' });
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    const g = await bubbleGeometry();
    const thread = await page.evaluate(() => document.querySelector('#assistant .asst-thread').hidden);
    const expanded = await page.getAttribute('#btn-ask', 'aria-expanded');
    if (focused !== 'asst-input' || expanded !== 'true' || !thread) throw new Error(JSON.stringify({ focused, expanded, thread }));
    if (!g.inside || g.overlapsLauncher || g.side !== 'above' || g.width > 680 || g.height > 420) throw new Error(JSON.stringify(g));
    await page.waitForTimeout(400);
    if (JSON.stringify(await paneRects()) !== JSON.stringify(rects)) throw new Error('a pane changed size');
    if (JSON.stringify(await restingNavigation()) !== JSON.stringify(nav0)) throw new Error('a view moved');
    if (await page.evaluate(() => document.querySelectorAll('canvas.gl').length) !== canvases) throw new Error('canvas count changed');
    await shot('02-bubble-compact');
    return { bubble: g, placement: s.assistant.placement };
  });

  await step('a drag across the 3D pane moves the launcher only: no click/open, no pan, no selection, no request; position stored', async () => {
    await page.keyboard.press('Escape');
    const n = posts.length;
    const before = await snap();
    nav0 = await restingNavigation();
    const b = await launcherBox();
    const view = await page.evaluate(() => { const r = document.querySelector('#view-3d .pane-content').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
    await page.mouse.move(b.left + b.width / 2, b.top + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(view.x, view.y, { steps: 14 });
    await page.mouse.move(view.x + 40, view.y + 30, { steps: 4 });
    await page.mouse.up();
    await page.waitForTimeout(500);
    const s = await snap();
    const a = await launcherBox();
    if (s.assistant.open) throw new Error('the drag opened the bubble');
    if (Math.abs(a.left + a.width / 2 - (view.x + 40)) > 2 || Math.abs(a.top + a.height / 2 - (view.y + 30)) > 2) throw new Error(`launcher did not follow: ${JSON.stringify(a)} vs ${JSON.stringify(view)}`);
    if (JSON.stringify(await restingNavigation()) !== JSON.stringify(nav0)) throw new Error('the drag moved a view');
    if (JSON.stringify(s.selection) !== JSON.stringify(before.selection)) throw new Error('the drag changed the selection');
    if (postsSince(n).length) throw new Error(`requests sent: ${JSON.stringify(postsSince(n))}`);
    const stored = await page.evaluate((k) => localStorage.getItem(k), STORAGE_KEY);
    if (!s.askLauncher.stored || !stored) throw new Error('position not stored');
    await shot('03-launcher-dragged');
    // a press that moves under the threshold is still a click
    const c = await launcherBox();
    await page.mouse.move(c.left + c.width / 2, c.top + c.height / 2);
    await page.mouse.down();
    await page.mouse.move(c.left + c.width / 2 + 2, c.top + c.height / 2 + 1);
    await page.mouse.up();
    if (!(await snap()).assistant.open) throw new Error('a 2 px press did not open');
    const g = await bubbleGeometry();
    if (!g.inside || g.overlapsLauncher) throw new Error(JSON.stringify(g));
    await shot('04-bubble-at-moved-launcher');
    // dragging while open carries the bubble along
    const d0 = await launcherBox();
    await page.mouse.move(d0.left + d0.width / 2, d0.top + d0.height / 2);
    await page.mouse.down();
    await page.mouse.move(d0.left + d0.width / 2 - 200, d0.top + d0.height / 2 + 120, { steps: 8 });
    await page.mouse.up();
    const g2 = await bubbleGeometry();
    const s2 = await snap();
    if (!s2.assistant.open || !g2.inside || g2.overlapsLauncher || (g2.left === g.left && g2.top === g.top)) throw new Error(JSON.stringify({ g, g2, open: s2.assistant.open }));
    await page.keyboard.press('Escape');
    return { launcher: a, stored: JSON.parse(stored), sidesOpen: [g.side, g2.side] };
  });

  await step('stored position survives reload; resize clamps it into the window and restores it', async () => {
    const before = await launcherBox();
    await page.reload();
    await settled(0);
    const after = await launcherBox();
    if (Math.abs(after.left - before.left) > 1 || Math.abs(after.top - before.top) > 1) throw new Error(`reload moved it: ${JSON.stringify([before, after])}`);
    const sizes = [];
    for (const [w, hgt] of [[900, 760], [700, 420], [1600, 1000]]) {
      await page.setViewportSize({ width: w, height: hgt });
      await page.waitForTimeout(250);
      const b = await launcherBox();
      sizes.push({ w, h: hgt, left: Math.round(b.left), top: Math.round(b.top) });
      if (b.left < 0 || b.top < 0 || b.right > w || b.bottom > hgt) throw new Error(`outside ${w}×${hgt}: ${JSON.stringify(b)}`);
    }
    const back = await launcherBox();
    if (Math.abs(back.left - before.left) > 1 || Math.abs(back.top - before.top) > 1) throw new Error(`not restored after resize: ${JSON.stringify([before, back])}`);
    // dragged against an edge: clamped inside the viewport
    await page.mouse.move(back.left + back.width / 2, back.top + back.height / 2);
    await page.mouse.down();
    await page.mouse.move(-200, -200, { steps: 6 });
    await page.mouse.up();
    const edge = await launcherBox();
    if (Math.round(edge.left) !== 16 || Math.round(edge.top) !== 16) throw new Error(`edge clamp ${JSON.stringify(edge)}`);
    await page.click('#btn-ask');
    const g = await bubbleGeometry();
    if (!g.inside || g.overlapsLauncher || g.side !== 'below') throw new Error(JSON.stringify(g));
    await shot('05-launcher-top-left-bubble-below');
    await page.keyboard.press('Escape');
    return { sizes, edge, bubble: g };
  });

  await step('keyboard placement: Shift+Arrow moves the focused launcher, Shift+Home resets; typing in the bubble never moves it', async () => {
    await page.focus('#btn-ask');
    await page.keyboard.press('Shift+Home');
    const home = await launcherBox();
    if (Math.round(1600 - home.right) !== 16 || Math.round(1000 - home.bottom) !== 16) throw new Error(`reset ${JSON.stringify(home)}`);
    await page.keyboard.press('Shift+ArrowLeft');
    await page.keyboard.press('Shift+ArrowLeft');
    await page.keyboard.press('Shift+ArrowUp');
    const moved = await launcherBox();
    if (Math.round(home.left - moved.left) !== 48 || Math.round(home.top - moved.top) !== 24) throw new Error(`nudge ${JSON.stringify([home, moved])}`);
    const ring = await page.evaluate(() => { const b = document.getElementById('btn-ask'); const cs = getComputedStyle(b); return { focusVisible: b.matches(':focus-visible'), outline: `${cs.outlineStyle} ${cs.outlineWidth}` }; });
    if (!ring.focusVisible || !/solid 2px/.test(ring.outline)) throw new Error(JSON.stringify(ring));
    await page.screenshot({ path: path.join(dir, '06-launcher-focus-ring.png'), clip: { x: moved.left - 30, y: moved.top - 30, width: moved.width + 60, height: moved.height + 60 } });
    if ((await snap()).assistant.open) throw new Error('placement keys opened the bubble');
    await page.keyboard.press('Enter');
    if (!(await snap()).assistant.open) throw new Error('Enter on the launcher did not open');
    await page.keyboard.type('pump');
    await page.keyboard.press('Shift+ArrowLeft');
    await page.keyboard.press('Shift+ArrowUp');
    const still = await launcherBox();
    if (still.left !== moved.left || still.top !== moved.top) throw new Error('typing moved the launcher');
    await page.keyboard.press('Escape');
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (focused !== 'btn-ask') throw new Error(`focus went to ${focused}`);
    await page.keyboard.press('Shift+Home');
    return { nudged: [home.left - moved.left, home.top - moved.top], ring };
  });

  await step('outside click and Escape close without losing the draft; a running question survives dismissal; Cancel is separate', async () => {
    await page.click('#btn-ask');
    await page.fill('#asst-input', 'draft kept across dismissal');
    // an outside click on the sheet: closes, does not restore focus to the launcher
    const box = await pumpSymbol().boundingBox();
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
    let s = await snap();
    if (s.assistant.open || s.assistant.draftLength !== 'draft kept across dismissal'.length) throw new Error(JSON.stringify(s.assistant));
    if (s.selection.join() !== 'ent-pump') throw new Error('the click did not reach the sheet underneath');
    await page.click('#btn-ask');
    if (await page.inputValue('#asst-input') !== 'draft kept across dismissal') throw new Error('draft lost');
    // an answer expands the bubble; it stays inside the window
    await ask('What is upstream and downstream of it?');
    const expanded = await bubbleGeometry();
    if (!expanded.inside || expanded.overlapsLauncher) throw new Error(JSON.stringify(expanded));
    await shot('07-bubble-answer');
    // slow question, dismissed by an outside click: still running; reopen shows it; Cancel cancels it
    const t = await ask('slow: what is P-101?', { waitDone: false });
    await page.mouse.click(40, 600);
    s = await snap();
    if (s.assistant.open || !s.assistant.pending || s.assistant.pending.request_id !== t.request_id) throw new Error(`dismissal cancelled: ${JSON.stringify(s.assistant.pending)}`);
    if (postsSince(0, '/api/cancel-ask').some((p) => p.body.request_id === t.request_id)) throw new Error('dismissal sent a cancel');
    await page.click('#btn-ask');
    if (!(await snap()).assistant.pending) throw new Error('request lost on reopen');
    await shot('08-running-after-reopen');
    await page.click('#assistant .asst-turn:last-child button:has-text("Cancel question")');
    const done = await waitFor(async () => { const l = (await turns()).at(-1); return l.phase !== 'running' ? l : null; }, { timeout: 20000, message: 'cancelled' });
    if (!postsSince(0, '/api/cancel-ask').some((p) => p.body.request_id === t.request_id)) throw new Error('no cancel request');
    // Escape inside the input closes and returns focus to the launcher
    await page.focus('#asst-input');
    await page.keyboard.press('Escape');
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if ((await snap()).assistant.open || focused !== 'btn-ask') throw new Error(`Escape: focus ${focused}`);
    return { cancelled: done.status || done.failure, expanded };
  });

  await step('dock Feedback opens Change in the same bubble; the one composer and its proposal survive close and reopen; nothing applies on close', async () => {
    await page.evaluate(() => { document.querySelector('#panel-feedback textarea').dataset.probe = 'one-instance'; });
    await page.click('#change-tabs button[data-tab="feedback"]');
    let s = await waitFor(async () => { const x = await snap(); return x.assistant.open && x.assistant.mode === 'change' ? x : null; }, { message: 'change open' });
    const tabsKeyboard = await page.evaluate(() => [...document.querySelectorAll('#assistant [role="tab"]')].map((t) => `${t.id}:${t.getAttribute('aria-selected')}:${t.tabIndex}`));
    await page.fill('#panel-feedback textarea', 'Set motor length of P-101 to 0.42 m');
    await page.click('#panel-feedback .composer > button');
    await page.waitForSelector('#panel-feedback .proposal .verdict.yes');
    const proposal = (await snap()).composer.proposal;
    const g = await bubbleGeometry();
    if (!g.inside) throw new Error(JSON.stringify(g));
    await shot('09-change-proposal');
    const rev = (await snap()).revision;
    await page.keyboard.press('Escape');
    await page.mouse.click(40, 600); // and an outside click while closed does nothing
    await page.click('#btn-ask');
    await page.click('#asst-tab-change');
    s = await snap();
    const probe = await page.evaluate(() => document.querySelector('#panel-feedback textarea').dataset.probe);
    if (s.revision !== rev || s.composer.proposal?.id !== proposal.id || probe !== 'one-instance') throw new Error(JSON.stringify({ rev: s.revision, p: s.composer.proposal, probe }));
    // keyboard-operable mode tabs
    await page.focus('#asst-tab-change');
    await page.keyboard.press('ArrowLeft');
    if ((await snap()).assistant.mode !== 'ask') throw new Error('arrow keys did not switch mode');
    await page.keyboard.press('Escape');
    return { proposal: proposal.id, tabsKeyboard, bubble: g };
  });

  await step('matrix: normal/focus × two/four panes × 900/1280/1600 — bubble inside, launcher, close and exit reachable, no pane change, no overflow', async () => {
    const results = [];
    for (const w of [900, 1280, 1600]) {
      await page.setViewportSize({ width: w, height: w === 900 ? 760 : 900 });
      for (const n of [2, 4]) {
        for (const focus of [false, true]) {
          if ((await snap()).assistant.open) await page.keyboard.press('Escape');
          const s0 = await snap();
          if (s0.layout.focus !== focus) await page.evaluate((on) => document.querySelector(on ? '#btn-focus' : '#btn-exit-focus').click(), focus);
          if (!focus) await page.evaluate((k) => document.querySelector(`#layout-seg button[data-layout="${k}"]`).click(), n);
          await page.waitForTimeout(300);
          const rects = await paneRects();
          await page.click('#btn-ask');
          await page.waitForTimeout(250);
          const g = await bubbleGeometry();
          const r = await reach();
          const same = JSON.stringify(await paneRects()) === JSON.stringify(rects);
          const s = await snap();
          results.push({ w, n, focus, visible: s.layout.visible.length, ...g, reach: r, paneSizesUnchanged: same });
          await shot(`10-${w}-${n}p-${focus ? 'focus' : 'normal'}`);
        }
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    const bad = results.filter((x) => !x.inside || !x.launcherInside || x.overflowX || !x.paneSizesUnchanged || !x.reach.launcher || !x.reach.close || !x.reach.input || x.reach.exitFocus === false || x.visible !== (x.focus ? x.n : x.n));
    if (bad.length) throw new Error(JSON.stringify(bad).slice(0, 2500));
    if ((await snap()).layout.focus) await page.click('#btn-exit-focus');
    return results.map((x) => `${x.w}/${x.n}/${x.focus ? 'focus' : 'normal'} ${x.width}×${x.height} ${x.side}`);
  });

  await step('no decorative vertical rules in the rendered UI: normal, focus, four panes, Index selection, Move panel, bubble with answers and proposal', async () => {
    const found = {};
    if ((await snap()).assistant.open) await page.keyboard.press('Escape');
    await page.evaluate(() => document.querySelector('#layout-seg button[data-layout="4"]').click());
    await page.click('#view-p3 .pane-tabs button[data-doc="index"]');
    await page.waitForSelector('#view-p3 tr[data-id="ent-pump"]');
    await page.click('#view-p3 tr[data-id="ent-pump"]');
    if (await page.locator('#p0-move').count()) await page.click('#p0-move');
    await page.waitForTimeout(400);
    found.normalFour = await verticalRules();
    await shot('11-normal-four-index-move');
    await page.click('#btn-ask');
    found.bubbleAsk = await verticalRules();
    await page.click('#asst-tab-change');
    found.bubbleChange = await verticalRules();
    await page.keyboard.press('Escape');
    await page.click('#btn-focus');
    found.focus = await verticalRules();
    await page.click('#btn-exit-focus');
    await page.evaluate(() => document.querySelector('#layout-seg button[data-layout="2"]').click());
    if (await page.locator('#p0-move[aria-pressed="true"]').count()) await page.click('#p0-move');
    // the column splitter still drags although it draws nothing
    const sp = await page.evaluate(() => { const r = document.getElementById('splitter').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, cursor: getComputedStyle(document.getElementById('splitter')).cursor }; });
    const left0 = (await paneRects())[0];
    await page.mouse.move(sp.x, sp.y);
    await page.mouse.down();
    await page.mouse.move(sp.x - 120, sp.y, { steps: 6 });
    await page.mouse.up();
    const left1 = (await paneRects())[0];
    if (left0 === left1 || sp.cursor !== 'col-resize' || sp.w < 6) throw new Error(`splitter did not resize: ${JSON.stringify({ sp, left0, left1 })}`);
    found.splitterDrag = [];
    const all = Object.entries(found).filter(([, v]) => v.length);
    if (all.length) throw new Error(JSON.stringify(found));
    return found;
  });

  await step('storage that refuses reads and writes: the launcher still works for the page, no page errors', async () => {
    const p2 = await context.newPage();
    const p2problems = watch(p2);
    await p2.addInitScript(() => {
      const deny = () => { throw new DOMException('denied', 'SecurityError'); };
      Object.defineProperty(window, 'localStorage', { get: deny, configurable: true });
    });
    await installStub(p2);
    await p2.setViewportSize({ width: 1280, height: 800 });
    await p2.goto(url);
    await waitFor(async () => { const x = await snapshot(p2); return x && x.revision != null ? x : null; }, { message: 'loaded' });
    const b = await p2.evaluate(() => { const r = document.getElementById('btn-ask').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height }; });
    if (Math.round(1280 - b.left - b.width) !== 16) throw new Error(`not default: ${JSON.stringify(b)}`);
    await p2.mouse.move(b.left + b.width / 2, b.top + b.height / 2);
    await p2.mouse.down();
    await p2.mouse.move(400, 300, { steps: 6 });
    await p2.mouse.up();
    const s = await snapshot(p2);
    if (s.askLauncher.stored || s.assistant.open || Math.abs(s.askLauncher.left + b.width / 2 - 400) > 2) throw new Error(JSON.stringify(s.askLauncher));
    await p2.click('#btn-ask');
    if (!(await snapshot(p2)).assistant.open) throw new Error('did not open');
    await p2.close();
    if (p2problems.length) throw new Error(p2problems.join(' | '));
    return { launcher: s.askLauncher };
  });

  await step('no page errors or console errors', async () => {
    if (problems.length) throw new Error(problems.join(' | '));
  });
} finally {
  report.problems = problems;
  report.posts = posts.map((p) => p.path);
  report.passed = report.steps.every((s) => s.ok) && problems.length === 0;
  report.finished_at = new Date().toISOString();
  fs.writeFileSync(path.join(dir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(`${report.steps.filter((s) => s.ok).length}/${report.steps.length} passed · problems ${problems.length} · ${path.relative(ROOT, dir)}`);
  await browser.close().catch(() => {});
  service.kill('SIGTERM');
}
process.exit(report.passed ? 0 : 1);
