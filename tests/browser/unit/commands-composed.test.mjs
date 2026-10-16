// Composed-equipment and batch builders (web/js/commands-model.js) against the real fixture and the
// example definition, both read only. The service and core remain the validators; these tests check
// that the UI prepares complete envelopes, fresh identities, explicit provenance and honest review text.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  BATCH_LIMIT, batchErrors, buildBatch, buildInsertEquipment, buildPlanExample, buildSetParameter, buildSetRoute, describeCommand, isComposed,
  kindLabel, parameterNames, recipeRange, reviewCommandText, tagInUse,
} from '../../../web/js/commands-model.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const fixture = () => JSON.parse(fs.readFileSync(path.join(ROOT, 'examples', 'pump_skid.json'), 'utf8')).current;
const DEFINITION_FILE = path.join(ROOT, 'examples', 'recipes', 'horizontal_drum.definition.json');
const definition = () => (fs.existsSync(DEFINITION_FILE)
  ? JSON.parse(fs.readFileSync(DEFINITION_FILE, 'utf8'))
  : { schema: 'skid.equipment.definition/1', id: 'horizontal-drum', name: 'Horizontal drum', function: 'Illustrative.',
    geometry: { parameters: { shell_length: { value: 1.4, unit: 'm', minimum: 0.8, maximum: 2.6 }, shell_diameter: { value: 0.6, unit: 'm' } } },
    ports: { inlet: { direction: 'in', required: true, service: 'liquid' }, outlet: { direction: 'out', required: true, service: 'liquid' } },
    symbol: { shape: 'horizontal_vessel', anchors: { inlet: [-60, 0], outlet: [60, 0] } }, unknowns: [] });

function withDrum(project) {
  const d = definition();
  const insert = buildInsertEquipment(project, 'conn-discharge-2', { definition: d, tag: 'D-101', lineTag: 'L-106' });
  const p = structuredClone(project);
  const e = insert.payload.entity;
  p.model.entities[e.id] = e;
  for (const port of insert.payload.ports) p.model.ports[port.id] = port;
  return { project: p, entityId: e.id, definition: d };
}

function allIds(project) {
  const m = project.model;
  return new Set([...Object.keys(m.entities), ...Object.keys(m.ports), ...Object.keys(m.connections), ...Object.keys(m.assumptions),
    ...Object.keys(project.pid.symbols), ...Object.keys(project.pid.edges)]);
}

test('composed entities are labelled by their declared definition, parameters in authored order', () => {
  const { project, entityId, definition: d } = withDrum(fixture());
  const drum = project.model.entities[entityId];
  assert.equal(isComposed(drum), true);
  assert.equal(isComposed(project.model.entities['ent-pump']), false);
  assert.equal(kindLabel(drum), `${d.name} (composed)`);
  assert.equal(kindLabel(project.model.entities['ent-pump']), 'Centrifugal pump');
  assert.equal(kindLabel({ kind: 'composed_equipment' }), 'Composed equipment (no definition)');
  assert.deepEqual(parameterNames(drum), Object.keys(d.geometry.parameters));
  const first = Object.keys(d.geometry.parameters)[0];
  const declared = d.geometry.parameters[first];
  assert.deepEqual(recipeRange(drum, first), { minimum: declared.minimum, maximum: declared.maximum, unit: declared.unit });
  assert.equal(recipeRange(project.model.entities['ent-pump'], 'motor_length'), null);
});

test('insert_equipment from a definition: fresh IDs, explicit initial provenance, no invented placement', () => {
  const p = fixture();
  const d = definition();
  const cmd = buildInsertEquipment(p, 'conn-discharge-2', { definition: d });
  const pl = cmd.payload;
  assert.equal(cmd.type, 'insert_equipment');
  assert.deepEqual(Object.keys(pl).sort(), ['connection_id', 'downstream_connection', 'downstream_edge', 'entity', 'ports', 'symbol']);
  assert.equal(pl.entity.kind, 'composed_equipment');
  assert.equal(pl.entity.template_version, 1);
  assert.deepEqual(pl.entity.definition, d);
  assert.deepEqual(pl.ports.map((x) => [x.name, x.direction]), [['inlet', 'in'], ['outlet', 'out']]);
  assert.equal(pl.downstream_connection.to_port_id, p.model.connections['conn-discharge-2'].to_port_id);
  assert.equal(pl.downstream_connection.from_port_id, pl.ports[1].id);
  const fresh = [pl.entity.id, ...pl.ports.map((x) => x.id), pl.downstream_connection.id, pl.symbol.id, pl.downstream_edge.id];
  assert.equal(new Set(fresh).size, fresh.length);
  for (const id of fresh) assert.ok(!allIds(p).has(id), `${id} is new`);
  assert.equal(tagInUse(p, pl.entity.tag), null);
  assert.ok(!('placement' in pl));
  // initial values are the authoring defaults, every one referencing the single new assumption record
  const added = Object.keys(cmd.assumptions);
  assert.equal(added.length, 1);
  assert.deepEqual(Object.keys(pl.entity.parameters), Object.keys(d.geometry.parameters));
  for (const [name, q] of Object.entries(pl.entity.parameters)) {
    assert.deepEqual(q, { value: d.geometry.parameters[name].value, unit: d.geometry.parameters[name].unit, assumption_id: added[0] });
  }
  assert.ok(!pl.downstream_edge.reason.includes(pl.entity.tag), 'stored reason text carries no tag that a rename could leave stale');
  const review = reviewCommandText(JSON.stringify(cmd), p);
  assert.equal(review.ok, true, review.errors.join('; '));
  assert.ok(review.lines.some((l) => /Insert Horizontal drum \(composed\) .* into L-104: NRV-101 → .* → HV-102/.test(l)), review.lines.join('\n'));
  assert.ok(review.lines.some((l) => /generic P&ID outline horizontal vessel/.test(l)));
  assert.ok(review.lines.some((l) => /explicitly unresolved placement/.test(l)));
});

