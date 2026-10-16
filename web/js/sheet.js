// Pan/zoom/selection view for generated SVG sheets (P&ID-001, LEG-001).
// The generator output is imported as inert markup (scripts, handlers, foreign
// content and external references stripped, in addition to the service's own
// sanitization checks). View state is kept in paper units, so it survives every
// incremental replacement of the sheet. Selection uses external CSS classes only.

import { classifyRelease, movedBeyond } from './gestures.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
// <use> stays allowed: only same-document '#' references survive the href filter below.
const FORBIDDEN = 'script, foreignObject, iframe, object, embed, animate, animateMotion, animateTransform, set, image';

export function sanitizeSvg(text) {
  const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
  const root = doc.documentElement;
  if (!root || root.nodeName !== 'svg' || doc.getElementsByTagName('parsererror').length) {
    throw new Error('the sheet is not well-formed SVG');
  }
  for (const el of root.querySelectorAll(FORBIDDEN)) el.remove();
  for (const el of [root, ...root.querySelectorAll('*')]) {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim().toLowerCase();
      if (name.startsWith('on')) el.removeAttribute(attr.name);
      else if ((name === 'href' || name === 'xlink:href') && !value.startsWith('#')) el.removeAttribute(attr.name);
      else if (value.includes('javascript:') || (name === 'style' && value.includes('url('))) el.removeAttribute(attr.name);
    }
  }
  return root;
}

export class SheetView {
  constructor(host, options = {}) {
    this.host = host;
    this.options = options;
    this.paper = { w: 1200, h: 840 };
    this.view = null; // {cx, cy, scale} in paper units / px per unit
    this.svg = null;
    this.selected = { semantic: new Set(), occurrences: new Set() };
    this.pickMode = null;
    this.stage = document.createElement('div');
    this.stage.className = 'sheet-stage';
    host.append(this.stage);
    this.readout = options.readout || null;
    this.pendingFit = false;
    this.listeners = new AbortController(); // every host listener is removed by destroy()
    if (options.initialView) this.setViewState(options.initialView);
    this._bind();
    this.resizeObserver = new ResizeObserver(() => {
      if (this.pendingFit) this.fit(); // first shown after being installed while hidden
      else this._applyView();
    });
    this.resizeObserver.observe(host);
  }

  /** Restore a paper view {cx, cy, scale} (paper units), e.g. when a pane returns to this sheet. */
  setViewState(view) {
    if (!view || ![view.cx, view.cy, view.scale].every(Number.isFinite) || !(view.scale > 0)) return;
    this.view = { cx: view.cx, cy: view.cy, scale: view.scale };
    this.pendingFit = false;
    this._applyView();
  }

  /** Remove the sheet, its observers and listeners. */
  destroy() {
    this.resizeObserver.disconnect();
    this.listeners.abort();
    this.cancelPick();
    this.stage.remove();
    this.svg = null;
  }

