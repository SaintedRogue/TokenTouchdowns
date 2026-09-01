/** Split argv into a command, positional arguments, and long flags. */
export function parseArgs(argv) {
  const flags = {};
  const args = [];
  for (const token of argv) {
    if (token === '-h' || token === '--help') { flags.help = true; continue; }
    if (token.startsWith('--')) {
      const [name, value] = token.slice(2).split('=');
      flags[name] = value ?? true;
      continue;
    }
    args.push(token);
  }
  const command = flags.help || args.length === 0 ? 'help' : args.shift();
  return { command, args, flags };
}

/** Render rows as an aligned plain-text table. */
export function formatTable(rows, columns) {
  if (rows.length === 0) return '(none)';
  const cell = (row, col) => {
    const v = row[col.key];
    // '-' means "no such value"; an empty string is a deliberate blank
    // (e.g. an unset marker column) and should render as whitespace.
    if (v === undefined || v === null) return '-';
    return String(v);
  };
  const widths = columns.map((col) =>
    Math.max(col.label.length, ...rows.map((r) => cell(r, col).length)));
  const line = (cells) =>
    cells.map((c, i) => (i === cells.length - 1 ? c : c.padEnd(widths[i]))).join('  ');
  return [
    line(columns.map((c) => c.label)),
    ...rows.map((r) => line(columns.map((c) => cell(r, c)))),
  ].join('\n');
}

import { writeFile, readFile } from 'node:fs/promises';
import { SessionExpiredError, YahooApiError } from './client.js';
import {
  SOURCES, sourcesProviding, recordsOf, metaOf, feedVariantLabel,
} from './sources/index.js';
import { SCORING_FORMATS } from './sources/ffc.js';
import { readCache, writeCache } from './cache.js';
import { buildCrosswalk, buildAdpIndex, matchAdp, lookupByYahooKey } from './identity.js';
import { enrichPlayers, IMPLEMENTED_CAPABILITIES } from './enrich.js';
import { createAnalyticsClient, AnalyticsError, DEFAULT_ANALYTICS_DIR } from './analytics.js';
import path from 'node:path';

/**
 * The user's input was invalid -- distinct from a Yahoo-side failure. A typo
 * in `--with` is the user's mistake, not Yahoo's, and must not be reported
 * (or exit-coded) as a Yahoo API error.
 */
export class UsageError extends Error {
  constructor(message) { super(message); this.name = 'UsageError'; }
}

const USAGE = `tokentouchdowns — Yahoo fantasy football CLI

Usage: tt <command> [args] [--json]

Commands:
  login                  Sign in to Yahoo (opens a browser once)
  leagues                List your fantasy leagues
  teams [league_key]     List teams in a league
  standings [league_key] Show league standings
  roster [team_key]      Show a team roster (--week=N for a past/future week)
  matchup [week]         Show your matchup for a week (default: current)
  transactions [league]  Recent adds, drops and trades
  players [league_key]   Browse the player pool
  sync                   Refresh cached external data (ADP, injury, ...)
  sources                List registered external data sources
  league export [key]    Export scoring/roster settings for the draft engine
  draft board            Ranked draft board (VOR, tier, ADP, survival)
  draft pick              What to take right now
  mock                   Simulate and compare draft strategies
  lineup                 Optimal lineup for this week
  playoff                Variance-aware lineup vs. an opponent
  season                 Championship odds via season simulation
  help                   Show this message

Draft engine flags (analytics/data/league.json + nflverse parquet required):
  --teams=N  --slot=K  --season=YYYY  --conditional
  draft board [--teams N] [--slot K] [--count N]
  draft pick  [--roster PATH] --slot K [--round N] [--rounds N]
  mock [--trials N] [--teams N] [--strategy adp,vor,vor_survival] [--rounds N]
  lineup [--week N]
  playoff [--week N] [--opponent team_key_or_name]
  season  [--n N] [--week N]
          By default, fetches every team's live roster + real schedule and
          simulates the season -- fails clearly if the league is predraft
          (nothing to simulate yet). For a demonstration before the draft:
            --mock-draft          simulate a draft, then simulate the season
            --rosters=PATH        supply your own hypothetical rosters
                                   (JSON: {"rosters": {team: [...]}, "schedule": [...]})

Player filters:
  --position=QB  --status=A  --search=kelce  --count=25  --sort=OR
  --with=adp,injury      Enrich rows with cached external data (run sync first)

Transaction filters:
  --type=add,drop  --team=<team_key>  --count=25

Sync flags:
  --source=<name>  --force
  --scoring=standard|half-ppr|ppr   ADP scoring format (default: standard)
  --teams=<n>                       ADP league size (default: 12)
  ADP is published per scoring format and league size; the two flags above
  select which board --with=adp shows, and the format is labelled in the
  footer so an unlabelled number can never be read as the wrong one.

League export flags:
  --out=PATH   Write the config to a file instead of stdout

Omitted keys are resolved from your own leagues and team.`;

const LEAGUES_RESOURCE = 'users;use_login=1/games;game_keys=nfl/leagues';

/** Flags that map to Yahoo player matrix parameters. Anything else is ignored. */
const PLAYER_FILTERS = ['position', 'status', 'search', 'sort'];

/**
 * Build a `players` resource path with Yahoo's matrix-parameter syntax.
 * Always paginated: an unbounded player request pulls thousands of rows.
 */
export function buildPlayersResource(leagueKey, flags = {}) {
  // Default to overall rank: Yahoo's natural order is arbitrary, which puts
  // fringe players ahead of stars when browsing a position.
  const withDefaults = { sort: 'OR', ...flags };
  const parts = PLAYER_FILTERS
    .filter((f) => withDefaults[f] !== undefined && withDefaults[f] !== true)
    .map((f) => `${f}=${withDefaults[f]}`);
  parts.push(`start=${flags.start ?? 0}`, `count=${flags.count ?? 25}`);
  return `league/${leagueKey}/players;${parts.join(';')}`;
}

/**
 * Build a `roster` resource path, optionally scoped to a week.
 * A bare `--week` parses as `true` and must not become ";week=true".
 */
export function buildRosterResource(teamKey, flags = {}) {
  const week = flags.week;
  if (week === undefined || week === true) return `team/${teamKey}/roster`;
  return `team/${teamKey}/roster;week=${week}`;
}

/** Yahoo epoch seconds -> YYYY-MM-DD. UTC so output does not vary by machine. */
export function formatTimestamp(ts) {
  const n = Number(ts);
  if (!ts || !Number.isFinite(n)) return '-';
  return new Date(n * 1000).toISOString().slice(0, 10);
}

/** Build a `transactions` resource path. Always paginated. */
export function buildTransactionsResource(leagueKey, flags = {}) {
  const parts = [];
  if (flags.type && flags.type !== true) parts.push(`types=${flags.type}`);
  if (flags.team && flags.team !== true) parts.push(`team_key=${flags.team}`);
  parts.push(`count=${flags.count ?? 25}`);
  return `league/${leagueKey}/transactions;${parts.join(';')}`;
}

