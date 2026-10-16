// Pointer gesture decisions for the sheet view (pure; unit-tested in Node).
//
// The press target is recorded at pointerdown, and the release target comes from a
// hit test at the release point (pointer capture would otherwise retarget pointerup
// to the capturing element). A drag never selects.

export const DRAG_THRESHOLD_PX = 3;

export function movedBeyond(down, x, y, threshold = DRAG_THRESHOLD_PX) {
  return Math.hypot(x - down.x, y - down.y) > threshold;
}

/**
 * {moved, cancelled, pickMode, downTarget, upTarget} -> one of
 * {action:'none'} | {action:'pan-end'} | {action:'pick-point'} | {action:'select', target} | {action:'clear'}.
 * Targets are opaque (elements in the browser); the release hit wins, the press target
 * is used when the release hit test finds nothing semantic.
 */
export function classifyRelease({ moved, cancelled, pickMode, downTarget, upTarget }) {
  if (cancelled) return { action: 'none' };
  if (moved) return { action: 'pan-end' };
  if (pickMode) return { action: 'pick-point' };
  const target = upTarget || downTarget || null;
  return target ? { action: 'select', target } : { action: 'clear' };
}
