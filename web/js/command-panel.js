// Advanced command editor: fill an example envelope from the current project and
// selection, edit the JSON, review a plain-language summary, then apply it through
// /api/command. Stale revisions are never rebased silently: the user must refresh
// expected_revision deliberately and review again.

import { h, replace } from './dom.js';
import {
  COMMAND_TYPES, buildInsertEquipment, buildInsertValve, buildMoveSymbol, buildPlacement, buildPlanExample, buildRename,
  buildSetDiagramRoute, buildSetParameter, buildSetRoute, diagramPath, freshId, isComposed, orthogonalPath,
  reviewCommandText, VALVE_KINDS,
} from './commands-model.js';

const HELP = {
  insert_valve: 'Rewires one connection A → B into A → valve → B. The upstream line keeps its ID; the downstream line, ports, symbol and edge get fresh IDs. Without a placement the valve is saved with an explicitly unresolved physical placement and the build stays pending.',
  set_route: 'Physical route points in world metres. Accepted only when it starts and ends at the two port positions (±1 µm) and every segment is axis-aligned. The example is an editable axis-aligned path between the port frames of the scene shown; it is not a routing result. An updated service refuses a route that enters a conservative equipment-body box; other pipes, supports, stress and fabrication are not checked.',
  set_diagram_route: 'Diagram path points in stored units. Endpoints must coincide with the symbol port anchors. The example uses the port anchors of the P&ID shown (paper − 100).',
  place_equipment: 'Resolves or replaces a physical placement. Incident routes become needs_reroute.',
  move_equipment: 'Moves a placed item physically. Incident routes become needs_reroute; the P&ID is unchanged.',
  move_symbol: 'Moves one P&ID symbol occurrence (stored units, +y down). Physical placement is unchanged.',
  set_parameter: 'Sets one template parameter in metres with a new explicit assumption record.',
  rename_entity: 'Changes an equipment tag; the stable ID is unchanged.',
  insert_equipment: 'Inserts composed two-port equipment (inlet/outlet) defined by an embedded skid.equipment.definition/1 object, with the same rewiring as insert_valve. Its parameters become the item\'s authoritative values. Without a placement it stays explicitly unresolved and the build pending. Whether its CAD solids build is decided by the geometry build after applying.',
  batch: 'Applies 1–12 operations atomically: all or nothing, one revision, one undo step and one rebuild. Operations carry only type, payload and their own assumptions; batch, undo and redo cannot be nested.',
};

