// World-coordinate overlay of the 3D view (pure; unit-tested in Node).
//
// World axes are the engineering axes: metres, right-handed, Z up. The reference grid lies in
// the world XY plane at Z = 0. A selected equipment item's readout is its authoritative
// placement origin from the current model (not a mesh centre, nozzle frame or camera point),
// and always names that source; when the view shows another revision the readout says so
// rather than presenting current values as coordinates of an older mesh. Unplaced items read
// "Unplaced" (never zeroes). A move in progress is labelled Preview until it is saved. A selected
// line has no equipment origin: with a straight section selected the readout shows that section's
// midpoint from the current route (or, during a section move, the tentative midpoint and offset).

import { MOVE_REASONS, contextDigest, placementOf } from './equipment-move.js';

export const AXIS_LETTERS = Object.freeze(['X', 'Y', 'Z']);
const GRID_STEPS = [[0.25, 1], [1, 5], [5, 25], [25, 100]]; // [minor, major] metres
const MAX_LINES = 240; // per direction, minor lines included
const PHASE_TEXT = {
  preview: 'Not saved',
  submitting: 'Submitting the move…',
  accepted: 'Accepted · waiting for regenerated geometry',
  uncertain: 'Outcome uncertain · checking the service',
};
const SECTION_PHASE_TEXT = {
  preview: 'Tentative · not saved; the service validates it on Apply',
  submitting: 'Submitting the section move…',
  accepted: 'Accepted · waiting for regenerated geometry',
  uncertain: 'Outcome uncertain · checking the service',
};

export function formatMetres(v, digits = 3) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  const s = v.toFixed(digits);
  return /^-0\.?0*$/.test(s) ? s.slice(1) : s;
}

export function formatPoint(p, digits = 3) {
  return Array.isArray(p) ? p.map((v) => formatMetres(v, digits)).join(', ') : '—';
}

function signed(v) {
  const s = formatMetres(v);
  return s.startsWith('-') || s === formatMetres(0) ? s : `+${s}`;
}

/**
 * Reference grid for content bounds {min, max} (null: around the origin): whole-metre extent
 * that always contains the world origin, a margin of one major step, and spacing chosen so a
 * large plant stays a restrained grid. {min: [x, y], max: [x, y], minor, major, zTop}.
 */
export function gridExtent(bounds) {
  const lo = bounds && Array.isArray(bounds.min) ? bounds.min : [-1, -1, 0];
  const hi = bounds && Array.isArray(bounds.max) ? bounds.max : [1, 1, 1];
  const ok = [...lo, ...hi].every((v) => typeof v === 'number' && Number.isFinite(v));
  const min = ok ? [Math.min(lo[0], 0), Math.min(lo[1], 0)] : [-1, -1];
  const max = ok ? [Math.max(hi[0], 0), Math.max(hi[1], 0)] : [1, 1];
  const span = Math.max(max[0] - min[0], max[1] - min[1]);
  const [minor, major] = GRID_STEPS.find(([m, M]) => (span + 2 * M) / m <= MAX_LINES) || GRID_STEPS[GRID_STEPS.length - 1];
  const out = {
    min: min.map((v) => Math.floor(v / major) * major - major),
    max: max.map((v) => Math.ceil(v / major) * major + major),
    minor, major,
    zTop: Math.max(1, Math.ceil(ok ? hi[2] : 1)),
  };
  return out;
}

/** Grid line coordinates along one axis: {minor: [...], major: [...]} (major excludes the 0 axis line). */
export function gridLines(lo, hi, minor, major) {
  const out = { minor: [], major: [] };
  const n = Math.round((hi - lo) / minor);
  for (let i = 0; i <= n; i += 1) {
    const v = Math.round((lo + i * minor) * 1e6) / 1e6;
    if (Math.abs(v) < 1e-9) continue; // the world axis is drawn separately
    const onMajor = Math.abs(v / major - Math.round(v / major)) < 1e-6;
    (onMajor ? out.major : out.minor).push(v);
  }
  return out;
}

/**
 * Readout of the coordinate overlay: {kind, entityId, tag, title, xyz, lines}.
 * kind: 'preview' | 'origin' | 'unplaced' | 'missing' | 'connection' | 'multiple' | 'none' | 'no-context'.
 * context: {project (revision, model, physical), inputDigest (the current document digest)}; a view
 * matches only when both its revision and digest equal them. viewerSource: the installed scene
 * ({source_revision, input_digest, kind}) or null; owner: selectionOwner(...); preview:
 * effectivePreview(...) or null; move: {enabled, eligible, snap} for the tool line, or null.
 * section (a selected line only): {tag, index, count, axis, length, midpoint, ok, message, moveAxes} of the
 * selected straight section, or with phase/offset/sourceRevision for a section record drawn on this view.
 */
