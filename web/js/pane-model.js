// View-pane layout model (pure; unit-tested in Node).
//
// The workspace shows two or four independent panes. Each pane chooses one of six
// content types and its own Live/Published source; navigation (camera, paper view,
// scroll) belongs to the pane controller. Hidden panes keep their choices. Focus is
// a presentation state that hides the global chrome only; it records the panel
// state at entry so leaving focus restores exactly that state.

export const PANE_TYPES = Object.freeze(['3d', 'pid', 'legend', 'index', 'description', 'outputs']);
export const PANE_LABELS = Object.freeze({
  '3d': { full: '3D', short: '3D', title: '3D view · CAD tessellation' },
  pid: { full: 'P&ID', short: 'P&ID', title: 'P&ID sheet P&ID-001' },
  legend: { full: 'Legend', short: 'Leg', title: 'Legend sheet LEG-001 (exact, from the drawn inventory)' },
  index: { full: 'Index', short: 'Idx', title: 'Tagged-item index with sheet zones' },
  description: { full: 'Description', short: 'Desc', title: 'Tagged process description' },
  outputs: { full: 'Outputs', short: 'Out', title: 'All outputs, sources and downloads' },
});
/** Initial choices: the two-pane default is 3D / P&ID; the first four-pane view adds Legend / Index. */
export const DEFAULT_TYPES = Object.freeze(['3d', 'pid', 'legend', 'index']);
export const LAYOUTS = Object.freeze([2, 4]);
export const MODES = Object.freeze(['live', 'published']);
export const SHEET_TYPES = new Set(['pid', 'legend']);
/** Artifact file key each content type loads (outputs lists every key). */
export const TYPE_FILE_KEY = Object.freeze({ '3d': 'display', pid: 'pid', legend: 'legend', index: 'index', description: 'description', outputs: null });

export function createLayout() {
  return { panes: 2, types: [...DEFAULT_TYPES], modes: ['live', 'live', 'live', 'live'], maximized: null, focus: null };
}

export function paneCount(layout) {
  return layout.panes === 4 ? 4 : 2;
}

/** Panes that belong to the current layout (independent of maximize). */
export function layoutIndices(layout) {
  return [...Array(paneCount(layout)).keys()];
}

/** Panes on screen: the layout's panes, or only the maximized one. */
export function visibleIndices(layout) {
  const n = paneCount(layout);
  if (layout.maximized != null && layout.maximized < n) return [layout.maximized];
  return layoutIndices(layout);
}

export function setLayout(layout, panes) {
  if (!LAYOUTS.includes(panes)) throw new Error(`unsupported layout: ${panes} panes`);
  const next = { ...layout, panes };
  if (next.maximized != null && next.maximized >= panes) next.maximized = null;
  return next;
}

export function setPaneType(layout, index, type) {
  if (!PANE_TYPES.includes(type)) throw new Error(`unknown pane type: ${type}`);
  if (!(index >= 0 && index < 4)) throw new Error(`no pane ${index}`);
  const types = layout.types.slice();
  types[index] = type;
  return { ...layout, types };
}

export function setPaneMode(layout, index, mode) {
  if (!MODES.includes(mode)) throw new Error(`unknown source mode: ${mode}`);
  if (!(index >= 0 && index < 4)) throw new Error(`no pane ${index}`);
  const modes = layout.modes.slice();
  modes[index] = mode;
  return { ...layout, modes };
}

export function toggleMaximized(layout, index) {
  return { ...layout, maximized: layout.maximized === index || index == null ? null : index };
}

/** Enter focus, recording the panel state to restore. Entering twice keeps the first record. */
export function enterFocus(layout, panels) {
  if (layout.focus) return layout;
  return { ...layout, focus: { saved: { ...panels } } };
}

/** Leave focus: {layout, restore} where restore is the panel state recorded at entry (or null). */
export function exitFocus(layout) {
  if (!layout.focus) return { layout, restore: null };
  return { layout: { ...layout, focus: null }, restore: { ...layout.focus.saved } };
}

/**
 * What Escape does, most local first: cancel an active 3D move preview, cancel a sheet pick,
 * close the assistant, leave focus, restore a maximized pane, clear the selection.
 */
export function escapeAction({ moving = false, picking = false, assistant = false, focus = false, maximized = null, selection = 0 } = {}) {
  if (moving) return 'cancel-move';
  if (picking) return 'cancel-pick';
  if (assistant) return 'close-assistant';
  if (focus) return 'exit-focus';
  if (maximized != null) return 'restore-max';
  if (selection) return 'clear-selection';
  return null;
}

// ---------------------------------------------------------------- DOM identifiers

const LEGACY_3D = {
  host: 'gl-host', badge: 'src-3d', mode: 'mode-3d', note: 'note-3d', foot: 'foot-3d', empty: 'empty-3d',
  fit: 'btn-3d-fit', focus: 'btn-3d-focus', labels: 'btn-3d-labels', labelsState: 'labels-state', edges: 'btn-3d-edges',
};
const LEGACY_DOC = {
  tools: 'sheet-tools', badge: 'src-doc', mode: 'mode-doc', note: 'note-doc', foot: 'foot-doc', readout: 'sheet-readout',
  fit: 'btn-sheet-fit', zoomOut: 'btn-sheet-out', zoomIn: 'btn-sheet-in', actual: 'btn-sheet-1x',
};
export const PANE_ROLES = Object.freeze(['root', 'tabs', 'tools', 'badge', 'mode', 'note', 'foot', 'readout', 'empty', 'host',
  'fit', 'focus', 'labels', 'labelsState', 'edges', 'coords', 'move', 'zoomOut', 'zoomIn', 'actual', 'max']);

/**
 * Element IDs of one pane showing one type. Earlier single-view hooks are kept where they
 * still mean the same thing: pane 0 showing 3D uses the former 3D IDs (#gl-host, #src-3d, …),
 * pane 1 showing a document uses the former document IDs (#src-doc, #pane-pid, …). Pane roots
 * and tab strips keep slot IDs (#view-3d, #view-sheet, #doc-tabs). Everything else is scoped
 * as p<index>-<role>, so no two panes ever share an ID.
 */
export function paneIds(index, type) {
  const scoped = (role) => `p${index}-${role}`;
  const ids = {};
  for (const role of PANE_ROLES) ids[role] = scoped(role);
  ids.host = `p${index}-${type}`;
  ids.root = ['view-3d', 'view-sheet'][index] || `view-p${index}`;
  if (index === 1) ids.tabs = 'doc-tabs';
  if (index === 0 && type === '3d') Object.assign(ids, LEGACY_3D);
  if (index === 1 && type !== '3d') Object.assign(ids, LEGACY_DOC, { host: `pane-${type}` });
  return ids;
}
