// Engineering-mode composer, composed equipment and generic inspector fields, end to end in Chromium.
//
// Uses a fresh copy of the fixture under artifacts/ui-review/engineering-<stamp>/ and its own service on
// a free port, started by tests/browser/fake_planner_service.py with a SCRIPTED FAKE planner (no
// language model). Everything after the fake plan is real: schema check, translation into one batch,
// core dry-run, apply, persistence and the real generators. It never touches the shared demo service.
// Passing here says nothing about the real planner; GPT runs that separately.
//
// Usage: node tests/browser/engineering.e2e.mjs
// Output: artifacts/ui-review/engineering-<stamp>/{*.png, report.json}; exit code 1 on any failed step.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17); // to the second: reruns never collide
const dir = path.join(REVIEW_DIR, `engineering-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const report = { started_at: new Date().toISOString(), dir, planner: 'SCRIPTED FAKE (no language model)', steps: [] };

const sample = spawnSync(PY, ['-m', 'skid.cli', 'sample', path.join(dir, 'project.json')], { cwd: ROOT, encoding: 'utf8' });
if (sample.status !== 0) throw new Error(`sample failed: ${sample.stderr}`);

const service = spawn(PY, [path.join(ROOT, 'tests', 'browser', 'fake_planner_service.py'), '--project', path.join(dir, 'project.json'), '--port', '0'], { cwd: ROOT });
let serviceLog = '';
service.stderr.on('data', (chunk) => { serviceLog += chunk; });
const url = await new Promise((resolve, reject) => {
  let out = '';
  const timer = setTimeout(() => reject(new Error(`service did not start: ${out}${serviceLog}`)), 60000);
  service.stdout.on('data', (chunk) => {
    out += chunk;
    const m = /at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(out);
    if (m) { clearTimeout(timer); resolve(m[1]); }
  });
  service.on('exit', (code) => reject(new Error(`service exited ${code}: ${out}${serviceLog}`)));
});
report.url = url;

const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const problems = watch(page);
const snap = () => snapshot(page);
const shot = (name) => page.screenshot({ path: path.join(dir, `${name}.png`) });
const serviceState = () => page.evaluate(async () => (await fetch('/api/state', { cache: 'no-store' })).json());

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
}, { timeout: 240000, message: `synchronized r${revision}` });
const buildSettled = (revision) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === revision && s.build && s.build.status !== 'queued' && s.build.status !== 'running'
    && s.pidSource && s.pidSource.revision === revision ? s : null;
}, { timeout: 240000, message: `build of r${revision} settled with its P&ID installed` });

/** The composer lives in the assistant's Change section; the dock's Feedback entry opens it. */
async function openChange() {
  const s = await snap();
  if (s && s.assistant && s.assistant.open && s.assistant.mode === 'change') return;
  await page.click('#change-tabs button[data-tab="feedback"]');
}

let lastProposal = null;
async function requestPlan(text) {
  await openChange();
  await page.fill('#panel-feedback textarea', text);
  await page.click('#panel-feedback .composer > button');
  const s = await waitFor(async () => {
    const x = await snap();
    return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null;
  }, { timeout: 60000, message: `a proposal for "${text}"` });
  lastProposal = s.composer.proposal.id;
  return s;
}
const applyCount = () => page.locator('#panel-feedback .proposal button:has-text("Apply")').count();
/** The floating assistant may lie over pane headers or the inspector: close it (nothing is cancelled or applied). */
async function closeAssistant() {
  if ((await snap()).assistant.open) await page.click('#assistant .asst-close');
}

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await step('first frame synchronized at r0; quick mode is the default', async () => {
    await synchronizedAt(0);
    await openChange();
    const s = await snap();
    if (s.composer.mode !== 'quick') throw new Error(`default mode ${s.composer.mode}`);
    if ((await page.textContent('#panel-feedback .composer > button')).trim() !== 'Review') throw new Error('quick submit label changed');
  });

  await step('engineering mode is offered by the service and relabels the composer', async () => {
    await page.click('#panel-feedback .mode-seg button[data-mode="engineering"]');
    const s = await snap();
    if (s.composer.mode !== 'engineering') throw new Error('mode did not switch');
    const label = (await page.textContent('#panel-feedback .composer > button')).trim();
    if (label !== 'Plan') throw new Error(`submit label ${label}`);
    const intro = await page.textContent('#panel-feedback > p.limitation');
    if (!/current project data.*Claude Max.*external service/s.test(intro)) throw new Error(`engineering intro: ${intro}`);
    await shot('01-engineering-mode');
  });

  await step('questions come back as questions with no apply', async () => {
    const s = await requestPlan('fake-questions: put a drum somewhere');
    if (s.composer.review.verdict !== 'questions' || s.composer.review.canApply) throw new Error(JSON.stringify(s.composer.review));
    if (await page.locator('.proposal .prop-block.questions li').count() !== 2) throw new Error('questions not listed');
    if (await applyCount()) throw new Error('apply offered for questions');
    await shot('02-questions');
    await page.click('.proposal button:has-text("Answer in a new request")');
    const text = await page.inputValue('#panel-feedback textarea');
    if (!/^fake-questions: put a drum somewhere\n\n.*self-contained.*\nline: \ntag: $/s.test(text)) throw new Error(`follow-up text: ${text}`);
  });

  await step('actual eight questions: ID slots fit the budget, and empty or over-limit requests are never sent', async () => {
    let sent = 0;
    const count = (r) => { if (r.url().endsWith('/api/propose')) sent += 1; };
    page.on('request', count);
    try {
      const s = await requestPlan('fake-eight: assess what is missing to size TK-101 and P-101; ask, do not change');
      if (s.composer.review.verdict !== 'questions') throw new Error(JSON.stringify(s.composer.review));
      const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'browser', 'unit', 'fixtures', 'real-questions-8.json'), 'utf8'));
      const shown = await page.locator('.proposal .prop-block.questions .qid').allTextContents();
      if (JSON.stringify(shown) !== JSON.stringify(fixture.questions.map((q) => q.id))) throw new Error(`IDs shown: ${shown}`);
      await page.click('#panel-feedback .mode-seg button[data-mode="quick"]');
      await page.click('.proposal button:has-text("Answer in a new request")');
      let x = await snap();
      if (x.composer.mode !== 'engineering') throw new Error('Answer did not select engineering mode');
      const text = await page.inputValue('#panel-feedback textarea');
      if (!text.startsWith('fake-eight: assess what is missing to size TK-101 and P-101; ask, do not change\n\n')) throw new Error('original not kept');
      const r = x.composer.request;
      if (r.limit !== 2000 || r.length > 700 || r.emptySlots.length !== 8 || r.ok) throw new Error(JSON.stringify(r));
      if (!(await page.isDisabled('#panel-feedback .composer > button'))) throw new Error('empty scaffold can be sent');
      if (!/8 answer slots are empty/.test(await page.textContent('#panel-feedback .budget-note'))) throw new Error('no empty-slot note');
      await shot('02b-eight-question-follow-up');
      const before = sent;
      await page.focus('#panel-feedback textarea');
      await page.keyboard.press('Control+Enter');
      await page.keyboard.press('Meta+Enter');
      // answer every slot, then push it over the limit
      const answered = fixture.questions.reduce((t, q) => t.replace(`${q.id}: `, `${q.id}: unknown for now; keep it explicit.`), text);
      await page.fill('#panel-feedback textarea', answered);
      if (await page.isDisabled('#panel-feedback .composer > button')) throw new Error(`answered follow-up blocked: ${(await snap()).composer.request.message}`);
      await page.fill('#panel-feedback textarea', `${answered}\n${'details '.repeat(250)}`);
      x = await snap();
      if (x.composer.request.problem !== 'too_long' || !(await page.isDisabled('#panel-feedback .composer > button'))) throw new Error(JSON.stringify(x.composer.request));
      if (!/Over the 2000-character engineering limit/.test(await page.textContent('#panel-feedback .budget-note'))) throw new Error('no over-limit note');
      await page.keyboard.press('Control+Enter');
      // code points, not UTF-16 units: 2000 emoji fit, 2001 do not
      await page.fill('#panel-feedback textarea', '🙂'.repeat(2000));
      if (await page.isDisabled('#panel-feedback .composer > button')) throw new Error('2000 code points refused');
      await page.fill('#panel-feedback textarea', '🙂'.repeat(2001));
      if (!(await page.isDisabled('#panel-feedback .composer > button'))) throw new Error('2001 code points accepted');
      await page.keyboard.press('Control+Enter');
      // quick edit: 500 limit, Enter sends
      await page.click('#panel-feedback .mode-seg button[data-mode="quick"]');
      await page.fill('#panel-feedback textarea', 'x'.repeat(501));
      if ((await snap()).composer.request.limit !== 500 || !(await page.isDisabled('#panel-feedback .composer > button'))) throw new Error('quick limit not enforced');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(500);
      if (sent !== before) throw new Error(`${sent - before} refused request(s) reached the service`);
      // a valid follow-up is sent exactly once
      await page.click('#panel-feedback .mode-seg button[data-mode="engineering"]');
      await page.fill('#panel-feedback textarea', answered);
      const again = await requestPlan(answered);
      if (sent !== before + 1 || again.composer.review.verdict !== 'questions') throw new Error(`sent ${sent - before}`);
      return { followUpLength: r.length, answeredOk: true };
    } finally {
      page.off('request', count);
    }
  });

  await step('an unavailable planner is reported with no apply', async () => {
    const s = await requestPlan('fake-unavailable');
    if (s.composer.review.verdict !== 'unavailable' || await applyCount()) throw new Error(JSON.stringify(s.composer.review));
  });

  await step('planning keeps navigation live, and Cancel plan stops the planner', async () => {
    await page.fill('#panel-feedback textarea', 'fake-slow: take your time');
    await page.click('#panel-feedback .composer > button');
    await page.waitForSelector('#panel-feedback .planning:not([hidden])');
    const cancel = page.locator('#panel-feedback .planning button:has-text("Cancel plan")');
    await cancel.waitFor({ timeout: 15000 });
    // closing the floating assistant keeps the plan running; the views underneath are then free to use
    await closeAssistant();
    if (!(await snap()).composer.busy) throw new Error('closing the assistant stopped the planner');
    const before = (await snap()).pidView;
    await page.click('#btn-sheet-in');
    const box = await page.locator('#pane-pid [data-occurrence-id="sym-pump"] [data-role="symbol-body"] circle').first().boundingBox();
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
    const during = await waitFor(async () => { const x = await snap(); return x.selection.join() === 'ent-pump' ? x : null; }, { message: 'selection during planning' });
    if (!during.composer.busy) throw new Error('planner finished before the navigation check');
    if (JSON.stringify(during.pidView) === JSON.stringify(before)) throw new Error('sheet zoom did not respond while planning');
    const st = await serviceState();
    if (!st.planning || st.planning.active.length !== 1) throw new Error(`service reports ${JSON.stringify(st.planning && st.planning.active)}`);
    await shot('03-planning-live');
    await openChange(); // reopen: the same running plan and its Cancel plan button
    await cancel.click();
    const s = await waitFor(async () => { const x = await snap(); return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null; }, { timeout: 30000, message: 'cancelled plan reply' });
    lastProposal = s.composer.proposal.id;
    if (s.composer.review.verdict !== 'unavailable' || await applyCount()) throw new Error(JSON.stringify(s.composer.review));
    return { busySeconds: during.composer.busy.seconds };
  });

  await step('a two-operation plan applies as one revision and one undo step', async () => {
    const s = await requestPlan('Rename P-101 to P-201 and set its motor length to 0.42 m');
    const r = s.composer.review;
    if (r.verdict !== 'ready' || !r.canApply || r.operations !== 2) throw new Error(JSON.stringify(r));
    if (!(await page.textContent('.proposal .opcount')).includes('2 operations → 1 batch')) throw new Error('operation count not shown');
    if (!(await page.locator('.proposal .geom.unchecked').count())) throw new Error('geometry not marked unchecked');
    const meta = await page.textContent('.proposal .prop-head .meta');
    if (/object Object/.test(meta) || !/effort n\/a \(fake\) requested/.test(meta) || /reported/.test(meta)) throw new Error(`planner header: ${meta}`);
    const viewport = page.viewportSize();
    for (const sel of ['.proposal .prop-head', '.proposal button:has-text("Apply plan")']) {
      const b = await page.locator(sel).boundingBox();
      if (!b || b.y < 0 || b.y + b.height > viewport.height) throw new Error(`${sel} is outside the viewport (${JSON.stringify(b)})`);
    }
    await shot('04-ready-plan');
    const before = await serviceState();
    const motor = before.project.model.entities['ent-pump'].parameters.motor_length.value;
    await page.click('.proposal button:has-text("Apply plan")');
    await synchronizedAt(1);
    const after = await serviceState();
    const pump = after.project.model.entities['ent-pump'];
    if (pump.tag !== 'P-201' || pump.parameters.motor_length.value !== 0.42) throw new Error(JSON.stringify(pump));
    if (after.history.undo !== before.history.undo + 1) throw new Error(`undo depth ${before.history.undo} -> ${after.history.undo}`);
    await page.click('#btn-undo');
    await synchronizedAt(2);
    const undone = (await serviceState()).project.model.entities['ent-pump'];
    if (undone.tag !== 'P-101' || undone.parameters.motor_length.value !== motor) throw new Error(`one undo left ${JSON.stringify(undone)}`);
    return { undo: [before.history.undo, after.history.undo] };
  });

  await step('a composed drum plan draws a generic outline and leaves the build honestly pending', async () => {
    const s = await requestPlan('fake-drum: add a hold-up drum to L-104');
    if (s.composer.review.verdict !== 'ready' || s.composer.review.operations !== 1) throw new Error(JSON.stringify(s.composer.review));
    if (!(await page.locator('.proposal .tchip.new:has-text("D-101")').count())) throw new Error('new item not listed');
    await shot('05-drum-plan');
    await page.click('.proposal button:has-text("Apply plan")');
    await closeAssistant();
    const done = await buildSettled(3);
    if (done.sync === 'synchronized') throw new Error('an unplaced item cannot be synchronized');
    await page.waitForSelector('#pane-pid g[data-semantic-id="ent-drum-fake"][data-symbol-shape="horizontal_vessel"]');
    if (/^Current/.test(done.badges.doc)) throw new Error(`P&ID badge ${done.badges.doc}`);
    await page.click('#doc-tabs button[data-doc="legend"]');
    await page.waitForSelector('#pane-legend [data-legend-entry="symbol/horizontal_vessel"]');
    if (await page.locator('#pane-legend [data-legend-entry="symbol/vertical_vessel"], #pane-legend [data-legend-entry="symbol/box_equipment"]').count()) throw new Error('unused family listed');
    await page.click('#doc-tabs button[data-doc="pid"]');
    await shot('06-drum-on-pid');
    return { build: done.build, badge: done.badges.doc };
  });

  await step('the inspector edits composed parameters generically with the definition range', async () => {
    await closeAssistant();
    const glyph = page.locator('#pane-pid g[data-semantic-id="ent-drum-fake"] [data-role="symbol-body"] path[data-role="body"]').first();
    const box = await glyph.boundingBox();
    await page.mouse.click(box.x + box.width * 0.2, box.y + box.height * 0.5);
    await page.waitForSelector('#insp-body details.sec >> text=Definition');
    const field = '#insp-body input[data-field="param:ent-drum-fake:shell_length"]';
    await page.waitForSelector(field);
    if (!(await page.textContent('#insp-body')).includes('Declared function')) throw new Error('declared function not shown');
    await page.fill(field, '3.0');
    await page.dispatchEvent(field, 'input');
    const warn = await page.textContent('#insp-body .field-row + .inline-msg.err');
    if (!/authoring range/.test(warn || '')) throw new Error(`no range hint: ${warn}`);
    await shot('07-inspector-definition');
    await page.fill(field, '1.6');
    await page.locator(`${field} >> xpath=ancestor::div[contains(@class,"field-row")]//button`).click();
    await waitFor(async () => { const x = await snap(); return x.revision === 4 ? x : null; }, { message: 'parameter edit revision' });
    const v = (await serviceState()).project.model.entities['ent-drum-fake'].parameters.shell_length;
    if (v.value !== 1.6 || !v.assumption_id) throw new Error(JSON.stringify(v));
  });

  await step('placing the drum and routing both lines with existing controls reaches a synchronized build', async () => {
    // the drum is still selected. Its outlet sits at the centreline height; 0.70 m matches HV-102's inlet, so the
    // axis-aligned route example needs no short vertical jog (a 50 mm jog has no room for a pipe bend and fails the build)
    const centre = '#insp-body input[data-field="param:ent-drum-fake:centreline_height"]';
    await page.fill(centre, '0.7');
    await page.locator(`${centre} >> xpath=ancestor::div[contains(@class,"field-row")]//button`).click();
    await waitFor(async () => { const x = await snap(); return x.revision === 5 ? x : null; }, { message: 'centreline edit' });
    await page.waitForSelector('#insp-body input[data-field="pos:ent-drum-fake:x"]');
    for (const [axis, value] of [['x', '3.4'], ['y', '0.6'], ['z', '0']]) await page.fill(`#insp-body input[data-field="pos:ent-drum-fake:${axis}"]`, value);
    for (const axis of ['x', 'y', 'z']) await page.fill(`#insp-body input[data-field="rot:ent-drum-fake:${axis}"]`, '0');
    await page.click('#insp-body button:has-text("Place")');
    await waitFor(async () => { const x = await snap(); return x.revision === 6 ? x : null; }, { message: 'drum placed' });
    const lines = (await serviceState()).project.model.connections;
    const incident = Object.values(lines).filter((c) => /drum-fake/.test(c.to_port_id) || /drum-fake/.test(c.from_port_id)).map((c) => c.tag).sort();
    let revision = 6;
    for (const tag of incident) {
      // route examples copy port frames from the installed scene: wait for this revision's settled scene
      await waitFor(async () => {
        const x = await snap();
        return x.revision === revision && x.build && !['queued', 'running'].includes(x.build.status)
          && x.viewer.source && x.viewer.source.source_revision === revision && x.viewer.source.kind !== 'preview' ? x : null;
      }, { timeout: 240000, message: `settled scene of r${revision}` });
      await page.evaluate(() => document.activeElement && document.activeElement.blur());
      if ((await snap()).assistant.open) await page.keyboard.press('Escape'); // Escape closes the assistant before clearing the selection
      await page.keyboard.press('Escape');
      await page.click(`#insp-body .overview-list .tag-link:text-is("${tag}")`);
      await page.click('#insp-body button:has-text("Edit route…")');
      const review = await page.textContent('#panel-command .review');
      if (await page.isDisabled('#panel-command button:has-text("Apply command")')) throw new Error(`${tag} route example not applicable: ${review}`);
      await page.click('#panel-command button:has-text("Apply command")');
      revision += 1;
      await waitFor(async () => { const x = await snap(); return x.revision === revision ? x : null; }, { message: `route ${tag} applied` });
    }
    const s = await waitFor(async () => {
      const x = await snap();
      if (x.build && x.build.status === 'failed') return x;
      return x.revision === revision && x.sync === 'synchronized' && x.viewer.source && x.viewer.source.source_revision === revision ? x : null;
    }, { timeout: 300000, message: `synchronized r${revision}` });
    if (s.sync !== 'synchronized') throw new Error(`build ${JSON.stringify(s.build)} for the placed drum`);
    // the camera is kept across rebuilds, so the new item may sit outside the view: fit, then wait for the tween.
    // Labels are clickable overlays; switch them off so the click reaches the mesh (all -> equipment -> off).
    await page.click('#btn-3d-labels');
    await page.click('#btn-3d-labels');
    if ((await page.textContent('#labels-state')).trim() !== 'Off') throw new Error('labels not off');
    await page.click('#btn-3d-fit');
    let last = null;
    const point = await waitFor(async () => {
      const p = await page.evaluate(() => window.skidWorkspace.screenPointOf('ent-drum-fake#body'));
      const stable = p && last && Math.abs(p.x - last.x) < 0.5 && Math.abs(p.y - last.y) < 0.5;
      last = p;
      return stable ? p : null;
    }, { interval: 250, message: 'stable drum screen point' });
    const canvas = await page.locator('#gl-host').boundingBox();
    if (point.x < canvas.x || point.x > canvas.x + canvas.width || point.y < canvas.y || point.y > canvas.y + canvas.height) {
      throw new Error(`drum body projects outside the 3D view: ${JSON.stringify(point)}`);
    }
    await page.click('#doc-tabs button[data-doc="pid"]');
    await page.mouse.click(point.x, point.y);
    const sel = await waitFor(async () => { const x = await snap(); return x.selection.join() === 'ent-drum-fake' && x.pidSelected.length ? x : null; }, { message: 'drum selected from 3D' });
    await page.click('#btn-3d-labels'); // back to all labels
    await shot('08-drum-synchronized');
    return { revision, routes: incident, components: sel.viewer.selected.length, pidSelected: sel.pidSelected };
  });

  await step('command JSON offers insert_equipment and batch examples that pass review', async () => {
    await page.click('#change-tabs button[data-tab="command"]');
    for (const type of ['insert_equipment', 'batch']) {
      await page.selectOption('#panel-command select', type);
      await page.click('#panel-command button:has-text("Fill example")');
      if (await page.locator('#panel-command .review .errs').count()) throw new Error(`${type}: ${await page.textContent('#panel-command .review')}`);
      if (await page.isDisabled('#panel-command button:has-text("Apply command")')) throw new Error(`${type} example not reviewable`);
    }
    await shot('09-command-batch');
    await page.click('#change-tabs button[data-tab="feedback"]');
  });

  await step('narrow layout keeps the composer and plan card usable', async () => {
    await page.setViewportSize({ width: 900, height: 1000 });
    const s = await requestPlan('Rename P-101 to P-202 and set its motor length to 0.40 m');
    if (s.composer.review.verdict !== 'ready') throw new Error(JSON.stringify(s.composer.review));
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 0) throw new Error(`horizontal overflow ${overflow}px`);
    await shot('10-narrow-plan');
    await page.setViewportSize({ width: 1600, height: 1000 });
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
