// Pure builders and reviewers for the existing command envelope
// {id, expected_revision, type, payload, assumptions?} (docs/CONTRACTS.md).
// No DOM and no network: the service validates every command with apply_command;
// these helpers only prepare fresh IDs, explicit assumption records and a
// human-readable review of exactly what will be sent.

export const COMMAND_TYPES = [
  'rename_entity', 'set_parameter', 'move_equipment', 'place_equipment', 'move_symbol',
  'insert_valve', 'set_route', 'set_diagram_route', 'insert_equipment', 'batch',
];
export const VALVE_KINDS = ['isolation_valve', 'check_valve'];
export const COMPOSED_KIND = 'composed_equipment';
export const BATCH_LIMIT = 12;
const OPERATION_KEYS = ['type', 'payload', 'assumptions'];
const NOT_IN_BATCH = ['batch', 'undo', 'redo'];
const DEFINITION_PLACEHOLDER = 'PASTE a skid.equipment.definition/1 object here (for example examples/recipes/horizontal_drum.definition.json)';

const KIND_NAMES = {
  tank: 'Tank', centrifugal_pump: 'Centrifugal pump', isolation_valve: 'Isolation valve', check_valve: 'Check valve',
  indicator: 'Indicator (conceptual)', boundary: 'Scope boundary',
};

export function isComposed(entity) {
  return !!entity && entity.kind === COMPOSED_KIND && !!entity.definition && typeof entity.definition === 'object';
}

/** Readable item type: built-in kind names, or the declared definition name for composed equipment. */
export function kindLabel(entity) {
  if (!entity) return '';
  if (isComposed(entity)) {
    const d = entity.definition;
    const name = typeof d.name === 'string' && d.name.trim() ? d.name.trim() : (d.id || 'unnamed definition');
    return `${name} (composed)`;
  }
  if (entity.kind === COMPOSED_KIND) return 'Composed equipment (no definition)';
  return KIND_NAMES[entity.kind] || String(entity.kind || '').replace(/_/g, ' ');
}

/** The definition recipe's authoring range for one parameter: {minimum?, maximum?, unit?} or null. */
export function recipeRange(entity, name) {
  if (!isComposed(entity)) return null;
  const params = entity.definition.geometry && entity.definition.geometry.parameters;
  const rec = params && params[name];
  if (!rec || typeof rec !== 'object') return null;
  const out = {};
  for (const k of ['minimum', 'maximum', 'unit']) if (k in rec) out[k] = rec[k];
  return Object.keys(out).length ? out : null;
}

/** Parameter names in display order: the definition's authored order for composed equipment. */
export function parameterNames(entity) {
  const names = Object.keys(entity.parameters || {});
  if (!isComposed(entity)) return names;
  const params = entity.definition.geometry && entity.definition.geometry.parameters;
  const authored = params && typeof params === 'object' ? Object.keys(params) : [];
  return [...authored.filter((n) => names.includes(n)), ...names.filter((n) => !authored.includes(n))];
}

function randomHex(bytes) {
  const c = globalThis.crypto;
  if (c && typeof c.getRandomValues === 'function') {
    const a = new Uint8Array(bytes);
    c.getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  throw new Error('no secure random source for fresh IDs');
}

export function freshUuid() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const h = randomHex(16);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

/** A fresh, never-reused ID with a readable prefix. */
export function freshId(prefix) {
  return `${prefix}-${freshUuid()}`;
}

export function envelope(type, payload, expectedRevision, assumptions) {
  const command = { id: freshId('cmd-ui'), expected_revision: expectedRevision, type, payload };
  if (assumptions && Object.keys(assumptions).length) command.assumptions = assumptions;
  return command;
}

export function humanize(name) {
  return String(name).replace(/_/g, ' ');
}

export function fmtNum(v, digits = 4) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v);
  return String(Number(v.toFixed(digits)));
}

export function fmtPoint(p, digits = 4) {
  return Array.isArray(p) ? `(${p.map((v) => fmtNum(v, digits)).join(', ')})` : String(p);
}

function tagKey(tag) {
  return String(tag).split(/\s+/).filter(Boolean).join(' ').toLowerCase();
}

export function tagInUse(project, tag, exceptId = null) {
  const key = tagKey(tag);
  const { entities, connections } = project.model;
  for (const [id, rec] of Object.entries(entities)) if (id !== exceptId && tagKey(rec.tag) === key) return id;
  for (const [id, rec] of Object.entries(connections)) if (id !== exceptId && tagKey(rec.tag) === key) return id;
  return null;
}

