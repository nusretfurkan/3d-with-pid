// Same-origin JSON transport for the local service (docs/INTERACTIVE.md).
// Errors carry the service's {error, diagnostics} body and HTTP status.

export class ApiError extends Error {
  constructor(status, error, diagnostics = [], network = false) {
    super(error);
    this.status = status;
    this.error = error;
    this.diagnostics = Array.isArray(diagnostics) ? diagnostics : [];
    this.network = network;
  }

  get conflict() {
    return this.status === 409;
  }
}

async function request(url, init, timeoutMs, signal) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const forward = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', forward, { once: true });
  }
  let response;
  try {
    response = await fetch(url, { ...init, signal: controller.signal, cache: 'no-store', credentials: 'same-origin' });
  } catch (exc) {
    if (signal && signal.aborted) {
      const stopped = new ApiError(0, 'stopped waiting for the answer', [], false);
      stopped.aborted = true;
      throw stopped;
    }
    const aborted = exc && exc.name === 'AbortError';
    throw new ApiError(0, aborted ? `no answer from the local service within ${Math.round(timeoutMs / 1000)} s`
      : 'the local service is not reachable', [], true);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', forward);
  }
  return response;
}

async function decode(response) {
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!response.ok) {
    const error = body && typeof body.error === 'string' ? body.error : `HTTP ${response.status}`;
    throw new ApiError(response.status, error, body && body.diagnostics);
  }
  if (body === null) throw new ApiError(response.status, 'the service answered with something other than JSON');
  return body;
}

export async function getJSON(url, timeoutMs = 10000) {
  return decode(await request(url, { method: 'GET', headers: { Accept: 'application/json' } }, timeoutMs));
}

export async function postJSON(url, body, timeoutMs = 30000, { signal } = {}) {
  return decode(await request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  }, timeoutMs, signal));
}

/** Text of an immutable artifact (SVG/HTML). */
export async function getText(url, timeoutMs = 20000) {
  const response = await request(url, { method: 'GET' }, timeoutMs);
  if (!response.ok) throw new ApiError(response.status, `could not load ${url} (HTTP ${response.status})`);
  return response.text();
}
