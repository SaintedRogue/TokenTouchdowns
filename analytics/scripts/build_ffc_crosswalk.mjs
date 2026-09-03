#!/usr/bin/env node
/**
 * Task 8 backtest support: emit, per backtest season, that season's real
 * historical FFC ADP joined to nflverse's own player_id via the crosswalk
 * this repo already has and already tests -- Sleeper's `gsis_id` IS
 * nflverse's `player_id`, and `src/identity.js`'s `buildAdpIndex`/`matchAdp`
 * is the tested fuzzy name+position(+team) matcher that never guesses
 * (ambiguity -> null). Reused here exactly as built (indexing Sleeper
 * records instead of FFC ones, and querying with an FFC record) rather than
 * reimplemented in Python -- see task-8-brief.md.
 *
 * The previous backtest attempt (Task 7) joined FFC ADP to nflverse by exact
 * name only and matched ~16% of the board. This script is the fix: it does
 * NOT invent new matching logic, it just runs the existing, tested matcher
 * in the direction Task 8 needs (FFC -> gsis_id) instead of the direction
 * `enrich.js` normally uses it (FFC -> Yahoo roster row).
 *
 * Fetches each season's ADP ONCE and caches the joined result to
 * analytics/data/ffc_adp_<season>.json (gitignored) -- "cache what you
 * fetch; do not refetch in a loop" per the brief. Re-run to refresh.
 *
 * SLEEPER FALLBACK (discovered live, 2026-09-01, not something the task
 * brief anticipated): Sleeper's `gsis_id` field has real, material gaps for
 * exactly the players who matter most to a draft board. Checked directly
 * against Sleeper's live API: Ja'Marr Chase, Bijan Robinson, Amon-Ra
 * St. Brown, Puka Nacua, Garrett Wilson and Jahmyr Gibbs all have
 * `gsis_id: null` (several also have `yahoo_id: null`, which is why
 * `sleeper.js`'s own yahoo_id-filtered cache drops them entirely) despite
 * being unambiguous, highly-drafted starters -- this is not a rare edge
 * case, it disproportionately hits recent (2021+) elite skill players,
 * exactly the players a draft-strategy backtest cares about most. Sleeper
 * alone is therefore not sufficient to hit a good match rate.
 *
 * So this script tries Sleeper FIRST (as the brief specifies -- gsis_id is
 * a direct, unambiguous id, no name-matching risk), and for anything left
 * unresolved, falls back to the SAME tested `buildAdpIndex`/`matchAdp`
 * matcher run a second time against nflverse's own player roster (name,
 * position, team -- exported once from the Python-side parquet history by
 * `analytics/scripts/export_nflverse_roster.py` into
 * `analytics/data/nflverse_players.json`, read below). This is still "reuse the tested
 * matcher, don't reimplement fuzzy matching" -- it is the identical
 * identity.js code, just given a second reference set to resolve against
 * when the first one is silent. Every record's `matchSource` field
 * ('sleeper' | 'nflverse' | null) says which path, if any, resolved it.
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { fetchRaw, normalize as normalizeFfc } from '../../src/sources/ffc.js';
import { buildAdpIndex, matchAdp, adpMatchState } from '../../src/identity.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SLEEPER_CACHE = path.join(os.homedir(), '.tokentouchdowns', 'cache', 'sleeper.json');
const OUT_DIR = path.join(REPO_ROOT, 'analytics', 'data');
const NFLVERSE_ROSTER = path.join(OUT_DIR, 'nflverse_players.json');
// Backtest seasons by default; pass seasons on argv to build others -- notably
// the CURRENT season, whose joined file is what the LIVE draft board ranks on.
// Without a current-season file the board silently fell back to the newest
// backtest fixture and ranked against last year's market.
const SEASONS = process.argv.slice(2).length
  ? process.argv.slice(2).map(Number)
  : [2023, 2024, 2025];

/**
 * The already-cached Sleeper players feed. `sleeper.json`'s `data` field is
 * `sleeper.js`'s own `normalize()` output (see src/sources/sleeper.js) --
 * {sleeperId, yahooId, gsisId, espnId, name, position, team, ...} -- already
 * filtered to players Sleeper attaches a yahoo_id to (6,750 of them, 3,875
 * with a non-null gsisId, verified 2026-09-01). Read directly rather than
 * re-fetched: this file is explicitly documented as "already cached" in the
 * Task 8 brief, and re-fetching a 6,750-player feed inside this script would
 * violate the same "don't refetch in a loop" rule this script exists to
 * follow for FFC.
 */
