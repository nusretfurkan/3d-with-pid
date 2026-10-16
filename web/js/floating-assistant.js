// Floating Ask launcher and the placement of the assistant bubble next to it. Presentation
// only: the launcher's position is a UI preference kept in localStorage (never in the project),
// and the bubble floats over the panes, so opening it changes no pane size or view.
//
// Positions are stored normalized: 0..1 across the room the launcher can occupy inside the
// viewport margins, so a position survives window resizes and is clamped back on screen.

export const LAUNCHER_STORAGE_KEY = 'skid.askLauncher.position.v1';
export const DRAG_THRESHOLD_PX = 5;
export const NUDGE_PX = 24;
export const LAUNCHER_MARGIN = 16;
export const DEFAULT_POSITION = Object.freeze({ x: 1, y: 1 }); // bottom-right

const BUBBLE = Object.freeze({ maxWidth: 680, margin: 12, marginTop: 12, gap: 10, minHeight: 240 });

const clamp01 = (v) => Math.min(1, Math.max(0, v));

/** The launcher's free range inside the viewport: [min, max] for left and top. */
function range(size, viewport, margin) {
  return {
    minX: margin, maxX: Math.max(margin, viewport.width - size.width - margin),
    minY: margin, maxY: Math.max(margin, viewport.height - size.height - margin),
  };
}

/** Pixel position (left/top) of a normalized position, always inside the viewport margins. */
export function fromNormalized(norm, size, viewport, margin = LAUNCHER_MARGIN) {
  const r = range(size, viewport, margin);
  const n = validNormalized(norm) ? norm : DEFAULT_POSITION;
  return { left: Math.round(r.minX + clamp01(n.x) * (r.maxX - r.minX)), top: Math.round(r.minY + clamp01(n.y) * (r.maxY - r.minY)) };
}

/** Normalized position of a pixel position; positions beyond the margins clamp to 0 or 1. */
export function toNormalized(pos, size, viewport, margin = LAUNCHER_MARGIN) {
  const r = range(size, viewport, margin);
  const along = (v, lo, hi) => (hi > lo ? clamp01((v - lo) / (hi - lo)) : 1);
  return { x: along(pos.left, r.minX, r.maxX), y: along(pos.top, r.minY, r.maxY) };
}

export function validNormalized(n) {
  return !!n && typeof n === 'object' && [n.x, n.y].every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1);
}

/** The stored position, or null when storage is unavailable, empty or holds anything else. */
export function readStoredPosition(storage) {
  try {
    const raw = storage ? storage.getItem(LAUNCHER_STORAGE_KEY) : null;
    if (!raw) return null;
    const n = JSON.parse(raw);
    return validNormalized(n) ? { x: n.x, y: n.y } : null;
  } catch {
    return null;
  }
}

/** Store a position; false when storage refuses it (private mode, quota): the position then lasts for the page. */
export function writeStoredPosition(storage, norm) {
  try {
    if (!storage || !validNormalized(norm)) return false;
    storage.setItem(LAUNCHER_STORAGE_KEY, JSON.stringify({ x: +norm.x.toFixed(4), y: +norm.y.toFixed(4) }));
    return true;
  } catch {
    return false;
  }
}

export function clearStoredPosition(storage) {
  try {
    if (storage) storage.removeItem(LAUNCHER_STORAGE_KEY);
  } catch { /* storage unavailable: nothing stored */ }
}

/** A pointer that moved at least the threshold is a drag; less is a click. */
export function isDrag(dx, dy, threshold = DRAG_THRESHOLD_PX) {
  return Math.hypot(dx, dy) >= threshold;
}

/** Shift+Arrow placement: the new pixel position for a key, or null for other keys. */
export function nudge(pos, key, step = NUDGE_PX) {
  const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[key];
  return d ? { left: pos.left + d[0], top: pos.top + d[1] } : null;
}

/**
 * Where the bubble goes for a launcher rectangle: above it when there is room (growing upward
 * from just above the launcher), else below it, else over it within the viewport. Horizontally
 * it lines up with the launcher's nearer edge and stays inside the margins.
 * `marginTop` keeps a band at the top free (the focus exit control).
 * Returns {left, width, top, bottom, maxHeight, side}; exactly one of top/bottom is a number.
 */
export function placeBubble(anchor, viewport, opts = {}) {
  const { maxWidth, margin, gap, minHeight } = { ...BUBBLE, ...opts };
  const marginTop = Math.max(margin, opts.marginTop ?? BUBBLE.marginTop);
  const width = Math.max(0, Math.min(maxWidth, viewport.width - 2 * margin));
  const centre = (anchor.left + anchor.right) / 2;
  let left = centre >= viewport.width / 2 ? anchor.right - width : anchor.left;
  left = Math.round(Math.min(Math.max(margin, left), Math.max(margin, viewport.width - width - margin)));
  const above = anchor.top - gap - marginTop;
  const below = viewport.height - anchor.bottom - gap - margin;
  const full = Math.max(0, viewport.height - marginTop - margin);
  if (above >= minHeight && above >= below) return { left, width, top: null, bottom: Math.round(viewport.height - anchor.top + gap), maxHeight: Math.floor(above), side: 'above' };
  if (below >= minHeight) return { left, width, top: Math.round(anchor.bottom + gap), bottom: null, maxHeight: Math.floor(below), side: 'below' };
  if (above >= minHeight) return { left, width, top: null, bottom: Math.round(viewport.height - anchor.top + gap), maxHeight: Math.floor(above), side: 'above' };
  return { left, width, top: marginTop, bottom: null, maxHeight: Math.floor(full), side: 'over' };
}

