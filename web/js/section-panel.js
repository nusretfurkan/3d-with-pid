// Per-pane pipe-section panel of a 3D pane's Move tool, shown instead of the equipment Move panel
// while one line is selected. It lists the line's straight sections of the current route (so
// short, end or hidden sections are reachable without a pick), shows the selected one, and edits
// it with one perpendicular world axis and a typed offset in metres. Typing drives the same
// begin / preview / cancel path as the gizmo drag (app.beginPipeMove ...); the preview is
// tentative and nothing is sent until Apply (or Enter). Apply sends exactly one
// move_route_segment through the coordinator; the service derives the committed route.

import { h } from './dom.js';
import { parseMetres } from './move-coordinator.js';
import { MAX_OFFSET_M, checkOffset, offsetVector, routeOf, straightSections } from './pipe-sections.js';
import { offsetText, pipeCapable } from './pipe-move-coordinator.js';

const PHASE_CAPTION = {
  preview: 'Tentative preview · not saved',
  submitting: 'Submitted · sending',
  accepted: 'Accepted',
  uncertain: 'Submitted · unconfirmed',
};
const COMPACT_BELOW_PX = 300;
const SENT_OR_DONE = new Set(['accepted', 'no-op', 'uncertain', 'unconfirmed', 'confirmed-late']);
const USER_CANCEL = new Set(['escape', 'cancel-button', 'move-disabled', 'no-op', 'selection-changed']);
const WARN_KINDS = new Set(['rejected', 'not-sent', 'build-failed', 'build-cancelled', 'superseded', 'session-changed', 'uncertain', 'unconfirmed', 'confirmed-late']);
const fmt = (v) => String(Number(v.toFixed(3)));

/** The one selected line of the shared selection, or null. */
export function selectedLine(project, ids) {
  if (!project || !Array.isArray(ids) || ids.length !== 1) return null;
  return Object.prototype.hasOwnProperty.call(project.model.connections, ids[0]) ? ids[0] : null;
}

