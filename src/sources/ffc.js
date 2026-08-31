const BASE = 'https://fantasyfootballcalculator.com/api/v1/adp';

/**
 * FFC serves a DIFFERENT dataset per scoring format -- not the same numbers
 * rounded differently. Non-PPR and Half-PPR for 2026 were aggregated over
 * 1,884 and 3,208 drafts respectively and list a different number of players.
 * An ADP shown without its variant is therefore not "approximate", it is
 * potentially the wrong board entirely, so the format is a first-class,
 * user-selectable input rather than a constant buried in a default argument.
 */
export const SCORING_FORMATS = ['standard', 'half-ppr', 'ppr'];

export const meta = {
  name: 'ffc',
  provides: ['adp'],
  // FFC exposes only its own player_id, with no external crosswalk, so ADP
  // must be matched on normalised name + position. See identity.js.
  joinKey: 'fuzzy',
  ttlHours: 12,
  documented: true,
  // The API is public and unauthenticated, but FFC publishes no reuse terms.
  // 'unverified' rather than 'public': see the design doc's §2 constraints.
  license: 'unverified',
};

/**
 * Fetches raw ADP data from Fantasy Football Calculator.
 * Injects fetch to allow testing without network calls.
 *
 * `teams`/`scoring` keep FFC's own defaults (12-team Non-PPR) rather than any
 * one league's settings: this module knows nothing about the caller's league.
 * `tt sync --scoring=half-ppr --teams=10` is how a user selects their own.
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
 * FFC's feed-level metadata: which scoring variant, how many teams, and the
 * draft window the average was taken over. Kept in the normalized payload
 * because it is the only evidence of WHICH ADP the cached numbers are --
 * discarding it is what makes an unlabelled ADP column dangerous.
 */
function normalizeMeta(raw) {
  if (!raw || typeof raw !== 'object') return null;
  return {
    type: raw.type ?? null,
    teams: raw.teams ?? null,
    rounds: raw.rounds ?? null,
    totalDrafts: raw.total_drafts ?? null,
    startDate: raw.start_date ?? null,
    endDate: raw.end_date ?? null,
  };
}

/**
 * Normalizes FFC raw response into a uniform shape.
 * Maps snake_case fields to camelCase, preserves position and team exactly
 * as received (position aliasing and DEF-by-team matching are handled in identity.js).
 *
 * Returns `{ meta, records }` rather than a bare array: consumers need the
 * variant alongside the numbers. `recordsOf`/`metaOf` in sources/index.js
 * read both this and the older bare-array cache files.
 */
export function normalize(raw) {
  return {
    meta: normalizeMeta(raw?.meta),
    records: (raw?.players ?? []).map((p) => ({
      name: p.name,
      position: p.position,
      team: p.team,
      adp: p.adp,
      timesDrafted: p.times_drafted,
      high: p.high,
      low: p.low,
      stdev: p.stdev,
      bye: p.bye,
    })),
  };
}
