import { lookupByYahooKey, matchAdp, adpMatchState } from './identity.js';

/**
 * Decorate Yahoo players with external attributes.
 * Unmatched players pass through untouched: enrichment is additive and must
 * never break a command that worked without it.
 *
 * `stats.adp` classifies every player exactly once into matched / ambiguous /
 * absent (via `adpMatchState`) rather than a bare match count, because the
 * spec requires telling "the ADP source changed format and lookups are now
 * ambiguous" apart from "this player just isn't in the feed" -- those two
 * failures call for different responses, and a single count conflates them.
 */
export function enrichPlayers(players, { crosswalk, adpIndex, capabilities = [] } = {}) {
  const want = new Set(capabilities);
  const stats = {
    total: players.length,
    adp: { matched: 0, ambiguous: 0, absent: 0 },
    injury: { matched: 0 },
  };
  if (want.size === 0) return { players, stats };

  const enriched = players.map((p) => {
    const out = { ...p };
    const xw = lookupByYahooKey(crosswalk, p.player_key);

    if (want.has('injury') && xw?.injuryStatus) {
      out.injury = xw.injuryStatus;
      stats.injury.matched += 1;
    }
    if (want.has('adp')) {
      const query = {
        name: p.name?.full ?? xw?.name,
        position: p.display_position ?? xw?.position,
        // Sleeper assigns no yahoo_id to team defenses, so a DEF can never
        // appear in the crosswalk and xw is always null -- without this
        // fallback to Yahoo's own editorial_team_abbr, every defense
        // silently resolves to 'absent' (DEF has no name-only fallback; see
        // identity.js). Prefer the crosswalk's team when present: it is the
        // authoritative post-trade value for a player who did get matched.
        team: xw?.team ?? p.editorial_team_abbr,
      };
      const state = adpMatchState(adpIndex, query);
      stats.adp[state] += 1;
      if (state === 'matched') {
        out.adp = matchAdp(adpIndex, query).adp;
      }
    }
    return out;
  });

  return { players: enriched, stats };
}
