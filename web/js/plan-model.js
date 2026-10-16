// Pure review model for feedback proposals (no DOM, no network), quick and engineering modes.
// The service is the authority: it dry-runs every proposal through apply_command and stores
// only checked proposals. This module only decides what the review card states and whether
// Apply may be offered, and it refuses whenever the reply is not a complete, current,
// question-free plan of exactly one batch envelope.

import { describeCommand, kindLabel } from './commands-model.js';

export const MODES = ['quick', 'engineering'];
export const MAX_BATCH_OPERATIONS = 12;

/** Readable text of a proposal list item, which may be a string or a small record. */
export function itemText(item) {
  if (item == null) return '';
  if (typeof item === 'string') return item;
  if (typeof item !== 'object') return String(item);
  for (const key of ['text', 'question', 'effect', 'description', 'summary', 'purpose', 'message', 'note', 'statement']) {
    if (typeof item[key] === 'string' && item[key].trim()) return item[key];
  }
  return JSON.stringify(item);
}

/** Stable IDs a list item refers to (affects / semantic_ids / entity_ids / ids / target_id). */
export function itemRefs(item) {
  if (!item || typeof item !== 'object') return [];
  const out = [];
  for (const key of ['affects', 'affected', 'semantic_ids', 'entity_ids', 'ids', 'items']) {
    if (Array.isArray(item[key])) for (const v of item[key]) if (typeof v === 'string') out.push(v);
  }
  for (const key of ['target_id', 'semantic_id', 'entity_id']) if (typeof item[key] === 'string') out.push(item[key]);
  return [...new Set(out)];
}

/** Operations of a proposal's single batch envelope (or the single command as one operation). */
export function proposalOperations(p) {
  const commands = Array.isArray(p && p.commands) ? p.commands : [];
  if (commands.length !== 1) return [];
  const cmd = commands[0];
  if (cmd && cmd.type === 'batch') {
    const ops = cmd.payload && Array.isArray(cmd.payload.operations) ? cmd.payload.operations : [];
    return ops.map((op) => ({ type: op && op.type, payload: (op && op.payload) || {}, assumptions: op && op.assumptions }));
  }
  return cmd ? [{ type: cmd.type, payload: cmd.payload || {}, assumptions: cmd.assumptions }] : [];
}

/**
 * Human-readable lines for each operation of the exact envelope (the command editor's wording),
 * joined with the planner's own stated purpose for that operation when the service reports one.
 */
export function describeOperations(ops, project, described = []) {
  const byIndex = new Map((Array.isArray(described) ? described : []).filter((d) => d && Number.isInteger(d.index)).map((d) => [d.index, d]));
  return ops.map((op, index) => {
    let lines;
    try {
      lines = describeCommand({ type: op.type, payload: op.payload, assumptions: op.assumptions }, project);
    } catch {
      lines = [`${op.type} (could not be summarized)`];
    }
    const d = byIndex.get(index);
    return {
      index,
      type: op.type,
      lines,
      purpose: d && typeof d.purpose === 'string' ? d.purpose : null,
      summary: d && typeof d.summary === 'string' ? d.summary : null,
      newAssumptions: d && Array.isArray(d.new_assumptions) ? d.new_assumptions : Object.keys(op.assumptions || {}),
    };
  });
}

function addRef(out, project, id, how) {
  if (typeof id !== 'string' || !id) return;
  const m = project.model;
  let target = id;
  let kind = null;
  if (m.entities[id]) kind = 'entity';
  else if (m.connections[id]) kind = 'connection';
  else if (m.ports[id]) { target = m.ports[id].entity_id; kind = 'entity'; }
  else if (project.pid.symbols[id]) { target = project.pid.symbols[id].entity_id; kind = 'entity'; }
  else if (project.pid.edges[id]) { target = project.pid.edges[id].connection_id; kind = 'connection'; }
  if (!kind) return;
  const rec = out.get(target) || { id: target, kind, tag: (m.entities[target] || m.connections[target]).tag, how: new Set() };
  rec.how.add(how);
  out.set(target, rec);
}

/**
 * Items an operation list touches, resolved by stable ID to current tags, plus new items it
 * creates (by the IDs and tags the operations declare). Derived from the operations themselves.
 */