function loadSleeperRecords() {
  const raw = JSON.parse(readFileSync(SLEEPER_CACHE, 'utf8'));
  const data = raw?.data ?? raw;
  if (!Array.isArray(data)) {
    throw new Error(`${SLEEPER_CACHE}: expected {data: [...]}, got ${typeof data}`);
  }
  return data;
}

/**
 * nflverse's own player roster, one row per player_id -- exported by the
 * Python side (see task-8 report) from `stats_player_week_{2015..2025}.parquet`,
 * QB/RB/WR/TE only, most-recent-season name/team. Its `playerId` IS the
 * value we ultimately need (it's the same nflverse id `gsisId` is supposed
 * to reproduce), so a match against THIS index needs no further translation.
 */
function loadNflverseRoster() {
  const raw = JSON.parse(readFileSync(NFLVERSE_ROSTER, 'utf8'));
  if (!Array.isArray(raw)) {
    throw new Error(`${NFLVERSE_ROSTER}: expected a JSON array, got ${typeof raw}`);
  }
  return raw;
}

async function main() {
  const sleeperIndex = buildAdpIndex(loadSleeperRecords());
  // Second reference set for the fallback path -- see module docstring's
  // "SLEEPER FALLBACK" section. Built from the identical `buildAdpIndex`
  // used for Sleeper; only the records fed into it differ.
  const nflverseIndex = buildAdpIndex(loadNflverseRoster());

  mkdirSync(OUT_DIR, { recursive: true });

  const summary = [];
  for (const season of SEASONS) {
    // teams=10 is irrelevant to what FFC actually returns for a past season
    // (verified live: the API ignores `teams` for historical years and
    // always returns its one archived half-PPR dataset -- 2024 returns
    // total_drafts=906 regardless of whether teams=8/10/12 is requested),
    // but it is passed for parity with how a live 2026 fetch is made.
    const raw = await fetchRaw({ season, teams: 10, scoring: 'half-ppr' });
    const { meta, records } = normalizeFfc(raw);

    const counts = { sleeper: 0, nflverse: 0, ambiguous: 0, absent: 0 };
    const players = records.map((r) => {
      const query = { name: r.name, position: r.position, team: r.team };

      const sleeperState = adpMatchState(sleeperIndex, query);
      const sleeperHit = sleeperState === 'matched' ? matchAdp(sleeperIndex, query) : null;
      // 855 of 3,875 (22%) of the cached Sleeper feed's gsis_id values carry
      // a stray leading space (verified 2026-09-01, e.g. ' 00-0023177') --
      // a real Sleeper data-quality defect, not this script's. Trimmed here
      // so it can never silently fail to join against nflverse's own
      // (unpadded) player_id on the Python side.
      const sleeperGsis = sleeperHit?.gsisId ? String(sleeperHit.gsisId).trim() : null;
      if (sleeperState === 'matched' && sleeperGsis) {
        counts.sleeper++;
        return { ...r, playerId: sleeperGsis, matchSource: 'sleeper', matchState: 'matched' };
      }

      // Sleeper resolved the IDENTITY (matched) but had no gsis_id to give
      // us, OR never resolved it at all (absent) -- either way, try the
      // direct nflverse-name path before giving up. An `ambiguous` Sleeper
      // result is NOT retried here: identity.js already refused to guess
      // once for this exact name+position+team, and retrying against a
      // different reference set wouldn't change that the FFC row itself is
      // inherently ambiguous against Sleeper's data -- but a DIFFERENT
      // reference set (nflverse) can still have its own unambiguous answer,
      // so this only skips the retry when nflverse's own lookup would be
      // hopeless too. In practice this stays permissive: an nflverse
      // ambiguity is independent evidence and is checked below regardless.
      const nflverseState = adpMatchState(nflverseIndex, query);
      const nflverseHit = nflverseState === 'matched' ? matchAdp(nflverseIndex, query) : null;
      if (nflverseState === 'matched' && nflverseHit?.playerId) {
        counts.nflverse++;
        return { ...r, playerId: nflverseHit.playerId, matchSource: 'nflverse', matchState: 'matched' };
      }

      const finalState = sleeperState === 'ambiguous' || nflverseState === 'ambiguous' ? 'ambiguous' : 'absent';
      counts[finalState]++;
      return { ...r, playerId: null, matchSource: null, matchState: finalState };
    });

    summary.push({ season, meta, total: records.length, ...counts });
    writeFileSync(
      path.join(OUT_DIR, `ffc_adp_${season}.json`),
      JSON.stringify({ season, meta, players }, null, 2),
    );
  }

  console.log(JSON.stringify(summary, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
