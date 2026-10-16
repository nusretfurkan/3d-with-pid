// Ask request/reply rules of the assistant (assistant-model.js) against the frozen contract
// in docs/ASSISTANT_3D_CONTRACTS.md, with the pump-skid fixture (read only).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  ASK_LIMITS, MAX_TURNS, answerFreshness, askFailure, boundTurns, buildAskRequest, codePoints, currentLabel, displayedSource,
  historyFrom, identityKey, knownIds, looksLikeEdit, newRequestId, preflightAsk, readAnswer, referenceNow, safeRequestId,
  visibleSourceNote,
} from '../../../web/js/assistant-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const project = JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;
const DIGEST = 'a'.repeat(64);

function stateAt(revision = 4, digest = DIGEST, startedAt = '2026-10-15T08:00:00Z') {
  return { project: { ...project, revision }, document: { input_digest: digest }, service: { started_at: startedAt } };
}

const bytes = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 37 + 11) & 255);
const pumpId = Object.keys(project.model.entities).find((id) => project.model.entities[id].kind === 'centrifugal_pump');
const connId = Object.keys(project.model.connections)[0];
const portId = Object.keys(project.model.ports)[0];

function reply(request, extra = {}) {
  return {
    id: 'ans-1', request_id: request.request_id, project_id: project.id, source_revision: request.expected_revision,
    input_digest: request.input_digest, current_revision: request.expected_revision, stale: false, status: 'answered',
    answer: 'P-101 is a centrifugal pump.', references: [{ id: pumpId, tag: 'P-101', kind: 'centrifugal_pump', field_paths: ['model.entities.x.tag'], facts: ['tag P-101'] }],
    unknowns: ['duty point'], limitations: ['Model connectivity, not measured flow.'], responder: { kind: 'local', language_model: null },
    displayed_source: null, ...extra,
  };
}

test('request IDs are safe, fresh-looking and within 80 characters', () => {
  const id = newRequestId(bytes);
  assert.match(id, /^ask-[0-9a-f]{32}$/);
  assert.ok(safeRequestId(id));
  for (const bad of ['', 'x'.repeat(81), 'ask id', '<script>', '-lead', null]) assert.equal(safeRequestId(bad), false, String(bad));
  assert.ok(safeRequestId('a'.repeat(80)));
});

test('the request body has exactly the contracted fields; optional ones only when present', () => {
  const state = stateAt();
  const minimal = buildAskRequest({ requestId: 'ask-1', question: '  What is P-101?  ', selectedIds: [], state });
  assert.deepEqual(Object.keys(minimal).sort(), ['expected_revision', 'input_digest', 'question', 'request_id', 'selected_ids']);
  assert.deepEqual(minimal, { request_id: 'ask-1', question: 'What is P-101?', selected_ids: [], expected_revision: 4, input_digest: DIGEST });
  const full = buildAskRequest({
    requestId: 'ask-2', question: 'What is upstream of it?', selectedIds: [pumpId, 'ent-gone', pumpId, portId, connId], state,
    history: [{ question: 'q', answer: 'a', extra: 'dropped' }], displayed: { source_revision: 2, input_digest: 'b'.repeat(64), mode: 'published', url: '/x' },
  });
  assert.deepEqual(Object.keys(full).sort(), ['displayed_source', 'expected_revision', 'history', 'input_digest', 'question', 'request_id', 'selected_ids']);
  assert.deepEqual(full.selected_ids, [pumpId, portId, connId], 'unknown IDs dropped, duplicates removed');
  assert.deepEqual(full.history, [{ question: 'q', answer: 'a' }]);
  assert.deepEqual(full.displayed_source, { source_revision: 2, input_digest: 'b'.repeat(64), mode: 'published' });
  assert.throws(() => buildAskRequest({ requestId: 'bad id', question: 'x', state }), /safe identifier/);
  assert.throws(() => buildAskRequest({ requestId: 'ask-3', question: 'x', state: { project } }), /digest/);
});

