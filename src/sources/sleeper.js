const URL_PLAYERS = 'https://api.sleeper.app/v1/players/nfl';

export const meta = {
  name: 'sleeper',
  provides: ['identity', 'injury', 'depth'],
  joinKey: 'yahoo_id',
  ttlHours: 24,
  documented: true,
  license: 'non-commercial',
};

export async function fetchRaw({ fetch: fetchImpl = globalThis.fetch } = {}) {
  const res = await fetchImpl(URL_PLAYERS);
  if (!res.ok) throw new Error(`Sleeper players fetch failed: HTTP ${res.status}`);
  return res.json();
}

/**
 * Players without a yahoo_id cannot be joined to a Yahoo roster, so they are
 * dropped here rather than carried through the pipeline.
 * yahooId is coerced to a string: Sleeper sends a number, Yahoo's player_key
 * parses to a string, and a type mismatch makes every join miss silently.
 */
export function normalize(raw) {
  const out = [];
  for (const p of Object.values(raw ?? {})) {
    if (p?.yahoo_id === null || p?.yahoo_id === undefined) continue;
    out.push({
      sleeperId: String(p.player_id),
      yahooId: String(p.yahoo_id),
      // Trimmed, not passed through: Sleeper ships 855 of its 3,875
      // gsis_id values with a LEADING SPACE (" 00-0023177"), and nflverse
      // player_ids have none, so every one of those fails an exact-match
      // join -- silently, since a missed join is indistinguishable from a
      // player who is simply absent from the crosswalk. Same failure the
      // yahooId coercion above exists to prevent. A whitespace-only id
      // becomes null: it carries no identity, and an empty string would
      // join against nothing while still looking present.
      gsisId: typeof p.gsis_id === 'string' ? (p.gsis_id.trim() || null) : (p.gsis_id ?? null),
      espnId: p.espn_id === null || p.espn_id === undefined ? null : String(p.espn_id),
      name: p.full_name ?? [p.first_name, p.last_name].filter(Boolean).join(' '),
      position: p.position ?? null,
      team: p.team ?? null,
      injuryStatus: p.injury_status ?? null,
      depthChartOrder: p.depth_chart_order ?? null,
    });
  }
  return out;
}
