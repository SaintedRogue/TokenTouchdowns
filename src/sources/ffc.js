const BASE = 'https://fantasyfootballcalculator.com/api/v1/adp';

export const meta = {
  name: 'ffc',
  provides: ['adp'],
  // FFC exposes only its own player_id, with no external crosswalk, so ADP
  // must be matched on normalised name + position. See identity.js.
  joinKey: 'fuzzy',
  ttlHours: 12,
  documented: true,
  license: 'public',
};

/**
 * Fetches raw ADP data from Fantasy Football Calculator.
 * Injects fetch to allow testing without network calls.
 */
export async function fetchRaw({
  fetch: fetchImpl = globalThis.fetch,
  season = new Date().getUTCFullYear(),
  teams = 12,
  scoring = 'standard',
} = {}) {
  const url = new URL(`${BASE}/${scoring}`);
  url.searchParams.set('teams', String(teams));
  url.searchParams.set('year', String(season));
  const res = await fetchImpl(url.toString());
  if (!res.ok) throw new Error(`FFC ADP fetch failed: HTTP ${res.status}`);
  return res.json();
}

/**
 * Normalizes FFC raw response into a uniform shape.
 * Maps snake_case fields to camelCase, preserves position and team exactly
 * as received (position aliasing and DEF-by-team matching are handled in identity.js).
 */
export function normalize(raw) {
  return (raw?.players ?? []).map((p) => ({
    name: p.name,
    position: p.position,
    team: p.team,
    adp: p.adp,
    timesDrafted: p.times_drafted,
    high: p.high,
    low: p.low,
    stdev: p.stdev,
    bye: p.bye,
  }));
}