test('preflight refuses what the service would refuse, before any request', () => {
  const state = stateAt();
  assert.equal(preflightAsk({ question: 'What is P-101?', state }).ok, true);
  assert.equal(preflightAsk({ question: '   ', state }).problem, 'empty');
  assert.equal(preflightAsk({ question: 'x'.repeat(ASK_LIMITS.question), state }).ok, true);
  const long = preflightAsk({ question: 'x'.repeat(ASK_LIMITS.question + 1), state });
  assert.equal(long.problem, 'too_long');
  assert.match(long.message, /4,000-character limit by 1/);
  assert.equal(preflightAsk({ question: '🙂'.repeat(4000), state }).ok, true, 'code points, not UTF-16 units');
  assert.equal(preflightAsk({ question: '🙂'.repeat(4001), state }).problem, 'too_long');
  assert.equal(preflightAsk({ question: 'a\u0000b', state }).problem, 'control');
  assert.equal(preflightAsk({ question: 'line one\nline two\ttab', state }).ok, true);
  assert.equal(preflightAsk({ question: 'x', selectedIds: Array(17).fill('id'), state }).problem, 'too_many_ids');
  assert.equal(preflightAsk({ question: 'x', selectedIds: Array(16).fill('id'), state }).ok, true);
  assert.equal(preflightAsk({ question: 'x', online: false, state }).problem, 'offline');
  assert.equal(preflightAsk({ question: 'x', busy: true, state }).problem, 'busy');
  assert.equal(preflightAsk({ question: 'x', state: null }).problem, 'no_state');
  assert.equal(codePoints('🙂a'), 2);
});

test('history: last six answered or clarified turns, each field at most 2000 code points', () => {
  const turns = [];
  for (let i = 0; i < 9; i += 1) turns.push({ question: `q${i}`, answer: { status: 'answered', answer: `a${i}` } });
  turns.push({ question: 'cancelled', answer: { status: 'cancelled', answer: 'x' } });
  turns.push({ question: 'failed', answer: null, failure: {} });
  turns.push({ question: 'long', answer: { status: 'clarification', answer: '🙂'.repeat(2500) } });
  const h = historyFrom(turns);
  assert.equal(h.length, ASK_LIMITS.history);
  assert.deepEqual(h.slice(0, 5).map((r) => r.question), ['q4', 'q5', 'q6', 'q7', 'q8']);
  assert.equal(h[5].question, 'long');
  assert.equal(codePoints(h[5].answer), ASK_LIMITS.historyField);
  assert.ok(h[5].answer.endsWith('…'));
  for (const r of h) assert.deepEqual(Object.keys(r), ['question', 'answer']);
});

test('replies keep only contracted fields; executable or unknown content never reaches the UI', () => {
  const request = buildAskRequest({ requestId: 'ask-9', question: 'What is P-101?', state: stateAt() });
  const malicious = reply(request, {
    answer: '<img src=x onerror=alert(1)> see https://example.invalid',
    operations: [{ type: 'move_equipment' }], commands: [{ type: 'undo' }], apply: true, html: '<b>x</b>',
    references: [
      { id: pumpId, tag: '<script>alert(1)</script>', kind: 'centrifugal_pump', field_paths: ['a', 7, null], facts: ['ok', { x: 1 }], url: 'javascript:alert(1)', onclick: 'x' },
      { tag: 'no id' }, 'string-ref', null,
    ],
    responder: { kind: 'model', language_model: 'claude-opus-5-5', api_key: 'secret', usage: { inputTokens: 5, auth_token: 'x' }, effort: { requested: 'xhigh' } },
  });
  const { ok, answer } = readAnswer(malicious, request);
  assert.ok(ok);
  for (const key of ['operations', 'commands', 'apply', 'html']) assert.ok(!(key in answer), key);
  assert.equal(answer.answer, '<img src=x onerror=alert(1)> see https://example.invalid', 'kept as text; rendering assigns textContent');
  assert.equal(answer.references.length, 1);
  assert.deepEqual(Object.keys(answer.references[0]).sort(), ['facts', 'field_paths', 'id', 'kind', 'tag']);
  assert.deepEqual(answer.references[0].field_paths, ['a']);
  assert.deepEqual(answer.references[0].facts, ['ok']);
  assert.equal(answer.responder.language_model, true);
  assert.equal(answer.responder.model, 'claude-opus-5-5');
  const factKeys = answer.responder.facts.map(([k]) => k);
  assert.ok(!factKeys.some((k) => /key|auth|token/i.test(k)), factKeys.join());
  assert.ok(factKeys.includes('effort requested'));
});

