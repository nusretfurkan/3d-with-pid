// Displayed identity: badges describe the installed file, never merely the requested one (review finding 4).
import test from 'node:test';
import assert from 'node:assert/strict';
import { offeredSources, slotBadge } from '../../../web/js/view-source.js';

const D0 = '0'.repeat(64);
const D1 = '1'.repeat(64);
const B0 = 'b20261014T072622-r0-66f3af3f';
const B1 = 'b20261014T080000-r1-11111111';
const url = (b, p) => `/artifacts/${b}/${p}`;
const published = { build_id: B0, source_revision: 0, input_digest: D0, artifacts: { display: url(B0, 'scene.json'), pid: url(B0, 'pid.svg') } };

function stateAt(revision, { build = null, artifacts = {}, sources = {}, pub = published, sync = 'synchronized' } = {}) {
  return { project: { revision }, published: pub, build, artifacts, artifact_sources: sources, sync: { state: sync } };
}

const pubSrc = { origin: 'published', kind: 'output', build_id: B0, build_status: 'complete', artifact_status: 'complete', source_revision: 0, input_digest: D0 };
const prevSrc = (n) => ({ origin: 'working', kind: 'preview', build_id: B1, build_status: 'running', artifact_status: 'running', source_revision: 1, input_digest: D1, n });
const runningBuild = (previews) => ({ id: B1, status: 'running', source_revision: 1, input_digest: D1, integrity: null, previews, artifacts: {}, artifact_status: { display: 'running' } });

test('offered sources cover live artifacts, the publication and the running build', () => {
  const state = stateAt(1, {
    build: runningBuild({ display: url(B1, 'preview/display-0003.json') }),
    artifacts: { display: url(B1, 'preview/display-0003.json') },
    sources: { display: prevSrc(3) },
  });
  const offered = offeredSources(state);
  assert.equal(offered.get(url(B1, 'preview/display-0003.json')).origin, 'working');
  assert.equal(offered.get(url(B0, 'scene.json')).origin, 'published');
  assert.equal(offered.get(url(B0, 'pid.svg')).key, 'pid');
});

test('a rejected build offers nothing, even with leftover URLs', () => {
  const rejected = { id: B1, status: 'complete', integrity: 'rejected', source_revision: 1, input_digest: D1, previews: { display: url(B1, 'preview/display-0013.json') }, artifacts: { display: url(B1, 'scene.json') } };
  const offered = offeredSources(stateAt(1, { build: rejected, pub: null, sync: 'invalid' }));
  assert.equal(offered.size, 0);
});

test('the badge keeps describing old installed content while a newer file loads', () => {
  const desiredUrl = url(B1, 'preview/display-0003.json');
  const state = stateAt(1, { build: runningBuild({ display: desiredUrl }), artifacts: { display: desiredUrl }, sources: { display: prevSrc(3) } });
  const slot = { installed: { url: url(B0, 'scene.json'), src: pubSrc }, desired: { url: desiredUrl, src: prevSrc(3) }, pending: { url: desiredUrl }, error: null };
  const b = slotBadge(slot, state);
  assert.equal(b.tone, 'stale'); // r0 publication shown while the project is at r1
  assert.match(b.label, /published r0/);
  assert.equal(b.note.tone, 'loading');
  assert.match(b.note.text, /r1 preview/);
});

test('a failed download is reported without relabelling the installed content', () => {
  const desiredUrl = url(B1, 'preview/display-0004.json');
  const state = stateAt(1, { build: runningBuild({ display: desiredUrl }), artifacts: { display: desiredUrl }, sources: { display: prevSrc(4) } });
  const slot = { installed: { url: url(B0, 'scene.json'), src: pubSrc }, desired: { url: desiredUrl, src: prevSrc(4) }, pending: null, error: { url: desiredUrl, message: 'HTTP 404' } };
  const b = slotBadge(slot, state);
  assert.match(b.label, /published r0/);
  assert.equal(b.note.tone, 'failed');
  assert.match(b.note.text, /failed to load/);
});

test('withdrawn content after an integrity failure is never current', () => {
  const state = stateAt(0, { pub: null, sync: 'invalid' });
  const slot = { installed: { url: url(B0, 'scene.json'), src: pubSrc }, desired: null, pending: null, error: null };
  const b = slotBadge(slot, state);
  assert.equal(b.tone, 'failed');
  assert.match(b.label, /Withdrawn/);
  assert.equal(b.note.text, 'not offered now');
});

test('content no longer offered for other reasons is labelled previous, not current', () => {
  const state = stateAt(0, { pub: { ...published, build_id: 'b20261014T090000-r0-22222222', artifacts: {} } });
  const slot = { installed: { url: url(B0, 'scene.json'), src: pubSrc }, desired: null, pending: null, error: null };
  const b = slotBadge(slot, state);
  assert.equal(b.tone, 'stale');
  assert.match(b.label, /Previous output · r0/);
});

test('the same immutable file is relabelled when the service verifies and publishes it', () => {
  const sceneUrl = url(B1, 'scene.json');
  const outSrc = { origin: 'working', kind: 'output', build_id: B1, build_status: 'running', artifact_status: 'running', source_revision: 1, input_digest: D1 };
  const slot = { installed: { url: sceneUrl, src: outSrc }, desired: { url: sceneUrl, src: outSrc }, pending: null, error: null };
  const running = stateAt(1, { build: { ...runningBuild({}), artifacts: { display: sceneUrl } }, artifacts: { display: sceneUrl }, sources: { display: outSrc } });
  assert.equal(slotBadge(slot, running).tone, 'working');
  const pub1 = { build_id: B1, source_revision: 1, input_digest: D1, artifacts: { display: sceneUrl } };
  const done = stateAt(1, {
    pub: pub1, build: { id: B1, status: 'complete', integrity: 'verified', source_revision: 1, input_digest: D1 },
    artifacts: { display: sceneUrl },
    sources: { display: { origin: 'published', kind: 'output', build_id: B1, build_status: 'complete', artifact_status: 'complete', source_revision: 1, input_digest: D1 } },
  });
  const b = slotBadge(slot, done);
  assert.equal(b.tone, 'current');
  assert.equal(b.note, null);
});

test('nothing installed: loading, failed or no output', () => {
  const state = stateAt(0);
  const desired = { url: url(B0, 'scene.json'), src: pubSrc };
  assert.equal(slotBadge({ installed: null, desired, pending: { url: desired.url }, error: null }, state).tone, 'loading');
  assert.equal(slotBadge({ installed: null, desired, pending: null, error: { url: desired.url, message: 'boom' } }, state).tone, 'failed');
  assert.equal(slotBadge({ installed: null, desired: null, pending: null, error: null }, state).tone, 'none');
});
