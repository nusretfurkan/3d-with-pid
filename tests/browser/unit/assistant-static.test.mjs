// Static checks of the assistant integration: one composer instance, inert rendering of model
// text, the Index table's three columns and the Escape wiring order.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'web');
const read = (rel) => fs.readFileSync(path.join(WEB, rel), 'utf8').replace(/\/\/.*$/gm, '');

test('the existing composer is created exactly once and handed to the assistant', () => {
  const calls = [];
  for (const file of fs.readdirSync(path.join(WEB, 'js')).filter((f) => f.endsWith('.js'))) {
    for (const m of read(`js/${file}`).matchAll(/\bcreateComposer\s*\(/g)) calls.push(`${file}@${m.index}`);
  }
  const app = read('js/app.js');
  assert.equal(calls.filter((c) => !c.startsWith('composer.js')).length, 1, calls.join());
  assert.match(app, /createComposer\(app, \$\('#panel-feedback'\)\)/);
  assert.match(app, /createAssistant\(app, \$\('#assistant'\), \$\('#panel-feedback'\)\)/);
  assert.ok(app.indexOf('createComposer(') < app.indexOf('createAssistant(app'), 'composer first, then moved');
});

test('assistant modules never parse model text as markup or code', () => {
  for (const file of ['js/assistant.js', 'js/assistant-model.js']) {
    const source = read(file);
    for (const bad of [/innerHTML/, /outerHTML\s*=/, /insertAdjacentHTML/, /document\.write/, /\beval\(/, /new Function/, /\.href\s*=/, /setAttribute\(\s*'on/]) {
      assert.ok(!bad.test(source), `${file} uses ${bad}`);
    }
  }
});

test('the Index page shows Tag / Item / Sheet-zone only; legend keys stay in the data', () => {
  const source = read('js/documents.js');
  const index = source.slice(source.indexOf('export function createIndexContent'), source.indexOf('export function createDescriptionContent'));
  const head = /h\('thead'[\s\S]*?\)\)\),/.exec(index)[0];
  assert.deepEqual([...head.matchAll(/h\('th', null, '([^']+)'\)/g)].map((m) => m[1]), ['Tag', 'Item', 'Sheet / zone']);
  assert.ok(!/legend_keys/.test(index), 'no legend-key cells');
  const row = /const rows = items\.map[\s\S]*?\n {4}h\('td'[\s\S]*?\)\)\)\)\);/.exec(index);
  assert.ok(row, 'row builder found');
  assert.equal([...row[0].matchAll(/h\('td'/g)].length, 3);
  assert.match(index, /legend keys drawn/, 'the drawn-key count from the inventory is still reported');
});

test('Escape inside the assistant or a Move field cancels a move or pick, then closes; elsewhere typing keeps its keys', () => {
  const app = read('js/app.js');
  assert.match(app, /const field = app\.assistant\.contains\(e\.target\) \|\| !!\(e\.target\.closest && e\.target\.closest\('\.move-panel'\)\);/);
  assert.match(app, /e\.key === 'Escape' && typing && !mod && !e\.altKey && !e\.isComposing && field\)/);
  assert.match(app, /if \(typing \|\| mod \|\| e\.altKey\) return;/);
  assert.match(app, /moving: app\.moves\.cancellable\(\)/);
  const escape = app.slice(app.indexOf('function escape()'));
  const order = ['cancel-move', 'cancel-pick', 'close-assistant', 'exit-focus', 'restore-max', 'clear-selection'].map((a) => escape.indexOf(`'${a}'`));
  assert.ok(order.every((i, k) => i > 0 && (k === 0 || i > order[k - 1])), order.join());
});
