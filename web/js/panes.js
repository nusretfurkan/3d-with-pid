// Independent view panes. Each pane chooses one of six content types and owns that
// content's controller: its own latest-wins loader(s), installed-source badge,
// Live/Published choice, tools and navigation (3D camera, paper view, scroll). Panes
// share the workspace state, the semantic selection and the immutable artifact caches;
// nothing in a pane mutates the project. Switching a pane's type disposes the previous
// controller (WebGL context, observers, listeners) after recording its navigation, so
// returning to that type restores it. Panes outside the layout are paused: they keep
// their content and choices but neither load nor render until shown again.
//
// A 3D pane also has display-only Coordinates and a deliberate Move tool (gizmo drag or typed
// X/Y/Z, move-panel.js). Its viewer only reports move intent; the application's coordinator
// (move-coordinator.js) owns the one move record and the single command. While a submitted
// move waits for regenerated geometry, a Live pane showing the move's base scene keeps that
// scene (with the labelled preview) instead of installing progressive files of the new build.
// With one line selected, the Move tool edits that line's straight sections instead (section-panel.js,
// pipe-move-coordinator.js): the same gates, one edit at a time, Apply sends one move_route_segment.

import { h, replace, icon } from './dom.js';
import { createLoader } from './loader.js';
import { checkIdentity, offeredSources, slotBadge } from './view-source.js';
import { shortBuild, shortDigest, syncBadge, viewSource } from './status.js';
import { PANE_LABELS, PANE_TYPES, SHEET_TYPES, paneIds } from './pane-model.js';
import { SheetView } from './sheet.js';
import { createDescriptionContent, createIndexContent, createOutputsContent } from './documents.js';
import { DEFAULT_SNAP_M } from './equipment-move.js';
import { createMovePanel } from './move-panel.js';
import { createSectionPanel } from './section-panel.js';
import { pipeCapable } from './pipe-move-coordinator.js';

const LABEL_MODES = ['all', 'equipment', 'off'];
const LABEL_TEXT = { all: 'All', equipment: 'Equip', off: 'Off' };

function setBadge(el, b) {
  el.className = `badge tone-${b.tone}`;
  el.title = b.detail || '';
  replace(el, h('i', { class: 'dot' }), b.label);
}

function setNote(el, note) {
  el.hidden = !note;
  if (!note) return;
  el.className = `plate-note tone-${note.tone}`;
  el.textContent = note.text;
}

function tool(act, title, ...children) {
  return h('button', { class: 'tool', type: 'button', title, 'aria-label': title.replace(/\s*\(.*\)$/, ''), dataset: { act } }, ...children);
}

export class Pane {
  constructor(app, index, { type, mode }) {
    this.app = app;
    this.index = index;
    this.type = type;
    this.mode = mode;
    this.nav = {}; // type -> navigation recorded when the pane last left that type
    this.moveSnap = DEFAULT_SNAP_M; // drag snap of this pane's Move tool (null = off); kept across type switches
    this.active = false;
    this.content = null;
    this.generation = 0;
    this._build();
    this._mount(type);
  }

  // ---------------------------------------------------------------- structure

