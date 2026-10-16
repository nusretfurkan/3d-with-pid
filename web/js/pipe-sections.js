// Straight pipe sections of one connection's physical route (pure; unit-tested in Node).
//
// A section is one maximal straight centreline leg of the current authoritative route, under the
// stable parent connection ID (contract: skid/route_edit.py module docstring). Its index is scoped to
// the exact stored route points; it is an editing unit, never a new semantic item, tag or
// fabrication spool. The display keeps the generator's single conn#pipe sweep: a raycast hit point
// on that mesh is classified against the legs' straight tangency limits, so bends, flanges and
// bolting are never a straight section.
//
// This is a port of skid/route_edit.py (algorithm steps 1-6 in its module docstring): the same
// IEEE-754 operations in the same order, the same constants and the same refusal codes, checked
// against tests/fixtures/pipe-segments.json. The service's conservative equipment-body screen
// (route_edit_intrusion) is not ported: it needs the service's body boxes, so a preview may pass
// here and still be refused on Apply. The preview is tentative; Apply sends only the section
// index and offset, and the service derives and validates the committed route.

export const AXIS_TOLERANCE_M = 1e-6;
export const TERMINAL_STUB_M = 0.16;
export const MAX_OFFSET_M = 10;
export const MAX_POINTS = 16;
export const MIN_LEG_M = 0.001;
export const MIN_OFFSET_M = MIN_LEG_M; // a smaller move would vanish in simplify or only drift the stored points
export const RADIUS_MARGIN = 1.01;
export const BEND_RADIUS_FACTOR = 1.5; // skid.clearance / skid.geometry.routes: bend centreline radius = 1.5 x DN
export const CORNER_ROOM = 0.49;       // share of the shorter adjacent run a corner may use (CAD sweep)

const has = (obj, key) => !!obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key);

export class RouteEditError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'RouteEditError';
    this.code = code;
  }
}

export function isPoint(p) {
  return Array.isArray(p) && p.length === 3 && p.every((v) => typeof v === 'number' && Number.isFinite(v));
}

/** [{axis, sign, length}] of consecutive points (step 1); throws for a zero-length or non-orthogonal leg. */
export function legsOf(points) {
  const legs = [];
  for (let index = 0; index < points.length - 1; index += 1) {
    const a = points[index];
    const b = points[index + 1];
    const moving = [0, 1, 2].filter((i) => Math.abs(b[i] - a[i]) > AXIS_TOLERANCE_M);
    if (!moving.length) throw new RouteEditError('route_edit_zero_leg', `leg ${index} has zero length`);
    if (moving.length > 1) throw new RouteEditError('route_edit_not_orthogonal', `leg ${index} is not parallel to one axis`);
    const axis = moving[0];
    const delta = b[axis] - a[axis];
    legs.push({ axis, sign: delta > 0 ? 1 : -1, length: Math.abs(delta) });
  }
  return legs;
}

/** Repeated points dropped, same-direction consecutive legs merged (step 2); copies. */
export function simplify(points) {
  const kept = [];
  for (const p of points) {
    const point = p.map(Number);
    if (kept.length && kept[kept.length - 1].every((a, i) => Math.abs(a - point[i]) <= AXIS_TOLERANCE_M)) {
      if (kept.length > 1) kept[kept.length - 1] = point;
      continue;
    }
    kept.push(point);
  }
  if (kept.length < 2) throw new RouteEditError('route_edit_zero_leg', 'the route has no length');
  for (;;) {
    const legs = legsOf(kept);
    let merged = false;
    for (let i = 1; i < legs.length; i += 1) {
      if (legs[i - 1].axis === legs[i].axis) {
        if (legs[i - 1].sign !== legs[i].sign) throw new RouteEditError('route_edit_reverses', `the route reverses direction at point ${i}`);
        kept.splice(i, 1);
        merged = true;
        break;
      }
    }
    if (!merged) return kept;
  }
}

/** Corner cut at each vertex (step 3): min(bend, ROOM * min(adjacent lengths)); 0 at both ends. */
export function cutsOf(legs, nominalDiameterMm) {
  const bend = (BEND_RADIUS_FACTOR * nominalDiameterMm) / 1000;
  const cuts = new Array(legs.length + 1).fill(0);
  for (let i = 1; i < legs.length; i += 1) cuts[i] = Math.min(bend, CORNER_ROOM * Math.min(legs[i - 1].length, legs[i].length));
  return cuts;
}