test('the service responder shape: local, model and fallback answers say whether anything left the computer', () => {
  const request = buildAskRequest({ requestId: 'ask-13', question: 'q', state: stateAt() });
  const local = readAnswer(reply(request, { responder: { kind: 'deterministic', language_model: false, query: 'topology', model_called: false } }), request).answer.responder;
  assert.deepEqual([local.language_model, local.model_called, local.query, local.model], [false, false, 'topology', null]);
  const model = readAnswer(reply(request, { responder: {
    kind: 'language_model', language_model: true, query: 'change_request', model_called: true, prompt_bytes: 5120, prompt_sha256: 'f'.repeat(64),
    bridge: { model: 'claude-opus-5-5', effort: { requested: 'xhigh' }, api_key_source: 'none', auth: 'claude.ai', tools: [], response_models: ['claude-opus-5-5'] },
  } }), request).answer.responder;
  assert.deepEqual([model.language_model, model.model_called, model.query, model.model], [true, true, 'change_request', 'claude-opus-5-5']);
  const keys = model.facts.map(([k]) => k);
  assert.ok(keys.includes('bridge effort requested') && keys.includes('bridge tools') && keys.includes('prompt_bytes'), keys.join());
  assert.ok(!keys.some((k) => /key|auth/i.test(k)), keys.join());
  const fallback = readAnswer(reply(request, { status: 'unavailable', responder: { kind: 'deterministic', language_model: false, model_called: true, error: { code: 'ask_timeout', message: 'no answer in time' } } }), request).answer.responder;
  assert.deepEqual([fallback.language_model, fallback.model_called, fallback.error], [false, true, { code: 'ask_timeout', message: 'no answer in time' }]);
});

test('replies that do not match the contract are refused with reasons', () => {
  const request = buildAskRequest({ requestId: 'ask-10', question: 'q', state: stateAt() });
  assert.equal(readAnswer(null, request).ok, false);
  assert.equal(readAnswer([], request).ok, false);
  assert.deepEqual(readAnswer(reply(request, { request_id: 'ask-other' }), request).problems, ['the reply belongs to another question']);
  assert.match(readAnswer(reply(request, { status: 'applied' }), request).problems.join(), /unknown status/);
  assert.match(readAnswer(reply(request, { source_revision: '4' }), request).problems.join(), /source revision/);
  assert.match(readAnswer(reply(request, { answer: { html: 'x' } }), request).problems.join(), /answer text/);
  for (const status of ['answered', 'clarification', 'unavailable', 'cancelled']) assert.ok(readAnswer(reply(request, { status }), request).ok, status);
  const huge = readAnswer(reply(request, { answer: 'x'.repeat(50000), references: Array(200).fill({ id: pumpId, tag: 'P', facts: Array(100).fill('f') }) }), request).answer;
  assert.equal(codePoints(huge.answer), 20000);
  assert.equal(huge.references.length, 64);
  assert.equal(huge.references[0].facts.length, 32);
});

test('an answer keeps its frozen source; later model revisions make it stale with an explicit label', () => {
  const request = buildAskRequest({ requestId: 'ask-11', question: 'q', state: stateAt(4) });
  const { answer } = readAnswer(reply(request), request);
  assert.deepEqual(answerFreshness(answer, stateAt(4)), { stale: false, tone: 'current', text: 'Answer based on the current model r4' });
  const later = answerFreshness(answer, stateAt(6, 'c'.repeat(64)));
  assert.equal(later.stale, true);
  assert.equal(later.text, 'Answer based on r4; current r6');
  assert.equal(answerFreshness(answer, stateAt(4, 'd'.repeat(64))).stale, true, 'same revision, different digest');
  const { answer: flagged } = readAnswer(reply(request, { stale: true, current_revision: 5 }), request);
  assert.equal(answerFreshness(flagged, stateAt(4)).stale, true, 'the service reported a change during the answer');
});