/**
 * One row per player movement: an add/drop transaction moves two players and
 * should read as two lines, not one.
 */
export function transactionRows(transactions) {
  const rows = [];
  for (const t of transactions ?? []) {
    const players = t.players ?? [];
    for (const p of Array.isArray(players) ? players : [players]) {
      // transaction_data is an object in every shape observed, but Yahoo
      // sometimes wraps single children in arrays -- tolerate both.
      const raw = p.transaction_data;
      const d = (Array.isArray(raw) ? raw[0] : raw) ?? {};
      rows.push({
        date: formatTimestamp(t.timestamp),
        type: t.type,
        player: p.name?.full,
        move: `${d.source_team_name ?? d.source_type ?? '?'} -> ${d.destination_team_name ?? d.destination_type ?? '?'}`,
      });
    }
  }
  return rows;
}

/**
 * Parse `--with=adp,injury` into validated capability names.
 *
 * Validated against IMPLEMENTED_CAPABILITIES, not the source registry's
 * `allCapabilities()`: the registry advertises 'identity' and 'depth' that
 * enrichment does not attach, so validating against it accepted
 * `--with=depth` and then produced output identical to no flag at all.
 */
export function parseWith(value) {
  if (!value || value === true) return [];
  const known = new Set(IMPLEMENTED_CAPABILITIES);
  const caps = String(value).split(',').map((s) => s.trim()).filter(Boolean);
  for (const c of caps) {
    if (!known.has(c)) {
      throw new UsageError(
        `Unknown capability "${c}". Available: ${[...known].join(', ')}`);
    }
  }
  return caps;
}

/**
 * The extra columns `--with` contributes, and the row fields that feed them.
 * Shared by `players` and `roster`: the two rendered the same enrichment from
 * two copies of this logic, so a fix (or a new capability) had to be made
 * twice, and deleting one copy left the whole suite green.
 */
export function enrichmentColumns(caps) {
  const columns = [];
  if (caps.includes('adp')) columns.push({ key: 'adp', label: 'ADP' });
  if (caps.includes('injury')) columns.push({ key: 'injury', label: 'INJ' });
  return columns;
}

const enrichmentFields = (p) => ({ adp: p.adp, injury: p.injury });

/**
 * Match-rate visibility per the spec: silent degradation (a source that
 * stopped matching) must show up somewhere a human will see it. Never under
 * --json -- it would corrupt machine-readable output.
 *
 * The adp line carries the feed variant ("[Half-PPR, 10-team]") whenever the
 * cache knows it. ADP means nothing without its scoring format: a Non-PPR
 * number read as Half-PPR is wrong, not approximate, and it gets drafted on.
 */
export function writeEnrichmentFooter(out, { stats, adpVariant } = {}, caps = [], flags = {}) {
  if (!stats || flags?.json) return;
  if (caps.includes('adp')) {
    out.write(`adp${adpVariant ? ` [${adpVariant}]` : ''}: ${stats.adp.matched} matched, ` +
      `${stats.adp.ambiguous} ambiguous, ${stats.adp.absent} absent (of ${stats.total})\n`);
  }
  if (caps.includes('injury')) {
    out.write(`injury: ${stats.injury.matched} matched (of ${stats.total})\n`);
  }
}

/** Whole hours since a cache entry was written, for staleness reporting. */
const ageHours = (fetchedAt, now = Date.now()) =>
  Math.max(0, Math.round((now - fetchedAt) / (60 * 60 * 1000)));

/**
 * ADP fetch options for `sync`, validated before any network call.
 *
 * FFC publishes a different dataset per scoring format and league size, and
 * `sync` passed neither -- so the module defaults (12-team Non-PPR) were the
 * only reachable board. These flags make the others reachable without
 * hardcoding any one league's settings into the source module.
 */
export function syncFetchOptions(flags = {}) {
  const options = {};
  if (flags.scoring !== undefined) {
    if (flags.scoring === true) {
      throw new UsageError(`--scoring needs a value. Available: ${SCORING_FORMATS.join(', ')}`);
    }
    const scoring = String(flags.scoring).toLowerCase();
    if (!SCORING_FORMATS.includes(scoring)) {
      throw new UsageError(
        `Unknown scoring format "${flags.scoring}". Available: ${SCORING_FORMATS.join(', ')}`);
    }
    options.scoring = scoring;
  }
  if (flags.teams !== undefined) {
    if (flags.teams === true) throw new UsageError('--teams needs a value, e.g. --teams=10');
    const teams = Number(flags.teams);
    if (!Number.isInteger(teams) || teams < 2 || teams > 32) {
      throw new UsageError(`--teams must be a whole number between 2 and 32 (got "${flags.teams}")`);
    }
    options.teams = teams;
  }
  return options;
}

// A source's TTL lives on its own meta object; looking it up here instead of
// hardcoding it at each call site means the two can never drift apart. A
// lookup miss (unknown source name) yields undefined, which readCache's
// isStale treats as non-finite -> stale, so it fails safe rather than
// freezing the cache.
const ttlFor = (name) => SOURCES.find((s) => s.meta.name === name)?.meta.ttlHours;

/**
 * Enrich Yahoo players with cached external data, additively: a cold or
 * partial cache must never fail a command that would have worked without
 * `--with`. Returns the (possibly unchanged) players plus enrichment stats,
 * or null stats when no enrichment happened (no capabilities requested, or
 * the cache was not ready).
 */
async function tryEnrich(players, caps, { cacheDir, err }) {
  const unenriched = { players, stats: null, adpVariant: null };
  if (caps.length === 0) return unenriched;

  // Only the sources that actually PROVIDE a requested capability are
  // required. Demanding both caches meant `--with=injury` failed whenever the
  // FFC cache was missing, even though a fresh Sleeper cache held exactly
  // what was asked for. Every present cache is still read -- the crosswalk
  // sharpens an adp-only run -- but a source nothing asked for may be absent.
  const required = [...new Set(
    caps.flatMap((c) => sourcesProviding(c).map((s) => s.meta.name)))];
  const cached = new Map();
  for (const s of SOURCES) {
    cached.set(s.meta.name,
      await readCache(s.meta.name, { dir: cacheDir, ttlHours: ttlFor(s.meta.name) }));
  }

  const missing = required.filter((name) => !cached.get(name));
  if (missing.length > 0) {
    err.write(`Enrichment cache is empty for: ${missing.join(', ')}. Run: tt sync\n`);
    return unenriched;
  }

  // Spec §5: an expired cache still enriches -- stale data beats none -- but
  // it must never do so silently. `readCache` has always computed `.stale`
  // and nothing outside `sync` read it, so a two-day-old ADP was served as
  // though it were today's.
  const stale = required
    .filter((name) => cached.get(name).stale)
    .map((name) => `${name} ${ageHours(cached.get(name).fetchedAt)}h`);
  if (stale.length > 0) {
    err.write(`Enrichment cache is stale (${stale.join(', ')} old). Run: tt sync\n`);
  }

  const dataOf = (name) => cached.get(name)?.data;
  try {
    const ffcData = dataOf('ffc');
    const sleeperData = dataOf('sleeper');
    const { players: enriched, stats } = enrichPlayers(players, {
      crosswalk: buildCrosswalk(sleeperData === undefined ? [] : recordsOf(sleeperData)),
      adpIndex: buildAdpIndex(ffcData === undefined ? [] : recordsOf(ffcData)),
      capabilities: caps,
    });
    return { players: enriched, stats, adpVariant: feedVariantLabel(metaOf(ffcData)) };
  } catch (e) {
    // Cache entries carry no schema version and outlive an upgrade: a stale-
    // shape payload (e.g. `data` no longer an array) throws deep inside
    // buildCrosswalk/buildAdpIndex. That is exactly the kind of failure
    // enrichment must never propagate -- degrade to the unenriched table
    // instead of taking down a command that worked fine before --with.
    err.write(`Enrichment cache is corrupt (${e.message}). Run: tt sync --force\n`);
    return unenriched;
  }
}

