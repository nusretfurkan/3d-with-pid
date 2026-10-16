// Feedback composer: text -> reviewable proposal -> explicit apply, in two modes.
//
// Quick edit: the service's bounded deterministic parser on this computer (one rename, one dimension
// or one move). Engineering plan: the service sends the request and current project data through the
// local Claude Code CLI to Claude on the Claude Max subscription (an external model service), checks
// the reply as untrusted input, turns it into ONE batch envelope and dry-runs it. The review card shows reasoning, affected items,
// proposed assumptions, unknowns, questions, the exact operations and the validation, and states
// that geometry has not been checked. Questions, refusals and stale or expired plans never offer
// Apply. Applying a plan is one revision and one undo step. Planning never blocks the workspace.
// The composer is created once; the assistant moves its mounted root into its Change section, so
// draft, proposal and running request survive opening, closing and focus. Its root emits
// `composer-change` whenever the draft, mode, request or proposal changes.
//
// Engineering plan has one compact option, "Research equipment", shown only when the running service
// advertises it. When ticked, only the request text first goes to a separate search-only research step;
// its verified brief is planner context (untrusted evidence). Progress shows the service's stage
// (researching, then planning); the review card lists the sources (links only for public http(s) URLs),
// sourced features, research assumptions and unknowns before any Apply, whether applying keeps any
// research reference in the model (only assumption records carry it), and inside "Research run" the
// recorded search queries as plain text. A research failure is shown as a refusal: no plan is substituted.

import { h, replace, icon } from './dom.js';
import { fmtNum, isComposed, nextFreeTag, tagInUse } from './commands-model.js';
import {
  followUp, itemRefs, itemText, plannerFacts, plannerHeadline, preflight, questionIds, reviewProposal,
} from './plan-model.js';
import { QUERY_SHOWN, assumptionCitations, researchOffered, reviewResearch, stageInfo } from './research-model.js';
import { creationExamples, isEmptyProject } from './creation-model.js';

export const MODE_INFO = {
  quick: {
    label: 'Quick edit',
    button: 'Review',
    placeholder: 'Describe one change, e.g. “Set motor length of P-101 to 0.42 m”',
    intro: 'Requests go to the service\'s bounded deterministic parser on this computer: one rename, one template dimension, or one physical or P&ID move per proposal. Other requests are reported as unsupported.',
  },
  engineering: {
    label: 'Engineering plan',
    button: 'Plan',
    placeholder: 'Describe an engineering change: add or connect equipment, several edits, or composed equipment. Missing decisions come back as questions.',
    intro: 'Your request and the current project data (tags, topology, parameters, layout and assumptions) are sent through the local Claude Code CLI to Claude on the Claude Max subscription, an external service; the model does not run on this computer and gets no tools. The reply is untrusted: it is checked, turned into one atomic batch and dry-run by the command validator before you can apply it. Geometry and CAD are checked only by the build after apply. Planning can take minutes; views, selection and edits stay live.',
  },
};
export const RESEARCH_INTRO = 'Research equipment: before planning, only your request text (no project data) goes to a separate '
  + 'search-only step (Claude with web search on the same subscription). Its cited brief of public sources is checked and given '
  + 'to the planner as untrusted evidence; sources, sourced features, assumptions and unknowns are shown for review. Text '
  + 'search only: no image or CAD file is inspected, and every dimension stays an illustrative assumption. If research '
  + 'fails, no plan is made.';

const VERDICT = {
  ready: ['yes', 'Ready to review'],
  questions: ['ask', 'Questions first'],
  unavailable: ['no', 'Planner unavailable'],
  rejected: ['no', 'Plan rejected'],
  invalid: ['no', 'Rejected by validation'],
  unsupported: ['no', 'Unsupported'],
  'not-run': ['no', 'Not an engineering plan'],
};

function quickExamples(project) {
  const entities = Object.values(project.model.entities);
  const pump = entities.find((e) => e.kind === 'centrifugal_pump');
  const tank = entities.find((e) => e.kind === 'tank');
  const out = [];
  if (pump) {
    const target = tagInUse(project, 'P-201') ? nextFreeTag(project, 'P-') : 'P-201';
    out.push(`Rename ${pump.tag} to ${target}`);
    out.push(`Set motor length of ${pump.tag} to 0.42 m`);
    out.push(`Move ${pump.tag} 0.25 m in +x`);
    out.push(`Move ${pump.tag} symbol right by 40`);
  }
  if (tank) out.push(`Set the height of ${tank.tag} to 1.3 m`);
  return out;
}

