// Pure review model for equipment research on engineering proposals (no DOM, no network).
//
// The service verifies every research brief before planning; this module still treats the brief
// as untrusted text. Source links are offered only for public absolute http(s) URLs without
// credentials, explicit ports, local/special-use host names or non-public IP literals; anything else
// is shown as plain text with the link withheld. Text is never interpreted as markup (the DOM
// helper assigns text only), and nothing here fetches a URL.

export const STAGE_LABELS = { researching: 'Researching', planning: 'Planning' };
const MAX_URL = 2048;
const URL_CHARS = /^[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+$/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;
const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const TLD = /^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/;
const BLOCKED_HOSTS = new Set(['localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback', 'broadcasthost']);
const BLOCKED_SUFFIXES = ['.localhost', '.local', '.localdomain', '.internal', '.intranet', '.lan', '.home', '.corp',
  '.private', '.arpa', '.test', '.example', '.invalid', '.onion', '.alt'];

function publicIPv4(host) {
  const parts = host.split('.');
  if (parts.length !== 4 || !parts.every((p) => /^(0|[1-9][0-9]{0,2})$/.test(p) && Number(p) <= 255)) return false;
  const [a, b] = parts.map(Number);
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && b === 168) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 192 && b === 0) return false;
  if (a === 198 && (b === 18 || b === 19)) return false;
  return true;
}

/**
 * Why a URL is not offered as a link (null when it is a public absolute http(s) URL). Strict and
 * conservative: ASCII URL characters only, well-formed escapes, lowercase scheme, no credentials,
 * no explicit port, no IPv6 literal, no local or special-use host, no non-public IPv4.
 */
export function urlProblem(value) {
  if (typeof value !== 'string' || !value) return 'not a URL';
  if (value.length > MAX_URL) return 'too long';
  if (!URL_CHARS.test(value) || BAD_ESCAPE.test(value)) return 'characters outside the URL grammar';
  const match = /^(https?):\/\/([^/?#]*)/.exec(value);
  if (!match) return 'not an absolute http(s) URL';
  const authority = match[2];
  if (!authority) return 'no host';
  if (authority.includes('@')) return 'carries credentials';
  if (authority.includes('[') || authority.includes(']')) return 'IP literal host';
  if (authority.includes(':')) return 'explicit port';
  const host = authority.toLowerCase();
  if (/^[0-9.]+$/.test(host)) return publicIPv4(host) ? null : 'non-public IP address';
  if (host.endsWith('.')) return 'trailing-dot host';
  if (BLOCKED_HOSTS.has(host) || BLOCKED_SUFFIXES.some((s) => host.endsWith(s))) return 'local or special-use host';
  const labels = host.split('.');
  if (host.length > 253 || labels.length < 2 || !labels.every((l) => LABEL.test(l))) return 'invalid host name';
  if (!TLD.test(labels[labels.length - 1])) return 'invalid top-level domain';
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password || parsed.port) return 'credentials or port';
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return 'not http(s)';
  } catch {
    return 'not a valid URL';
  }
  return null;
}

/** The href to offer for a source URL, or null (link withheld; the text is still shown). */
export function safeHref(value) {
  return urlProblem(value) === null ? value : null;
}

/** Host name shown beside a source title (from a safe URL only). */
export function sourceHost(value) {
  if (safeHref(value) === null) return '';
  const host = /^https?:\/\/([^/?#]+)/.exec(value)[1].toLowerCase();
  return host.startsWith('www.') ? host.slice(4) : host;
}

const str = (v) => (typeof v === 'string' ? v : '');
const strings = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x.trim()) : []);

/**
 * Normalized research section of a proposal, or null when research was not requested.
 * {requested, status, verified, id, error, equipmentType, summary, sources, features, assumptions,
 *  unknowns, warnings, briefSha, verifiedAt, elapsed, checks, facts, searches, persistence, limitation}
 */
export function reviewResearch(p) {
  const r = p && p.research && typeof p.research === 'object' ? p.research : null;
  if (!r || r.requested !== true) return null;
  const status = str(r.status) || 'unknown';
  const brief = status === 'verified' && r.brief && typeof r.brief === 'object' ? r.brief : null;
  const sources = (brief && Array.isArray(brief.sources) ? brief.sources : []).filter((s) => s && typeof s === 'object').map((s) => {
    const url = str(s.url);
    const problem = urlProblem(url);
    return {
      id: str(s.id), title: str(s.title) || url, url, href: problem === null ? url : null, linkProblem: problem,
      host: sourceHost(url), observations: strings(s.observations),
    };
  });
  const known = new Set(sources.map((s) => s.id));
  const features = (brief && Array.isArray(brief.features) ? brief.features : []).filter((f) => f && typeof f === 'object').map((f) => {
    const ids = strings(f.source_ids);
    return { text: str(f.text), sourceIds: ids, unknownSources: ids.filter((id) => !known.has(id)) };
  });
  const error = r.error && typeof r.error === 'object' ? { code: str(r.error.code), message: str(r.error.message) } : null;
  return {
    requested: true,
    status,
    verified: status === 'verified' && !!brief,
    id: str(r.id),
    error,
    equipmentType: brief ? str(brief.equipment_type) : '',
    summary: brief ? str(brief.summary) : '',
    sources,
    features,
    assumptions: brief ? strings(brief.assumptions) : [],
    unknowns: brief ? strings(brief.unknowns) : [],
    warnings: brief ? strings(brief.warnings) : [],
    briefSha: str(r.brief_sha256),
    verifiedAt: str(r.verified_at),
    elapsed: Number.isFinite(r.elapsed_s) ? r.elapsed_s : null,
    checks: r.checks && typeof r.checks === 'object' ? r.checks : {},
    facts: researchFacts(r.metadata),
    searches: researchSearches(r.metadata),
    persistence: researchPersistence(r.persistence),
    limitation: str(r.limitation),
  };
}

