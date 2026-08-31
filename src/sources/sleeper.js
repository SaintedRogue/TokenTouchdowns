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
      gsisId: p.gsis_id ?? null,
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