export function affectedItems(ops, project) {
  const existing = new Map();
  const created = [];
  for (const op of ops) {
    const p = op.payload || {};
    switch (op.type) {
      case 'rename_entity': addRef(existing, project, p.entity_id, 'renamed'); break;
      case 'set_parameter': addRef(existing, project, p.entity_id, `parameter ${p.name}`); break;
      case 'move_equipment':
      case 'place_equipment': addRef(existing, project, p.entity_id, 'physical placement'); break;
      case 'move_symbol': addRef(existing, project, p.symbol_id, 'P&ID symbol'); break;
      case 'set_route': addRef(existing, project, p.connection_id, 'physical route'); break;
      case 'set_diagram_route': addRef(existing, project, p.edge_id, 'diagram path'); break;
      case 'set_port_size': addRef(existing, project, p.port_id, 'port size'); break;
      case 'insert_valve':
      case 'insert_equipment': {
        addRef(existing, project, p.connection_id, 'split by insertion');
        const conn = project.model.connections[p.connection_id];
        if (conn && project.model.ports[conn.to_port_id]) addRef(existing, project, conn.to_port_id, 'reconnected downstream');
        if (p.entity && typeof p.entity.id === 'string') {
          created.push({ id: p.entity.id, tag: p.entity.tag, kind: 'entity', what: kindLabel(p.entity) });
        }
        const down = p.downstream_connection;
        if (down && typeof down.id === 'string') created.push({ id: down.id, tag: down.tag, kind: 'connection', what: 'process line' });
        break;
      }
      case 'add_equipment': {
        if (p.entity && typeof p.entity.id === 'string') {
          created.push({ id: p.entity.id, tag: p.entity.tag, kind: 'entity', what: kindLabel(p.entity) });
        }
        break;
      }
      case 'connect_ports': {
        const c = p.connection || {};
        addRef(existing, project, c.from_port_id, 'connected (outlet side)');
        addRef(existing, project, c.to_port_id, 'connected (inlet side)');
        if (typeof c.id === 'string') created.push({ id: c.id, tag: c.tag, kind: 'connection', what: 'process line' });
        break;
      }
      default: break;
    }
  }
  // once a plan is applied its new items exist too; they stay listed only as created
  const createdIds = new Set(created.map((c) => c.id));
  return {
    existing: [...existing.values()].filter((r) => !createdIds.has(r.id)).map((r) => ({ ...r, how: [...r.how] })),
    created,
  };
}

/** Assumption records the operations would add to the model, flattened with their operation index. */
export function operationAssumptions(ops) {
  const out = [];
  ops.forEach((op, index) => {
    if (op.assumptions && typeof op.assumptions === 'object') {
      for (const [id, rec] of Object.entries(op.assumptions)) out.push({ index, id, ...(rec && typeof rec === 'object' ? rec : { description: String(rec) }) });
    }
  });
  return out;
}

const REFUSALS = ['unavailable', 'rejected', 'unsupported', 'invalid'];

function proposalStatus(p) {
  const planner = p && p.planner && typeof p.planner === 'object' ? p.planner : {};
  return String((p && p.status) || planner.status || '').toLowerCase();
}

/**
 * Everything the review card needs, with the Apply decision and the reasons it is withheld.
 * `state` is the current service state (for the revision check); `requestedMode` is the mode
 * the user asked for, so a reply that did not actually run that mode is never mistaken for it.
 */
export function reviewProposal(p, state, requestedMode = 'quick', now = Date.now()) {
  const project = state ? state.project : null;
  const mode = p && MODES.includes(p.mode) ? p.mode : 'quick';
  const engineering = requestedMode === 'engineering';
  const validation = (p && p.validation) || {};
  const questions = Array.isArray(p && p.questions) ? p.questions.filter((q) => itemText(q).trim()) : [];
  const commands = Array.isArray(p && p.commands) ? p.commands : [];
  const ops = proposalOperations(p || {});
  const stale = !!p && !p.applied && (p.stale === true || (!!project && project.revision !== p.expected_revision));
  const expiresAt = p && typeof p.expires_at === 'string' ? Date.parse(p.expires_at) : NaN;
  const expired = !p.applied && Number.isFinite(expiresAt) && now > expiresAt;
  const status = proposalStatus(p);
  const reasons = [];
  let verdict;
  if (engineering && mode !== 'engineering') {
    verdict = 'not-run';
    reasons.push('The service did not run the engineering planner for this request (its reply is not an engineering plan).');
  } else if (status === 'questions' || questions.length) {
    verdict = 'questions';
    reasons.push(`${questions.length || 'Open'} question(s) must be answered in a new request before anything can be applied.`);
  } else if (!p.supported) {
    verdict = REFUSALS.includes(status) ? status : (mode === 'engineering' ? 'rejected' : 'unsupported');
    reasons.push(p.reason || 'The request is not supported; nothing will be changed.');
  } else if (!validation.accepted) {
    verdict = 'invalid';
    reasons.push('The dry run through the command validator rejected this plan.');
  } else if (mode === 'engineering' && status && status !== 'ready') {
    verdict = 'invalid';
    reasons.push(`The service reports status "${status}", not ready.`);
  } else {
    verdict = 'ready';
  }
  if (p.supported && commands.length !== 1) reasons.push(`Expected exactly one command envelope, received ${commands.length}.`);
  if (mode === 'engineering' && p.supported && commands.length === 1 && commands[0].type !== 'batch') {
    reasons.push('An engineering plan must be one batch envelope.');
  }
  if (mode === 'engineering' && ops.length > MAX_BATCH_OPERATIONS) reasons.push(`More than ${MAX_BATCH_OPERATIONS} operations.`);
  if (validation.no_op) reasons.push('The dry run found that it would change nothing (no-op).');
  if (p.applied) reasons.push('Already applied.');
  if (stale) {
    reasons.push(project && project.revision !== p.expected_revision
      ? `Made for revision ${p.expected_revision}; the project is now at revision ${project.revision}.`
      : 'The service marked this proposal stale.');
  }
  if (expired) reasons.push(`Expired at ${p.expires_at}; request a new plan.`);
  const canApply = verdict === 'ready' && reasons.length === 0;
  return {
    mode,
    engineering: mode === 'engineering',
    verdict,
    status,
    canApply,
    reasons,
    stale,
    expired,
    questions,
    operations: project ? describeOperations(ops, project, p.operations) : [],
    operationCount: ops.length,
    commandCount: commands.length,
    affected: project ? affectedItems(ops, project) : { existing: [], created: [] },
    addedAssumptions: operationAssumptions(ops),
    geometryChecked: validation.geometry_checked === true,
  };
}

