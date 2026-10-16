// Pure status derivation for the workspace: no DOM, no network, no Three.js.
// Everything here reads the service's /api/state payload as documented in
// docs/INTERACTIVE.md and docs/WORKFLOW_CONTRACTS.md; nothing is inferred
// from timing or invented.

export const ARTIFACTS = ['display', 'cad', 'pid', 'legend', 'description'];
export const ARTIFACT_LABEL = {
  display: '3D view', cad: 'CAD / STEP', pid: 'P&ID', legend: 'Legend', description: 'Description',
  index: 'Item index', inventory: 'Drawing inventory', recipe: 'Geometry recipe', verification: 'CAD verification',
};
// Published-file keys (service PUBLISHED_FILES) and the artifact whose status governs each.
export const FILE_KEYS = ['display', 'cad', 'pid', 'legend', 'description', 'index', 'inventory', 'recipe', 'verification'];
export const ARTIFACT_OF_KEY = {
  display: 'display', cad: 'cad', pid: 'pid', legend: 'legend', description: 'description',
  index: 'pid', inventory: 'pid', recipe: 'cad', verification: 'cad',
};
export const ACTIVE_STATUSES = new Set(['queued', 'running']);
export const TERMINAL_STATUSES = new Set(['complete', 'pending', 'failed', 'cancelled', 'superseded']);

export function shortDigest(digest) {
  return typeof digest === 'string' && digest.length >= 8 ? digest.slice(0, 8) : '—';
}

export function shortBuild(buildId) {
  // b20261014T072622-r0-66f3af3f -> 07:26:22 · 66f3af3f
  const m = /^b(\d{8})T(\d{2})(\d{2})(\d{2})-r(\d+)-([0-9a-f]{8})$/.exec(buildId || '');
  return m ? `${m[2]}:${m[3]}:${m[4]} · ${m[6]}` : (buildId || '—');
}

/**
 * Source identity of one file key as shown in a view.
 * mode 'live' follows state.artifacts/artifact_sources (working preview when one exists);
 * mode 'published' shows the last verified complete set only.
 */
export function viewSource(state, key, mode = 'live') {
  if (!state) return null;
  if (mode === 'published') {
    const pub = state.published;
    if (!pub || !pub.artifacts || !pub.artifacts[key]) return null;
    return {
      url: pub.artifacts[key], origin: 'published', kind: 'output', build_id: pub.build_id,
      build_status: 'complete', artifact_status: 'complete', source_revision: pub.source_revision,
      input_digest: pub.input_digest,
    };
  }
  const url = state.artifacts && state.artifacts[key];
  const src = state.artifact_sources && state.artifact_sources[key];
  if (!url || !src) return null;
  return { url, ...src };
}

/** True only for a source that is the service's verified publication (state.published). */
export function isVerifiedPublished(src, published) {
  return !!(src && published && src.origin === 'published' && src.build_status === 'complete'
    && src.artifact_status === 'complete' && src.integrity !== 'rejected'
    && published.build_id === src.build_id && published.source_revision === src.source_revision
    && published.input_digest === src.input_digest);
}

/**
 * Badge for a view's source: {tone, label, detail}. Tones: current, working, pending,
 * failed, cancelled, stale, unverified, none. "current" only when the source is the
 * verified publication (`published`, i.e. state.published) of exactly the project's
 * current revision; lifecycle completion alone is never enough.
 */
export function sourceBadge(src, projectRevision, published = null) {
  if (!src) return { tone: 'none', label: 'No output yet', detail: 'Neither a working preview nor a published output exists.' };
  const rev = src.source_revision;
  const behind = typeof projectRevision === 'number' && rev !== projectRevision;
  const st = src.build_status;
  const art = src.artifact_status;
  let tone;
  let label;
  if (art === 'rejected' || src.integrity === 'rejected') {
    tone = 'failed';
    label = `Rejected · r${rev} · failed verification`;
  } else if (src.origin === 'published') {
    if (isVerifiedPublished(src, published)) {
      tone = behind ? 'stale' : 'current';
      label = behind ? `Stale · published r${rev}` : `Current · r${rev}`;
    } else {
      tone = 'unverified';
      label = `Unverified · r${rev}`;
    }
  } else if (ACTIVE_STATUSES.has(st)) {
    tone = 'working';
    if (src.kind === 'preview') label = `Working preview · r${rev}`;
    else label = `Output written · r${rev} · awaiting verification`;
  } else if (st === 'pending') {
    tone = 'pending';
    label = `Pending · r${rev} · not published`;
  } else if (st === 'failed') {
    tone = 'failed';
    label = art === 'failed' ? `Failed · r${rev} draft` : `Draft of failed build · r${rev}`;
  } else if (st === 'cancelled') {
    tone = 'cancelled';
    label = `Cancelled draft · r${rev}`;
  } else if (st === 'superseded') {
    tone = 'stale';
    label = `Superseded draft · r${rev}`;
  } else if (st === 'complete') {
    // A completed build that is not offered as the verified publication is not current.
    tone = 'unverified';
    label = `Unverified · r${rev}`;
  } else {
    tone = 'none';
    label = `r${rev} · ${st || 'unknown'}`;
  }
  if (behind && tone !== 'stale') label += ` · project r${projectRevision}`;
  const kind = src.kind === 'preview' ? 'progressive preview' : 'output file';
  const detail = `${kind} from build ${src.build_id || '—'} (${st || 'unknown'}${art ? `, artifact ${art}` : ''}); ` +
    `source revision ${rev}, digest ${shortDigest(src.input_digest)}` +
    (behind ? `; the project is at revision ${projectRevision}, so this view is not current.` : '.');
  return { tone, label, detail, behind };
}

