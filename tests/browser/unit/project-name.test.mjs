// Project display name rule and editor decisions (project-name.js). The rule mirrors
// skid.core.validation.project_name_problem; the expected verdicts below follow
// docs/PROJECT_NAMING.md.
import test from 'node:test';
import assert from 'node:assert/strict';
import { PROJECT_NAME_MAX, commitDecision, conflictNote, displayName, nameProblem, trimName } from '../../../web/js/project-name.js';

const unnamed = { id: 'project-pump-skid', revision: 4 };
const named = { id: 'project-pump-skid', name: 'North pump skid', revision: 5 };

// The twelve bidirectional formatting code points refused by the service.
const BIDI_FORBIDDEN = [0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069];
// Same cases as tests/test_project_name.py: no base character, or genuine names that keep one.
const NO_BASE_CHARACTER = [
  '\u200b', '\ufeff', '\u2060', '\u00ad', '\u200b\u200b\u2060',
  '\u0301', '\u0301\u0308', '\u20dd',
  '\u200b \u200b', '\u00ad\u00a0\u0301', '\u2060\u3000\ufeff', '\u0301\u200d\u0300',
];
const WITH_BASE_CHARACTER = [
  'e\u0301té', 'नमस\u094dत\u0947', 'ป\u0e31\u0e4aม',
  'משאבה', '\u{1f469}\u200d\u{1f527}', '\u{1f1f9}\u{1f1f7}', '1\ufe0f\u20e3',
  'Skid\u200b2', 'co\u00adoperative', '—', '™', '#',
];

test('display name: the valid stored name, else the stable project ID', () => {
  assert.equal(displayName(unnamed), 'project-pump-skid');
  assert.equal(displayName(named), 'North pump skid');
  assert.equal(displayName({ id: 'p', name: ' padded ' }), 'p', 'an invalid stored name is not shown');
  assert.equal(displayName({ id: 'p', name: '' }), 'p');
  assert.equal(displayName({ id: 'p', name: 42 }), 'p');
  assert.equal(displayName({ id: 'p', name: '\u200b' }), 'p', 'an invisible stored name is not shown');
  assert.equal(displayName(null), '');
});

test('name rule: trimmed, 1–120 code points, no controls, separators, noncharacters or bidi formatting', () => {
  assert.equal(nameProblem('North pump skid'), null);
  assert.equal(nameProblem('  North pump skid \t'), null, 'surrounding whitespace is trimmed like the command');
  assert.match(nameProblem('   '), /Enter a name/);
  assert.match(nameProblem(''), /Enter a name/);
  assert.equal(nameProblem('a'.repeat(PROJECT_NAME_MAX)), null);
  assert.match(nameProblem('a'.repeat(PROJECT_NAME_MAX + 1)), /121 characters/);
  assert.equal(nameProblem('\u{1f600}'.repeat(PROJECT_NAME_MAX)), null, 'astral characters count as one code point');
  assert.equal(nameProblem('Skid \u{1f469}\u200d\u{1f527} <b>&"\''), null, 'emoji sequences and markup characters are allowed');
  assert.equal(nameProblem('Pump\u00a0skid'), null, 'inner no-break space is allowed');
  assert.equal(nameProblem('Ünïcödé 泵 مضخة'), null);
  for (const bad of ['a\tb', 'a\nb', 'a\u007fb', 'a\u0085b', 'a\u2028b', 'a\u2029b', 'a\ufdd0b', 'a\uffffb', 'a\u{1fffe}b',
    'a\u202eb', 'a\u2066b', 'a\u200eb', 'a\u061cb', 'a\ud800b']) {
    assert.match(nameProblem(bad), /disallowed character U\+/, JSON.stringify(bad));
  }
  assert.equal(nameProblem('a\u200bb'), null, 'zero-width space beside letters is not in the refused set');
  assert.match(nameProblem(7), /must be text/);
});

test('every bidi formatting code point is refused by number, wherever it appears', () => {
  for (const code of BIDI_FORBIDDEN) {
    const hex = code.toString(16).toUpperCase().padStart(4, '0');
    const ch = String.fromCodePoint(code);
    for (const value of [`Pump${ch}skid`, `${ch}Pump`, `Pump${ch}`]) {
      assert.equal(nameProblem(value), `Contains the disallowed character U+${hex}.`, JSON.stringify(value));
    }
  }
  for (const value of ['a\u200bb', 'a\u200cb', 'a\u200db', 'a\u2060b', 'a\u206ab']) {
    assert.equal(nameProblem(value), null, `${JSON.stringify(value)}: other format characters beside a letter`);
  }
});

test('base character: format-, mark- and space-only text is refused; real names with marks/joiners pass', () => {
  for (const value of NO_BASE_CHARACTER) {
    assert.match(nameProblem(value), /at least one visible letter/, JSON.stringify(value));
    assert.match(nameProblem(` ${value} `), /at least one visible letter/, `${JSON.stringify(value)} after trimming`);
    assert.equal(commitDecision(value, named, { name: named.name, revision: 5 }).action, 'invalid');
  }
  assert.match(nameProblem('\u200b\u202e'), /U\+202E/, 'a forbidden character keeps its specific reason');
  for (const value of WITH_BASE_CHARACTER) {
    assert.equal(nameProblem(value), null, JSON.stringify(value));
  }
});

test('trimming follows Python str.strip(): U+001C–001F and U+0085 trimmed, U+FEFF kept', () => {
  assert.equal(trimName('\u001c\u0085 x \u3000'), 'x');
  assert.equal(trimName('\ufeffx'), '\ufeffx');
  assert.match(nameProblem('\ufeff'), /at least one visible letter/, 'a lone BOM is not trimmed but has no base character, as in the service');
});

test('commit: invalid and unchanged send nothing; a fresh base sends against its revision', () => {
  const base = { name: 'project-pump-skid', revision: 4 };
  assert.equal(commitDecision('  ', unnamed, base).action, 'invalid');
  assert.deepEqual(commitDecision(' project-pump-skid ', unnamed, base), { action: 'unchanged', name: 'project-pump-skid' });
  assert.deepEqual(commitDecision('North pump skid ', unnamed, base), { action: 'send', name: 'North pump skid', expectedRevision: 4 });
  assert.equal(commitDecision('north pump skid', named, { name: 'North pump skid', revision: 5 }).action, 'send', 'case changes are changes');
});

test('commit after a change elsewhere: visible conflict, confirm first, never a silent overwrite', () => {
  const base = { name: 'project-pump-skid', revision: 4 };
  const renamed = { id: 'project-pump-skid', name: 'Theirs', revision: 5 };
  const d = commitDecision('Mine', renamed, base);
  assert.equal(d.action, 'confirm');
  assert.match(d.reason, /Renamed elsewhere to “Theirs” \(revision 5\)/);
  const rebased = { name: displayName(renamed), revision: renamed.revision };
  assert.deepEqual(commitDecision('Mine', renamed, rebased), { action: 'send', name: 'Mine', expectedRevision: 5 });
  const moved = { id: 'project-pump-skid', revision: 6 };
  assert.match(conflictNote(moved, base), /revision 6 while you were editing/);
  assert.equal(commitDecision('Mine', moved, base).action, 'confirm');
  assert.equal(conflictNote(unnamed, base), null);
  assert.equal(commitDecision('Theirs', renamed, base).action, 'unchanged', 'already the shown name');
});
