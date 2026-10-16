// Inspector: stable IDs, current tags, typed dimensions and placement/layout editors.
// Every edit becomes one existing command envelope (fresh command ID, current
// expected_revision) sent through /api/command; the service validates it.

import { h, replace, icon, copyText } from './dom.js';
import { diagnosticsFor, shortBuild } from './status.js';
import { offeredSources } from './view-source.js';
import {
  buildMoveSymbol, buildPlacement, buildRename, buildSetParameter, fmtNum, humanize, incidentConnections, isComposed,
  kindLabel, parameterNames, recipeRange, tagInUse,
} from './commands-model.js';
import { rerouteOffer } from './reroute-model.js';
import { displayName } from './project-name.js';

function rangeText(range) {
  if (!range || (range.minimum == null && range.maximum == null)) return '';
  const unit = range.unit || 'm';
  if (range.minimum != null && range.maximum != null) return `${fmtNum(range.minimum, 6)}–${fmtNum(range.maximum, 6)} ${unit}`;
  return range.minimum != null ? `≥ ${fmtNum(range.minimum, 6)} ${unit}` : `≤ ${fmtNum(range.maximum, 6)} ${unit}`;
}

function outsideRange(range, value) {
  if (!range || !Number.isFinite(value)) return false;
  return (range.minimum != null && value < range.minimum) || (range.maximum != null && value > range.maximum);
}

function num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? fmtNum(value, 6) : '';
}

function statusChip(label, status) {
  const warn = status && status !== 'current' && status !== 'placed';
  return h('span', { class: `chip ${warn ? 'warn' : ''}` }, h('b', null, label), status || '—');
}

function section(title, meta, open, ...body) {
  const d = h('details', { class: 'sec', open: open || null }, h('summary', null, title, meta ? h('span', { class: 'sum-meta' }, meta) : null),
    h('div', { class: 'sec-body' }, ...body));
  return d;
}

function numberInput(value, attrs = {}) {
  const input = h('input', { type: 'number', step: attrs.step || 'any', value: num(value), 'data-field': attrs.field, 'aria-label': attrs.label || attrs.field });
  input.dataset.original = num(value);
  input.addEventListener('input', () => input.classList.toggle('changed', input.value !== input.dataset.original));
  return input;
}

function readNumber(input) {
  if (input.value.trim() === '') return NaN;
  const v = Number(input.value);
  return Number.isFinite(v) ? v : NaN;
}

