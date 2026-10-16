// Static checks of the unbundled web/ tree: every named import exists, every element the
// controller looks up exists in index.html, nothing is loaded from the network, and the
// vendored Three.js files match the hashes recorded in web/vendor/README.md.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'web');
const JS = path.join(WEB, 'js');
const read = (p) => fs.readFileSync(p, 'utf8');
const modules = fs.readdirSync(JS).filter((f) => f.endsWith('.js'));

function exportsOf(source) {
  const names = new Set();
  for (const m of source.matchAll(/export\s+(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)/g)) names.add(m[1]);
  for (const m of source.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(',')) {
      const name = part.trim().split(/\s+as\s+/).pop().trim();
      if (name) names.add(name);
    }
  }
  return names;
}

test('every relative named import resolves to an exported name', () => {
  const problems = [];
  for (const file of modules) {
    const source = read(path.join(JS, file));
    for (const m of source.matchAll(/import\s*\{([^}]*)\}\s*from\s*'(\.\/[^']+)'/g)) {
      const target = path.join(JS, m[2]);
      if (!fs.existsSync(target)) {
        problems.push(`${file}: missing module ${m[2]}`);
        continue;
      }
      const exported = exportsOf(read(target));
      for (const part of m[1].split(',')) {
        const name = part.trim().split(/\s+as\s+/)[0].trim();
        if (name && !exported.has(name)) problems.push(`${file}: ${m[2]} does not export ${name}`);
      }
    }
    for (const m of source.matchAll(/import\(\s*'(\.\/[^']+)'\s*\)/g)) {
      if (!fs.existsSync(path.join(JS, m[1]))) problems.push(`${file}: missing dynamic module ${m[1]}`);
    }
  }
  assert.deepEqual(problems, []);
});

test('pure modules stay free of DOM and Three.js so they run in Node', () => {
  for (const file of ['status.js', 'loader.js', 'view-source.js', 'scene-diff.js', 'gestures.js', 'labels-layout.js', 'commands-model.js',
    'pane-model.js', 'project-name.js', 'reroute-model.js', 'assistant-model.js', 'move-coordinator.js',
    'research-model.js', 'creation-model.js']) {
    const source = read(path.join(JS, file)).replace(/\/\/.*$/gm, '');
    assert.ok(!/from\s+'three/.test(source), `${file} imports three`);
    assert.ok(!/\b(document|window)\./.test(source), `${file} touches the DOM`);
  }
});

test('every #id the scripts look up exists in index.html', () => {
  const html = read(path.join(WEB, 'index.html'));
  const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
  const missing = [];
  for (const file of modules) {
    const source = read(path.join(JS, file));
    for (const m of source.matchAll(/\$\('#([A-Za-z][\w-]*)/g)) if (!ids.has(m[1])) missing.push(`${file}: #${m[1]}`);
    for (const m of source.matchAll(/getElementById\('([^']+)'\)/g)) if (!ids.has(m[1])) missing.push(`${file}: #${m[1]}`);
  }
  assert.deepEqual(missing, []);
  for (const m of html.matchAll(/href="#(i-[\w-]+)"/g)) assert.ok(ids.has(m[1]), `icon ${m[1]} is defined`);
});

test('no remote scripts, styles, fonts or fetches', () => {
  const files = [path.join(WEB, 'index.html'), path.join(WEB, 'css', 'app.css'), ...modules.map((f) => path.join(JS, f))];
  for (const file of files) {
    const text = read(file);
    assert.ok(!/(src|href)\s*=\s*["']https?:/i.test(text), `${file} references a remote URL`);
    assert.ok(!/url\(\s*["']?https?:/i.test(text), `${file} loads a remote CSS resource`);
    assert.ok(!/(fetch|import)\(\s*["'`]https?:/i.test(text), `${file} fetches from the network`);
    assert.ok(!/@import|@font-face/i.test(text), `${file} imports styles or fonts`);
  }
  const map = JSON.parse(/<script type="importmap">([\s\S]*?)<\/script>/.exec(read(path.join(WEB, 'index.html')))[1]);
  assert.deepEqual(map.imports, { three: '/vendor/three/build/three.module.min.js', 'three/addons/': '/vendor/three/examples/jsm/' });
});

test('vendored Three.js files match the pinned hashes', () => {
  const readme = read(path.join(WEB, 'vendor', 'README.md'));
  const rows = [...readme.matchAll(/\| `(three\/[^`]+)` \| `[^`]+` \| `([0-9a-f]{64})` \| (\d+) \|/g)];
  assert.equal(rows.length, 4);
  for (const [, rel, sha, bytes] of rows) {
    const data = fs.readFileSync(path.join(WEB, 'vendor', rel));
    assert.equal(crypto.createHash('sha256').update(data).digest('hex'), sha, rel);
    assert.equal(data.length, Number(bytes), rel);
  }
  assert.ok(read(path.join(WEB, 'vendor', 'three', 'build', 'three.module.min.js')).includes('"170"'), 'REVISION 170');
});