export function createSectionPanel({ app, pane, host, viewer: getViewer }) {
  let enabled = false;
  let draft = null;     // {key, axis, text}: typed offset until sent, cancelled or another section is shown
  let problem = null;
  let wasMine = false;
  let statusKey = null;
  let listKey = null;
  let axis = null;      // chosen world axis (0|1|2) for the shown section

  const title = h('b', { class: 'mp-title' }, 'Section');
  const tag = h('span', { class: 'mp-tag' });
  const short = h('span', { class: 'mp-short' });
  const caption = h('div', { class: 'mp-caption' });
  const list = h('div', { class: 'sp-list', role: 'listbox', 'aria-label': `Pane ${pane.index + 1} straight sections of the selected line` });
  const axisSeg = h('div', { class: 'seg sp-axis', role: 'radiogroup', 'aria-label': 'World axis of the section move' },
    ['X', 'Y', 'Z'].map((a, k) => h('button', { type: 'button', role: 'radio', class: `ax-${a.toLowerCase()}`, dataset: { axis: String(k) }, title: `Move across the section along world ${a}` }, a)));
  const input = h('input', {
    type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', class: 'sp-offset',
    'aria-label': 'Offset of the selected section along the chosen axis (metres)',
  });
  const apply = h('button', { type: 'button', class: 'btn small primary', dataset: { act: 'apply-section' }, title: 'Apply one undoable section move (Enter)' }, 'Apply', h('span', { class: 'sp-long' }, ' section move'));
  const cancel = h('button', { type: 'button', class: 'btn small', dataset: { act: 'cancel-section' }, title: 'Cancel the preview; nothing is sent (Esc)' }, 'Cancel');
  const dismiss = h('button', {
    type: 'button', class: 'btn small', dataset: { act: 'dismiss-section' }, hidden: true,
    title: 'Hide the preview of this unconfirmed move. This does not cancel or undo it: the service may still apply it. Nothing is resent.',
  }, 'Hide preview');
  const status = h('div', { class: 'mp-status', role: 'status', 'aria-live': 'polite' });
  const el = h('div', { class: 'move-panel section-panel', role: 'group', 'aria-label': `Pane ${pane.index + 1} pipe section move`, hidden: true },
    h('div', { class: 'mp-head' }, title, tag, short),
    caption,
    list,
    h('div', { class: 'sp-fields' }, axisSeg, h('label', { class: 'sp-f' }, h('span', { class: 'sp-long' }, 'Offset'), input), h('span', { class: 'mp-unit' }, 'm')),
    h('div', { class: 'mp-actions' }, apply, cancel, dismiss),
    status);

  // ---------------------------------------------------------------- reading the situation

  function record() {
    const r = app.pipeSectionMove;
    return r && r.paneIndex === pane.index ? r : null;
  }

  function eligible() {
    const viewer = getViewer();
    return viewer ? viewer.pipeSnapshot().eligible : null;
  }

  /** {line, sections, section, record, routeStatus}: what the panel describes now. */
  function situation() {
    const s = app.state;
    const mine = record();
    const line = mine ? mine.connectionId : s ? selectedLine(s.project, app.selectionIds) : null;
    const route = s && line ? routeOf(s.project, line) : null;
    const sections = s && line ? straightSections(s.project, line) : [];
    const sel = app.section && app.section.connectionId === line ? app.section : null;
    const index = mine ? mine.segmentIndex : sel ? sel.segmentIndex : null;
    return {
      line, sections, index, section: index === null ? null : sections.find((x) => x.index === index) || null,
      record: mine, routeStatus: route ? route.route.status : null,
    };
  }

  /** Visible while Move is on and one line is selected (or this pane holds a section edit). */
  function wanted() {
    if (!enabled) return false;
    const sit = situation();
    return !!sit.line;
  }

  // ---------------------------------------------------------------- rendering

  function renderList(sit) {
    const key = JSON.stringify([sit.line, sit.index, sit.sections.map((x) => [x.axis, x.length_m])]);
    if (key === listKey) return;
    listKey = key;
    const last = sit.sections.length - 1;
    list.replaceChildren(...sit.sections.map((x) => {
      const end = x.index === 0 || x.index === last;
      const b = h('button', {
        type: 'button', role: 'option', class: 'sp-item', dataset: { index: String(x.index) }, 'aria-selected': String(x.index === sit.index),
        title: `Section ${x.index + 1} of ${sit.sections.length}: along ${x.axis === null ? 'no world axis' : 'XYZ'[x.axis]}, straight ${fmt(x.length_m)} m${end ? ' · ends at a nozzle' : ''}`,
      }, h('span', { class: 'sp-n' }, String(x.index + 1)), h('span', { class: `sp-ax ax-${x.axis === null ? 'none' : 'xyz'[x.axis]}` }, x.axis === null ? '·' : 'XYZ'[x.axis]),
      h('span', { class: 'sp-len' }, `${fmt(x.length_m)}`), end ? h('span', { class: 'sp-end', 'aria-label': 'end section' }, '⊣') : null);
      b.classList.toggle('on', x.index === sit.index);
      return b;
    }));
  }

  function render() {
    const show = wanted();
    el.hidden = !show;
    if (!show) return;
    const s = app.state;
    const mine = record();
    const outcome = app.pipeMoves.outcome();
    if (mine && (mine.phase === 'accepted' || mine.phase === 'uncertain')) draft = null;
    if (wasMine && !mine) {
      if (!outcome || SENT_OR_DONE.has(outcome.kind) || (outcome.kind === 'cancelled' && USER_CANCEL.has(outcome.reason))) draft = null;
      problem = null;
    }
    wasMine = !!mine;
    const sit = situation();
    const sectionKey = sit.section ? `${sit.line}|${sit.index}` : null;
    if (draft && draft.key !== sectionKey) draft = null;
    const moveAxes = sit.section ? sit.section.move_axes : [];
    if (mine) {
      const k = mine.offset.findIndex((v) => v !== 0);
      if (k >= 0) axis = k;
      else if (!moveAxes.includes(axis)) axis = mine.axis === 2 ? 0 : 2; // a vertical leg moves along X first, others along Z
    }
    if (!moveAxes.includes(axis)) axis = moveAxes.includes(2) ? 2 : moveAxes[0] ?? null;
    const conn = s && sit.line ? s.project.model.connections[sit.line] : null;
    tag.textContent = conn ? conn.tag : sit.line || '—';
    tag.title = sit.line || '';
    const e = eligible();
    const phase = mine ? mine.phase : null;
    const editable = !!sit.section && !!e && e.ok && (!mine || mine.phase === 'preview');
    el.dataset.phase = phase || (editable ? 'ready' : 'idle');
    caption.textContent = phase
      ? `${PHASE_CAPTION[phase]} · section ${mine.segmentIndex + 1} · base r${mine.sourceRevision}`
      : sit.section
        ? `Section ${sit.section.index + 1} of ${sit.sections.length} · along ${'XYZ'[sit.section.axis] ?? '—'} · ${fmt(sit.section.length_m)} m straight`
        : sit.sections.length ? 'Pick a straight run of the pipe, or choose a section below' : 'No straight sections';
    short.textContent = phase ? { preview: 'Preview', submitting: 'Sending', accepted: 'Accepted', uncertain: 'Unconfirmed' }[phase] : sit.section ? `§${sit.section.index + 1}` : '';
    short.title = caption.textContent;
    el.classList.toggle('compact', !!host && host.clientHeight > 0 && host.clientHeight < COMPACT_BELOW_PX);
    renderList(sit);
    for (const b of list.children) b.disabled = !!mine && mine.phase !== 'preview';
    for (const b of axisSeg.children) {
      const k = Number(b.dataset.axis);
      const on = k === axis;
      b.hidden = !moveAxes.includes(k);
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
      b.disabled = !editable;
    }
    const recordText = mine ? (() => { const k = mine.offset.findIndex((v) => v !== 0); return k >= 0 ? String(mine.offset[k]) : ''; })() : '';
    if (editable && draft) { if (input.value !== draft.text) input.value = draft.text; }
    else if (mine) { if (document.activeElement !== input || !editable) input.value = recordText; }
    else if (!draft) input.value = '';
    input.disabled = !editable;
    const read = readOffset();
    const bad = editable && draft && !read.ok;
    if (bad) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
    if (bad) problem = { kind: 'entry', message: read.message };
    else if (problem && problem.kind === 'entry') problem = null;
    const previewProblem = mine && mine.problem ? mine.problem.message : null;
    apply.disabled = !editable || !read.ok || !!previewProblem || app.online !== true;
    cancel.disabled = !(mine && mine.phase === 'preview');
    dismiss.hidden = !(mine && mine.phase === 'uncertain');

    const lines = [];
    if (problem) lines.push(['err', problem.message]);
    const own = outcome && outcome.paneIndex === pane.index ? outcome : null;
    if (mine && mine.phase === 'preview') {
      if (previewProblem) lines.push(['err', `Not possible: ${previewProblem}`]);
      else if (mine.points) lines.push(['', `${offsetText(mine.offset)} · tentative until the service validates it · Enter or Apply sends one move · Esc cancels`]);
      else lines.push(['', 'Drag the axis handle or type an offset · Esc cancels']);
      lines.push(['muted', 'Both nozzle ends stay fixed; the neighbouring runs follow. Clearance is checked by the service.']);
    } else if (mine && own) {
      lines.push([WARN_KINDS.has(own.kind) ? 'warn' : '', own.text]);
    } else if (app.pipeSectionMove && !mine) {
      const other = app.pipeSectionMove;
      lines.push(['', `A section move is in progress in pane ${other.paneIndex + 1} (${other.phase}); one edit at a time.`]);
    } else {
      if (own && own.kind !== 'submitting') lines.push([WARN_KINDS.has(own.kind) ? 'warn' : '', own.text]);
      for (const u of app.pipeMoves.unresolved().filter((x) => x.paneIndex === pane.index)) {
        lines.push(['warn', `Unconfirmed: the hidden move of ${u.tag} (prepared for r${u.baseRevision}) may still be applied; it is watched and not resent.`]);
      }
      if (sit.routeStatus && sit.routeStatus !== 'current') lines.push(['warn', 'This route is not current; reroute it before moving a section.']);
      else if (!sit.section && sit.sections.length) lines.push(['muted', 'A bend, flange or bolting is not a straight section.']);
      else if (editable) lines.push(['', `Drag the ${moveAxes.map((k) => 'XYZ'[k]).join('/')} handle or type an offset (±${MAX_OFFSET_M} m) · snap ${pane.moveSnap ? `${pane.moveSnap} m` : 'off'} (drag only)`]);
      else if (e && e.message) lines.push(['muted', e.message]);
      else if (s && !pipeCapable(s)) lines.push(['muted', 'This workspace service does not offer section moves yet; reload once the updated service is running.']);
    }
    const shown = lines.filter(([, text]) => text);
    const key = JSON.stringify(shown);
    if (key !== statusKey) {
      statusKey = key;
      status.replaceChildren(...shown.map(([tone, text]) => h('div', { class: tone ? `mp-line ${tone}` : 'mp-line', title: text }, text)));
    }
  }

  let compact = null;
  const observer = host && typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
    const next = host.clientHeight > 0 && host.clientHeight < COMPACT_BELOW_PX;
    if (next === compact) return;
    compact = next;
    render();
  }) : null;
  if (observer) observer.observe(host);

  // ---------------------------------------------------------------- actions

  /** The typed offset as a vector on the chosen axis: {ok, offset, message}. */
  function readOffset() {
    const sit = situation();
    if (!sit.section || axis === null) return { ok: false, offset: null, message: 'Select a straight section first.' };
    const parsed = parseMetres(input.value);
    if (!parsed.ok) return { ok: false, offset: null, message: parsed.message };
    const offset = offsetVector(axis, parsed.value);
    const check = checkOffset(sit.section, offset);
    return check.ok ? { ok: true, offset, message: null } : { ok: false, offset, message: check.message };
  }

  function refusalText() {
    const r = app.pipeMoves.refusal();
    return r ? `Not started: ${r.message}` : 'Not started.';
  }

  function intent() {
    const e = eligible();
    return e && e.ok ? e.intent : null;
  }

  /** Begin (if needed) and preview the typed offset. */
  function previewTyped() {
    const sit = situation();
    const read = readOffset();
    if (!read.ok) {
      if (record()) app.previewPipeMove([0, 0, 0], pane.index);
      return;
    }
    if (!record()) {
      const i = intent();
      if (!i || !app.beginPipeMove(pane.index, i, { origin: 'numeric' })) {
        problem = { kind: 'refused', message: refusalText() };
        return;
      }
    }
    app.previewPipeMove(read.offset, pane.index);
    if (!sit.section) problem = null;
  }

  function typedNow() {
    problem = null;
    const sit = situation();
    if (!sit.section) return render();
    draft = { key: `${sit.line}|${sit.index}`, axis, text: input.value };
    previewTyped();
    render();
  }

  async function applyNow() {
    problem = null;
    const sit = situation();
    if (!sit.section) return render();
    const read = readOffset();
    if (!read.ok) {
      problem = { kind: 'entry', message: read.message };
      input.focus();
      return render();
    }
    draft = { key: `${sit.line}|${sit.index}`, axis, text: input.value };
    previewTyped();
    if (!record()) return render();
    render();
    const result = await app.commitPipeMove(pane.index);
    if (result && (result.reason === 'invalid-offset' || result.reason === 'preview-refused')) problem = { kind: 'refused', message: result.message };
    render();
  }

  input.addEventListener('input', typedNow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      applyNow();
    }
  });
  axisSeg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-axis]');
    if (!b || b.disabled) return;
    axis = Number(b.dataset.axis);
    if (input.value.trim()) typedNow();
    else render();
  });
  list.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-index]');
    if (!b || b.disabled) return;
    const sit = situation();
    app.selectSection(sit.line, Number(b.dataset.index));
  });
  apply.addEventListener('click', applyNow);
  cancel.addEventListener('click', () => {
    app.cancelPipeMove('cancel-button', pane.index);
    draft = null;
    problem = null;
    render();
  });
  dismiss.addEventListener('click', () => {
    app.dismissPipeMove(pane.index);
    render();
  });

  return {
    el,
    render,
    setEnabled(on) {
      enabled = !!on;
      if (!enabled) {
        draft = null;
        problem = null;
      }
      render();
    },
    /** Whether the panel takes the Move slot now (the equipment panel steps aside). */
    get shown() { return !el.hidden; },
    contains: (node) => !!node && el.contains(node),
    dispose() { if (observer) observer.disconnect(); },
    snapshot() {
      const sit = situation();
      return {
        hidden: el.hidden, phase: el.dataset.phase || null, tag: tag.textContent, caption: caption.textContent,
        line: sit.line, selected: sit.index, axis, value: input.value, invalid: input.getAttribute('aria-invalid') === 'true',
        sections: [...list.children].map((b) => ({ index: Number(b.dataset.index), text: b.textContent, selected: b.getAttribute('aria-selected') === 'true', disabled: b.disabled })),
        axes: [...axisSeg.children].filter((b) => !b.hidden).map((b) => 'XYZ'[Number(b.dataset.axis)]),
        apply: !apply.disabled, cancel: !cancel.disabled, dismiss: !dismiss.hidden, compact: el.classList.contains('compact'),
        status: [...status.children].map((c) => c.textContent),
      };
    },
  };
}
