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