// credential-like names, long evidence lists (URLs, per-search records) that the review shows elsewhere,
// and the local CLI path (a file path on this machine, not a fact about the research)
const HIDDEN_FACT = /secret|password|credential|token|^key$|(auth|access|refresh|bearer|session|api)[_-]?(token|key|id)$|_urls$|^searches$|^cli$/i;

/**
 * Research run facts to display (model, effort, verification, search counts, timing ...) from the bounded
 * metadata the service kept; long URL lists and anything credential-like are omitted. Values are text.
 */
export function researchFacts(metadata) {
  if (!metadata || typeof metadata !== 'object') return [];
  const out = [];
  const scalar = (v) => v == null || ['string', 'number', 'boolean'].includes(typeof v);
  for (const [key, value] of Object.entries(metadata)) {
    if (HIDDEN_FACT.test(key) || value == null) continue;
    const label = key.replace(/_/g, ' ');
    if (scalar(value)) out.push([label, String(value)]);
    else if (Array.isArray(value) && value.length <= 8 && value.every(scalar)) out.push([label, value.length ? value.map(String).join(', ') : 'none']);
    else if (!Array.isArray(value) && typeof value === 'object') {
      for (const [sub, v] of Object.entries(value)) if (v != null && scalar(v) && !HIDDEN_FACT.test(sub)) out.push([`${label} ${sub}`, String(v)]);
    }
  }
  return out.slice(0, 40);
}

export const SEARCH_RECORDS_SHOWN = 12;
export const QUERY_SHOWN = 300;
// skid/research/stream.py records a query longer than 100 characters as its first 100 and '…'
const RECORDED_QUERY = 100;

/**
 * The search records the research stream kept, as given: {records: [{query, clipped, status, links}], more,
 * missing}. `clipped` is 'recorded' for a query the recorder already shortened, 'display' when this view
 * cut an unexpectedly long value, else null; `query` null means no query was recorded. `missing` is true
 * when the run reports searches but no records reached the proposal (the metadata size bound drops them).
 * null when the metadata says nothing about searches. Values are text, never links or markup.
 */
export function researchSearches(metadata) {
  if (!metadata || typeof metadata !== 'object') return null;
  const list = Array.isArray(metadata.searches) ? metadata.searches : null;
  if (!list) {
    const calls = metadata.search_calls;
    return Number.isInteger(calls) && calls > 0 ? { records: [], more: 0, missing: true } : null;
  }
  const records = list.slice(0, SEARCH_RECORDS_SHOWN).map((s) => {
    const query = s && typeof s === 'object' && typeof s.query === 'string' ? s.query : null;
    const recorded = query !== null && query.length === RECORDED_QUERY + 1 && query.endsWith('…');
    const long = query !== null && query.length > QUERY_SHOWN;
    return {
      query: long ? query.slice(0, QUERY_SHOWN) : query,
      clipped: recorded ? 'recorded' : long ? 'display' : null,
      status: (s && typeof s === 'object' && str(s.status)) || 'unknown',
      links: s && Number.isInteger(s.links) && s.links >= 0 ? s.links : null,
    };
  });
  return { records, more: list.length - records.length, missing: false };
}

/**
 * Where a ready researched plan's references go, as the service states it: 'saved_with_assumptions'
 * (the listed assumption records get the research identity and source URLs on apply) or 'proposal_only'
 * (the plan records no assumption, so nothing in the model will cite the research). null otherwise.
 */
export function researchPersistence(value) {
  if (!value || typeof value !== 'object') return null;
  const status = str(value.status);
  if (status !== 'saved_with_assumptions' && status !== 'proposal_only') return null;
  return { status, assumptionIds: strings(value.assumption_ids), note: str(value.note) };
}

/** Source IDs each proposed assumption cites, keyed by the assumption's model ID (or plan key). */
export function assumptionCitations(p) {
  const out = new Map();
  for (const a of Array.isArray(p && p.proposed_assumptions) ? p.proposed_assumptions : []) {
    if (!a || typeof a !== 'object') continue;
    const ids = strings(a.source_ids);
    if (ids.length) out.set(str(a.id) || str(a.key), ids);
  }
  return out;
}

/** Whether the running service offers equipment research (old services do not advertise it). */
export function researchOffered(state) {
  return !!(state && state.capabilities && state.capabilities.equipment_research === true
    && state.planning && state.planning.research && state.planning.research.available === true);
}

/**
 * The progress line for a running engineering request: stage label and step from the service's
 * plan record when known, else from what was requested.
 */
export function stageInfo(plan, requestedResearch) {
  const research = plan && typeof plan.research === 'boolean' ? plan.research : !!requestedResearch;
  const stage = plan && STAGE_LABELS[plan.stage] ? plan.stage : null;
  if (!research) return { label: 'Planning', step: null, stage: stage || 'planning', research };
  if (!stage) return { label: 'Researching, then planning', step: null, stage: null, research };
  return { label: STAGE_LABELS[stage], step: stage === 'researching' ? 'step 1 of 2' : 'step 2 of 2', stage, research };
}
