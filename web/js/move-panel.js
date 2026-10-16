// Per-pane Move panel of a 3D pane: typed world X/Y/Z of the selected equipment origin, the
// pane's snap choice, and Apply / Cancel. Typing drives the same begin / preview / cancel /
// commit path as a gizmo drag (app.beginEquipmentMove ...), so the preview, its mirrors in
// compatible Live panes and the single command are identical. Typed values are used exactly
// (no rounding, snapping or clamping); an entry that is not a number, or is beyond the drag
// limit, is refused with its reason and previews nothing. Every origin shown names whether it
// is a preview, the current model's value or the value of an older view. Typed values are kept
// until they are sent or explicitly cancelled, also when a check before sending refuses them.
// An unconfirmed move (no answer from the service) offers Hide preview, which cancels nothing.

import { h, icon } from './dom.js';
import { isNoOp, placementOf } from './equipment-move.js';
import { pendingIncidentRoutes, readTyped } from './move-coordinator.js';

const SNAP_CHOICES = [[0.05, '0.05 m'], [null, 'Off']];
const PHASE_CAPTION = {
  preview: 'Preview origin · not saved',
  submitting: 'Submitted origin · sending',
  accepted: 'Accepted origin',
  uncertain: 'Submitted origin · unconfirmed',
};
const COMPACT_BELOW_PX = 300; // a 3D pane body this short gets the one-line head and one status line
/** Ends of a typed preview after which the typed values are no longer wanted (sent, or nothing to send). */
const SENT_OR_DONE = new Set(['accepted', 'no-op', 'uncertain', 'unconfirmed', 'confirmed-late']);
const USER_CANCEL = new Set(['escape', 'cancel-button', 'move-disabled', 'no-op']);
const WARN_KINDS = new Set(['rejected', 'not-sent', 'build-failed', 'build-cancelled', 'superseded', 'session-changed', 'uncertain', 'unconfirmed', 'confirmed-late']);

/** Exact text of a coordinate: the shortest form that reads back as the same number. */
function exact(v) {
  return typeof v === 'number' && Number.isFinite(v) ? String(Object.is(v, -0) ? 0 : v) : '';
}

function signed(v) {
  const s = Number(v.toFixed(6));
  return `${s > 0 ? '+' : ''}${s}`;
}