const nonEmpty = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);

/**
 * Effort as the service states it: a string is the planner's static configuration (what is
 * requested); runtime metadata is {requested, reported}, and "reported" appears only when the CLI
 * actually reported an effort. Returns '' when nothing is stated.
 */
export function effortText(effort) {
  if (nonEmpty(effort)) return `effort ${nonEmpty(effort)} requested`;
  if (!effort || typeof effort !== 'object') return '';
  const requested = nonEmpty(effort.requested);
  const reported = nonEmpty(effort.reported);
  return [requested ? `effort ${requested} requested` : null, reported ? `${reported} reported` : null].filter(Boolean).join(', ');
}

/** Short planner line for card headers and tooltips: model and effort as stated. */
export function plannerHeadline(planner) {
  if (!planner || typeof planner !== 'object') return '';
  return [nonEmpty(planner.model), effortText(planner.effort)].filter(Boolean).join(' · ');
}

/**
 * Planner facts to display (model, effort, verification, auth source, response models, timing ...),
 * never secrets. Effort is split into requested/reported; lists of scalars are joined ("none" when
 * empty, e.g. tools); small objects of scalars become one fact per field; null values are omitted.
 */
export function plannerFacts(planner) {
  if (!planner || typeof planner !== 'object') return [];
  // credential-like names only: usage counts such as inputTokens and api_key_source stay visible
  const hidden = /secret|password|credential|^token$|^key$|(auth|access|refresh|bearer|session|api)[_-]?(token|key|id)$/i;
  const scalar = (v) => v == null || ['string', 'number', 'boolean'].includes(typeof v);
  const out = [];
  for (const [key, value] of Object.entries(planner)) {
    if (hidden.test(key)) continue;
    const label = key.replace(/_/g, ' ');
    if (value == null) continue;
    if (key === 'effort' && typeof value === 'object') {
      if (nonEmpty(value.requested)) out.push(['effort requested', value.requested]);
      if (nonEmpty(value.reported)) out.push(['effort reported', value.reported]);
    } else if (Array.isArray(value)) {
      if (value.every(scalar)) out.push([label, value.length ? value.map(String).join(', ') : 'none']);
    } else if (typeof value === 'object') {
      for (const [sub, v] of Object.entries(value)) {
        if (v != null && scalar(v) && !hidden.test(sub)) out.push([`${label} ${sub}`, String(v)]);
      }
    } else {
      out.push([label, String(value)]);
    }
  }
  return out;
}

// ---------------------------------------------------------------- request budget

/** Request length limits of the service, in Unicode code points after its whitespace cleaning. */
export const REQUEST_LIMITS = { quick: 500, engineering: 2000 };
// Python str.split()/str.strip() whitespace, which the service uses to clean requests
const WS = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const WS_RUN = new RegExp(`[${WS}]+`, 'g');
const WS_EDGES = new RegExp(`^[${WS}]+|[${WS}]+$`, 'g');
const collapse = (s) => s.replace(WS_RUN, ' ').replace(WS_EDGES, '');

