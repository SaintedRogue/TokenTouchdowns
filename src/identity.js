/** Yahoo player keys look like `<game_key>.p.<player_id>`. */
export function yahooPlayerId(playerKey) {
  const m = /^\d+\.p\.(\d+)$/.exec(playerKey ?? '');
  return m ? m[1] : null;
}

export function buildCrosswalk(records) {
  const map = new Map();
  for (const r of records ?? []) if (r?.yahooId) map.set(String(r.yahooId), r);
  return map;
}

export function lookupByYahooKey(crosswalk, playerKey) {
  const id = yahooPlayerId(playerKey);
  return id ? crosswalk.get(id) ?? null : null;
}

const SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);

/** Lowercase, drop punctuation, drop generational suffixes, collapse spaces. */
export function normalizeName(name) {
  return String(name ?? '')
    .toLowerCase()
    .replace(/[^a-z\s]/g, '')
    .split(/\s+/)
    .filter((w) => w && !SUFFIXES.has(w))
    .join(' ');
}

const keyOf = (name, position) => `${normalizeName(name)}|${String(position ?? '').toUpperCase()}`;

/**
 * Index ADP records by `name|POSITION`. A key colliding between two distinct
 * records stores `null` -- the marker for "ambiguous, never guess". The
 * colliding records are kept under a team-qualified key so an exact team
 * match can still resolve them.
 */
export function buildAdpIndex(records) {
  const index = new Map();
  for (const r of records ?? []) {
    const k = keyOf(r.name, r.position);
    index.set(k, index.has(k) ? null : r);
    if (r.team) index.set(`${k}|${String(r.team).toUpperCase()}`, r);
  }
  return index;
}

/**
 * Look up ADP for a player. Returns null rather than a best guess: a wrong ADP
 * on a plausible player is worse than a missing one, because it gets drafted on.
 */
export function matchAdp(index, { name, position, team } = {}) {
  const k = keyOf(name, position);
  if (team) {
    const exact = index.get(`${k}|${String(team).toUpperCase()}`);
    if (exact) return exact;
  }
  return index.get(k) ?? null;
}
