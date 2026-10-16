// Shared helpers for the browser checks. Playwright is resolved from the repository's own
// `npm ci` install (root package.json, pinned 1.58.2) and, failing that, from the older local
// install under artifacts/ui-runtime. Nothing is downloaded or installed globally here.
//
// Environment overrides (none changes any assertion):
//   SKID_PYTHON        Python with the project's CAD dependencies (default .venv-cad/bin/python)
//   SKID_BROWSER_ARGS  Chromium arguments as a JSON array, replacing the platform default
//   UI_REVIEW_DIR      output directory for samples and captures, relative to the project root

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
// UI_REVIEW_DIR (relative to the project root) sends a run's samples and captures elsewhere, e.g. one review's folder
export const REVIEW_DIR = process.env.UI_REVIEW_DIR ? path.resolve(ROOT, process.env.UI_REVIEW_DIR) : path.join(ROOT, 'artifacts', 'ui-review');
export const PY = process.env.SKID_PYTHON ? path.resolve(ROOT, process.env.SKID_PYTHON) : path.join(ROOT, '.venv-cad', 'bin', 'python');

const PLAYWRIGHT_ROOTS = [path.join(ROOT, 'package.json'), path.join(ROOT, 'artifacts', 'ui-runtime', 'package.json')];

export function playwright() {
  const tried = [];
  for (const anchor of PLAYWRIGHT_ROOTS) {
    if (!fs.existsSync(anchor)) continue;
    try {
      return createRequire(anchor)('playwright');
    } catch (error) {
      if (error && error.code !== 'MODULE_NOT_FOUND') throw error;
      tried.push(path.dirname(anchor));
    }
  }
  throw new Error(`playwright is not installed (looked beside ${tried.join(', ') || 'no package.json'}); run \`npm ci\` in ${ROOT}`);
}

function launchArgs() {
  if (process.env.SKID_BROWSER_ARGS) {
    const args = JSON.parse(process.env.SKID_BROWSER_ARGS);
    if (!Array.isArray(args) || !args.every((a) => typeof a === 'string')) throw new Error('SKID_BROWSER_ARGS must be a JSON array of strings');
    return args;
  }
  // macOS: hardware WebGL through ANGLE/Metal; headless Chromium otherwise has no WebGL there.
  if (process.platform === 'darwin') return ['--enable-gpu', '--use-angle=metal', '--ignore-gpu-blocklist'];
  // Elsewhere (for example a GPU-less Linux CI runner): software WebGL through SwiftShader.
  return ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'];
}

export const LAUNCH = { headless: true, args: launchArgs() };

export function reviewPath(name) {
  fs.mkdirSync(REVIEW_DIR, { recursive: true });
  return path.join(REVIEW_DIR, name);
}

/** Collect console errors, page errors and failed requests for a page. */
export function watch(page) {
  const problems = [];
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console: ${msg.text()}`); });
  page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
  page.on('requestfailed', (req) => {
    const failure = req.failure();
    if (failure && failure.errorText !== 'net::ERR_ABORTED') problems.push(`requestfailed: ${req.url()} ${failure.errorText}`);
  });
  page.on('response', (res) => { if (res.status() >= 400 && !res.url().includes('/api/')) problems.push(`HTTP ${res.status()} ${res.url()}`); });
  return problems;
}

export async function waitFor(fn, { timeout = 20000, interval = 100, message = 'condition' } = {}) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeout) {
    last = await fn();
    if (last) return last;
    await new Promise((r) => setTimeout(r, interval));
  }
  throw new Error(`timed out after ${timeout} ms waiting for ${message}`);
}

export async function snapshot(page) {
  return page.evaluate(() => window.skidWorkspace && window.skidWorkspace.snapshot());
}