export function coordinateReadout({ context = null, viewerSource = null, owner = null, preview = null, move = null, section = null } = {}) {
  const project = context && context.project;
  const rev = project ? project.revision : null;
  const lines = [];
  const tagOf = (id) => {
    const e = project && project.model && project.model.entities ? project.model.entities[id] : null;
    return e && typeof e.tag === 'string' ? e.tag : id;
  };
  const moveLine = () => {
    if (!move || !move.enabled) return;
    if (move.eligible && move.eligible.ok) {
      lines.push(`Move: drag an axis or plane handle · snap ${move.snap ? `${formatMetres(move.snap, 2)} m` : 'off'}`);
    } else if (move.eligible) {
      lines.push(`Move unavailable: ${move.eligible.message || MOVE_REASONS[move.eligible.reason] || move.eligible.reason}`);
    }
  };
  if (preview) {
    const tag = tagOf(preview.entityId);
    lines.push(`Δ ${preview.delta.map(signed).join(', ')} m from ${formatPoint(preview.base)}`);
    lines.push(`${PHASE_TEXT[preview.phase] || preview.phase} · base r${(preview.sourceRevision ?? (viewerSource ? viewerSource.source_revision : '?'))}`);
    if (preview.origin === 'local' && preview.phase === 'preview') lines.push(`Release to submit · Esc cancels · snap ${move && move.snap ? `${formatMetres(move.snap, 2)} m` : 'off'}`);
    return { kind: 'preview', entityId: preview.entityId, tag, title: `${tag} · Preview origin`, xyz: preview.position.slice(), lines };
  }
  if (!project) {
    lines.push('Model data is not connected to this view.');
    return { kind: 'no-context', entityId: null, tag: null, title: 'World origin', xyz: null, lines };
  }
  const who = owner || { entityId: null, reason: 'none' };
  const finish = (kind, entityId, title, xyz = null) => {
    moveLine();
    return { kind, entityId, tag: entityId ? tagOf(entityId) : null, title, xyz, lines };
  };
  if (who.reason === 'none') {
    lines.push('Select one equipment item to read its origin.');
    return finish('none', null, 'World origin');
  }
  if (who.reason === 'multiple') {
    lines.push('Several items are selected; select one equipment item.');
    return finish('multiple', null, 'World origin');
  }
  if (who.reason === 'connection') return lineReadout(section, move, rev, lines);
  const placement = placementOf(project, who.entityId);
  if (who.reason === 'missing' || placement.state === 'missing' || placement.state === 'unknown') {
    lines.push(`Not in the current model r${rev}.`);
    return finish('missing', who.entityId, `${who.entityId}`);
  }
  if (placement.state === 'connection') {
    lines.push('A pipe or connection has no equipment origin.');
    return finish('connection', null, 'World origin');
  }
  const tag = placement.tag || who.entityId;
  if (placement.state !== 'placed') {
    lines.push(`Unplaced in the current model r${rev}${placement.reason ? `: ${placement.reason}` : ''}.`);
    return finish('unplaced', who.entityId, `${tag} · Unplaced`);
  }
  const digest = contextDigest(context);
  const matches = !!viewerSource && viewerSource.source_revision === rev && digest !== null && viewerSource.input_digest === digest;
  if (matches) lines.push(`Current model r${rev} · matches this view`);
  else if (viewerSource && digest === null) {
    lines.push(`Current model r${rev} · digest unknown, not matched to this view (r${viewerSource.source_revision})`);
  } else if (viewerSource) {
    const shown = viewerSource.source_revision === rev ? `r${rev} with another digest` : `r${viewerSource.source_revision}`;
    lines.push(`Current model r${rev} · this view shows ${shown}`);
  } else lines.push(`Current model r${rev} · no scene installed`);
  const title = matches ? `${tag} · equipment origin` : `${tag} · origin in current model r${rev}`;
  return finish('origin', who.entityId, title, placement.position);
}

/** Readout of a selected line: its selected straight section, a section move in progress, or a hint. */
function lineReadout(section, move, rev, lines) {
  if (!section) {
    lines.push('A line has no equipment origin; pick a straight run of it to read that section.');
    if (move && move.enabled) lines.push('Section move: pick a straight run of the line, or choose a section in the Section list.');
    return { kind: 'connection', entityId: null, tag: null, title: 'World origin', xyz: null, lines };
  }
  const name = `${section.tag} · section ${section.index + 1}${section.count ? ` of ${section.count}` : ''}`;
  if (section.phase) {
    const k = section.offset.findIndex((v) => v !== 0);
    lines.push(k < 0 ? 'No offset yet · drag a handle or type an offset' : `${signed(section.offset[k])} m along ${AXIS_LETTERS[k]} · both nozzle ends fixed`);
    lines.push(`${SECTION_PHASE_TEXT[section.phase] || section.phase} · base r${section.sourceRevision}`);
    return { kind: 'preview', entityId: null, tag: section.tag, title: `${name} · tentative midpoint`, xyz: section.midpoint.map((v, i) => v + section.offset[i]), lines };
  }
  lines.push(`Straight section midpoint · along ${AXIS_LETTERS[section.axis]} · ${formatMetres(section.length)} m straight · current model r${rev}`);
  if (move && move.enabled) {
    lines.push(section.ok
      ? `Section move: drag the ${section.moveAxes.map((a) => AXIS_LETTERS[a]).join('/')} handle or type an offset`
      : `Section move unavailable: ${section.message}`);
  }
  return { kind: 'section', entityId: null, tag: section.tag, title: `${name} · midpoint`, xyz: section.midpoint.slice(), lines };
}
