import { normalize } from './normalize.js';

const BASE = 'https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2';
const REFERER = 'https://football.fantasysports.yahoo.com/';
const USER_AGENT =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36';

/** The session is gone. Callers decide whether to re-auth (see design doc §9.1). */
export class SessionExpiredError extends Error {
  constructor(message = 'Yahoo session expired or rejected') {
    super(message);
    this.name = 'SessionExpiredError';
  }
}

/** Yahoo answered, but with an error envelope or a non-OK status. */
export class YahooApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'YahooApiError';
    this.status = status;
  }
}

/**
 * Read client for the Yahoo Fantasy v2 API over a browser session.
 * `fetch` is injectable so callers (and tests) can supply their own transport.
 */
export function createClient({ cookieHeader, fetch: fetchImpl = globalThis.fetch, baseUrl = BASE }) {
  async function get(resource, params = {}) {
    const url = new URL(`${baseUrl}/${resource}`);
    url.searchParams.set('format', 'json');
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);

    const res = await fetchImpl(url.toString(), {
      headers: { Cookie: cookieHeader, 'User-Agent': USER_AGENT, Referer: REFERER },
    });

    // 401/403 is the unambiguous dead-session signal; never trust cookie expiry.
    if (res.status === 401 || res.status === 403) {
      throw new SessionExpiredError(`Yahoo returned ${res.status} for ${resource}`);
    }
    if (!res.ok) {
      throw new YahooApiError(`Yahoo returned ${res.status} for ${resource}`, res.status);
    }

    const body = await res.json();
    // Yahoo reports semantic failures in a 200 body, not the status code.
    if (body?.error) {
      throw new YahooApiError(body.error.description ?? 'Unknown Yahoo API error', res.status);
    }
    return normalize(body);
  }

  return { get };
}
