// Research review and creation helpers (web/js/research-model.js, web/js/creation-model.js).
// Proposal records here are hand-written in the shapes the service returns; they are review-logic
// fixtures (FAKE research briefs with invented example sources), not research or planner output.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  QUERY_SHOWN, SEARCH_RECORDS_SHOWN, assumptionCitations, researchFacts, researchOffered, researchPersistence,
  researchSearches, reviewResearch, safeHref, sourceHost, stageInfo, urlProblem,
} from '../../../web/js/research-model.js';
import { creationExamples, isEmptyProject, openPorts } from '../../../web/js/creation-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const fixture = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;
const empty = () => ({ id: 'blank', revision: 0, model: { entities: {}, ports: {}, connections: {}, assumptions: {} },
  pid: { symbols: {}, edges: {} }, physical: { placements: {}, routes: {} } });

const BRIEF = {
  schema: 'skid.equipment.research/1', query: 'FAKE shell-and-tube exchanger HX-101', equipment_type: 'Shell-and-tube heat exchanger',
  summary: 'FAKE summary: two separate circuits.\nTube side and shell side.',
  sources: [
    { id: 'S1', url: 'https://www.fake-vendor-source.com/products/shell-and-tube', title: 'FAKE vendor page', observations: ['FAKE: fixed tube sheet'] },
    { id: 'S2', url: 'javascript:alert(1)', title: '<img src=x onerror=alert(1)>', observations: ['FAKE hostile entry'] },
    { id: 'S3', url: 'http://127.0.0.1/admin', title: 'FAKE loopback', observations: ['x'] },
  ],
  features: [{ text: 'Four nozzles: tube inlet/outlet and shell inlet/outlet', source_ids: ['S1'] },
    { text: 'FAKE feature citing an unknown source', source_ids: ['S9'] }],
  assumptions: ['Dimensions are illustrative'], unknowns: ['Thermal duty'], warnings: ['Research used text web-search results only.'],
};

test('only public absolute http(s) URLs become links', () => {
  for (const ok of ['https://www.alfalaval.com/products/heat-transfer/', 'http://example.org/a?b=c#d', 'https://8.8.8.8/x',
    'https://xn--bcher-kva.ch/path%20x']) {
    assert.equal(urlProblem(ok), null, ok);
    assert.equal(safeHref(ok), ok);
  }
  const refused = [
    'javascript:alert(1)', 'data:text/html,<b>x</b>', 'vbscript:x', 'file:///etc/passwd', 'ftp://example.org/x', '//example.org/x',
    'HTTPS://example.org/', 'https://user:pass@example.org/', 'https://user@example.org/', 'https://example.org:8443/',
    'http://localhost/x', 'http://intranet.local/x', 'http://printer.lan/', 'https://a.example/x', 'https://site.test/',
    'http://127.0.0.1/', 'http://10.1.2.3/', 'http://192.168.0.5/', 'http://172.20.0.1/', 'http://169.254.169.254/latest',
    'http://100.64.0.1/', 'http://0.0.0.0/', 'http://[::1]/', 'http://[2001:db8::1]/', 'https://example.org/a b',
    'https://example.org/"onmouseover=x', 'https://example.org/<script>', 'https://exa‮mple.org/', 'https://exämple.org/',
    'https://example.org/%zz', 'https://example', 'https://example.123', 'https://-bad-.org/', 'https://example.org./',
    '', null, 42, `https://example.org/${'a'.repeat(2100)}`,
  ];
  for (const bad of refused) {
    assert.notEqual(urlProblem(bad), null, String(bad));
    assert.equal(safeHref(bad), null, String(bad));
  }
  assert.equal(sourceHost('https://www.fake-vendor-source.com/products'), 'fake-vendor-source.com');
  assert.equal(sourceHost('javascript:alert(1)'), '');
});