/**
 * Parse an integer-valued flag (`--teams=4`), or `undefined` if the flag
 * was never given. A BARE flag (`--teams` with no `=value`, which
 * `parseArgs` reads as the boolean `true`) and a non-integer value are both
 * usage errors, not silently coerced to `NaN` or `1` -- a typo in a numeric
 * draft-engine flag (wrong team count, wrong slot) produces a wrong board
 * or a wrong pick, not a crash Python would have to explain instead.
 */
export function intFlag(flags, name) {
  const v = flags?.[name];
  if (v === undefined) return undefined;
  if (v === true) throw new UsageError(`--${name} needs a value, e.g. --${name}=4`);
  const n = Number(v);
  if (!Number.isInteger(n)) {
    throw new UsageError(`--${name} must be a whole number (got "${v}")`);
  }
  return n;
}

/** Two-decimal rounding for display -- the analytics engine's Monte Carlo
 * floats carry far more precision than a draft board needs to read. Passes
 * through anything that isn't a number (null, undefined, a string) so a
 * missing value renders as the table's own "-" rather than "NaN". */
export function round2(n) {
  return typeof n === 'number' ? Math.round(n * 100) / 100 : n;
}

/** A probability as a whole-number percentage, or '-' when absent. */
export function pct(n) {
  return typeof n === 'number' ? `${Math.round(n * 100)}%` : '-';
}

/**
 * `--roster=PATH` for `draft pick`: the user's own JSON file tracking what
 * they've drafted so far THIS draft night. Node has no live source for this
 * (unlike `lineup`/`playoff`'s season roster, an in-progress live draft
 * isn't something the Yahoo roster endpoint tracks) -- see src/analytics.js
 * and analytics/src/tt/cli.py's module docstrings for the full reasoning.
 * A bare `--roster` (no path) and an unreadable/malformed file are both
 * usage errors: a typo'd path must never silently draft as though nothing
 * had been picked yet.
 */