/** {vertices, sections} of a point list (step 4): records as skid.route_edit.section_records. */
export function sectionRecords(points, nominalDiameterMm) {
  const vertices = simplify(points);
  const legs = legsOf(vertices);
  const cuts = cutsOf(legs, nominalDiameterMm);
  const sections = legs.map(({ axis, sign }, k) => {
    const start = vertices[k].slice();
    const end = vertices[k + 1].slice();
    start[axis] += sign * cuts[k];
    end[axis] -= sign * cuts[k + 1];
    return {
      index: k, point_start: k, point_end: k + 1, axis, start_m: start, end_m: end,
      midpoint_m: start.map((a, i) => (a + end[i]) / 2), length_m: Math.abs(end[axis] - start[axis]),
      move_axes: [0, 1, 2].filter((i) => i !== axis),
    };
  });
  return { vertices, sections };
}

/** The connection's stored route: {connection, route, points} or null. */
export function routeOf(project, connectionId) {
  const model = project && project.model;
  const physical = project && project.physical;
  if (!model || !has(model.connections, connectionId) || !physical || !has(physical.routes, connectionId)) return null;
  const route = physical.routes[connectionId];
  if (!route || !Array.isArray(route.points_m) || route.points_m.length < 2 || !route.points_m.every(isPoint)) return null;
  return { connection: model.connections[connectionId], route, points: route.points_m };
}

/** Outside radius of a connection's pipe in metres, or null. */
export function pipeRadius(connection) {
  const od = connection && Number(connection.outside_diameter_m);
  return Number.isFinite(od) && od > 0 ? od / 2 : null;
}

/**
 * Straight sections of a connection's stored route ([] when it has none or it is not a simple
 * orthogonal route): the service's straight_sections records.
 */
export function straightSections(project, connectionId) {
  const r = routeOf(project, connectionId);
  if (!r) return [];
  try {
    return sectionRecords(r.points, r.connection.nominal_diameter_mm).sections;
  } catch (error) {
    if (error instanceof RouteEditError) return [];
    throw error;
  }
}

/** A stable key of the exact route a section index refers to (indices are scoped to it). */
export function routeKey(project, connectionId) {
  const r = routeOf(project, connectionId);
  return r ? JSON.stringify(r.points) : null;
}

/**
 * The straight section a world hit point on the connection's pipe sweep lies on, or null (a bend,
 * an end beyond the tangency limits, or a point not on the pipe surface). radius: the pipe's
 * outside radius in metres; tolerance absorbs tessellation (chords lie inside the true circle).
 */
export function sectionAtPoint(sections, point, radius, { tolerance = null } = {}) {
  if (!isPoint(point) || !Array.isArray(sections) || !(radius > 0)) return null;
  const tol = tolerance ?? Math.max(0.004, radius * 0.35);
  let best = null;
  for (const s of sections) {
    const lo = Math.min(s.start_m[s.axis], s.end_m[s.axis]);
    const hi = Math.max(s.start_m[s.axis], s.end_m[s.axis]);
    if (!(hi - lo > 1e-9) || point[s.axis] < lo - 1e-6 || point[s.axis] > hi + 1e-6) continue;
    const radial = Math.hypot(...[0, 1, 2].filter((i) => i !== s.axis).map((i) => point[i] - s.start_m[i]));
    const miss = Math.abs(radial - radius);
    if (miss > tol) continue;
    if (!best || miss < best.miss) best = { index: s.index, miss };
  }
  return best ? best.index : null;
}

/** One world-axis offset: [dx, dy, dz] with value on axis (0|1|2). */
export function offsetVector(axis, value) {
  const v = [0, 0, 0];
  v[axis] = Object.is(value, -0) ? 0 : value;
  return v;
}

