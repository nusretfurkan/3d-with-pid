// Inline project-name editor in the toolbar. Click (or Enter/Space on the focused name)
// opens a text field; Enter or Save sends one fresh rename_project command through the
// validated command path; Escape or Cancel closes without a request. Polling updates the
// shown name but never the field being edited: a change made elsewhere is reported next
// to the field and must be confirmed with a second Save. The project ID never changes.

import { $ } from './dom.js';
import { envelope } from './commands-model.js';
import { ApiError } from './api.js';
import { PROJECT_NAME_MAX, commitDecision, conflictNote, displayName, nameProblem, trimName } from './project-name.js';

export function createNameEditor(app) {
  const button = $('#tb-project-name');
  const label = $('#tb-project-id');
  const form = $('#tb-name-form');
  const input = $('#tb-name-input');
  const save = $('#tb-name-save');
  const cancel = $('#tb-name-cancel');
  const msg = $('#tb-name-msg');
  let editing = false;
  let base = null;       // {name, revision} the edit is based on
  let sending = false;
  let notice = null;     // {tone, text} kept until the next edit or decision
  let rebase = false;    // after a conflict, move base to the next state's revision
  let project = null;

  function show(text, tone = '') {
    msg.textContent = text || '';
    msg.className = `tb-name-msg ${tone}`;
    msg.hidden = !text;
  }

  function refresh() {
    if (!editing) return;
    const problem = nameProblem(input.value);
    save.disabled = sending || !!problem;
    input.setAttribute('aria-invalid', String(!!problem));
    if (sending) return show('Saving…');
    if (notice) return show(notice.text, notice.tone);
    if (problem) return show(problem, 'err');
    const drift = conflictNote(project, base);
    if (drift) return show(drift, 'warn');
    show(`${[...trimName(input.value)].length}/${PROJECT_NAME_MAX} · Enter saves · Esc cancels · ID ${project ? project.id : ''} is unchanged`);
  }

  function open() {
    if (!app.state || editing) return;
    project = app.state.project;
    editing = true;
    notice = null;
    rebase = false;
    base = { name: displayName(project), revision: project.revision };
    input.value = base.name;
    button.hidden = true;
    form.hidden = false;
    form.closest('.toolbar').classList.add('editing-name');
    input.focus();
    input.select();
    refresh();
  }

  function close() {
    editing = false;
    sending = false;
    notice = null;
    base = null;
    form.hidden = true;
    button.hidden = false;
    form.closest('.toolbar').classList.remove('editing-name');
    show('');
    if (project) render(project);
    button.focus({ preventScroll: true });
  }

  async function commit() {
    if (!editing || sending || !app.state) return;
    project = app.state.project;
    const decision = commitDecision(input.value, project, base);
    if (decision.action === 'invalid') {
      notice = { tone: 'err', text: decision.reason };
      return refresh();
    }
    if (decision.action === 'unchanged') return close();
    if (decision.action === 'confirm') {
      notice = { tone: 'warn', text: `${decision.reason} Press Save again to apply your name to revision ${project.revision}.` };
      base = { name: displayName(project), revision: project.revision };
      return refresh();
    }
    sending = true;
    notice = null;
    refresh();
    const command = envelope('rename_project', { name: decision.name }, decision.expectedRevision);
    const result = await app.submitCommand(command, 'Rename project', { quiet: true });
    sending = false;
    if (!editing) return;
    if (result.ok) return close();
    const e = result.error;
    const text = e instanceof ApiError
      ? [e.error, ...(e.diagnostics || []).map((d) => d.message)].filter(Boolean).join(' — ')
      : String(e && e.message ? e.message : e);
    if (e instanceof ApiError && e.status === 409) {
      notice = { tone: 'warn', text: `Conflict: ${text}. Your text is kept; Save again to apply it to the current revision.` };
      rebase = true;
    } else {
      notice = { tone: 'err', text: e instanceof ApiError && e.network ? `Not sent: ${text}` : `Rejected: ${text}` };
    }
    refresh();
  }

  function render(p) {
    const shown = displayName(p);
    label.textContent = shown;
    const named = typeof p.name === 'string' && shown === p.name;
    label.classList.toggle('unnamed', !named);
    button.title = `${named ? `Project “${shown}”` : 'Unnamed project (showing its ID)'} · ID ${p.id}\nClick to rename; the ID, files and outputs keep their identity.`;
    button.setAttribute('aria-label', `Project name ${shown}. Rename`);
    document.title = `${shown} · Skid workspace`;
  }

  button.addEventListener('click', open);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    commit();
  });
  cancel.addEventListener('click', close);
  input.addEventListener('input', () => {
    if (notice && notice.tone === 'err') notice = null;
    refresh();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  });

  return {
    /** Called with every applied state; the field being edited is never overwritten. */
    update(p) {
      project = p;
      if (!editing) return render(p);
      if (rebase && base && p.revision !== base.revision) {
        base = { name: displayName(p), revision: p.revision };
        rebase = false;
      }
      refresh();
    },
    open,
    snapshot: () => ({
      shown: label.textContent,
      name: project && typeof project.name === 'string' ? project.name : null,
      id: project ? project.id : null,
      editing,
      draft: editing ? input.value : null,
      base: base ? { ...base } : null,
      sending,
      message: msg.hidden ? null : msg.textContent,
      tone: msg.hidden ? null : (msg.className.replace('tb-name-msg', '').trim() || 'info'),
    }),
  };
}
