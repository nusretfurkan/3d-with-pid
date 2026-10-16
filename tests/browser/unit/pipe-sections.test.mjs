// Straight pipe sections and the ported section-move transform (pipe-sections.js): leg records,
// raycast hit classification (straight legs only, never bends), offsets, terminal stubs, refusal
// codes, and parity with skid/route_edit.py through the shared fixture and a direct cross-check of
// generated cases against the Python module (body screen excluded on both sides).
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  RouteEditError, checkOffset, movePoints, moveSection, routeKey, sectionAtPoint, sectionRecords, simplify, snapDirection, straightSections,
} from '../../../web/js/pipe-sections.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'pipe-segments.json');
const PY = process.env.SKID_PYTHON ? path.resolve(ROOT, process.env.SKID_PYTHON) : path.join(ROOT, '.venv-cad', 'bin', 'python');
const OD = 0.0603;
const DN = 50;
const ROUTE = [[1.5, 0, 0.5], [1.5, 0, 0.95], [1.9, 0, 0.95], [1.9, 0, 0.7], [2.21, 0, 0.7]];
const UP = [0, 0, 1];
const INTO_X = [-1, 0, 0]; // outward direction of a port entered along +X

function project(points, status = 'current') {
  return {
    model: { connections: { c: { nominal_diameter_mm: DN, outside_diameter_m: OD, from_port_id: 'a', to_port_id: 'b' } } },
    physical: { routes: { c: { points_m: points, status } } },
  };
}

const codeOf = (fn) => {
  try {
    fn();
    return null;
  } catch (error) {
    assert.ok(error instanceof RouteEditError, String(error));
    return error.code;
  }
};

test('section records: tangency limits of 1.5 DN bends (reduced on short runs), axes and move axes', () => {
  const s = straightSections(project(ROUTE), 'c');
  assert.equal(s.length, 4);
  assert.deepEqual(s.map((x) => x.axis), [2, 0, 2, 0]);
  assert.deepEqual(s[1].move_axes, [1, 2]);
  assert.deepEqual(s[0].start_m, [1.5, 0, 0.5], 'a terminal leg starts at the port');
  assert.ok(Math.abs(s[0].end_m[2] - 0.875) < 1e-12);
  assert.ok(Math.abs(s[1].start_m[0] - 1.575) < 1e-12 && Math.abs(s[1].end_m[0] - 1.825) < 1e-12);
  // 0.25 m run between two corners: 0.49 x 0.25 = 0.1225 > 0.075, so full bends
  assert.ok(Math.abs(s[2].length_m - (0.25 - 0.075 - 0.075)) < 1e-12);
  assert.deepEqual(s[3].end_m, [2.21, 0, 0.7]);
  assert.equal(straightSections(project([[0, 0, 0], [1, 1, 0]]), 'c').length, 0, 'not orthogonal: no sections');
  assert.equal(routeKey(project(ROUTE), 'c'), JSON.stringify(ROUTE));
});

test('simplify drops repeats and merges same-direction legs; a reversal is refused', () => {
  assert.deepEqual(simplify([[0, 0, 0], [0, 0, 0], [1, 0, 0], [2, 0, 0], [2, 1, 0]]), [[0, 0, 0], [2, 0, 0], [2, 1, 0]]);
  assert.equal(codeOf(() => simplify([[0, 0, 0], [1, 0, 0], [0.5, 0, 0]])), 'route_edit_reverses');
});

test('a hit point names the straight leg under it; bends and points off the surface name none', () => {
  const s = straightSections(project(ROUTE), 'c');
  const r = OD / 2;
  assert.equal(sectionAtPoint(s, [1.7, -r, 0.95], r), 1, 'front surface of the X leg');
  assert.equal(sectionAtPoint(s, [1.7, 0, 0.95 + r * 0.98], r), 1, 'tessellated top surface');
  assert.equal(sectionAtPoint(s, [1.5 - r, 0, 0.7], r), 0);
  assert.equal(sectionAtPoint(s, [1.9 - 0.02, -r, 0.95 - 0.02], r), null, 'inside the bend at the 1.9/0.95 corner');
  assert.equal(sectionAtPoint(s, [1.7, 0, 0.95], r), null, 'the centreline is not the surface');
  assert.equal(sectionAtPoint(s, [1.7, -0.2, 0.95], r), null);
});

test('move a middle section: neighbours stretch, both nozzle ends and tangents stay', () => {
  const out = movePoints(ROUTE, UP, INTO_X, 1, [0, 0, 0.2], OD, DN);
  assert.deepEqual(out, [[1.5, 0, 0.5], [1.5, 0, 1.15], [1.9, 0, 1.15], [1.9, 0, 0.7], [2.21, 0, 0.7]]);
  const y = movePoints(ROUTE, UP, INTO_X, 1, [0, -0.3, 0], OD, DN);
  assert.deepEqual(y[0], ROUTE[0]);
  assert.deepEqual(y[y.length - 1], ROUTE[ROUTE.length - 1]);
  assert.equal(y.length, 7, 'connectors along Y are added on both sides');
  assert.deepEqual(y.slice(1, 6), [[1.5, 0, 0.95], [1.5, -0.3, 0.95], [1.9, -0.3, 0.95], [1.9, 0, 0.95], [1.9, 0, 0.7]]);
});

