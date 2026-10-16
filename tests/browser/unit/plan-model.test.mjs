// Review decisions for quick and engineering proposals (web/js/plan-model.js), against the real
// fixture (read only). Proposal records here are hand-written in the shapes the service returns;
// they are review-logic fixtures, not planner output, and say nothing about any real planner run.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildBatch, buildInsertEquipment, buildRename, buildSetParameter } from '../../../web/js/commands-model.js';
import {
  FOLLOW_UP_NOTE, REQUEST_LIMITS, affectedItems, cleanRequest, describeOperations, effortText, emptySlots, followUp,
  followUpText, itemRefs, itemText, plannerFacts, plannerHeadline, preflight, proposalOperations, questionIds,
  reviewProposal,
} from '../../../web/js/plan-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const fixture = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;
const stateOf = (project) => ({ project });

function quick(project, overrides = {}) {
  return {
    id: 'prop-q', text: 'Set motor length of P-101 to 0.42 m', expected_revision: project.revision, parser: 'deterministic-v1',
    supported: true, intent: 'set_parameter', summary: 'Set P-101 motor_length to 0.42 m.',
    commands: [buildSetParameter(project, 'ent-pump', 'motor_length', 0.42)], assumptions: ['x'], unknowns: [],
    validation: { accepted: true, diagnostics: [], warnings: [], no_op: false }, applied: false, ...overrides,
  };
}

function plan(project, overrides = {}) {
  const set = buildSetParameter(project, 'ent-pump', 'motor_length', 0.42);
  const batch = buildBatch(project, [
    { type: 'rename_entity', payload: { entity_id: 'ent-pump', tag: 'P-201' } },
    { type: 'set_parameter', payload: set.payload, assumptions: set.assumptions },
  ]);
  return {
    id: 'prop-e', mode: 'engineering', status: 'ready', text: 'Rename P-101 to P-201 and set its motor length to 0.42 m',
    expected_revision: project.revision, supported: true, summary: 'Rename and lengthen the motor.',
    commands: [batch], command_count: 1, operation_count: 2,
    operations: [{ index: 0, type: 'rename_entity', purpose: 'new tag requested', ids: ['ent-pump'] },
      { index: 1, type: 'set_parameter', purpose: 'longer motor requested', ids: ['ent-pump'] }],
    assumptions: [], proposed_assumptions: [{ key: 'motor', id: 'asm-p', description: 'User-requested length.', recorded: true }],
    unknowns: ['Motor rating is unknown.'], questions: [], reasoning: ['Two edits requested together.'],
    impacts: [{ ids: ['ent-pump'], effect: 'model.entities changed (parameters, tag)', source: 'validator' }],
    validation: { accepted: true, diagnostics: [], warnings: [], no_op: false, geometry_checked: false },
    planner: { model: 'claude-opus-5-5', effort: 'xhigh', api_key_source: 'none', session_token: 'hidden' },
    applied: false, stale: false, expires_at: '2999-01-01T00:00:00.000+00:00', ...overrides,
  };
}

test('quick proposals keep their original apply rule', () => {
  const p = fixture();
  const ok = reviewProposal(quick(p), stateOf(p));
  assert.deepEqual([ok.verdict, ok.canApply, ok.engineering], ['ready', true, false]);
  const stale = reviewProposal(quick(p), stateOf({ ...p, revision: p.revision + 1 }));
  assert.equal(stale.canApply, false);
  assert.ok(stale.reasons.some((r) => /now at revision/.test(r)));
  const unsupported = reviewProposal(quick(p, { supported: false, commands: [], reason: 'compound request' }), stateOf(p));
  assert.deepEqual([unsupported.verdict, unsupported.canApply], ['unsupported', false]);
  assert.equal(reviewProposal(quick(p, { validation: { accepted: true, no_op: true } }), stateOf(p)).canApply, false);
});

