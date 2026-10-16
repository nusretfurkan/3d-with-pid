// Companion-document content for a view pane: tagged-item index, process description
// and the output set. Each pane that shows one of these owns its controller, loader and
// scroll position, and follows its own Live/Published choice. Index and description show
// only content installed by their loader (after an identity check), with that content's
// own build and revision; the outputs list shows the files the service offers now, each
// with its verified/unverified status. Fetches go through the shared immutable caches.

import { h, icon, replace } from './dom.js';
import {
  ARTIFACTS, FILE_KEYS, ARTIFACT_LABEL, ARTIFACT_OF_KEY, shortBuild, shortDigest, sourceBadge, viewSource,
} from './status.js';
import { checkIdentity, slotBadge } from './view-source.js';

function placeholder(slot, state, what) {
  const b = slotBadge(slot, state);
  if (b.tone === 'loading') return `Loading the ${what}…`;
  if (b.tone === 'failed') return `The ${what} could not be loaded: ${b.detail}`;
  return `No ${what} has been generated yet.`;
}

/** Scroll position survives re-renders and a later return of the pane to this type. */
function keepScroll(host, initial) {
  let restored = initial == null;
  return {
    after() {
      if (!restored && host.scrollHeight > host.clientHeight) {
        host.scrollTop = initial;
        restored = true;
      }
    },
  };
}

/** ctx = {app, pane, host, makeLoader(label, fetch, install), nav} */
export function createIndexContent({ app, pane, host, makeLoader, nav }) {
  let installed = null; // {data, src, url}
  let inventory = null; // {data, src} of the same build as the installed index
  let inventoryUrl = null;
  let selected = new Set(app.selectionIds);
  const scroll = keepScroll(host, nav ? nav.scrollTop : null);

  const loader = makeLoader('Item index', (url) => app.cachedJSON(url), (data, target) => {
    checkIdentity('Item index', { revision: data.source_revision, digest: data.input_digest }, target.src);
    installed = { data, src: target.src, url: target.url };
    render();
    if (app.state) loadInventory(app.state);
  });

  function render() {
    if (!installed) {
      replace(host, h('div', { class: 'doc-intro' }, placeholder(loader.state(), app.state, 'item index')));
      return;
    }
    const top = host.scrollTop;
    const { data: index, src } = installed;
    const items = Array.isArray(index.items) ? index.items : [];
    const indexIds = new Set(items.map((i) => i.semantic_id));
    let crossCheck = null;
    if (inventory && inventory.src.build_id === src.build_id && Array.isArray(inventory.data.tagged_ids)) {
      const inv = new Set(inventory.data.tagged_ids);
      const missing = [...inv].filter((id) => !indexIds.has(id));
      const surplus = [...indexIds].filter((id) => !inv.has(id));
      crossCheck = missing.length || surplus.length
        ? h('span', { class: 'chip warn' }, `index ≠ drawn tagged IDs: missing ${missing.join(', ') || '—'}, surplus ${surplus.join(', ') || '—'}`)
        : h('span', { class: 'chip', title: 'UI cross-check of item-index.json against drawing.inventory.json of the same build' }, h('b', null, 'check'), `index = drawn tagged IDs (${inv.size})`);
    }
    // The tag is a toggle button inside the row, so the row's selection is reachable from the keyboard
    // and announced; its click bubbles to the row, which owns the selection behaviour.
    const rows = items.map((item) => h('tr', {
      class: `clickable ${selected.has(item.semantic_id) ? 'sel' : ''}`, dataset: { id: item.semantic_id },
      onclick: (e) => app.select([item.semantic_id], 'inspector', e.shiftKey || e.metaKey),
    },
    h('td', { class: 'tag' }, h('button', { type: 'button', class: 'tag-btn', 'aria-pressed': String(selected.has(item.semantic_id)), title: `Select ${item.tag} in every view (Shift adds)` }, item.tag)),
    h('td', null, h('div', { class: 'item-name' }, item.description || item.kind), h('div', { class: 'tid' }, item.semantic_id)),
    h('td', null, h('div', { class: 'refs' }, (item.occurrences || []).map((o) => h('span', { class: 'ref', title: `${o.occurrence_id} · ${o.occurrence_kind} · paper (${(o.position || []).join(', ')})` }, o.ref))))));
    replace(host,
      h('div', { class: 'doc-intro' },
        h('span', null, h('b', null, `${items.length} tagged items`), ` on ${index.sheet_id} · legend ${index.legend_sheet_id}`),
        inventory && inventory.src.build_id === src.build_id && Array.isArray(inventory.data.keys) ? h('span', null, `${inventory.data.keys.length} legend keys drawn`) : null,
        crossCheck,
        h('span', { class: 'mono' }, `r${index.source_revision} · ${shortBuild(src.build_id)}`)),
      h('table', { class: 'table' },
        h('thead', null, h('tr', null, h('th', null, 'Tag'), h('th', null, 'Item'), h('th', null, 'Sheet / zone'))),
        h('tbody', null, rows)),
      index.paper && index.paper.boundary_policy ? h('p', { class: 'doc-note' }, index.paper.boundary_policy) : null);
    host.scrollTop = top;
    scroll.after();
  }

  async function loadInventory(state) {
    const src = viewSource(state, 'inventory', pane.mode);
    if (!installed || !src || src.build_id !== installed.src.build_id || src.url === inventoryUrl) return;
    inventoryUrl = src.url;
    try {
      const data = await app.cachedJSON(src.url);
      if (inventoryUrl !== src.url) return;
      if (data.source_revision !== src.source_revision || data.input_digest !== src.input_digest) return;
      inventory = { data, src };
      render();
    } catch { /* the cross-check is simply not shown */ }
  }

  render();
  return {
    type: 'index',
    loader,
    update(state) {
      const src = viewSource(state, 'index', pane.mode);
      loader.want(src ? src.url : null, src);
      if (!installed) render();
      else loadInventory(state);
    },
    setSelection(ids) {
      selected = new Set(ids);
      for (const tr of host.querySelectorAll('tr[data-id]')) {
        const on = selected.has(tr.dataset.id);
        tr.classList.toggle('sel', on);
        const tag = tr.querySelector('.tag-btn');
        if (tag) tag.setAttribute('aria-pressed', String(on));
      }
    },
    navState: () => ({ scrollTop: host.scrollTop }),
    destroy() { replace(host); },
  };
}

