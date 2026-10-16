// Activity: the real ordered events of the newest build (service + generator),
// per-artifact status from the service, and elapsed time from event data.
// No percentages are computed and nothing is animated beyond what arrived.

import { $, h, replace } from './dom.js';
import { ARTIFACTS, ACTIVE_STATUSES, artifactProgress, buildElapsed, formatSeconds, shortBuild } from './status.js';

const ARTIFACT_LABEL = { display: '3D', cad: 'CAD', pid: 'P&ID', legend: 'Legend', description: 'Description' };

export function createActivity(app) {
  const log = $('#act-log');
  const arts = $('#act-artifacts');
  const meta = $('#act-meta');
  let buildId = null;
  let lastSeq = 0;
  let diagShown = null;

  function renderArtifacts(build, state) {
    const progress = artifactProgress(build);
    replace(arts, ARTIFACTS.map((a) => {
      const p = progress[a];
      let status = p.status;
      let count = '';
      if (!build) {
        status = state.published ? 'complete' : null;
        count = state.published ? `published r${state.published.source_revision}` : '—';
      } else {
        if (p.total != null && p.completed != null) count = `${p.completed}/${p.total} units`;
        if (p.previews) count += `${count ? ' · ' : ''}${p.previews} preview${p.previews === 1 ? '' : 's'}`;
        if (!count) count = p.message ? p.message : '—';
      }
      return h('div', { class: `act-art s-${status || 'none'}`, title: p.message || '' },
        h('h5', null, h('i'), ARTIFACT_LABEL[a]),
        h('div', { class: 'st' }, status ? status.replace('_', ' ') : '—'),
        h('div', { class: 'ct' }, count));
    }));
  }

  function eventItem(e) {
    const counted = e.completed != null && e.total != null ? `[${e.completed}/${e.total}] ` : '';
    const bad = e.kind === 'status' && /^(Failed|Cancelled|Superseded)/.test(e.message || '');
    return h('li', { class: `k-${e.kind}${bad ? ' bad' : ''}`, title: `#${e.sequence} · ${e.source}${e.path ? ` · ${e.path}` : ''}` },
      h('span', { class: 't' }, `+${(e.elapsed_s ?? 0).toFixed(2)}s`),
      h('span', { class: 'a' }, e.artifact ? ARTIFACT_LABEL[e.artifact] || e.artifact : 'service'),
      h('span', { class: 'm' }, counted + e.message));
  }

  function update(state) {
    const b = state.build;
    if (!b) {
      if (buildId !== '(none)') {
        buildId = '(none)';
        lastSeq = 0;
        const pub = state.published;
        replace(log, h('li', { class: 'sep' }, 'No build in this service session'),
          h('li', null, h('span', { class: 't' }, ''), h('span', { class: 'a' }, 'service'),
            h('span', { class: 'm' }, pub ? `The verified published set is build ${pub.build_id} (revision ${pub.source_revision}). Changes start a new build automatically.`
              : 'Nothing has been published yet. Use Rebuild to generate the five outputs.')));
      }
      meta.textContent = state.published ? `published r${state.published.source_revision}` : 'idle';
      renderArtifacts(null, state);
      return;
    }
    if (b.id !== buildId) {
      buildId = b.id;
      lastSeq = 0;
      diagShown = null;
      replace(log, h('li', { class: 'sep' }, `Build ${shortBuild(b.id)} · revision ${b.source_revision} · ${b.reason || ''}`));
    }
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const fresh = (b.events || []).filter((e) => e.sequence > lastSeq);
    if (fresh.length) {
      for (const li of log.querySelectorAll('li.latest')) li.classList.remove('latest');
      for (const e of fresh) log.append(eventItem(e));
      const last = log.lastElementChild;
      if (last && ACTIVE_STATUSES.has(b.status)) last.classList.add('latest');
      lastSeq = fresh[fresh.length - 1].sequence;
      if (nearBottom) log.scrollTop = log.scrollHeight;
    }
    if (b.events_dropped && !log.querySelector('.dropped')) {
      log.append(h('li', { class: 'sep dropped' }, `${b.events_dropped} event(s) beyond the service's retention limit are not listed`));
    }
    if (b.integrity === 'rejected' && !log.querySelector('.rejected')) {
      log.append(h('li', { class: 'k-status bad rejected' }, h('span', { class: 't' }, ''), h('span', { class: 'a' }, 'integrity'),
        h('span', { class: 'm' }, 'Rejected: this build\'s outputs no longer verify and are not offered (see the published_* diagnostics).')));
      if (nearBottom) log.scrollTop = log.scrollHeight;
    }
    const diagKey = `${b.id}|${b.status}|${(b.diagnostics || []).length}`;
    if (!ACTIVE_STATUSES.has(b.status) && diagKey !== diagShown) {
      diagShown = diagKey;
      const problems = (b.diagnostics || []).filter((d) => d.severity === 'error' || b.status === 'pending');
      for (const d of problems.slice(0, 8)) {
        log.append(h('li', { class: `k-status ${d.severity === 'error' ? 'bad' : ''}` }, h('span', { class: 't' }, ''),
          h('span', { class: 'a' }, d.severity), h('span', { class: 'm' }, `[${d.code}] ${d.message}`)));
      }
      if (nearBottom) log.scrollTop = log.scrollHeight;
    }
    renderArtifacts(b, state);
    tick(state);
  }

  function tick(state) {
    const b = state.build;
    if (!b) return;
    meta.textContent = `${shortBuild(b.id)} · r${b.source_revision} · ${b.status}${b.integrity === 'rejected' ? ' · integrity rejected' : ''} · ` +
      `${formatSeconds(buildElapsed(b))}${b.last_sequence ? ` · ${b.last_sequence} events` : ''}`;
  }

  return { update, tick };
}