test('a verified research section is normalized; unsafe links are withheld and hostile text stays text', () => {
  const r = reviewResearch({ research: { requested: true, status: 'verified', id: 'res-abc', brief: BRIEF, brief_sha256: 'f'.repeat(64),
    elapsed_s: 12.5, checks: { structural: true } } });
  assert.equal(r.verified, true);
  assert.equal(r.sources.length, 3);
  assert.equal(r.sources[0].href, BRIEF.sources[0].url);
  assert.equal(r.sources[0].host, 'fake-vendor-source.com');
  assert.equal(r.sources[1].href, null);
  assert.equal(r.sources[1].title, '<img src=x onerror=alert(1)>'); // plain text for the DOM helper, never markup
  assert.match(r.sources[1].linkProblem, /http/);
  assert.equal(r.sources[2].href, null);
  assert.deepEqual(r.features[1].unknownSources, ['S9']);
  assert.deepEqual(r.unknowns, ['Thermal duty']);
  assert.equal(r.elapsed, 12.5);
});

test('research not requested, failed, cancelled or malformed never yields sources', () => {
  assert.equal(reviewResearch({}), null);
  assert.equal(reviewResearch({ research: { requested: false, status: 'not_requested' } }), null);
  const failed = reviewResearch({ research: { requested: true, status: 'rejected', id: 'res-x', brief: BRIEF,
    error: { code: 'research_brief_invalid', message: 'FAKE invented URL' } } });
  assert.equal(failed.verified, false);
  assert.deepEqual(failed.sources, []); // a brief attached to a non-verified status is not shown as evidence
  assert.equal(failed.error.code, 'research_brief_invalid');
  const odd = reviewResearch({ research: { requested: true, status: 'verified', brief: { sources: 'x', features: [null] } } });
  assert.deepEqual([odd.sources, odd.features], [[], []]);
});

test('research run facts show counts and models, never credential-like keys or long URL lists', () => {
  const facts = new Map(researchFacts({
    model: 'claude-opus-5-5', verified: true, successful_searches: 5, api_key_source: 'none', session_token: 'x',
    observed_urls: ['https://a.org/'], source_urls: ['https://a.org/'], searches: [{ query: 'q' }],
    effort: { requested: 'xhigh', reported: null }, response_models: ['claude-opus-5-5'],
  }));
  assert.equal(facts.get('model'), 'claude-opus-5-5');
  assert.equal(facts.get('successful searches'), '5');
  assert.equal(facts.get('api key source'), 'none');
  assert.equal(facts.get('effort requested'), 'xhigh');
  assert.equal(facts.get('response models'), 'claude-opus-5-5');
  for (const hidden of ['session token', 'observed urls', 'source urls', 'searches']) assert.ok(!facts.has(hidden), hidden);
  assert.deepEqual(researchFacts(null), []);
});

test('the local CLI path is not a research fact; assistant, auth and retrieval facts stay', () => {
  const facts = new Map(researchFacts({
    kind: 'claude_cli', cli: '/Users/someone/.local/lib/node_modules/@anthropic-ai/claude-code/claude', cli_version: '9.9.9',
    model: 'claude-opus-5-5', init_model: 'claude-opus-5-5', auth: { method: 'claude.ai', subscription: 'max' },
    api_key_source: 'none', usage_models: ['claude-haiku-5-5', 'claude-opus-5-5'],
    internal_retrieval: { model: 'claude-haiku-5-5', web_search_requests: 5, role: 'CLI-internal WebSearch retrieval' },
  }));
  assert.ok(!facts.has('cli'));
  assert.ok(![...facts.values()].some((v) => v.includes('/Users/someone')), 'no value carries the local path');
  assert.equal(facts.get('cli version'), '9.9.9');
  assert.equal(facts.get('model'), 'claude-opus-5-5');
  assert.equal(facts.get('auth method'), 'claude.ai');
  assert.equal(facts.get('auth subscription'), 'max');
  assert.equal(facts.get('api key source'), 'none');
  assert.equal(facts.get('internal retrieval model'), 'claude-haiku-5-5');
  assert.equal(facts.get('internal retrieval web_search_requests'), '5');
});