  /**
   * Replace the sheet with new SVG text; zoom/pan and selection are preserved.
   * verify(root) may throw to refuse the file before anything on screen changes.
   */
  setSvg(text, meta = {}, verify = null) {
    const parsed = sanitizeSvg(text);
    if (verify) verify(parsed);
    const imported = document.importNode(parsed, true);
    const vb = (imported.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    if (vb.length === 4 && vb.every(Number.isFinite) && vb[2] > 0 && vb[3] > 0) this.paper = { x: vb[0], y: vb[1], w: vb[2], h: vb[3] };
    else this.paper = { x: 0, y: 0, w: Number(imported.getAttribute('width')) || 1200, h: Number(imported.getAttribute('height')) || 840 };
    imported.removeAttribute('width');
    imported.removeAttribute('height');
    imported.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    imported.setAttribute('class', 'sheet-svg');
    imported.setAttribute('role', 'img');
    this.meta = meta;
    if (this.svg) this.svg.replaceWith(imported);
    else this.stage.append(imported);
    this.svg = imported;
    if (!this.view) this.fit();
    else this._applyView();
    this._applySelection();
    return imported;
  }

  clear() {
    if (this.svg) this.svg.remove();
    this.svg = null;
  }

  // ---------------------------------------------------------------- view

  fit() {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    if (w < 2 || h < 2) { // hidden pane: fit once it has a size
      this.pendingFit = true;
      return;
    }
    this.pendingFit = false;
    const scale = Math.min(w / this.paper.w, h / this.paper.h) * 0.97;
    this.view = { cx: (this.paper.x || 0) + this.paper.w / 2, cy: (this.paper.y || 0) + this.paper.h / 2, scale };
    this._applyView();
  }

  actualSize() {
    if (!this.view) this.fit();
    if (!this.view) return;
    this.view.scale = 1;
    this._applyView();
  }

  zoomBy(factor, clientX, clientY) {
    if (!this.view) this.fit();
    if (!this.view) return;
    const rect = this.host.getBoundingClientRect();
    const px = clientX == null ? rect.width / 2 : clientX - rect.left;
    const py = clientY == null ? rect.height / 2 : clientY - rect.top;
    const before = this.toPaper(px, py);
    const minScale = Math.min(rect.width / this.paper.w, rect.height / this.paper.h) * 0.4;
    this.view.scale = Math.min(12, Math.max(minScale, this.view.scale * factor));
    const after = this.toPaper(px, py);
    this.view.cx += before.x - after.x;
    this.view.cy += before.y - after.y;
    this._applyView();
  }

  /** Centre the given paper point (e.g. a selected symbol) without changing zoom. */
  centerOn(x, y) {
    if (!this.view) this.fit();
    if (!this.view) return;
    this.view.cx = x;
    this.view.cy = y;
    this._applyView();
  }

  toPaper(px, py) {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    return { x: this.view.cx + (px - w / 2) / this.view.scale, y: this.view.cy + (py - h / 2) / this.view.scale };
  }

  _applyView() {
    if (!this.svg || !this.view) return;
    const w = Math.max(1, this.host.clientWidth);
    const h = Math.max(1, this.host.clientHeight);
    const vw = w / this.view.scale;
    const vh = h / this.view.scale;
    this.svg.setAttribute('viewBox', `${(this.view.cx - vw / 2).toFixed(3)} ${(this.view.cy - vh / 2).toFixed(3)} ${vw.toFixed(3)} ${vh.toFixed(3)}`);
    if (this.options.onView) this.options.onView(this.view);
  }

  viewState() {
    return this.view ? { ...this.view } : null;
  }

  // ---------------------------------------------------------------- selection

  setSelection(semanticIds, occurrenceIds) {
    this.selected = { semantic: new Set(semanticIds), occurrences: new Set(occurrenceIds) };
    this._applySelection();
  }

  _applySelection() {
    if (!this.svg) return;
    for (const el of this.svg.querySelectorAll('.is-sel')) el.classList.remove('is-sel');
    if (!this.selected.semantic.size && !this.selected.occurrences.size) return;
    for (const el of this.svg.querySelectorAll('[data-semantic-id], [data-occurrence-id]')) {
      if (this.selected.occurrences.has(el.getAttribute('data-occurrence-id')) || this.selected.semantic.has(el.getAttribute('data-semantic-id'))) {
        el.classList.add('is-sel');
      }
    }
  }

  /** Paper-space centre of the first selected occurrence, from the generator's data-paper-x/y. */
  selectionAnchor() {
    if (!this.svg) return null;
    const el = this.svg.querySelector('.is-sel[data-paper-x][data-paper-y]');
    if (!el) return null;
    const x = Number(el.getAttribute('data-paper-x'));
    const y = Number(el.getAttribute('data-paper-y'));
    return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
  }

  /** Stored-coordinate port anchors (paper - 100) from port-anchor occurrences: {port_id: [x, y]}. */
  portAnchors() {
    const out = {};
    if (!this.svg) return out;
    for (const el of this.svg.querySelectorAll('[data-occurrence-kind="port-anchor"][data-semantic-id]')) {
      const x = Number(el.getAttribute('data-paper-x'));
      const y = Number(el.getAttribute('data-paper-y'));
      if (Number.isFinite(x) && Number.isFinite(y)) out[el.getAttribute('data-semantic-id')] = [x - 100, y - 100];
    }
    return out;
  }

  /** Enter a one-shot pick mode: the next click reports paper coordinates instead of selecting. */
  pickPoint(callback) {
    this.pickMode = callback;
    this.host.classList.add('picking');
  }

  cancelPick() {
    this.pickMode = null;
    this.host.classList.remove('picking');
  }

  // ---------------------------------------------------------------- input

  /** Nearest semantic SVG element at or above a DOM node inside the current sheet. */
  _semanticAt(node) {
    if (!this.svg || !(node instanceof Element)) return null;
    // a port anchor sits inside its symbol: the innermost semantic element (the port) wins
    const el = node.closest('[data-semantic-id]');
    return el && this.svg.contains(el) ? el : null;
  }

  /** Hit test at client coordinates (pointer capture retargets pointerup, so its target is not used). */
  _semanticAtPoint(x, y) {
    const root = this.host.getRootNode && this.host.getRootNode();
    const hit = root && typeof root.elementFromPoint === 'function' ? root.elementFromPoint(x, y) : null;
    return hit && this.host.contains(hit) ? this._semanticAt(hit) : null;
  }

  _bind() {
    const host = this.host;
    const { signal } = this.listeners;
    let drag = null;
    host.addEventListener('wheel', (e) => {
      if (!this.svg) return;
      e.preventDefault();
      const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0018));
      this.zoomBy(factor, e.clientX, e.clientY);
    }, { passive: false, signal });
    host.addEventListener('pointerdown', (e) => {
      if (!this.svg || !this.view || (e.button !== 0 && e.button !== 1)) return;
      // the press target is recorded now; capture starts only once the pointer actually drags
      drag = { x: e.clientX, y: e.clientY, cx: this.view.cx, cy: this.view.cy, moved: false, id: e.pointerId,
        captured: false, downTarget: this._semanticAt(e.target) };
    }, { signal });
    host.addEventListener('pointermove', (e) => {
      if (this.svg && this.view && this.readout) {
        const r = host.getBoundingClientRect();
        const p = this.toPaper(e.clientX - r.left, e.clientY - r.top);
        this.readout(p);
      }
      if (drag) {
        if (!drag.moved && movedBeyond(drag, e.clientX, e.clientY)) {
          drag.moved = true;
          try { host.setPointerCapture(drag.id); drag.captured = true; } catch { /* pointer already gone */ }
        }
        if (drag.moved) {
          host.classList.add('panning');
          this.view.cx = drag.cx - (e.clientX - drag.x) / this.view.scale;
          this.view.cy = drag.cy - (e.clientY - drag.y) / this.view.scale;
          this._applyView();
        }
        return;
      }
      const el = this._semanticAt(e.target);
      if (el !== this.hoverEl) {
        if (this.hoverEl) this.hoverEl.classList.remove('is-hover');
        this.hoverEl = el;
        if (el) el.classList.add('is-hover');
        if (this.options.onHover) this.options.onHover(el, e);
      }
    }, { signal });
    const end = (e) => {
      if (!drag) return;
      const gesture = drag;
      drag = null;
      host.classList.remove('panning');
      if (gesture.captured) {
        try { host.releasePointerCapture(gesture.id); } catch { /* already released */ }
      }
      const decision = classifyRelease({
        moved: gesture.moved, cancelled: e.type === 'pointercancel', pickMode: !!this.pickMode,
        downTarget: gesture.downTarget, upTarget: gesture.moved ? null : this._semanticAtPoint(e.clientX, e.clientY),
      });
      if (decision.action === 'pick-point') {
        const r = host.getBoundingClientRect();
        const p = this.toPaper(e.clientX - r.left, e.clientY - r.top);
        const cb = this.pickMode;
        this.cancelPick();
        cb(p);
        return;
      }
      if (!this.options.onPick || (decision.action !== 'select' && decision.action !== 'clear')) return;
      const el = decision.target;
      this.options.onPick(el ? {
        semanticId: el.getAttribute('data-semantic-id'),
        occurrenceId: el.getAttribute('data-occurrence-id'),
        additive: e.shiftKey || e.metaKey,
      } : null);
    };
    host.addEventListener('pointerup', end, { signal });
    host.addEventListener('pointercancel', end, { signal });
    host.addEventListener('pointerleave', () => {
      if (this.hoverEl) this.hoverEl.classList.remove('is-hover');
      this.hoverEl = null;
      if (this.options.onHover) this.options.onHover(null);
    }, { signal });
    host.addEventListener('dblclick', (e) => this.zoomBy(1.6, e.clientX, e.clientY), { signal });
  }
}

export { SVG_NS };