test('a ready engineering plan is one batch envelope with distinct command and operation counts', () => {
  const p = fixture();
  const r = reviewProposal(plan(p), stateOf(p), 'engineering');
  assert.equal(r.verdict, 'ready');
  assert.equal(r.canApply, true, r.reasons.join('; '));
  assert.equal(r.commandCount, 1);
  assert.equal(r.operationCount, 2);
  assert.equal(r.geometryChecked, false, 'a dry run is never presented as a geometry check');
  assert.deepEqual(r.operations.map((o) => o.type), ['rename_entity', 'set_parameter']);
  assert.equal(r.operations[0].purpose, 'new tag requested');
  assert.match(r.operations[1].lines.join(' '), /motor_length from .* to 0\.42 m/);
  const pump = r.affected.existing.find((a) => a.id === 'ent-pump');
  assert.deepEqual([pump.tag, pump.how.sort()], ['P-101', ['parameter motor_length', 'renamed']]);
  assert.equal(r.addedAssumptions.length, 1);
  assert.equal(r.addedAssumptions[0].index, 1);
});

test('only an explicit geometry_checked true is reported as checked', () => {
  const p = fixture();
  for (const value of [undefined, false, 'true', 1, null]) {
    const v = { accepted: true, diagnostics: [], warnings: [], no_op: false };
    if (value !== undefined) v.geometry_checked = value;
    assert.equal(reviewProposal(plan(p, { validation: v }), stateOf(p), 'engineering').geometryChecked, false, String(value));
  }
  assert.equal(reviewProposal(plan(p, { validation: { accepted: true, geometry_checked: true } }), stateOf(p), 'engineering').geometryChecked, true);
});

test('questions, refusals and non-batch replies never offer apply', () => {
  const p = fixture();
  const asks = reviewProposal(plan(p, { status: 'questions', supported: false, commands: [],
    questions: [{ id: 'q1', question: 'Which line should the drum go in?', reason: 'Two candidates.' }] }), stateOf(p), 'engineering');
  assert.deepEqual([asks.verdict, asks.canApply, asks.questions.length], ['questions', false, 1]);
  // even a reply that carries commands alongside questions is not applicable
  const both = reviewProposal(plan(p, { questions: ['Which tag?'] }), stateOf(p), 'engineering');
  assert.deepEqual([both.verdict, both.canApply], ['questions', false]);
  for (const status of ['unavailable', 'rejected', 'invalid', 'unsupported']) {
    const r = reviewProposal(plan(p, { status, supported: false, commands: [], reason: `planner ${status}` }), stateOf(p), 'engineering');
    assert.deepEqual([r.verdict, r.canApply], [status, false]);
    assert.equal(r.reasons[0], `planner ${status}`);
  }
  const two = plan(p);
  two.commands = [two.commands[0], buildRename(p, 'ent-tank', 'TK-9')];
  assert.equal(reviewProposal(two, stateOf(p), 'engineering').canApply, false);
  const single = plan(p, { commands: [buildRename(p, 'ent-pump', 'P-201')] });
  const rs = reviewProposal(single, stateOf(p), 'engineering');
  assert.equal(rs.canApply, false);
  assert.ok(rs.reasons.some((x) => /one batch envelope/.test(x)));
  const notReady = reviewProposal(plan(p, { status: 'pending' }), stateOf(p), 'engineering');
  assert.equal(notReady.canApply, false);
});

test('a reply that did not run engineering mode is never shown as an engineering plan', () => {
  const p = fixture();
  const r = reviewProposal(quick(p), stateOf(p), 'engineering');
  assert.deepEqual([r.verdict, r.canApply], ['not-run', false]);
});

test('stale, expired, applied and no-op plans cannot be applied', () => {
  const p = fixture();
  const later = { ...p, revision: p.revision + 1 };
  assert.equal(reviewProposal(plan(p), stateOf(later), 'engineering').canApply, false);
  assert.equal(reviewProposal(plan(p, { stale: true }), stateOf(p), 'engineering').canApply, false);
  const expired = reviewProposal(plan(p, { expires_at: '2026-10-14T10:00:00.000+00:00' }), stateOf(p), 'engineering', Date.parse('2026-10-14T11:00:00Z'));
  assert.deepEqual([expired.expired, expired.canApply], [true, false]);
  assert.equal(reviewProposal(plan(p, { applied: true }), stateOf(p), 'engineering').canApply, false);
  assert.equal(reviewProposal(plan(p, { validation: { accepted: true, no_op: true } }), stateOf(p), 'engineering').canApply, false);
});