/** Description HTML after an identity check, rendered inert in a sandboxed srcdoc frame. */
export function createDescriptionContent({ app, pane, host, makeLoader }) {
  let shown = null;
  const loader = makeLoader('Description', (url) => app.cachedText(url), (text, target) => {
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const body = doc.body;
    const revision = Number(body && body.getAttribute('data-source-revision'));
    const digest = body && body.getAttribute('data-input-digest');
    if (revision !== target.src.source_revision || digest !== target.src.input_digest) {
      throw new Error(`description identity (revision ${revision}, digest ${shortDigest(digest)}) does not match its offered source`);
    }
    for (const el of doc.querySelectorAll('script, iframe, object, embed, link[rel="import"], base')) el.remove();
    // A srcdoc frame resolves URLs against the parent page, so a "#item-…" link would load the app
    // into the frame. The page CSP (base-uri 'none') also applies to the frame and blocks a <base>,
    // so only the embedded copy's fragment links point at the frame's own document.
    for (const a of doc.querySelectorAll('a[href^="#"], area[href^="#"]')) a.setAttribute('href', `about:srcdoc${a.getAttribute('href')}`);
    const frame = h('iframe', {
      class: 'doc-frame', title: `Process description, revision ${revision}`, sandbox: '', referrerpolicy: 'no-referrer',
    });
    frame.srcdoc = `<!doctype html>${doc.documentElement.outerHTML}`;
    replace(host, frame);
    shown = target.url;
  });

  function placeholderView() {
    if (shown) return;
    replace(host, h('div', { class: 'view-empty' }, placeholder(loader.state(), app.state, 'process description')));
  }

  placeholderView();
  return {
    type: 'description',
    loader,
    update(state) {
      const src = viewSource(state, 'description', pane.mode);
      loader.want(src ? src.url : null, src);
      placeholderView();
    },
    setSelection() {},
    navState: () => null,
    destroy() { replace(host); },
  };
}