/** `creation`: the running service advertises add/connect operations (older services do not). */
function engineeringExamples(project, creation = false) {
  const m = project.model;
  const entities = Object.values(m.entities);
  const pump = entities.find((e) => e.kind === 'centrifugal_pump');
  const composed = entities.find(isComposed);
  const out = creation ? creationExamples(project) : [];
  if (!entities.length) return out;
  if (pump) {
    const target = tagInUse(project, 'P-201') ? nextFreeTag(project, 'P-') : 'P-201';
    out.push(`Rename ${pump.tag} to ${target} and set its motor length to 0.42 m`);
  }
  // an inline line between two valves is a natural place for a hold-up drum
  const line = Object.values(m.connections).find((c) => {
    const a = m.ports[c.from_port_id];
    const b = m.ports[c.to_port_id];
    return a && b && /valve/.test(m.entities[a.entity_id]?.kind || '') && /valve/.test(m.entities[b.entity_id]?.kind || '');
  });
  if (line) {
    const a = m.entities[m.ports[line.from_port_id].entity_id];
    const b = m.entities[m.ports[line.to_port_id].entity_id];
    out.push(`Insert a horizontal drum from the example definition into ${line.tag} between ${a.tag} and ${b.tag}; leave its 3D placement unresolved`);
  }
  if (composed) {
    const first = Object.keys(composed.parameters || {})[0];
    if (first) out.push(`Increase the ${first.replace(/_/g, ' ')} of ${composed.tag} by 10 %`);
  }
  return out;
}

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function createComposer(app, root) {
  let mode = 'quick';
  const seg = h('div', { class: 'seg mode-seg', role: 'radiogroup', 'aria-label': 'Feedback mode' });
  const modeButtons = Object.entries(MODE_INFO).map(([key, info]) => {
    const b = h('button', { type: 'button', role: 'radio', 'data-mode': key, 'aria-checked': String(key === mode), class: key === mode ? 'on' : '' }, info.label);
    b.addEventListener('click', () => setMode(key));
    seg.append(b);
    return b;
  });
  const modeNote = h('span', { class: 'mode-note' });
  const input = h('textarea', { rows: 1, placeholder: MODE_INFO.quick.placeholder, 'aria-label': 'Change request', spellcheck: 'false' });
  const propose = h('button', { class: 'btn primary', type: 'button', title: 'Ask the service for a reviewable proposal (nothing is changed yet)' }, icon('send'), h('span', null, 'Review'));
  const examples = h('div', { class: 'examples' });
  const progress = h('div', { class: 'planning', hidden: true, role: 'status', 'aria-live': 'polite' });
  const result = h('div');
  const intro = h('p', { class: 'limitation' }, MODE_INFO.quick.intro);
  const form = h('div', { class: 'composer' }, input, propose);
  const budgetCount = h('span', { class: 'budget-count mono', title: 'Characters as the service counts them (code points, whitespace collapsed) / its limit for this mode' });
  const budgetNote = h('span', { class: 'budget-note', role: 'status', 'aria-live': 'polite' });
  // the one research option of Engineering plan; hidden unless the running service advertises research
  const researchInput = h('input', { type: 'checkbox', 'aria-describedby': 'composer-research-hint' });
  const researchBox = h('label', { class: 'research-opt', hidden: true, title: RESEARCH_INTRO },
    researchInput, h('span', null, 'Research equipment'));
  const researchHint = h('p', { class: 'limitation research-hint', id: 'composer-research-hint', hidden: true }, RESEARCH_INTRO);
  const budgetRow = h('div', { class: 'budget-row' }, researchBox, budgetCount, budgetNote);
  replace(root, h('div', { class: 'mode-row' }, seg, modeNote), form, budgetRow, examples, progress, result, intro, researchHint);
  let slotIds = []; // answer slots of a follow-up the composer filled in; unanswered ones block sending
  let researchAvailable = false;

  /** Show the request budget and enable the submit button only for a request the service will accept. */
  function updateBudget() {
    const check = preflight(input.value, mode, slotIds);
    const { length, limit, problem } = check.budget;
    budgetCount.textContent = `${length.toLocaleString()} / ${limit.toLocaleString()}`;
    const tone = problem === 'too_long' || problem === 'control' || check.emptySlots.length ? 'bad'
      : length > limit * 0.9 ? 'near' : '';
    budgetRow.className = `budget-row ${tone}`.trim();
    budgetNote.textContent = check.message;
    propose.disabled = !!busy || !check.ok;
    return check;
  }

  /** Whether the next engineering request asks for research (option shown, offered and ticked). */
  function researching() {
    return mode === 'engineering' && researchAvailable && researchInput.checked;
  }

  function renderResearchOption() {
    const shown = mode === 'engineering' && researchAvailable;
    researchBox.hidden = !shown;
    researchHint.hidden = !shown || !researchInput.checked;
    researchInput.disabled = !!busy;
    root.classList.toggle('research-on', researching());
    if (mode === 'engineering') {
      modeNote.textContent = researching() ? 'web research, then Claude (Max) · one atomic batch' : 'sent to Claude (Max) · one atomic batch';
      propose.lastElementChild.textContent = researching() ? 'Research & plan' : MODE_INFO.engineering.button;
      propose.title = researching()
        ? 'Research the equipment from public web sources (request text only), then plan with the current project data (nothing is changed yet)'
        : 'Send the request and current project data to Claude (Claude Max) for a reviewable plan (nothing is changed yet)';
    }
  }

  let proposal = null;
  let requestedMode = 'quick';
  let examplesKey = null;
  let busy = null; // {mode, research, started, controller, revision, before}
  let shownKey = null;
  let ticker = null;
  let planning = null; // state.planning from the service: {modes, planner, active: [...]}, or null

  function activePlans() {
    return planning && Array.isArray(planning.active) ? planning.active : [];
  }

  /** The service-side plan this request started, when it can be identified unambiguously. */
  function ownPlan() {
    if (!busy || busy.mode !== 'engineering') return null;
    const candidates = activePlans().filter((a) => a && !a.cancelled && a.expected_revision === busy.revision && !busy.before.has(a.id));
    return candidates.length === 1 ? candidates[0] : null;
  }

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    for (const b of modeButtons) {
      const on = b.dataset.mode === mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    }
    const info = MODE_INFO[mode];
    input.placeholder = info.placeholder;
    propose.lastElementChild.textContent = info.button;
    propose.title = mode === 'engineering'
      ? 'Send the request and current project data to Claude (Claude Max) for a reviewable plan (nothing is changed yet)'
      : 'Ask the service for a reviewable proposal (nothing is changed yet)';
    intro.textContent = info.intro;
    form.classList.toggle('tall', mode === 'engineering');
    root.classList.toggle('mode-engineering', mode === 'engineering');
    modeNote.textContent = mode === 'engineering' ? 'sent to Claude (Max) · one atomic batch' : 'local · deterministic · one change';
    renderResearchOption();
    examplesKey = null;
    if (app.state) renderExamples(app.state);
    updateBudget();
    changed();
  }

  researchInput.addEventListener('change', () => {
    renderResearchOption();
    changed();
  });

  function changed() {
    root.dispatchEvent(new CustomEvent('composer-change'));
  }

  function fill(text, slots = []) {
    input.value = text;
    slotIds = slots;
    updateBudget();
    input.focus();
    changed();
  }

  function renderExamples(state) {
    const creation = !!(state.capabilities && state.capabilities.equipment_creation === true);
    const texts = mode === 'engineering' ? engineeringExamples(state.project, creation) : quickExamples(state.project);
    const empty = isEmptyProject(state.project);
    const key = `${mode}|${empty}|${creation}|${texts.join('|')}`;
    if (key === examplesKey) return;
    examplesKey = key;
    if (!texts.length && empty) {
      replace(examples, h('span', { class: 'empty-hint' }, mode === 'quick'
        ? 'This project is empty. Quick edit changes existing items; choose Engineering plan to add and connect equipment.'
        : 'This project is empty and this service cannot add equipment; start it with the current version to create equipment.'));
      return;
    }
    replace(examples, texts.length ? h('span', null, 'Try') : null, texts.map((t) => h('button', {
      class: 'example', type: 'button', title: 'Fill the request (review before applying)',
      onclick: () => fill(t),
    }, t)));
  }

  function renderProgress() {
    if (!busy) {
      clearInterval(ticker);
      ticker = null;
      delete progress.dataset.key;
      const others = activePlans().filter((a) => a && !a.cancelled);
      progress.hidden = !others.length;
      if (others.length) {
        replace(progress, h('i', { class: 'pulse' }), h('span', { class: 'pl-what' }, 'Planner busy'),
          h('span', { class: 'pl-note' }, `${others.length} engineering plan${others.length === 1 ? '' : 's'} running on the service (requested elsewhere)`));
      }
      return;
    }
    const elapsed = (Date.now() - busy.started) / 1000;
    const own = ownPlan();
    const stage = busy.mode === 'engineering' ? stageInfo(own, busy.research) : null;
    const key = `${busy.started}|${own ? own.id : ''}|${stage ? stage.stage : ''}`;
    const time = progress.querySelector('.pl-time');
    if (key === progress.dataset.key && time) {
      time.textContent = clock(elapsed); // keep the buttons in place between ticks so clicks are not lost
      return;
    }
    progress.dataset.key = key;
    let control = null;
    if (own) {
      control = h('button', { class: 'btn small', type: 'button', title: `Terminate plan ${own.id} on the service (whichever stage is running); it yields no command` }, 'Cancel plan');
      control.addEventListener('click', async () => {
        control.disabled = true;
        await app.cancelPlan(own.id);
      });
    } else if (busy.mode === 'engineering') {
      control = h('button', { class: 'btn small ghost', type: 'button', title: 'Stop waiting for this answer. The service may still finish planning; its answer will not be shown.' }, 'Stop waiting');
      control.addEventListener('click', () => busy && busy.controller.abort());
    }
    progress.hidden = false;
    progress.dataset.stage = stage ? (stage.stage || 'starting') : '';
    let note = `for r${busy.revision}`;
    if (stage && stage.stage === 'researching') {
      note = `${own ? `${own.id} · ` : ''}${stage.step} · searching public sources with the request text only; views and edits stay live`;
    } else if (stage && stage.research) {
      note = `${own ? `${own.id} · ` : ''}${stage.step || 'research, then plan'} · for r${busy.revision} · may take minutes; views and edits stay live`;
    } else if (busy.mode === 'engineering') {
      note = `${own ? `${own.id} · ` : ''}for r${busy.revision} · the planner may take minutes; views, selection and edits stay live`;
    }
    replace(progress,
      h('i', { class: 'pulse' }),
      h('span', { class: 'pl-what' }, stage ? stage.label : 'Asking the service'),
      h('span', { class: 'pl-time mono', title: 'Elapsed since the request was sent' }, clock(elapsed)),
      h('span', { class: 'pl-note' }, note),
      control);
  }

  /**
   * Send one request. `research` overrides the option for a re-plan (the original request's choice);
   * research is only ever requested in engineering mode from a service that offers it.
   */
  async function run(text, runMode = mode, { research } = {}) {
    if (busy || !app.state) return;
    // never send what the service would refuse: empty, over its limit, refused characters, unanswered slots
    const check = text === undefined ? updateBudget() : preflight(text, runMode);
    if (!check.ok) return;
    const value = (text ?? input.value).trim();
    if (text === undefined) slotIds = [];
    const wantResearch = runMode === 'engineering' && researchAvailable
      && (research === undefined ? researchInput.checked : research === true);
    busy = {
      mode: runMode, research: wantResearch, started: Date.now(), controller: new AbortController(),
      revision: app.state.project.revision,
      before: new Set(activePlans().map((a) => a && a.id)), // plans already running belong to someone else
    };
    propose.disabled = true;
    renderResearchOption();
    replace(result);
    renderProgress();
    progress.scrollIntoView({ block: 'nearest' });
    ticker = setInterval(renderProgress, 1000);
    changed();
    const own = busy;
    const r = await app.propose(value, runMode, { signal: busy.controller.signal, research: wantResearch });
    if (busy !== own) return; // reset meanwhile: this answer belongs to another project or service run
    busy = null;
    renderResearchOption();
    updateBudget();
    renderProgress();
    changed();
    if (!r.ok) {
      if (r.aborted) {
        replace(result, h('div', { class: 'inline-msg' }, 'Stopped waiting. Nothing was changed; the service may still finish that plan, but it will not be offered here. Request again to review a plan.'));
      } else {
        replace(result, h('div', { class: 'diag error' }, h('code', null, r.error.status ? `HTTP ${r.error.status}` : 'network'), r.error.error,
          (r.error.diagnostics || []).map((d) => h('div', null, h('code', null, d.code), d.message))));
      }
      return;
    }
    proposal = r.proposal;
    requestedMode = runMode;
    renderProposal();
    if (result.firstElementChild) result.firstElementChild.scrollIntoView({ block: 'start' });
  }

  function list(title, items, cls = '') {
    if (!items || !items.length) return null;
    return h('div', { class: `prop-block ${cls}` }, h('h6', null, title), h('ul', null, items.map((t) => h('li', null, t))));
  }

  function tagChip(id, label, extra, cls = '') {
    return h('button', { class: `tchip ${cls}`, type: 'button', title: `${id}${extra ? ` · ${extra}` : ''}`, onclick: () => app.select([id], 'inspector') }, label);
  }

  function refChips(project, ids) {
    const m = project.model;
    return ids.map((id) => {
      const rec = m.entities[id] || m.connections[id];
      if (rec) return tagChip(id, rec.tag);
      if (m.ports[id]) return tagChip(id, `${m.entities[m.ports[id].entity_id]?.tag ?? '?'}.${m.ports[id].name}`);
      return h('span', { class: 'tchip new', title: 'Not in the current revision (created by the plan, or not an item ID)' }, id);
    });
  }

  // ---------------------------------------------------------------- quick card (unchanged behavior)

  function renderQuick(p, review) {
    const validation = p.validation || {};
    const current = app.state ? app.state.project.revision : null;
    const verdict = p.supported
      ? h('span', { class: 'verdict yes' }, `Supported · ${String(p.intent || '').replace(/_/g, ' ')}`)
      : h('span', { class: 'verdict no' }, 'Unsupported');
    const body = [h('div', { class: 'prop-summary' }, p.summary)];
    if (p.supported) {
      body.push(list('Interpretation and assumptions', (p.assumptions || []).map(itemText)));
      body.push(list('Unknowns', (p.unknowns || []).map(itemText)));
      if (validation.accepted) {
        const warnings = (validation.warnings || []).filter((d) => d.code !== 'no_op').map((d) => `[${d.code}] ${d.message}`);
        body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Dry-run validation'),
          h('div', { class: 'inline-msg ok' }, validation.no_op ? 'Accepted, but it would change nothing (no-op).' : 'Accepted by the command validator against this revision.'),
          warnings.length ? h('ul', null, warnings.map((w) => h('li', null, w))) : null));
      } else {
        body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Dry-run validation'),
          (validation.diagnostics || []).map((d) => h('div', { class: `diag ${d.severity}` }, h('code', null, d.code), d.message))));
      }
      body.push(h('details', { class: 'raw', open: true }, h('summary', null, `Structured command payload (${(p.commands || []).length})`),
        h('pre', { class: 'payload' }, JSON.stringify(p.commands, null, 2))));
    } else {
      body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Reason'), h('div', null, p.reason || '')));
      if (Array.isArray(p.examples) && p.examples.length) {
        body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Supported forms (service examples)'),
          h('div', { class: 'examples' }, p.examples.map((t) => h('button', { class: 'example', type: 'button', onclick: () => fill(t) }, t)))));
      }
    }
    body.push(h('div', { class: 'limitation' }, p.limitation || ''));
    let note = '';
    if (p.applied) note = `Applied as revision ${p.appliedRevision ?? '?'} through the validated command path.`;
    else if (review.stale) note = `Made for revision ${p.expected_revision}; the project is now at ${current}. Review again.`;
    else if (!p.supported) note = 'Nothing will be changed.';
    else if (!validation.accepted) note = 'Validation rejected this change; it cannot be applied.';
    else note = 'Nothing has changed yet.';
    return h('div', { class: `proposal ${p.supported ? '' : 'unsupported'} ${p.applied ? 'applied' : ''}` },
      h('div', { class: 'prop-head' }, verdict, h('span', { class: 'meta' }, `${p.id} · for r${p.expected_revision} · ${p.parser || ''}`)),
      h('div', { class: 'prop-body' }, body),
      h('div', { class: 'prop-foot' }, p.supported ? applyButton(p, review, `Apply → r${p.expected_revision + 1}`) : null, discardButton(p),
        review.stale ? h('button', { class: 'btn small', type: 'button', onclick: () => run(p.text, 'quick') }, `Re-review on r${current}`) : null,
        h('span', { class: 'note' }, note)));
  }

  // ---------------------------------------------------------------- research review

  /** A source title as a link only for a safe public http(s) URL; otherwise plain text, link withheld. */
  function sourceLink(source) {
    if (source.href) {
      return h('a', {
        class: 'src-link', href: source.href, target: '_blank', rel: 'noopener noreferrer nofollow',
        referrerpolicy: 'no-referrer', title: `${source.url}\nOpens the public source in a new tab`,
      }, source.title);
    }
    return h('span', { class: 'src-link withheld', title: `Link withheld (${source.linkProblem}): ${source.url}` }, source.title);
  }

  function sourceRefs(ids) {
    return ids.map((id) => h('span', { class: 'sref mono', title: `Research source ${id}` }, id));
  }

  /** The recorded search queries, as plain text: what the research stage actually searched for. */
  function renderSearches(searches) {
    if (!searches) return null;
    if (searches.missing) {
      return h('div', { class: 'rs-searches' }, h('b', null, 'Search queries'),
        h('small', null, 'The run reports searches, but no query records reached this proposal.'));
    }
    const total = searches.records.length + searches.more;
    return h('div', { class: 'rs-searches' },
      h('b', null, `Search queries as recorded (${total})`),
      h('ol', null, searches.records.map((s) => h('li', null,
        s.query === null ? h('i', null, 'no query recorded') : h('span', { class: 'rs-query' }, s.query),
        h('small', null, [s.status, s.links != null ? `${s.links} link${s.links === 1 ? '' : 's'}` : null,
          s.clipped === 'recorded' ? 'clipped to 100 characters when recorded'
            : s.clipped === 'display' ? `first ${QUERY_SHOWN} characters shown` : null].filter(Boolean).join(' · '))))),
      searches.more ? h('small', null, `${searches.more} more not shown`) : null);
  }

  /** Whether the model will keep this research's references once the plan is applied (as the service states). */
  function renderPersistence(persistence) {
    if (!persistence) return null;
    if (persistence.status === 'proposal_only') {
      return h('div', { class: 'rs-persist proposal-only' }, h('b', null, 'Not saved with the project. '),
        persistence.note || 'No assumption this plan records cites the research, so nothing in the model will cite it.');
    }
    const n = persistence.assumptionIds.length;
    return h('div', { class: 'rs-persist' },
      `Applying records the research identity and source URLs in ${n} assumption source${n === 1 ? '' : 's'}: `,
      persistence.assumptionIds.map((id, i) => [i ? ', ' : '', h('code', null, id)]));
  }

  function renderResearch(research) {
    if (!research) return null;
    if (!research.verified) {
      return h('div', { class: 'prop-block research failed' },
        h('h6', null, `Equipment research · ${research.status}`),
        research.error ? h('div', { class: 'diag error' }, h('code', null, research.error.code), research.error.message) : null,
        h('div', { class: 'limitation' }, 'Research was requested, so no plan was made without verified sources. Nothing can be applied.'));
    }
    const n = research.sources.length;
    return h('div', { class: 'prop-block research' },
      h('h6', null, `Equipment research · ${n} source${n === 1 ? '' : 's'} · untrusted text evidence`),
      research.equipmentType ? h('div', { class: 'rs-type' }, research.equipmentType) : null,
      research.summary ? h('p', { class: 'rs-summary' }, research.summary) : null,
      h('ol', { class: 'rs-sources' }, research.sources.map((s) => h('li', null,
        h('div', { class: 'rs-src' }, h('span', { class: 'sref mono' }, s.id), sourceLink(s), s.host ? h('small', null, s.host) : null),
        s.observations.length ? h('ul', null, s.observations.map((o) => h('li', null, o))) : null))),
      research.features.length ? h('div', { class: 'rs-group' }, h('b', null, 'Sourced features'),
        h('ul', null, research.features.map((f) => h('li', null, f.text, ' ', sourceRefs(f.sourceIds))))) : null,
      research.assumptions.length ? h('div', { class: 'rs-group' }, h('b', null, 'Research assumptions (illustrative, not from sources)'),
        h('ul', null, research.assumptions.map((a) => h('li', null, a)))) : null,
      research.unknowns.length ? h('div', { class: 'rs-group' }, h('b', null, 'Not established by the sources'),
        h('ul', null, research.unknowns.map((u) => h('li', null, u)))) : null,
      research.warnings.length ? h('ul', { class: 'rs-warnings' }, research.warnings.map((w) => h('li', null, w))) : null,
      renderPersistence(research.persistence),
      h('div', {
        class: 'rs-id mono',
        title: research.persistence && research.persistence.status === 'proposal_only'
          ? 'Research identity; kept only in this proposal, no assumption cites it'
          : 'Research identity; applied assumptions cite it with their source URLs',
      }, `${research.id}${research.briefSha ? ` · brief ${research.briefSha.slice(0, 12)}` : ''}${research.elapsed != null ? ` · ${fmtNum(research.elapsed, 1)} s` : ''}`),
      research.facts.length || research.searches ? h('details', { class: 'raw' }, h('summary', null, 'Research run'),
        research.facts.length ? h('div', { class: 'kv-grid mono facts' }, research.facts.flatMap(([k, v]) => [h('span', null, k), h('span', null, v)])) : null,
        renderSearches(research.searches)) : null);
  }

  // ---------------------------------------------------------------- engineering card

  function renderEngineering(p, review) {
    const project = app.state.project;
    const validation = p.validation || {};
    const research = reviewResearch(p);
    let [tone, label] = VERDICT[review.verdict] || ['no', review.verdict];
    if (review.verdict !== 'ready' && p.stale === true) label = 'Stale: project changed';
    else if (research && !research.verified) {
      label = { rejected: 'Research rejected', cancelled: 'Research cancelled' }[research.status] || 'Research unavailable';
    }
    const planner = p.planner && typeof p.planner === 'object' ? p.planner : {};
    const who = plannerHeadline(planner);
    const count = review.operationCount;
    const head = h('div', { class: 'prop-head' },
      h('span', { class: `verdict ${tone}` }, label),
      review.verdict === 'ready' ? h('span', { class: 'opcount' }, `${count} operation${count === 1 ? '' : 's'} → 1 batch`) : null,
      research && research.verified && p.stale !== true ? h('span', { class: 'opcount researched', title: 'Planned with a verified research brief' }, 'researched') : null,
      h('span', { class: 'meta' }, `${p.id} · for r${p.expected_revision}${who ? ` · ${who}` : ''}`));
    const body = [h('div', { class: 'prop-summary' }, p.summary || '(no summary)')];
    body.push(renderResearch(research));

    if (review.questions.length) {
      const ids = questionIds(review.questions);
      body.push(h('div', { class: 'prop-block questions' }, h('h6', null, `Questions to answer (${review.questions.length})`),
        h('ol', null, review.questions.map((q, i) => h('li', null,
          h('code', { class: 'qid', title: 'Answer slot name in the follow-up request' }, ids[i]), ' ',
          h('b', null, itemText(q)), q && q.reason ? h('small', null, q.reason) : null))),
        h('button', {
          class: 'btn small', type: 'button',
          title: 'Switch to Engineering plan and fill a new request: your original text plus one answer slot per question ID',
          onclick: () => {
            setMode('engineering'); // the follow-up is an engineering request whichever mode is selected now
            const next = followUp(p);
            fill(next.text, next.ids);
          },
        }, 'Answer in a new request')));
    }
    if (review.verdict !== 'ready' && review.verdict !== 'questions') {
      body.push(h('div', { class: 'prop-block refusal' }, h('h6', null, 'Why nothing can be applied'), h('div', null, p.reason || review.reasons[0] || '')));
    }
    body.push(list('Reasoning (planner)', (p.reasoning || []).map(itemText)));

    const { existing, created } = review.affected;
    if (existing.length || created.length) {
      body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Affected items (from the exact operations)'),
        h('div', { class: 'chips' },
          existing.map((r) => tagChip(r.id, r.tag, r.how.join(', '))),
          created.map((c) => h('span', { class: 'tchip new', title: `${c.id} · ${c.what}` }, `+ ${c.tag || c.id}`)))));
    }
    const impacts = Array.isArray(p.impacts) ? p.impacts : [];
    if (impacts.length) {
      body.push(h('div', { class: 'prop-block' }, h('h6', null, `Impacts (${impacts.length})`),
        h('ul', { class: 'impacts' }, impacts.map((i) => h('li', null,
          i && i.source ? h('span', { class: `src src-${i.source}` }, i.source) : null,
          h('span', null, itemText(i)), ' ', refChips(project, itemRefs(i)))))));
    }
    const proposed = Array.isArray(p.proposed_assumptions) && p.proposed_assumptions.length ? p.proposed_assumptions : (p.assumptions || []);
    const cites = assumptionCitations(p);
    body.push(list('Proposed assumptions', proposed.map((a) => (a && typeof a === 'object'
      ? [`${a.id || a.key ? `${a.id || a.key}: ` : ''}${itemText(a)}${'recorded' in a ? (a.recorded ? ' (recorded on apply)' : ' (not referenced; not recorded)') : ''}`,
        cites.get(a.id || a.key) ? [' ', h('span', { class: 'cites' }, 'cites ', sourceRefs(cites.get(a.id || a.key)))] : null]
      : itemText(a)))));
    if (review.addedAssumptions.length) {
      body.push(list('Assumption records the operations add', review.addedAssumptions.map((a) => `op ${a.index + 1} · ${a.id}: ${a.description || ''}${a.source ? ` (${a.source})` : ''}`)));
    }
    body.push(list('Unknowns', (p.unknowns || []).map(itemText)));

    if (review.operations.length) {
      body.push(h('div', { class: 'prop-block' }, h('h6', null, `Exact operations (${review.operationCount}) in one batch envelope`),
        h('ol', { class: 'ops' }, review.operations.map((op) => h('li', null,
          h('code', { class: 'optype' }, op.type),
          h('span', null, op.lines.join(' ')),
          op.purpose ? h('small', { class: 'purpose' }, `Planner's purpose: ${op.purpose}`) : null)))));
    }
    // validation: dry run only; geometry is never claimed unless the service says it was checked
    const vRows = [];
    if (validation.accepted) {
      vRows.push(h('div', { class: 'inline-msg ok' }, validation.no_op ? 'Accepted by the command validator, but it would change nothing (no-op).'
        : `Dry run accepted by the command validator against revision ${(validation.checked_against && validation.checked_against.revision) ?? p.expected_revision}.`));
    } else if (review.verdict !== 'questions') {
      vRows.push(h('div', { class: 'inline-msg err' }, 'Not accepted: no command will be sent.'));
    }
    for (const d of validation.diagnostics || []) {
      const where = Number.isInteger(d.operation_index) ? `op ${d.operation_index + 1} · ` : Number.isInteger(d.index) ? `op ${d.index + 1} · ` : '';
      vRows.push(h('div', { class: `diag ${d.severity || 'error'}` }, h('code', null, `${where}${d.code}`), d.message));
    }
    for (const d of (validation.warnings || []).filter((w) => w.code !== 'no_op')) vRows.push(h('div', { class: 'diag warning' }, h('code', null, d.code), d.message));
    vRows.push(h('div', { class: `geom ${review.geometryChecked ? 'ok' : 'unchecked'}` },
      h('b', null, review.geometryChecked ? 'Geometry checked' : 'Geometry not checked'),
      h('span', null, review.geometryChecked ? (validation.geometry_note || 'The service reports a geometry check for this plan.')
        : (validation.geometry_note || 'The dry run validates the engineering model only. CAD solids, STEP export and reimport run in the build after apply; a failure there is a failed build and the last complete outputs are kept.'))));
    body.push(h('div', { class: 'prop-block' }, h('h6', null, 'Validation'), vRows));

    if (p.commands && p.commands.length) {
      body.push(h('details', { class: 'raw' }, h('summary', null, `Exact command envelope (${review.commandCount} command · ${count} operation${count === 1 ? '' : 's'})`),
        h('pre', { class: 'payload' }, JSON.stringify(p.commands, null, 2))));
    }
    const facts = plannerFacts(planner);
    if (facts.length) {
      body.push(h('details', { class: 'raw' }, h('summary', null, 'Planner run'),
        h('div', { class: 'kv-grid mono facts' }, facts.flatMap(([k, v]) => [h('span', null, k), h('span', null, v)]))));
    }
    if (p.expires_at && !p.applied) body.push(h('div', { class: 'limitation' }, `Valid until ${new Date(p.expires_at).toLocaleTimeString()} and only for revision ${p.expected_revision}.`));
    body.push(h('div', { class: 'limitation' }, p.limitation || ''));

    let note;
    if (p.applied) note = `Applied as revision ${p.appliedRevision ?? '?'}: one revision, one undo step reverts the whole plan.`;
    else if (review.canApply) note = 'Nothing has changed yet. Apply commits all operations together as one revision and one undo step, then rebuilds once.';
    else note = review.reasons[0] || 'Nothing will be changed.';
    const current = project.revision;
    return h('div', { class: `proposal engineering v-${review.verdict} ${p.applied ? 'applied' : ''}` }, head,
      h('div', { class: 'prop-body' }, body),
      h('div', { class: 'prop-foot' },
        review.verdict === 'ready' ? applyButton(p, review, `Apply plan (${count} op${count === 1 ? '' : 's'}) → r${p.expected_revision + 1}`) : null,
        discardButton(p),
        (review.stale || review.expired) && !p.applied ? h('button', {
          class: 'btn small', type: 'button',
          title: research ? 'Researches and plans again on the current revision' : 'Runs the planner again on the current revision',
          onclick: () => run(p.text, 'engineering', { research: !!research }),
        }, `Re-plan on r${current}`) : null,
        h('span', { class: 'note' }, note)));
  }

  function applyButton(p, review, label) {
    const btn = h('button', { class: 'btn accent', type: 'button', disabled: !review.canApply || null }, p.applied ? 'Applied' : label);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const before = app.state ? app.state.history.undo : null;
      const r = await app.applyProposal(p);
      if (r.ok) {
        const after = r.state.history.undo;
        proposal = { ...p, applied: true, appliedRevision: r.state.project.revision };
        const ops = review.operationCount;
        app.toast('ok', `Applied ${p.id}`, [
          `Revision ${p.expected_revision} → ${r.state.project.revision}; regeneration started.`,
          review.engineering ? `${ops} operation${ops === 1 ? '' : 's'} as one batch${before != null && after != null ? `; undo steps ${before} → ${after}` : ''}.` : null,
        ]);
      }
      renderProposal();
    });
    return btn;
  }

  function discardButton(p) {
    return h('button', { class: 'btn ghost', type: 'button', onclick: () => { proposal = null; renderProposal(); } }, p.applied ? 'Close' : 'Discard');
  }

  function renderProposal() {
    changed();
    if (!proposal) {
      shownKey = null;
      replace(result);
      return;
    }
    const review = reviewProposal(proposal, app.state, requestedMode);
    shownKey = `${review.stale}|${review.expired}|${proposal.applied}`;
    replace(result, review.engineering || requestedMode === 'engineering' ? renderEngineering(proposal, review) : renderQuick(proposal, review));
  }

  propose.addEventListener('click', () => run());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && mode === 'quick') {
      e.preventDefault();
      run();
    } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      run();
    }
  });
  input.addEventListener('input', () => {
    updateBudget();
    changed();
  });
  setMode('quick');
  modeNote.textContent = 'local · deterministic · one change';
  updateBudget();

  function renderAvailability(state) {
    planning = state.planning && typeof state.planning === 'object' ? state.planning : null;
    const offered = !!planning && Array.isArray(planning.modes) && planning.modes.includes('engineering');
    const engineeringButton = modeButtons.find((b) => b.dataset.mode === 'engineering');
    const planner = planning && planning.planner ? planning.planner : {};
    engineeringButton.disabled = !offered;
    engineeringButton.title = offered
      ? `Planner ${plannerHeadline(planner) || '(not described)'} via the local Claude Code CLI and Claude Max${planner.cli_present === false ? ' (CLI not found: plans will come back unavailable)' : ''}`
      : 'This service does not offer engineering planning';
    if (!offered && mode === 'engineering' && !busy) setMode('quick');
    const available = offered && researchOffered(state);
    if (available !== researchAvailable) {
      researchAvailable = available;
      renderResearchOption();
      changed();
    }
  }

  return {
    update(state) {
      renderAvailability(state);
      renderExamples(state);
      if (!busy) renderProgress();
      // re-render only when stale/expired/applied flips, so the card is not rebuilt on every poll
      if (proposal) {
        const review = reviewProposal(proposal, state, requestedMode);
        if (`${review.stale}|${review.expired}|${proposal.applied}` !== shownKey) renderProposal();
      }
    },
    current: () => proposal,
    setMode,
    root,
    /** The request text as typed (nothing is sent). */
    draft: () => input.value,
    /** Replace the request text without sending it; follow-up answer slots no longer apply. */
    setDraft(text) {
      fill(String(text ?? ''));
    },
    focusInput: () => input.focus({ preventScroll: true }),
    /**
     * Another project or service run: stop waiting for a running request, drop the proposal and
     * the draft (none of them belongs to the new project) and say so. Nothing is sent.
     */
    reset(note) {
      if (busy) busy.controller.abort();
      busy = null;
      clearInterval(ticker);
      ticker = null;
      proposal = null;
      input.value = '';
      slotIds = [];
      renderResearchOption();
      renderProposal();
      renderProgress();
      if (note) replace(result, h('div', { class: 'inline-msg' }, note));
      updateBudget();
      changed();
    },
    /** Compact facts for entry points: draft length, running request and proposal verdict. */
    summary() {
      const review = proposal && app.state ? reviewProposal(proposal, app.state, requestedMode) : null;
      return {
        mode,
        draftLength: input.value.trim().length,
        research: researching(),
        busy: busy ? { mode: busy.mode, research: busy.research, seconds: Math.floor((Date.now() - busy.started) / 1000), revision: busy.revision } : null,
        proposal: proposal ? { id: proposal.id, applied: !!proposal.applied, verdict: review ? review.verdict : null, canApply: !!(review && review.canApply), stale: !!(review && (review.stale || review.expired)) } : null,
      };
    },
    snapshot: () => {
      const review = proposal && app.state ? reviewProposal(proposal, app.state, requestedMode) : null;
      return {
        mode,
        research: { offered: researchAvailable, checked: researchInput.checked, requested: researching() },
        busy: busy ? { mode: busy.mode, research: busy.research, stage: progress.dataset.stage || null, seconds: fmtNum((Date.now() - busy.started) / 1000, 1) } : null,
        proposal: proposal ? { id: proposal.id, mode: proposal.mode || 'quick', applied: !!proposal.applied } : null,
        review: review ? { verdict: review.verdict, canApply: review.canApply, operations: review.operationCount, reasons: review.reasons } : null,
        request: (() => {
          const check = preflight(input.value, mode, slotIds);
          return { length: check.budget.length, limit: check.budget.limit, ok: check.ok, problem: check.budget.problem,
            emptySlots: check.emptySlots, slots: slotIds.slice(), message: check.message };
        })(),
      };
    },
  };
}