test('recorded search queries are kept as given text with clipping stated, never links', () => {
  const recorded = `${'q'.repeat(100)}…`; // the stream records a longer query as its first 100 characters and '…'
  const hostile = '<img src=x onerror=alert(1)> https://evil.example/';
  const s = researchSearches({ search_calls: 4, searches: [
    { query: 'shell and tube heat exchanger nozzles', status: 'ok', links: 9 },
    { query: recorded, status: 'ok', links: 1 },
    { query: hostile, status: 'error', links: 0 },
    { query: null, status: 'unanswered', links: 0 },
  ] });
  assert.equal(s.missing, false);
  assert.equal(s.more, 0);
  assert.deepEqual(s.records[0], { query: 'shell and tube heat exchanger nozzles', clipped: null, status: 'ok', links: 9 });
  assert.deepEqual(s.records[1], { query: recorded, clipped: 'recorded', status: 'ok', links: 1 }); // not re-expanded
  assert.equal(s.records[2].query, hostile); // text for the DOM helper; no href is derived from a query
  assert.ok(!('href' in s.records[2]));
  assert.deepEqual(s.records[3], { query: null, clipped: null, status: 'unanswered', links: 0 });
  // a 100-character query or one that merely ends with '…' was not clipped by the recorder
  assert.equal(researchSearches({ searches: [{ query: 'q'.repeat(100), status: 'ok', links: 1 }] }).records[0].clipped, null);
  assert.equal(researchSearches({ searches: [{ query: 'short…', status: 'ok', links: 1 }] }).records[0].clipped, null);
  const long = researchSearches({ searches: [{ query: 'x'.repeat(QUERY_SHOWN + 50), status: 'ok', links: 2 }] }).records[0];
  assert.deepEqual([long.query.length, long.clipped], [QUERY_SHOWN, 'display']);
  const many = researchSearches({ searches: Array.from({ length: SEARCH_RECORDS_SHOWN + 3 }, (_, i) => ({ query: `q${i}`, status: 'ok', links: 1 })) });
  assert.deepEqual([many.records.length, many.more], [SEARCH_RECORDS_SHOWN, 3]);
  const odd = researchSearches({ searches: ['text', null, { query: 5, status: 7, links: -1 }] });
  assert.deepEqual(odd.records, [{ query: null, clipped: null, status: 'unknown', links: null },
    { query: null, clipped: null, status: 'unknown', links: null }, { query: null, clipped: null, status: 'unknown', links: null }]);
  // searches reported but no records kept (the metadata size bound dropped the list): said so, nothing invented
  assert.deepEqual(researchSearches({ search_calls: 3 }), { records: [], more: 0, missing: true });
  assert.equal(researchSearches({ search_calls: 0 }), null);
  assert.equal(researchSearches(null), null);
  const r = reviewResearch({ research: { requested: true, status: 'verified', id: 'res-abc', brief: BRIEF,
    metadata: { search_calls: 1, searches: [{ query: 'FAKE query', status: 'ok', links: 3 }] } } });
  assert.equal(r.searches.records[0].query, 'FAKE query');
});

test('research persistence: assumption records on apply, or only this proposal with its note', () => {
  const saved = reviewResearch({ research: { requested: true, status: 'verified', id: 'res-abc', brief: BRIEF,
    persistence: { status: 'saved_with_assumptions', assumption_ids: ['asm-p-dims', 'asm-p-dn50'] } } });
  assert.deepEqual(saved.persistence, { status: 'saved_with_assumptions', assumptionIds: ['asm-p-dims', 'asm-p-dn50'], note: '' });
  const only = researchPersistence({ status: 'proposal_only', assumption_ids: [], note: 'FAKE note: nothing in the model cites it.' });
  assert.deepEqual(only, { status: 'proposal_only', assumptionIds: [], note: 'FAKE note: nothing in the model cites it.' });
  assert.equal(researchPersistence({ status: 'saved' }), null); // an unknown statement is not shown as either
  assert.equal(researchPersistence(null), null);
  assert.equal(reviewResearch({ research: { requested: true, status: 'verified', brief: BRIEF } }).persistence, null);
});