async function readRosterFile(flagValue) {
  if (flagValue === undefined) return [];
  if (flagValue === true) throw new UsageError('--roster needs a file path, e.g. --roster=my-draft.json');
  let raw;
  try {
    raw = await readFile(flagValue, 'utf8');
  } catch (e) {
    throw new UsageError(`Could not read --roster file "${flagValue}": ${e.message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new UsageError(`--roster file "${flagValue}" is not valid JSON: ${e.message}`);
  }
  if (!Array.isArray(parsed)) {
    throw new UsageError(`--roster file "${flagValue}" must contain a JSON array of drafted players`);
  }
  return parsed;
}

/**
 * The nflverse roster export `analytics/scripts/export_nflverse_roster.py`
 * writes (gitignored; the script must be run to produce it). Used ONLY as
 * a name+position+team fallback index -- see `resolveRosterIdentity` below.
 */
export const DEFAULT_NFLVERSE_ROSTER_PATH = path.join(DEFAULT_ANALYTICS_DIR, 'data', 'nflverse_players.json');

// Memoised per path within one process: `playoff` resolves two rosters
// (mine, theirs) in the same run and would otherwise read and re-index this
// ~1,850-row file twice for no reason. The underlying file is static
// per-invocation (nothing in this process writes it), so caching it for the
// lifetime of the process is safe.
const _nflverseIndexCache = new Map();

async function loadNflverseIndex(rosterPath) {
  if (_nflverseIndexCache.has(rosterPath)) return _nflverseIndexCache.get(rosterPath);
  let index;
  try {
    const records = JSON.parse(await readFile(rosterPath, 'utf8'));
    index = buildAdpIndex(records);
  } catch {
    // Missing/corrupt file (e.g. export_nflverse_roster.py hasn't been run
    // yet) degrades to "no fallback available" -- resolveRosterIdentity
    // still works with just the Sleeper gsis_id pass, exactly as it did
    // before this fallback existed. Never a hard failure over an optional
    // enrichment source, same principle as tryEnrich's cache handling above.
    index = buildAdpIndex([]);
  }
  _nflverseIndexCache.set(rosterPath, index);
  return index;
}

/**
 * Resolve a Yahoo roster to the nflverse `player_id` the analytics engine
 * projects against.
 *
 * PASS 1: the tested crosswalk `--with=adp,injury` already uses
 * (`buildCrosswalk` + `lookupByYahooKey`, joined through the cached Sleeper
 * payload's `gsis_id` -- see src/identity.js). Deliberately NOT a new join.
 *
 * PASS 2 (fallback): Sleeper's own `gsis_id` field has REAL, MEASURED gaps
 * for exactly the players a real roster cares about most -- checked live
 * against this project's own cached Sleeper payload and a real Yahoo
 * league's player pool 2026-09-01: Sleeper-gsis_id alone resolved only
 * 19% (38/200), because star players like Ja'Marr Chase, Bijan Robinson,
 * Puka Nacua and Jahmyr Gibbs carry `gsis_id: null` in Sleeper's feed (the
 * exact gap `analytics/scripts/build_ffc_crosswalk.mjs`'s own module
 * docstring documents for the ADP crosswalk). This is the SAME gap, hit a
 * second time on a different data path. Rather than inventing a new
 * matcher, pass 2 reuses the IDENTICAL tested `buildAdpIndex`/`matchAdp`
 * (src/identity.js) against nflverse's own roster export
 * (`DEFAULT_NFLVERSE_ROSTER_PATH`) -- the exact second-pass technique
 * `build_ffc_crosswalk.mjs` already uses for the ADP crosswalk, applied
 * here to Yahoo names instead of FFC names. Combined, the two passes
 * resolved 170/200 (85%) of that same real player pool -- the residue is
 * almost entirely 2025-rookie-class players not yet in the historical
 * nflverse export, and team defenses (which never match by name at all,
 * by `buildAdpIndex`'s own design -- see that module's docstring -- and
 * which this engine does not project regardless, see `projections.
 * PROJECTABLE_POSITIONS`).
 *
 * A player neither pass can resolve gets `player_id: null` and is added to
 * `unresolved` -- carried through to the analytics engine rather than
 * dropped, so `lineup.optimal_lineup`'s own NaN-points handling benches
 * them visibly instead of a roster spot silently vanishing.
 */
async function resolveRosterIdentity(players, { cacheDir, err, nflverseRosterPath = DEFAULT_NFLVERSE_ROSTER_PATH }) {
  const cached = await readCache('sleeper', { dir: cacheDir, ttlHours: ttlFor('sleeper') });
  if (!cached) err.write('Identity cache is empty for: sleeper. Run: tt sync\n');
  const crosswalk = buildCrosswalk(cached ? recordsOf(cached.data) : []);
  const nflverseIndex = await loadNflverseIndex(nflverseRosterPath);

  const roster = [];
  const unresolved = [];
  for (const p of players ?? []) {
    const xw = lookupByYahooKey(crosswalk, p.player_key);
    let playerId = xw?.gsisId ?? null;
    const name = p.name?.full ?? '(unknown player)';
    if (!playerId) {
      const fallback = matchAdp(nflverseIndex, {
        name, position: p.display_position, team: p.editorial_team_abbr ?? xw?.team,
      });
      if (fallback?.playerId) playerId = fallback.playerId;
    }
    if (!playerId) unresolved.push(name);
    roster.push({ player_id: playerId, name, position: p.display_position });
  }
  return { roster, total: (players ?? []).length, matched: (players ?? []).length - unresolved.length, unresolved };
}

/** Match-rate footer for lineup/playoff, mirroring writeEnrichmentFooter's
 * "never under --json" rule. Also names every unresolved player -- spec:
 * "name any roster player that could not be resolved rather than silently
 * dropping them." */
function writeIdentityFooter(out, identity, flags) {
  if (flags?.json) return;
  out.write(`identity: ${identity.matched} matched, ${identity.unresolved.length} unresolved (of ${identity.total})\n`);
  if (identity.unresolved.length > 0) out.write(`Unresolved: ${identity.unresolved.join(', ')}\n`);
}

/** Names players the analytics engine itself could not project (a resolved
 * id with no data, e.g. K/DEF, or one Node passed through unresolved) --
 * distinct from `writeIdentityFooter`'s crosswalk-specific reporting, since
 * a resolved player can still have no projection. */
function writeUnprojectedFooter(out, names, flags, label) {
  if (flags?.json || !names || names.length === 0) return;
  out.write(`${label}: ${names.join(', ')}\n`);
}

/** Roster entries that are not lineup slots. */
const NON_STARTING_SLOTS = new Set(['BN', 'IR']);

/**
 * Derive the parameters a draft engine needs from a league's own settings.
 *
 * Reading these rather than hardcoding them is what keeps the engine usable in
 * any league -- and replacement level, which every VOR number depends on, is a
 * direct function of `rosterSlots` and `numTeams`.
 */
export function leagueConfig(league) {
  const s = league.settings;
  const slots = {};
  for (const p of s.roster_positions ?? []) {
    if (p.is_starting_position === 1 && !NON_STARTING_SLOTS.has(p.position)) {
      slots[p.position] = Number(p.count);
    }
  }
  // stat_modifiers carries values keyed by stat_id; stat_categories carries
  // the human name and group. Neither is useful without the other -- and the
  // join key MUST be stat_id, not display_name: Yahoo reuses display names
  // across unrelated stats (e.g. stat_id 6 "Interceptions"/passing, an
  // individual QB's picks thrown, modifier -1; and stat_id 33
  // "Interception"/def_turnovers, a defense's takeaways, modifier +2 -- both
  // display as "Int"). A name-keyed dict can only hold one of a colliding
  // pair and silently drops or overwrites the other; stat_id is the field
  // Yahoo itself treats as unique, so keying the export by it keeps both.
  // `name`/`group` ride along for human readability only, never as keys.
  // Every modifier with a matching category is emitted -- filtering to a
  // subset (e.g. offense-only) is a downstream concern, not this export's.
  const categories = new Map(
    (s.stat_categories?.stats ?? []).map((c) => [String(c.stat_id), c]));
  const scoring = [];
  for (const m of s.stat_modifiers?.stats ?? []) {
    const category = categories.get(String(m.stat_id));
    if (!category) continue; // a modifier with no matching category can't be labelled
    scoring.push({
      statId: Number(m.stat_id),
      name: category.display_name,
      group: category.group,
      value: Number(m.value),
    });
  }
  return {
    leagueKey: league.league_key,
    name: league.name,
    numTeams: Number(league.num_teams),
    maxTeams: Number(league.max_teams),
    draftStatus: league.draft_status,
    rosterSlots: slots,
    scoring,
    // Playoff structure -- read here, never hardcoded, so `tt season` can
    // forward THIS league's real numbers to the analytics engine instead of
    // assuming any particular week/field size (the league can grow from 4
    // teams up to `maxTeams`). `end_week` lives on `league` itself;
    // everything else is under `league.settings` -- see the live-verified
    // shape in test/fixtures/league-settings.json.
    playoffStartWeek: Number(s.playoff_start_week),
    endWeek: Number(league.end_week),
    numPlayoffTeams: Number(s.num_playoff_teams),
    usesPlayoffReseeding: Number(s.uses_playoff_reseeding) === 1,
  };
}

/** The team the logged-in user owns in a league. */
async function myTeamKey(client, leagueKey) {
  const { league } = await client.get(`league/${leagueKey}/teams`);
  const mine = league.teams.find((t) => t.is_owned_by_current_login === 1);
  if (!mine) throw new YahooApiError('Could not find your team in that league');
  return mine.team_key;
}

/** Every league the logged-in user plays in. */
async function myLeagues(client) {
  const data = await client.get(LEAGUES_RESOURCE);
  return data.users?.[0]?.games?.[0]?.leagues ?? [];
}

/** Use the supplied key, else fall back to the user's first league. */
async function resolveLeagueKey(client, given) {
  if (given) return given;
  const leagues = await myLeagues(client);
  if (leagues.length === 0) throw new YahooApiError('No leagues found for this account');
  return leagues[0].league_key;
}

export async function runCommand(
  { command, args, flags },
  {
    client, out = process.stdout, err = process.stderr, interactive = false,
    // Injectable so tests can point the enrichment cache at a scratch
    // directory and stub the network, without touching the real
    // ~/.tokentouchdowns/cache or making live requests.
    cacheDir = undefined, fetch: fetchImpl = globalThis.fetch,
    // Injectable the same way `client` is: production gets a real client
    // that spawns analytics/.venv/bin/python; tests always supply a fake
    // `{ run() {...} }` so no test ever shells out to a real Python process
    // (see src/analytics.js and test/cli.test.js's fakeAnalytics).
    analytics = createAnalyticsClient(),
    // Injectable so tests point identity resolution's nflverse-name
    // fallback at a small fixture instead of the real (large, gitignored)
    // analytics/data/nflverse_players.json -- see resolveRosterIdentity.
    nflverseRosterPath = DEFAULT_NFLVERSE_ROSTER_PATH,
  } = {},
) {
  const emit = (rows, columns) => {
    out.write(flags?.json ? `${JSON.stringify(rows, null, 2)}\n` : `${formatTable(rows, columns)}\n`);
  };

  try {
    switch (command) {
      case 'help':
        out.write(`${USAGE}\n`);
        return 0;

      case 'leagues': {
        const leagues = await myLeagues(client);
        emit(leagues, [
          { key: 'league_key', label: 'KEY' },
          { key: 'name', label: 'NAME' },
          { key: 'num_teams', label: 'TEAMS' },
          { key: 'draft_status', label: 'DRAFT' },
        ]);
        return 0;
      }

      case 'teams': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(`league/${key}/teams`);
        const rows = league.teams.map((t) => ({
          ...t, mine: t.is_owned_by_current_login === 1 ? '*' : '',
        }));
        emit(rows, [
          { key: 'mine', label: '' },
          { key: 'team_key', label: 'KEY' },
          { key: 'name', label: 'NAME' },
        ]);
        return 0;
      }

      case 'standings': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(`league/${key}/standings`);
        const rows = league.standings.teams.map((t) => ({
          name: t.name,
          rank: t.team_standings?.rank,
          wins: t.team_standings?.outcome_totals?.wins,
          losses: t.team_standings?.outcome_totals?.losses,
        }));
        emit(rows, [
          { key: 'rank', label: 'RANK' },
          { key: 'name', label: 'NAME' },
          { key: 'wins', label: 'W' },
          { key: 'losses', label: 'L' },
        ]);
        return 0;
      }

      case 'roster': {
        // Parsed before any Yahoo call: a bad --with is a local mistake and
        // should fail fast rather than after spending a network round trip.
        const caps = parseWith(flags.with);
        let teamKey = args[0];
        if (!teamKey) {
          const leagueKey = await resolveLeagueKey(client, undefined);
          const { league } = await client.get(`league/${leagueKey}/teams`);
          teamKey = league.teams.find((t) => t.is_owned_by_current_login === 1)?.team_key;
          if (!teamKey) throw new YahooApiError('Could not find your team in that league');
        }
        const { team } = await client.get(buildRosterResource(teamKey, flags));
        const enrichment = await tryEnrich(team.roster?.players ?? [], caps, { cacheDir, err });

        if (!flags?.json) out.write(`Week ${team.roster?.week ?? '?'}\n`);
        const rows = enrichment.players.map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          status: p.status ?? '',
          ...enrichmentFields(p),
        }));
        emit(rows, [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'status', label: 'STATUS' },
          ...enrichmentColumns(caps),
        ]);
        writeEnrichmentFooter(out, enrichment, caps, flags);
        return 0;
      }

      case 'players': {
        const caps = parseWith(flags.with);
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(buildPlayersResource(key, flags));
        const enrichment = await tryEnrich(league.players ?? [], caps, { cacheDir, err });

        const rows = enrichment.players.map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          bye: p.bye_weeks?.week,
          status: p.status ?? '',
          ...enrichmentFields(p),
        }));
        emit(rows, [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'bye', label: 'BYE' },
          { key: 'status', label: 'STATUS' },
          ...enrichmentColumns(caps),
        ]);
        writeEnrichmentFooter(out, enrichment, caps, flags);
        return 0;
      }

      case 'sources': {
        // AGE/STALE/VARIANT are read from the cache, not from meta. TTL alone
        // is a constant -- the README promised "what's registered and how
        // stale" while this command never opened a cache file, so a source
        // could be two days cold and look identical to one synced a minute
        // ago. VARIANT names WHICH feed the cached numbers came from.
        const rows = [];
        for (const s of SOURCES) {
          const cached = await readCache(s.meta.name,
            { dir: cacheDir, ttlHours: s.meta.ttlHours });
          rows.push({
            name: s.meta.name,
            provides: s.meta.provides.join(','),
            join: s.meta.joinKey,
            ttl: `${s.meta.ttlHours}h`,
            age: cached ? `${ageHours(cached.fetchedAt)}h` : '-',
            stale: cached ? (cached.stale ? 'yes' : 'no') : 'never',
            variant: feedVariantLabel(metaOf(cached?.data)) ?? '-',
            documented: s.meta.documented ? 'yes' : 'NO',
          });
        }
        emit(rows, [
          { key: 'name', label: 'SOURCE' },
          { key: 'provides', label: 'PROVIDES' },
          { key: 'join', label: 'JOIN' },
          { key: 'ttl', label: 'TTL' },
          { key: 'age', label: 'AGE' },
          { key: 'stale', label: 'STALE' },
          { key: 'variant', label: 'VARIANT' },
          { key: 'documented', label: 'DOCUMENTED' },
        ]);
        return 0;
      }

      case 'league': {
        const sub = args[0];
        if (sub !== 'export') {
          throw new UsageError(
            `Unknown league subcommand "${sub ?? ''}". Usage: league export [league_key] [--out=PATH]`);
        }
        // Deliberately `myLeagues` directly, not `resolveLeagueKey`: the
        // latter throws when none are found, so an account with no
        // discoverable leagues still reaches the settings request below and
        // surfaces Yahoo's own error for a missing key, instead of a
        // resolution step masking it with a less specific one.
        const key = flags.league ?? args[1] ?? (await myLeagues(client))[0]?.league_key;
        const { league } = await client.get(`league/${key}/settings`);
        const cfg = leagueConfig(league);
        const json = `${JSON.stringify(cfg, null, 2)}\n`;
        if (flags.out && flags.out !== true) {
          await writeFile(flags.out, json);
          out.write(`Wrote ${flags.out}\n`);
        } else {
          out.write(json);
        }
        return 0;
      }

      case 'sync': {
        // Record counts only -- sync has no Yahoo player list to compute a
        // match rate against, and any denominator it invented here would be
        // meaningless. Match-rate visibility belongs to players/roster's
        // --with footer, which has real Yahoo rows to classify.
        const only = flags.source;
        const named = only !== undefined && only !== true;
        if (named && !SOURCES.some((s) => s.meta.name === only)) {
          // Previously a silent no-op exiting 0: `tt sync --source=sleper`
          // reported nothing, synced nothing, and looked like success.
          throw new UsageError(
            `Unknown source "${only}". Available: ${SOURCES.map((s) => s.meta.name).join(', ')}`);
        }
        const fetchOptions = syncFetchOptions(flags);
        // A payload we cannot parse is worth replacing, so an unreadable old
        // cache counts as zero rather than blocking the overwrite guard below.
        const cachedCount = (data) => { try { return recordsOf(data).length; } catch { return 0; } };
        let failed = 0;

        for (const s of SOURCES) {
          if (named && s.meta.name !== only) continue;
          const cached = await readCache(s.meta.name, { dir: cacheDir, ttlHours: ttlFor(s.meta.name) });
          if (cached && !cached.stale && !flags.force) {
            out.write(`${s.meta.name}: fresh (cached)\n`);
            continue;
          }
          try {
            const payload = s.normalize(await s.fetchRaw({ fetch: fetchImpl, ...fetchOptions }));
            const count = recordsOf(payload).length;
            const previous = cached ? cachedCount(cached.data) : 0;
            // A source that renames a field normalizes to zero records. Left
            // unguarded, sync would overwrite a working 6,750-record
            // crosswalk with [] and print "0 records", after which every
            // --with run is blank with nothing to explain why.
            if (count === 0 && previous > 0) {
              err.write(`${s.meta.name}: REFUSED to replace ${previous} cached records with 0 — ` +
                `the source's response shape has probably changed. Keeping the existing cache.\n`);
              failed += 1;
              continue;
            }
            await writeCache(s.meta.name, payload, { dir: cacheDir });
            const variant = feedVariantLabel(metaOf(payload));
            out.write(`${s.meta.name}: ${count} records${variant ? ` [${variant}]` : ''}\n`);
          } catch (e) {
            // Per source, not per run: Sleeper is first in SOURCES, so an
            // unhandled Sleeper failure used to stop FFC from syncing at all
            // and surfaced as a raw stack trace out of bin/tt.js.
            err.write(`${s.meta.name}: FAILED (${e.message})\n`);
            failed += 1;
          }
        }
        return failed > 0 ? 1 : 0;
      }

      case 'transactions': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(buildTransactionsResource(key, flags));
        emit(transactionRows(league.transactions), [
          { key: 'date', label: 'DATE' },
          { key: 'type', label: 'TYPE' },
          { key: 'player', label: 'PLAYER' },
          { key: 'move', label: 'MOVE' },
        ]);
        return 0;
      }

      case 'matchup': {
        const week = args[0];
        const leagueKey = await resolveLeagueKey(client, flags.league);
        const teamKey = flags.team ?? (await myTeamKey(client, leagueKey));
        const { team } = await client.get(`team/${teamKey}/matchups`);
        const matchups = team.matchups ?? [];
        const found = week
          ? matchups.find((m) => String(m.week) === String(week))
          // "Current" = the first week not already played.
          : matchups.find((m) => m.status !== 'postevent') ?? matchups[0];
        if (!found) {
          err.write(`No matchup found for week ${week ?? '(current)'}\n`);
          return 1;
        }
        if (flags?.json) {
          out.write(`${JSON.stringify(found, null, 2)}\n`);
          return 0;
        }
        out.write(`Week ${found.week}  (${found.status})\n`);
        const rows = found.teams.map((t) => ({
          mine: t.is_owned_by_current_login === 1 ? '*' : '',
          name: t.name,
          points: t.team_points?.total,
          projected: t.team_projected_points?.total,
        }));
        out.write(`${formatTable(rows, [
          { key: 'mine', label: '' },
          { key: 'name', label: 'TEAM' },
          { key: 'points', label: 'PTS' },
          { key: 'projected', label: 'PROJ' },
        ])}\n`);
        return 0;
      }

      case 'draft': {
        const sub = args[0];
        if (sub === 'board') {
          const payload = await analytics.run('board', {
            flags: {
              teams: intFlag(flags, 'teams'),
              slot: intFlag(flags, 'slot'),
              count: intFlag(flags, 'count'),
              conditional: flags.conditional === true,
            },
          });
          if (flags?.json) {
            out.write(`${JSON.stringify(payload, null, 2)}\n`);
            return 0;
          }
          const hasSurvival = payload.slot != null;
          const rows = (payload.players ?? []).map((p) => ({
            name: p.name, pos: p.position, proj: round2(p.proj_points),
            vor: round2(p.vor), tier: p.tier,
            adp: p.adp == null ? '-' : round2(p.adp),
            ...(hasSurvival ? { gone: pct(p.p_gone_by_next) } : {}),
          }));
          out.write(`${formatTable(rows, [
            { key: 'name', label: 'PLAYER' }, { key: 'pos', label: 'POS' },
            { key: 'proj', label: 'PROJ' }, { key: 'vor', label: 'VOR' },
            { key: 'tier', label: 'TIER' }, { key: 'adp', label: 'ADP' },
            ...(hasSurvival ? [{ key: 'gone', label: 'P(GONE)' }] : []),
          ])}\n`);
          const slotNote = payload.slot ? `, slot ${payload.slot}` : '';
          const adpNote = payload.adp_source ? `adp: ${payload.adp_source}` : 'adp: none (VOR-only board)';
          out.write(`Season ${payload.season}, ${payload.teams} teams${slotNote} | ${adpNote}\n`);
          return 0;
        }
        if (sub === 'pick') {
          const roster = await readRosterFile(flags.roster);
          const payload = await analytics.run('pick', {
            flags: {
              teams: intFlag(flags, 'teams'),
              slot: intFlag(flags, 'slot'),
              round: intFlag(flags, 'round'),
              rounds: intFlag(flags, 'rounds'),
              n: intFlag(flags, 'n'),
              conditional: flags.conditional === true,
            },
            stdin: { roster },
          });
          if (flags?.json) {
            out.write(`${JSON.stringify(payload, null, 2)}\n`);
            return 0;
          }
          const rows = (payload.recommendations ?? []).map((p) => ({
            name: p.name, pos: p.position, vor: round2(p.vor),
            gone: pct(p.p_gone_by_next), loss: round2(p.expected_loss),
          }));
          out.write(`${formatTable(rows, [
            { key: 'name', label: 'PLAYER' }, { key: 'pos', label: 'POS' },
            { key: 'vor', label: 'VOR' }, { key: 'gone', label: 'P(GONE)' },
            { key: 'loss', label: 'EXP LOSS' },
          ])}\n`);
          out.write(`Pick ${payload.pick} (your next: ${payload.next_pick})\n`);
          return 0;
        }
        throw new UsageError(
          `Unknown draft subcommand "${sub ?? ''}". Usage: ` +
          'draft board [--teams N] [--slot K] | draft pick [--roster PATH] --slot K',
        );
      }

      case 'mock': {
        const payload = await analytics.run('mock', {
          flags: {
            teams: intFlag(flags, 'teams'),
            slot: intFlag(flags, 'slot'),
            trials: intFlag(flags, 'trials'),
            rounds: intFlag(flags, 'rounds'),
            strategy: flags.strategy === true
              ? (() => { throw new UsageError('--strategy needs a value, e.g. --strategy=adp,vor'); })()
              : flags.strategy,
          },
        });
        if (flags?.json) {
          out.write(`${JSON.stringify(payload, null, 2)}\n`);
          return 0;
        }
        const rows = (payload.strategies ?? []).map((s) => ({
          strategy: s.strategy, trials: s.trials, mean: round2(s.mean_score),
          std: round2(s.std_score), ci95: `[${round2(s.ci95_low)}, ${round2(s.ci95_high)}]`,
        }));
        out.write(`${formatTable(rows, [
          { key: 'strategy', label: 'STRATEGY' }, { key: 'trials', label: 'TRIALS' },
          { key: 'mean', label: 'MEAN' }, { key: 'std', label: 'STD' }, { key: 'ci95', label: 'CI95' },
        ])}\n`);
        if (payload.note) out.write(`${payload.note}\n`);
        return 0;
      }

      case 'lineup': {
        const leagueKey = await resolveLeagueKey(client, flags.league);
        const teamKey = flags.team && flags.team !== true ? flags.team : await myTeamKey(client, leagueKey);
        const { team } = await client.get(buildRosterResource(teamKey, flags));
        const identity = await resolveRosterIdentity(team.roster?.players ?? [], { cacheDir, err, nflverseRosterPath });
        const payload = await analytics.run('lineup', {
          flags: { week: intFlag(flags, 'week') },
          stdin: { roster: identity.roster },
        });
        if (flags?.json) {
          out.write(`${JSON.stringify(payload, null, 2)}\n`);
          return 0;
        }
        out.write(`Week ${payload.week ?? '(current)'}\n`);
        const rows = (payload.lineup ?? []).filter((r) => r.starter).map((r) => ({
          slot: r.slot, name: r.empty ? '(empty)' : (r.name ?? '-'),
          pos: r.position ?? '-',
          proj: r.empty || r.proj_points == null ? '-' : round2(r.proj_points),
        }));
        out.write(`${formatTable(rows, [
          { key: 'slot', label: 'SLOT' }, { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' }, { key: 'proj', label: 'PROJ' },
        ])}\n`);
        writeIdentityFooter(out, identity, flags);
        writeUnprojectedFooter(out, payload.unprojected_players, flags,
          'No projection available (e.g. K/DEF are not modelled)');
        return 0;
      }

      case 'playoff': {
        const week = intFlag(flags, 'week');
        const leagueKey = await resolveLeagueKey(client, flags.league);
        const teamKey = flags.team && flags.team !== true ? flags.team : await myTeamKey(client, leagueKey);
        const { team } = await client.get(`team/${teamKey}/matchups`);
        const matchups = team.matchups ?? [];
        const found = week
          ? matchups.find((m) => String(m.week) === String(week))
          : matchups.find((m) => m.status !== 'postevent') ?? matchups[0];
        if (!found) throw new UsageError(`No matchup found for week ${week ?? '(current)'}`);

        const teamsInMatchup = found.teams ?? [];
        let opponentTeam;
        if (flags.opponent && flags.opponent !== true) {
          const needle = String(flags.opponent).toLowerCase();
          opponentTeam = teamsInMatchup.find((t) =>
            t.team_key === flags.opponent || (t.name ?? '').toLowerCase().includes(needle));
          if (!opponentTeam) {
            throw new UsageError(
              `Could not find an opponent matching "${flags.opponent}" in week ${found.week}'s matchup`);
          }
        } else {
          opponentTeam = teamsInMatchup.find((t) => t.is_owned_by_current_login !== 1);
          if (!opponentTeam) throw new UsageError(`Could not find an opponent for week ${found.week}'s matchup`);
        }

        const [{ team: myTeam }, { team: oppTeam }] = await Promise.all([
          client.get(buildRosterResource(teamKey, { ...flags, week: found.week })),
          client.get(buildRosterResource(opponentTeam.team_key, { ...flags, week: found.week })),
        ]);
        const mine = await resolveRosterIdentity(myTeam.roster?.players ?? [], { cacheDir, err, nflverseRosterPath });
        const theirs = await resolveRosterIdentity(oppTeam.roster?.players ?? [], { cacheDir, err, nflverseRosterPath });

        const payload = await analytics.run('playoff', {
          flags: { week: found.week },
          stdin: { roster: mine.roster, opponent_roster: theirs.roster },
        });
        if (flags?.json) {
          out.write(`${JSON.stringify(payload, null, 2)}\n`);
          return 0;
        }
        out.write(`Week ${payload.week} vs ${opponentTeam.name}\n`);
        const rows = (payload.lineup ?? []).filter((r) => r.starter).map((r) => ({
          slot: r.slot, name: r.empty ? '(empty)' : (r.name ?? '-'),
          pos: r.position ?? '-',
          proj: r.empty || r.proj_points == null ? '-' : round2(r.proj_points),
        }));
        out.write(`${formatTable(rows, [
          { key: 'slot', label: 'SLOT' }, { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' }, { key: 'proj', label: 'PROJ' },
        ])}\n`);
        out.write(`Win probability: ${pct(payload.win_probability)} ` +
          `(expected-points lineup would be ${pct(payload.expected_points_lineup_win_probability)})\n`);
        writeIdentityFooter(out, mine, flags);
        writeUnprojectedFooter(out, payload.unprojected_players, flags,
          'No projection available (e.g. K/DEF are not modelled)');
        return 0;
      }

      case 'season': {
        const mockDraft = flags['mock-draft'] === true;
        const rostersFlag = flags.rosters;
        if (mockDraft && rostersFlag !== undefined) {
          throw new UsageError('--mock-draft and --rosters are mutually exclusive -- pass one or the other');
        }

        let stdinPayload = {};
        let teamNames = {};
        let sourceNote = null;
        let extraFlags = {};
        let identities = null;

        if (mockDraft) {
          sourceNote =
            'SOURCE: a SIMULATED DRAFT (mock.simulate_draft against this league\'s own player ' +
            'pool), not your live league -- run `tt season` again once your real draft is ' +
            'complete for an answer that means something.';
          extraFlags = { mockDraft: true, teams: intFlag(flags, 'teams') };
        } else if (rostersFlag !== undefined) {
          if (rostersFlag === true) {
            throw new UsageError('--rosters needs a file path, e.g. --rosters=my-rosters.json');
          }
          let raw;
          try {
            raw = await readFile(rostersFlag, 'utf8');
          } catch (e) {
            throw new UsageError(`Could not read --rosters file "${rostersFlag}": ${e.message}`);
          }
          let parsed;
          try {
            parsed = JSON.parse(raw);
          } catch (e) {
            throw new UsageError(`--rosters file "${rostersFlag}" is not valid JSON: ${e.message}`);
          }
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)
              || !parsed.rosters || typeof parsed.rosters !== 'object') {
            throw new UsageError(
              `--rosters file "${rostersFlag}" must be a JSON object shaped ` +
              '{"rosters": {"<team>": [{"player_id":...,"name":...,"position":...}, ...]}, ' +
              '"schedule": [{"week":1,"home_team":...,"away_team":...}, ...] (optional)}',
            );
          }
          stdinPayload = { rosters: parsed.rosters, schedule: parsed.schedule };
          teamNames = Object.fromEntries(Object.keys(parsed.rosters).map((k) => [k, k]));
          sourceNote =
            `SOURCE: rosters supplied from ${rostersFlag} -- not your live league unless you ` +
            'built this file from one.';
        } else {
          const leagueKey = await resolveLeagueKey(client, flags.league);
          const { league: settingsLeague } = await client.get(`league/${leagueKey}/settings`);
          const league = leagueConfig(settingsLeague);
          if (league.draftStatus === 'predraft') {
            throw new UsageError(
              'This league is predraft -- every roster is empty, so there is nothing to ' +
              'simulate.\nRun `tt season` again once your draft is complete, or exercise this ' +
              'command right now with:\n' +
              '  tt season --mock-draft          (a simulated draft against this league\'s own player pool)\n' +
              '  tt season --rosters=PATH         (your own hand-built hypothetical rosters)',
            );
          }
          extraFlags = {
            playoffStartWeek: league.playoffStartWeek,
            endWeek: league.endWeek,
            playoffTeams: league.numPlayoffTeams,
            reseed: league.usesPlayoffReseeding ? 1 : 0,
          };

          const { league: teamsLeague } = await client.get(`league/${leagueKey}/teams`);
          const teams = teamsLeague.teams ?? [];
          teamNames = Object.fromEntries(teams.map((t) => [t.team_key, t.name]));

          // Yahoo rejects a combined `teams;out=roster` request for a league
          // ("Invalid subresource roster requested") -- one call per team is
          // the only way in. Considerately few for this league (4); noted
          // here since it scales with num_teams up to max_teams (10).
          if (!flags?.json) {
            out.write(`Fetching ${teams.length} team rosters individually ` +
              '(Yahoo has no combined roster resource for a league)...\n');
          }
          const rosters = {};
          identities = {};
          let anyPlayers = false;
          for (const t of teams) {
            const { team } = await client.get(buildRosterResource(t.team_key, {}));
            const players = team.roster?.players ?? [];
            if (players.length > 0) anyPlayers = true;
            const identity = await resolveRosterIdentity(players, { cacheDir, err, nflverseRosterPath });
            identities[t.team_key] = identity;
            rosters[t.team_key] = identity.roster;
          }
          if (!anyPlayers) {
            throw new UsageError(
              'Every roster in this league is empty -- there is nothing to simulate.\n' +
              'Try:\n  tt season --mock-draft\n  tt season --rosters=PATH',
            );
          }

          if (!flags?.json) {
            out.write(`Fetching ${league.playoffStartWeek - 1} weeks of regular-season schedule...\n`);
          }
          const schedule = [];
          for (let week = 1; week < league.playoffStartWeek; week += 1) {
            // eslint-disable-next-line no-await-in-loop -- Yahoo has no bulk
            // scoreboard resource; each week is its own request.
            const { league: sb } = await client.get(`league/${leagueKey}/scoreboard;week=${week}`);
            for (const m of sb.scoreboard?.matchups ?? []) {
              const [home, away] = m.teams ?? [];
              if (home?.team_key && away?.team_key) {
                schedule.push({ week, home_team: home.team_key, away_team: away.team_key });
              }
            }
          }
          stdinPayload = { rosters, schedule };
        }

        const payload = await analytics.run('season', {
          flags: { n: intFlag(flags, 'n'), week: intFlag(flags, 'week'), ...extraFlags },
          stdin: stdinPayload,
        });

        if (flags?.json) {
          out.write(`${JSON.stringify(payload, null, 2)}\n`);
          return 0;
        }

        const sePct = typeof payload.monte_carlo_se === 'number'
          ? `±${(payload.monte_carlo_se * 100).toFixed(2)}%` : '±?';
        out.write(
          `Season ${payload.season} championship odds -- SIMULATION ` +
          `(n=${payload.n}, worst-case standard error ${sePct})\n`,
        );
        out.write(
          `Regular season weeks 1-${payload.playoff_start_week - 1}, playoffs weeks ` +
          `${payload.playoff_start_week}-${payload.end_week} (top ${payload.playoff_teams}` +
          `${payload.reseed ? ', reseeded each round' : ''})\n`,
        );
        if (sourceNote) out.write(`${sourceNote}\n`);

        const rows = (payload.teams ?? []).map((t) => ({
          team: teamNames[t.team] ?? t.team,
          title: pct(t.championship_prob),
          final: pct(t.p_final),
          wins: round2(t.expected_wins),
          seed: round2(t.mean_seed),
          seed1: pct(t.p_seed_1),
          ptswk: round2(t.exp_points_per_week),
          sdwk: round2(t.sd_per_week),
          empty: t.empty_slots,
        }));
        out.write(`${formatTable(rows, [
          { key: 'team', label: 'TEAM' }, { key: 'title', label: 'TITLE%' },
          { key: 'final', label: 'FINAL%' }, { key: 'wins', label: 'EXP W' },
          { key: 'seed', label: 'MEAN SEED' }, { key: 'seed1', label: 'P(SEED1)' },
          { key: 'ptswk', label: 'PTS/WK' }, { key: 'sdwk', label: 'SD/WK' },
          { key: 'empty', label: 'EMPTY' },
        ])}\n`);

        if (identities) {
          for (const [teamKey, identity] of Object.entries(identities)) {
            const label = teamNames[teamKey] ?? teamKey;
            out.write(`${label}: ${identity.matched} matched, ${identity.unresolved.length} ` +
              `unresolved (of ${identity.total})\n`);
            if (identity.unresolved.length > 0) {
              out.write(`  Unresolved: ${identity.unresolved.join(', ')}\n`);
            }
          }
        }
        for (const [teamKey, names] of Object.entries(payload.unprojected_players ?? {})) {
          if (!names || names.length === 0) continue;
          const label = teamNames[teamKey] ?? teamKey;
          out.write(`${label}: no projection available for ${names.join(', ')} ` +
            '(e.g. K/DEF are not modelled)\n');
        }
        return 0;
      }

      default:
        err.write(`Unknown command: ${command}\n\n${USAGE}\n`);
        return 1;
    }
  } catch (e) {
    if (e instanceof AnalyticsError) {
      err.write(`${e.message}\n`);
      return 4;
    }
    if (e instanceof SessionExpiredError) {
      // Design doc §9.1: only auto-launch a browser where a human can use it.
      err.write(
        interactive
          ? 'Session expired. Re-authenticating...\n'
          : 'Session expired or rejected by Yahoo.\nRun: tt login\n',
      );
      return 2;
    }
    if (e instanceof UsageError) {
      // Exit code 1 (this CLI's existing usage-error code), not 3: a typo in
      // --with is the user's mistake, not a Yahoo-side failure.
      err.write(`${e.message}\n`);
      return 1;
    }
    if (e instanceof YahooApiError) {
      err.write(`Yahoo API error: ${e.message}\n`);
      return 3;
    }
    throw e;
  }
}