export function createMovePanel({ app, pane, host, viewer: getViewer }) {
  let enabled = false;
  let draft = null;         // {entityId, texts}: what the user typed, until sent, cancelled or another item is shown
  let problem = null;       // {message} of the typed entry
  let notice = null;        // {tone, text} of the last local action
  let wasMine = false;
  let statusKey = null;     // text of the status lines on screen
  const label = (axis) => `${axis} world coordinate of the equipment origin (metres)`;
  const inputs = ['X', 'Y', 'Z'].map((axis) => h('input', {
    type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', 'aria-label': label(axis),
    dataset: { axis: axis.toLowerCase() },
  }));
  const snapButtons = SNAP_CHOICES.map(([value, text]) => h('button', {
    type: 'button', role: 'radio', dataset: { snap: value === null ? 'off' : String(value) },
    title: value === null ? 'Drag without snapping' : `Drag in steps of ${text} from the current origin`,
  }, text));
  const snapSeg = h('div', { class: 'seg mp-snap', role: 'radiogroup', 'aria-label': `Pane ${pane.index + 1} drag snap` }, snapButtons);
  const title = h('b', { class: 'mp-title' }, 'Move');
  const tag = h('span', { class: 'mp-tag' });
  const short = h('span', { class: 'mp-short' }); // the caption in one word or two, for short panes
  const caption = h('div', { class: 'mp-caption' });
  const apply = h('button', { type: 'button', class: 'btn small primary', dataset: { act: 'apply' }, title: 'Send one move_equipment command for this origin (Enter)' }, 'Apply move');
  const cancel = h('button', { type: 'button', class: 'btn small', dataset: { act: 'cancel' }, title: 'Cancel the preview; nothing is sent (Esc)' }, 'Cancel');
  const dismiss = h('button', {
    type: 'button', class: 'btn small', dataset: { act: 'dismiss' }, hidden: true,
    title: 'Hide the preview of this unconfirmed move. This does not cancel or undo it: the service may still apply it, and you will be told if it does. Nothing is resent.',
  }, 'Hide preview');
  const reroute = h('button', { type: 'button', class: 'btn small ghost', dataset: { act: 'reroute' }, hidden: true, title: 'Open the assistant\'s Change section: pending routes, Reroute, Undo and Redo' }, icon('route'), 'Reroute…');
  const status = h('div', { class: 'mp-status', role: 'status', 'aria-live': 'polite' });
  const el = h('div', { class: 'move-panel', role: 'group', 'aria-label': `Pane ${pane.index + 1} equipment move`, hidden: true },
    h('div', { class: 'mp-head' }, title, tag, short, snapSeg),
    caption,
    h('div', { class: 'mp-fields' }, inputs.map((input, i) => h('label', { class: `mp-f ${'xyz'[i]}` }, h('span', null, 'XYZ'[i]), input)), h('span', { class: 'mp-unit' }, 'm')),
    h('div', { class: 'mp-actions' }, apply, cancel, dismiss, reroute),
    status);

  // ---------------------------------------------------------------- reading the situation

  function record() {
    const r = app.equipmentMove;
    return r && r.paneIndex === pane.index ? r : null;
  }

  function eligible() {
    const viewer = getViewer();
    return viewer ? viewer.moveSnapshot().eligible : null;
  }

  /** What the fields describe now: {base, shown, editable, entityId, caption, brief, phase}. */
  function situation() {
    const s = app.state;
    const mine = record();
    const viewer = getViewer();
    if (mine) {
      const caption = mine.phase === 'accepted' && s
        ? `${PHASE_CAPTION.accepted} · model r${s.project.revision}; this view shows r${mine.sourceRevision} until the regenerated scene arrives`
        : `${PHASE_CAPTION[mine.phase] || mine.phase} · base r${mine.sourceRevision}`;
      const brief = { preview: 'Preview · not saved', submitting: 'Sending', accepted: `Accepted · view r${mine.sourceRevision}`, uncertain: 'Unconfirmed' }[mine.phase];
      return { base: mine.basePosition, shown: mine.position, editable: mine.phase === 'preview', entityId: mine.entityId, caption, brief, phase: mine.phase };
    }
    const e = eligible();
    if (e && e.ok) {
      return { base: e.base, shown: e.base, editable: !app.equipmentMove, entityId: e.entityId, caption: `Current origin · model r${e.sourceRevision} · matches this view`, brief: `Current · r${e.sourceRevision}`, phase: null };
    }
    const entityId = e ? e.entityId : null;
    const placement = s && entityId ? placementOf(s.project, entityId) : null;
    if (placement && placement.state === 'placed') {
      const shownRev = viewer && viewer.source ? viewer.source.source_revision : null;
      const older = shownRev !== null && shownRev !== s.project.revision;
      return {
        base: null, shown: placement.position, editable: false, entityId, phase: null,
        caption: `Current origin in model r${s.project.revision}${older ? ` · this view shows r${shownRev}` : ''}`,
        brief: `Current r${s.project.revision}${older ? ` · view r${shownRev}` : ''}`,
      };
    }
    const unplaced = !!placement && placement.state === 'unplaced';
    return { base: null, shown: null, editable: false, entityId, caption: unplaced ? 'Unplaced in the current model' : 'No equipment origin', brief: unplaced ? 'Unplaced' : '', phase: null };
  }

  function typed(base) {
    return readTyped(inputs.map((i) => i.value), base);
  }

  // ---------------------------------------------------------------- rendering

  function setTexts(texts) {
    inputs.forEach((input, i) => {
      if (input.value !== texts[i]) input.value = texts[i];
    });
  }

  function setFields(values) {
    setTexts(inputs.map((_, i) => (values ? exact(values[i]) : '')));
  }

  function mark(fields) {
    inputs.forEach((input, i) => {
      const bad = !!(fields && fields[i] && !fields[i].ok);
      if (bad) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
      input.title = bad ? fields[i].message : '';
    });
  }

  function render() {
    el.hidden = !enabled;
    if (!enabled) return;
    const viewer = getViewer();
    const mine = record();
    const outcome = app.moves.outcome();
    if (mine && (mine.phase === 'accepted' || mine.phase === 'uncertain')) draft = null; // sent: the record shows it now
    if (wasMine && !mine) { // the preview ended; typed values stay unless they were sent or explicitly cancelled
      if (!outcome || SENT_OR_DONE.has(outcome.kind) || (outcome.kind === 'cancelled' && USER_CANCEL.has(outcome.reason))) draft = null;
      problem = null;
    }
    wasMine = !!mine;
    const sit = situation();
    if (draft && draft.entityId !== sit.entityId) draft = null; // the panel now shows another item
    const ownOutcome = outcome && outcome.paneIndex === pane.index ? outcome : null;
    el.dataset.phase = sit.phase || (sit.editable ? 'ready' : 'idle');
    const s = app.state;
    const entity = s && sit.entityId && s.project.model.entities[sit.entityId];
    tag.textContent = entity ? entity.tag : sit.entityId || '—';
    tag.title = sit.entityId || '';
    caption.textContent = sit.caption;
    short.textContent = sit.brief || '';
    short.title = sit.caption;
    el.classList.toggle('compact', !!host && host.clientHeight > 0 && host.clientHeight < COMPACT_BELOW_PX);
    if (sit.editable && draft) setTexts(draft.texts);
    else {
      setFields(sit.shown);
      if (!sit.editable) mark(null);
      if (!sit.editable && !draft) problem = null;
    }
    for (const input of inputs) {
      input.disabled = !sit.editable && !sit.phase;
      input.readOnly = !!sit.phase && !sit.editable;
    }
    for (const b of snapButtons) {
      const on = (b.dataset.snap === 'off' ? null : Number(b.dataset.snap)) === (pane.moveSnap ?? null);
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    }
    const check = sit.editable ? typed(sit.base) : null;
    if (sit.editable && draft) { // entry problems follow the fields; a refusal stays until the next action
      mark(check.fields);
      if (!check.ok) problem = { kind: 'entry', message: check.message };
      else if (problem && problem.kind === 'entry') problem = null;
    }
    const unchanged = !!(check && check.ok && isNoOp(sit.base, check.position));
    apply.disabled = !sit.editable || !check || !check.ok || unchanged || app.online !== true;
    apply.title = unchanged ? 'This is the current origin; type a new X, Y or Z first' : 'Send one move_equipment command for this origin (Enter)';
    cancel.disabled = !(mine && mine.phase === 'preview');
    dismiss.hidden = !(mine && mine.phase === 'uncertain');
    const routes = pendingIncidentRoutes(app.state, sit.entityId);
    reroute.hidden = !routes;
    reroute.lastChild.textContent = routes ? `Reroute ${routes}…` : 'Reroute…';

    const lines = [];
    if (problem) lines.push(['err', problem.message]);
    if (mine && mine.phase === 'preview') {
      const d = mine.position.map((v, i) => v - mine.basePosition[i]);
      lines.push(['', `Δ ${d.map(signed).join(', ')} m · Enter or Apply sends one move · Esc cancels`]);
    } else if (mine && ownOutcome) {
      lines.push([WARN_KINDS.has(ownOutcome.kind) ? 'warn' : '', ownOutcome.text]);
    } else if (app.equipmentMove && !mine) {
      const other = app.equipmentMove;
      lines.push(['', other.phase === 'uncertain'
        ? `An unconfirmed move in pane ${other.paneIndex + 1} waits for the service; hide its preview there to start another.`
        : `A move is in progress in pane ${other.paneIndex + 1} (${other.phase}); one move at a time.`]);
    } else {
      if (ownOutcome && ownOutcome.kind !== 'submitting') lines.push([WARN_KINDS.has(ownOutcome.kind) ? 'warn' : '', ownOutcome.text]);
      if (!ownOutcome || ownOutcome.kind !== 'unconfirmed') {
        for (const u of app.moves.unresolved().filter((x) => x.paneIndex === pane.index)) {
          lines.push(['warn', `Unconfirmed: the hidden move of ${u.tag} (prepared for r${u.baseRevision}) may still be applied; it is watched and not resent.`]);
        }
      }
      if (notice) lines.push([notice.tone, notice.text]);
      const e = eligible();
      if (sit.editable) lines.push(['', `Type X, Y, Z or drag an axis/plane handle · snap ${pane.moveSnap ? `${pane.moveSnap} m` : 'off'} (drag only)`]);
      else if (draft) lines.push(['muted', `Your typed X, Y, Z are kept; they return when Move is available again. ${e && e.message ? e.message : ''}`.trim()]);
      else if (e && e.message) lines.push(['muted', e.message]);
      else if (!viewer) lines.push(['muted', 'The 3D view is starting…']);
    }
    // most important first: a short pane shows only the first line (its full text in the tooltip). The
    // live region is rebuilt only when its text changes, so polling does not repeat announcements.
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
    if (enabled) render();
  }) : null;
  if (observer) observer.observe(host);

  // ---------------------------------------------------------------- actions

  function refusalText() {
    const r = app.moves.refusal();
    return r ? `Not started: ${r.message}` : 'Not started.';
  }

  /** Typing previews through the same record as a drag (begun on the first changed valid entry). */
  function typedNow() {
    notice = null;
    const sit = situation();
    if (!sit.editable) return render();
    draft = { entityId: sit.entityId, texts: inputs.map((i) => i.value) };
    const read = typed(sit.base);
    mark(read.fields);
    problem = read.ok ? null : { kind: 'entry', message: read.message };
    if (read.ok) {
      const mine = record();
      if (mine) app.previewEquipmentMove(read.position, pane.index);
      else if (!isNoOp(sit.base, read.position)) {
        const e = eligible();
        if (e && e.ok && app.beginEquipmentMove(pane.index, e.intent, { origin: 'numeric' })) app.previewEquipmentMove(read.position, pane.index);
        else problem = { kind: 'refused', message: refusalText() };
      }
    }
    render();
  }

  /** Apply: begin if needed, then commit the typed origin; refused entries and failed checks keep the typing. */
  function applyNow() {
    notice = null;
    const sit = situation();
    if (!sit.editable) return render();
    const read = typed(sit.base);
    mark(read.fields);
    if (!read.ok) {
      problem = { kind: 'entry', message: read.message };
      const bad = read.fields.findIndex((f) => !f.ok);
      if (bad >= 0) inputs[bad].focus();
      return render();
    }
    problem = null;
    draft = { entityId: sit.entityId, texts: inputs.map((i) => i.value) };
    if (!record()) {
      if (isNoOp(sit.base, read.position)) {
        draft = null;
        notice = { tone: '', text: 'Unchanged: this is the current origin; nothing was sent.' };
        return render();
      }
      const e = eligible();
      if (!e || !e.ok || !app.beginEquipmentMove(pane.index, e.intent, { origin: 'numeric' })) {
        problem = { kind: 'refused', message: refusalText() };
        return render();
      }
    }
    app.commitEquipmentMove(read.position, pane.index);
    render();
  }

  for (const input of inputs) {
    input.addEventListener('input', typedNow);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        applyNow();
      }
    });
  }
  apply.addEventListener('click', applyNow);
  cancel.addEventListener('click', () => {
    app.cancelEquipmentMove('cancel-button', pane.index);
    draft = null;
    problem = null;
    render();
  });
  dismiss.addEventListener('click', () => {
    app.dismissEquipmentMove(pane.index);
    render();
  });
  reroute.addEventListener('click', () => app.assistant.open('change', reroute));
  snapSeg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-snap]');
    if (!b) return;
    pane.moveSnap = b.dataset.snap === 'off' ? null : Number(b.dataset.snap);
    const viewer = getViewer();
    if (viewer) viewer.setMoveSnap(pane.moveSnap);
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
        notice = null;
      }
      render();
    },
    get enabled() { return enabled; },
    contains: (node) => !!node && el.contains(node),
    /** Release the size observer (the pane's 3D content is going away). */
    dispose() { if (observer) observer.disconnect(); },
    /** Read-only facts for automated checks. */
    snapshot() {
      return {
        enabled, hidden: el.hidden, phase: el.dataset.phase || null, tag: tag.textContent, caption: caption.textContent,
        brief: short.textContent, compact: el.classList.contains('compact'),
        values: inputs.map((i) => i.value), invalid: inputs.map((i) => i.getAttribute('aria-invalid') === 'true'),
        disabled: inputs.map((i) => i.disabled), readOnly: inputs.map((i) => i.readOnly), dirty: !!draft, draft: draft ? draft.texts.slice() : null,
        apply: !apply.disabled, cancel: !cancel.disabled, dismiss: !dismiss.hidden, reroute: !reroute.hidden,
        snap: pane.moveSnap ?? null, status: [...status.children].map((c) => c.textContent),
      };
    },
  };
}