/** All published-file keys with offered source, build/digest, download and the CAD reimport summary. */
export function createOutputsContent({ app, pane, host, nav }) {
  let verification = null;
  let verificationUrl = null;
  let renderedKey = null;
  const scroll = keepScroll(host, nav ? nav.scrollTop : null);

  async function render(state) {
    const vSrc = viewSource(state, 'verification', pane.mode);
    if (vSrc && vSrc.kind === 'output' && vSrc.url !== verificationUrl) {
      verificationUrl = vSrc.url;
      try {
        const data = await app.cachedJSON(vSrc.url);
        const id = data && data.identity;
        if (verificationUrl === vSrc.url && id && id.source_revision === vSrc.source_revision && id.input_digest === vSrc.input_digest) {
          verification = { data, src: vSrc };
          renderedKey = null;
        }
      } catch { verification = null; }
      state = app.state || state;
    }
    const key = `${state.project.revision}|${state.sync ? state.sync.state : ''}|${state.published ? state.published.build_id : ''}|` +
      `${state.build ? `${state.build.id}:${state.build.status}:${state.build.integrity}` : ''}|${pane.mode}|${verification ? verification.src.url : ''}`;
    if (key === renderedKey) return;
    renderedKey = key;
    const top = host.scrollTop;
    const row = (k) => {
      const src = viewSource(state, k, pane.mode);
      const b = sourceBadge(src, state.project.revision, state.published);
      const published = src && src.origin === 'published';
      return h('tr', null,
        h('td', { class: 'file' }, h('b', null, ARTIFACT_LABEL[k]), h('div', { class: 'tid' }, `${ARTIFACT_OF_KEY[k]} artifact`)),
        h('td', null, h('span', { class: `badge tone-${b.tone}`, title: b.detail }, h('i', { class: 'dot' }), b.label)),
        h('td', { class: 'rev' }, src ? `r${src.source_revision}` : '—'),
        h('td', { class: 'bd' }, src ? [h('div', null, shortBuild(src.build_id)), h('div', null, `digest ${shortDigest(src.input_digest)}`)] : '—'),
        h('td', { class: 'dl' }, src ? h('a', {
          class: `dl-link${published ? '' : ' preview'}`, href: src.url, download: '', title: src.url,
          'aria-label': `${published ? 'Download' : `Download (${src.kind})`} ${ARTIFACT_LABEL[k]}`,
        }, icon('download'), h('span', null, published ? 'Download' : `Download (${src.kind})`)) : '—'));
    };
    const group = (title, note, keys) => h('tbody', null,
      h('tr', { class: 'group' }, h('th', { colspan: '5', scope: 'colgroup' }, title, h('span', null, note))),
      keys.map(row));
    const v = verification && verification.data;
    const summary = v && v.summary ? v.summary : null;
    replace(host,
      h('div', { class: 'doc-intro' }, h('span', null, h('b', null, 'Five required outputs'),
        ': 3D view, CAD (STEP), P&ID, legend and description. The set is current only when the service has verified all five for the same revision and digest.'),
      h('span', null, state.sync ? state.sync.message : '')),
      h('table', { class: 'table outputs' },
        h('thead', null, h('tr', null, h('th', null, 'File'), h('th', { title: 'The source this pane is offered for the file, with its verification status' }, 'Status'),
          h('th', null, 'Revision'), h('th', null, 'Build · digest'), h('th', { class: 'dl' }, 'Download'))),
        group('Required outputs', 'the five files of one synchronized set', FILE_KEYS.filter((k) => ARTIFACTS.includes(k))),
        group('Supporting files', 'derived by the same generators', FILE_KEYS.filter((k) => !ARTIFACTS.includes(k)))),
      summary ? h('div', { class: `out-check${v.ok ? '' : ' bad'}` },
        h('span', null, h('b', null, `CAD reimport check ${v.ok ? 'passed' : 'FAILED'}`), ` · revision ${v.identity.source_revision} · ${shortBuild(verification.src.build_id)}`),
        h('span', { class: 'mono' }, `${summary.parts} parts · ${summary.solids} solids · ${summary.ports_on_flange_faces}/${summary.ports_checked} ports on flange faces · envelopes ${summary.envelope_checks_passed}/${summary.envelope_checks} · max placement Δ ${summary.max_placement_delta_mm} mm · unit ${summary.length_unit}`),
        (v.problems || []).length ? h('span', null, h('span', { class: 'chip warn' }, `${v.problems.length} problem(s)`)) : null) : null,
      h('p', { class: 'doc-note' }, 'The STEP file and the geometry recipe are illustrative prototype geometry with explicit assumptions; they are not a fabrication or vendor model.'));
    host.scrollTop = top;
    scroll.after();
  }

  return {
    type: 'outputs',
    loader: null,
    update(state) { render(state); },
    setSelection() {},
    navState: () => ({ scrollTop: host.scrollTop }),
    destroy() { replace(host); },
  };
}
