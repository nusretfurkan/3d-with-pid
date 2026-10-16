// Incremental scene update plan (pure; unit-tested in Node).
//
// Components are matched by stable ID. Geometry is reused only when positions,
// normals and indices are exactly equal (element by element); nothing is inferred
// from samples, bounds or lengths. Appearance (material, status, display-only) can
// change without rebuilding geometry.

export function sameNumbers(a, b) {
  if (a === b) return true;
  const aa = Array.isArray(a) || ArrayBuffer.isView(a);
  const bb = Array.isArray(b) || ArrayBuffer.isView(b);
  if (!aa || !bb) return (a == null || (aa && a.length === 0)) && (b == null || (bb && b.length === 0));
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
  return true;
}

/** Exact equality of the geometry buffers of an installed record and an incoming component. */
export function geometryEqual(previous, component) {
  return sameNumbers(previous.positions, component.positions)
    && sameNumbers(previous.normals, component.normals)
    && sameNumbers(previous.indices, component.indices);
}

export function appearanceKey(component) {
  const m = component.material || {};
  return JSON.stringify([m.color ?? null, m.metalness ?? null, m.roughness ?? null, !!component.display_only, component.status ?? null]);
}

/** Reason a component's buffers cannot be rendered, or null. */
export function geometryProblem(component) {
  const p = component.positions;
  if (!Array.isArray(p) || p.length < 9 || p.length % 3) return 'positions must be a flat list of at least three xyz vertices';
  for (const v of p) if (typeof v !== 'number' || !Number.isFinite(v)) return 'positions contain a non-finite value';
  const count = p.length / 3;
  const idx = component.indices;
  if (idx != null) {
    if (!Array.isArray(idx) || idx.length % 3) return 'indices must be a flat list of triangles';
    for (const i of idx) if (!Number.isInteger(i) || i < 0 || i >= count) return 'an index is outside the vertex list';
  }
  const n = component.normals;
  if (n != null && Array.isArray(n) && n.length && n.length !== p.length) return 'normals do not match the vertex count';
  return null;
}

/**
 * installed: Map id -> {geometry: {positions, normals, indices}, appearance}
 * components: incoming scene components; final: whether the scene is a final output.
 * Returns {add, replace, restyle, keep, remove, ghost, invalid}; component lists hold the
 * incoming component objects, remove/ghost hold IDs, invalid holds {id, reason}.
 */
export function planSceneUpdate(installed, components, final) {
  const plan = { add: [], replace: [], restyle: [], keep: [], remove: [], ghost: [], invalid: [] };
  const seen = new Set();
  for (const c of Array.isArray(components) ? components : []) {
    if (!c || typeof c.id !== 'string' || !c.id) {
      plan.invalid.push({ id: null, reason: 'component without an id' });
      continue;
    }
    if (seen.has(c.id)) {
      plan.invalid.push({ id: c.id, reason: 'duplicate component id' });
      continue;
    }
    const problem = geometryProblem(c);
    if (problem) {
      plan.invalid.push({ id: c.id, reason: problem });
      continue;
    }
    seen.add(c.id);
    const previous = installed.get(c.id);
    if (!previous) plan.add.push(c);
    else if (!geometryEqual(previous.geometry, c)) plan.replace.push(c);
    else if (previous.appearance !== appearanceKey(c)) plan.restyle.push(c);
    else plan.keep.push(c);
  }
  for (const id of installed.keys()) {
    if (seen.has(id)) continue;
    if (final) plan.remove.push(id);
    else plan.ghost.push(id);
  }
  return plan;
}

/** A final scene (scene.json) replaces everything; a working preview keeps unreached parts as ghosts. */
export function isFinalScene(json) {
  return !!json && !(json.preview && json.preview.final === false) && json.status !== 'working';
}