/** A port's outward direction snapped to a world axis (as route_edit.port_frame), or null. */
export function snapDirection(direction) {
  if (!isPoint(direction)) return null;
  const axes = [0, 1, 2].filter((i) => Math.abs(direction[i]) > 1e-12);
  if (axes.length !== 1 || Math.abs(Math.abs(direction[axes[0]]) - 1) > 1e-12) return null;
  const out = [0, 0, 0];
  out[axes[0]] = Math.sign(direction[axes[0]]);
  return out;
}

const MESSAGES = {
  route_edit_invalid_offset: 'The offset must move along exactly one world axis.',
  route_edit_no_op: 'The offset is zero; nothing would move.',
  route_edit_offset_parallel: 'A straight section moves only across its own axis.',
  route_edit_offset_too_small: `A section move must be at least ${MIN_OFFSET_M} m (${MIN_OFFSET_M * 1000} mm).`,
  route_edit_offset_too_large: `A section move is limited to ±${MAX_OFFSET_M} m.`,
};

/** Admissibility of an offset for a section, before any geometry (codes as the service): {ok, code, message}. */
export function checkOffset(section, offset) {
  const fail = (code) => ({ ok: false, code, message: MESSAGES[code] || code });
  if (!isPoint(offset)) return fail('route_edit_invalid_offset');
  const moving = [0, 1, 2].filter((i) => offset[i] !== 0);
  if (!moving.length) return fail('route_edit_no_op');
  if (moving.length > 1) return fail('route_edit_invalid_offset');
  if (!section || moving[0] === section.axis) return fail('route_edit_offset_parallel');
  if (Math.abs(offset[moving[0]]) < MIN_OFFSET_M) return fail('route_edit_offset_too_small');
  if (Math.abs(offset[moving[0]]) > MAX_OFFSET_M) return fail('route_edit_offset_too_large');
  return { ok: true, code: null, message: null };
}

const shifted = (point, axis, delta) => {
  const q = point.slice();
  q[axis] += delta;
  return q;
};

function gap(a0, a1, b0, b1) {
  let total = 0;
  for (let i = 0; i < 3; i += 1) {
    const g = Math.max(0, Math.min(b0[i], b1[i]) - Math.max(a0[i], a1[i]), Math.min(a0[i], a1[i]) - Math.max(b0[i], b1[i]));
    total += g * g;
  }
  return Math.sqrt(total);
}

const direction = (axis, sign) => {
  const unit = [0, 0, 0];
  unit[axis] = sign;
  return unit;
};
const sameDirection = (a, b) => a.every((v, i) => v === Number(b[i]));

/** Step 6 without the equipment-body screen (service only); throws RouteEditError. */
export function checkPoints(points, startDirection, endDirection, outsideDiameterM, nominalDiameterMm) {
  if (points.length > MAX_POINTS) throw new RouteEditError('route_edit_too_many_points', `the edited route would have ${points.length} points (at most ${MAX_POINTS})`);
  const legs = legsOf(points);
  if (!sameDirection(direction(legs[0].axis, legs[0].sign), startDirection)) throw new RouteEditError('route_edit_port_tangent', "the first leg would not leave along the start port's outward direction");
  const last = legs[legs.length - 1];
  if (!sameDirection(direction(last.axis, -last.sign), endDirection)) throw new RouteEditError('route_edit_port_tangent', "the last leg would not enter along the end port's inward direction");
  legs.forEach((leg, index) => {
    if (leg.length < MIN_LEG_M) throw new RouteEditError('route_edit_short_leg', `leg ${index} would be shorter than ${MIN_LEG_M * 1000} mm`);
  });
  const radius = outsideDiameterM / 2;
  cutsOf(legs, nominalDiameterMm).forEach((cut, i) => {
    if (i > 0 && i < legs.length && cut <= radius * RADIUS_MARGIN) {
      throw new RouteEditError('route_edit_bend_room', `corner ${i} would have no room for a bend larger than the pipe radius (${(cut * 1000).toFixed(1)} mm)`);
    }
  });
  for (let i = 0; i < legs.length; i += 1) {
    for (let j = i + 2; j < legs.length; j += 1) {
      if (gap(points[i], points[i + 1], points[j], points[j + 1]) < outsideDiameterM) {
        throw new RouteEditError('route_edit_self_clearance', `legs ${i} and ${j} would pass closer than one pipe diameter`);
      }
    }
  }
}