export function createInspector(app, root) {
  let ids = [];
  let renderedKey = null;
  // installed companion data (app.docData), each {data, src, url}; identity was checked on install
  const docs = {
    get index() { return app.docData.index ? app.docData.index.data : null; },
    get indexSrc() { return app.docData.index ? app.docData.index.src : null; },
    get desc() { return app.docData.descriptionJson ? app.docData.descriptionJson.data : null; },
    get descEntry() { return app.docData.descriptionJson; },
  };

  function key() {
    const s = app.state;
    if (!s) return null;
    const offered = docs.descEntry ? offeredSources(s).has(docs.descEntry.src.url) : null;
    return `${s.project.revision}|${ids.join(',')}|${app.docData.index ? app.docData.index.url : ''}|` +
      `${docs.descEntry ? docs.descEntry.url : ''}|${offered}|${s.diagnostics.length}|${s.selection.sequence}|` +
      `${displayName(s.project)}|${app.rerouteVersion}|${app.online}|${s.sync ? s.sync.state : ''}|${s.published ? s.published.build_id : ''}`;
  }

  function render(force = false) {
    const s = app.state;
    if (!s) return;
    const k = key();
    if (!force && k === renderedKey) return;
    renderedKey = k;
    // keep every edited field (focused or not, e.g. focus already on its Rename/Apply button) whose
    // value underneath is unchanged; its own checks run again on the recreated form
    const active = document.activeElement;
    const focused = active && root.contains(active) && active.dataset.field ? active.dataset.field : null;
    const drafts = [...root.querySelectorAll('[data-field]')]
      .filter((el) => el.value !== el.dataset.original || el.dataset.field === focused)
      .map((el) => ({ field: el.dataset.field, value: el.value, original: el.dataset.original }));
    const scroll = root.scrollTop;
    const project = s.project;
    const valid = ids.filter((id) => id in project.model.entities || id in project.model.ports || id in project.model.connections);
    let content;
    if (!valid.length) content = overview(s);
    else content = [valid.length > 1 ? multiHeader(s, valid) : null, detail(s, valid[0])];
    replace(root, content);
    for (const keep of drafts) {
      const el = root.querySelector(`[data-field="${CSS.escape(keep.field)}"]`);
      if (!el || el.dataset.original !== keep.original) continue; // the value changed underneath: no rebase
      if (el.value !== keep.value) {
        el.value = keep.value;
        el.dispatchEvent(new Event('input', { bubbles: true })); // changed mark, tag check and hints follow the draft
      }
      if (keep.field === focused) el.focus();
    }
    root.scrollTop = scroll;
  }

  // ---------------------------------------------------------------- pieces

  function tagButton(id, label) {
    return h('button', { class: 'tag-link', type: 'button', onclick: () => app.select([id], 'inspector') }, label);
  }

  function tagOf(project, id) {
    const m = project.model;
    if (m.entities[id]) return m.entities[id].tag;
    if (m.connections[id]) return m.connections[id].tag;
    if (m.ports[id]) return `${m.entities[m.ports[id].entity_id]?.tag ?? '?'}.${m.ports[id].name}`;
    return id;
  }

  function indexItem(id) {
    return docs.index && Array.isArray(docs.index.items) ? docs.index.items.find((i) => i.semantic_id === id) : null;
  }

  function descItem(id) {
    return docs.desc && Array.isArray(docs.desc.items) ? docs.desc.items.find((i) => i.semantic_id === id) : null;
  }

  function resolvedFor(s, id) {
    return (s.selection.resolved || []).find((r) => r.id === id) || null;
  }

  function header(s, id, kicker, title) {
    const project = s.project;
    const resolved = resolvedFor(s, id);
    const chips = [];
    const item = indexItem(id);
    const indexRev = docs.indexSrc ? docs.indexSrc.source_revision : null;
    const indexBehind = indexRev !== null && indexRev !== project.revision;
    if (item) {
      for (const o of item.occurrences || []) {
        chips.push(h('span', {
          class: `chip link ${indexBehind ? 'warn' : ''}`, onclick: () => app.focusSelection(),
          title: `${o.occurrence_id} at paper (${o.position.join(', ')}) · from item index r${indexRev}${indexBehind ? ` (project is r${project.revision})` : ''}`,
        }, h('b', null, 'P&ID'), `${o.zone || o.ref}${indexBehind ? ` · r${indexRev}` : ''}`));
      }
    } else if (resolved && resolved.pid_occurrences && resolved.pid_occurrences.length) {
      chips.push(h('span', { class: 'chip' }, h('b', null, 'P&ID'), resolved.pid_occurrences.join(', ')));
    }
    // 3D: an explicit [] from the verified manifest is a known absence; null is unknown.
    const comps = app.viewer ? app.viewer.componentsFor(id) : [];
    const shownBuild = app.viewer && app.viewer.source ? app.viewer.source.build_id : null;
    const mapped = resolved && Array.isArray(resolved.display_components) && resolved.display_build_id === shownBuild
      ? resolved.display_components : null;
    if (mapped && mapped.length === 0) {
      chips.push(h('span', { class: 'chip warn', title: resolved.absent_3d_reason || 'no 3D representation in the verified display mapping' }, h('b', null, '3D'), 'absent'));
    } else if (mapped) {
      chips.push(h('span', { class: 'chip link', title: mapped.join('\n'), onclick: () => app.viewer && app.viewer.focusSelection() }, h('b', null, '3D'), `${mapped.length} component${mapped.length === 1 ? '' : 's'}`));
    } else if (comps.length) {
      chips.push(h('span', { class: 'chip link', title: `${comps.join('\n')}\n(from the scene shown; the verified display mapping is unavailable)`, onclick: () => app.viewer && app.viewer.focusSelection() },
        h('b', null, '3D'), `${comps.length} in shown scene · mapping unverified`));
    } else if (app.viewer && app.viewer.hasScene) {
      chips.push(h('span', { class: 'chip warn', title: 'The verified display mapping is unknown and the scene shown has no component for this ID.' }, h('b', null, '3D'), 'mapping unknown'));
    }
    const copy = h('button', { type: 'button', title: 'Copy stable ID', onclick: (e) => copyText(id, e.currentTarget) }, icon('copy'));
    return h('div', { class: 'insp-head' },
      h('div', { class: 'insp-kicker' }, kicker, h('span', { class: 'chip' }, h('b', null, 'rev'), String(project.revision))),
      h('div', { class: 'insp-tag' }, title),
      h('div', { class: 'insp-id mono' }, id, copy),
      chips.length ? h('div', { class: 'insp-map' }, chips) : null);
  }

  function multiHeader(s, valid) {
    return h('div', { class: 'insp-head' },
      h('div', { class: 'insp-kicker' }, `${valid.length} items selected`),
      h('ul', { class: 'list' }, valid.map((id) => h('li', null, tagButton(id, tagOf(s.project, id)), h('small', { class: 'mono' }, id)))),
      h('p', { class: 'sec-note' }, 'Shift-click in either view adds or removes items. Details below are for the first item.'));
  }

  function descriptionSection(s, id) {
    const item = descItem(id);
    if (!docs.desc) return section('Explanation', 'not loaded', false, h('p', { class: 'sec-note' }, 'No generated description is available yet.'));
    const srcRev = docs.desc.source_revision;
    const behind = srcRev !== s.project.revision;
    const offered = offeredSources(s).has(docs.descEntry.src.url);
    const meta = `description r${srcRev}${behind ? ' · stale' : ''}${offered ? '' : ' · no longer offered'}`;
    if (!item) return section('Explanation', meta, false, h('p', { class: 'sec-note' }, `The description of revision ${srcRev} has no entry for this item${behind ? ' (it may be newer than that description)' : ''}.`));
    const parts = (item.function && item.function.parts) || [];
    const text = h('p', { class: 'desc-text' }, parts.map((p) => (p.ref
      ? h('span', { class: 'tref', title: p.ref, onclick: () => app.select([p.ref], 'inspector') }, p.tag)
      : p.text)));
    const assumptions = (item.assumption_ids || []).map((aid) => {
      const rec = s.project.model.assumptions[aid];
      return h('li', null, h('span', null, rec ? rec.description : '(not in the current model)'), h('small', { class: 'mono' }, aid));
    });
    const unknowns = (item.unknown_ids || []).map((uid) => {
      const u = (docs.desc.unknowns || []).find((x) => x.id === uid);
      return h('li', null, h('span', null, u ? `${u.topic}: ${u.text}` : uid), h('small', { class: 'mono' }, ''));
    });
    const local = (item.definition_assumption_ids || []).map((sid) => {
      const rec = (docs.desc.definition_assumptions || []).find((x) => x.scoped_id === sid);
      return h('li', null, h('span', null, rec ? rec.description : '(not in this description)'), h('small', { class: 'mono' }, rec ? rec.local_id : sid));
    });
    return section('Explanation', meta, true,
      !offered ? h('div', { class: 'diag error' }, h('code', null, 'not offered'), `The service no longer offers this description file (${shortBuild(docs.descEntry.src.build_id)}); it may have been superseded or failed verification.`) : null,
      behind ? h('div', { class: 'diag warning' }, `From the description of revision ${srcRev}; the project is at revision ${s.project.revision}. Tags shown here are as of that revision.`) : null,
      text,
      item.intent_basis ? h('p', { class: 'sec-note' }, item.intent_basis) : null,
      assumptions.length ? h('details', { class: 'raw' }, h('summary', null, `Assumptions (${assumptions.length})`), h('ul', { class: 'list' }, assumptions)) : null,
      local.length ? h('details', { class: 'raw' }, h('summary', null, `Definition-local assumptions (${local.length})`), h('ul', { class: 'list' }, local)) : null,
      unknowns.length ? h('details', { class: 'raw' }, h('summary', null, `Explicit unknowns (${unknowns.length})`), h('ul', { class: 'list' }, unknowns)) : null);
  }

  /** Declared definition of a composed item: identity, authored function, ports, symbol, unknowns, local assumptions. */
  function definitionSection(s, entity) {
    const d = entity.definition;
    const project = s.project;
    const declaredPorts = d.ports && typeof d.ports === 'object' ? Object.entries(d.ports) : [];
    const modelPorts = Object.values(project.model.ports).filter((p) => p.entity_id === entity.id);
    const shape = d.symbol && d.symbol.shape ? String(d.symbol.shape).replace(/_/g, ' ') : '—';
    const unknowns = Array.isArray(d.unknowns) ? d.unknowns.filter((u) => typeof u === 'string' && u.trim()) : [];
    const local = d.geometry && d.geometry.assumptions && typeof d.geometry.assumptions === 'object'
      ? Object.entries(d.geometry.assumptions).sort(([a], [b]) => a.localeCompare(b)) : [];
    const parts = d.geometry && Array.isArray(d.geometry.components) ? d.geometry.components.length : 0;
    return section('Definition', d.id || '', true,
      h('div', { class: 'kv-grid' },
        h('span', { class: 'k' }, 'name'), h('span', null, d.name || '—'),
        h('span', { class: 'k' }, 'id'), h('span', { class: 'mono' }, d.id || '—'),
        h('span', { class: 'k' }, 'schema'), h('span', { class: 'mono' }, d.schema || '—'),
        h('span', { class: 'k' }, 'P&ID outline'), h('span', null, `generic ${shape}`),
        h('span', { class: 'k' }, 'geometry'), h('span', null, `${parts} component${parts === 1 ? '' : 's'} from reusable parts`)),
      typeof d.function === 'string' && d.function.trim()
        ? h('blockquote', { class: 'declared', title: 'Authored definition text; not verified behavior' },
          h('small', null, 'Declared function · authored, not verified'), d.function) : null,
      declaredPorts.length ? h('ul', { class: 'list' }, declaredPorts.map(([name, rec]) => {
        const port = modelPorts.find((p) => p.name === name);
        return h('li', null,
          h('span', null, port ? tagButton(port.id, name) : h('b', null, name),
            h('small', null, ` declared ${rec.direction || '?'} · ${rec.service || '?'}${rec.required === false ? '' : ' · required'}`)),
          port ? h('small', { class: 'mono' }, `DN${port.nominal_diameter_mm}`) : h('small', { class: 'chip warn' }, 'no model port'));
      })) : null,
      unknowns.length ? h('details', { class: 'raw', open: true }, h('summary', null, `Declared unknowns (${unknowns.length})`),
        h('ul', { class: 'list plain' }, unknowns.map((u) => h('li', null, h('span', null, u))))) : null,
      local.length ? h('details', { class: 'raw' }, h('summary', null, `Definition-local assumptions (${local.length})`),
        h('p', { class: 'sec-note' }, 'Recorded in this item\'s definition recipe. They apply to this item only and are not project assumptions.'),
        h('ul', { class: 'list' }, local.map(([lid, rec]) => h('li', null,
          h('span', null, rec && rec.description ? rec.description : '—'),
          h('small', { class: 'mono', title: rec && rec.category ? rec.category : '' }, lid))))) : null);
  }

  function diagnosticsSection(s, ids2) {
    const diags = diagnosticsFor(s, ids2);
    if (!diags.length) return null;
    return section('Open items', String(diags.length), true, diags.map((d) => h('div', { class: `diag ${d.severity}` }, h('code', null, d.code), d.message)));
  }

  /**
   * A pending physical route as a compact Reroute button that keeps its status and reason;
   * a current route stays a plain status chip. After a reroute from this page, the build
   * note reports the five outputs until they are published (or the build fails).
   */
  function rerouteControl(s, connId) {
    const offer = rerouteOffer(s.project, connId);
    const ui = app.rerouteState(connId);
    const rec = ui.record;
    const conn = s.project.model.connections[connId];
    if (!offer.pending) {
      const note = rec && rec.status === 'done' ? ui.note : null;
      const showNote = note && !(note.settled && (note.tone === 'current' || note.tone === 'none'));
      return h('span', { class: 'reroute-cell' }, statusChip('route', offer.status),
        showNote ? h('small', { class: `rr-note tone-${note.tone}`, title: note.text }, note.text) : null);
    }
    const busy = !!rec && rec.status === 'busy';
    const failed = !!rec && rec.status === 'error' && rec.fromRevision === s.project.revision;
    const blocked = !offer.actionable ? offer.why : ui.anyBusy && !busy ? 'Another reroute is running.' : !ui.online ? 'The local service is not reachable.' : null;
    const what = busy ? 'Computing a guided route, saving and regenerating…'
      : blocked || 'Compute a guided route between the current placed endpoints, commit it as one validated revision and regenerate all five outputs.';
    const button = h('button', {
      class: `reroute${busy ? ' busy' : ''}${failed ? ' failed' : ''}`, type: 'button', disabled: busy || !!blocked,
      title: `${offer.status}${offer.reason ? `: ${offer.reason}` : ''}\n${what}${failed ? `\nLast attempt: ${rec.message}` : ''}`,
      'aria-label': `Reroute ${conn ? conn.tag : connId}, route ${offer.status}`, 'aria-busy': busy ? 'true' : null,
      dataset: { connection: connId }, onclick: () => app.reroute(connId),
    }, h('b', null, 'route'), h('span', { class: 'st' }, offer.status.replace(/_/g, ' ')), h('span', { class: 'act' }, icon('route'), busy ? 'Rerouting…' : 'Reroute'));
    return h('span', { class: 'reroute-cell' }, button,
      failed ? h('small', { class: 'rr-note tone-failed', title: rec.message }, rec.message) : null,
      !failed && !offer.actionable ? h('small', { class: 'rr-note' }, offer.why) : null);
  }

  function message(el, kind, text) {
    el.className = `inline-msg ${kind}`;
    el.textContent = text;
  }

  async function send(command, label, msg) {
    if (msg) message(msg, '', 'Sending…');
    const r = await app.submitCommand(command, label);
    if (msg && !r.ok) message(msg, 'err', `${r.error.error}${r.error.diagnostics.length ? ` — ${r.error.diagnostics.map((d) => d.message).join('; ')}` : ''}`);
    else if (msg) message(msg, 'ok', 'Accepted.');
    return r;
  }

  // ---------------------------------------------------------------- entity

  function entityDetail(s, id) {
    const project = s.project;
    const entity = project.model.entities[id];
    const composed = isComposed(entity);
    const out = [header(s, id, h('span', null, `Equipment · ${kindLabel(entity)}`), entity.tag)];

    // rename
    const tagInput = h('input', { type: 'text', value: entity.tag, 'data-field': `tag:${id}`, 'aria-label': 'Tag', maxlength: 40 });
    tagInput.dataset.original = entity.tag;
    const renameMsg = h('div', { class: 'inline-msg' });
    const renameBtn = h('button', { class: 'btn primary', type: 'button' }, 'Rename');
    const checkTag = () => {
      const value = tagInput.value.trim();
      const clash = value && value !== entity.tag ? tagInUse(project, value, id) : null;
      tagInput.classList.toggle('changed', value !== entity.tag);
      renameBtn.disabled = !value || value === entity.tag || !!clash;
      message(renameMsg, clash ? 'err' : '', clash ? `${value} is already used by ${tagOf(project, clash)} (${clash}).` : 'The stable ID, ports, connections and layout are unchanged.');
    };
    tagInput.addEventListener('input', checkTag);
    tagInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !renameBtn.disabled) renameBtn.click(); });
    renameBtn.addEventListener('click', () => send(buildRename(project, id, tagInput.value.trim()), `Rename ${entity.tag}`, renameMsg));
    checkTag();
    out.push(section('Tag', null, true, h('div', { class: 'composer' }, tagInput, renameBtn), renameMsg));

    // dimensions: generic over the entity's own parameters (authoritative current values)
    const names = parameterNames(entity);
    if (names.length) {
      const rows = names.map((name) => {
        const q = entity.parameters[name];
        const range = recipeRange(entity, name);
        const input = numberInput(q.value, { field: `param:${id}:${name}`, step: '0.005', label: humanize(name) });
        const msg = h('div', { class: 'inline-msg' });
        const apply = h('button', { class: 'btn small', type: 'button', title: `set_parameter ${name}` }, 'Set');
        const hint = () => {
          const value = readNumber(input);
          if (outsideRange(range, value)) {
            message(msg, 'err', `Outside the definition's authoring range ${rangeText(range)}; the service or the geometry build may reject it.`);
          } else if (msg.classList.contains('err')) message(msg, '', '');
        };
        const doApply = () => {
          const value = readNumber(input);
          if (!(value > 0)) return message(msg, 'err', 'Enter a positive number of metres.');
          if (value === q.value) return message(msg, '', 'Unchanged.');
          send(buildSetParameter(project, id, name, value), `${entity.tag} ${name}`, msg);
        };
        apply.addEventListener('click', doApply);
        input.addEventListener('input', hint);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doApply(); });
        const assumption = project.model.assumptions[q.assumption_id];
        const provenance = q.assumption_id || q.source || '';
        return [h('div', { class: 'field-row' },
          h('label', { title: `${assumption ? assumption.description : q.source || ''}${range ? `\nDefinition authoring range: ${rangeText(range)}` : ''}` },
            humanize(name), h('small', null, range ? `${rangeText(range)} · ${provenance}` : provenance)),
          h('div', { class: 'num' }, input, h('i', null, q.unit)),
          apply), msg];
      });
      out.push(section('Dimensions', composed ? `definition ${entity.definition.id || ''}` : `template v${entity.template_version}`, true, rows,
        h('p', { class: 'sec-note' }, composed
          ? 'These are the item\'s authoritative current values; the definition\'s recipe resolves its geometry from them. Each change records a new user-specified prototype assumption. A value the recipe cannot build fails the build and keeps the last complete set.'
          : 'Each change records a new user-specified prototype assumption. Values outside the geometry recipe\'s supported range fail the build and keep the last complete set.')));
    }
    if (composed) out.push(definitionSection(s, entity));

    // physical placement
    const placement = project.physical.placements[id];
    const placed = placement && placement.status === 'placed';
    const pos = ['x', 'y', 'z'].map((a, i) => numberInput(placed ? placement.position_m[i] : null, { field: `pos:${id}:${a}`, step: '0.05', label: `${a} position (m)` }));
    const rot = ['x', 'y', 'z'].map((a, i) => numberInput(placed ? placement.rotation_deg[i] : 0, { field: `rot:${id}:${a}`, step: '15', label: `rotation about ${a} (deg)` }));
    const placeMsg = h('div', { class: 'inline-msg' });
    const placeBtn = h('button', { class: 'btn primary', type: 'button' }, placed ? 'Move' : 'Place');
    placeBtn.addEventListener('click', () => {
      const p = pos.map(readNumber);
      const r = rot.map(readNumber);
      if (![...p, ...r].every(Number.isFinite)) return message(placeMsg, 'err', 'Enter all three coordinates and rotations.');
      if (placed && p.every((v, i) => v === placement.position_m[i]) && r.every((v, i) => v === placement.rotation_deg[i])) return message(placeMsg, '', 'Unchanged.');
      send(buildPlacement(project, id, p, r), `${placed ? 'Move' : 'Place'} ${entity.tag}`, placeMsg);
    });
    const incident = incidentConnections(project, id);
    const routeList = incident.length ? h('ul', { class: 'list' }, incident.map((c) => {
      const route = project.physical.routes[c.id];
      return h('li', null, tagButton(c.id, c.tag), route && route.status === 'current' ? statusChip('route', route.status) : rerouteControl(s, c.id));
    })) : null;
    const xyz = (label, inputs, unit) => h('div', { class: 'xyz' }, h('span', null, label),
      inputs.map((input, i) => h('div', { class: 'num' }, h('b', null, 'xyz'[i]), input, h('i', null, unit))));
    out.push(section('Physical placement', placement ? `${placement.status}${placement.manual ? ' · manual' : ''}` : 'none', true,
      !placed && placement ? h('div', { class: 'diag warning' }, h('code', null, 'placement_unresolved'), placement.reason || 'No physical placement.') : null,
      xyz('Position', pos, 'm'), xyz('Rotation', rot, '°'),
      h('div', { class: 'btn-row' }, placeBtn, h('span', { class: 'sec-note' }, placed ? 'move_equipment · P&ID unchanged' : 'place_equipment')),
      placeMsg,
      routeList ? [h('p', { class: 'sec-note' }, 'Incident routes become needs_reroute when the placement changes; their points are kept. Reroute computes a guided prototype path from the current placement.'), routeList] : null));

    // P&ID symbols
    const symbols = Object.values(project.pid.symbols).filter((sym) => sym.entity_id === id);
    if (symbols.length) {
      const blocks = symbols.map((sym) => {
        const sx = numberInput(sym.x, { field: `sym:${sym.id}:x`, step: '4', label: 'x (stored units)' });
        const sy = numberInput(sym.y, { field: `sym:${sym.id}:y`, step: '4', label: 'y (stored units)' });
        const sr = numberInput(sym.rotation_deg, { field: `sym:${sym.id}:r`, step: '90', label: 'rotation (deg)' });
        const msg = h('div', { class: 'inline-msg' });
        const pick = h('button', { class: 'btn small', type: 'button', title: 'Click a point on the P&ID' }, icon('pick'), 'Pick');
        pick.addEventListener('click', () => app.pickOnSheet((p) => {
          sx.value = String(Math.round(p.x - 100));
          sy.value = String(Math.round(p.y - 100));
          sx.dispatchEvent(new Event('input'));
          sy.dispatchEvent(new Event('input'));
          message(msg, '', `Picked paper (${Math.round(p.x)}, ${Math.round(p.y)}) → stored (${sx.value}, ${sy.value}). Review, then Move.`);
        }));
        const move = h('button', { class: 'btn primary', type: 'button' }, 'Move symbol');
        move.addEventListener('click', () => {
          const [x, y, r] = [sx, sy, sr].map(readNumber);
          if (![x, y, r].every(Number.isFinite)) return message(msg, 'err', 'Enter x, y and rotation.');
          if (x === sym.x && y === sym.y && r === sym.rotation_deg) return message(msg, '', 'Unchanged.');
          send(buildMoveSymbol(project, sym.id, x, y, r), `Move ${entity.tag} symbol`, msg);
        });
        const occ = indexItem(id) ? (indexItem(id).occurrences || []).find((o) => o.occurrence_id === sym.id) : null;
        return h('div', null,
          h('div', { class: 'kv-grid mono' }, h('span', null, 'occurrence'), h('span', null, sym.id), h('span', null, 'sheet ref'), h('span', null, occ ? `${occ.ref} @ paper (${occ.position.join(', ')})` : '—')),
          h('div', { class: 'xyz' }, h('span', null, 'Stored'),
            h('div', { class: 'num' }, h('b', null, 'x'), sx), h('div', { class: 'num' }, h('b', null, 'y'), sy), h('div', { class: 'num' }, h('b', null, '∠'), sr, h('i', null, '°'))),
          h('div', { class: 'btn-row' }, move, pick, h('span', { class: 'sec-note' }, 'move_symbol · physical placement unchanged')), msg);
      });
      out.push(section('P&ID symbol', `${symbols.length} occurrence${symbols.length === 1 ? '' : 's'}${symbols.some((x) => x.manual) ? ' · manual' : ''}`, true, blocks,
        h('p', { class: 'sec-note' }, 'Stored diagram units, +y down; the sheet shows them at paper = stored + 100. Zone references follow the new position after regeneration.')));
    }

    // ports
    const ports = Object.values(project.model.ports).filter((p) => p.entity_id === id);
    if (ports.length) {
      out.push(section('Ports', String(ports.length), false, h('ul', { class: 'list' }, ports.map((p) => {
        const conn = Object.values(project.model.connections).find((c) => c.from_port_id === p.id || c.to_port_id === p.id);
        return h('li', null,
          h('span', null, tagButton(p.id, p.name), h('small', null, ` ${p.direction} · DN${p.nominal_diameter_mm} · ${p.service}${p.required ? ' · required' : ''}`)),
          conn ? tagButton(conn.id, conn.tag) : h('small', null, 'unconnected'));
      }))));
    }
    if (entity.observes) {
      out.push(section('Observes', null, false, h('p', { class: 'desc-text' }, 'Conceptual indication of ', h('b', null, entity.observes.variable), ' on ',
        tagButton(entity.observes.entity_id, tagOf(project, entity.observes.entity_id)), '. No control loop is modeled.')));
    }
    out.push(descriptionSection(s, id));
    out.push(diagnosticsSection(s, [id]));
    return out;
  }

  // ---------------------------------------------------------------- connection

  function connectionDetail(s, id) {
    const project = s.project;
    const conn = project.model.connections[id];
    const from = project.model.ports[conn.from_port_id];
    const to = project.model.ports[conn.to_port_id];
    const route = project.physical.routes[id];
    const edges = Object.values(project.pid.edges).filter((e) => e.connection_id === id);
    const out = [header(s, id, 'Line · process connection', conn.tag)];
    out.push(section('Connection', null, true,
      h('ul', { class: 'list' },
        h('li', null, h('span', null, 'From ', from ? tagButton(from.entity_id, tagOf(project, from.entity_id)) : '?', h('small', null, from ? ` .${from.name}` : '')), h('small', { class: 'mono' }, conn.from_port_id)),
        h('li', null, h('span', null, 'To ', to ? tagButton(to.entity_id, tagOf(project, to.entity_id)) : '?', h('small', null, to ? ` .${to.name}` : '')), h('small', { class: 'mono' }, conn.to_port_id)),
        h('li', null, h('span', null, `DN${conn.nominal_diameter_mm} · OD ${fmtNum(conn.outside_diameter_m)} m (assumed)`), h('small', { class: 'mono' }, conn.assumption_id))),
      h('p', { class: 'sec-note' }, 'Line tags have no rename command in this version.')));
    out.push(section('Physical route', route ? `${route.status} · ${route.mode}` : 'none', true,
      route && route.status !== 'current' ? h('div', { class: 'diag warning' }, h('code', null, route.status), route.reason || '') : null,
      route ? h('div', { class: 'pts' }, (route.points_m || []).map((p) => `(${p.map((v) => fmtNum(v)).join(', ')})`).join('\n') || '(no points)') : null,
      !route || route.status !== 'current' || app.rerouteState(id).record ? h('div', { class: 'btn-row' }, rerouteControl(s, id)) : null,
      h('div', { class: 'btn-row' },
        h('button', { class: 'btn', type: 'button', onclick: () => app.commandPanel.prefill('set_route', { connectionId: id }) }, 'Edit route…'),
        h('button', { class: 'btn', type: 'button', onclick: () => app.commandPanel.prefill('insert_valve', { connectionId: id }) }, 'Insert valve…'),
        h('button', { class: 'btn', type: 'button', title: 'insert_equipment: composed two-port equipment from a definition', onclick: () => app.commandPanel.prefill('insert_equipment', { connectionId: id }) }, 'Insert equipment…'))));
    for (const edge of edges) {
      out.push(section('Diagram path', `${edge.status}${edge.manual ? ' · manual' : ' · guided'}`, true,
        edge.status !== 'current' ? h('div', { class: 'diag warning' }, h('code', null, edge.status), edge.reason || '') : null,
        h('div', { class: 'kv-grid mono' }, h('span', null, 'edge'), h('span', null, edge.id)),
        h('div', { class: 'pts' }, (edge.points || []).map((p) => `(${p.map((v) => fmtNum(v)).join(', ')})`).join('  ') || '(no points)'),
        h('div', { class: 'btn-row' }, h('button', { class: 'btn', type: 'button', onclick: () => app.commandPanel.prefill('set_diagram_route', { edgeId: edge.id }) }, 'Edit diagram path…'))));
    }
    out.push(descriptionSection(s, id));
    out.push(diagnosticsSection(s, [id]));
    return out;
  }

  // ---------------------------------------------------------------- port

  function portDetail(s, id) {
    const project = s.project;
    const port = project.model.ports[id];
    const conn = Object.values(project.model.connections).find((c) => c.from_port_id === id || c.to_port_id === id);
    const facts = app.sceneFacts();
    const scenePort = facts.ports.get(id);
    return [header(s, id, `Port · ${port.direction}`, tagOf(project, id)),
      section('Port', null, true, h('ul', { class: 'list' },
        h('li', null, h('span', null, 'Equipment ', tagButton(port.entity_id, tagOf(project, port.entity_id))), h('small', { class: 'mono' }, port.entity_id)),
        h('li', null, h('span', null, `DN${port.nominal_diameter_mm} · ${port.service}${port.required ? ' · required' : ''}`), h('small', { class: 'mono' }, port.assumption_id)),
        h('li', null, h('span', null, 'Connection '), conn ? tagButton(conn.id, conn.tag) : h('small', null, 'none')),
        scenePort ? h('li', null, h('span', null, `3D frame (${scenePort.position_m.map((v) => fmtNum(v)).join(', ')}) m → (${scenePort.direction.map((v) => fmtNum(v, 3)).join(', ')})`),
          h('small', null, facts.source ? `scene r${facts.source.source_revision}` : '')) : null)),
      diagnosticsSection(s, [id, port.entity_id])];
  }

  function detail(s, id) {
    const m = s.project.model;
    if (m.entities[id]) return entityDetail(s, id);
    if (m.connections[id]) return connectionDetail(s, id);
    return portDetail(s, id);
  }

  // ---------------------------------------------------------------- overview

  function overview(s) {
    const project = s.project;
    const entities = Object.values(project.model.entities).sort((a, b) => String(a.tag).localeCompare(String(b.tag), undefined, { numeric: true }));
    const lines = Object.values(project.model.connections).sort((a, b) => String(a.tag).localeCompare(String(b.tag), undefined, { numeric: true }));
    const zone = (id) => {
      const item = indexItem(id);
      return item && item.occurrences && item.occurrences.length ? item.occurrences.map((o) => o.zone).join(' ') : '';
    };
    const recent = (s.history.recent || []).slice(-6).reverse();
    return [
      h('div', { class: 'insp-head' },
        h('div', { class: 'insp-kicker' }, 'Project', h('span', { class: 'chip' }, h('b', null, 'rev'), String(project.revision))),
        h('div', { class: 'insp-tag' }, displayName(project)),
        displayName(project) !== project.id ? h('div', { class: 'insp-id mono', title: 'Stable project ID; renaming never changes it' }, project.id) : null,
        h('p', { class: 'sec-note' }, s.sync ? s.sync.message : ''),
        h('p', { class: 'sec-note' }, 'Select an item in the 3D view, on the P&ID, or below. Shift-click adds to the selection.')),
      section('Equipment', String(entities.length), true, h('ul', { class: 'list overview-list' }, entities.map((e) => {
        const pl = project.physical.placements[e.id];
        return h('li', null, tagButton(e.id, e.tag), h('small', null, kindLabel(e)),
          h('small', { class: 'mono', title: 'P&ID zone · physical status' }, `${zone(e.id)}${pl && pl.status !== 'placed' ? ' · unplaced' : ''}`));
      }))),
      section('Lines', String(lines.length), true, h('ul', { class: 'list overview-list' }, lines.map((c) => {
        const route = project.physical.routes[c.id];
        const a = project.model.ports[c.from_port_id];
        const b = project.model.ports[c.to_port_id];
        return h('li', null, tagButton(c.id, c.tag), h('small', null, `${a ? tagOf(project, a.entity_id) : '?'} → ${b ? tagOf(project, b.entity_id) : '?'}`),
          route && route.status !== 'current' ? rerouteControl(s, c.id) : h('small', { class: 'mono' }, zone(c.id)));
      }))),
      s.diagnostics.length ? section('Diagnostics', String(s.diagnostics.length), true, s.diagnostics.map((d) => h('div', { class: `diag ${d.severity}` }, h('code', null, d.code), d.message))) : null,
      recent.length ? section('Recent commands', String(recent.length), false, h('ul', { class: 'list' }, recent.map((c) => h('li', null,
        h('span', null, `${c.type}`, h('small', null, ` · ${c.source}`)), h('small', { class: 'mono' }, `r${c.from_revision}→r${c.to_revision}`))))) : null,
      app.viewer && app.viewer.source ? h('p', { class: 'sec-note', style: { padding: '0 16px' } }, `3D scene shown: build ${shortBuild(app.viewer.source.build_id)}, revision ${app.viewer.source.source_revision}.`) : null,
    ];
  }

  return {
    setSelection(next) {
      ids = next.slice();
      render();
    },
    update() {
      render();
    },
    docsChanged() {
      render(true);
    },
    sceneChanged() {
      if (ids.length) render(true);
    },
  };
}