test('insert_equipment copies a composed donor with its current values and provenance', () => {
  const { project, entityId } = withDrum(fixture());
  const donor = project.model.entities[entityId];
  donor.parameters[Object.keys(donor.parameters)[0]] = { value: 1.9, unit: 'm', assumption_id: 'asm-dn50-pipe' };
  const cmd = buildInsertEquipment(project, 'conn-discharge-3', { donorId: entityId });
  assert.deepEqual(cmd.payload.entity.parameters, donor.parameters);
  assert.equal(cmd.payload.entity.definition, donor.definition);
  assert.ok(!('assumptions' in cmd), 'copied values keep their existing provenance');
});

test('without a definition the example cannot be sent until one is pasted', () => {
  const p = fixture();
  const cmd = buildInsertEquipment(p, 'conn-discharge-2');
  assert.equal(typeof cmd.payload.entity.definition, 'string');
  const review = reviewCommandText(JSON.stringify(cmd), p);
  assert.equal(review.ok, false);
  assert.ok(review.errors.some((e) => /definition object/.test(e)));
});

test('batch envelopes carry operations only, each with its own assumptions', () => {
  const p = fixture();
  const cmd = buildPlanExample(p, 'ent-pump');
  assert.equal(cmd.type, 'batch');
  assert.ok(!('assumptions' in cmd));
  assert.deepEqual(cmd.payload.operations.map((o) => o.type), ['rename_entity', 'set_parameter']);
  for (const op of cmd.payload.operations) assert.ok(Object.keys(op).every((k) => ['type', 'payload', 'assumptions'].includes(k)));
  const set = cmd.payload.operations[1];
  assert.ok(set.assumptions[set.payload.quantity.assumption_id], 'the operation adds the assumption it references');
  assert.equal(tagInUse(p, cmd.payload.operations[0].payload.tag), null);
  const review = reviewCommandText(JSON.stringify(cmd), p);
  assert.equal(review.ok, true, review.errors.join('; '));
  assert.match(review.lines[0], /Atomic batch of 2 operations: all apply or none; one revision, one undo step and one rebuild\./);
  assert.match(review.lines[1], /^1\. Rename P-101/);
  assert.match(review.lines[2], /^2\. Set P-101 /);
});

test('a route example without points is refused before sending', () => {
  const p = fixture();
  const review = reviewCommandText(JSON.stringify(buildSetRoute(p, 'conn-discharge-1', [])), p);
  assert.equal(review.ok, false);
  assert.ok(review.errors.some((e) => /at least two points/.test(e)));
});

test('batch review refuses shapes the core rejects', () => {
  const p = fixture();
  const ok = buildBatch(p, [{ type: 'rename_entity', payload: { entity_id: 'ent-pump', tag: 'P-9' } }]);
  assert.deepEqual(batchErrors(ok), []);
  const bad = (mutate) => { const c = structuredClone(ok); mutate(c); return reviewCommandText(JSON.stringify(c), p); };
  assert.ok(bad((c) => { c.payload.operations = []; }).errors.some((e) => /1 to 12/.test(e)));
  assert.ok(bad((c) => { c.payload.operations = Array.from({ length: BATCH_LIMIT + 1 }, () => c.payload.operations[0]); }).errors.some((e) => /1 to 12/.test(e)));
  assert.ok(bad((c) => { c.payload.operations.push({ type: 'batch', payload: { operations: [] } }); }).errors.some((e) => /batch cannot be part/.test(e)));
  assert.ok(bad((c) => { c.payload.operations.push({ type: 'undo', payload: {} }); }).errors.some((e) => /undo cannot be part/.test(e)));
  assert.ok(bad((c) => { c.payload.operations[0].id = 'x'; }).errors.some((e) => /unsupported fields id/.test(e)));
  assert.ok(bad((c) => { c.assumptions = { a: { description: 'd', source: 's' } }; }).errors.some((e) => /inside its operations/.test(e)));
  assert.ok(bad((c) => { c.payload.note = 'x'; }).errors.some((e) => /Unsupported batch payload fields: note/.test(e)));
  const set = buildSetParameter(p, 'ent-pump', 'motor_length', 0.5);
  assert.match(describeCommand(buildBatch(p, [{ type: 'set_parameter', payload: set.payload, assumptions: set.assumptions }]), p).join(' '),
    /Adds assumption record|motor_length/);
});