test('insertion operations list the split line, the reconnected item and the new items', () => {
  const p = fixture();
  const definition = { schema: 'skid.equipment.definition/1', id: 'demo', name: 'Demo drum', symbol: { shape: 'horizontal_vessel' },
    ports: { inlet: { direction: 'in' }, outlet: { direction: 'out' } }, geometry: { parameters: { shell_length: { value: 1.4, unit: 'm' } } } };
  const insert = buildInsertEquipment(p, 'conn-discharge-2', { definition });
  const ops = proposalOperations({ commands: [buildBatch(p, [{ type: insert.type, payload: insert.payload, assumptions: insert.assumptions }])] });
  const a = affectedItems(ops, p);
  assert.deepEqual(a.existing.map((x) => x.id).sort(), ['conn-discharge-2', 'ent-discharge-valve']);
  assert.deepEqual(a.created.map((c) => c.kind), ['entity', 'connection']);
  assert.match(a.created[0].what, /Demo drum \(composed\)/);
  // after apply the new items exist in the project; they are still reported once, as created
  const applied = structuredClone(p);
  applied.model.entities[insert.payload.entity.id] = insert.payload.entity;
  applied.model.connections[insert.payload.downstream_connection.id] = insert.payload.downstream_connection;
  const again = affectedItems(ops, applied);
  assert.deepEqual(again.existing.map((x) => x.id).sort(), ['conn-discharge-2', 'ent-discharge-valve']);
  assert.equal(again.created.length, 2);
  const described = describeOperations(ops, p, [{ index: 0, purpose: 'hold-up volume requested' }]);
  assert.equal(described[0].purpose, 'hold-up volume requested');
  assert.match(described[0].lines[0], /Insert Demo drum \(composed\) .* into L-104: NRV-101 → .* → HV-102\./);
});

test('list items and planner facts render as text without secrets', () => {
  assert.equal(itemText('plain'), 'plain');
  assert.equal(itemText({ question: 'Which line?', reason: 'two' }), 'Which line?');
  assert.equal(itemText({ ids: ['a'], effect: 'model.entities changed' }), 'model.entities changed');
  assert.deepEqual(itemRefs({ ids: ['ent-pump', 'ent-pump'], target_id: 'ent-tank' }), ['ent-pump', 'ent-tank']);
  const facts = Object.fromEntries(plannerFacts({ model: 'claude-opus-5-5', effort: 'xhigh', api_key_source: 'none', session_token: 's', nested: { a: 1, deep: { b: 2 } } }));
  assert.deepEqual(facts, { model: 'claude-opus-5-5', effort: 'xhigh', 'api key source': 'none', 'nested a': '1' });
  const text = followUpText({ text: 'Insert a drum', questions: [{ id: 'line', question: 'Which line?' }, 'Which tag?'] });
  assert.equal(text, `Insert a drum\n\n${FOLLOW_UP_NOTE}\nline: \nq2: `);
});