test('freshness uses the service\'s known current revision before the next poll, and later states beyond it', () => {
  // the answer arrives (source r0, service current r1) while the last state on screen is still r0 / same digest
  const request = buildAskRequest({ requestId: 'ask-13', question: 'q', state: stateAt(0) });
  const { answer } = readAnswer(reply(request, { source_revision: 0, current_revision: 1, stale: true }), request);
  assert.deepEqual(answerFreshness(answer, stateAt(0)), { stale: true, tone: 'stale', text: 'Answer based on r0; current r1' }, 'not "an earlier digest" of r0');
  assert.equal(answerFreshness(answer, stateAt(1, 'b'.repeat(64))).text, 'Answer based on r0; current r1', 'the poll catches up');
  assert.equal(answerFreshness(answer, stateAt(3, 'c'.repeat(64))).text, 'Answer based on r0; current r3', 'a later state beyond the reply wins');
  // same revision, another digest (e.g. an external edit): no newer revision is invented
  const r4 = buildAskRequest({ requestId: 'ask-14', question: 'q', state: stateAt(4) });
  const { answer: same } = readAnswer(reply(r4), r4);
  assert.equal(answerFreshness(same, stateAt(4, 'd'.repeat(64))).text, 'Answer based on r4 (an earlier digest); the current model differs');
  const { answer: sameFlagged } = readAnswer(reply(r4, { stale: true, current_revision: 4 }), r4);
  assert.equal(answerFreshness(sameFlagged, stateAt(4)).text, 'Answer based on r4 (an earlier digest); the current model differs', 'the service said stale at the same revision');
  // the historical answer itself is untouched by any of this
  assert.equal(answer.source_revision, 0);
  assert.equal(answer.current_revision, 1);
});

test('an answer whose source could not be verified at completion is never labelled current', () => {
  const request = buildAskRequest({ requestId: 'ask-15', question: 'q', state: stateAt(2) });
  const { answer } = readAnswer(reply(request, { current_revision: null, stale: true, limitations: ['The project file could not be read after answering.'] }), request);
  assert.equal(answer.current_revision, null);
  const f = answerFreshness(answer, stateAt(2));
  assert.equal(f.stale, true);
  assert.equal(f.text, 'Answer based on r2; whether that was still the current model could not be verified when the answer completed');
  assert.doesNotMatch(f.text, /current model r2|earlier digest/);
  assert.equal(answerFreshness(answer, stateAt(5, 'e'.repeat(64))).text, 'Answer based on r2; current r5', 'a later state settles it');
});

test('a stale_ask refusal names no revision from the cached state; it quotes the service', () => {
  const error = { status: 409, error: 'the project changed since this question was prepared (now revision 1)', diagnostics: [{ code: 'stale_ask' }] };
  const f = askFailure(error, 0); // the state on screen is still r0
  assert.equal(f.title, 'Not asked: the project changed');
  assert.equal(f.text, 'The project changed before the question was accepted; nothing was sent to a model. The service said: “the project changed since this question was prepared (now revision 1)”');
  assert.doesNotMatch(f.text, /\br0\b|to use r/);
  assert.equal(f.retry, true, 'the retry control names the latest received state when shown');
  assert.equal(askFailure({ status: 409, diagnostics: [{ code: 'stale_ask' }] }, 0).text, 'The project changed before the question was accepted; nothing was sent to a model.');
});