test('the composer renders queries and persistence as text only', () => {
  const source = fs.readFileSync(path.join(ROOT, 'web', 'js', 'composer.js'), 'utf8');
  for (const name of ['renderSearches', 'renderPersistence']) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start > 0, name);
    const body = source.slice(start, source.indexOf('\n  }\n', start));
    assert.ok(!/h\('a'|href|innerHTML|insertAdjacentHTML|outerHTML/.test(body), `${name} creates no link or markup`);
  }
  const run = source.slice(source.indexOf("h('summary', null, 'Research run')"));
  assert.ok(run.slice(0, 400).includes('renderSearches(research.searches)'), 'queries sit inside the existing Research run details');
});

test('assumption citations map model IDs to cited source IDs', () => {
  const cites = assumptionCitations({ proposed_assumptions: [
    { key: 'shell', id: 'asm-prop-1-shell', source_ids: ['S1', 'S2'] }, { key: 'dims', id: 'asm-prop-1-dims', source_ids: [] }, null] });
  assert.deepEqual([...cites.entries()], [['asm-prop-1-shell', ['S1', 'S2']]]);
});

test('research is offered only when the service advertises it and has a researcher', () => {
  assert.equal(researchOffered({ capabilities: { pipe_segment_move: true } }), false); // an older service
  assert.equal(researchOffered({ capabilities: { equipment_research: true }, planning: { research: { available: false } } }), false);
  assert.equal(researchOffered({ capabilities: { equipment_research: false }, planning: { research: { available: true } } }), false);
  assert.equal(researchOffered({ capabilities: { equipment_research: true }, planning: { research: { available: true } } }), true);
});

test('stage labels follow the service record, then what was requested', () => {
  assert.deepEqual(stageInfo(null, false), { label: 'Planning', step: null, stage: 'planning', research: false });
  assert.equal(stageInfo(null, true).label, 'Researching, then planning');
  assert.deepEqual(stageInfo({ research: true, stage: 'researching' }, true),
    { label: 'Researching', step: 'step 1 of 2', stage: 'researching', research: true });
  assert.deepEqual(stageInfo({ research: true, stage: 'planning' }, true),
    { label: 'Planning', step: 'step 2 of 2', stage: 'planning', research: true });
  assert.equal(stageInfo({ research: false, stage: 'planning' }, true).label, 'Planning'); // the service's word wins
});

test('creation examples follow the model: blank, open outlet, complete', () => {
  const blank = empty();
  assert.equal(isEmptyProject(blank), true);
  const first = creationExamples(blank);
  assert.equal(first.length, 2);
  assert.match(first[0], /TK-101.*HV-101/);
  assert.match(first[1], /HX-101/);
  for (const text of first) assert.doesNotMatch(text, /\d+(\.\d+)?\s*m\b/); // no dimensions to copy

  const partial = empty();
  partial.model.entities = { 'ent-t': { id: 'ent-t', tag: 'TK-101', kind: 'tank' }, 'ent-v': { id: 'ent-v', tag: 'HV-101', kind: 'isolation_valve' } };
  partial.model.ports = {
    'port-t-outlet': { id: 'port-t-outlet', entity_id: 'ent-t', name: 'outlet', direction: 'out', required: true },
    'port-v-inlet': { id: 'port-v-inlet', entity_id: 'ent-v', name: 'inlet', direction: 'in', required: true },
    'port-v-outlet': { id: 'port-v-outlet', entity_id: 'ent-v', name: 'outlet', direction: 'out', required: true, connection_status: 'unresolved' },
  };
  partial.model.connections = { 'conn-a': { id: 'conn-a', tag: 'L-101', from_port_id: 'port-t-outlet', to_port_id: 'port-v-inlet' } };
  assert.deepEqual(openPorts(partial).map((x) => x.port.id), ['port-v-outlet']);
  const next = creationExamples(partial);
  assert.equal(next.length, 1);
  assert.match(next[0], /^Continue from HV-101 outlet: add a centrifugal pump P-101 with its own suction isolation valve HV-102/);

  assert.deepEqual(openPorts(fixture()), []);
  assert.deepEqual(creationExamples(fixture()), []); // a complete project gets no creation prompt
});