/** Next unused tag with the given prefix, e.g. HV- -> HV-103; L- -> L-106. Suggestion only. */
export function nextFreeTag(project, prefix, reserved = []) {
  const re = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\d+)$`, 'i');
  let max = 100;
  const all = [...Object.values(project.model.entities), ...Object.values(project.model.connections)];
  for (const rec of all) {
    const m = re.exec(String(rec.tag || '').trim());
    if (m) max = Math.max(max, Number(m[1]));
  }
  for (let n = max + 1; n < max + 1000; n += 1) {
    const tag = `${prefix}${n}`;
    if (!tagInUse(project, tag) && !reserved.some((r) => tagKey(r) === tagKey(tag))) return tag;
  }
  return `${prefix}${freshUuid().slice(0, 4)}`;
}

function entityTag(project, id) {
  const e = project.model.entities[id];
  return e ? e.tag : id;
}

function portLabel(project, portId) {
  const p = project.model.ports[portId];
  if (!p) return portId;
  return `${entityTag(project, p.entity_id)}.${p.name}`;
}

export function incidentConnections(project, entityId) {
  const ports = new Set(Object.values(project.model.ports).filter((p) => p.entity_id === entityId).map((p) => p.id));
  return Object.values(project.model.connections)
    .filter((c) => ports.has(c.from_port_id) || ports.has(c.to_port_id))
    .sort((a, b) => String(a.tag).localeCompare(String(b.tag)));
}

function stamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

// ---------------------------------------------------------------- builders

export function buildRename(project, entityId, tag) {
  return envelope('rename_entity', { entity_id: entityId, tag }, project.revision);
}

/** set_parameter with a new explicit assumption record naming this UI edit. */
export function buildSetParameter(project, entityId, name, value) {
  const entity = project.model.entities[entityId];
  const assumptionId = freshId('asm-ui');
  const cmd = envelope('set_parameter', {
    entity_id: entityId, name, quantity: { value, unit: 'm', assumption_id: assumptionId },
  }, project.revision, {
    [assumptionId]: {
      description: `${entity ? entity.tag : entityId} ${name} set to ${fmtNum(value, 6)} m in the web inspector against ` +
        `revision ${project.revision}. User-specified prototype value; not a vendor, design or verified value.`,
      source: `local web workspace edit, ${stamp()}`,
    },
  });
  return cmd;
}

/** move_equipment for a placed item, place_equipment for an unresolved one; new manual-layout assumption. */
export function buildPlacement(project, entityId, position, rotation) {
  const entity = project.model.entities[entityId];
  const current = project.physical.placements[entityId];
  const type = current && current.status === 'placed' ? 'move_equipment' : 'place_equipment';
  const assumptionId = freshId('asm-ui');
  return envelope(type, {
    entity_id: entityId,
    placement: { position_m: position, rotation_deg: rotation, status: 'placed', manual: true, assumption_id: assumptionId },
  }, project.revision, {
    [assumptionId]: {
      description: `${entity ? entity.tag : entityId} placed at ${fmtPoint(position, 6)} m, rotation ${fmtPoint(rotation, 3)} deg, ` +
        `in the web inspector against revision ${project.revision}. Manual prototype layout choice; not a verified ` +
        'installation position.',
      source: `local web workspace edit, ${stamp()}`,
    },
  });
}

export function buildMoveSymbol(project, symbolId, x, y, rotation) {
  return envelope('move_symbol', { symbol_id: symbolId, x, y, rotation_deg: rotation }, project.revision);
}

export function buildSetRoute(project, connectionId, points, mode = 'manual') {
  const conn = project.model.connections[connectionId];
  const assumptionId = freshId('asm-ui');
  return envelope('set_route', { connection_id: connectionId, points_m: points, mode, assumption_id: assumptionId },
    project.revision, {
      [assumptionId]: {
        description: `${conn ? conn.tag : connectionId} route points entered in the web command editor against revision ` +
          `${project.revision}. Orthogonal prototype path; other pipes, supports, stress and fabrication are not checked.`,
        source: `local web workspace edit, ${stamp()}`,
      },
    });
}

/**
 * move_route_segment: one straight section (zero-based index over the simplified route's straight
 * legs) moved by one world-axis offset; the service derives the new route. New manual-route assumption.
 */
export function buildRouteSegmentMove(project, connectionId, segmentIndex, offset) {
  const conn = project.model.connections[connectionId];
  const assumptionId = freshId('asm-ui');
  return envelope('move_route_segment', { connection_id: connectionId, segment_index: segmentIndex, offset_m: offset.slice(), assumption_id: assumptionId },
    project.revision, {
      [assumptionId]: {
        description: `${conn ? conn.tag : connectionId} straight section ${segmentIndex + 1} moved by ${fmtPoint(offset, 6)} m in the web ` +
          `3D view against revision ${project.revision}. Manual prototype route choice; the service derived the path. ` +
          'Supports, stress and fabrication are not checked.',
        source: `local web workspace edit, ${stamp()}`,
      },
    });
}

export function buildSetDiagramRoute(project, edgeId, points) {
  return envelope('set_diagram_route', { edge_id: edgeId, points }, project.revision);
}

/**
 * Axis-aligned 3D path between two port frames {position_m, direction}: an editable example,
 * not routing, clearance or design validation. Endpoints are the frames' coordinates copied
 * exactly; every interior point copies the exact coordinates of its neighbours on the axes it
 * does not move along, and only a moved stub coordinate is rounded (to 1 µm).
 *
 * A full stub must not overshoot the other port and come back. Frames that face each other along
 * one axis closer than two stubs run straight when collinear, and otherwise both stubs end at the
 * midpoint of the gap. For frames on different axes, a stub stops at the other port's coordinate
 * on its axis when that port is less than one stub ahead. Paths stay monotonic along the stub
 * axes; other close arrangements are not special-cased, and the geometry build still reports any
 * path it cannot build (for example bends without room).
 */
export function orthogonalPath(from, to, stub = 0.1) {
  const axisOf = (d) => {
    if (!Array.isArray(d)) return -1;
    const moving = [0, 1, 2].filter((k) => Math.abs(d[k]) > 1e-9);
    return moving.length === 1 ? moving[0] : -1;
  };
  const offset = (p, d) => {
    const axis = axisOf(d);
    if (axis < 0) return p.slice();
    const q = p.slice();
    q[axis] = round6(p[axis] + Math.sign(d[axis]) * stub);
    return q;
  };
  const a = from.position_m.slice();
  const b = to.position_m.slice();
  let a1 = offset(a, from.direction);
  let b1 = offset(b, to.direction);
  const k = axisOf(from.direction);
  const capped = (p, d, other, p1) => { // stop the stub at the other port's coordinate if it lies within the stub
    const axis = axisOf(d);
    const ahead = (other[axis] - p[axis]) * Math.sign(d[axis]);
    if (!(ahead > 0 && ahead < stub)) return p1;
    const q = p.slice();
    q[axis] = other[axis];
    return q;
  };
  if (k >= 0 && axisOf(to.direction) >= 0 && k !== axisOf(to.direction)) {
    a1 = capped(a, from.direction, b, a1);
    b1 = capped(b, to.direction, a, b1);
  } else if (k >= 0 && k === axisOf(to.direction) && Math.sign(from.direction[k]) === -Math.sign(to.direction[k])) {
    const gap = (b[k] - a[k]) * Math.sign(from.direction[k]); // > 0 when b lies in front of a
    if (gap > 0 && gap < 2 * stub) {
      if ([0, 1, 2].every((i) => i === k || Math.abs(a[i] - b[i]) <= 1e-9)) return [a, b];
      const middle = (a[k] + b[k]) / 2; // strictly between the ports; shared by both stubs
      a1 = a.slice();
      b1 = b.slice();
      a1[k] = middle;
      b1[k] = middle;
    }
  }
  const pts = [a];
  pts.push(a1);
  let cur = a1.slice();
  for (const axis of [2, 1, 0]) { // vertical first, then across, then along
    if (Math.abs(cur[axis] - b1[axis]) <= 1e-9) continue;
    // take b1's exact value on this axis, and on any axis that differs only by float noise
    cur = cur.map((v, k) => (k === axis || Math.abs(v - b1[k]) <= 1e-9 ? b1[k] : v));
    pts.push(cur);
  }
  pts.push(b1, b);
  return simplifyPath(pts);
}

function round6(v) {
  return Math.round(v * 1e6) / 1e6 + 0;
}

/**
 * Drop zero-length segments and merge collinear runs. Only interior points are removed or
 * replaced: the first and last points survive exactly as given.
 */
export function simplifyPath(points) {
  const out = [];
  points.forEach((p, j) => {
    const last = out[out.length - 1];
    if (last && last.every((v, i) => Math.abs(v - p[i]) <= 1e-9)) {
      if (out.length > 1) out[out.length - 1] = p; // keep the later point, so the end stays exact
      else if (j === points.length - 1) out.push(p); // degenerate path whose ends coincide
      return;
    }
    out.push(p);
  });
  for (let i = out.length - 2; i >= 1; i -= 1) {
    const a = out[i - 1];
    const b = out[i];
    const c = out[i + 1];
    const d1 = b.map((v, k) => v - a[k]);
    const d2 = c.map((v, k) => v - b[k]);
    const moving1 = d1.map((v) => Math.abs(v) > 1e-9);
    const moving2 = d2.map((v) => Math.abs(v) > 1e-9);
    const sameAxis = moving1.every((m, k) => m === moving2[k]) && moving1.filter(Boolean).length === 1;
    const sameDirection = sameAxis && d1.every((v, k) => Math.sign(v) === Math.sign(d2[k]));
    if (sameDirection) out.splice(i, 1);
  }
  return out;
}

/** Guided-style diagram path between two stored-coordinate anchors (straight if aligned, else a Z). */
export function diagramPath(a, b) {
  if (Math.abs(a[0] - b[0]) < 1e-9 || Math.abs(a[1] - b[1]) < 1e-9) return [a, b];
  const mid = (a[0] + b[0]) / 2;
  return [a, [mid, a[1]], [mid, b[1]], b];
}

/**
 * insert_valve example: fresh IDs for entity, ports, downstream connection, symbol and edge;
 * template parameters copied (with their existing assumption IDs) from an existing item of the
 * same kind; no physical placement, so the service records it as explicitly unresolved.
 */
export function buildInsertValve(project, connectionId, options = {}) {
  const model = project.model;
  const conn = model.connections[connectionId];
  if (!conn) throw new Error(`unknown connection ${connectionId}`);
  const kind = VALVE_KINDS.includes(options.kind) ? options.kind : 'isolation_valve';
  const donor = Object.values(model.entities).find((e) => e.kind === kind);
  if (!donor) throw new Error(`no existing ${kind} to copy template parameters from`);
  const prefix = kind === 'check_valve' ? 'NRV-' : 'HV-';
  const tag = options.tag || nextFreeTag(project, prefix);
  const lineTag = options.lineTag || nextFreeTag(project, 'L-', [tag]);
  const u = freshUuid().slice(0, 8);
  const entityId = `ent-ui-${u}`;
  const inlet = `port-ui-${u}-inlet`;
  const outlet = `port-ui-${u}-outlet`;
  const downId = `conn-ui-${u}`;
  const symbolId = `sym-ui-${u}`;
  const edgeId = `edge-ui-${u}`;
  const upEdge = Object.values(project.pid.edges).find((e) => e.connection_id === connectionId);
  const toPort = model.ports[conn.to_port_id];
  const portAssumption = toPort ? toPort.assumption_id : conn.assumption_id;
  let x = 0;
  let y = 0;
  if (options.symbolXY) [x, y] = options.symbolXY;
  else if (upEdge && upEdge.points && upEdge.points.length >= 2) {
    // midpoint of the longest existing segment of the upstream diagram edge
    let best = null;
    for (let i = 0; i + 1 < upEdge.points.length; i += 1) {
      const [p, q] = [upEdge.points[i], upEdge.points[i + 1]];
      const len = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (!best || len > best.len) best = { len, p, q };
    }
    x = Math.round((best.p[0] + best.q[0]) / 2);
    y = Math.round((best.p[1] + best.q[1]) / 2);
  }
  const parameters = {};
  for (const [name, q] of Object.entries(donor.parameters)) parameters[name] = { ...q };
  const payload = {
    connection_id: connectionId,
    entity: { id: entityId, kind, tag, template_version: donor.template_version, parameters },
    ports: [
      { id: inlet, entity_id: entityId, name: 'inlet', direction: 'in', required: true,
        nominal_diameter_mm: conn.nominal_diameter_mm, service: toPort ? toPort.service : 'liquid', assumption_id: portAssumption },
      { id: outlet, entity_id: entityId, name: 'outlet', direction: 'out', required: true,
        nominal_diameter_mm: conn.nominal_diameter_mm, service: toPort ? toPort.service : 'liquid', assumption_id: portAssumption },
    ],
    downstream_connection: {
      id: downId, tag: lineTag, from_port_id: outlet, to_port_id: conn.to_port_id,
      nominal_diameter_mm: conn.nominal_diameter_mm, outside_diameter_m: conn.outside_diameter_m, assumption_id: conn.assumption_id,
    },
    symbol: { id: symbolId, entity_id: entityId, x, y, rotation_deg: 0, manual: true },
    downstream_edge: {
      id: edgeId, connection_id: downId, from_symbol_id: symbolId, to_symbol_id: upEdge ? upEdge.to_symbol_id : '',
      points: [], manual: false, status: 'unresolved',
      // stored model text: no tag in it, so a later rename cannot leave it stale
      reason: 'downstream diagram path not yet drawn; supply it with set_diagram_route',
    },
  };
  return envelope('insert_valve', payload, project.revision);
}

/**
 * insert_equipment example for a composed two-port inline item (same payload as insert_valve).
 * The definition is copied from an existing composed entity (with its current parameters and their
 * provenance) or supplied as options.definition (its authoring defaults become the initial values,
 * recorded with one new explicit assumption). Without either, a placeholder that the review refuses
 * to send is filled in, so nothing incomplete can be applied.
 */
export function buildInsertEquipment(project, connectionId, options = {}) {
  const model = project.model;
  const conn = model.connections[connectionId];
  if (!conn) throw new Error(`unknown connection ${connectionId}`);
  const donor = options.donorId ? model.entities[options.donorId] : Object.values(model.entities).find(isComposed);
  const definition = options.definition || (isComposed(donor) ? donor.definition : null);
  const shape = definition && definition.symbol ? definition.symbol.shape : '';
  const prefix = /vessel/.test(String(shape)) ? 'V-' : 'X-';
  const tag = options.tag || nextFreeTag(project, prefix);
  const lineTag = options.lineTag || nextFreeTag(project, 'L-', [tag]);
  const u = freshUuid().slice(0, 8);
  const entityId = `ent-ui-${u}`;
  const ids = { inlet: `port-ui-${u}-inlet`, outlet: `port-ui-${u}-outlet` };
  const downId = `conn-ui-${u}`;
  const symbolId = `sym-ui-${u}`;
  const edgeId = `edge-ui-${u}`;
  const upEdge = Object.values(project.pid.edges).find((e) => e.connection_id === connectionId);
  const toPort = model.ports[conn.to_port_id];
  const portAssumption = toPort ? toPort.assumption_id : conn.assumption_id;
  let assumptions;
  let parameters = {};
  if (definition && isComposed(donor) && donor.definition === definition) {
    for (const [name, q] of Object.entries(donor.parameters || {})) parameters[name] = { ...q };
  } else if (definition && definition.geometry && definition.geometry.parameters) {
    const assumptionId = freshId('asm-ui');
    for (const [name, rec] of Object.entries(definition.geometry.parameters)) {
      parameters[name] = { value: rec.value, unit: rec.unit || 'm', assumption_id: assumptionId };
    }
    assumptions = {
      [assumptionId]: {
        description: `Initial ${tag} parameters equal to the authoring defaults of definition ${definition.id || '(no id)'}, ` +
          `entered in the web command editor against revision ${project.revision}. Illustrative values, not design values.`,
        source: `local web workspace edit, ${stamp()}`,
      },
    };
  }
  const services = definition && definition.ports ? definition.ports : {};
  const port = (name, direction) => ({
    id: ids[name], entity_id: entityId, name, direction, required: services[name] ? services[name].required !== false : true,
    nominal_diameter_mm: conn.nominal_diameter_mm, service: (services[name] && services[name].service) || (toPort ? toPort.service : 'liquid'),
    assumption_id: portAssumption,
  });
  let x = 0;
  let y = 0;
  if (options.symbolXY) [x, y] = options.symbolXY;
  else if (upEdge && upEdge.points && upEdge.points.length >= 2) {
    let best = null;
    for (let i = 0; i + 1 < upEdge.points.length; i += 1) {
      const [p, q] = [upEdge.points[i], upEdge.points[i + 1]];
      const len = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (!best || len > best.len) best = { len, p, q };
    }
    x = Math.round((best.p[0] + best.q[0]) / 2);
    y = Math.round((best.p[1] + best.q[1]) / 2);
  }
  const payload = {
    connection_id: connectionId,
    entity: {
      id: entityId, kind: COMPOSED_KIND, tag, template_version: 1, parameters,
      definition: definition || DEFINITION_PLACEHOLDER,
    },
    ports: [port('inlet', 'in'), port('outlet', 'out')],
    downstream_connection: {
      id: downId, tag: lineTag, from_port_id: ids.outlet, to_port_id: conn.to_port_id,
      nominal_diameter_mm: conn.nominal_diameter_mm, outside_diameter_m: conn.outside_diameter_m, assumption_id: conn.assumption_id,
    },
    symbol: { id: symbolId, entity_id: entityId, x, y, rotation_deg: 0, manual: true },
    downstream_edge: {
      id: edgeId, connection_id: downId, from_symbol_id: symbolId, to_symbol_id: upEdge ? upEdge.to_symbol_id : '',
      points: [], manual: false, status: 'unresolved',
      reason: 'downstream diagram path not yet drawn; supply it with set_diagram_route',
    },
  };
  return envelope('insert_equipment', payload, project.revision, assumptions);
}

/** One atomic batch envelope: operations apply all-or-nothing as one revision and one undo step. */
export function buildBatch(project, operations) {
  return envelope('batch', {
    operations: operations.map((op) => {
      const out = { type: op.type, payload: op.payload };
      if (op.assumptions && Object.keys(op.assumptions).length) out.assumptions = op.assumptions;
      return out;
    }),
  }, project.revision);
}

/** Example plan: rename an item and change one parameter, applied together as one step. */
export function buildPlanExample(project, entityId) {
  const entity = project.model.entities[entityId];
  if (!entity) throw new Error(`unknown entity ${entityId}`);
  const operations = [{ type: 'rename_entity', payload: { entity_id: entityId, tag: nextFreeTag(project, `${String(entity.tag).replace(/\d+$/, '')}`) } }];
  const names = parameterNames(entity);
  if (names.length) {
    const set = buildSetParameter(project, entityId, names[0], entity.parameters[names[0]].value);
    operations.push({ type: 'set_parameter', payload: set.payload, assumptions: set.assumptions });
  }
  return buildBatch(project, operations);
}

// ---------------------------------------------------------------- review

const ENVELOPE_KEYS = ['id', 'expected_revision', 'type', 'payload'];

/**
 * Parse and summarize a command JSON text for review before it is sent.
 * Returns {ok, command, errors, warnings, lines}; validation proper is the service's job.
 */
export function reviewCommandText(text, project) {
  const errors = [];
  const warnings = [];
  let command = null;
  try {
    command = JSON.parse(text);
  } catch (exc) {
    return { ok: false, command: null, errors: [`Not valid JSON: ${exc.message}`], warnings, lines: [] };
  }
  if (!command || typeof command !== 'object' || Array.isArray(command)) {
    return { ok: false, command: null, errors: ['The command must be a JSON object envelope.'], warnings, lines: [] };
  }
  for (const key of ENVELOPE_KEYS) if (!(key in command)) errors.push(`Missing envelope field "${key}".`);
  const extra = Object.keys(command).filter((k) => !ENVELOPE_KEYS.includes(k) && k !== 'assumptions');
  if (extra.length) errors.push(`Unsupported envelope fields: ${extra.join(', ')}.`);
  if (typeof command.id !== 'string' || !command.id) errors.push('id must be a nonempty string (use a fresh ID).');
  if (!Number.isInteger(command.expected_revision) || command.expected_revision < 0) {
    errors.push('expected_revision must be a nonnegative integer.');
  } else if (project && command.expected_revision !== project.revision) {
    warnings.push(`expected_revision ${command.expected_revision} differs from the current revision ${project.revision}; ` +
      'the service will reject it as stale. Refresh it deliberately after re-reviewing.');
  }
  if (!COMMAND_TYPES.includes(command.type) && !['set_port_size', 'undo', 'redo'].includes(command.type)) {
    errors.push(`Unsupported type ${JSON.stringify(command.type)}.`);
  }
  if (!command.payload || typeof command.payload !== 'object' || Array.isArray(command.payload)) errors.push('payload must be an object.');
  else if (command.type === 'batch') errors.push(...batchErrors(command));
  else if (command.type === 'insert_equipment') errors.push(...insertEquipmentErrors(command.payload));
  else if (command.type === 'set_route' && !(Array.isArray(command.payload.points_m) && command.payload.points_m.length >= 2)) {
    errors.push('points_m needs at least two points. If the example came out empty, the scene shown had no frames for these ports; fill it again once the current scene is installed.');
  }
  const lines = errors.length || !project ? [] : describeCommand(command, project);
  return { ok: errors.length === 0, command, errors, warnings, lines };
}

function insertEquipmentErrors(payload, where = '') {
  const entity = payload.entity;
  if (!entity || typeof entity !== 'object') return [`${where}insert_equipment needs an entity object.`];
  if (entity.kind !== COMPOSED_KIND) return [`${where}insert_equipment inserts kind "${COMPOSED_KIND}" only.`];
  if (!entity.definition || typeof entity.definition !== 'object' || Array.isArray(entity.definition)) {
    return [`${where}entity.definition must be a definition object; paste one before sending.`];
  }
  return [];
}

/** Envelope-shape checks for a batch (the service validates every operation and the final result). */
export function batchErrors(command) {
  const errors = [];
  if ('assumptions' in command) errors.push('A batch carries assumptions inside its operations, not on the envelope.');
  const extra = Object.keys(command.payload).filter((k) => k !== 'operations');
  if (extra.length) errors.push(`Unsupported batch payload fields: ${extra.join(', ')}.`);
  const ops = command.payload.operations;
  if (!Array.isArray(ops) || ops.length < 1 || ops.length > BATCH_LIMIT) {
    errors.push(`payload.operations must list 1 to ${BATCH_LIMIT} operations.`);
    return errors;
  }
  ops.forEach((op, i) => {
    const at = `Operation ${i + 1}: `;
    if (!op || typeof op !== 'object' || Array.isArray(op)) { errors.push(`${at}must be an object.`); return; }
    const unknown = Object.keys(op).filter((k) => !OPERATION_KEYS.includes(k));
    if (unknown.length) errors.push(`${at}unsupported fields ${unknown.join(', ')} (only type, payload, assumptions).`);
    if (NOT_IN_BATCH.includes(op.type)) errors.push(`${at}${op.type} cannot be part of a batch.`);
    else if (!COMMAND_TYPES.includes(op.type) && op.type !== 'set_port_size') errors.push(`${at}unsupported type ${JSON.stringify(op.type)}.`);
    if (!op.payload || typeof op.payload !== 'object' || Array.isArray(op.payload)) errors.push(`${at}payload must be an object.`);
    else if (op.type === 'insert_equipment') errors.push(...insertEquipmentErrors(op.payload, at));
  });
  return errors;
}

/** Human-readable lines describing what a command asks for (not whether it will validate). */
export function describeCommand(cmd, project) {
  const p = cmd.payload || {};
  const m = project.model;
  const lines = [];
  const added = cmd.assumptions ? Object.keys(cmd.assumptions) : [];
  switch (cmd.type) {
    case 'rename_entity': {
      const e = m.entities[p.entity_id];
      lines.push(`Rename ${e ? `${e.tag} (${p.entity_id})` : `unknown entity ${p.entity_id}`} to ${JSON.stringify(p.tag)}.`);
      lines.push('ID, ports, connections, P&ID layout and physical placement are unchanged.');
      break;
    }
    case 'set_parameter': {
      const e = m.entities[p.entity_id];
      const old = e && e.parameters ? e.parameters[p.name] : null;
      const q = p.quantity || {};
      lines.push(`Set ${e ? e.tag : p.entity_id} ${p.name} from ${old ? `${fmtNum(old.value, 6)} ${old.unit}` : 'unset'} to ` +
        `${fmtNum(q.value, 6)} ${q.unit} (provenance ${q.assumption_id || q.source || 'missing'}).`);
      if (e) {
        const lines2 = incidentConnections(project, e.id).map((c) => c.tag);
        if (lines2.length) lines.push(`If a nozzle moves, routes ${lines2.join(', ')} become needs_reroute; no route points are invented.`);
      }
      break;
    }
    case 'move_equipment':
    case 'place_equipment': {
      const e = m.entities[p.entity_id];
      const pl = p.placement || {};
      const cur = project.physical.placements[p.entity_id];
      lines.push(`${cmd.type === 'place_equipment' ? 'Place' : 'Move'} ${e ? e.tag : p.entity_id} physically ` +
        `${cur && cur.position_m ? `from ${fmtPoint(cur.position_m)} ` : ''}to ${fmtPoint(pl.position_m)} m, rotation ${fmtPoint(pl.rotation_deg, 2)} deg.`);
      if (e) {
        const inc = incidentConnections(project, e.id).map((c) => c.tag);
        if (inc.length) lines.push(`Incident routes ${inc.join(', ')} become needs_reroute with their old points kept.`);
      }
      lines.push('The P&ID symbol and process connectivity are unchanged.');
      break;
    }
    case 'move_symbol': {
      const s = project.pid.symbols[p.symbol_id];
      lines.push(`Move P&ID symbol ${p.symbol_id}${s ? ` of ${entityTag(project, s.entity_id)} from (${fmtNum(s.x)}, ${fmtNum(s.y)})` : ''} ` +
        `to (${fmtNum(p.x)}, ${fmtNum(p.y)}), rotation ${fmtNum(p.rotation_deg)} deg (stored diagram units, +y down).`);
      lines.push('Physical placement is unchanged; guided edges are regenerated and manual edges kept or flagged.');
      break;
    }
    case 'set_route': {
      const c = m.connections[p.connection_id];
      lines.push(`Set physical route of ${c ? `${c.tag} (${portLabel(project, c.from_port_id)} → ${portLabel(project, c.to_port_id)})` : p.connection_id} ` +
        `to ${Array.isArray(p.points_m) ? p.points_m.length : '?'} points, mode ${p.mode}.`);
      lines.push('The service accepts it only if it starts and ends at the port positions and every segment is axis-aligned.');
      break;
    }
    case 'set_diagram_route': {
      const edge = project.pid.edges[p.edge_id];
      const c = edge ? m.connections[edge.connection_id] : null;
      lines.push(`Set diagram path of edge ${p.edge_id}${c ? ` (${c.tag})` : ''} to ${Array.isArray(p.points) ? p.points.length : '?'} points; it becomes a manual path.`);
      lines.push('Endpoints must coincide with the symbol port anchors.');
      break;
    }
    case 'insert_valve':
    case 'insert_equipment': {
      const c = m.connections[p.connection_id];
      const ent = p.entity || {};
      const down = p.downstream_connection || {};
      const what = cmd.type === 'insert_equipment' ? (isComposed(ent) ? kindLabel(ent) : 'composed equipment') : (ent.kind || 'valve');
      if (c) {
        const fromE = m.ports[c.from_port_id] ? entityTag(project, m.ports[c.from_port_id].entity_id) : '?';
        const toE = m.ports[c.to_port_id] ? entityTag(project, m.ports[c.to_port_id].entity_id) : '?';
        lines.push(`Insert ${what} ${ent.tag} (${ent.id}) into ${c.tag}: ${fromE} → ${ent.tag} → ${toE}.`);
        lines.push(`${c.tag} keeps ID ${c.id} upstream; new downstream line ${down.tag} (${down.id}).`);
      } else {
        lines.push(`Insert into unknown connection ${p.connection_id}.`);
      }
      if (cmd.type === 'insert_equipment' && isComposed(ent)) {
        const d = ent.definition;
        const shape = d.symbol && d.symbol.shape ? String(d.symbol.shape).replace(/_/g, ' ') : 'unspecified';
        lines.push(`Definition ${d.id || '(no id)'} (${d.schema || 'no schema'}), generic P&ID outline ${shape}; ` +
          `${Object.keys(ent.parameters || {}).length} parameter(s) become the item's authoritative values.`);
      }
      const item = cmd.type === 'insert_valve' ? 'valve' : 'item';
      lines.push(p.placement ? `Physical placement supplied: ${fmtPoint(p.placement.position_m)} m.` :
        `No physical placement supplied: the ${item} is saved with an explicitly unresolved placement and the build stays pending.`);
      if (p.downstream_edge && p.downstream_edge.status !== 'current') {
        lines.push(`Downstream diagram edge ${p.downstream_edge.id} is ${p.downstream_edge.status} until set_diagram_route supplies its path.`);
      }
      break;
    }
    case 'add_equipment': {
      const ent = p.entity || {};
      const ports = Array.isArray(p.ports) ? p.ports : Object.values(p.ports || {});
      const what = kindLabel(ent) || 'item';
      lines.push(`Add ${what} ${ent.tag} (${ent.id}) with ${ports.length} port${ports.length === 1 ? '' : 's'}: ` +
        `${ports.map((port) => `${port.name} (${port.direction}, ${port.id})`).join(', ') || 'none'}.`);
      const pl = p.placement;
      lines.push(pl && pl.status === 'placed'
        ? `Physical placement ${fmtPoint(pl.position_m)} m, rotation ${fmtPoint(pl.rotation_deg, 2)} deg.`
        : 'No physical position: the item is saved with an explicitly unresolved placement and the build stays pending.');
      if (p.symbol) lines.push(`P&ID symbol ${p.symbol.id} at (${fmtNum(p.symbol.x)}, ${fmtNum(p.symbol.y)}).`);
      const open = ports.filter((port) => port.connection_status === 'unresolved').length;
      if (open) lines.push(`${open} port${open === 1 ? '' : 's'} recorded as deliberately open until a line connects ${open === 1 ? 'it' : 'them'}; an open required port keeps the outputs pending.`);
      break;
    }
    case 'connect_ports': {
      const c = p.connection || {};
      const edge = p.edge || {};
      const route = p.route;
      const label = (id) => (m.ports[id] ? portLabel(project, id) : id);
      const od = Number.isFinite(c.outside_diameter_m) ? `, OD ${fmtNum(c.outside_diameter_m * 1000, 1)} mm` : '';
      lines.push(`Connect ${label(c.from_port_id)} → ${label(c.to_port_id)} as new line ${c.tag} (${c.id}), DN${c.nominal_diameter_mm}${od}.`);
      lines.push(edge.status === 'current' ? `P&ID edge ${edge.id} on the guided path between the symbol anchors.`
        : `P&ID edge ${edge.id} is ${edge.status}${edge.reason ? `: ${edge.reason}` : ''}.`);
      if (!route) lines.push('No physical route supplied: it is recorded unresolved.');
      else if (route.status === 'current') lines.push(`Physical route: ${Array.isArray(route.points_m) ? route.points_m.length : '?'} points, mode ${route.mode}.`);
      else lines.push(`Physical route ${route.status}${route.reason ? `: ${route.reason}` : ''}.`);
      break;
    }
    case 'batch': {
      const ops = Array.isArray(p.operations) ? p.operations : [];
      lines.push(`Atomic batch of ${ops.length} operation${ops.length === 1 ? '' : 's'}: all apply or none; ` +
        'one revision, one undo step and one rebuild.');
      ops.forEach((op, i) => {
        const inner = describeCommand({ type: op && op.type, payload: (op && op.payload) || {}, assumptions: op && op.assumptions }, project);
        lines.push(`${i + 1}. ${inner.join(' ')}`);
      });
      break;
    }
    case 'undo':
    case 'redo':
      lines.push(`${cmd.type === 'undo' ? 'Undo' : 'Redo'} one step of persisted history.`);
      break;
    default:
      lines.push(`${cmd.type} with payload fields ${Object.keys(p).join(', ')}.`);
  }
  if (added.length) lines.push(`Adds assumption record(s): ${added.join(', ')}.`);
  return lines;
}
