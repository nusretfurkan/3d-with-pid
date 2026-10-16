// Browser check of the embedded process description: narrative tag links inside the app's
// sandboxed srcdoc frame open the collapsed Item details and scroll to the item, without loading
// the app into the frame; the exported file keeps plain "#…" links and no <base>; a validated rename regenerates
// the narrative with the new tag and the same line chain, and undo restores it. Fresh sample + own
// service on a free port; never touches demo files or services 8765–8770. No Ask or planner call.
//
// Usage: [UI_REVIEW_DIR=artifacts/ui-review/<review>] node tests/browser/description.e2e.mjs [step-filter-regex]
// Output: <review dir>/description-<stamp>/{*.png, report.json}; exit code 1 on any failure.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { LAUNCH, PY, REVIEW_DIR, ROOT, playwright, snapshot, waitFor, watch } from './lib.mjs';

const stamp = new Date().toISOString().replace(/[:.]/g, '').slice(0, 17);
const dir = path.join(REVIEW_DIR, `description-${stamp}`);
fs.mkdirSync(dir, { recursive: true });
const projectPath = path.join(dir, 'project.json');
const report = { started_at: new Date().toISOString(), dir, steps: [] };
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
// Playwright's own evaluate calls inside the sandboxed frame are reported as blocked scripts;
// the description has no script of its own. Those messages are kept apart, not hidden.
const SANDBOX_NOTE = /Blocked script execution in 'about:srcdoc(#[\w-]*)?' because the document's frame is sandboxed/;
const sandboxNotes = [];
page.on('console', (msg) => { if (SANDBOX_NOTE.test(msg.text())) sandboxNotes.push(msg.text()); });
const problems = watch(page);
const realProblems = () => problems.filter((p) => !SANDBOX_NOTE.test(p));
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
const api = async (p, body) => {
  const r = await fetch(new URL(p, url), { method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  return r.json();
};
const anchor = (id) => `item-${id.replace(/[^A-Za-z0-9_-]/g, (c) => `_${c.charCodeAt(0).toString(16)}_`)}`;

const posts = [];
page.on('request', (req) => { if (req.method() === 'POST') posts.push(new URL(req.url()).pathname); });

const settled = (rev) => waitFor(async () => {
  const s = await snap();
  return s && s.revision === rev && s.sync === 'synchronized' ? s : null;
}, { timeout: 240000, message: `synchronized r${rev}` });

/** The description frame of pane 1 once it shows revision rev; returns {handle, frame, src}. */
async function descriptionFrame(rev) {
  const st = await api('/api/state');
  const handle = await waitFor(async () => {
    const el = await page.$(`#view-sheet iframe.doc-frame[title="Process description, revision ${rev}"]`);
    return el && (await el.contentFrame()) ? el : null;
  }, { timeout: 60000, message: `description frame r${rev}` });
  const frame = await handle.contentFrame();
  await frame.waitForSelector('details#item-details', { state: 'attached' });
  return { handle, frame, state: st };
}

/** Facts read from inside the frame (DOM only; nothing is injected into the document). */
const frameFacts = (frame) => frame.evaluate(() => {
  const d = document.getElementById('item-details');
  return {
    url: location.href,
    hash: location.hash,
    revision: Number(document.body.getAttribute('data-source-revision')),
    digest: document.body.getAttribute('data-input-digest'),
    bases: document.querySelectorAll('base').length,
    bareFragments: document.querySelectorAll('a[href^="#"], area[href^="#"]').length,
    srcdocFragments: document.querySelectorAll('a[href^="about:srcdoc#"]').length,
    detailsOpen: !!(d && d.open),
    scripts: document.querySelectorAll('script').length,
    nestedFrames: document.querySelectorAll('iframe').length,
    appChrome: !!document.querySelector('#workspace, #btn-ask, .pane-tabs'),
    narrative: [...document.querySelectorAll('p[data-role="narrative"]')].map((p) => p.textContent),
    narrativeRefs: [...document.querySelectorAll('p[data-role="narrative"] a.tag')].map((a) => ({ tag: a.textContent, ref: a.getAttribute('data-ref'), href: a.getAttribute('href') })),
    scrollY: window.scrollY,
  };
});
const inView = (frame, id) => frame.evaluate((i) => {
  const el = document.getElementById(i);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: innerHeight, visible: r.height > 0 && r.top >= -1 && r.top < innerHeight, target: el.matches(':target') };
}, id);

/** Line tags in reading order from the narrative text, by the model's connection tags. */
const lineChain = (texts, lineTags) => texts.join(' ').match(new RegExp(`\\b(${lineTags.map((t) => t.replace(/[-]/g, '\\-')).join('|')})\\b`, 'g')) || [];

try {
  await page.goto(url);
  let rev0;
  let chain0;
  let lineTags;

  await step('app Description pane: srcdoc frame, sandbox="", no <base>, fragment links aimed at about:srcdoc, identity matches its source', async () => {
    const s = await settled(0);
    rev0 = s.revision;
    await page.locator('#view-sheet .pane-tabs button[data-doc="description"]').click();
    const { handle, frame, state } = await descriptionFrame(rev0);
    const attrs = await handle.evaluate((f) => ({ sandbox: f.getAttribute('sandbox'), srcdocBase: (f.srcdoc.match(/<base\b/g) || []).length, src: f.getAttribute('src') }));
    if (attrs.sandbox !== '' || attrs.srcdocBase !== 0 || attrs.src) throw new Error(JSON.stringify(attrs));
    const f = await frameFacts(frame);
    if (f.url !== 'about:srcdoc' || f.bases !== 0 || f.bareFragments !== 0 || f.srcdocFragments < f.narrativeRefs.length) throw new Error(JSON.stringify({ ...f, narrative: undefined }));
    if (f.revision !== state.project.revision || f.digest !== state.document.input_digest) throw new Error(`identity ${f.revision}/${f.digest} vs state ${state.project.revision}/${state.document.input_digest}`);
    if (f.detailsOpen) throw new Error('Item details open at first');
    if (f.scripts || f.nestedFrames || f.appChrome) throw new Error(JSON.stringify(f));
    if (!f.narrative.length || !f.narrativeRefs.length) throw new Error('no narrative or no narrative links');
    lineTags = Object.values(state.project.model.connections).map((c) => c.tag);
    chain0 = lineChain(f.narrative, lineTags);
    if (chain0.length < 2) throw new Error(`line chain too short: ${chain0}`);
    await shot('01-description-r0');
    return { sandbox: attrs.sandbox, srcdocFragments: f.srcdocFragments, revision: f.revision, digest: f.digest, refs: f.narrativeRefs.length, chain: chain0 };
  });

  await step('narrative stable-ID link: same frame keeps the description, Item details open, target scrolled into view, no nested app', async () => {
    const { handle, frame } = await descriptionFrame(rev0);
    const before = await frameFacts(frame);
    // the last narrative link points furthest down the details, so a scroll is needed
    const ref = before.narrativeRefs.filter((r) => r.ref).at(-1);
    if (ref.href !== `about:srcdoc#${anchor(ref.ref)}`) throw new Error(`link ${ref.href} is not the stable-ID anchor of ${ref.ref}`);
    const target = anchor(ref.ref);
    const pre = await inView(frame, target);
    if (!pre || pre.visible) throw new Error(`target already visible or missing before the click: ${JSON.stringify(pre)}`);
    const appUrl = page.url();
    await frame.locator(`p[data-role="narrative"] a.tag[data-ref="${ref.ref}"]`).last().click();
    const after = await waitFor(async () => { const x = await frameFacts(frame); return x.detailsOpen && x.hash === `#${target}` ? x : null; }, { timeout: 5000, message: 'details open at the target' });
    const sameFrame = (await handle.contentFrame()) === frame && (await page.$$('#view-sheet iframe.doc-frame')).length === 1;
    const post = await inView(frame, target);
    if (!sameFrame || after.url !== `about:srcdoc#${target}` || after.revision !== before.revision || after.digest !== before.digest) throw new Error(JSON.stringify({ sameFrame, after }));
    if (after.appChrome || after.nestedFrames || frame.childFrames().length) throw new Error('the app or a nested frame loaded inside the description frame');
    if (!post.visible || !post.target || after.scrollY <= before.scrollY) throw new Error(`target not scrolled into view: ${JSON.stringify({ post, scrollY: [before.scrollY, after.scrollY] })}`);
    if (page.url() !== appUrl) throw new Error(`app URL changed to ${page.url()}`);
    // a summary click still toggles the details natively
    await frame.locator('details#item-details > summary').click();
    if ((await frameFacts(frame)).detailsOpen) throw new Error('summary click did not close the details');
    await frame.locator('details#item-details > summary').click();
    if (!(await frameFacts(frame)).detailsOpen) throw new Error('summary click did not open the details');
    await shot('02-link-opened-details');
    return { ref, target, scrollY: [before.scrollY, after.scrollY], post, frameUrl: after.url };
  });

  await step('standalone export: no <base>, plain fragment links open the details natively', async () => {
    const s = await snap();
    const descUrl = s.slots.description && s.slots.description.installed;
    if (!descUrl) throw new Error(`no installed description: ${JSON.stringify(s.slots.description)}`);
    const text = await (await fetch(new URL(descUrl, url))).text();
    if (/<base\b/i.test(text) || /about:srcdoc/.test(text)) throw new Error('exported description contains <base> or about:srcdoc');
    if (!/<a class="tag" href="#item-/.test(text)) throw new Error('exported description has no plain fragment tag links');
    const p2 = await context.newPage();
    const p2problems = watch(p2);
    try {
      const full = new URL(descUrl, url).href;
      await p2.goto(full);
      if (await p2.evaluate(() => document.getElementById('item-details').open)) throw new Error('details open at first');
      const ref = await p2.evaluate(() => { const a = [...document.querySelectorAll('p[data-role="narrative"] a.tag')].at(-1); return { ref: a.getAttribute('data-ref'), href: a.getAttribute('href') }; });
      await p2.locator(`p[data-role="narrative"] a.tag[data-ref="${ref.ref}"]`).last().click();
      const r = await waitFor(() => p2.evaluate(() => (document.getElementById('item-details').open ? { url: location.href, scrollY: scrollY } : null)), { timeout: 5000, message: 'standalone details open' });
      if (r.url !== `${full}${ref.href}` || r.scrollY <= 0) throw new Error(JSON.stringify(r));
      if (p2problems.length) throw new Error(p2problems.join(' | '));
      return { url: descUrl, ref, after: r };
    } finally { await p2.close(); }
  });

  await step('validated rename regenerates the narrative: new revision and tag, same line chain, Item details retained; undo restores', async () => {
    const st = await api('/api/state');
    const pump = st.project.model.entities['ent-pump'];
    const oldTag = pump.tag;
    const res = await api('/api/command', { id: `cmd-description-${Date.now()}`, expected_revision: st.project.revision, type: 'rename_entity', payload: { entity_id: 'ent-pump', tag: 'P-777' } });
    if (!res.project || res.project.revision !== st.project.revision + 1) throw new Error(`rename rejected: ${JSON.stringify(res).slice(0, 400)}`);
    const rev1 = res.project.revision;
    await settled(rev1);
    let { frame, state } = await descriptionFrame(rev1);
    let f = await frameFacts(frame);
    if (f.revision !== rev1 || f.digest !== state.document.input_digest || f.bases !== 0 || f.bareFragments !== 0) throw new Error(JSON.stringify(f));
    const text1 = f.narrative.join(' ');
    if (!/\bP-777\b/.test(text1) || new RegExp(`\\b${oldTag}\\b`).test(text1)) throw new Error(`narrative tags: ${text1}`);
    if (!f.narrativeRefs.some((r) => r.tag === 'P-777' && r.ref === 'ent-pump')) throw new Error('P-777 link does not carry the stable ID');
    const chain1 = lineChain(f.narrative, lineTags);
    if (chain1.join() !== chain0.join()) throw new Error(`line chain ${chain1} != ${chain0}`);
    const details = await frame.evaluate((id) => { const d = document.getElementById('item-details'); return { open: d.open, item: !!d.querySelector(`#${id}`), text: d.textContent.includes('P-777') }; }, anchor('ent-pump'));
    if (details.open || !details.item || !details.text) throw new Error(`details after rename: ${JSON.stringify(details)}`);
    await shot('03-after-rename');

    await page.click('#btn-undo');
    const rev2 = rev1 + 1;
    await settled(rev2);
    ({ frame, state } = await descriptionFrame(rev2));
    f = await frameFacts(frame);
    const text2 = f.narrative.join(' ');
    if (f.revision !== rev2 || f.digest !== state.document.input_digest || !new RegExp(`\\b${oldTag}\\b`).test(text2) || /\bP-777\b/.test(text2)) throw new Error(`after undo: ${JSON.stringify({ revision: f.revision, text2 })}`);
    if (lineChain(f.narrative, lineTags).join() !== chain0.join() || f.detailsOpen) throw new Error('chain or details changed after undo');
    await shot('04-after-undo');
    return { revisions: [st.project.revision, rev1, rev2], chain: chain1, renamed: 'P-777', restored: oldTag };
  });

  await step('no Ask or planner request; no page errors or console errors', async () => {
    const asks = posts.filter((p) => /ask|plan/.test(p));
    if (asks.length) throw new Error(`unexpected requests: ${asks}`);
    const real = realProblems();
    if (real.length) throw new Error(real.join(' | '));
    return { posts, sandboxNotes: sandboxNotes.length };
  });
} finally {
  report.problems = realProblems();
  report.sandbox_notes = sandboxNotes.length;
  report.posts = posts;
  report.passed = report.steps.length > 0 && report.steps.every((s) => s.ok) && report.problems.length === 0;
  report.finished_at = new Date().toISOString();
  fs.writeFileSync(path.join(dir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(`${report.steps.filter((s) => s.ok).length}/${report.steps.length} passed · problems ${report.problems.length} · ${path.relative(ROOT, dir)}`);
  await browser.close().catch(() => {});
  service.kill('SIGTERM');
}
process.exit(report.passed ? 0 : 1);