const SYNC_TONE = {
  synchronized: 'current', building: 'working', pending: 'pending', failed: 'failed',
  cancelled: 'cancelled', stale: 'stale', unpublished: 'none', invalid: 'failed', blocked: 'failed',
};

/** Overall synchronization badge; "Synchronized" only when the service says so. */
export function syncBadge(state) {
  const sync = state && state.sync;
  if (!sync || !sync.state) return { tone: 'none', label: 'Status unknown', detail: 'The service did not report a synchronization state.' };
  const rev = state.project ? state.project.revision : sync.current_revision;
  const labels = {
    synchronized: `Synchronized · r${rev}`,
    building: `Building r${rev}`,
    pending: `Pending r${rev}`,
    failed: `Build failed · r${rev}`,
    cancelled: `Cancelled · r${rev}`,
    stale: sync.published_revision != null ? `Outputs stale · published r${sync.published_revision}` : 'Outputs stale',
    unpublished: 'No complete output set',
    invalid: 'Published set invalid',
    blocked: 'Blocked · notes unreadable',
  };
  return {
    tone: SYNC_TONE[sync.state] || 'unverified',
    label: labels[sync.state] || `Unknown state: ${sync.state}`,
    detail: sync.message || '',
    synchronized: sync.state === 'synchronized' && sync.published_current === true,
  };
}

/**
 * Freshness gate for /api/state payloads that may arrive out of order.
 * applied = {startedAt, revision, buildId, buildSeq, reqSeq, selectionSeq} of the state on screen.
 * Returns true when the incoming payload (issued as request number reqSeq) is not older.
 */
export function shouldApplyState(applied, incoming, reqSeq) {
  if (!incoming || typeof incoming !== 'object' || !incoming.project) return false;
  if (!applied) return true;
  const started = incoming.service ? incoming.service.started_at ?? null : null;
  if (started !== applied.startedAt) return reqSeq > applied.reqSeq; // service restarted: newest request wins
  const rev = incoming.project.revision;
  if (rev < applied.revision) return false;
  if (rev > applied.revision) return true;
  const buildId = incoming.build ? incoming.build.id : null;
  const seq = incoming.build ? incoming.build.last_sequence || 0 : 0;
  const selSeq = incoming.selection ? incoming.selection.sequence || 0 : 0;
  if (buildId === applied.buildId) {
    if (seq < applied.buildSeq) return false;
    if (seq > applied.buildSeq) return true;
    return reqSeq > applied.reqSeq || selSeq > applied.selectionSeq;
  }
  return reqSeq > applied.reqSeq;
}

export function appliedMarker(state, reqSeq) {
  return {
    startedAt: state.service ? state.service.started_at ?? null : null,
    revision: state.project.revision,
    buildId: state.build ? state.build.id : null,
    buildSeq: state.build ? state.build.last_sequence || 0 : 0,
    selectionSeq: state.selection ? state.selection.sequence || 0 : 0,
    reqSeq,
  };
}

/** Per-artifact progress from the ordered events of one build: latest completed/total and message. */
export function artifactProgress(build) {
  const out = {};
  for (const a of ARTIFACTS) out[a] = { status: build && build.artifact_status ? build.artifact_status[a] : null, completed: null, total: null, message: null, previews: 0 };
  if (!build || !Array.isArray(build.events)) return out;
  for (const e of build.events) {
    if (!e || !out[e.artifact]) continue;
    const slot = out[e.artifact];
    if (typeof e.completed === 'number') slot.completed = e.completed;
    if (typeof e.total === 'number') slot.total = e.total;
    if (e.kind === 'preview') slot.previews += 1;
    slot.message = e.message;
  }
  return out;
}

/** Elapsed seconds of a build from real data: last event's elapsed_s, or wall clock while running. */
export function buildElapsed(build, nowMs = Date.now()) {
  if (!build) return null;
  if (ACTIVE_STATUSES.has(build.status) && build.started_at) {
    const started = Date.parse(build.started_at);
    if (Number.isFinite(started)) return Math.max(0, (nowMs - started) / 1000);
  }
  if (build.started_at && build.finished_at) {
    const a = Date.parse(build.started_at);
    const b = Date.parse(build.finished_at);
    if (Number.isFinite(a) && Number.isFinite(b)) return Math.max(0, (b - a) / 1000);
  }
  const events = Array.isArray(build.events) ? build.events : [];
  const last = events.length ? events[events.length - 1] : null;
  return last && typeof last.elapsed_s === 'number' ? last.elapsed_s : null;
}

/** Diagnostics from state.diagnostics that mention one of the IDs. */
export function diagnosticsFor(state, ids) {
  const set = new Set(ids);
  return (state && Array.isArray(state.diagnostics) ? state.diagnostics : [])
    .filter((d) => Array.isArray(d.entity_ids) && d.entity_ids.some((id) => set.has(id)));
}

export function formatSeconds(s) {
  if (s == null || !Number.isFinite(s)) return '—';
  if (s < 10) return `${s.toFixed(2)} s`;
  if (s < 120) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