export function createCommandPanel(app, root) {
  const typeSelect = h('select', { 'aria-label': 'Command type' }, COMMAND_TYPES.map((t) => h('option', { value: t }, t)));
  const valveKind = h('select', { 'aria-label': 'Valve kind', title: 'Valve kind for insert_valve' }, VALVE_KINDS.map((k) => h('option', { value: k }, k)));
  const fill = h('button', { class: 'btn', type: 'button', title: 'Fill an example for the current selection (fresh IDs)' }, 'Fill example');
  const text = h('textarea', { class: 'cmd-text', spellcheck: 'false', 'aria-label': 'Command JSON', placeholder: '{ "id": …, "expected_revision": …, "type": …, "payload": { … } }' });
  const help = h('p', { class: 'sec-note' });
  const review = h('div', { class: 'review', hidden: true });
  const reviewBtn = h('button', { class: 'btn primary', type: 'button' }, 'Review');
  const applyBtn = h('button', { class: 'btn accent', type: 'button', disabled: true }, 'Apply command');
  const refreshRev = h('button', { class: 'btn small', type: 'button', hidden: true }, 'Use current revision');
  const freshIdBtn = h('button', { class: 'btn small ghost', type: 'button', title: 'Replace the envelope id with a fresh command ID' }, 'New command ID');
  replace(root,
    h('div', { class: 'cmd-grid' }, h('label', null, 'Type'), typeSelect, valveKind, fill),
    help, text,
    h('div', { class: 'btn-row' }, reviewBtn, applyBtn, refreshRev, freshIdBtn),
    review);

  let reviewed = null; // {text, command}
  let context = {};

  function selectedOf(kind) {
    const s = app.state;
    if (!s) return null;
    const m = s.project.model;
    for (const id of app.selectionIds) {
      if (kind === 'entity' && m.entities[id]) return id;
      if (kind === 'connection' && m.connections[id]) return id;
      if (kind === 'entity' && m.ports[id]) return m.ports[id].entity_id;
    }
    return null;
  }

  function example(type) {
    const project = app.state.project;
    const m = project.model;
    const anyEntity = Object.keys(m.entities).sort()[0];
    const entityId = context.entityId || selectedOf('entity') || Object.values(m.entities).find((e) => e.kind === 'centrifugal_pump')?.id || anyEntity;
    const connectionId = context.connectionId || selectedOf('connection') || Object.keys(m.connections).sort()[0];
    const entity = m.entities[entityId];
    switch (type) {
      case 'rename_entity':
        return buildRename(project, entityId, `${entity.tag}`);
      case 'set_parameter': {
        const [name, q] = Object.entries(entity.parameters)[0];
        return buildSetParameter(project, entityId, name, q.value);
      }
      case 'move_equipment':
      case 'place_equipment': {
        const pl = project.physical.placements[entityId];
        const pos = pl && pl.position_m ? pl.position_m.slice() : [0, 0, 0];
        const rot = pl && pl.rotation_deg ? pl.rotation_deg.slice() : [0, 0, 0];
        const cmd = buildPlacement(project, entityId, pos, rot);
        cmd.type = type;
        return cmd;
      }
      case 'move_symbol': {
        const sym = Object.values(project.pid.symbols).find((s) => s.entity_id === entityId) || Object.values(project.pid.symbols)[0];
        return buildMoveSymbol(project, sym.id, sym.x, sym.y, sym.rotation_deg);
      }
      case 'insert_valve':
        return buildInsertValve(project, connectionId, { kind: valveKind.value });
      case 'insert_equipment': {
        const donor = Object.values(m.entities).find(isComposed);
        const cmd = buildInsertEquipment(project, connectionId, { donorId: donor ? donor.id : undefined });
        cmd._note = donor
          ? `Definition and current parameters copied from ${donor.tag}; edit the definition or values before reviewing.`
          : 'No composed equipment exists in this project to copy: paste a definition object over the placeholder (the review refuses to send it until you do), and set its initial parameters.';
        return cmd;
      }
      case 'batch':
        return buildPlanExample(project, entityId);
      case 'set_route': {
        const conn = m.connections[connectionId];
        const facts = app.sceneFacts();
        const a = facts.ports.get(conn.from_port_id);
        const b = facts.ports.get(conn.to_port_id);
        const route = project.physical.routes[connectionId];
        let points;
        let note;
        if (a && b) {
          points = orthogonalPath(a, b);
          const rev = facts.source ? facts.source.source_revision : '?';
          note = `Endpoints copied exactly from the port frames of the 3D scene shown (revision ${rev}${rev !== project.revision ? `, not the current ${project.revision}; refill after the current scene is installed` : ''}); interior bends are an editable example.`;
        } else {
          points = route && route.points_m ? route.points_m.map((p) => p.slice()) : [];
          note = 'The scene shown has no frames for these ports; the stored points are filled instead.';
        }
        const cmd = buildSetRoute(project, connectionId, points, 'manual');
        cmd._note = note;
        return cmd;
      }
      case 'set_diagram_route': {
        const edgeId = context.edgeId || Object.values(project.pid.edges).find((e) => e.connection_id === connectionId)?.id;
        const edge = project.pid.edges[edgeId];
        const conn = m.connections[edge.connection_id];
        const facts = app.sheetFacts();
        const a = facts.anchors[conn.from_port_id];
        const b = facts.anchors[conn.to_port_id];
        let points;
        let note;
        if (a && b) {
          points = diagramPath(a, b);
          const rev = facts.meta ? facts.meta.source_revision : '?';
          note = `Endpoints from port anchors of the P&ID shown (revision ${rev}${rev !== project.revision ? `, not the current ${project.revision}` : ''}).`;
        } else {
          points = edge.points.map((p) => p.slice());
          note = 'The sheet shown has no anchors for these ports; the stored points are filled instead.';
        }
        const cmd = buildSetDiagramRoute(project, edgeId, points);
        cmd._note = note;
        return cmd;
      }
      default:
        return null;
    }
  }

  function fillExample() {
    if (!app.state) return;
    let cmd;
    try {
      cmd = example(typeSelect.value);
    } catch (error) {
      help.textContent = `Could not prepare an example: ${error.message}`;
      return;
    }
    const note = cmd && cmd._note;
    if (cmd) delete cmd._note;
    text.value = JSON.stringify(cmd, null, 2);
    help.textContent = `${HELP[typeSelect.value] || ''}${note ? ` ${note}` : ''}`;
    context = {};
    invalidate();
    doReview();
  }

  function invalidate() {
    reviewed = null;
    applyBtn.disabled = true;
  }

  function doReview() {
    if (!app.state) return;
    const r = reviewCommandText(text.value, app.state.project);
    review.hidden = false;
    refreshRev.hidden = !(r.command && Number.isInteger(r.command.expected_revision) && r.command.expected_revision !== app.state.project.revision);
    replace(review,
      r.errors.length ? h('div', { class: 'errs' }, h('b', null, 'Cannot send'), h('ul', null, r.errors.map((e) => h('li', null, e)))) : null,
      r.warnings.length ? h('div', { class: 'warns' }, h('ul', null, r.warnings.map((e) => h('li', null, e)))) : null,
      r.ok ? h('div', null, h('b', null, `${r.command.type} · expected revision ${r.command.expected_revision} · id ${r.command.id}`),
        h('ul', null, r.lines.map((l) => h('li', null, l)))) : null,
      r.ok ? h('p', { class: 'sec-note' }, 'The service validates the whole result with apply_command before anything is saved; a rejection leaves the project unchanged.') : null);
    if (r.ok && !r.warnings.length) {
      reviewed = { text: text.value, command: r.command };
      applyBtn.disabled = false;
    } else {
      reviewed = null;
      applyBtn.disabled = true;
    }
  }

  typeSelect.addEventListener('change', () => {
    valveKind.hidden = typeSelect.value !== 'insert_valve';
    help.textContent = HELP[typeSelect.value] || '';
  });
  valveKind.hidden = true;
  fill.addEventListener('click', fillExample);
  text.addEventListener('input', invalidate);
  reviewBtn.addEventListener('click', doReview);
  refreshRev.addEventListener('click', () => {
    try {
      const cmd = JSON.parse(text.value);
      cmd.expected_revision = app.state.project.revision;
      cmd.id = freshId('cmd-ui');
      text.value = JSON.stringify(cmd, null, 2);
      invalidate();
      doReview();
    } catch { /* review reports the JSON error */ }
  });
  freshIdBtn.addEventListener('click', () => {
    try {
      const cmd = JSON.parse(text.value);
      cmd.id = freshId('cmd-ui');
      text.value = JSON.stringify(cmd, null, 2);
      invalidate();
      doReview();
    } catch { /* review reports the JSON error */ }
  });
  applyBtn.addEventListener('click', async () => {
    if (!reviewed || reviewed.text !== text.value) return doReview();
    applyBtn.disabled = true;
    const r = await app.submitCommand(reviewed.command, reviewed.command.type);
    const out = r.ok
      ? h('div', { class: 'inline-msg ok' }, `Accepted: ${r.state.last_command ? `revision ${r.state.last_command.from_revision} → ${r.state.last_command.to_revision} (${r.state.last_command.status})` : 'state updated'}. This JSON is spent; fill or edit a new one.`)
      : h('div', null, h('div', { class: 'inline-msg err' }, `${r.error.status === 409 ? 'Conflict' : 'Rejected'}: ${r.error.error}`),
        (r.error.diagnostics || []).map((d) => h('div', { class: `diag ${d.severity || 'error'}` }, h('code', null, d.code), d.message)));
    review.append(out);
    reviewed = null; // a command ID is used once
  });

  return {
    update(state) {
      if (reviewed && reviewed.command.expected_revision !== state.project.revision) {
        invalidate();
        refreshRev.hidden = false;
        review.append(h('div', { class: 'inline-msg err' }, `The project moved to revision ${state.project.revision}; this command would be stale. Review again.`));
      }
    },
    prefill(type, ctx = {}) {
      context = ctx;
      typeSelect.value = type;
      valveKind.hidden = type !== 'insert_valve';
      document.querySelector('#change-tabs button[data-tab="command"]').click();
      document.getElementById('workspace').classList.remove('no-dock');
      fillExample();
      text.focus();
    },
    sheetChanged() {},
  };
}