test('planner effort and status read honestly from the bridge metadata shape', () => {
  // shape of skid/planning/bridge.py run() metadata merged over describe(): effort becomes {requested, reported}
  const runtime = {
    kind: 'claude-cli', model: 'claude-opus-5-5', effort: { requested: 'xhigh', reported: null }, cli: '/opt/claude',
    cli_present: true, timeout_s: 600, tools: [], session_persistence: false, api_key_env_removed: true, verified: true,
    cli_version: '2.1.299', init_model: 'claude-opus-5-5', api_key_source: 'none', mcp_servers: [],
    response_models: ['claude-opus-5-5'], elapsed_s: 42.1, usage: { inputTokens: 1200, outputTokens: 300 }, prompt_sha256: 'ab',
  };
  assert.equal(plannerHeadline(runtime), 'claude-opus-5-5 · effort xhigh requested');
  assert.equal(plannerHeadline({ ...runtime, effort: { requested: 'xhigh', reported: 'xhigh' } }),
    'claude-opus-5-5 · effort xhigh requested, xhigh reported');
  assert.equal(plannerHeadline({ model: 'claude-opus-5-5', effort: 'xhigh' }), 'claude-opus-5-5 · effort xhigh requested', 'static describe() form');
  assert.equal(plannerHeadline({ model: 'none (fake)' }), 'none (fake)');
  assert.deepEqual([effortText(null), effortText({}), effortText({ requested: '', reported: null })], ['', '', '']);
  const facts = plannerFacts(runtime);
  const byKey = Object.fromEntries(facts);
  assert.ok(facts.every(([, v]) => !v.includes('[object Object]')), JSON.stringify(facts));
  assert.equal(byKey['effort requested'], 'xhigh');
  assert.ok(!('effort reported' in byKey), 'reported only when the CLI reports it');
  for (const [k, v] of [['model', 'claude-opus-5-5'], ['init model', 'claude-opus-5-5'], ['response models', 'claude-opus-5-5'],
    ['api key source', 'none'], ['verified', 'true'], ['tools', 'none'], ['mcp servers', 'none'], ['session persistence', 'false'],
    ['usage inputTokens', '1200']]) assert.equal(byKey[k], v, k);
  // the composer formats planner metadata only through these helpers (it once joined the raw effort object)
  const composer = fs.readFileSync(path.join(ROOT, 'web', 'js', 'composer.js'), 'utf8');
  assert.ok(!/planner\.effort/.test(composer), 'composer.js must not render planner.effort directly');
});

test('engineering mode says what leaves the computer; quick edit stays local', async () => {
  const { MODE_INFO } = await import('../../../web/js/composer.js'); // DOM is used only inside createComposer
  const engineering = MODE_INFO.engineering.intro;
  for (const phrase of ['current project data', 'Claude Code CLI', 'Claude Max', 'external service', 'does not run on this computer']) {
    assert.ok(engineering.includes(phrase), phrase);
  }
  assert.ok(!/local (engineering )?planner/i.test(engineering));
  assert.ok(MODE_INFO.quick.intro.includes('deterministic parser on this computer'));
});

// The actual eight-question reply of the real planner (copied from GPT's run, read only).
const realQuestions = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'browser', 'unit', 'fixtures', 'real-questions-8.json'), 'utf8'));

test('a follow-up to the actual eight questions keeps the request, uses ID slots and fits the engineering budget', () => {
  const p = realQuestions();
  assert.equal(p.questions.length, 8);
  // the former format repeated every question before any answer: 2379 characters, over the 2000 limit
  const former = `${p.text}\n\nAnswers:\n${p.questions.map((q, i) => `${i + 1}. ${q.question}\n   → `).join('\n')}`;
  assert.equal(cleanRequest(former, 'engineering').problem, 'too_long');
  const next = followUp(p);
  assert.ok(next.text.startsWith(`${p.text}\n\n`), 'original request kept verbatim at the start');
  assert.deepEqual(next.ids, p.questions.map((q) => q.id));
  assert.deepEqual(next.text.split('\n').slice(-8), p.questions.map((q) => `${q.id}: `));
  assert.ok(next.text.includes('self-contained') && next.text.includes('does not remember'), 'answers must stand alone');
  for (const q of p.questions) assert.ok(!next.text.includes(q.question), `question prose not repeated: ${q.id}`);
  assert.equal(next.fits, true);
  assert.ok(next.budget.length < 700 && next.budget.remaining > 1300, `room left for answers: ${next.budget.length}`);
  // the empty scaffold is never sent; answering every slot makes it sendable
  const blank = preflight(next.text, 'engineering', next.ids);
  assert.equal(blank.ok, false);
  assert.deepEqual(blank.emptySlots, next.ids);
  assert.match(blank.message, /8 answer slots are empty/);
  const answered = next.ids.reduce((t, id) => t.replace(`${id}: `, `${id}: Not known yet; keep it explicitly unknown.`), next.text);
  const sent = preflight(answered, 'engineering', next.ids);
  assert.equal(sent.ok, true, sent.message);
  assert.ok(sent.budget.length <= REQUEST_LIMITS.engineering);
  // too-long answers are refused before any request, never cut
  const verbose = `${answered}\n${'more detail '.repeat(150)}`;
  const over = preflight(verbose, 'engineering', next.ids);
  assert.deepEqual([over.ok, over.budget.problem], [false, 'too_long']);
  assert.match(over.message, /Over the 2000-character engineering limit by \d+\. The original request is kept unchanged/);
});

