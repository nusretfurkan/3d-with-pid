// Pure model of the read-only Ask conversation (no DOM, no network; unit-tested in Node).
// It builds the exact POST /api/ask body of docs/ASSISTANT_3D_CONTRACTS.md, refuses what the
// service would refuse before anything is sent, keeps only contracted fields of a reply as
// inert plain text, and states how an answer's frozen source relates to the current model and
// to the content a pane shows. The service is the authority for every project fact.

export const ASK_LIMITS = Object.freeze({
  question: 4000, requestId: 80, selectedIds: 16, history: 6, historyField: 2000,
});
/** Bounds of what the UI keeps and shows from one reply (the service bounds its own output). */
export const SHOW_LIMITS = Object.freeze({
  answer: 20000, references: 64, list: 32, item: 2000, tag: 120, id: 200, kind: 80, metadata: 24,
});
export const MAX_TURNS = 24;
export const STATUSES = Object.freeze(['answered', 'clarification', 'unavailable', 'cancelled']);
export const ERROR_CODES = Object.freeze({ stale: 'stale_ask', invalid: 'invalid_ask', busy: 'ask_busy' });

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
// U+0000–U+001F (except tab/newline/CR) and U+007F–U+009F
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/;

/** Length as the service counts it: Unicode code points. */
export function codePoints(text) {
  return [...String(text ?? '')].length;
}

function clip(text, limit) {
  const chars = [...String(text ?? '')];
  return chars.length <= limit ? chars.join('') : `${chars.slice(0, limit - 1).join('')}…`;
}