// ---------------------------------------------------------------- DOM

function viewportSize() {
  return { width: document.documentElement.clientWidth || window.innerWidth, height: document.documentElement.clientHeight || window.innerHeight };
}

function safeStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Make `button` a draggable fixed launcher. `onActivate` runs on a click that was not a drag
 * (pointer or keyboard); `onMove` runs whenever the launcher's position changes. A drag never
 * reaches the panes underneath: the pointer is captured by the launcher.
 */
export function createFloatingLauncher(button, { onActivate, onMove, storage = safeStorage() } = {}) {
  let norm = readStoredPosition(storage) || { ...DEFAULT_POSITION };
  let stored = validNormalized(readStoredPosition(storage));
  let pos = { left: 0, top: 0 };
  let drag = null; // {id, x0, y0, left0, top0, moved}
  let swallowClick = false;

  const size = () => ({ width: button.offsetWidth || 72, height: button.offsetHeight || 40 });

  function apply(next) {
    const vp = viewportSize();
    const r = range(size(), vp, LAUNCHER_MARGIN);
    pos = { left: Math.round(Math.min(Math.max(r.minX, next.left), r.maxX)), top: Math.round(Math.min(Math.max(r.minY, next.top), r.maxY)) };
    button.style.right = 'auto'; // the stylesheet's bottom-right corner applies only until this first layout
    button.style.bottom = 'auto';
    button.style.left = `${pos.left}px`;
    button.style.top = `${pos.top}px`;
    button.classList.toggle('low-right', pos.left + size().width > vp.width - 420 && pos.top + size().height > vp.height - 140);
    document.body.classList.toggle('ask-low-right', button.classList.contains('low-right'));
    if (onMove) onMove();
  }

  /** Re-derive the pixel position from the remembered normalized one (window resized, restored). */
  function layout() {
    apply(fromNormalized(norm, size(), viewportSize()));
  }

  function commit() {
    norm = toNormalized(pos, size(), viewportSize());
    stored = writeStoredPosition(storage, norm);
  }

  function endDrag(e, { cancelled = false } = {}) {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    const moved = drag.moved;
    drag = null;
    button.classList.remove('dragging');
    if (moved) {
      commit();
      swallowClick = true; // the click that follows a drag is not an activation
      setTimeout(() => { swallowClick = false; }, 0);
    } else if (cancelled) {
      swallowClick = true;
      setTimeout(() => { swallowClick = false; }, 0);
    }
  }

  button.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || drag) return;
    e.stopPropagation();
    drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, left0: pos.left, top0: pos.top, moved: false };
    try { button.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
  });
  button.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x0;
    const dy = e.clientY - drag.y0;
    if (!drag.moved && !isDrag(dx, dy)) return;
    if (!drag.moved) button.classList.add('dragging');
    drag.moved = true;
    e.preventDefault();
    apply({ left: drag.left0 + dx, top: drag.top0 + dy });
  });
  button.addEventListener('pointerup', (e) => endDrag(e));
  button.addEventListener('pointercancel', (e) => endDrag(e, { cancelled: true }));
  button.addEventListener('lostpointercapture', (e) => endDrag(e, { cancelled: true }));
  button.addEventListener('click', (e) => {
    if (swallowClick || drag) {
      e.preventDefault();
      e.stopImmediatePropagation();
      swallowClick = false;
      return;
    }
    if (onActivate) onActivate(e);
  });
  button.addEventListener('keydown', (e) => {
    if (!e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === 'Home') {
      e.preventDefault();
      e.stopPropagation();
      reset();
      return;
    }
    const next = nudge(pos, e.key);
    if (!next) return;
    e.preventDefault();
    e.stopPropagation();
    apply(next);
    commit();
  });
  window.addEventListener('resize', () => layout());

  function reset() {
    norm = { ...DEFAULT_POSITION };
    clearStoredPosition(storage);
    stored = false;
    layout();
  }

  layout();
  // the label's font may still be loading on the first layout; settle once more
  requestAnimationFrame(() => layout());

  return {
    rect: () => button.getBoundingClientRect(),
    layout,
    reset,
    snapshot: () => ({ left: pos.left, top: pos.top, normalized: { ...norm }, stored, dragging: !!(drag && drag.moved) }),
  };
}

/** Position the bubble element next to the launcher (fixed, inside the viewport). */
export function positionBubble(bubble, launcherRect) {
  const focus = document.body.classList.contains('focus-mode');
  const p = placeBubble(launcherRect, viewportSize(), { marginTop: focus ? 42 : 12 });
  bubble.style.left = `${p.left}px`;
  bubble.style.width = `${p.width}px`;
  bubble.style.top = p.top == null ? 'auto' : `${p.top}px`;
  bubble.style.bottom = p.bottom == null ? 'auto' : `${p.bottom}px`;
  bubble.style.maxHeight = `${p.maxHeight}px`;
  bubble.dataset.side = p.side;
  return p;
}
