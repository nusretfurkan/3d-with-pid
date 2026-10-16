// Empty-project creation and the Research equipment option, end to end in Chromium (UI mechanics).
//
// Creates a genuinely blank project with `python -m skid.cli new` under
// artifacts/agents/empty-research/integration/browser-<stamp>/ and serves it with its own service on a free
// port (tests/browser/fake_research_service.py): a SCRIPTED FAKE planner and a SCRIPTED FAKE researcher (no
// language model, no web search). Everything after their replies is real: brief re-validation, plan schema
// check, translation with deterministic guided routes, core dry-run, apply, persistence and the real
// generators. It never touches a user service or project. Passing here says nothing about the real planner or
// real WebSearch; root runs those separately through Claude Max.
//
// Usage: node tests/browser/creation.e2e.mjs
// Output: <dir>/{*.png, report.json}; exit code 1 on any failed step.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const base = process.env.UI_REVIEW_DIR ? path.resolve(ROOT, process.env.UI_REVIEW_DIR)
  : path.join(ROOT, 'artifacts', 'agents', 'empty-research', 'integration');
const dir = path.join(base, `browser-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const project = path.join(dir, 'blank project.json');
const report = {
  started_at: new Date().toISOString(), dir,
  providers: 'SCRIPTED FAKE planner and SCRIPTED FAKE researcher (no language model, no web search); real service and generators',
  steps: [],
};

const created = spawnSync(PY, ['-m', 'skid.cli', 'new', project, '--name', 'Blank creation e2e', '--project-id', `fake-creation-e2e-${stamp}`],
  { cwd: ROOT, encoding: 'utf8' });
if (created.status !== 0) throw new Error(`skid.cli new failed: ${created.stderr}`);
const again = spawnSync(PY, ['-m', 'skid.cli', 'new', project], { cwd: ROOT, encoding: 'utf8' });
report.cli_new_refuses_existing = again.status !== 0;

// CREATION_E2E_WEB_ROOT serves another copy of web/ (used only for negative controls; never the shared web/ edited)
const webArgs = process.env.CREATION_E2E_WEB_ROOT ? ['--web-root', process.env.CREATION_E2E_WEB_ROOT] : [];
report.web_root = process.env.CREATION_E2E_WEB_ROOT || 'web/';
const service = spawn(PY, [path.join(ROOT, 'tests', 'browser', 'fake_research_service.py'), '--project', project, '--port', '0', ...webArgs], { cwd: ROOT });
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
const proposals = []; // bodies of every /api/propose request the page sends
page.on('request', (r) => { if (r.url().endsWith('/api/propose')) proposals.push(JSON.parse(r.postData() || '{}')); });

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
const settled = (revision) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === revision && s.build && s.build.status !== 'queued' && s.build.status !== 'running'
    && s.pidSource && s.pidSource.revision === revision ? s : null;
}, { timeout: 300000, message: `build of r${revision} settled with its P&ID installed` });
const synchronizedAt = (revision) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === revision && s.sync === 'synchronized' && s.viewer && s.viewer.source && s.viewer.source.source_revision === revision ? s : null;
}, { timeout: 300000, message: `synchronized r${revision}` });

async function openChange() {
  const s = await snap();
  if (s && s.assistant && s.assistant.open && s.assistant.mode === 'change') return;
  await page.click('#change-tabs button[data-tab="feedback"]');
}
async function closeAssistant() {
  if ((await snap()).assistant.open) await page.click('#assistant .asst-close');
}
async function engineering(research) {
  await openChange();
  await page.click('#panel-feedback .mode-seg button[data-mode="engineering"]');
  const box = page.locator('#panel-feedback .research-opt input');
  if ((await box.isChecked()) !== research) await box.click();
}
let lastProposal = null;
async function plan(text, { research = false, wait = true } = {}) {
  await engineering(research);
  await page.fill('#panel-feedback textarea', text);
  await page.click('#panel-feedback .composer > button');
  if (!wait) return null;
  const s = await waitFor(async () => {
    const x = await snap();
    return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null;
  }, { timeout: 90000, message: `a proposal for "${text}"` });
  lastProposal = s.composer.proposal.id;
  return s;
}
const applyButtons = () => page.locator('#panel-feedback .proposal button:has-text("Apply")');
/** Symbol occurrences drawn in the first P&ID pane (edges and port occurrences `sym#port` excluded). */
const pidSymbols = () => page.evaluate(() => [...new Set([...document.querySelectorAll('#pane-pid [data-occurrence-id]')]
  .map((e) => e.getAttribute('data-occurrence-id')).filter((id) => id.startsWith('sym-') && !id.includes('#')))].sort());
const textHasNaN = () => page.evaluate(() => /\bNaN\b|undefined|\[object Object\]/.test(document.body.innerText));

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' });

  await step('a genuinely blank project renders stable, empty views with no hidden seed', async () => {
    if (!report.cli_new_refuses_existing) throw new Error('skid.cli new overwrote an existing project');
    const s = await settled(0);
    const st = await serviceState();
    const m = st.project.model;
    const counts = [m.entities, m.ports, m.connections, m.assumptions, st.project.pid.symbols, st.project.physical.placements].map((x) => Object.keys(x).length);
    if (counts.some((n) => n)) throw new Error(`seed records: ${counts}`);
    if ((await pidSymbols()).length) throw new Error('P&ID symbols drawn for an empty project');
    if (await textHasNaN()) throw new Error('NaN/undefined text on the blank page');
    await page.waitForTimeout(1500);
    const later = await snap();
    if (JSON.stringify(later.pidView) !== JSON.stringify(s.pidView)) throw new Error('empty sheet view drifted');
    await shot('01-blank-project');
    return { sync: s.sync, build: s.build.status, badges: s.badges, sceneStats: s.sceneStats };
  });

  await step('quick edit explains itself on a blank project; Engineering plan offers creation and Research equipment', async () => {
    await openChange();
    await page.click('#panel-feedback .mode-seg button[data-mode="quick"]');
    const hint = await page.textContent('#panel-feedback .examples');
    if (!/empty.*Engineering plan/.test(hint)) throw new Error(`quick hint: ${hint}`);
    if (await page.isVisible('#panel-feedback .research-opt')) throw new Error('research option shown in quick edit');
    await page.click('#panel-feedback .mode-seg button[data-mode="engineering"]');
    if (!(await page.isVisible('#panel-feedback .research-opt'))) throw new Error('research option missing in engineering mode');
    if (await page.isChecked('#panel-feedback .research-opt input')) throw new Error('research is on by default');
    const examples = await page.locator('#panel-feedback .examples .example').allTextContents();
    if (!examples.some((t) => /storage tank TK-101.*HV-101/.test(t)) || !examples.some((t) => /heat exchanger HX-101/.test(t))) throw new Error(JSON.stringify(examples));
    await page.click('#panel-feedback .research-opt input');
    const label = (await page.textContent('#panel-feedback .composer > button')).trim();
    const hintShown = await page.isVisible('#panel-feedback .research-hint');
    await page.click('#panel-feedback .research-opt input');
    if (label !== 'Research & plan' || !hintShown) throw new Error(`${label} ${hintShown}`);
    await shot('02-engineering-blank');
    return { examples };
  });

  await step('tank and outlet valve: one reviewed batch, guided line, open valve outlet stays honestly pending', async () => {
    const before = proposals.length;
    await engineering(false);
    await page.click('#panel-feedback .examples .example:has-text("storage tank")');
    await page.click('#panel-feedback .composer > button');
    const s = await waitFor(async () => { const x = await snap(); return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null; }, { timeout: 60000, message: 'tank proposal' });
    lastProposal = s.composer.proposal.id;
    const body = proposals[before];
    if (!body || 'research' in body || body.mode !== 'engineering') throw new Error(`request body ${JSON.stringify(body)}`);
    const r = s.composer.review;
    if (r.verdict !== 'ready' || r.operations !== 3) throw new Error(JSON.stringify(r));
    const created = await page.locator('#panel-feedback .proposal .tchip.new').allTextContents();
    for (const tag of ['TK-101', 'HV-101', 'L-101']) if (!created.some((t) => t.includes(tag))) throw new Error(`${tag} not listed: ${created}`);
    const ops = await page.textContent('#panel-feedback .proposal .ops');
    if (!/Physical route: 2 points, mode guided/.test(ops) || !/deliberately open/.test(ops) || !/P&ID edge edge-l101 on the guided path/.test(ops)) throw new Error(ops);
    const warnings = await page.textContent('#panel-feedback .proposal');
    if (!/required_port_unconnected/.test(warnings)) throw new Error('open port not reported in review');
    await shot('03-tank-valve-review');
    await page.click('#panel-feedback .proposal button:has-text("Apply plan")');
    await closeAssistant();
    const done = await settled(1);
    if (done.sync === 'synchronized') throw new Error('an open required port cannot be synchronized');
    // the first equipment of a blank project must be framed in the 3D view (its tags visible), not left off-screen
    await waitFor(async () => {
      const v = (await snap()).viewer;
      return v && v.components > 0 && ['TK-101', 'HV-101'].every((t) => (v.labelsVisible || []).includes(t));
    }, { timeout: 20000, message: 'TK-101 and HV-101 visible in the 3D view' });
    const symbols = await pidSymbols();
    if (JSON.stringify(symbols) !== JSON.stringify(['sym-hv101', 'sym-tk101'])) throw new Error(`symbols ${symbols}`);
    const st = await serviceState();
    if (st.project.model.ports['port-hv101-outlet'].connection_status !== 'unresolved') throw new Error('open port not marked');
    if (st.project.physical.routes['conn-l101'].status !== 'current') throw new Error('guided route not current');
    await shot('04-tank-valve-applied');
    return { sync: done.sync, badges: done.badges };
  });

  await step('pump with its own suction valve and a terminal boundary completes and synchronizes all five outputs', async () => {
    await openChange();
    await engineering(false);
    const example = page.locator('#panel-feedback .examples .example:has-text("Continue from HV-101 outlet")');
    if (!(await example.count())) throw new Error('no continue example for the open outlet');
    await example.click();
    await page.click('#panel-feedback .composer > button');
    const s = await waitFor(async () => { const x = await snap(); return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null; }, { timeout: 60000, message: 'pump proposal' });
    lastProposal = s.composer.proposal.id;
    if (s.composer.review.verdict !== 'ready' || s.composer.review.operations !== 6) throw new Error(JSON.stringify(s.composer.review));
    await page.click('#panel-feedback .proposal button:has-text("Apply plan")');
    await closeAssistant();
    const done = await synchronizedAt(2);
    const st = await serviceState();
    const valves = Object.values(st.project.model.entities).filter((e) => e.kind === 'isolation_valve').map((e) => e.tag).sort();
    if (JSON.stringify(valves) !== '["HV-101","HV-102"]') throw new Error(`valves ${valves}`);
    if (Object.values(st.project.model.ports).some((p) => 'connection_status' in p)) throw new Error('open markers left');
    if ((await pidSymbols()).length !== 5) throw new Error(`symbols ${await pidSymbols()}`);
    await shot('05-train-synchronized');
    return { badges: done.badges };
  });

  await step('research option: stages, sources, features and unknowns are reviewed before Apply; links are safe', async () => {
    const before = proposals.length;
    await plan('fake-stages Add a shell-and-tube heat exchanger HX-101 with separate shell-side and tube-side circuits', { research: true, wait: false });
    const researching = await waitFor(async () => { const x = await snap(); return x.composer.busy && x.composer.busy.stage === 'researching' ? x : null; }, { timeout: 15000, message: 'researching stage' });
    const label = await page.textContent('#panel-feedback .planning .pl-what');
    await shot('06-researching');
    await closeAssistant(); // dismissal keeps the running request
    if (!(await snap()).composer.busy) throw new Error('closing stopped the research');
    await openChange();
    await waitFor(async () => { const x = await snap(); return x.composer.busy && x.composer.busy.stage === 'planning' ? x : null; }, { timeout: 15000, message: 'planning stage' });
    const label2 = await page.textContent('#panel-feedback .planning .pl-what');
    await shot('07-planning-after-research');
    const s = await waitFor(async () => { const x = await snap(); return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null; }, { timeout: 60000, message: 'researched proposal' });
    lastProposal = s.composer.proposal.id;
    if (proposals[before].research !== true) throw new Error(`request body ${JSON.stringify(proposals[before])}`);
    if (label !== 'Researching' || label2 !== 'Planning') throw new Error(`stages ${label} / ${label2}`);
    if (s.composer.review.verdict !== 'ready') throw new Error(JSON.stringify(s.composer.review));
    const card = page.locator('#panel-feedback .proposal');
    const links = await card.locator('.prop-block.research a.src-link').evaluateAll((as) => as.map((a) => ({
      href: a.getAttribute('href'), target: a.target, rel: a.rel, referrer: a.getAttribute('referrerpolicy'), text: a.textContent })));
    if (links.length !== 2 || links.some((l) => !/^https:\/\//.test(l.href) || l.target !== '_blank' || !/noopener/.test(l.rel) || !/noreferrer/.test(l.rel) || l.referrer !== 'no-referrer')) throw new Error(JSON.stringify(links));
    if (!links.some((l) => l.text.includes('<img src=x onerror=alert(1)>'))) throw new Error('hostile title not shown as text');
    if (await card.locator('img, script, iframe').count()) throw new Error('source text became markup');
    const research = await card.locator('.prop-block.research').textContent();
    for (const needle of ['Sourced features', 'Two separate circuits', 'Not established by the sources', 'Thermal duty', "<script>alert('observation')</script>"]) {
      if (!research.includes(needle)) throw new Error(`missing ${needle}`);
    }
    if (!(await card.locator('.cites .sref').count())) throw new Error('assumption citations not shown');
    if ((await card.locator('.opcount.researched').count()) !== 1) throw new Error('researched badge missing');
    await shot('08-researched-review');
    await card.locator('button:has-text("Apply plan")').click();
    await closeAssistant();
    const done = await settled(3);
    const st = await serviceState();
    const sources = Object.values(st.project.model.assumptions).map((a) => a.source).filter((x) => /Research res-/.test(x));
    if (!sources.length || !sources.some((x) => x.includes('https://www.fake-exchanger-maker.com/'))) throw new Error('research provenance not persisted');
    const html = await page.evaluate(async () => {
      const st = await (await fetch('/api/state', { cache: 'no-store' })).json();
      const u = st.artifacts && st.artifacts.description;
      return u ? (await fetch(u)).text() : '';
    });
    if (!html.includes('https://www.fake-exchanger-maker.com/products/shell-and-tube')) throw new Error('rebuilt description lacks the source URL');
    if (/<img src=x/.test(html)) throw new Error('description contains hostile markup');
    await shot('09-exchanger-applied');
    return { sync: done.sync, researchAssumptions: sources.length };
  });

  await step('failed, forged and cancelled research never offer Apply or a source-free plan', async () => {
    const outcomes = {};
    for (const [key, text] of [['failed', 'fake-research-fail heat exchanger please'], ['forged', 'fake-forged heat exchanger please']]) {
      const s = await plan(text, { research: true });
      const head = await page.textContent('#panel-feedback .proposal .prop-head .verdict');
      if (s.composer.review.canApply || await applyButtons().count()) throw new Error(`${key}: apply offered`);
      if (!/Research (rejected|unavailable)/.test(head)) throw new Error(`${key}: ${head}`);
      outcomes[key] = head;
    }
    await shot('10-research-refused');
    await plan('fake-slow heat exchanger please', { research: true, wait: false });
    const cancel = page.locator('#panel-feedback .planning button:has-text("Cancel plan")');
    await cancel.waitFor({ timeout: 15000 });
    await cancel.click();
    const s = await waitFor(async () => { const x = await snap(); return x.composer && !x.composer.busy && x.composer.proposal && x.composer.proposal.id !== lastProposal ? x : null; }, { timeout: 30000, message: 'cancelled research reply' });
    lastProposal = s.composer.proposal.id;
    const head = await page.textContent('#panel-feedback .proposal .prop-head .verdict');
    if (s.composer.review.canApply || head !== 'Research cancelled') throw new Error(head);
    outcomes.cancelled = head;
    if ((await serviceState()).project.revision !== 3) throw new Error('a refused research changed the project');
    return outcomes;
  });

  await step('draft and research choice survive dismissal; quick edit never sends research', async () => {
    await engineering(true);
    await page.fill('#panel-feedback textarea', 'Keep this draft');
    await closeAssistant();
    await page.click('#btn-ask');
    await page.click('#asst-tab-change').catch(() => {});
    const kept = await page.inputValue('#panel-feedback textarea');
    const checked = await page.isChecked('#panel-feedback .research-opt input');
    if (kept !== 'Keep this draft' || !checked) throw new Error(`${kept} ${checked}`);
    await page.click('#panel-feedback .mode-seg button[data-mode="quick"]');
    const before = proposals.length;
    await page.fill('#panel-feedback textarea', 'Rename HX-101 to HX-201');
    await page.click('#panel-feedback .composer > button');
    await waitFor(async () => proposals.length > before && !(await snap()).composer.busy, { message: 'quick reply' });
    if ('research' in proposals[before] || 'mode' in proposals[before]) throw new Error(JSON.stringify(proposals[before]));
    await page.fill('#panel-feedback textarea', '');
    return { quickBody: proposals[before] };
  });

  await step('no clipping: researched review at 900/1280/1600, two/four panes, normal and focus', async () => {
    await plan('Add a shell-and-tube heat exchanger HX-102 as a second unit', { research: true });
    const results = [];
    for (const w of [900, 1280, 1600]) {
      await page.setViewportSize({ width: w, height: w === 900 ? 760 : 900 });
      for (const n of [2, 4]) {
        for (const focus of [false, true]) {
          if ((await snap()).assistant.open) await page.keyboard.press('Escape');
          const s0 = await snap();
          if (s0.layout.focus !== focus) await page.evaluate((on) => document.querySelector(on ? '#btn-focus' : '#btn-exit-focus').click(), focus);
          if (!focus) await page.evaluate((k) => document.querySelector(`#layout-seg button[data-layout="${k}"]`).click(), n);
          await page.waitForTimeout(250);
          await page.click('#btn-ask');
          await page.click('#asst-tab-change').catch(() => {});
          await page.waitForTimeout(250);
          const g = await page.evaluate(() => {
            const a = document.getElementById('assistant').getBoundingClientRect();
            const vw = document.documentElement.clientWidth;
            const vh = document.documentElement.clientHeight;
            const card = document.querySelector('#panel-feedback .proposal');
            const research = document.querySelector('#panel-feedback .prop-block.research');
            const opt = document.querySelector('#panel-feedback .research-opt');
            const wide = (el) => !!el && el.scrollWidth > el.clientWidth + 1;
            return {
              inside: a.left >= -0.5 && a.top >= -0.5 && a.right <= vw + 0.5 && a.bottom <= vh + 0.5,
              overflowX: document.documentElement.scrollWidth > vw,
              cardOverflow: wide(card), researchOverflow: wide(research),
              optionVisible: !!opt && opt.getClientRects().length > 0,
              exit: document.body.classList.contains('focus-mode') ? (() => {
                const b = document.getElementById('btn-exit-focus').getBoundingClientRect();
                const top = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
                return !!top && document.getElementById('btn-exit-focus').contains(top);
              })() : null,
            };
          });
          results.push({ w, n, focus, ...g });
          await shot(`11-${w}-${n}p-${focus ? 'focus' : 'normal'}`);
        }
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    if ((await snap()).layout.focus) await page.click('#btn-exit-focus');
    const bad = results.filter((x) => !x.inside || x.overflowX || x.cardOverflow || x.researchOverflow || !x.optionVisible || x.exit === false);
    if (bad.length) throw new Error(JSON.stringify(bad));
    return results.map((x) => `${x.w}/${x.n}/${x.focus ? 'focus' : 'normal'}`);
  });

  await step('no page errors, failed requests or NaN text', async () => {
    if (await textHasNaN()) throw new Error('NaN/undefined/[object Object] text on the page');
    if (problems.length) throw new Error(problems.join('\n'));
  });
} finally {
  report.problems = problems;
  report.finished_at = new Date().toISOString();
  report.passed = report.steps.filter((s) => s.ok).length;
  report.failed = report.steps.filter((s) => !s.ok).length;
  report.proposal_requests = proposals.map((b) => ({ mode: b.mode ?? null, research: 'research' in b ? b.research : '(absent)' }));
  fs.writeFileSync(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close();
  service.kill('SIGTERM');
  await new Promise((resolve) => { service.once('exit', resolve); setTimeout(resolve, 15000); });
  console.log(`${report.passed}/${report.steps.length} steps passed; ${problems.length} page problems; report ${path.join(dir, 'report.json')}`);
  for (const s of report.steps.filter((x) => !x.ok)) console.log(`FAILED: ${s.name}\n${s.error}`);
  process.exitCode = report.failed || problems.length ? 1 : 0;
}
