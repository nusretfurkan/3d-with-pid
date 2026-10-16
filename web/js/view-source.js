// Displayed identity of a viewport (pure; unit-tested in Node).
//
// A view distinguishes the file it *wants* (the newest source the service offers)
// from the file actually *installed* on screen. The badge always describes the
// installed file, as the service describes that exact URL now; a wanted newer
// file only adds a "loading" or "failed to load" note until it is installed.
// A file the service no longer offers is labelled withdrawn/previous, never current.

import { ARTIFACT_OF_KEY, shortDigest, sourceBadge } from './status.js';

/**
 * Every artifact URL the service currently offers, with its source identity.
 * Rejected builds offer nothing (the service empties their previews/artifacts).
 */
export function offeredSources(state) {
  const out = new Map();
  if (!state) return out;
  const sources = state.artifact_sources || {};
  for (const [key, url] of Object.entries(state.artifacts || {})) {
    if (url && sources[key] && !out.has(url)) out.set(url, { ...sources[key], key, url });
  }
  const pub = state.published;
  if (pub && pub.artifacts) {
    for (const [key, url] of Object.entries(pub.artifacts)) {
      if (url && !out.has(url)) {
        out.set(url, {
          key, url, origin: 'published', kind: 'output', build_id: pub.build_id, build_status: 'complete',
          artifact_status: 'complete', source_revision: pub.source_revision, input_digest: pub.input_digest,
        });
      }
    }
  }
  const b = state.build;
  if (b && b.integrity !== 'rejected' && b.status !== 'complete') {
    const add = (key, url, kind) => {
      if (!url || out.has(url)) return;
      out.set(url, {
        key, url, origin: 'working', kind, build_id: b.id, build_status: b.status,
        artifact_status: b.artifact_status ? b.artifact_status[ARTIFACT_OF_KEY[key] || key] ?? null : null,
        source_revision: b.source_revision, input_digest: b.input_digest,
      });
    };
    for (const [key, url] of Object.entries(b.previews || {})) add(key, url, 'preview');
    for (const [key, url] of Object.entries(b.artifacts || {})) add(key, url, /\/preview\//.test(url) ? 'preview' : 'output');
  }
  return out;
}

function noun(src) {
  return src && src.kind === 'preview' ? 'preview' : 'output';
}

/**
 * Badge for a view slot {desired, installed, pending, error} (see loader.js):
 * {tone, label, detail, note: {tone, text} | null}.
 */
export function slotBadge(slot, state) {
  const revision = state && state.project ? state.project.revision : null;
  const published = state ? state.published : null;
  const desired = slot ? slot.desired : null;
  const installed = slot ? slot.installed : null;
  const error = slot ? slot.error : null;
  const desiredFailed = !!(desired && error && error.url === desired.url);

  if (!installed) {
    if (desiredFailed) {
      return { tone: 'failed', label: `Load failed · r${desired.src.source_revision}`, detail: `${desired.url}: ${error.message}`, note: null };
    }
    if (desired) {
      return { tone: 'loading', label: `Loading r${desired.src.source_revision} ${noun(desired.src)}…`, detail: desired.url, note: null };
    }
    return { tone: 'none', label: 'No output yet', detail: 'The service offers no file for this view.', note: null };
  }

  const live = offeredSources(state).get(installed.url);
  let badge;
  if (live) {
    badge = sourceBadge(live, revision, published);
  } else {
    const rev = installed.src.source_revision;
    const integrity = (state && state.sync && state.sync.state === 'invalid')
      || (state && state.build && state.build.id === installed.src.build_id && state.build.integrity === 'rejected');
    badge = integrity
      ? { tone: 'failed', label: `Withdrawn · r${rev} · failed verification`, detail: `${installed.url} is no longer offered: the service could not verify it.` }
      : { tone: 'stale', label: `Previous ${noun(installed.src)} · r${rev}`, detail: `${installed.url} is no longer offered by the service.` };
  }

  let note = null;
  if (desired && desired.url !== installed.url) {
    note = desiredFailed
      ? { tone: 'failed', text: `r${desired.src.source_revision} ${noun(desired.src)} failed to load` }
      : { tone: 'loading', text: `loading r${desired.src.source_revision} ${noun(desired.src)}…` };
  } else if (!desired && !live) {
    note = { tone: 'failed', text: 'not offered now' };
  }
  return { ...badge, note };
}

/** An immutable artifact must carry the identity the service attached to its URL; throws otherwise. */
export function checkIdentity(what, actual, src) {
  if (actual.revision !== src.source_revision || actual.digest !== src.input_digest) {
    throw new Error(`${what} identity (revision ${actual.revision}, digest ${shortDigest(actual.digest)}) does not match its offered source ` +
      `(revision ${src.source_revision}, digest ${shortDigest(src.input_digest)}); not installed`);
  }
}