  _build() {
    const i = this.index;
    this.tabs = h('div', { class: 'tabs pane-tabs', role: 'tablist', 'aria-label': `Pane ${i + 1} content` },
      PANE_TYPES.map((t) => h('button', {
        type: 'button', role: 'tab', dataset: { doc: t }, title: PANE_LABELS[t].title, 'aria-label': PANE_LABELS[t].title,
      }, h('span', { class: 't-full' }, PANE_LABELS[t].full), h('span', { class: 't-short', 'aria-hidden': 'true' }, PANE_LABELS[t].short))));
    this.tabs.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-doc]');
      if (b) this.app.setPaneType(i, b.dataset.doc);
    });
    this.tabs.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const k = PANE_TYPES.indexOf(this.type) + (e.key === 'ArrowRight' ? 1 : -1);
      const next = PANE_TYPES[(k + PANE_TYPES.length) % PANE_TYPES.length];
      this.app.setPaneType(i, next);
      this.tabs.querySelector(`button[data-doc="${next}"]`).focus();
      e.preventDefault();
    });
    this.tools = h('div', { class: 'vh-tools' });
    this.maxButton = tool('max', 'Maximize this pane (M)', icon('max'));
    this.maxButton.addEventListener('click', () => this.app.toggleMax(i));
    this.badge = h('span', { class: 'badge tone-none' }, h('i', { class: 'dot' }), 'No output');
    this.modeSeg = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': `Pane ${i + 1} source` },
      h('button', { type: 'button', dataset: { mode: 'live' }, role: 'radio', title: 'Follow the newest build (working previews included)' }, 'Live'),
      h('button', { type: 'button', dataset: { mode: 'published' }, role: 'radio', title: 'Show the last verified complete set' }, 'Published'));
    this.modeSeg.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode]');
      if (b) this.app.setPaneMode(i, b.dataset.mode);
    });
    this.note = h('span', { class: 'plate-note', hidden: true });
    this.plate = h('div', { class: 'source-plate' }, this.badge, this.modeSeg, this.note);
    this.body = h('div', { class: 'view-body' }, this.plate);
    this.footInfo = h('span', null, '—');
    this.readout = h('span', { class: 'readout mono' });
    this.foot = h('footer', { class: 'view-foot' }, this.footInfo, this.readout);
    this.el = h('section', { class: 'view pane', dataset: { pane: String(i) } },
      h('header', { class: 'view-head' }, this.tabs, this.tools), this.body, this.foot);
    this.el.addEventListener('pointerenter', () => { this.app.hoveredPane = i; });
    // the pane last worked in is the one whose shown source the assistant names
    for (const type of ['pointerdown', 'wheel', 'focusin']) {
      this.el.addEventListener(type, () => this.app.noteWorkedPane(i), { passive: true });
    }
    this._renderMode();
  }

  _renderMode() {
    for (const b of this.modeSeg.querySelectorAll('button')) {
      const on = b.dataset.mode === this.mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', String(on));
    }
  }

  _applyIds() {
    const ids = paneIds(this.index, this.type);
    this.ids = ids;
    this.el.id = ids.root;
    this.tabs.id = ids.tabs;
    this.tools.id = ids.tools;
    this.badge.id = ids.badge;
    this.modeSeg.id = ids.mode;
    this.note.id = ids.note;
    this.foot.id = ids.foot;
    this.readout.id = ids.readout;
    this.host.id = ids.host;
    this.maxButton.id = ids.max;
    for (const b of this.tools.querySelectorAll('[data-act]')) {
      if (ids[b.dataset.act]) b.id = ids[b.dataset.act];
    }
    if (this.labelsState) this.labelsState.id = ids.labelsState;
    if (this.empty) this.empty.id = ids.empty;
  }

  // ---------------------------------------------------------------- content

  _mount(type) {
    this.type = type;
    this.generation += 1;
    this.labelsState = null;
    this.empty = null;
    this.host = h('div', { class: 'pane-content' });
    this.body.append(this.host);
    this.el.classList.toggle('is-3d', type === '3d');
    this.el.classList.toggle('is-doc', type !== '3d');
    this.body.classList.toggle('doc-body', type !== '3d');
    this.el.setAttribute('aria-label', `Pane ${this.index + 1}: ${PANE_LABELS[type].title}`);
    for (const b of this.tabs.querySelectorAll('button')) {
      const on = b.dataset.doc === type;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    const ctx = { app: this.app, pane: this, host: this.host, nav: this.nav[type] || null, makeLoader: (label, fetch, install) => this._makeLoader(label, fetch, install) };
    let tools = [];
    if (type === '3d') {
      this.content = this._create3D(ctx);
      tools = this.content.tools;
    } else if (SHEET_TYPES.has(type)) {
      this.content = this._createSheet(ctx);
      tools = this.content.tools;
    } else if (type === 'index') {
      this.host.classList.add('doc-pane', 'scroll');
      this.content = createIndexContent(ctx);
    } else if (type === 'description') {
      this.host.classList.add('doc-pane', 'desc');
      this.content = createDescriptionContent(ctx);
    } else {
      this.host.classList.add('doc-pane', 'scroll');
      this.content = createOutputsContent(ctx);
    }
    replace(this.tools, tools, this.maxButton);
    this._applyIds();
    this.readout.textContent = '';
    this.footInfo.textContent = '—';
  }

  /** Switch this pane to another type; navigation of the old type is kept for a later return. */
  setType(type) {
    if (type === this.type) return;
    this.nav[this.type] = this.content.navState() || this.nav[this.type] || null;
    this._teardown();
    this._mount(type);
    this.content.setActive?.(this.active);
    if (this.active && this.app.state) this.update(this.app.state);
    this.app.paintPane(this);
  }

  setMode(mode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this._renderMode();
    if (this.active && this.app.state) this.update(this.app.state);
  }

  setActive(on) {
    if (on === this.active) return;
    this.active = on;
    this.content.setActive?.(on);
    if (on && this.app.state) this.update(this.app.state);
  }

  _teardown() {
    const c = this.content;
    if (c.loader) c.loader.want(null, null); // a fetch in flight completes unwanted and is discarded
    c.destroy();
    this.host.remove();
    this.content = null;
  }

  destroy() {
    this._teardown();
    this.el.remove();
  }

  _makeLoader(label, fetch, install) {
    let reported = null; // each failure is announced once; the badge keeps showing it
    const generation = this.generation;
    return createLoader({
      fetch,
      install: (data, target) => {
        if (generation !== this.generation) throw new Error('the pane shows other content now');
        return install(data, target);
      },
      onChange: (slot) => {
        if (generation !== this.generation) return;
        this.renderBadge();
        if (this.content && this.content.loaderChanged) this.content.loaderChanged(slot);
        if (slot.error && slot.desired && slot.error.url === slot.desired.url && slot.error !== reported) {
          reported = slot.error;
          this.app.toast('err', `${label} could not be shown`, [`${slot.error.url}: ${slot.error.message}`, 'The previous content stays on screen with its own identity.']);
        }
      },
    });
  }

  // ---------------------------------------------------------------- 3D

  _create3D({ app, host, nav, makeLoader }) {
    const pane = this;
    host.classList.add('gl-host');
    this.empty = h('div', { class: 'view-empty' }, 'Waiting for the first scene from the local service…');
    host.append(this.empty, h('div', { class: 'view-hint' }, 'Drag orbit · right-drag pan · wheel zoom · double-click focus'));
    let viewer = null;
    let destroyed = false;
    let stats = null;
    const empty = this.empty;
    const loader = makeLoader('3D scene', (url) => app.sceneJSON(url), (json, target) => {
      checkIdentity('3D scene', { revision: json.source_revision, digest: json.input_digest }, target.src);
      stats = viewer.setScene(json, { ...target.src, url: target.url });
      if (stats.invalid.length) {
        app.toast('warn', '3D scene has unusable components', stats.invalid.slice(0, 5).map((i) => `${i.id || '(no id)'}: ${i.reason}`));
      }
      empty.hidden = true;
      pane.renderFooter();
      app.paintPane(pane);
      app.sceneChanged(pane);
    });

    this.labelsState = h('span', { class: 'tool-state' }, LABEL_TEXT[(nav && nav.labelMode) || 'all']);
    const views = h('div', { class: 'seg compact', role: 'group', 'aria-label': 'Standard views' },
      [['iso', 'Iso', 'Isometric (1)'], ['front', 'Front', 'Front, looking +Y (2)'], ['top', 'Top', 'Top, looking −Z (3)'], ['right', 'Right', 'Right, looking −X (4)']]
        .map(([v, label, title]) => h('button', { type: 'button', dataset: { view: v }, title }, h('span', { class: 't-full' }, label), h('span', { class: 't-short', 'aria-hidden': 'true' }, label[0]))));
    views.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-view]');
      if (b && viewer) viewer.setView(b.dataset.view);
    });
    const fit = tool('fit', 'Fit all (0)', icon('fit'));
    fit.addEventListener('click', () => viewer && viewer.fitAll());
    const focus = tool('focus', 'Focus selection (F)', icon('focus'));
    focus.addEventListener('click', () => viewer && viewer.focusSelection());
    const labels = tool('labels', 'Labels: all / equipment / off (L)', icon('label'), this.labelsState);
    labels.addEventListener('click', () => {
      if (!viewer) return;
      const next = LABEL_MODES[(LABEL_MODES.indexOf(viewer.labelMode) + 1) % LABEL_MODES.length];
      viewer.setLabelMode(next);
      this.labelsState.textContent = LABEL_TEXT[next];
    });
    const edges = tool('edges', 'Feature edges (E)', icon('edges'));
    edges.classList.toggle('on', !nav || nav.edgesOn !== false);
    edges.addEventListener('click', () => {
      if (!viewer) return;
      viewer.setEdges(!viewer.edgesOn);
      edges.classList.toggle('on', viewer.edgesOn);
    });
    // display only: world axes, XY grid at Z = 0 and the selected origin; kept in this pane's navigation
    const coords = tool('coords', 'Coordinates: world axes, XY grid at Z = 0 and the selected origin (display only)', icon('axes'));
    coords.addEventListener('click', () => {
      if (!viewer) return;
      viewer.setCoordinates(!viewer.coordinatesOn);
      syncTools();
    });
    // deliberate tool, off by default: ordinary select and orbit otherwise
    let moveOn = false;
    const move = tool('move', 'Move: equipment (drag a world axis or plane handle, or type X/Y/Z) or a straight section of one selected line', icon('move'));
    move.addEventListener('click', () => setMove(!moveOn));
    const equipmentPanel = createMovePanel({ app, pane, host, viewer: () => viewer });
    const sectionPanel = createSectionPanel({ app, pane, host, viewer: () => viewer });
    host.append(equipmentPanel.el, sectionPanel.el);
    // one Move slot: the section panel takes it while one line is selected
    const panel = {
      render() {
        sectionPanel.render();
        equipmentPanel.render();
        if (sectionPanel.shown) equipmentPanel.el.hidden = true;
      },
      setEnabled(on) {
        sectionPanel.setEnabled(on);
        equipmentPanel.setEnabled(on);
        if (sectionPanel.shown) equipmentPanel.el.hidden = true;
      },
      dispose() { equipmentPanel.dispose(); sectionPanel.dispose(); },
      contains: (node) => equipmentPanel.contains(node) || sectionPanel.contains(node),
      snapshot: () => equipmentPanel.snapshot(),
      section: sectionPanel,
    };
    let held = null; // {url, revision, kind, keep}: newer file not installed while a submitted move keeps the base scene
    let lastInstalled = null;

    function syncTools() {
      const c = viewer ? viewer.coordinatesOn : !!(nav && nav.coordinatesOn);
      coords.classList.toggle('on', c);
      coords.setAttribute('aria-pressed', String(c));
      move.classList.toggle('on', moveOn);
      move.setAttribute('aria-pressed', String(moveOn));
    }

    function setMove(on) {
      moveOn = !!on;
      if (!moveOn) {
        app.cancelEquipmentMove('move-disabled', pane.index); // a typed preview of this pane
        app.cancelPipeMove('move-disabled', pane.index);
      }
      if (viewer) viewer.setMoveEnabled(moveOn);
      host.classList.toggle('move-on', moveOn);
      panel.setEnabled(moveOn);
      syncTools();
    }

    /**
     * The installed file {url, src}, its build status as the service describes that URL now (like the
     * badge): a scene installed while its build was still running is the same immutable file once the
     * build completes. The identity (revision, digest, build) never changes; a URL no longer offered
     * keeps the description it was installed with.
     */
    function installedNow() {
      const installed = loader.state().installed;
      if (!installed || !app.state) return installed;
      const now = offeredSources(app.state).get(installed.url);
      if (!now || now.source_revision !== installed.src.source_revision || now.input_digest !== installed.src.input_digest || now.build_id !== installed.src.build_id) return installed;
      const { key, url, ...src } = now;
      return { url: installed.url, src: { ...installed.src, ...src } };
    }

    /** The authoritative context the viewer gates moves and labels coordinates with. */
    function pushContext() {
      if (viewer) {
        const s = app.state;
        viewer.setEngineeringContext(s ? {
          project: s.project, inputDigest: s.document ? s.document.input_digest : null, online: app.online === true,
          mode: pane.mode, installedSource: installedNow(), move: app.equipmentMove,
          section: app.section, pipeMove: app.pipeSectionMove, pipeCapable: pipeCapable(s), paneIndex: pane.index,
        } : null);
      }
      panel.render();
    }

    import('./viewer3d.js').then(({ Viewer3D }) => {
      if (destroyed) return;
      try {
        viewer = new Viewer3D(host, {
          onPick: (hit) => app.pickFromView(hit, 'display', pane.index),
          onError: (message) => app.toast('err', '3D view', [message]),
          initialView: nav,
          onMoveBegin: (intent) => app.beginEquipmentMove(pane.index, intent),
          onMovePreview: (position) => app.previewEquipmentMove(position, pane.index),
          onMoveCancel: (reason) => app.cancelEquipmentMove(reason, pane.index),
          onMoveCommit: (position) => { app.commitEquipmentMove(position, pane.index); },
          onMoveAvailability: () => panel.render(),
          onPipeBegin: (intent) => app.beginPipeMove(pane.index, intent),
          onPipePreview: (offset) => app.previewPipeMove(offset, pane.index),
          onPipeCancel: (reason) => app.cancelPipeMove(reason, pane.index),
          onPipeAvailability: () => panel.render(),
        });
      } catch (error) {
        empty.classList.add('error');
        empty.textContent = `The 3D view cannot start: ${error.message || error}`;
        return;
      }
      viewer.setMoveSnap(pane.moveSnap);
      viewer.setMoveEnabled(moveOn);
      syncTools();
      viewer.setActive(pane.active);
      pushContext();
      if (pane.active && app.state) pane.update(app.state);
    }).catch((error) => {
      empty.classList.add('error');
      empty.textContent = `The 3D module failed to load: ${error.message || error}`;
    });
    syncTools();

    return {
      type: '3d',
      loader,
      panel,
      tools: [views, fit, focus, labels, edges, coords, move],
      get viewer() { return viewer; },
      get stats() { return stats; },
      get moveOn() { return moveOn; },
      get held() { return held; },
      installedNow,
      update(state) {
        if (!viewer) return; // started once the module has loaded
        const src = viewSource(state, 'display', pane.mode);
        const holding = { mode: pane.mode, installed: loader.state().installed, offered: src };
        const keep = app.moves.hold(holding) || app.pipeMoves.hold(holding);
        held = keep ? { url: src.url, revision: src.source_revision, kind: src.kind, keep: keep.url, keepRevision: keep.src.source_revision } : null;
        if (keep) loader.want(keep.url, keep.src);
        else loader.want(src ? src.url : null, src);
        const slot = loader.state();
        if (!slot.installed && !slot.desired) {
          empty.hidden = false;
          empty.textContent = pane.mode === 'published' ? 'No verified published scene exists.' : 'No scene has been generated yet.';
        }
        pushContext();
      },
      /** A newly installed scene: context and the coordinator learn the pane's new identity. */
      loaderChanged(slot) {
        const url = slot.installed ? slot.installed.url : null;
        if (url === lastInstalled) return;
        lastInstalled = url;
        pushContext();
        app.movePaneChanged(pane);
      },
      /** The global move record changed. */
      moveChanged() {
        if (pane.active && app.state) pane.update(app.state);
        else pushContext();
      },
      pushContext,
      setSelection(ids, resolved) {
        if (!viewer) return;
        const { components, ports } = app.highlightFor(ids, resolved, viewer);
        viewer.setSelection(components, ports, ids);
        pushContext();
      },
      setActive(on) { if (viewer) viewer.setActive(on); },
      focusSelection() { if (viewer) viewer.focusSelection(); },
      command(name) {
        if (!viewer) return;
        if (name === 'fit') viewer.fitAll();
        else if (name === 'labels') labels.click();
        else if (name === 'edges') edges.click();
        else viewer.setView(name);
      },
      navState: () => (viewer ? viewer.viewState() : nav),
      destroy() {
        destroyed = true;
        held = null;
        panel.dispose();
        if (viewer) viewer.dispose();
        viewer = null;
        replace(host);
      },
    };
  }

  // ---------------------------------------------------------------- sheets

  _createSheet({ app, host, nav, makeLoader }) {
    const pane = this;
    const type = this.type;
    const label = type === 'pid' ? 'P&ID' : 'Legend';
    host.classList.add('doc-pane', 'sheet-host');
    host.append(h('div', { class: 'view-empty' }, `Waiting for the ${type === 'pid' ? 'P&ID' : 'legend'}…`));
    const sheet = new SheetView(host, {
      onPick: (hit) => app.pickFromView(hit, 'pid'),
      readout: (p) => { pane.readout.textContent = `paper ${p.x.toFixed(0)}, ${p.y.toFixed(0)} · stored ${(p.x - 100).toFixed(0)}, ${(p.y - 100).toFixed(0)}`; },
      initialView: nav ? nav.view : null,
    });
    const loader = makeLoader(label, (url) => app.cachedText(url), (text, target) => {
      sheet.setSvg(text, { ...target.src, url: target.url }, (root) => {
        checkIdentity(label, { revision: Number(root.getAttribute('data-source-revision')), digest: root.getAttribute('data-input-digest') }, target.src);
      });
      const empty = host.querySelector('.view-empty');
      if (empty) empty.remove();
      pane.renderFooter();
      app.paintPane(pane);
    });
    const fit = tool('fit', 'Fit sheet', icon('fit'));
    fit.addEventListener('click', () => sheet.fit());
    const out = tool('zoomOut', 'Zoom out', icon('zoom-out'));
    out.addEventListener('click', () => sheet.zoomBy(1 / 1.4));
    const zin = tool('zoomIn', 'Zoom in', icon('zoom-in'));
    zin.addEventListener('click', () => sheet.zoomBy(1.4));
    const actual = tool('actual', '1 paper unit = 1 px', '1:1');
    actual.addEventListener('click', () => sheet.actualSize());
    return {
      type,
      loader,
      sheet,
      tools: [fit, out, zin, actual],
      update(state) {
        const src = viewSource(state, type, pane.mode);
        loader.want(src ? src.url : null, src);
      },
      setSelection(ids, resolved) {
        const { occurrences } = app.highlightFor(ids, resolved, null);
        sheet.setSelection(ids, occurrences);
      },
      focusSelection() {
        const anchor = sheet.selectionAnchor();
        if (anchor) sheet.centerOn(anchor.x, anchor.y);
      },
      navState: () => ({ view: sheet.viewState() }),
      destroy() {
        sheet.destroy();
        replace(host);
      },
    };
  }

  // ---------------------------------------------------------------- rendering

  update(state) {
    if (!this.active || !this.content) return;
    this.content.update(state);
    this.renderBadge();
    this.renderFooter();
  }

  setSelection(ids, resolved) {
    if (this.content) this.content.setSelection(ids, resolved);
  }

  /** The badge describes installed content (loader state), never merely the requested source. */
  renderBadge() {
    const state = this.app.state;
    if (!state || !this.content) return;
    const loader = this.content.loader;
    if (loader) {
      const b = slotBadge(loader.state(), state);
      setBadge(this.badge, b);
      const held = this.type === '3d' ? this.content.held : null;
      // the badge still describes the installed (base) scene; the note says what is not installed and why
      setNote(this.note, held ? {
        tone: 'loading', text: `r${held.revision} ${held.kind === 'preview' ? 'generating' : 'not final'} · r${held.keepRevision} kept for the move`,
      } : b.note);
      if (held) this.note.title = `${held.url} is not installed yet: this pane keeps the scene the move preview was drawn on until a final scene of the new revision is offered, so the moved item is never shown twice or back at its old position.`;
      else this.note.removeAttribute('title');
      if (this.type === '3d') this.host.classList.toggle('not-offered', b.tone === 'failed' && !!loader.state().installed);
    } else {
      setBadge(this.badge, syncBadge(state));
      setNote(this.note, null);
    }
  }

  renderFooter() {
    const state = this.app.state;
    if (!this.content) return;
    if (this.type === '3d') {
      const v = this.content.viewer;
      if (!v || !v.source) return;
      const s = v.source;
      const info = v.debugInfo();
      this.footInfo.textContent = `${s.kind === 'preview' ? 'preview' : 'output'} · build ${shortBuild(s.build_id)} · r${s.source_revision} · ${shortDigest(s.input_digest)}`;
      this.readout.textContent = `${info.components - info.ghosts} components${info.ghosts ? ` + ${info.ghosts} from previous view` : ''} · ${info.triangles.toLocaleString()} tris · m · Z up`;
      return;
    }
    const loader = this.content.loader;
    if (loader) {
      const inst = loader.state().installed;
      this.footInfo.textContent = inst
        ? `${inst.src.kind} · build ${shortBuild(inst.src.build_id)} · r${inst.src.source_revision} · ${shortDigest(inst.src.input_digest)}`
        : '—';
    } else if (state) {
      this.footInfo.textContent = state.published ? `published build ${state.published.build_id}` : 'no verified publication';
    }
  }

  /** Read-only facts for automated checks. */
  snapshot() {
    const c = this.content;
    const slot = c && c.loader ? c.loader.state() : null;
    const installed = slot && slot.installed;
    const out = {
      index: this.index,
      id: this.el.id,
      type: this.type,
      mode: this.mode,
      active: this.active,
      visible: this.active && this.el.getClientRects().length > 0,
      badge: this.badge.textContent,
      tone: (/tone-(\w+)/.exec(this.badge.className) || [])[1] || null,
      installed: installed ? {
        url: installed.url, build_id: installed.src.build_id, revision: installed.src.source_revision,
        digest: installed.src.input_digest, kind: installed.src.kind, origin: installed.src.origin,
      } : null,
      desired: slot && slot.desired ? slot.desired.url : null,
      loading: !!(slot && slot.pending),
      error: slot && slot.error ? slot.error.message : null,
      loaderStats: c && c.loader ? { ...c.loader.stats } : null,
      ids: { ...this.ids },
      generation: this.generation,
      navigation: null,
      selected: null,
      remembered: Object.keys(this.nav).filter((t) => this.nav[t]),
    };
    if (this.type === '3d' && c.viewer) {
      const info = c.viewer.debugInfo();
      out.navigation = { camera: info.camera, labelMode: info.labelMode, edgesOn: c.viewer.edgesOn, coordinatesOn: c.viewer.coordinatesOn };
      out.selected = info.selected;
      out.components = info.components;
      out.frames = info.frames;
      const m = c.viewer.moveSnapshot();
      out.tools = { coordinates: c.viewer.coordinatesOn, move: c.moveOn, snap: this.moveSnap };
      out.move = {
        enabled: m.enabled, snap: m.snap, eligible: { ok: m.eligible.ok, reason: m.eligible.reason, entityId: m.eligible.entityId },
        preview: m.preview ? { entityId: m.preview.entityId, phase: m.preview.phase, origin: m.preview.origin, delta: m.preview.delta.slice() } : null,
        gizmo: { ...m.gizmo }, active: m.active ? m.active.stage : null,
      };
      out.moveHold = c.held ? { ...c.held } : null;
      out.movePanel = c.panel.snapshot();
      out.sectionPanel = c.panel.section.snapshot();
      out.pipe = c.viewer.pipeSnapshot();
    } else if (c && c.sheet) {
      out.navigation = { view: c.sheet.viewState() };
      out.selected = c.sheet.svg ? [...c.sheet.svg.querySelectorAll('.is-sel')].map((e) => e.getAttribute('data-occurrence-id')).filter(Boolean) : [];
    } else if (c) {
      out.navigation = c.navState();
    }
    return out;
  }
}