test('terminal sections keep a fixed 0.16 m nozzle stub; too short ones are refused', () => {
  const out = movePoints(ROUTE, UP, INTO_X, 0, [0, 0.2, 0], OD, DN);
  assert.deepEqual(out.slice(0, 3), [[1.5, 0, 0.5], [1.5, 0, 0.66], [1.5, 0.2, 0.66]]);
  assert.equal(movePoints(ROUTE, UP, INTO_X, 3, [0, 0.2, 0], OD, DN).length, 8, 'a 0.31 m end leg keeps one 0.16 m stub (two connector bends added)');
  const shortEnd = [[1.5, 0, 0.5], [1.5, 0, 0.95], [2.1, 0, 0.95], [2.1, 0, 0.7], [2.21, 0, 0.7]];
  assert.equal(codeOf(() => movePoints(shortEnd, UP, INTO_X, 3, [0, 0.2, 0], OD, DN)), 'route_edit_stub_too_short', '0.11 m end leg');
  assert.equal(codeOf(() => movePoints([[0, 0, 0], [0.3, 0, 0]], [1, 0, 0], [1, 0, 0], 0, [0, 0, 0.2], OD, DN)), 'route_edit_stub_too_short', 'one leg needs > 0.32 m');
});

test('refusal codes as the service: no-op, two axes, parallel, too far, unknown section, bend room, port tangent', () => {
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0, 0], OD, DN)), 'route_edit_no_op');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0.1, 0.1], OD, DN)), 'route_edit_invalid_offset');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0.1, 0, 0], OD, DN)), 'route_edit_offset_parallel');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 10.5, 0], OD, DN)), 'route_edit_offset_too_large');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 4, [0, 0.1, 0], OD, DN)), 'route_edit_unknown_section');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0, -0.23], OD, DN)), 'route_edit_bend_room', '0.02 m leg left');
  assert.deepEqual(movePoints(ROUTE, UP, INTO_X, 1, [0, 0, -0.25], OD, DN), [[1.5, 0, 0.5], [1.5, 0, 0.7], [2.21, 0, 0.7]], 'legs merge back');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0, -0.4], OD, DN)), 'route_edit_bend_room', '0.05 m first leg');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0, -0.6], OD, DN)), 'route_edit_port_tangent', 'would leave the nozzle downward');
  assert.deepEqual(checkOffset(straightSections(project(ROUTE), 'c')[1], [0, 0, 0.1]), { ok: true, code: null, message: null });
  assert.equal(checkOffset(straightSections(project(ROUTE), 'c')[1], [0.1, 0, 0]).code, 'route_edit_offset_parallel');
});

test('offsets below 1 mm refuse in the service order; exactly +/-1 mm is admissible', () => {
  const section = straightSections(project(ROUTE), 'c')[1];
  for (const d of [1e-7, -1e-7, 0.0009999, -0.000999]) {
    assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, d, 0], OD, DN)), 'route_edit_offset_too_small', `${d}`);
    assert.deepEqual(checkOffset(section, [0, d, 0]), { ok: false, code: 'route_edit_offset_too_small', message: 'A section move must be at least 0.001 m (1 mm).' });
  }
  for (const d of [0.001, -0.001]) assert.deepEqual(checkOffset(section, [0, d, 0]), { ok: true, code: null, message: null }, `${d}`);
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 0, 0], OD, DN)), 'route_edit_no_op');
  assert.equal(checkOffset(section, [1e-7, 0, 0]).code, 'route_edit_offset_parallel');
  assert.equal(checkOffset(section, [0, 1e-7, 1e-7]).code, 'route_edit_invalid_offset');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [1e-7, 0, 0], OD, DN)), 'route_edit_offset_parallel');
  assert.equal(codeOf(() => movePoints(ROUTE, UP, INTO_X, 1, [0, 1e-7, 1e-7], OD, DN)), 'route_edit_invalid_offset');
});

test('moveSection uses the stored route and the scene port frames; a route not current or a skewed frame is refused', () => {
  const frames = { start: [6e-17, 0, 1], end: [-1, 1e-17, 0] };
  assert.deepEqual(moveSection(project(ROUTE), 'c', 1, [0, 0, 0.2], frames)[1], [1.5, 0, 1.15]);
  assert.equal(codeOf(() => moveSection(project(ROUTE, 'needs_reroute'), 'c', 1, [0, 0, 0.2], frames)), 'route_edit_not_current');
  assert.equal(codeOf(() => moveSection(project(ROUTE), 'c', 1, [0, 0, 0.2], { start: [0.7, 0, 0.7], end: [-1, 0, 0] })), 'route_edit_frame_unavailable');
  assert.deepEqual(snapDirection([-1e-13, -1, 0]), [0, -1, 0]);
});

