// Pane layout, focus restoration, Escape order and per-pane DOM identifiers (pane-model.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_TYPES, PANE_ROLES, PANE_TYPES, createLayout, enterFocus, escapeAction, exitFocus, layoutIndices, paneIds,
  setLayout, setPaneMode, setPaneType, toggleMaximized, visibleIndices,
} from '../../../web/js/pane-model.js';

test('defaults: two panes 3D / P&ID; the first four-pane layout adds Legend / Index', () => {
  const two = createLayout();
  assert.equal(two.panes, 2);
  assert.deepEqual(visibleIndices(two), [0, 1]);
  assert.deepEqual(two.types.slice(0, 2), ['3d', 'pid']);
  const four = setLayout(two, 4);
  assert.deepEqual(visibleIndices(four), [0, 1, 2, 3]);
  assert.deepEqual(four.types, ['3d', 'pid', 'legend', 'index']);
  assert.deepEqual(DEFAULT_TYPES, ['3d', 'pid', 'legend', 'index']);
  assert.deepEqual(PANE_TYPES, ['3d', 'pid', 'legend', 'index', 'description', 'outputs']);
});

test('every pane can take every type, duplicates included; choices survive layout switches', () => {
  let layout = setLayout(createLayout(), 4);
  layout = setPaneType(layout, 2, '3d');
  layout = setPaneType(layout, 3, '3d');
  layout = setPaneMode(layout, 3, 'published');
  assert.deepEqual(layout.types, ['3d', 'pid', '3d', '3d']);
  const two = setLayout(layout, 2);
  assert.deepEqual(visibleIndices(two), [0, 1]);
  assert.deepEqual(two.types, layout.types, 'hidden panes keep their type');
  assert.deepEqual(two.modes, ['live', 'live', 'live', 'published'], 'and their source mode');
  assert.deepEqual(setLayout(two, 4).types, ['3d', 'pid', '3d', '3d']);
  for (let i = 0; i < 4; i += 1) for (const t of PANE_TYPES) assert.equal(setPaneType(layout, i, t).types[i], t);
  assert.throws(() => setPaneType(layout, 0, 'cad'), /unknown pane type/);
  assert.throws(() => setPaneType(layout, 4, 'pid'), /no pane/);
  assert.throws(() => setPaneMode(layout, 0, 'draft'), /unknown source mode/);
  assert.throws(() => setLayout(layout, 3), /unsupported layout/);
});

test('transitions return new layouts and never mutate the previous one', () => {
  const a = createLayout();
  const frozen = JSON.stringify(a);
  setPaneType(a, 1, 'outputs');
  setPaneMode(a, 0, 'published');
  setLayout(a, 4);
  toggleMaximized(a, 1);
  enterFocus(a, { dockHidden: true });
  assert.equal(JSON.stringify(a), frozen);
});

test('maximize shows one pane; leaving a layout that hid it clears the maximize', () => {
  let layout = setLayout(createLayout(), 4);
  layout = toggleMaximized(layout, 3);
  assert.deepEqual(visibleIndices(layout), [3]);
  assert.deepEqual(layoutIndices(layout), [0, 1, 2, 3]);
  assert.equal(toggleMaximized(layout, 3).maximized, null, 'second toggle restores');
  assert.equal(toggleMaximized(layout, null).maximized, null);
  const two = setLayout(layout, 2);
  assert.equal(two.maximized, null);
  assert.deepEqual(visibleIndices(two), [0, 1]);
  assert.equal(setLayout(toggleMaximized(createLayout(), 1), 4).maximized, 1, 'a pane still in the layout stays maximized');
});

test('focus records the exact panel state at entry and returns it on exit', () => {
  const panels = { inspectorHidden: true, inspectorOpen: false, dockHidden: false };
  let layout = enterFocus(setLayout(createLayout(), 4), panels);
  assert.deepEqual(layout.focus.saved, panels);
  panels.dockHidden = true; // later changes to the caller's object do not leak into the record
  assert.equal(layout.focus.saved.dockHidden, false);
  const again = enterFocus(layout, { inspectorHidden: false, inspectorOpen: true, dockHidden: true });
  assert.equal(again, layout, 'entering twice keeps the first record');
  layout = setPaneType(layout, 0, 'legend'); // pane changes during focus are kept
  const { layout: after, restore } = exitFocus(layout);
  assert.equal(after.focus, null);
  assert.deepEqual(restore, { inspectorHidden: true, inspectorOpen: false, dockHidden: false });
  assert.equal(after.types[0], 'legend');
  assert.equal(after.panes, 4);
  assert.deepEqual(exitFocus(after), { layout: after, restore: null });
});

test('Escape: cancel move, cancel pick, close the assistant, leave focus, restore maximize, clear selection', () => {
  assert.equal(escapeAction({ moving: true, picking: true, assistant: true, focus: true, maximized: 1, selection: 2 }), 'cancel-move');
  assert.equal(escapeAction({ picking: true, assistant: true, focus: true, maximized: 1, selection: 2 }), 'cancel-pick');
  assert.equal(escapeAction({ assistant: true, focus: true, maximized: 1, selection: 2 }), 'close-assistant');
  assert.equal(escapeAction({ picking: true, focus: true, maximized: 1, selection: 2 }), 'cancel-pick');
  assert.equal(escapeAction({ focus: true, maximized: 1, selection: 2 }), 'exit-focus');
  assert.equal(escapeAction({ maximized: 0, selection: 2 }), 'restore-max');
  assert.equal(escapeAction({ selection: 1 }), 'clear-selection');
  assert.equal(escapeAction({}), null);
});

test('legacy hooks stay on the first panes; IDs are unique for every type combination', () => {
  const p0 = paneIds(0, '3d');
  assert.equal(p0.host, 'gl-host');
  assert.equal(p0.badge, 'src-3d');
  assert.equal(p0.root, 'view-3d');
  assert.equal(p0.labelsState, 'labels-state');
  assert.equal(p0.fit, 'btn-3d-fit');
  const p1 = paneIds(1, 'pid');
  assert.equal(p1.tabs, 'doc-tabs');
  assert.equal(p1.badge, 'src-doc');
  assert.equal(p1.host, 'pane-pid');
  assert.equal(p1.root, 'view-sheet');
  assert.equal(p1.zoomIn, 'btn-sheet-in');
  assert.equal(paneIds(1, 'legend').host, 'pane-legend');
  assert.equal(paneIds(1, 'outputs').host, 'pane-outputs');
  // a legacy ID never describes the wrong kind of content
  assert.equal(paneIds(0, 'pid').badge, 'p0-badge');
  assert.equal(paneIds(1, '3d').host, 'p1-3d');
  assert.equal(paneIds(1, '3d').badge, 'p1-badge');
  assert.equal(paneIds(2, '3d').host, 'p2-3d');
  let combos = 0;
  for (const a of PANE_TYPES) for (const b of PANE_TYPES) for (const c of PANE_TYPES) for (const d of PANE_TYPES) {
    const ids = [a, b, c, d].flatMap((t, i) => PANE_ROLES.map((r) => paneIds(i, t)[r]));
    assert.equal(new Set(ids).size, ids.length, `${a},${b},${c},${d}`);
    combos += 1;
  }
  assert.equal(combos, 6 ** 4);
});
