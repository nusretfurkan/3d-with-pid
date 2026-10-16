// Read-only capture of the workspace served by an already running service.
// STATUS: run once on 2026-10-14 against the first UI draft (artifacts/ui-review/live-*.png,
// live-report.json), before browser automation was ruled out for that session; those images
// predate the review fixes. Run again only where driving a headless browser is allowed.
// Usage: node tests/browser/capture-live.mjs [http://127.0.0.1:8765/] [prefix]
// Only GET requests are made (no selection, command or build); screenshots and a
// JSON report go to artifacts/ui-review/.

import fs from 'node:fs';
import { LAUNCH, playwright, reviewPath, snapshot, waitFor, watch } from './lib.mjs';

const url = process.argv[2] || 'http://127.0.0.1:8765/';
const prefix = process.argv[3] || 'live';
const { chromium } = playwright();
const browser = await chromium.launch(LAUNCH);
const report = { url, captured_at: new Date().toISOString(), shots: [] };
try {
  for (const [w, hgt] of [[1600, 1000], [1280, 800]]) {
    const page = await browser.newPage({ viewport: { width: w, height: hgt }, deviceScaleFactor: 1 });
    // block mutations defensively: this script must stay read-only
    await page.route('**/api/**', (route) => (route.request().method() === 'GET' ? route.continue() : route.abort()));
    const problems = watch(page);
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    const snap = await waitFor(async () => {
      const s = await snapshot(page);
      return s && s.viewer && s.viewer.components > 0 && s.pidSource ? s : null;
    }, { timeout: 30000, message: 'scene and P&ID' });
    await page.waitForTimeout(800);
    const file = reviewPath(`${prefix}-${w}x${hgt}.png`);
    await page.screenshot({ path: file });
    report.shots.push({ file, viewport: [w, hgt], snapshot: await snapshot(page), problems });
    await page.close();
  }
} finally {
  await browser.close();
}
fs.writeFileSync(reviewPath(`${prefix}-report.json`), JSON.stringify(report, null, 2));
for (const s of report.shots) console.log(s.file, JSON.stringify({ problems: s.problems, viewer: s.snapshot.viewer && { components: s.snapshot.viewer.components, frames: s.snapshot.viewer.frames, labels: s.snapshot.viewer.labelsVisible.length }, sync: s.snapshot.sync, rev: s.snapshot.revision }));