/**
 * The request as the service will see it, and whether it can be sent. Quick requests collapse all
 * whitespace (500 limit); engineering requests collapse whitespace within lines but keep line breaks
 * (2000 limit) and also refuse U+2028/U+2029. Lengths count code points (an emoji is one), never UTF-16
 * units. Returns {mode, text, length, limit, remaining, problem: null|'control'|'empty'|'too_long'}.
 */
export function cleanRequest(text, mode = 'quick') {
  const engineering = mode === 'engineering';
  const limit = engineering ? REQUEST_LIMITS.engineering : REQUEST_LIMITS.quick;
  const raw = typeof text === 'string' ? text : '';
  let problem = null;
  for (const ch of raw) {
    const c = ch.codePointAt(0);
    if ((c < 32 && ch !== '\t' && ch !== '\n' && ch !== '\r') || (engineering && (c === 0x2028 || c === 0x2029))) {
      problem = 'control';
      break;
    }
  }
  const cleaned = engineering
    ? raw.replace(/\r\n/g, '\n').split('\n').map(collapse).join('\n').replace(WS_EDGES, '')
    : collapse(raw);
  const length = [...cleaned].length;
  if (!problem && !length) problem = 'empty';
  if (!problem && length > limit) problem = 'too_long';
  return { mode: engineering ? 'engineering' : 'quick', text: cleaned, length, limit, remaining: limit - length, problem };
}

// ---------------------------------------------------------------- follow-up answers

/** The instruction line between the original request and the answer slots of a follow-up. */
export const FOLLOW_UP_NOTE = 'Answers to the planner\'s questions (a new, self-contained request: the planner does not remember '
  + 'its previous reply, so state each answer in full; the full questions stay in the review card):';

/** Short descriptive slot IDs for the questions: the planner's IDs, made unique; q1, q2 ... when absent. */
export function questionIds(questions) {
  const used = new Set();
  return (Array.isArray(questions) ? questions : []).map((q, i) => {
    const declared = q && typeof q === 'object' && typeof q.id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(q.id) ? q.id : `q${i + 1}`;
    let id = declared;
    for (let n = 2; used.has(id); n += 1) id = `${declared}-${n}`;
    used.add(id);
    return id;
  });
}

/**
 * Follow-up request for a questions reply: the original request kept verbatim, one instruction line and
 * one empty "<question-id>:" slot per question. The question prose is not repeated (it stays visible in
 * the card), so the answers get the budget. Nothing is truncated: when even the empty scaffold is over
 * the engineering limit, `fits` is false and the operator must shorten the request before planning.
 */
export function followUp(p) {
  const original = typeof p.text === 'string' ? p.text : '';
  // the same questions the review card lists (reviewProposal drops items without text), in order
  const ids = questionIds((Array.isArray(p.questions) ? p.questions : []).filter((q) => itemText(q).trim()));
  const text = ids.length ? `${original}\n\n${FOLLOW_UP_NOTE}\n${ids.map((id) => `${id}: `).join('\n')}` : original;
  const budget = cleanRequest(text, 'engineering');
  return { text, ids, budget, fits: budget.problem !== 'too_long' };
}

export function followUpText(p) {
  return followUp(p).text;
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Answer slots of a follow-up that are still empty ("<id>:" with nothing after it on its line). */
export function emptySlots(text, ids = []) {
  return ids.filter((id) => new RegExp(`^[ \\t]*${escapeRe(id)}[ \\t]*:[ \\t]*$`, 'm').test(String(text)));
}

/**
 * Decide whether a request may be sent, with a message for the operator. Requests that are empty, refused
 * by the service's cleaning, over its limit, or follow-ups with unanswered slots are never sent.
 */
export function preflight(text, mode = 'quick', slotIds = []) {
  const budget = cleanRequest(text, mode);
  const empty = budget.problem ? [] : emptySlots(text, slotIds);
  let message = '';
  if (budget.problem === 'control') {
    message = mode === 'engineering' ? 'Contains control or line/paragraph-separator characters the service refuses; remove them.'
      : 'Contains control characters the service refuses; remove them.';
  } else if (budget.problem === 'too_long') {
    message = `Over the ${budget.limit}-character ${mode === 'engineering' ? 'engineering' : 'quick-edit'} limit by ${-budget.remaining}. `
      + `${slotIds.length ? 'The original request is kept unchanged: shorten it, the answers, or remove answer slots' : 'Shorten the request'}; `
      + 'nothing is sent until it fits.';
  } else if (empty.length) {
    message = `${empty.length} answer slot${empty.length === 1 ? ' is' : 's are'} empty (${empty.join(', ')}): answer ${empty.length === 1 ? 'it' : 'them'} or delete ${empty.length === 1 ? 'that line' : 'those lines'}.`;
  }
  return { ok: !budget.problem && !empty.length, budget, emptySlots: empty, message };
}