/** A fresh client request ID: `ask-` and 32 hex digits from the supplied random bytes. */
export function newRequestId(randomBytes) {
  const bytes = randomBytes(16);
  return `ask-${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}

export function safeRequestId(id) {
  return typeof id === 'string' && id.length > 0 && id.length <= ASK_LIMITS.requestId && SAFE_ID.test(id);
}

/** Conversation scope: a different project or a restarted service starts a new conversation. */
export function identityKey(state) {
  if (!state || !state.project) return null;
  const started = state.service ? state.service.started_at ?? '' : '';
  return `${state.project.id}|${started}`;
}

/** Revision and digest of the current authoritative model, as the service reports them. */
export function currentSource(state) {
  if (!state || !state.project) return null;
  const { document: doc } = state; // the service's document record, not the DOM
  const digest = doc && typeof doc.input_digest === 'string' ? doc.input_digest : null;
  return { revision: state.project.revision, digest };
}

/**
 * displayed_source of the contract from a pane's installed source: null when the pane shows
 * nothing identifiable, otherwise {source_revision, input_digest, mode} with mode live/published.
 */
export function displayedSource(installedSrc, paneMode) {
  if (!installedSrc || !Number.isInteger(installedSrc.source_revision) || typeof installedSrc.input_digest !== 'string') return null;
  return {
    source_revision: installedSrc.source_revision,
    input_digest: installedSrc.input_digest,
    mode: paneMode === 'published' ? 'published' : 'live',
  };
}

/** Stable IDs of the selection that still exist in the project (entities, ports, connections). */
export function knownIds(project, ids) {
  const m = project && project.model;
  if (!m) return [];
  return [...new Set((ids || []).filter((id) => typeof id === 'string' && (id in m.entities || id in m.ports || id in m.connections)))];
}

/** Current human label of a stable ID (tag, TAG.port), or null when it no longer exists. */
export function currentLabel(project, id) {
  const m = project && project.model;
  if (!m || typeof id !== 'string') return null;
  if (m.entities[id]) return m.entities[id].tag;
  if (m.connections[id]) return m.connections[id].tag;
  if (m.ports[id]) {
    const port = m.ports[id];
    const owner = m.entities[port.entity_id];
    return `${owner ? owner.tag : '?'}.${port.name}`;
  }
  return null;
}

/** The last answered turns as bounded {question, answer} records (oldest first). */
export function historyFrom(turns) {
  const done = (turns || []).filter((t) => t && t.answer && (t.answer.status === 'answered' || t.answer.status === 'clarification')
    && typeof t.question === 'string' && typeof t.answer.answer === 'string' && t.answer.answer.trim());
  return done.slice(-ASK_LIMITS.history).map((t) => ({
    question: clip(t.question, ASK_LIMITS.historyField),
    answer: clip(t.answer.answer, ASK_LIMITS.historyField),
  }));
}

/**
 * Check a question before sending: {ok, problem, message, length}. Problems: empty, too_long,
 * control, too_many_ids, offline, no_state, busy.
 */
export function preflightAsk({ question, selectedIds = [], online = true, state = null, busy = false }) {
  const text = String(question ?? '').trim();
  const length = codePoints(text);
  const out = (problem, message) => ({ ok: !problem, problem, message, length });
  if (!state || !state.project) return out('no_state', 'Waiting for the project state from the local service.');
  if (online === false) return out('offline', 'The local service is not reachable; your question stays here until it is.');
  if (busy) return out('busy', 'One question at a time: wait for the current answer or cancel it.');
  if (!length) return out('empty', '');
  if (length > ASK_LIMITS.question) return out('too_long', `Over the ${ASK_LIMITS.question.toLocaleString('en-US')}-character limit by ${(length - ASK_LIMITS.question).toLocaleString('en-US')}.`);
  if (CONTROL.test(text)) return out('control', 'The question contains control characters the service refuses.');
  if (selectedIds.length > ASK_LIMITS.selectedIds) {
    return out('too_many_ids', `${selectedIds.length} items are selected; at most ${ASK_LIMITS.selectedIds} can go with a question. Narrow the selection or clear the context.`);
  }
  return out(null, '');
}

/** The exact request body: required fields always, optional history/displayed_source only when present. */
export function buildAskRequest({ requestId, question, selectedIds = [], state, history = [], displayed = null }) {
  if (!safeRequestId(requestId)) throw new Error('request ID must be a safe identifier of at most 80 characters');
  const source = currentSource(state);
  if (!source || typeof source.digest !== 'string') throw new Error('the current revision and digest are unknown');
  const body = {
    request_id: requestId,
    question: String(question).trim(),
    selected_ids: knownIds(state.project, selectedIds).slice(0, ASK_LIMITS.selectedIds),
    expected_revision: source.revision,
    input_digest: source.digest,
  };
  const past = (history || []).slice(-ASK_LIMITS.history).map((r) => ({
    question: clip(r.question, ASK_LIMITS.historyField), answer: clip(r.answer, ASK_LIMITS.historyField),
  }));
  if (past.length) body.history = past;
  if (displayed) body.displayed_source = { source_revision: displayed.source_revision, input_digest: displayed.input_digest, mode: displayed.mode };
  return body;
}

function text(value, limit) {
  return typeof value === 'string' ? clip(value, limit) : null;
}

function strings(value, limit = SHOW_LIMITS.list) {
  if (!Array.isArray(value)) return [];
  return value.filter((v) => typeof v === 'string' && v.trim()).slice(0, limit).map((v) => clip(v, SHOW_LIMITS.item));
}

const SECRET_KEY = /token|secret|key|auth|credential|password|cookie/i;

/** Scalar responder facts as [label, text] pairs, three levels deep; credential-like keys are never shown. */
function scalarMetadata(responder) {
  const out = [];
  const walk = (record, prefix, depth) => {
    for (const [key, value] of Object.entries(record || {})) {
      if (out.length >= SHOW_LIMITS.metadata) return;
      if (SECRET_KEY.test(key)) continue;
      const label = prefix ? `${prefix} ${key}` : key;
      if (['string', 'number', 'boolean'].includes(typeof value)) out.push([clip(label, 60), clip(String(value), 200)]);
      else if (Array.isArray(value) && value.every((v) => ['string', 'number', 'boolean'].includes(typeof v))) out.push([clip(label, 60), clip(value.join(', ') || 'none', 200)]);
      else if (value && typeof value === 'object' && !Array.isArray(value) && depth < 2) walk(value, label, depth + 1);
    }
  };
  walk(responder, '', 0);
  return out;
}

/**
 * Keep only the contracted reply fields, as plain strings/numbers, or explain why the reply
 * cannot be shown: {ok, answer, problems}. Anything else (operations, commands, HTML, URLs as
 * links) is never carried into the UI; the strings that remain are rendered as text.
 */
export function readAnswer(body, request) {
  const problems = [];
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, answer: null, problems: ['the reply is not a JSON object'] };
  if (request && body.request_id !== request.request_id) problems.push('the reply belongs to another question');
  if (!STATUSES.includes(body.status)) problems.push(`unknown status ${JSON.stringify(String(body.status)).slice(0, 40)}`);
  if (!Number.isInteger(body.source_revision)) problems.push('the reply has no source revision');
  if (typeof body.input_digest !== 'string') problems.push('the reply has no source digest');
  if (typeof body.answer !== 'string') problems.push('the reply has no answer text');
  if (problems.length) return { ok: false, answer: null, problems };
  const references = [];
  for (const r of Array.isArray(body.references) ? body.references : []) {
    if (references.length >= SHOW_LIMITS.references) break;
    if (!r || typeof r !== 'object' || typeof r.id !== 'string' || !r.id) continue;
    references.push({
      id: clip(r.id, SHOW_LIMITS.id),
      tag: text(r.tag, SHOW_LIMITS.tag),
      kind: text(r.kind, SHOW_LIMITS.kind),
      field_paths: strings(r.field_paths),
      facts: strings(r.facts),
    });
  }
  const responder = body.responder && typeof body.responder === 'object' && !Array.isArray(body.responder) ? body.responder : {};
  const bridge = responder.bridge && typeof responder.bridge === 'object' && !Array.isArray(responder.bridge) ? responder.bridge : {};
  const fault = responder.error && typeof responder.error === 'object' ? responder.error : null;
  const shown = body.displayed_source && typeof body.displayed_source === 'object' ? body.displayed_source : null;
  return {
    ok: true,
    problems: [],
    answer: {
      id: text(body.id, SHOW_LIMITS.id),
      request_id: body.request_id,
      project_id: text(body.project_id, SHOW_LIMITS.id),
      source_revision: body.source_revision,
      input_digest: body.input_digest,
      current_revision: Number.isInteger(body.current_revision) ? body.current_revision : null,
      stale: body.stale === true,
      status: body.status,
      answer: clip(body.answer, SHOW_LIMITS.answer),
      references,
      unknowns: strings(body.unknowns),
      limitations: strings(body.limitations),
      responder: {
        kind: text(responder.kind, SHOW_LIMITS.kind),
        language_model: responder.language_model === true || (typeof responder.language_model === 'string' && responder.language_model.length > 0),
        model: typeof responder.language_model === 'string' ? clip(responder.language_model, 120) : text(responder.model, 120) || text(bridge.model, 120),
        // the question and project facts left this computer (also when the model then failed)
        model_called: responder.model_called === true || responder.language_model === true || typeof responder.language_model === 'string',
        query: text(responder.query, SHOW_LIMITS.kind),
        error: fault ? { code: text(fault.code, 60), message: text(fault.message, SHOW_LIMITS.item) } : null,
        facts: scalarMetadata(responder),
      },
      displayed_source: shown && Number.isInteger(shown.source_revision) && typeof shown.input_digest === 'string'
        ? { source_revision: shown.source_revision, input_digest: shown.input_digest, mode: shown.mode === 'published' ? 'published' : 'live' }
        : null,
    },
  };
}

/**
 * How an answer relates to the current model now: {stale, tone, text}. An answer stays on its
 * frozen revision; when the model has moved on (or the service already said so) it is stale.
 */
export function answerFreshness(answer, state) {
  const now = currentSource(state);
  if (!answer || !now) return { stale: false, tone: 'none', text: '' };
  const src = answer.source_revision;
  const stale = (text) => ({ stale: true, tone: 'stale', text });
  // the newest current revision known: the latest state, or the service's own report when the answer
  // completed if that is newer (an answer can arrive before the next state poll does)
  const reported = Number.isInteger(answer.current_revision) ? answer.current_revision : null;
  const current = reported !== null && reported > now.revision ? reported : now.revision;
  if (current !== src) return stale(`Answer based on r${src}; current r${current}`);
  if (now.digest && answer.input_digest !== now.digest) return stale(`Answer based on r${src} (an earlier digest); the current model differs`);
  if (answer.stale) {
    // the service could not read its persisted source when the answer completed: nothing proves it current
    if (reported === null) return stale(`Answer based on r${src}; whether that was still the current model could not be verified when the answer completed`);
    return stale(`Answer based on r${src} (an earlier digest); the current model differs`);
  }
  return { stale: false, tone: 'current', text: `Answer based on the current model r${now.revision}` };
}

/**
 * Whether the answer's model source and the content a pane showed differ: then both are named
 * ("Answer: current model r9; visible publication r6"). null when they are the same source.
 */
export function visibleSourceNote(answer) {
  const shown = answer && answer.displayed_source;
  if (!shown) return null;
  if (shown.source_revision === answer.source_revision && shown.input_digest === answer.input_digest) return null;
  const what = shown.mode === 'published' ? 'visible publication' : 'visible live view';
  return `Answer: model r${answer.source_revision}; ${what} r${shown.source_revision}. The answer describes the model, not that older content.`;
}

/** A reference as clickable now: {exists, label, renamed} (historical tag text stays as answered). */
export function referenceNow(ref, project) {
  const label = currentLabel(project, ref && ref.id);
  if (label == null) return { exists: false, label: null, renamed: false };
  return { exists: true, label, renamed: !!(ref.tag && label !== ref.tag) };
}

/** True when a question reads like an edit request (offered as a Change draft, never sent there). */
export function looksLikeEdit(question) {
  const t = String(question ?? '').trim().toLowerCase();
  if (!t || /\?\s*$/.test(t)) return false;
  return /^(please\s+)?(rename|set|move|insert|add|change|delete|remove|increase|decrease|reduce|make|replace|connect|disconnect|place|rotate|resize|reroute|swap|undo|redo)\b/.test(t);
}

/** Drop the oldest turns beyond MAX_TURNS (never the running one): {turns, dropped}. */
export function boundTurns(turns, max = MAX_TURNS) {
  if (turns.length <= max) return { turns, dropped: 0 };
  const dropped = turns.length - max;
  return { turns: turns.slice(dropped), dropped };
}

/**
 * Plain explanation of a failed /api/ask call: {tone, title, text, retry}. A stale refusal names no
 * revision of its own: the state on screen may not have caught up with the service, so the text
 * quotes the service's message as given and the retry control names the latest received revision.
 * (currentRevision is accepted for callers but not used for that reason.)
 */
export function askFailure(error, currentRevision) {
  if (!error) return { tone: 'failed', title: 'Not answered', text: 'Unknown failure.', retry: true };
  if (error.aborted) return { tone: 'cancelled', title: 'Stopped waiting', text: 'Nothing was changed. The service was asked to cancel this question.', retry: true };
  const codes = (error.diagnostics || []).map((d) => d && d.code);
  if (error.status === 409 && codes.includes(ERROR_CODES.stale)) {
    const said = typeof error.error === 'string' && error.error.trim() ? ` The service said: “${clip(error.error.trim(), 300)}”` : '';
    return { tone: 'stale', title: 'Not asked: the project changed', text: `The project changed before the question was accepted; nothing was sent to a model.${said}`, retry: true };
  }
  if (error.status === 409 && codes.includes(ERROR_CODES.busy)) {
    return { tone: 'pending', title: 'Service busy', text: 'Another model question is running on the service. Try again when it has finished.', retry: true };
  }
  if (error.status === 400) return { tone: 'failed', title: 'Question refused', text: error.error || 'The service refused the question.', retry: false };
  if (error.status === 404 || error.status === 405) {
    return { tone: 'failed', title: 'Ask is not offered', text: `This service does not offer Ask (HTTP ${error.status}). Change requests still work.`, retry: false };
  }
  if (error.network) return { tone: 'failed', title: 'No answer', text: `${error.error}. Your question is kept; nothing was changed.`, retry: true };
  return { tone: 'failed', title: `Not answered (HTTP ${error.status})`, text: error.error || 'The service could not answer.', retry: true };
}