test('a long original request is never truncated; editing is required instead', () => {
  const p = realQuestions();
  const long = { ...p, text: `${'Size the skid for the duty stated here. '.repeat(48)}End.`.trim() };
  assert.ok(cleanRequest(long.text, 'engineering').length <= REQUEST_LIMITS.engineering, 'the original alone was valid');
  const next = followUp(long);
  assert.ok(next.text.startsWith(long.text), 'original kept intact');
  assert.equal(next.fits, false);
  const check = preflight(next.text, 'engineering', next.ids);
  assert.equal(check.ok, false);
  assert.equal(check.budget.problem, 'too_long');
  assert.match(check.message, /original request is kept unchanged: shorten it, the answers, or remove answer slots; nothing is sent/);
});

test('request budget counts code points after the service whitespace cleaning', () => {
  const smile = '🙂'; // one code point, two UTF-16 units
  assert.equal(cleanRequest(smile.repeat(2000), 'engineering').problem, null);
  assert.equal(cleanRequest(smile.repeat(2001), 'engineering').problem, 'too_long');
  assert.equal(cleanRequest(smile.repeat(500), 'quick').length, 500);
  assert.equal(cleanRequest(`${smile.repeat(500)}x`, 'quick').problem, 'too_long');
  assert.equal(cleanRequest('é', 'quick').length, 2, 'combining marks count as their own code points');
  // engineering keeps line breaks and collapses whitespace within lines (incl. no-break and ideographic spaces)
  const e = cleanRequest('  Rename  P-101\t to P-201 \r\n\n  and set　motor length  \n\n', 'engineering');
  assert.equal(e.text, 'Rename P-101 to P-201\n\nand set motor length');
  assert.equal(e.length, [...e.text].length);
  // quick collapses everything, line breaks included
  assert.equal(cleanRequest(' Rename\nP-101 to   P-201 ', 'quick').text, 'Rename P-101 to P-201');
  // whitespace-only requests are empty; refused characters are reported, not stripped
  assert.equal(cleanRequest(' \n \t', 'engineering').problem, 'empty');
  assert.equal(cleanRequest('', 'quick').problem, 'empty');
  assert.equal(cleanRequest('a\u0007b', 'quick').problem, 'control');
  assert.equal(cleanRequest('line separator', 'engineering').problem, 'control');
  assert.equal(preflight('  ', 'quick').ok, false);
  assert.equal(preflight('x'.repeat(501), 'quick').message, 'Over the 500-character quick-edit limit by 1. Shorten the request; nothing is sent until it fits.');
});

test('question IDs are descriptive slots, unique, with fallbacks; empty-slot detection is per line', () => {
  assert.deepEqual(questionIds([{ id: 'line' }, { id: 'line' }, 'Which tag?', { id: 'bad id!' }]), ['line', 'line-2', 'q3', 'q4']);
  assert.deepEqual(emptySlots('line: L-104\ntag:\n  q3 :  \nnote: tag: later', ['line', 'tag', 'q3']), ['tag', 'q3']);
  assert.deepEqual(emptySlots('Fluid: water', ['fluid-properties']), []);
});

test('plan-model stays free of DOM so it runs in Node', () => {
  const source = fs.readFileSync(path.join(ROOT, 'web', 'js', 'plan-model.js'), 'utf8').replace(/\/\/.*$/gm, '');
  assert.ok(!/\b(document|window)\./.test(source));
  assert.ok(!/from\s+'three/.test(source));
});
