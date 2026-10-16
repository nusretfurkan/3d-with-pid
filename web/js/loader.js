// Latest-wins artifact loader for one view (pure; fetch/install are injected so
// the ordering rules are unit-tested in Node).
//
// State: desired (newest URL the service offers for the view), installed (the URL
// actually on screen), pending (URL being fetched) and error (last failure of a URL).
// - Only one fetch runs at a time; when it completes, the result is installed only if
//   it is still wanted, or if it is a newer progressive preview of the same build as
//   the one on screen (an honest intermediate step). Everything else is discarded.
// - An older preview of the build on screen is never installed over a newer one.
// - A failed fetch or install leaves the installed content untouched and is recorded,
//   so the badge can say so; the same URL is retried only after retryAfterMs.
// - want(null) records that the service offers nothing now; installed content stays
//   on screen but the caller's badge labels it as no longer offered.

/** Order of a progressive snapshot (preview/display-0007.json -> 7); final outputs sort last. */
export function previewOrder(url) {
  const m = /\/preview\/[a-z]+-(\d+)\.(?:json|svg)$/.exec(url || '');
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER;
}

export function createLoader({ fetch, install, onChange = () => {}, now = () => Date.now(), retryAfterMs = 4000 }) {
  let desired = null;
  let installed = null;
  let pending = null;
  let error = null;
  let ignored = null; // a wanted URL refused for being older than the content on screen
  let busy = false;
  const stats = { installs: 0, discarded: 0, failures: 0 };

  const snapshot = () => ({ desired, installed, pending, error });
  const changed = () => onChange(snapshot());

  function blocked() {
    return !desired || (installed && desired.url === installed.url) || (error && error.url === desired.url)
      || desired.url === ignored;
  }

  function olderThanInstalled(target) {
    return !!(installed && installed.src && target.src && installed.src.build_id === target.src.build_id
      && previewOrder(target.url) < previewOrder(installed.url));
  }

  async function run() {
    if (busy) return;
    busy = true;
    try {
      while (!blocked()) {
        const target = desired;
        pending = { url: target.url };
        changed();
        let data;
        try {
          data = await fetch(target.url);
        } catch (exc) {
          stats.failures += 1;
          pending = null;
          if (desired && desired.url === target.url) error = { url: target.url, message: String(exc && exc.message ? exc.message : exc), at: now() };
          changed();
          continue;
        }
        pending = null;
        const current = desired;
        const stillWanted = current && current.url === target.url;
        const progressive = current && installed && target.src && current.src && installed.src
          && current.src.build_id === target.src.build_id && installed.src.build_id === target.src.build_id
          && previewOrder(target.url) > previewOrder(installed.url);
        if ((!stillWanted && !progressive) || olderThanInstalled(target)) {
          stats.discarded += 1;
          if (stillWanted) ignored = target.url; // do not refetch it until something else is wanted
          changed();
          continue;
        }
        try {
          install(data, target);
        } catch (exc) {
          stats.failures += 1;
          error = { url: target.url, message: String(exc && exc.message ? exc.message : exc), at: now() };
          changed();
          continue;
        }
        installed = { url: target.url, src: target.src };
        if (error && (error.url === target.url || !desired || error.url !== desired.url)) error = null;
        stats.installs += 1;
        changed();
      }
    } finally {
      busy = false;
    }
  }

  return {
    /** Request the newest offered file (url null when the service offers none). */
    want(url, src) {
      if (!url) {
        if (desired !== null) {
          desired = null;
          changed();
        }
        return;
      }
      const moved = !desired || desired.url !== url;
      desired = { url, src };
      if (ignored !== url) ignored = null;
      if (error && error.url !== url) error = null;
      else if (error && now() - error.at >= retryAfterMs) error = null;
      if (moved) changed();
      run();
    },
    retry() {
      error = null;
      run();
    },
    state: snapshot,
    stats,
    /** Resolves when the current fetch/install loop has drained (tests). */
    async idle() {
      while (busy) await new Promise((r) => setTimeout(r, 0));
    },
  };
}
