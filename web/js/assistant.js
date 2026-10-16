// Shared nonmodal assistant: Ask (read-only answers about the current model) and Change (the
// one existing composer, moved here with its controller, draft, proposal and running request).
// It is a floating, nonmodal bubble opened from the draggable Ask launcher (floating-assistant.js):
// it reserves no column, so opening or closing never resizes or remounts a pane, moves a view or
// creates a WebGL context. An outside click or Escape only closes it. The Ask thread, draft and
// running question live for the page session and are cleared only when the project or the
// service changes. Every model-provided string is rendered as text; references
// are stable IDs that select their current counterpart, never links or commands.

import { h, replace, icon } from './dom.js';
import { shortDigest } from './status.js';
import { rerouteOffer } from './reroute-model.js';
import { DRAG_THRESHOLD_PX, isDrag, positionBubble } from './floating-assistant.js';
import {
  ASK_LIMITS, answerFreshness, askFailure, boundTurns, buildAskRequest, currentLabel, displayedSource, historyFrom,
  identityKey, knownIds, looksLikeEdit, newRequestId, preflightAsk, readAnswer, referenceNow, visibleSourceNote,
} from './assistant-model.js';

const MODES = ['ask', 'change'];
const CANCEL_GRACE_MS = 8000;
const STAMP = { answered: 'Answer', clarification: 'Clarification needed', unavailable: 'Model unavailable', cancelled: 'Cancelled' };
const TYPE_NAMES = { '3d': '3D', pid: 'P&ID', legend: 'Legend', index: 'Index', description: 'Description', outputs: 'Outputs' };

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function randomBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