/** Run skid.route_edit over cases: [{points, start, end, k, offset}] -> [{points} | {code}] (no obstacles). */
function python(cases) {
  const code = [
    'import json, sys',
    'from skid.route_edit import RouteEditError, move_points, section_records',
    'out = []',
    'for c in json.load(sys.stdin):',
    '    rec = {}',
    '    try:',
    "        rec['sections'] = section_records(c['points'], c['dn'])[1]",
    '    except RouteEditError as e:',
    "        rec['sections_code'] = e.code",
    '    try:',
    "        rec['points'] = move_points(c['points'], c['start'], c['end'], c['k'], c['offset'], c['od'], c['dn'])",
    '    except RouteEditError as e:',
    "        rec['code'] = e.code",
    '    out.append(rec)',
    'print(json.dumps(out))',
  ].join('\n');
  const r = spawnSync(PY, ['-c', code], { cwd: ROOT, input: JSON.stringify(cases), encoding: 'utf8' });
  if (r.status !== 0) throw new Error(r.stderr);
  return JSON.parse(r.stdout);
}

function js(c) {
  const rec = {};
  try { rec.sections = sectionRecords(c.points, c.dn).sections; } catch (error) { rec.sections_code = error.code; }
  try { rec.points = movePoints(c.points, c.start, c.end, c.k, c.offset, c.od, c.dn); } catch (error) { rec.code = error.code; }
  return rec;
}

test('parity with skid/route_edit.py: shared fixture (tests/fixtures/pipe-segments.json)', { skip: !fs.existsSync(FIXTURE) && 'fixture not present yet' }, () => {
  const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  assert.equal(fixture.schema, 'skid.route_edit.parity/1');
  assert.ok(fixture.cases.length >= 10, 'fixture has cases');
  let screened = 0;
  for (const c of fixture.cases) {
    const name = c.name;
    if (c.sections) assert.deepEqual(sectionRecords(c.points_m, c.nominal_diameter_mm).sections, c.sections, `${name}: sections`);
    let got;
    try {
      got = { points_m: movePoints(c.points_m, c.start_direction, c.end_direction, c.segment_index, c.offset_m, c.outside_diameter_m, c.nominal_diameter_mm) };
    } catch (error) {
      assert.ok(error instanceof RouteEditError, `${name}: ${error}`);
      got = { error: error.code };
    }
    if (c.expect.error === 'route_edit_intrusion') {
      // the body screen is the service's last check: the port must pass every earlier one
      assert.ok(got.points_m, `${name}: expected a candidate before the service's body screen, got ${got.error}`);
      screened += 1;
    } else {
      assert.deepEqual(got, c.expect, name);
    }
  }
  console.log(`# fixture cases: ${fixture.cases.length} (${screened} refused only by the service's body screen)`);
});

test('parity with skid/route_edit.py: generated cases cross-checked against the Python module', { skip: !fs.existsSync(PY) && 'no .venv-cad python' }, () => {
  let seed = 7;
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const cases = [];
  for (let n = 0; n < 400; n += 1) {
    const legs = 1 + Math.floor(rnd() * 5);
    const p = [[Number((rnd() * 4 - 2).toFixed(3)), Number((rnd() * 4 - 2).toFixed(3)), Number((rnd() * 2).toFixed(3))]];
    let last = -1;
    for (let i = 0; i < legs; i += 1) {
      const axis = pick([0, 1, 2].filter((a) => a !== last));
      last = axis;
      const q = p[p.length - 1].slice();
      q[axis] = Number((q[axis] + pick([-1, 1]) * (0.05 + rnd() * 1.2)).toFixed(4));
      p.push(q);
    }
    const dir = (a, b) => { const d = [0, 0, 0]; const k = [0, 1, 2].find((i) => a[i] !== b[i]); d[k] = Math.sign(b[k] - a[k]); return d; };
    const start = dir(p[0], p[1]);
    const end = dir(p[p.length - 1], p[p.length - 2]);
    const k = Math.floor(rnd() * (legs + (rnd() < 0.05 ? 1 : 0)));
    const offset = [0, 0, 0];
    offset[pick([0, 1, 2])] = Number(pick([0.05, 0.1, 0.25, -0.15, -0.4, 0.8, 11, 0, 1e-7]).toFixed(7));
    cases.push({ points: p, start, end, k, offset, od: OD, dn: pick([25, 50, 80]) });
  }
  const expected = python(cases);
  let compared = 0;
  cases.forEach((c, i) => {
    assert.deepEqual(js(c), expected[i], `case ${i}: ${JSON.stringify(c)}`);
    compared += 1;
  });
  const outcomes = new Set(expected.map((e) => e.code || 'moved'));
  assert.ok(outcomes.has('moved') && outcomes.size >= 5, `varied outcomes ${[...outcomes]}`);
  console.log(`# python parity outcomes: ${[...outcomes].sort().join(', ')}`);
  assert.equal(compared, 400);
});
