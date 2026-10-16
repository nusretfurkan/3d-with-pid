// Project display name: rule and editor decisions (pure; unit-tested in Node).
// Mirrors skid.core.validation.project_name_problem so the editor can refuse locally;
// the service remains the authority. The project ID is never edited.

export const PROJECT_NAME_MAX = 120;
const FORBIDDEN = new Set([0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069]);
// A base character: general category outside Other (C), Separator (Z) and Mark (M), so names made
// only of format characters, marks or spaces are refused while ZWJ emoji and accented letters pass.
const BASE = /[^\p{C}\p{Z}\p{M}]/u;

function isValidStored(name) {
  return typeof name === 'string' && nameProblem(name, { trim: false }) === null;
}

/** The title shown for a project: its valid stored name, else its stable ID. */
export function displayName(project) {
  if (!project) return '';
  return isValidStored(project.name) ? project.name : String(project.id ?? '');
}

// Python str.strip() whitespace (str.isspace): unlike JavaScript \s it includes U+001C–001F and
// U+0085 and excludes U+FEFF.
const WS = '\\t\\n\\v\\f\\r \\u001c-\\u001f\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const STRIP = new RegExp(`^[${WS}]+|[${WS}]+$`, 'gu');

export function trimName(raw) {
  return String(raw ?? '').replace(STRIP, '');
}

/**
 * Why a name would be refused, or null. With trim (the default) the text is first trimmed,
 * as the command does; the stored-name check passes trim: false.
 */
export function nameProblem(raw, { trim = true } = {}) {
  if (typeof raw !== 'string') return 'The name must be text.';
  const value = trim ? trimName(raw) : raw;
  if (!trimName(value)) return 'Enter a name.';
  if (!trim && value !== trimName(value)) return 'The name has leading or trailing spaces.';
  const points = [...value];
  if (points.length > PROJECT_NAME_MAX) return `${points.length} characters; at most ${PROJECT_NAME_MAX} are allowed.`;
  for (const ch of points) {
    const code = ch.codePointAt(0);
    const control = code < 0x20 || (code >= 0x7f && code <= 0x9f);
    const surrogate = code >= 0xd800 && code <= 0xdfff;
    const separator = code === 0x2028 || code === 0x2029;
    const nonchar = (code >= 0xfdd0 && code <= 0xfdef) || (code & 0xfffe) === 0xfffe;
    if (control || surrogate || separator || nonchar || FORBIDDEN.has(code)) {
      return `Contains the disallowed character U+${code.toString(16).toUpperCase().padStart(4, '0')}.`;
    }
  }
  if (!BASE.test(value)) return 'Use at least one visible letter, number, punctuation mark or symbol.';
  return null;
}

/**
 * Decide what Save does with the editor's text.
 * base = {name, revision} recorded when editing started or last confirmed.
 * - invalid: refused locally (reason);
 * - unchanged: equal to the name shown now, so no command is sent;
 * - confirm: the project changed since base; show the reason, rebase, and require a second Save;
 * - send: one fresh rename_project command expecting base.revision.
 */
export function commitDecision(text, project, base) {
  const problem = nameProblem(text);
  if (problem) return { action: 'invalid', reason: problem };
  const name = trimName(text);
  if (name === displayName(project)) return { action: 'unchanged', name };
  const conflict = conflictNote(project, base);
  if (conflict) return { action: 'confirm', name, reason: conflict };
  return { action: 'send', name, expectedRevision: base.revision };
}

/**
 * Visible note when the project changed while the editor was open (polling never replaces
 * the user's text). null when nothing relevant changed.
 */
export function conflictNote(project, base) {
  if (!project || !base) return null;
  const now = displayName(project);
  if (now !== base.name) return `Renamed elsewhere to “${now}” (revision ${project.revision}). Your text is kept; Save replaces it.`;
  if (project.revision !== base.revision) return `The project changed to revision ${project.revision} while you were editing.`;
  return null;
}