export function createAssistant(app, root, composerRoot) {
  let mode = 'ask';
  let open = false;
  let opener = null;
  let identity = null;
  let turns = [];
  let dropped = 0;
  let pending = null; // {turn, controller, request, cancelTimer, cancelling}
  let context = { clearedKey: null }; // the selection set the user detached from the next question
  let notice = null;
  let seq = 0;
  let placement = null;

  // ---------------------------------------------------------------- structure

  const tabButtons = MODES.map((m) => h('button', {
    type: 'button', role: 'tab', id: `asst-tab-${m}`, 'aria-controls': `asst-${m}`, dataset: { mode: m },
    title: m === 'ask' ? 'Read-only questions about the current model' : 'Quick edit or Engineering plan: reviewable proposals with explicit Apply',
  }, m === 'ask' ? 'Ask' : 'Change', h('i', { class: 'asst-dot', 'aria-hidden': 'true' })));
  const tabs = h('div', { class: 'seg asst-modes', role: 'tablist', 'aria-label': 'Assistant mode' }, tabButtons);
  tabs.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (b) setMode(b.dataset.mode, { focus: true });
  });
  tabs.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    setMode(mode === 'ask' ? 'change' : 'ask');
    tabButtons[MODES.indexOf(mode)].focus();
  });
  const closeButton = h('button', { type: 'button', class: 'tool asst-close', title: 'Close the assistant (Esc). Drafts, answers and running requests are kept.', 'aria-label': 'Close the assistant' }, icon('close'));
  closeButton.addEventListener('click', () => close());
  const head = h('header', { class: 'asst-head' }, tabs, h('span', { class: 'asst-title', id: 'asst-title' }, 'Assistant'), closeButton);

  // Ask section
  const contextRow = h('div', { class: 'asst-context' });
  const sourceRow = h('div', { class: 'asst-source mono' });
  const thread = h('div', { class: 'asst-thread', role: 'log', 'aria-live': 'polite', 'aria-label': 'Questions and answers' });
  const input = h('textarea', {
    rows: 1, id: 'asst-input', spellcheck: 'false', 'aria-label': 'Question about the project',
    placeholder: 'Ask about this project…',
  });
  const askButton = h('button', { type: 'button', class: 'btn primary asst-send', id: 'asst-send', title: 'Ask (Enter). Read-only: nothing in the project changes.', 'aria-label': 'Ask' }, icon('send'));
  const budgetCount = h('span', { class: 'budget-count mono' });
  const budgetNote = h('span', { class: 'budget-note', role: 'status', 'aria-live': 'polite' });
  const budgetRow = h('div', { class: 'budget-row' }, budgetCount, budgetNote);
  const editHint = h('div', { class: 'asst-edit-hint', hidden: true });
  const askNote = h('p', { class: 'limitation asst-note' },
    'Read-only. Identity and connection questions are answered on this computer from the current model. Broader questions send the question, the selected items and project facts through the local Claude Code CLI to Claude (Claude Max), an external service; its reply is checked against the model and labelled, which is not engineering verification.');
  // input first (search-style); context and source lines under it; answers expand below
  const compose = h('div', { class: 'asst-compose' },
    h('div', { class: 'asst-field' }, h('span', { class: 'asst-glyph', 'aria-hidden': 'true' }, icon('ask')), input, askButton), budgetRow, editHint);
  const askSection = h('section', { class: 'asst-section asst-ask', id: 'asst-ask', role: 'tabpanel', 'aria-labelledby': 'asst-tab-ask' },
    compose, h('div', { class: 'asst-meta' }, contextRow, sourceRow), thread, askNote);

  // Change section: history and route actions stay reachable in focus, then the one composer
  const actions = h('div', { class: 'asst-actions' });
  const changeSection = h('section', { class: 'asst-section asst-change', id: 'asst-change', role: 'tabpanel', 'aria-labelledby': 'asst-tab-change', hidden: true },
    actions, composerRoot);
  composerRoot.classList.add('asst-composer');

  replace(root, head, askSection, changeSection);

  // the dock's Feedback entry now opens Change; it mirrors the composer's state
  const entry = document.getElementById('panel-feedback-entry');
  const entrySummary = h('span', { class: 'entry-summary mono' });
  const entryButton = h('button', { type: 'button', class: 'btn primary', id: 'btn-open-change' }, icon('edit'), h('span', null, 'Open Change'));
  entryButton.addEventListener('click', () => openMode('change', entryButton));
  replace(entry, h('div', { class: 'entry-card' },
    h('p', null, h('b', null, 'Change requests'), ' — Quick edit and Engineering plan, with the same review and explicit Apply — are in the floating assistant (the Ask button), also in focus.'),
    h('div', { class: 'btn-row' }, entryButton, entrySummary)));

  // ---------------------------------------------------------------- open, close, mode

  const launcher = () => document.getElementById('btn-ask');

  function visible(el) {
    return !!(el && el.isConnected && el.getClientRects().length);
  }

  function syncChrome() {
    document.body.classList.toggle('asst-open', open);
    root.hidden = !open;
    const b = launcher();
    if (b) {
      b.setAttribute('aria-expanded', String(open));
      b.classList.toggle('on', open);
    }
    reposition();
  }

  /** Beside the launcher, inside the viewport; called on open, launcher moves and window resizes. */
  function reposition() {
    const b = launcher();
    if (!open || !b) return;
    placement = positionBubble(root, b.getBoundingClientRect());
  }

  function setMode(next, { focus = false } = {}) {
    if (!MODES.includes(next)) return;
    mode = next;
    for (const b of tabButtons) {
      const on = b.dataset.mode === mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    askSection.hidden = mode !== 'ask';
    changeSection.hidden = mode !== 'change';
    if (mode === 'ask') renderAsk();
    else renderActions();
    if (focus && open) focusMode();
  }

  function focusMode() {
    if (mode === 'ask') input.focus({ preventScroll: true });
    else app.composer.focusInput();
  }

  /** Open in a mode; `from` is the control that opened it (keyboard focus returns there on close). */
  function openMode(next = mode, from = document.activeElement) {
    if (from && from !== document.body && !root.contains(from)) opener = from;
    open = true;
    setMode(next);
    syncChrome();
    focusMode();
  }

  function close({ restoreFocus = true } = {}) {
    if (!open) return;
    const hadFocus = root.contains(document.activeElement);
    open = false;
    syncChrome();
    if (!restoreFocus || (!hadFocus && document.activeElement && document.activeElement !== document.body)) return;
    const target = visible(opener) ? opener : launcher();
    if (target) target.focus({ preventScroll: true });
  }

  // An outside click closes (drafts, answers, proposals and running requests stay); a drag outside,
  // such as orbiting or panning a view, keeps it open. Clicks on the launcher are its own toggle.
  let outside = null;
  document.addEventListener('pointerdown', (e) => {
    const b = launcher();
    outside = open && e.isPrimary && !root.contains(e.target) && !(b && b.contains(e.target)) ? { x: e.clientX, y: e.clientY, id: e.pointerId } : null;
  }, true);
  document.addEventListener('pointerup', (e) => {
    const o = outside;
    outside = null;
    if (!o || !open || e.pointerId !== o.id || isDrag(e.clientX - o.x, e.clientY - o.y, DRAG_THRESHOLD_PX)) return;
    close({ restoreFocus: false });
  }, true);
  document.addEventListener('pointercancel', () => { outside = null; }, true);
  window.addEventListener('resize', () => reposition());

  /** The Ask buttons: open Ask; when Ask is already shown, close. */
  function toggleAsk(from) {
    if (open && mode === 'ask') close();
    else openMode('ask', from);
  }


  // ---------------------------------------------------------------- Ask context

  function selectionKey(ids) {
    return [...ids].sort().join('|');
  }

  function contextIds() {
    const ids = app.state ? knownIds(app.state.project, app.selectionIds) : [];
    if (context.clearedKey !== null && context.clearedKey === selectionKey(ids)) return [];
    return ids;
  }

  /** The pane the user last worked in (else the first visible one) and what it shows. */
  function shownSource() {
    const pane = app.activePane ? app.activePane() : null;
    if (!pane || !pane.content) return { pane: null, src: null };
    const loader = pane.content.loader;
    const installed = loader ? loader.state().installed : null;
    return { pane, src: installed ? displayedSource(installed.src, pane.mode) : null, origin: installed ? installed.src.origin : null };
  }

  let contextKey = null;
  function renderContext() {
    const s = app.state;
    if (!s) {
      replace(contextRow, h('span', { class: 'asst-muted' }, 'Waiting for the project…'));
      replace(sourceRow);
      contextKey = null;
      return;
    }
    const all = knownIds(s.project, app.selectionIds);
    const ids = contextIds();
    const shownNow = shownSource();
    const key = JSON.stringify([all, ids, ids.map((id) => currentLabel(s.project, id)), s.project.revision, s.document && s.document.input_digest,
      shownNow.pane && [shownNow.pane.index, shownNow.pane.type], shownNow.src, shownNow.origin]);
    if (key === contextKey) return;
    contextKey = key;
    const chips = ids.slice(0, 8).map((id) => h('button', {
      type: 'button', class: 'tchip', title: `${id} · goes with the next question`, onclick: () => app.select([id], 'inspector'),
    }, currentLabel(s.project, id)));
    const more = ids.length > 8 ? h('span', { class: 'asst-muted' }, `+${ids.length - 8}`) : null;
    const clear = ids.length ? h('button', {
      type: 'button', class: 'btn small ghost', title: 'Send the next question without the selected items (the selection itself stays)',
      onclick: () => { context.clearedKey = selectionKey(ids); contextKey = null; renderAsk(); },
    }, icon('close'), 'Clear') : null;
    const reveal = all.length ? h('button', {
      type: 'button', class: 'btn small ghost', title: 'Bring the selected items into view in the visible panes (F)', onclick: () => app.focusSelection(),
    }, icon('focus'), 'Reveal') : null;
    replace(contextRow,
      h('span', { class: 'asst-label' }, 'Context'),
      ids.length ? h('span', { class: 'chips' }, chips, more) : h('span', { class: 'asst-muted' },
        all.length ? 'Selection detached from the next question' : 'No selection · select items to ask about “it”'),
      h('span', { class: 'asst-tools' }, clear, reveal));
    const now = s.document ? s.document.input_digest : null;
    const shown = shownNow;
    let visibleText = null;
    if (shown.pane) {
      const where = `pane ${shown.pane.index + 1} · ${TYPE_NAMES[shown.pane.type] || shown.pane.type}`;
      if (!shown.src) visibleText = `${where} · no identified content`;
      else if (shown.src.source_revision === s.project.revision && shown.src.input_digest === now) visibleText = `${where} · ${shown.src.mode} r${shown.src.source_revision} (same source)`;
      else visibleText = `${where} · ${shown.origin === 'published' ? 'published' : shown.src.mode} r${shown.src.source_revision} · not the current model`;
    }
    replace(sourceRow,
      h('span', null, h('b', null, 'Asks'), ` current model r${s.project.revision} · ${shortDigest(now)}`),
      visibleText ? h('span', { class: shown.src && shown.src.source_revision !== s.project.revision ? 'asst-older' : '' }, h('b', null, 'Visible'), ` ${visibleText}`) : null);
  }

  // ---------------------------------------------------------------- Ask request

  function check() {
    const ids = contextIds();
    const result = preflightAsk({ question: input.value, selectedIds: ids, online: app.online !== false, state: app.state, busy: !!pending });
    budgetCount.textContent = `${result.length.toLocaleString('en-US')} / ${ASK_LIMITS.question.toLocaleString('en-US')}`;
    budgetRow.className = `budget-row ${result.problem && !['empty', 'busy'].includes(result.problem) ? 'bad' : result.length > ASK_LIMITS.question * 0.9 ? 'near' : ''}`.trim();
    budgetNote.textContent = result.message;
    askButton.disabled = !result.ok;
    const edit = looksLikeEdit(input.value);
    editHint.hidden = !edit;
    if (edit && !editHint.childElementCount) {
      replace(editHint, h('span', null, 'This reads like a change. Ask only answers questions; nothing is planned or applied from here.'),
        h('button', { type: 'button', class: 'btn small', onclick: () => copyToChange(input.value, editHint) }, icon('edit'), 'Copy to Change'));
    }
    return result;
  }

  async function send(question = input.value, ids = contextIds(), { fromDraft = true } = {}) {
    const s = app.state;
    const result = preflightAsk({ question, selectedIds: ids, online: app.online !== false, state: s, busy: !!pending });
    if (!result.ok) {
      check();
      return;
    }
    const shown = shownSource();
    const requestId = newRequestId(randomBytes);
    let request;
    try {
      request = buildAskRequest({ requestId, question, selectedIds: ids, state: s, history: historyFrom(turns), displayed: shown.src });
    } catch (error) {
      notice = { tone: 'failed', text: String(error.message || error) };
      renderAsk();
      return;
    }
    const turn = {
      key: `t${++seq}`, request_id: requestId, question: request.question, asked_revision: request.expected_revision, asked_digest: request.input_digest,
      context: request.selected_ids.map((id) => ({ id, tag: currentLabel(s.project, id) })), shown: request.displayed_source || null,
      started: Date.now(), answer: null, failure: null, problems: null, phase: 'running',
    };
    const bounded = boundTurns([...turns, turn]);
    turns = bounded.turns;
    dropped += bounded.dropped;
    if (fromDraft) {
      input.value = '';
      fitInput();
    }
    pending = { turn, controller: new AbortController(), request, cancelTimer: null, cancelling: false };
    notice = null;
    renderAsk();
    scrollToTurn(turn);
    const reply = await app.ask(request, { signal: pending.controller.signal });
    if (!pending || pending.turn !== turn) return; // reset meanwhile: the answer belongs to an old conversation
    clearTimeout(pending.cancelTimer);
    const cancelled = pending.cancelling;
    pending = null;
    if (reply.ok) {
      const read = readAnswer(reply.answer, request);
      if (read.ok && app.state && read.answer.project_id && read.answer.project_id !== app.state.project.id) {
        turn.phase = 'failed';
        turn.failure = { tone: 'failed', title: 'Answer for another project', text: 'The reply names a different project; it is not shown.', retry: true };
      } else if (read.ok) {
        turn.phase = 'done';
        turn.answer = read.answer;
      } else {
        turn.phase = 'failed';
        turn.failure = { tone: 'failed', title: 'Reply not shown', text: `The service reply does not match the Ask contract: ${read.problems.join('; ')}.`, retry: true };
      }
    } else {
      turn.phase = 'failed';
      turn.failure = askFailure(reply.error, app.state ? app.state.project.revision : null);
      if (reply.aborted && cancelled) turn.failure.text = 'Cancelled here. Nothing was changed; if the service still finishes, its answer is not shown.';
      // no answer arrived (timeout or lost connection): the service may still be running it
      if (!reply.aborted && reply.error && reply.error.network) app.cancelAsk(request.request_id);
    }
    renderAsk();
    scrollToTurn(turn);
  }

  async function cancelPending() {
    if (!pending || pending.cancelling) return;
    const p = pending;
    p.cancelling = true;
    renderTurn(p.turn);
    const r = await app.cancelAsk(p.request.request_id);
    if (pending !== p) return;
    // the service answers the running request with status "cancelled"; stop waiting if it does not
    p.cancelTimer = setTimeout(() => { if (pending === p) p.controller.abort(); }, r.ok ? CANCEL_GRACE_MS : 0);
  }

  function scrollToTurn(turn) {
    const el = turn.el;
    if (el && el.isConnected) el.scrollIntoView({ block: 'nearest' });
  }

  // ---------------------------------------------------------------- copy to Change

  function copyToChange(text, where) {
    const value = String(text || '').trim();
    if (!value) return;
    const existing = app.composer.draft().trim();
    if (existing && existing !== value && !where.querySelector('.asst-confirm')) {
      where.append(h('div', { class: 'asst-confirm', role: 'group', 'aria-label': 'Replace the Change draft' },
        h('span', null, `Change already has a draft (${existing.length} characters).`),
        h('button', { type: 'button', class: 'btn small', onclick: () => { place(value); } }, 'Replace it'),
        h('button', { type: 'button', class: 'btn small ghost', onclick: (e) => e.target.closest('.asst-confirm').remove() }, 'Keep it')));
      return;
    }
    if (existing === value) {
      openMode('change');
      return;
    }
    place(value);
  }

  function place(value) {
    for (const c of root.querySelectorAll('.asst-confirm')) c.remove();
    app.composer.setDraft(value); // a draft only: review and Apply stay explicit
    openMode('change');
  }

  // ---------------------------------------------------------------- rendering: thread

  function chipFor(ref) {
    const s = app.state;
    const now = s ? referenceNow(ref, s.project) : { exists: false };
    const label = ref.tag || ref.id;
    if (!now.exists) {
      return h('span', { class: 'tchip gone', title: `${ref.id} is not in the current revision${ref.kind ? ` · ${ref.kind}` : ''}` }, label);
    }
    return h('button', {
      type: 'button', class: 'tchip', dataset: { ref: ref.id },
      title: `${ref.id}${ref.kind ? ` · ${ref.kind}` : ''}${now.renamed ? ` · now ${now.label}` : ''}\nSelect it in every pane (the cameras stay where they are)`,
      onclick: () => app.select([ref.id], 'inspector'),
    }, label, now.renamed ? h('small', null, ` → ${now.label}`) : null);
  }

  function evidence(answer) {
    const withFacts = answer.references.filter((r) => r.facts.length || r.field_paths.length);
    if (!withFacts.length) return null;
    return h('details', { class: 'raw asst-evidence' }, h('summary', null, `Evidence (${withFacts.length} reference${withFacts.length === 1 ? '' : 's'}, r${answer.source_revision})`),
      withFacts.map((r) => h('div', { class: 'ev' },
        h('div', { class: 'ev-head' }, h('b', null, r.tag || r.id), r.kind ? h('span', null, r.kind) : null, h('code', null, r.id)),
        r.facts.length ? h('ul', null, r.facts.map((f) => h('li', null, f))) : null,
        r.field_paths.length ? h('div', { class: 'ev-paths mono' }, r.field_paths.join(' · ')) : null)));
  }

  function responderLine(answer) {
    const r = answer.responder;
    if (r.language_model) {
      return h('span', { class: 'who model', title: 'A language model wrote this explanation from checked project facts, sent through the local Claude Code CLI to Claude (Claude Max). References were validated against the snapshot; sentences were not.' },
        `Claude (Max)${r.model ? ` · ${r.model}` : ''}`);
    }
    if (r.model_called) {
      return h('span', { class: 'who model', title: `The question and project facts were sent to Claude (Claude Max), but no model answer is shown${r.error ? `: ${r.error.message || r.error.code}` : ''}. What is shown was derived on this computer.` },
        'Local fallback · sent to Claude');
    }
    const how = r.kind && !/^local$/i.test(r.kind) ? ` · ${r.kind.replace(/_/g, ' ')}` : '';
    return h('span', { class: 'who local', title: 'Answered on this computer from the model and its directed connections; nothing was sent to a language model.' }, `Local${how}`);
  }

  function turnKey(turn) {
    const s = app.state;
    const fresh = turn.answer && s ? answerFreshness(turn.answer, s) : null;
    const refs = turn.answer && s ? turn.answer.references.map((r) => { const n = referenceNow(r, s.project); return `${n.exists}${n.label}`; }).join(',') : '';
    return `${turn.phase}|${fresh ? fresh.text : ''}|${refs}|${pending && pending.turn === turn ? pending.cancelling : ''}|${app.online !== false}|${s ? s.project.revision : ''}`;
  }

  /** Rebuilt only when what it states changes, so buttons stay in place between polls and ticks. */
  function renderTurn(turn) {
    const key = turnKey(turn);
    if (turn.el && turn.renderedKey === key) {
      const time = turn.phase === 'running' ? turn.el.querySelector('.pl-time') : null;
      if (time) time.textContent = clock((Date.now() - turn.started) / 1000);
      return turn.el;
    }
    turn.renderedKey = key;
    const s = app.state;
    const q = h('div', { class: 'turn-q' },
      h('span', { class: 'turn-mark', 'aria-hidden': 'true' }, 'Q'),
      h('p', null, turn.question),
      turn.context.length ? h('div', { class: 'turn-ctx' }, h('span', null, 'with'), turn.context.map((c) => h('span', { class: 'ctx-tag', title: c.id }, c.tag || c.id))) : null);
    let body;
    if (turn.phase === 'running') {
      const cancelling = pending && pending.turn === turn && pending.cancelling;
      body = h('div', { class: 'planning turn-run', role: 'status' },
        h('i', { class: 'pulse' }), h('span', { class: 'pl-what' }, cancelling ? 'Cancelling' : 'Asking'),
        h('span', { class: 'pl-time mono' }, clock((Date.now() - turn.started) / 1000)),
        h('span', { class: 'pl-note' }, `for r${turn.asked_revision} · views, selection and edits stay live`),
        h('button', { type: 'button', class: 'btn small', disabled: cancelling || null, title: 'Cancel only this question on the service (closing the assistant does not cancel it)', onclick: () => cancelPending() }, 'Cancel question'));
    } else if (turn.failure) {
      const f = turn.failure;
      const current = s ? s.project.revision : null;
      body = h('div', { class: `turn-a tone-${f.tone}` },
        h('div', { class: 'stamp' }, h('b', null, f.title), h('span', null, `asked on r${turn.asked_revision}`)),
        h('p', { class: 'a-text' }, f.text),
        h('div', { class: 'turn-foot' },
          f.retry ? h('button', { type: 'button', class: 'btn small', disabled: !!pending || app.online === false || null, onclick: () => resend(turn) }, `Ask again on r${current}`) : null,
          h('button', { type: 'button', class: 'btn small ghost', onclick: (e) => copyToChange(turn.question, e.target.closest('.turn-foot')) }, 'Copy to Change')));
    } else {
      const a = turn.answer;
      const answered = a.status === 'answered' || a.status === 'clarification';
      const fresh = answered ? answerFreshness(a, s) : { stale: false };
      const visibleNote = answered ? visibleSourceNote(a) : null;
      const edit = a.responder.query === 'change_request' || looksLikeEdit(turn.question);
      const offline = app.online === false;
      body = h('div', { class: `turn-a s-${a.status}${fresh.stale ? ' stale' : ''}` },
        h('div', { class: 'stamp' },
          h('b', null, STAMP[a.status] || a.status),
          h('span', { class: 'mono' }, `model r${a.source_revision} · ${shortDigest(a.input_digest)}`),
          responderLine(a)),
        fresh.stale ? h('div', { class: 'asst-stale' }, h('span', null, fresh.text),
          h('button', { type: 'button', class: 'btn small', disabled: !!pending || offline || null, title: 'Ask the same question again on the current model (a new request)', onclick: () => resend(turn) }, `Refresh on r${s.project.revision}`)) : null,
        visibleNote ? h('div', { class: 'asst-visible' }, visibleNote) : null,
        h('p', { class: 'a-text' }, a.answer),
        a.references.length ? h('div', { class: 'chips turn-refs' }, a.references.map(chipFor)) : null,
        a.unknowns.length ? h('div', { class: 'prop-block' }, h('h6', null, 'Unknown'), h('ul', null, a.unknowns.map((u) => h('li', null, u)))) : null,
        a.limitations.length ? h('ul', { class: 'turn-limits' }, a.limitations.map((l) => h('li', null, l))) : null,
        evidence(a),
        a.responder.facts.length ? h('details', { class: 'raw' }, h('summary', null, 'Responder'),
          h('div', { class: 'kv-grid mono facts' }, a.responder.facts.flatMap(([k, v]) => [h('span', null, k), h('span', null, v)]))) : null,
        h('div', { class: 'turn-foot' },
          h('span', { class: 'mono turn-id', title: `${a.id || ''} · ${a.request_id}` }, a.id || a.request_id),
          answered ? null : h('button', { type: 'button', class: 'btn small', disabled: !!pending || offline || null, title: 'Ask the same question again (a new request)', onclick: () => resend(turn) }, `Ask again on r${s.project.revision}`),
          h('button', { type: 'button', class: `btn small${edit ? '' : ' ghost'}`, title: 'Put this question into the Change composer as a draft (nothing is planned or applied)', onclick: (e) => copyToChange(turn.question, e.target.closest('.turn-foot')) }, edit ? 'Copy to Change' : 'To Change')));
    }
    const el = h('article', { class: `asst-turn p-${turn.phase}`, dataset: { request: turn.request_id } }, q, body);
    if (turn.el && turn.el.isConnected) turn.el.replaceWith(el);
    turn.el = el;
    return el;
  }

  function resend(turn) {
    if (pending) return;
    const ids = app.state ? knownIds(app.state.project, turn.context.map((c) => c.id)) : [];
    send(turn.question, ids, { fromDraft: false });
  }

  // header lines are rebuilt only when they change, so polling never re-attaches (and rescrolls) the thread
  let headKey = null;
  let headEls = [];
  function renderThread() {
    const items = turns.map((t) => renderTurn(t));
    const key = JSON.stringify([dropped, turns.length > 0, notice]);
    if (key !== headKey) {
      headKey = key;
      headEls = [];
      if (dropped) headEls.push(h('div', { class: 'asst-muted asst-sep' }, `${dropped} earlier question${dropped === 1 ? '' : 's'} removed (this page keeps the last ${turns.length})`));
      if (notice) headEls.push(h('div', { class: `asst-notice tone-${notice.tone}` }, notice.text));
    }
    const existing = [...thread.children];
    const wanted = [...headEls, ...items];
    if (existing.length !== wanted.length || existing.some((el, i) => el !== wanted[i])) replace(thread, wanted);
    thread.hidden = !wanted.length; // compact, input-first until there is something to show
  }

  function renderAsk() {
    renderContext();
    renderThread();
    check();
    renderDots();
  }

  // ---------------------------------------------------------------- rendering: Change actions

  let actionsKey = null;
  function renderActions() {
    const s = app.state;
    if (!s) {
      replace(actions);
      actionsKey = null;
      return;
    }
    const key = JSON.stringify([s.project.revision, s.history.undo, s.history.redo, app.rerouteVersion, app.online !== false,
      Object.entries(s.project.physical.routes || {}).map(([id, r]) => `${id}:${r.status}`)]);
    if (key === actionsKey) return;
    actionsKey = key;
    const rev = s.project.revision;
    const offline = app.online === false;
    const why = offline ? 'The local service is not reachable.' : null;
    const undo = h('button', { type: 'button', class: 'btn small', id: 'asst-undo', disabled: !s.history.undo || offline || null, title: why || 'Undo the last change (same command path as the toolbar)', onclick: () => app.undo() },
      icon('undo'), 'Undo', s.history.undo ? h('em', null, String(s.history.undo)) : null);
    const redo = h('button', { type: 'button', class: 'btn small', id: 'asst-redo', disabled: !s.history.redo || offline || null, title: why || 'Redo (same command path as the toolbar)', onclick: () => app.redo() },
      icon('redo'), 'Redo', s.history.redo ? h('em', null, String(s.history.redo)) : null);
    const pendingRoutes = Object.keys(s.project.model.connections).filter((id) => rerouteOffer(s.project, id).pending);
    const routes = pendingRoutes.map((id) => routeButton(s, id));
    replace(actions,
      h('div', { class: 'asst-hist' }, h('span', { class: 'asst-label' }, `r${rev}`), undo, redo),
      routes.length ? h('div', { class: 'asst-routes' }, h('span', { class: 'asst-label' }, 'Pending routes'), routes) : null);
  }

  function routeButton(s, connId) {
    const offer = rerouteOffer(s.project, connId);
    const ui = app.rerouteState(connId);
    const rec = ui.record;
    const conn = s.project.model.connections[connId];
    const busy = !!rec && rec.status === 'busy';
    const failed = !!rec && rec.status === 'error' && rec.fromRevision === s.project.revision;
    const blocked = !offer.actionable ? offer.why : ui.anyBusy && !busy ? 'Another reroute is running.' : !ui.online ? 'The local service is not reachable.' : null;
    return h('button', {
      class: `reroute${busy ? ' busy' : ''}${failed ? ' failed' : ''}`, type: 'button', disabled: busy || !!blocked || null,
      title: `${conn.tag}: ${offer.status}${offer.reason ? ` — ${offer.reason}` : ''}\n${busy ? 'Computing a guided route, saving and regenerating…' : blocked || 'Compute a guided route, commit it as one validated revision and regenerate all five outputs.'}${failed ? `\nLast attempt: ${rec.message}` : ''}`,
      'aria-label': `Reroute ${conn.tag}, route ${offer.status}`, dataset: { connection: connId }, onclick: () => app.reroute(connId),
    }, h('b', null, conn.tag), h('span', { class: 'st' }, offer.status.replace(/_/g, ' ')), h('span', { class: 'act' }, icon('route'), busy ? 'Rerouting…' : 'Reroute'));
  }

  function renderDots() {
    const c = app.composer.summary();
    const askBusy = !!pending;
    const changeBusy = !!c.busy || !!(c.proposal && !c.proposal.applied);
    tabButtons[0].classList.toggle('busy', askBusy);
    tabButtons[1].classList.toggle('busy', changeBusy);
    const b = launcher();
    if (b) b.classList.toggle('busy', askBusy || changeBusy);
    tabButtons[1].title = changeBusy
      ? `Change: ${c.busy ? `${c.busy.mode === 'engineering' ? 'planning' : 'reviewing'} for r${c.busy.revision}` : `proposal ${c.proposal.id} waiting for review`}`
      : 'Quick edit or Engineering plan: reviewable proposals with explicit Apply';
    renderEntry(c);
  }

  function renderEntry(c = app.composer.summary()) {
    const parts = [];
    if (c.busy) parts.push(`${c.busy.mode === 'engineering' ? 'planning' : 'reviewing'} ${clock(c.busy.seconds)}`);
    if (c.proposal) parts.push(c.proposal.applied ? `${c.proposal.id} applied` : `${c.proposal.id} · ${c.proposal.stale ? 'stale' : c.proposal.verdict || 'review'}`);
    if (c.draftLength) parts.push(`draft ${c.draftLength} chars`);
    entrySummary.textContent = parts.length ? parts.join(' · ') : `${c.mode === 'engineering' ? 'Engineering plan' : 'Quick edit'} · no draft`;
  }

  composerRoot.addEventListener('composer-change', () => renderDots());
  // a question left running when the page goes away would hold the service's model slot
  window.addEventListener('pagehide', () => {
    if (pending) app.cancelAsk(pending.request.request_id, { leaving: true });
  });

  // ---------------------------------------------------------------- input

  askButton.addEventListener('click', () => send());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  function fitInput() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight + 2, 132)}px`;
  }
  input.addEventListener('input', () => {
    if (!looksLikeEdit(input.value)) replace(editHint);
    fitInput();
    check();
  });
  input.addEventListener('focus', () => renderContext());

  // ---------------------------------------------------------------- lifecycle

  /** Abandon the running question: stop waiting and signal the service when it is reachable. */
  function abandon() {
    if (!pending) return;
    const id = pending.request.request_id;
    clearTimeout(pending.cancelTimer);
    pending.controller.abort();
    pending = null;
    app.cancelAsk(id);
  }

  function reset(reason) {
    abandon();
    app.composer.reset(reason);
    turns = [];
    dropped = 0;
    input.value = '';
    fitInput();
    context = { clearedKey: null };
    contextKey = null;
    notice = { tone: 'stale', text: reason };
    replace(editHint);
  }

  function update(state) {
    const key = identityKey(state);
    if (identity !== null && key !== identity) reset('The project or the service changed: earlier answers, drafts and the open proposal were cleared. Nothing was applied.');
    identity = key;
    if (mode === 'ask') renderAsk();
    else {
      renderActions();
      renderDots();
    }
  }

  setMode('ask');
  syncChrome();
  renderEntry();

  return {
    root,
    open: openMode,
    close,
    toggleAsk,
    setMode: (m) => setMode(m, { focus: open }),
    isOpen: () => open,
    contains: (el) => !!el && root.contains(el),
    /** The launcher moved or focus changed: keep the open bubble beside it. */
    place: reposition,
    update,
    refresh() {
      if (mode === 'ask') renderAsk();
      else renderActions();
    },
    tick() {
      if (pending && pending.turn.el) renderTurn(pending.turn);
      if (open && mode === 'ask') renderContext();
      renderDots();
    },
    routesChanged() {
      if (mode === 'change') renderActions();
    },
    /** A pane changed type, source mode or installed content: the Ask source line may name it. */
    sourcesChanged() {
      if (open && mode === 'ask') renderContext();
    },
    snapshot() {
      return {
        open,
        mode,
        layout: open ? 'floating' : null,
        placement: open && placement ? { ...placement } : null,
        draftLength: input.value.length,
        context: contextIds(),
        identity,
        pending: pending ? { request_id: pending.request.request_id, revision: pending.request.expected_revision, cancelling: pending.cancelling } : null,
        turns: turns.map((t) => ({
          request_id: t.request_id, phase: t.phase, question: t.question, asked_revision: t.asked_revision,
          status: t.answer ? t.answer.status : null, source_revision: t.answer ? t.answer.source_revision : null,
          stale: t.answer && app.state ? answerFreshness(t.answer, app.state).stale : null,
          references: t.answer ? t.answer.references.map((r) => r.id) : [], failure: t.failure ? t.failure.title : null,
        })),
        dropped,
        composerHost: composerRoot.parentElement === changeSection ? 'assistant' : 'elsewhere',
      };
    },
  };
}