/**
 * Move straight section k by offset (steps 5-6 without the body screen): the new simplified points
 * or a RouteEditError. startDirection / endDirection: the ports' outward world axis unit vectors.
 */
export function movePoints(points, startDirection, endDirection, segmentIndex, offset, outsideDiameterM, nominalDiameterMm) {
  const vertices = simplify(points);
  const legs = legsOf(vertices);
  const n = legs.length;
  if (!Number.isInteger(segmentIndex) || segmentIndex < 0 || segmentIndex >= n) {
    throw new RouteEditError('route_edit_unknown_section', `section ${segmentIndex} does not exist; the route has ${n} straight section(s)`);
  }
  const k = segmentIndex;
  if (!isPoint(offset)) throw new RouteEditError('route_edit_invalid_offset', MESSAGES.route_edit_invalid_offset);
  const moving = [0, 1, 2].filter((i) => offset[i] !== 0);
  if (!moving.length) throw new RouteEditError('route_edit_no_op', MESSAGES.route_edit_no_op);
  if (moving.length > 1) throw new RouteEditError('route_edit_invalid_offset', MESSAGES.route_edit_invalid_offset);
  const m = moving[0];
  const d = offset[m];
  const { axis, sign, length } = legs[k];
  if (m === axis) throw new RouteEditError('route_edit_offset_parallel', MESSAGES.route_edit_offset_parallel);
  if (Math.abs(d) < MIN_OFFSET_M) throw new RouteEditError('route_edit_offset_too_small', MESSAGES.route_edit_offset_too_small);
  if (Math.abs(d) > MAX_OFFSET_M) throw new RouteEditError('route_edit_offset_too_large', MESSAGES.route_edit_offset_too_large);
  const stubs = (k === 0 ? 1 : 0) + (k === n - 1 ? 1 : 0);
  if (stubs && !(length > stubs * TERMINAL_STUB_M)) {
    throw new RouteEditError('route_edit_stub_too_short', `this end section is ${length.toFixed(4)} m long; moving it keeps a fixed ${TERMINAL_STUB_M} m nozzle stub at each port end, so it must be longer than ${Number((stubs * TERMINAL_STUB_M).toFixed(6))} m`);
  }
  let head;
  let movedStart;
  if (k === 0) {
    const stub = shifted(vertices[0], axis, sign * TERMINAL_STUB_M);
    head = [vertices[0], stub];
    movedStart = shifted(stub, m, d);
  } else {
    movedStart = shifted(vertices[k], m, d);
    head = legs[k - 1].axis === m ? vertices.slice(0, k) : vertices.slice(0, k + 1);
  }
  let tail;
  let movedEnd;
  if (k === n - 1) {
    const stub = shifted(vertices[n], axis, -sign * TERMINAL_STUB_M);
    tail = [stub, vertices[n]];
    movedEnd = shifted(stub, m, d);
  } else {
    movedEnd = shifted(vertices[k + 1], m, d);
    tail = legs[k + 1].axis === m ? vertices.slice(k + 2) : vertices.slice(k + 1);
  }
  const candidate = simplify([...head, movedStart, movedEnd, ...tail]);
  checkPoints(candidate, startDirection, endDirection, outsideDiameterM, nominalDiameterMm);
  return candidate;
}

/**
 * Tentative new points of a connection's route after moving one section (movePoints on the stored
 * route). frames: {start, end} outward port directions from the installed scene of this revision.
 */
export function moveSection(project, connectionId, segmentIndex, offset, frames) {
  const r = routeOf(project, connectionId);
  if (!r) throw new RouteEditError('route_edit_no_route', 'The connection has no stored route points.');
  if (r.route.status !== 'current') throw new RouteEditError('route_edit_not_current', 'The route is not current; reroute it first.');
  const start = frames ? snapDirection(frames.start) : null;
  const end = frames ? snapDirection(frames.end) : null;
  if (!start || !end) throw new RouteEditError('route_edit_frame_unavailable', 'A port frame of this line is not a world axis in the installed scene.');
  return movePoints(r.points, start, end, segmentIndex, offset, r.connection.outside_diameter_m, r.connection.nominal_diameter_mm);
}
