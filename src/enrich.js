import { lookupByYahooKey, matchAdp, adpMatchState } from './identity.js';

/**
 * The capabilities `enrichPlayers` below actually attaches. This is NOT
 * `allCapabilities()` from the source registry: Sleeper's meta also advertises
 * 'identity' and 'depth', which no code here consumes. Validating `--with`
 * against the registry accepted `--with=depth` and then silently produced
 * output identical to no flag at all -- no column, no footer, exit 0.
 * `--with` validates against this list instead, so the error message offers
 * only flags that do something. Adding a branch below means adding its name
 * here, in the same commit.
 */
export const IMPLEMENTED_CAPABILITIES = ['adp', 'injury'];

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
        // Yahoo first: Yahoo is the league of record, it is the team the
        // user sees in the TEAM column, and spec 4.1 rule 3 frames the
        // tiebreaker against Yahoo's team. Preferring Sleeper's team here
        // meant a Sleeper row left stale by a trade could send the
        // team-qualified lookup at a DIFFERENT player's FFC row -- the only
        // path in this module that can attach a wrong player's ADP, which is
        // exactly the outcome the never-guess policy exists to prevent.
        // The crosswalk remains the fallback: Sleeper assigns no yahoo_id to
        // team defenses, so a DEF is never in the crosswalk, and a Yahoo row
        // without editorial_team_abbr still gets a team to match on.
        team: p.editorial_team_abbr ?? xw?.team,
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
