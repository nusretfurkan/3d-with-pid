// Physical-route repair decisions for the Reroute buttons (pure; unit-tested in Node).
// The service computes the route (POST /api/reroute) from the authoritative project; the
// UI only decides whether to offer the action, what to send and how to report the result.
// Nothing here invents route points or claims publication before the five outputs exist.

import { syncBadge } from './status.js';

function endpointEntities(project, connection) {
  const ports = project.model.ports;
  return [connection.from_port_id, connection.to_port_id].map((pid) => (ports[pid] ? ports[pid].entity_id : null));
}

/**
 * Offer for one connection's physical route:
 * {status, reason, pending, actionable, why}. pending = the route is not current (the badge
 * becomes a Reroute button); actionable = both endpoint items are placed, so the service can
 * try; why explains a disabled button.
 */
export function rerouteOffer(project, connectionId) {
  const connection = project.model.connections[connectionId];
  if (!connection) return { status: null, reason: null, pending: false, actionable: false, why: 'Not in the current revision.' };
  const route = project.physical.routes[connectionId] || null;
  const status = route ? route.status : 'none';
  const reason = route && route.reason ? route.reason : null;
  if (status === 'current') return { status, reason, pending: false, actionable: false, why: null };
  const unplaced = endpointEntities(project, connection).filter((eid) => {
    const placement = eid ? project.physical.placements[eid] : null;
    return !placement || placement.status !== 'placed';
  });
  if (unplaced.length) {
    const tags = unplaced.map((eid) => (eid && project.model.entities[eid] ? project.model.entities[eid].tag : eid || '?'));
    return {
      status, reason, pending: true, actionable: false,
      why: `${tags.join(' and ')} ${tags.length === 1 ? 'has' : 'have'} no physical placement; place ${tags.length === 1 ? 'it' : 'them'} first.`,
    };
  }
  return { status, reason, pending: true, actionable: true, why: null };
}

/** Request body for POST /api/reroute (exactly the two contracted fields). */
export function rerouteRequest(connectionId, expectedRevision) {
  return { connection_id: connectionId, expected_revision: expectedRevision };
}

/**
 * Outcome of a successful reply: {routed, toRevision, mode, status}. routed is true only when
 * the reply's project has a current route for the connection at a newer revision.
 */
export function rerouteOutcome(fromRevision, state, connectionId) {
  const project = state && state.project;
  const route = project ? project.physical.routes[connectionId] : null;
  const advanced = !!project && project.revision > fromRevision;
  return {
    routed: advanced && !!route && route.status === 'current',
    toRevision: project ? project.revision : null,
    mode: route ? route.mode || null : null,
    status: route ? route.status : null,
  };
}

/**
 * Honest build feedback after a reroute committed revision toRevision: the outputs are
 * current only when the service reports a synchronized set at (or after) that revision.
 * {tone, text, settled}.
 */
export function rerouteBuildNote(state, toRevision) {
  if (!state || toRevision == null) return null;
  const sync = state.sync || {};
  const published = state.published;
  if (state.project.revision > toRevision) {
    return { tone: 'none', text: `Superseded by revision ${state.project.revision}.`, settled: true };
  }
  if (sync.state === 'synchronized' && published && published.source_revision >= toRevision) {
    return { tone: 'current', text: `All five outputs published for r${toRevision}.`, settled: true };
  }
  const b = syncBadge(state);
  if (b.tone === 'current') { // "synchronized" without a publication of this revision is not current
    return { tone: 'unverified', text: `r${toRevision} saved · no verified publication of r${toRevision} yet`, settled: false };
  }
  const settled = ['failed', 'cancelled', 'invalid', 'blocked'].includes(sync.state);
  const text = sync.state === 'building' || sync.state === 'pending'
    ? `r${toRevision} saved · outputs ${sync.state === 'building' ? 'regenerating' : 'pending'}, not yet current`
    : `r${toRevision} saved · ${b.label}`;
  return { tone: b.tone, text, settled };
}