test('older visible content is named separately from the model source of the answer', () => {
  const request = buildAskRequest({ requestId: 'ask-12', question: 'q', state: stateAt(9) });
  const same = readAnswer(reply(request, { displayed_source: { source_revision: 9, input_digest: DIGEST, mode: 'live' } }), request).answer;
  assert.equal(visibleSourceNote(same), null);
  const older = readAnswer(reply(request, { displayed_source: { source_revision: 6, input_digest: 'e'.repeat(64), mode: 'published' } }), request).answer;
  assert.match(visibleSourceNote(older), /^Answer: model r9; visible publication r6\./);
  assert.equal(displayedSource({ source_revision: 6, input_digest: 'e'.repeat(64), build_id: 'b', url: '/u' }, 'published').mode, 'published');
  assert.deepEqual(Object.keys(displayedSource({ source_revision: 6, input_digest: 'e'.repeat(64) }, 'live')).sort(), ['input_digest', 'mode', 'source_revision']);
  assert.equal(displayedSource(null, 'live'), null);
  assert.equal(displayedSource({ source_revision: 6 }, 'live'), null);
});

test('references select current counterparts by stable ID; historical tags stay as answered', () => {
  const pumpTag = project.model.entities[pumpId].tag;
  assert.deepEqual(referenceNow({ id: pumpId, tag: pumpTag }, project), { exists: true, label: pumpTag, renamed: false });
  const renamed = structuredClone(project);
  renamed.model.entities[pumpId].tag = 'P-901';
  assert.deepEqual(referenceNow({ id: pumpId, tag: pumpTag }, renamed), { exists: true, label: 'P-901', renamed: true });
  assert.deepEqual(referenceNow({ id: 'ent-removed', tag: 'X-1' }, project), { exists: false, label: null, renamed: false });
  const port = project.model.ports[portId];
  assert.equal(currentLabel(project, portId), `${project.model.entities[port.entity_id].tag}.${port.name}`);
  assert.equal(currentLabel(project, connId), project.model.connections[connId].tag);
  assert.deepEqual(knownIds(project, [pumpId, 'nope', connId, pumpId]), [pumpId, connId]);
});

test('the conversation belongs to one project and service run', () => {
  const a = identityKey(stateAt(4, DIGEST, 't1'));
  assert.equal(identityKey(stateAt(9, 'f'.repeat(64), 't1')), a, 'new revisions keep the conversation');
  assert.notEqual(identityKey(stateAt(4, DIGEST, 't2')), a, 'a restarted service starts a new one');
  const other = stateAt(4, DIGEST, 't1');
  other.project = { ...other.project, id: 'project-other' };
  assert.notEqual(identityKey(other), a);
  assert.equal(identityKey(null), null);
});

test('edit-like questions are offered to Change; questions are not', () => {
  for (const t of ['Rename P-101 to P-201', 'move P-101 0.25 m in +x', 'Please set the motor length of P-101 to 0.42 m', 'Insert a valve into L-101']) assert.ok(looksLikeEdit(t), t);
  for (const t of ['What is HV-101?', 'What is upstream and downstream of C-101?', 'Should I move P-101?', 'Set point of PI-101?', '', 'Why was L-102 rerouted']) assert.ok(!looksLikeEdit(t), t);
});

test('the thread is bounded; failures explain what happened and never claim a change', () => {
  const turns = Array.from({ length: MAX_TURNS + 3 }, (_, i) => ({ i }));
  const b = boundTurns(turns);
  assert.equal(b.turns.length, MAX_TURNS);
  assert.equal(b.dropped, 3);
  assert.equal(b.turns[0].i, 3);
  assert.equal(boundTurns(turns.slice(0, 2)).dropped, 0);
  const stale = askFailure({ status: 409, error: 'stale', diagnostics: [{ code: 'stale_ask' }] }, 7);
  assert.equal(stale.tone, 'stale');
  assert.match(stale.text, /nothing was sent to a model/);
  assert.equal(askFailure({ status: 409, diagnostics: [{ code: 'ask_busy' }] }, 7).title, 'Service busy');
  assert.equal(askFailure({ status: 400, error: 'question too long', diagnostics: [{ code: 'invalid_ask' }] }, 7).retry, false);
  assert.match(askFailure({ status: 404, error: 'not found' }, 7).text, /does not offer Ask/);
  assert.match(askFailure({ status: 0, network: true, error: 'the local service is not reachable' }, 7).text, /question is kept; nothing was changed/);
  assert.equal(askFailure({ aborted: true }, 7).tone, 'cancelled');
});
