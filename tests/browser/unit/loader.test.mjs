// Latest-wins loader: out-of-order completions, progressive previews, failures and withdrawal.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createLoader, previewOrder } from '../../../web/js/loader.js';

const A = 'b20261014T080000-r1-aaaaaaaa';
const B = 'b20261014T080100-r2-bbbbbbbb';
const u = (build, path) => `/artifacts/${build}/${path}`;
const src = (build, rev = 1) => ({ build_id: build, source_revision: rev, input_digest: 'd' });

/** fetch() whose responses resolve or fail only when the test says so, in any order. */
function deferredFetch() {
  const calls = [];
  const fetch = (url) => new Promise((resolve, reject) => calls.push({ url, resolve, reject }));
  const settle = async (url, value, fail = false) => {
    const call = calls.find((c) => c.url === url && !c.done);
    assert.ok(call, `no pending fetch for ${url}`);
    call.done = true;
    if (fail) call.reject(new Error(value));
    else call.resolve(value);
    for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0));
  };
  return { fetch, calls, settle };
}

function harness(options = {}) {
  const f = deferredFetch();
  const installed = [];
  const loader = createLoader({
    fetch: f.fetch,
    install: (data, target) => {
      if (options.reject && options.reject(data)) throw new Error(`refused ${data}`);
      installed.push(target.url);
    },
    now: options.now,
    retryAfterMs: options.retryAfterMs,
  });
  return { loader, installed, ...f };
}

test('previewOrder sorts snapshots and puts final outputs last', () => {
  assert.equal(previewOrder(u(A, 'preview/display-0007.json')), 7);
  assert.equal(previewOrder(u(A, 'preview/pid-0012.svg')), 12);
  assert.ok(previewOrder(u(A, 'scene.json')) > previewOrder(u(A, 'preview/display-9999.json')));
});

test('an obsolete response from another build is discarded; only the newest source installs', async () => {
  const h = harness();
  h.loader.want(u(A, 'scene.json'), src(A));
  h.loader.want(u(B, 'preview/display-0001.json'), src(B, 2)); // newer request while A is in flight
  await h.settle(u(A, 'scene.json'), 'A');
  assert.deepEqual(h.installed, []);
  assert.equal(h.loader.stats.discarded, 1);
  await h.settle(u(B, 'preview/display-0001.json'), 'B1');
  assert.deepEqual(h.installed, [u(B, 'preview/display-0001.json')]);
  assert.equal(h.loader.state().installed.url, u(B, 'preview/display-0001.json'));
  assert.equal(h.loader.state().pending, null);
});

test('a newer preview of the build on screen installs as an intermediate step', async () => {
  const h = harness();
  h.loader.want(u(A, 'preview/display-0001.json'), src(A));
  await h.settle(u(A, 'preview/display-0001.json'), 'p1');
  h.loader.want(u(A, 'preview/display-0003.json'), src(A));
  h.loader.want(u(A, 'preview/display-0005.json'), src(A));
  await h.settle(u(A, 'preview/display-0003.json'), 'p3');
  await h.settle(u(A, 'preview/display-0005.json'), 'p5');
  assert.deepEqual(h.installed, ['0001', '0003', '0005'].map((n) => u(A, `preview/display-${n}.json`)));
});

test('an older preview of the build on screen is never installed over a newer one', async () => {
  const h = harness();
  h.loader.want(u(A, 'preview/display-0006.json'), src(A));
  await h.settle(u(A, 'preview/display-0006.json'), 'p6');
  h.loader.want(u(A, 'preview/display-0002.json'), src(A)); // e.g. a stale poll
  await h.settle(u(A, 'preview/display-0002.json'), 'p2');
  assert.deepEqual(h.installed, [u(A, 'preview/display-0006.json')]);
  assert.equal(h.loader.state().installed.url, u(A, 'preview/display-0006.json'));
  h.loader.want(u(A, 'preview/display-0002.json'), src(A));
  assert.equal(h.calls.length, 2, 'the refused older preview is not fetched again');
  h.loader.want(u(A, 'scene.json'), src(A));
  await h.settle(u(A, 'scene.json'), 'final');
  assert.equal(h.loader.state().installed.url, u(A, 'scene.json'));
});

test('a failed fetch keeps the installed content and records the failure; retry waits', async () => {
  let clock = 1000;
  const h = harness({ now: () => clock, retryAfterMs: 4000 });
  h.loader.want(u(A, 'scene.json'), src(A));
  await h.settle(u(A, 'scene.json'), 'A');
  h.loader.want(u(B, 'scene.json'), src(B, 2));
  await h.settle(u(B, 'scene.json'), 'HTTP 404', true);
  const s = h.loader.state();
  assert.equal(s.installed.url, u(A, 'scene.json'));
  assert.equal(s.error.url, u(B, 'scene.json'));
  assert.match(s.error.message, /404/);
  const before = h.calls.length;
  h.loader.want(u(B, 'scene.json'), src(B, 2)); // next poll, too soon
  assert.equal(h.calls.length, before);
  clock += 5000;
  h.loader.want(u(B, 'scene.json'), src(B, 2));
  assert.equal(h.calls.length, before + 1);
  await h.settle(u(B, 'scene.json'), 'B');
  assert.equal(h.loader.state().installed.url, u(B, 'scene.json'));
  assert.equal(h.loader.state().error, null);
});

test('an install refusal (e.g. identity mismatch) leaves the old content and reports it', async () => {
  const h = harness({ reject: (data) => data === 'bad' });
  h.loader.want(u(A, 'scene.json'), src(A));
  await h.settle(u(A, 'scene.json'), 'good');
  h.loader.want(u(B, 'scene.json'), src(B, 2));
  await h.settle(u(B, 'scene.json'), 'bad');
  assert.equal(h.loader.state().installed.url, u(A, 'scene.json'));
  assert.match(h.loader.state().error.message, /refused bad/);
});

test('withdrawal keeps installed content but records that nothing is wanted', async () => {
  const h = harness();
  h.loader.want(u(A, 'scene.json'), src(A));
  await h.settle(u(A, 'scene.json'), 'A');
  h.loader.want(null, null);
  const s = h.loader.state();
  assert.equal(s.desired, null);
  assert.equal(s.installed.url, u(A, 'scene.json'));
});

test('a response that arrives after its source was withdrawn is discarded', async () => {
  const h = harness();
  h.loader.want(u(A, 'scene.json'), src(A));
  h.loader.want(null, null);
  await h.settle(u(A, 'scene.json'), 'A');
  assert.deepEqual(h.installed, []);
  assert.equal(h.loader.state().installed, null);
});

test('re-requesting the installed URL does not refetch', async () => {
  const h = harness();
  h.loader.want(u(A, 'scene.json'), src(A));
  await h.settle(u(A, 'scene.json'), 'A');
  h.loader.want(u(A, 'scene.json'), src(A));
  h.loader.want(u(A, 'scene.json'), src(A));
  assert.equal(h.calls.length, 1);
});
