// End-to-end browser workflow check. STATUS: written but NOT EXECUTED in the session that
// wrote it — browser automation was not permitted there. Run it only where driving a
// headless browser is allowed. It never touches the shared demo: it creates a fresh copy of
// the fixture under artifacts/ui-review/e2e-<stamp>/ and starts its own service on a free port.
//
// Usage: node tests/browser/workflow.e2e.mjs
// Output: artifacts/ui-review/e2e-<stamp>/{*.png, report.json}; exit code 1 on any failed step.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17); // to the second: reruns never collide
const dir = path.join(REVIEW_DIR, `e2e-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const report = { started_at: new Date().toISOString(), dir, steps: [] };

const sample = spawnSync(PY, ['-m', 'skid.cli', 'sample', path.join(dir, 'project.json')], { cwd: ROOT, encoding: 'utf8' });
if (sample.status !== 0) throw new Error(`sample failed: ${sample.stderr}`);

const service = spawn(PY, ['-m', 'skid.interactive', '--project', path.join(dir, 'project.json'), '--port', '0', '--quiet'], { cwd: ROOT });
const url = await new Promise((resolve, reject) => {
  let out = '';
  const timer = setTimeout(() => reject(new Error(`service did not start: ${out}`)), 30000);
  service.stdout.on('data', (chunk) => {
    out += chunk;
    const m = /at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(out);
    if (m) { clearTimeout(timer); resolve(m[1]); }
  });
  service.on('exit', (code) => reject(new Error(`service exited ${code}: ${out}`)));
});
report.url = url;

const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const problems = watch(page);
const snap = () => snapshot(page);
const shot = (name) => page.screenshot({ path: path.join(dir, `${name}.png`) });

async function step(name, fn) {
  const t0 = Date.now();
  try {
    const detail = await fn();
    report.steps.push({ name, ok: true, ms: Date.now() - t0, detail: detail ?? null });
  } catch (error) {
    report.steps.push({ name, ok: false, ms: Date.now() - t0, error: String(error && error.stack ? error.stack : error) });
    await shot(`failed-${report.steps.length}`).catch(() => {});
  }
}
const synchronizedAt = (revision) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === revision && s.sync === 'synchronized' && s.viewer && s.viewer.source && s.viewer.source.source_revision === revision ? s : null;
}, { timeout: 120000, message: `synchronized r${revision}` });

/** The composer lives in the assistant's Change section; the dock's Feedback entry opens it. */
async function openChange() {
  const s = await snap();
  if (s && s.assistant && s.assistant.open && s.assistant.mode === 'change') return;
  await page.click('#change-tabs button[data-tab="feedback"]');
}
/** The floating assistant may lie over the toolbar or a pane: close it (nothing is cancelled or applied). */
async function closeAssistant() {
  if ((await snap()).assistant.open) await page.click('#assistant .asst-close');
}

/** While a build runs, sample badges: "Current" must only ever describe installed content of the current revision. */
async function watchBadges(ms) {
  const bad = [];
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const s = await snap();
    if (s && /^Current/.test(s.badges.view3d) && s.viewer.source && s.viewer.source.source_revision !== s.revision) bad.push(s.badges.view3d);
    if (s && /^Synchronized/i.test(s.badges.sync) && s.sync !== 'synchronized') bad.push(s.badges.sync);
    await new Promise((r) => setTimeout(r, 80));
  }
  return bad;
}

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await step('first frame: synchronized r0 with scene and P&ID installed', async () => {
    const s = await synchronizedAt(0);
    if (!/^Current · r0/.test(s.badges.view3d)) throw new Error(`3D badge ${s.badges.view3d}`);
    await shot('01-first-frame');
    return { components: s.viewer.components, labels: s.viewer.labelsVisible.length };
  });

  await step('A07 P&ID → 3D: clicking the pump symbol highlights all pump components', async () => {
    const box = await page.locator('#pane-pid [data-occurrence-id="sym-pump"] [data-role="symbol-body"] circle').first().boundingBox();
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
    const s = await waitFor(async () => { const x = await snap(); return x.selection.join() === 'ent-pump' && x.viewer.selected.length ? x : null; }, { message: 'pump selected' });
    if (!s.viewer.selected.includes('ent-pump#body') || !s.pidSelected.includes('sym-pump')) throw new Error(JSON.stringify(s.viewer.selected));
    await shot('02-pid-to-3d');
    return { components: s.viewer.selected.length };
  });

  await step('A07 3D → P&ID: clicking the tank mesh highlights its P&ID symbol', async () => {
    const p = await page.evaluate(() => window.skidWorkspace.screenPointOf('ent-tank#body'));
    await page.mouse.click(p.x, p.y);
    const s = await waitFor(async () => { const x = await snap(); return x.selection.join() === 'ent-tank' && x.pidSelected.includes('sym-tank') ? x : null; }, { message: 'tank selected' });
    await shot('03-3d-to-pid');
    return { pidSelected: s.pidSelected };
  });

  let pidView;
  let camera;
  await step('reviewable proposal → apply → live rebuild keeps camera, paper view and badges honest', async () => {
    // zoom first: a click on the sheet tools is an outside click, which would close the floating bubble
    await page.click('#btn-sheet-in');
    await page.click('#btn-sheet-in');
    pidView = (await snap()).pidView;
    camera = (await snap()).viewer.camera;
    await openChange();
    await page.fill('#panel-feedback textarea', 'Set motor length of P-101 to 0.42 m');
    await page.click('#panel-feedback button:has-text("Review")');
    await page.waitForSelector('.proposal .verdict.yes');
    const payload = await page.textContent('.proposal pre.payload');
    if (!payload.includes('"motor_length"') || !payload.includes('0.42')) throw new Error(payload);
    await page.click('.proposal button:has-text("Apply")');
    const bad = await watchBadges(2500);
    const s = await synchronizedAt(1);
    if (bad.length) throw new Error(`misleading badges during build: ${bad.slice(0, 3).join(' | ')}`);
    if (JSON.stringify(s.pidView) !== JSON.stringify(pidView)) throw new Error('P&ID zoom/pan changed across replacement');
    if (JSON.stringify(s.viewer.camera) !== JSON.stringify(camera)) throw new Error(`camera changed across replacement: ${JSON.stringify(camera)} -> ${JSON.stringify(s.viewer.camera)}`);
    await shot('04-after-proposal');
    return { sceneStats: s.sceneStats };
  });

  await step('inspector rename, then undo restores the tag', async () => {
    await closeAssistant();
    // the service's selection answer is held until the tag is typed, so the inspector re-renders under a
    // draft deterministically: the draft and its Rename check must survive that re-render
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route('**/api/selection', async (route) => { await held; await route.continue(); }, { times: 1 });
    const box = await page.locator('#pane-pid [data-occurrence-id="sym-pump"] [data-role="symbol-body"] circle').first().boundingBox();
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
    await page.waitForSelector('#insp-body input[data-field="tag:ent-pump"]');
    await page.fill('#insp-body input[data-field="tag:ent-pump"]', 'P-201');
    await page.evaluate(() => { window.__tagField = document.querySelector('#insp-body input[data-field="tag:ent-pump"]'); });
    release();
    await waitFor(() => page.evaluate(() => {
      const el = document.querySelector('#insp-body input[data-field="tag:ent-pump"]');
      return !!el && el !== window.__tagField;
    }), { message: 'inspector re-rendered after the selection answer' });
    if ((await page.inputValue('#insp-body input[data-field="tag:ent-pump"]')) !== 'P-201') throw new Error('the typed tag was lost on re-render');
    if (await page.locator('#insp-body button:has-text("Rename")').isDisabled()) throw new Error('Rename disabled after a re-render kept the draft');
    await page.click('#insp-body button:has-text("Rename")');
    await synchronizedAt(2);
    const pid = await page.textContent('#pane-pid svg');
    if (!pid.includes('P-201')) throw new Error('P&ID does not show P-201');
    await page.click('#btn-undo');
    await synchronizedAt(3);
    if (!(await page.textContent('#pane-pid svg')).includes('P-101')) throw new Error('undo did not restore P-101 on the P&ID');
    await shot('05-after-undo');
  });

  await step('save and reopen keep the revision and history', async () => {
    await page.click('#btn-save');
    await page.waitForSelector('.toast:has-text("Saved revision 3")');
    await page.click('#btn-reopen');
    await page.waitForSelector('.toast:has-text("Reopened at revision 3")');
  });

  await step('cancel a running build: never synchronized, badge not current', async () => {
    await openChange();
    await page.fill('#panel-feedback textarea', 'Set motor length of P-101 to 0.40 m');
    await page.click('#panel-feedback button:has-text("Review")');
    await page.waitForSelector('.proposal .verdict.yes');
    await page.click('.proposal button:has-text("Apply")');
    await closeAssistant(); // a tall bubble can lie over the toolbar's Cancel
    await page.waitForSelector('#btn-cancel:not([hidden])', { timeout: 10000 });
    await page.click('#btn-cancel');
    const s = await waitFor(async () => { const x = await snap(); return x.build && x.build.status === 'cancelled' ? x : null; }, { message: 'cancelled' });
    if (s.sync === 'synchronized' || /^Current/.test(s.badges.view3d)) throw new Error(`${s.sync} / ${s.badges.view3d}`);
    await shot('06-cancelled');
    return { sync: s.sync, badge: s.badges.view3d };
  });

  await step('unsupported request is reported as unsupported with no apply', async () => {
    await openChange();
    await page.fill('#panel-feedback textarea', 'Delete the pump');
    await page.click('#panel-feedback button:has-text("Review")');
    await page.waitForSelector('.proposal .verdict.no');
    if (await page.locator('.proposal button:has-text("Apply")').count()) throw new Error('apply offered for an unsupported request');
  });

  await step('stale command JSON is refused before sending', async () => {
    await page.click('#change-tabs button[data-tab="command"]');
    await page.selectOption('#panel-command select', 'rename_entity');
    await page.click('#panel-command button:has-text("Fill example")');
    const text = await page.inputValue('#panel-command textarea');
    const cmd = JSON.parse(text);
    cmd.expected_revision -= 1;
    await page.fill('#panel-command textarea', JSON.stringify(cmd));
    await page.click('#panel-command button:has-text("Review")');
    if (!(await page.isDisabled('#panel-command button:has-text("Apply command")'))) throw new Error('stale command can be applied');
  });
} finally {
  report.problems = problems;
  report.finished_at = new Date().toISOString();
  fs.writeFileSync(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close();
  service.kill('SIGTERM');
}
for (const s of report.steps) console.log(`${s.ok ? 'ok  ' : 'FAIL'} ${s.name}${s.ok ? '' : `\n     ${s.error.split('\n')[0]}`}`);
console.log(`console/page problems: ${problems.length}; report ${path.join(dir, 'report.json')}`);
process.exitCode = report.steps.every((s) => s.ok) && !problems.length ? 0 : 1;
