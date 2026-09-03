/**
 * The live draft room (docs/draft-room-design.md). A localhost server that
 * tracks picks as they happen, keeps a re-ranked board, and answers one
 * question repeatedly: who should I take right now?
 *
 * THE PERFORMANCE SPLIT THIS FILE EXISTS TO ENFORCE (design doc section 3).
 * Projections + VOR cost ~4-5s of Monte Carlo (analytics/src/tt/cli.py's
 * `board` subcommand, via src/analytics.js's `run`) and DO NOT CHANGE during
 * a draft, so that call happens exactly ONCE, in `createDraftRoom`, before
 * this module ever answers a request. Everything that DOES change --
 * survival (who's likely to still be there at my next pick), roster need,
 * and the ranked recommendation -- is recomputed via `analytics.runScript`
 * against src/draft-room-recompute.py, a standalone script that imports
 * `tt.survival`/`tt.draft` directly and re-uses the already-built board
 * instead of re-running `board`'s expensive step (see that script's own
 * docstring). Measured cost: ~0.5-0.7s per recompute, dominated by Python
 * process startup, not computation -- a 3s poll (or a manual mark/undo)
 * never re-triggers the ~5s path.
 *
 * STATE OWNERSHIP. All snake math, pick parsing, and draft-state
 * transitions are src/draft-state.js's job (already built, pure, tested) --
 * this module only ever calls it, never reimplements it. All fantasy logic
 * (VOR, survival, need-adjusted ranking) is the Python engine's job -- this
 * module maps its output into the view-model contract below and does
 * nothing else with it (design doc section 6: "the app is a VIEW over the
 * engine, never a second implementation of it").
 *
 * FAILURE MODES (design doc section 5) are handled here, not in the page:
 *   - a malformed draft_results entry -> loud `banner`, state untouched
 *   - a failing/rate-limited poll -> last good state kept, `lastPollOk`
 *     false, `staleSeconds` grows
 *   - an expired session -> banner naming the actual re-login command
 *   - the Python engine failing at startup -> `createDraftRoom` rejects,
 *     so the CLI command fails before the draft, never during it
 *   - the Python engine failing on a later recompute -> the last good board
 *     is kept, and the failure is surfaced only via `banner`
 *   - the poll loop itself NEVER throws (a `setInterval` callback that
 *     rejects is an unhandled rejection on draft day -- the worst outcome)
 *
 * /api/state SHAPE (what the page renders, nothing more):
 *   status         draftStatus, picksMade, currentPick/Round, myNextPick(+Round),
 *                  survivalPick (the pick every survival number is measured
 *                  to -- see `survivalPickFrom`), roundsRemaining, onTheClock,
 *                  isMyTurn, lastPollAt/Ok, staleSeconds, banner,
 *                  identity{matched,unresolved,total,rate}
 *   recommendations the engine's own ranking, top N, each with name/position/
 *                  proj/vor/tier/adp/pGone/expectedLoss + posRank (ADP rank
 *                  within position), adpDelta (currentPick - adp; positive =
 *                  he is falling), adpFall (that delta as a league-scaled
 *                  level), fillsNeed, games (expected games played, which is
 *                  what makes `proj` not a 17-game total), and Yahoo's own
 *                  team/bye/injuryStatus/injuryLabel/injuryNote (see
 *                  `enrichBoard`)
 *   board          the same decoration for every VOR-sorted row, plus
 *                  taken/takenBy, aboveCliff, and the three quiet visuals the
 *                  page draws from: vorShare (bar length, scaled within
 *                  position), tierBand/tierFirst (which rows are
 *                  interchangeable) and adpFall
 *   roster         {slots:[{slot,player}], bench:[...]}, each player carrying
 *                  his bye week and injury status
 *   guidance       THE REDESIGN (see `buildGuidance`): hero (= the engine's
 *                  own top recommendation, with four plain-language reasons),
 *                  cliffs (per-position count still above the measured cliff),
 *                  runway (unfilled starting slots vs rounds left -- the
 *                  largest measured effect in this league), urgency (the
 *                  MEASURED waiting cost for this round and team count, and
 *                  the level the page is allowed to render at), replacement
 *                  (where this board's own VOR crosses zero), glossary (one
 *                  plain-language definition per abbreviation the page
 *                  prints, written in THIS board's own numbers -- the fix for
 *                  "I don't know what the acronyms mean"), byes (weeks where
 *                  two of your starters are out at once) and league.
 *                  All of it built from src/draft-guidance.js, which
 *                  transcribes docs/positional-value.md and computes no
 *                  fantasy logic of its own.
 */
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  myPicks, nextPick, parseDraftResults, clockFromPending, createState, applyPicks,
  markTaken as markTakenState, undo as undoState,
} from './draft-state.js';
import {
  CLIFFS, positionAdpRanks, cliffStrip, waitingCost, urgencyLevel, basisTeamsFor,
  runwayState, replacementLine, heroReasons,
  glossary, byeConflicts, vorShares, tierBands, adpFallLevel,
} from './draft-guidance.js';
import { SessionExpiredError, YahooApiError } from './client.js';
import { buildCrosswalk, lookupByYahooKey, buildAdpIndex, matchAdp } from './identity.js';
import { recordsOf } from './sources/index.js';
import { readCache } from './cache.js';
import {
  myTeamKey as fetchMyTeamKey, resolveLeagueKey as fetchLeagueKey, DEFAULT_NFLVERSE_ROSTER_PATH,
  UsageError,
} from './cli.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
/** src/draft-room-recompute.py -- see that file's own docstring for why it
 * lives here, outside analytics/, rather than as a `tt.cli` subcommand. */
export const DEFAULT_RECOMPUTE_SCRIPT = path.join(HERE, 'draft-room-recompute.py');
export const DEFAULT_HTML_PATH = path.join(HERE, 'draft-room.html');

// Comfortably above the real board's ~1,849-row history (every player ever
// projectable, not just this year's draftable pool) -- see `createDraftRoom`
// startup: requesting this many from `board` means the truncation is never
// what decides who appears, VOR is (retired/irrelevant players sort to the
// very bottom on their own).
const ALL_PLAYERS_COUNT = 5000;
// How many (VOR-sorted) rows /api/state's "board" actually serves. The full
// history is kept in memory for ranking; the page never needs to render
// hundreds of players nobody would ever draft.
const DEFAULT_BOARD_VIEW_LIMIT = 300;
const DEFAULT_RECOMMEND_N = 10;
// How deep into Yahoo's own draft-rank-ordered player list `enrichBoard`
// walks for team/bye/injury. Measured against the real league: 400 covers
// 187 of the 300 rows the page renders, 600 covers 195, and 900 covers 195 --
// the ceiling is Yahoo's pool, not this number, because the rest of the
// VOR-sorted 300 are retired players the projection engine still carries
// (Gronkowski, Calvin Johnson, Tom Brady). 600 is where the curve flattens:
// six background requests, ~1.7s, and nothing gained by asking for more.
const DEFAULT_ENRICH_PLAYER_LIMIT = 600;

/** Yahoo roster slot -> which real positions can fill it (analytics/src/tt/
 * league.py's own FLEX_ELIGIBLE, duplicated here deliberately: this is
 * static roster-shape bookkeeping for a DISPLAY concern (which drafted
 * player goes under which slot label), not a modelling decision -- nothing
 * here computes a projection, a VOR, or a recommendation. */
const FLEX_ELIGIBLE = {
  'W/R/T': ['RB', 'WR', 'TE'],
  'W/R': ['RB', 'WR'],
  'Q/W/R/T': ['QB', 'RB', 'WR', 'TE'],
};

function sendJson(res, status, body) {
  const json = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(json),
  });
  res.end(json);
}

/** Reads and JSON-parses a request body. Returns `{}` for an empty body,
 * `null` (never throws) for invalid JSON, so a handler can 400 instead of
 * 500ing on a malformed POST. */
async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    return null;
  }
}

function roundsRemainingFor(teams, slot, rounds, myNextPickNumber) {
  // A team with no next pick has no runway left at all -- 0, not "the last
  // round". The redesign's runway row divides rounds by unfilled slots, and
  // a phantom final round there would understate the one effect this league
  // actually pays for (docs/positional-value.md: filling the lineup is worth
  // +63 to +80 actual points at 4 teams).
  if (myNextPickNumber === null || myNextPickNumber === undefined) return 0;
  const picks = myPicks(teams, slot, rounds);
  const idx = picks.indexOf(myNextPickNumber);
  const myRound = idx === -1 ? rounds : idx + 1;
  return Math.max(rounds - myRound + 1, 0);
}

/**
 * Build the live draft room's state + view-model logic. Returns
 * `{requestListener, poll, markTaken, undo, getViewModel, startPolling,
 * stopPolling}` -- no socket is opened here (see `startDraftRoomServer` for
 * that); this is the testable core, exercised directly by
 * test/draft-room.test.js without ever binding a port.
 *
 * FAILS LOUDLY, BEFORE THE DRAFT: every await in this function that can
 * fail (resolving the league/team, building the board, reading the league
 * config) is allowed to reject straight out of this function -- there is no
 * try/catch here. `src/cli.js`'s `draft-room` case lets that rejection
 * propagate to `runCommand`'s existing top-level error handling (which
 * already prints an `AnalyticsError`/`YahooApiError`/`SessionExpiredError`
 * clearly and exits non-zero), so a broken venv or a dead session fails the
 * command before the HTTP server ever starts listening -- never mid-draft.
 */
export async function createDraftRoom({
  teams, slot, rounds = 15,
  client, analytics,
  league: leagueFlag = undefined,
  cacheDir = undefined,
  leagueConfig: leagueConfigOverride = undefined,
  crosswalk: crosswalkOverride = undefined,
  nflverseIndex: nflverseIndexOverride = undefined,
  nflverseRosterPath = DEFAULT_NFLVERSE_ROSTER_PATH,
  myTeamKey: myTeamKeyOverride = undefined,
  now = () => Date.now(),
  recommendCount = DEFAULT_RECOMMEND_N,
  boardViewLimit = DEFAULT_BOARD_VIEW_LIMIT,
  enrichPlayerLimit = DEFAULT_ENRICH_PLAYER_LIMIT,
  allPlayersCount = ALL_PLAYERS_COUNT,
  htmlPath = DEFAULT_HTML_PATH,
  recomputeScript = DEFAULT_RECOMPUTE_SCRIPT,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  pollSeconds = 3,
} = {}) {
  const leagueKey = await fetchLeagueKey(client, leagueFlag);
  const myTeamKeyValue = myTeamKeyOverride ?? await fetchMyTeamKey(client, leagueKey);

  // The expensive step -- projections + VOR + tier + ADP, ~4-5s of Monte
  // Carlo -- called EXACTLY ONCE, here. See module docstring.
  const boardPayload = await analytics.run('board', {
    flags: { teams, slot, count: allPlayersCount },
  });
  const boardRecords = (boardPayload.players ?? []).filter((r) => r.player_id != null);
  const boardByPlayerId = new Map(boardRecords.map((r) => [r.player_id, r]));
  // Rank within position by consensus ADP -- the axis docs/positional-value.md
  // measures its cliffs on. Computed ONCE here, alongside the board, for
  // exactly the reason the board itself is (module docstring's performance
  // split): a player's ADP does not change during a draft.
  const adpRankByPlayerId = positionAdpRanks(boardRecords);
  // Where THIS board's own VOR crosses zero, per position. Startup-constant
  // for the same reason, and the ground the page's explainer stands on.
  const replacementByPosition = replacementLine(boardRecords);

  // WHICH ADP feed the numbers in the `adp` column came from -- read from the
  // file the Python side says it actually used (`adp_source`), never from the
  // Node-side `ffc` cache, which is a DIFFERENT feed: at the time of writing
  // the cache holds a 2026 Half-PPR 10-team pull over 3,208 drafts while the
  // engine built this board from analytics/data/ffc_adp_2025.json, Half-PPR
  // 12-team over 718. Explaining a column with the wrong feed's provenance is
  // the same class of error as showing the wrong number in it. Unreadable or
  // unlabelled -> null, and the glossary simply says less rather than
  // inventing a draft count.
  // `adp_source` is `tt.cli`'s own `_load_adp`, which returns a BARE FILENAME
  // ("ffc_adp_2025.json", `best.name`) -- not a path -- and its `--data-dir`
  // defaults to a relative "data". So it is resolved against the cwd the
  // analytics client spawns Python in, then against that cwd's data
  // directory. Reading it against Node's own cwd finds nothing, and the page
  // silently loses the provenance of the column it is trying to explain.
  let adpFeedMeta = null;
  const analyticsCwd = analytics.cwd ?? '.';
  for (const candidate of [
    path.resolve(analyticsCwd, boardPayload.adp_source ?? ''),
    path.resolve(analyticsCwd, 'data', boardPayload.adp_source ?? ''),
  ]) {
    try {
      // eslint-disable-next-line no-await-in-loop
      const parsed = JSON.parse(await readFile(candidate, 'utf8'));
      if (parsed?.meta) { adpFeedMeta = parsed.meta; break; }
    } catch {
      // Not here (or not readable): try the next candidate, then give up and
      // let the glossary simply say less rather than invent a draft count.
    }
  }

  const leagueConfigDict = leagueConfigOverride ?? JSON.parse(
    await readFile(path.join(analytics.cwd, 'data', 'league.json'), 'utf8'),
  );

  let crosswalk = crosswalkOverride;
  if (!crosswalk) {
    // Missing/corrupt cache degrades to an empty crosswalk (never a hard
    // failure) -- exactly resolveRosterIdentity's own tolerance in
    // src/cli.js, for the same reason: an optional identity enrichment
    // should never block the whole feature.
    const cached = await readCache('sleeper', { dir: cacheDir, ttlHours: undefined });
    crosswalk = buildCrosswalk(cached ? recordsOf(cached.data) : []);
  }

  // PASS 2 fallback for identity resolution (design doc 4.2; measured defect
  // writeup: Sleeper's gsis_id alone resolved ~22% of a real 210-pick draft).
  // IDENTICAL technique to src/cli.js's `resolveRosterIdentity` -- the SAME
  // tested buildAdpIndex/matchAdp matcher (src/identity.js), applied to
  // nflverse's own roster export -- loaded ONCE here, at startup, alongside
  // the board, never per poll (see module docstring's performance split).
  // Missing/corrupt file (export_nflverse_roster.py hasn't been run yet)
  // degrades to "no fallback available", exactly loadNflverseIndex's own
  // tolerance in src/cli.js: an optional identity enrichment must never
  // block the whole feature.
  let nflverseIndex = nflverseIndexOverride;
  if (!nflverseIndex) {
    try {
      const records = JSON.parse(await readFile(nflverseRosterPath, 'utf8'));
      nflverseIndex = buildAdpIndex(records);
    } catch {
      nflverseIndex = buildAdpIndex([]);
    }
  }

  // PASS 3 fallback for identity resolution -- the actual root cause writeup
  // (this branch's draft-room-names-report.md): `draft_results` carries NO
  // player name at all, only `player_key` (module docstring), so pass 2
  // above can only ever fire when Sleeper's own yahoo_id join already holds
  // *some* record for that player -- 150/210 real picks (71%) have none,
  // including the #1 overall pick, and nothing short of asking Yahoo itself
  // for the name closes that gap. Yahoo's own bulk `players;player_keys=`
  // resource answers exactly that (verified live: 25/50/100 keys requested
  // -> 25/50/100 returned, so the whole 210-pick draft resolves in <=3
  // calls) -- fetched HERE, keyed by Yahoo player_key, and kept for the life
  // of this room: a Yahoo player's identity never changes mid-draft, so a
  // key already in this map is never looked up again (see
  // `keysNeedingYahooNames`/`fetchYahooNames` below). This is enrichment
  // data only, feeding the SAME tested buildAdpIndex/matchAdp matcher pass 2
  // already uses -- no new matching logic.
  const yahooPlayerCache = new Map(); // yahooKey -> yahooPlayerRecord
  // Engine player_id -> the same record, for the rows the BOARD renders. See
  // `enrichBoard` for why this is keyed separately from the cache above.
  const enrichmentByPlayerId = new Map();
  const YAHOO_PLAYERS_CHUNK = 100; // Yahoo's own ceiling, verified live -- see above.

  /**
   * One Yahoo player, reduced to the fields this app shows. Verified field by
   * field against a live session; the shapes are Yahoo's, not ours:
   * `bye_weeks.week` arrives as a STRING and is coerced here so the page
   * never has to; `editorial_team_abbr` arrives title-cased ("Cin") and is
   * uppercased so a column of them lines up; and `status`/`status_full`/
   * `injury_note` are ABSENT ENTIRELY for a healthy player, which is why a
   * missing status becomes `null` (nothing wrong with him) rather than
   * anything the page could render as unknown.
   */
  function yahooPlayerRecord(p) {
    const bye = Number(p?.bye_weeks?.week);
    const team = p?.editorial_team_abbr;
    return {
      name: p?.name?.full ?? null,
      position: p?.display_position ?? null,
      team: team ? String(team).toUpperCase() : null,
      bye: Number.isFinite(bye) ? bye : null,
      status: p?.status ?? null,
      statusFull: p?.status_full ?? null,
      injuryNote: p?.injury_note ?? null,
      uniform: p?.uniform_number ?? null,
    };
  }

  /**
   * THE ONE PLACE THIS MODULE ASKS YAHOO ABOUT PLAYERS. Fetch `resource`,
   * fold every player it returns into `yahooPlayerCache` (keyed by Yahoo
   * player_key) and, where the SAME tested matchAdp/nflverseIndex matcher
   * passes 2-3 already use resolves one, into `enrichmentByPlayerId` (keyed
   * by the engine's own player_id, which is what the board is keyed on).
   * Returns how many players came back, so a caller walking pages knows when
   * to stop; returns 0 on any failure at all.
   *
   * NEVER THROWS (design doc section 5's explicit worst case: a poller that
   * rejects crashes the server mid-draft). A failing, rate-limited or
   * malformed response leaves the caches exactly as they were -- the affected
   * players simply stay undecorated, which costs a bye week on screen and
   * nothing else.
   *
   * FIRST ANSWER WINS, and it is never overwritten: a Yahoo player's team,
   * bye and identity do not change during a draft, so re-fetching one would
   * spend a live draft's request budget on nothing (and an injury designation
   * that flips mid-draft is not something this page should chase).
   */
  async function ingestYahooPlayers(resource) {
    let body;
    try {
      body = await client.get(resource);
    } catch {
      return 0;
    }
    const players = body?.league?.players ?? body?.players ?? [];
    if (!Array.isArray(players)) return 0;
    for (const p of players) {
      const key = p?.player_key;
      if (!key) continue;
      const record = yahooPlayerRecord(p);
      if (!yahooPlayerCache.has(key)) yahooPlayerCache.set(key, record);
      const match = matchAdp(nflverseIndex, record);
      if (match?.playerId && !enrichmentByPlayerId.has(match.playerId)) {
        enrichmentByPlayerId.set(match.playerId, record);
      }
    }
    return players.length;
  }

  /**
   * PASS 3's fetch: Yahoo's own record for `keys` (player_keys not already
   * cached), batched at `YAHOO_PLAYERS_CHUNK` per request. Never re-fetches:
   * a key is only ever passed in here once, by `keysNeedingYahooNames`. A
   * chunk that fails is skipped and retried on a later poll; chunks that
   * already succeeded are kept.
   */
  async function fetchYahooNames(keys) {
    for (let i = 0; i < keys.length; i += YAHOO_PLAYERS_CHUNK) {
      const chunk = keys.slice(i, i + YAHOO_PLAYERS_CHUNK);
      // eslint-disable-next-line no-await-in-loop
      await ingestYahooPlayers(`players;player_keys=${chunk.join(',')}`);
    }
  }

  /**
   * THE ENRICHMENT WALK: Yahoo's own NFL team, bye week and injury status for
   * the players on the board, in the background.
   *
   * WHY NOT `players;player_keys=`, THE WAY PASS 3 DOES IT. That resource
   * needs Yahoo player_keys, and the board is keyed on nflverse ids. The only
   * gsis -> yahoo crosswalk this app has is Sleeper's, and its gap clusters
   * on exactly the players who go early -- measured on the real board, it has
   * no yahoo id for Bijan Robinson, Jahmyr Gibbs, Ja'Marr Chase or Ashton
   * Jeanty, i.e. the whole top of the page. Worse, Yahoo rejects an ENTIRE
   * batch of 100 keys with a 400 if a single one is stale ("Player key
   * 470.p.24017 does not exist"), which is exactly what a crosswalk built
   * from another provider's ids produces. Both problems disappear by asking
   * Yahoo for its own list, in its own draft-rank order, and matching the
   * result back through the SAME tested matchAdp/nflverseIndex matcher
   * passes 2-3 already use -- no new matching logic, and every key it yields
   * is one Yahoo itself just handed us.
   *
   * ON A BACKGROUND PROMISE, NEVER AWAITED BY STARTUP. Six requests, ~1.7s
   * measured; the board is fully usable without any of it, and a drafter who
   * opens the page ten seconds before his pick needs the board, not the bye
   * weeks. The decoration simply appears on the next 3s poll.
   */
  async function enrichBoard() {
    for (let start = 0; start < enrichPlayerLimit; start += YAHOO_PLAYERS_CHUNK) {
      // eslint-disable-next-line no-await-in-loop
      const got = await ingestYahooPlayers(
        `league/${leagueKey}/players;sort=AR;start=${start};count=${YAHOO_PLAYERS_CHUNK}`,
      );
      // A short page is the end of Yahoo's list (or a failure, which returns
      // 0). Either way there is nothing further to walk toward.
      if (got < YAHOO_PLAYERS_CHUNK) return;
    }
  }

  let draftState = createState(boardRecords.map((r) => r.player_id));

  let lastPollAt = null; // ms epoch of the last poll that produced USABLE data
  let lastPollOk = true;
  let pollBanner = null;
  let recomputeCache = { board: boardRecords, recommendations: [] };
  let recomputeErrorMessage = null;

  /**
   * The live clock: who's on it, and my next pick -- with round, for
   * display (design doc 4.1/4.2 update). PRECEDENCE, made explicit: Yahoo's
   * `draft_results` publishes the WHOLE draft order up front, made or
   * pending, from the very first poll -- that is authoritative (it is
   * correct for custom draft order, third-round reversal, or any league
   * setting we don't model) and beats our own `myPicks`/`nextPick` snake
   * math outright. `clockFromPending` (src/draft-state.js) is tried FIRST;
   * `myPicks`/`nextPick` are kept only as the FALLBACK for when
   * `draft_results` is empty or unavailable (predraft, before the first
   * successful poll) -- see that function's own docstring for why it
   * returns null exactly in that case.
   */
  function deriveClock() {
    const fromYahoo = clockFromPending(draftState.pending, myTeamKeyValue);
    if (fromYahoo) {
      return {
        currentRound: fromYahoo.currentRound,
        onTheClock: fromYahoo.onTheClock,
        myNextPick: fromYahoo.myNextPick,
        myNextPickRound: fromYahoo.myNextPickRound,
      };
    }
    // FALLBACK: our own model of the snake order (design doc 4.1).
    const myNext = nextPick(teams, slot, draftState.currentPick, rounds);
    const isMyTurn = myNext !== null && draftState.currentPick === myNext;
    return {
      currentRound: Math.floor((draftState.currentPick - 1) / teams) + 1,
      onTheClock: isMyTurn ? myTeamKeyValue : null,
      myNextPick: myNext,
      myNextPickRound: myNext === null ? null : Math.floor((myNext - 1) / teams) + 1,
    };
  }

  /**
   * The pick survival is measured TO -- and it is NOT always `myNextPick`.
   *
   * While you are ON THE CLOCK, `deriveClock` correctly reports
   * `myNextPick === currentPick` (that is exactly what `isMyTurn` is built
   * from). `recompute` used to pass that straight through, and
   * `tt.survival.add_survival` rejects `next_pick <= pick` outright -- it
   * answers "will he last from my pick now to my NEXT one", which only makes
   * sense looking forward. The result was that EVERY one of the user's own
   * turns raised the recompute banner and froze the recommendations at
   * exactly the moment the tool is being used. (Found by rendering a real
   * 4-team draft state; see this branch's report.)
   *
   * On the clock the horizon that actually matters is the FOLLOWING turn: if
   * I don't take him now, will he still be there then? Yahoo's own pending
   * list is authoritative here for the same reason `deriveClock` prefers it
   * -- it is correct for a custom draft order this module does not model --
   * with `myPicks` as the fallback before the first successful poll.
   *
   * On the LAST pick of my draft there is no following turn at all, so the
   * horizon is the end of the draft: everyone I do not take now is gone,
   * which is the honest reading rather than a crash or a made-up number.
   */
  function survivalPickFrom(clock) {
    if (clock.myNextPick === null) return null;
    if (clock.myNextPick > draftState.currentPick) return clock.myNextPick;
    let following = null;
    for (const p of draftState.pending ?? []) {
      if (p.teamKey !== myTeamKeyValue) continue;
      if (p.pick > draftState.currentPick && (following === null || p.pick < following)) following = p.pick;
    }
    if (following === null) {
      following = myPicks(teams, slot, rounds).find((p) => p > draftState.currentPick) ?? null;
    }
    return following ?? (teams * rounds + 1);
  }

  /**
   * The cheap per-poll/per-mark step (design doc section 3): re-run
   * survival + need-adjusted ranking against the board built at startup,
   * via src/draft-room-recompute.py -- never re-run `board`/`pick`
   * (that would re-trigger the ~4-5s Monte Carlo on every poll).
   *
   * On failure, the LAST GOOD `recomputeCache` is kept untouched (design
   * doc section 5: "Python engine fails later -> keep the last good
   * board"); only `recomputeErrorMessage` changes, surfaced via `banner`.
   */
  async function recompute() {
    const clock = deriveClock();
    const myNext = clock.myNextPick;
    const survivalPick = survivalPickFrom(clock);
    if (myNext === null) {
      // My draft is over -- nothing left to recommend. The board itself is
      // still worth serving (with the last known decoration, if any), just
      // with no survival number that means anything going forward.
      recomputeCache = { board: recomputeCache.board, recommendations: [] };
      recomputeErrorMessage = null;
      return;
    }
    const roundsRemaining = roundsRemainingFor(teams, slot, rounds, myNext);
    const roster = draftState.myRoster.map((id) => ({
      player_id: id,
      position: boardByPlayerId.get(id)?.position ?? null,
    }));
    try {
      const result = await analytics.runScript(recomputeScript, {
        stdin: {
          records: boardRecords,
          pick: draftState.currentPick,
          nextPick: survivalPick,
          availableIds: [...draftState.available],
          roster,
          config: leagueConfigDict,
          teams,
          roundsRemaining,
          n: recommendCount,
        },
      });
      recomputeCache = result;
      recomputeErrorMessage = null;
    } catch (e) {
      // Deliberately catches EVERYTHING, not just AnalyticsError -- an
      // unanticipated bug here must degrade to "keep serving the last good
      // board", same as a known Python-side failure. See module docstring.
      recomputeErrorMessage = e?.message ? String(e.message) : String(e);
    }
  }

  /**
   * Resolve one Yahoo player_key to the engine's player_id -- THREE PASSES,
   * reusing src/cli.js's `resolveRosterIdentity` technique exactly rather
   * than reimplementing it (design doc 4.2; see this branch's reports for
   * the measured before/after at each step):
   *
   *   PASS 1: Sleeper's own gsis_id, via the existing tested crosswalk
   *     (buildCrosswalk + lookupByYahooKey, src/identity.js).
   *   PASS 2: when Sleeper carries a record for this yahoo id but no
   *     gsis_id -- a REAL, measured gap that clusters on exactly the
   *     players who go early, see resolveRosterIdentity's own docstring --
   *     the SAME tested buildAdpIndex/matchAdp matcher (src/identity.js),
   *     applied to that Sleeper record's OWN name/position/team against
   *     nflverse's roster export.
   *   PASS 3 (the actual root cause fix -- draft-room-names-report.md):
   *     `draft_results` carries no Yahoo player name at all, so passes 1-2
   *     never even fire for the 71% of a real draft with no Sleeper record
   *     for that yahoo id at all (not merely a missing gsis_id). Yahoo's OWN
   *     name/position/team for exactly those keys -- fetched in bulk via
   *     `players;player_keys=` and cached in `yahooPlayerCache` (see
   *     `fetchYahooNames`/`keysNeedingYahooNames` above) -- is fed into the
   *     SAME matchAdp/nflverseIndex matcher pass 2 already uses. A key not
   *     yet in the cache (not fetched yet, or the fetch failed/rate-limited)
   *     simply has nothing to try here and stays unresolved for this poll --
   *     there is no fourth pass.
   *
   * Never guesses (buildAdpIndex/matchAdp's own defining property): returns
   * null, never a best guess, when no pass resolves.
   */
  // Display name for a drafted player under WHICHEVER id lands in myRoster --
  // the engine player_id when identity resolved, the raw Yahoo key when it did
  // not. Without this the roster falls back to `rec?.name ?? id` and renders an
  // unresolved pick as "470.p.42654 (null)", which tells the user nothing at
  // the exact moment they are checking what they already own. We fetched the
  // real name in pass 3; not showing it was pure oversight.
  const displayNameById = new Map();

  function rememberName(id, playerKey) {
    if (!id) return;
    const known = yahooPlayerCache.get(playerKey);
    if (known?.name) displayNameById.set(id, known);
  }

  function resolveYahooKey(playerKey) {
    const xw = lookupByYahooKey(crosswalk, playerKey);
    if (xw?.gsisId) { rememberName(xw.gsisId, playerKey); return xw.gsisId; }
    if (xw) {
      const fallback = matchAdp(nflverseIndex, { name: xw.name, position: xw.position, team: xw.team });
      if (fallback?.playerId) { rememberName(fallback.playerId, playerKey); return fallback.playerId; }
    }
    const yahooRecord = yahooPlayerCache.get(playerKey);
    if (yahooRecord) {
      const fallback = matchAdp(nflverseIndex, yahooRecord);
      if (fallback?.playerId) { rememberName(fallback.playerId, playerKey); return fallback.playerId; }
    }
    // Unresolved: the pick still counts, and it still leaves the pool by Yahoo
    // key, so remember the name under THAT key for display.
    rememberName(playerKey, playerKey);
    return null;
  }

  /**
   * Which of `picks`' Yahoo player_keys are worth asking Yahoo's own
   * `players;player_keys=` resource about: made picks whose key
   *   (a) isn't already in `yahooPlayerCache` (never re-fetch a resolved
   *       key -- a player's identity doesn't change mid-draft, and
   *       `enrichBoard` has usually cached it already), AND
   *   (b) doesn't already resolve via passes 1-2 (asking Yahoo for a name
   *       the Sleeper crosswalk already gave us for free would waste a
   *       poll's worth of quota on nothing).
   * Deduped, order-preserving. `picks` is Yahoo's own FULL made-picks list
   * every poll, not a delta (see module docstring/`resolveAndApply`), so
   * this scan -- cheap Map/Set lookups only, no network -- is what keeps the
   * actual fetch scoped to only the picks that are NEW since the last poll.
   */
  function keysNeedingYahooNames(picks) {
    const seen = new Set();
    const keys = [];
    for (const p of picks) {
      const key = p?.playerKey;
      if (!key || seen.has(key) || yahooPlayerCache.has(key)) continue;
      seen.add(key);
      if (resolveYahooKey(key) == null) keys.push(key);
    }
    return keys;
  }

  /** Fold Yahoo `draft_results` picks into state, translating each Yahoo
   * player_key to the engine's player_id via `resolveYahooKey` (design doc
   * section 4.2). An unresolved key is passed through unchanged: it still
   * advances the pick count and `currentPick` (applyPicks records it in
   * `drafted` regardless), it just can never match a board row -- an
   * identity gap degrades the recommendation, never the pool. */
  function resolveAndApply(picks, pending) {
    const resolved = picks.map((p) => {
      const engineId = resolveYahooKey(p.playerKey);
      return engineId ? { ...p, playerKey: engineId } : p;
    });
    // `pending` entries have no player_key, so there's nothing to resolve
    // through the crosswalk -- they're only ever team_key/pick/round.
    draftState = applyPicks(draftState, resolved, { myTeamKey: myTeamKeyValue, pending });
  }

  /**
   * One poll cycle: fetch `league/{key}/draftresults`, parse it, and either
   * apply it or raise the appropriate banner. NEVER throws -- every branch
   * that can fail is caught here, and an outer catch-all guards against
   * anything unanticipated slipping through, because an unhandled
   * rejection inside the `setInterval` this drives would crash the server
   * mid-draft (design doc section 5's explicit worst case).
   */
  async function poll() {
    try {
      let body;
      try {
        body = await client.get(`league/${leagueKey}/draftresults`);
      } catch (e) {
        lastPollOk = false;
        if (e instanceof SessionExpiredError) {
          pollBanner = {
            level: 'error',
            message: 'Yahoo session expired. Run: tt login in another terminal, then restart '
              + 'tt draft-room to pick it back up. Manual override still works below.',
          };
        } else {
          const msg = e instanceof YahooApiError ? e.message : (e?.message ?? String(e));
          pollBanner = { level: 'warn', message: `Could not reach Yahoo: ${msg}. Showing the last known board.` };
        }
        return; // lastPollAt untouched -- staleSeconds grows from here
      }

      const raw = body?.league?.draft_results ?? body?.draft_results ?? [];
      const { picks, pending, malformed } = parseDraftResults(raw);

      if (malformed.length > 0) {
        lastPollOk = false;
        pollBanner = {
          level: 'error',
          message: `Could not parse ${malformed.length} draft pick(s) from Yahoo -- the `
            + 'draft_results shape may have changed. Showing the last known board; use manual '
            + 'override below.',
        };
        return; // NOTHING from this response is trusted, not even the well-formed picks/pending
      }

      const changed = picks.length > 0;
      if (changed) {
        // Pass 3 (see resolveYahooKey): ask Yahoo for the names of whatever
        // made picks aren't already resolved or cached, batched, capped at
        // YAHOO_PLAYERS_CHUNK per request -- see fetchYahooNames's own
        // docstring for why this can never throw or block the rest of the
        // poll even when it fails outright.
        const keysToFetch = keysNeedingYahooNames(picks);
        if (keysToFetch.length > 0) await fetchYahooNames(keysToFetch);
      }
      resolveAndApply(picks, pending);
      pollBanner = null;
      lastPollOk = true;
      lastPollAt = now();
      if (changed) await recompute();
    } catch (e) {
      lastPollOk = false;
      pollBanner = { level: 'warn', message: `Unexpected error polling Yahoo: ${e?.message ?? e}` };
    }
  }

  function currentBanner() {
    if (pollBanner) return pollBanner;
    if (recomputeErrorMessage) {
      return { level: 'warn', message: `Recommendations could not be refreshed: ${recomputeErrorMessage}` };
    }
    return null;
  }

  /**
   * How much of the visible board is actually trustworthy: of every pick
   * Yahoo has confirmed (`draftState.drafted`), how many resolved to a real
   * board row (`boardByPlayerId`) rather than being left on the board,
   * wrongly, by an identity gap. This is deliberately the SAME test
   * `buildBoardView`'s own `taken` flag uses (`draftState.available.has`,
   * which is exactly `boardByPlayerId.has` for a resolved id) -- the number
   * shown here and the board's own taken/available split can never disagree.
   *
   * This is the fix for the defect this branch exists to close: a silent
   * resolution gap is what made 78% of a real draft's picks stay marked
   * AVAILABLE invisible in the first place (see this branch's crosswalk
   * report). `rate` is null, not 0, before any pick has been made -- there
   * is nothing to report a rate over yet, and 0% would misleadingly read as
   * "the crosswalk is broken" during predraft.
   */
  function identityStats() {
    const total = draftState.drafted.size;
    if (total === 0) return { matched: 0, unresolved: 0, total: 0, rate: null };
    let matched = 0;
    for (const entry of draftState.drafted.values()) {
      if (boardByPlayerId.has(entry.playerId)) matched += 1;
    }
    return { matched, unresolved: total - matched, total, rate: matched / total };
  }

  function buildTakenByMap() {
    const m = new Map();
    for (const entry of draftState.drafted.values()) m.set(entry.playerId, entry.teamKey);
    return m;
  }

  /** Positional ADP rank, how far past (or short of) his own ADP a player is
   * RIGHT NOW, and Yahoo's own team/bye/injury for him.
   *
   * A positive `adpDelta` means he is falling -- "going 14 picks later than
   * ADP" is one of the most actionable things on a live board and the old
   * page rendered nothing at all for it; `adpFall` grades that same number
   * against a full turn of THIS league's draft, so a shallow room never shows
   * a false bargain (src/draft-guidance.js's `adpFallLevel`).
   *
   * `games` is the engine's own `proj_games` -- the number that makes `proj`
   * an expected-games total rather than a 17-game one. The engine has always
   * produced it; the view model simply dropped it, which is what let the page
   * imply a projection was a full healthy season.
   *
   * Every enrichment field is null until `enrichBoard`'s background walk has
   * come back (or forever, if it fails). A null bye is "we don't know", never
   * "no bye", and the page renders nothing at all for it. */
  function decorate(r) {
    const posRank = adpRankByPlayerId.get(r.player_id) ?? null;
    const cliff = CLIFFS[r.position] ?? null;
    const adpDelta = Number.isFinite(r.adp) ? draftState.currentPick - r.adp : null;
    const enriched = enrichmentByPlayerId.get(r.player_id) ?? null;
    return {
      posRank,
      adpDelta,
      adpFall: adpFallLevel(adpDelta, teams),
      aboveCliff: cliff !== null && posRank !== null ? posRank <= cliff.afterRank : null,
      games: Number.isFinite(r.proj_games) ? r.proj_games : null,
      team: enriched?.team ?? null,
      bye: enriched?.bye ?? null,
      injuryStatus: enriched?.status ?? null,
      injuryLabel: enriched?.statusFull ?? null,
      injuryNote: enriched?.injuryNote ?? null,
    };
  }

  function buildBoardView() {
    const takenByMap = buildTakenByMap();
    const rows = (recomputeCache.board ?? boardRecords).map((r) => {
      const available = draftState.available.has(r.player_id);
      return {
        playerId: r.player_id,
        name: r.name,
        position: r.position,
        proj: r.proj_points ?? null,
        vor: r.vor ?? null,
        tier: r.tier ?? null,
        adp: r.adp ?? null,
        pGone: r.p_gone_by_next ?? null,
        taken: !available,
        takenBy: available ? null : (takenByMap.get(r.player_id) ?? null),
        ...decorate(r),
      };
    });
    rows.sort((a, b) => (b.vor ?? -Infinity) - (a.vor ?? -Infinity));
    const visible = rows.slice(0, boardViewLimit);
    // The two quiet visuals, computed over exactly the rows that get drawn
    // and in exactly the order they get drawn in: a bar scaled within its own
    // position (never across the board -- comparing a QB's raw VOR to an RB's
    // is the mistake this whole page exists to prevent), and the tier runs
    // that make "these four are interchangeable" something you see rather
    // than infer. Both live in src/draft-guidance.js and are tested there.
    const shares = vorShares(visible);
    const bands = tierBands(visible);
    for (const row of visible) {
      row.vorShare = shares.get(row.playerId) ?? null;
      const band = bands.get(row.playerId);
      row.tierBand = band?.band ?? 0;
      row.tierFirst = band?.first ?? true;
    }
    return visible;
  }

  function buildRecommendationsView(runway) {
    const emptyPositions = new Set();
    for (const s of runway?.slots ?? []) {
      if (s.filled) continue;
      const eligible = FLEX_ELIGIBLE[s.slot] ?? [s.slot];
      for (const pos of eligible) emptyPositions.add(pos);
    }
    return (recomputeCache.recommendations ?? []).map((r) => ({
      playerId: r.player_id,
      name: r.name,
      position: r.position,
      // The engine already knows a projection for every ranked player; the
      // old view model simply dropped it before the page could show it.
      proj: r.proj_points ?? null,
      vor: r.vor ?? null,
      tier: r.tier ?? null,
      adp: r.adp ?? null,
      pGone: r.p_gone_by_next ?? null,
      expectedLoss: r.expected_loss ?? null,
      fillsNeed: emptyPositions.has(r.position),
      ...decorate(r),
    }));
  }

  function buildRosterView() {
    const slotDefs = [];
    for (const [pos, count] of Object.entries(leagueConfigDict.rosterSlots ?? {})) {
      for (let i = 0; i < count; i += 1) slotDefs.push(pos);
    }
    const remaining = draftState.myRoster.map((id) => {
      // Yahoo's own bye week and injury status for a player you already own:
      // the bye is what `byeConflicts` reads to warn about a week you cannot
      // field a lineup, and it belongs on the lineup panel either way.
      const enriched = enrichmentByPlayerId.get(id) ?? displayNameById.get(id) ?? null;
      const extra = {
        bye: enriched?.bye ?? null,
        injuryStatus: enriched?.status ?? null,
        injuryLabel: enriched?.statusFull ?? null,
        injuryNote: enriched?.injuryNote ?? null,
      };
      const rec = boardByPlayerId.get(id);
      if (rec) {
        return { playerId: id, name: rec.name, position: rec.position ?? null, unmatched: false, ...extra };
      }
      // Not on the board: either an unresolved pick, or a resolved one the
      // engine never projects (K and DEF). Either way Yahoo told us the name.
      const known = displayNameById.get(id);
      return {
        playerId: id,
        name: known?.name ?? id,
        position: known?.position ?? null,
        unmatched: true,
        ...extra,
      };
    });
    const slots = slotDefs.map((slotPos) => {
      let idx = remaining.findIndex((p) => p.position === slotPos);
      if (idx === -1 && FLEX_ELIGIBLE[slotPos]) {
        idx = remaining.findIndex((p) => FLEX_ELIGIBLE[slotPos].includes(p.position));
      }
      if (idx === -1) return { slot: slotPos, player: null };
      const [p] = remaining.splice(idx, 1);
      return { slot: slotPos, player: p };
    });
    return { slots, bench: remaining };
  }

  function buildStatus() {
    const clock = deriveClock();
    const isMyTurn = clock.myNextPick !== null && draftState.currentPick === clock.myNextPick;
    let draftStatus;
    if (draftState.drafted.size === 0 && draftState.manualMarks.length === 0) draftStatus = 'predraft';
    else if (clock.myNextPick === null) draftStatus = 'postdraft';
    else draftStatus = 'drafting';
    return {
      draftStatus,
      picksMade: draftState.drafted.size,
      currentPick: draftState.currentPick,
      currentRound: clock.currentRound,
      myNextPick: clock.myNextPick,
      myNextPickRound: clock.myNextPickRound,
      // The pick every survival number on the page is measured to. Equal to
      // myNextPick except while you are on the clock -- see survivalPickFrom.
      survivalPick: survivalPickFrom(clock),
      onTheClock: clock.onTheClock,
      isMyTurn,
      roundsRemaining: roundsRemainingFor(teams, slot, rounds, clock.myNextPick),
      lastPollAt: lastPollAt === null ? null : new Date(lastPollAt).toISOString(),
      lastPollOk,
      staleSeconds: lastPollAt === null ? 0 : Math.max(0, Math.floor((now() - lastPollAt) / 1000)),
      banner: currentBanner(),
      identity: identityStats(),
    };
  }

  /**
   * THE REDESIGN'S CORE, and the reason this block exists at all.
   *
   * `docs/positional-value.md` measured, out of sample and graded on actual
   * points, that in this 4-team league:
   *   - FILLING THE LINEUP is worth +63 to +80 points -- more than any
   *     positional strategy in the study. -> `runway`.
   *   - POSITIONAL URGENCY is mostly a 10-team problem: the largest waiting
   *     cost anywhere in a 4-team draft is 20 points, most rounds under 10.
   *     -> `urgency`, whose alarm threshold sits above that measured
   *     maximum on purpose, so a calm 4-team board renders calm.
   *   - THE CLIFFS ARE COUNTABLE: RB and WR after the 12th at the position,
   *     TE after the 6th, QB never. -> `cliffs`.
   *
   * Every number here is either an engine output being counted/ranked or a
   * constant transcribed from that document (src/draft-guidance.js). Nothing
   * here re-ranks, re-scores, or second-guesses the engine: `hero` is
   * literally `recommendations[0]`, explained.
   */
  function buildGuidance(status, runway, recommendations, roster) {
    const cliffs = cliffStrip({
      rows: recomputeCache.board ?? boardRecords,
      available: draftState.available,
      teams,
      rounds,
    });

    const round = status.currentRound;
    const byPosition = {};
    const levelByPosition = {};
    let clamped = false;
    for (const position of ['QB', 'RB', 'WR', 'TE']) {
      const cost = waitingCost({ position, round, teams, rounds });
      byPosition[position] = cost.cost;
      levelByPosition[position] = urgencyLevel(cost.cost);
      clamped = clamped || cost.clamped;
    }
    for (const cliff of cliffs) cliff.level = levelByPosition[cliff.position] ?? 'calm';

    const top = recommendations[0] ?? null;
    const heroCost = top ? byPosition[top.position] ?? null : null;
    const urgency = {
      round: round ?? null,
      teams,
      basisTeams: basisTeamsFor(teams),
      clamped,
      byPosition,
      levelByPosition,
      position: top ? top.position : null,
      cost: top ? heroCost : null,
      level: top ? urgencyLevel(heroCost) : 'calm',
    };

    const hero = top === null ? null : {
      ...top,
      reasons: heroReasons({ top, cliffs, runway, myNextPick: status.survivalPick }),
    };

    return {
      hero,
      cliffs,
      runway,
      urgency,
      replacement: replacementByPosition,
      // Every abbreviation this page prints, defined in THIS board's own
      // numbers. Composed in src/draft-guidance.js and asserted there, for
      // the reason that module exists: a definition written in the HTML is a
      // definition nothing can check, and a wrong one on draft day is worse
      // than none at all.
      glossary: glossary({
        rows: recomputeCache.board ?? boardRecords,
        replacement: replacementByPosition,
        teams,
        survivalPick: status.survivalPick,
        adp: adpFeedMeta,
      }),
      // Weeks where two of YOUR STARTERS are out at once -- the same class of
      // problem as an unfilled slot, and invisible until the byes were shown.
      // Read off the ROSTER's own slots (which carry the whole player, byes
      // included) rather than the runway's, whose `player` is only ever a
      // display name -- and every starting slot counts, K and DEF included:
      // the runway excludes those two because the engine cannot recommend
      // into them, which has nothing to do with whether they play that week.
      byes: byeConflicts(roster?.slots ?? []),
      league: { teams, rounds, slot },
    };
  }

  function getViewModel() {
    const status = buildStatus();
    const roster = buildRosterView();
    const runway = runwayState({ slots: roster.slots, roundsRemaining: status.roundsRemaining });
    const recommendations = buildRecommendationsView(runway);
    return {
      status,
      recommendations,
      board: buildBoardView(),
      roster,
      guidance: buildGuidance(status, runway, recommendations, roster),
    };
  }

  async function markTaken(playerId) {
    draftState = markTakenState(draftState, playerId);
    await recompute();
  }

  async function undo() {
    draftState = undoState(draftState);
    await recompute();
  }

  async function handleTaken(req, res) {
    const body = await readJsonBody(req);
    if (body === null) return sendJson(res, 400, { error: 'invalid JSON body' });
    const playerId = body?.playerId;
    if (typeof playerId !== 'string' || playerId === '') {
      return sendJson(res, 400, { error: 'playerId is required' });
    }
    await markTaken(playerId);
    return sendJson(res, 200, getViewModel());
  }

  async function handleUndo(req, res) {
    await readJsonBody(req);
    await undo();
    return sendJson(res, 200, getViewModel());
  }

  let htmlCache = null;
  async function requestListener(req, res) {
    try {
      const { pathname } = new URL(req.url, 'http://localhost');
      if (req.method === 'GET' && pathname === '/') {
        if (htmlCache === null) htmlCache = await readFile(htmlPath, 'utf8');
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(htmlCache);
        return;
      }
      if (req.method === 'GET' && pathname === '/api/state') {
        sendJson(res, 200, getViewModel());
        return;
      }
      if (req.method === 'POST' && pathname === '/api/taken') {
        await handleTaken(req, res);
        return;
      }
      if (req.method === 'POST' && pathname === '/api/undo') {
        await handleUndo(req, res);
        return;
      }
      sendJson(res, 404, { error: 'not found' });
    } catch (e) {
      sendJson(res, 500, { error: e?.message ?? 'internal error' });
    }
  }

  let intervalHandle = null;
  function startPolling() {
    if (intervalHandle) return;
    intervalHandle = setIntervalFn(() => { poll().catch(() => {}); }, pollSeconds * 1000);
    if (typeof intervalHandle.unref === 'function') intervalHandle.unref();
  }
  function stopPolling() {
    if (intervalHandle) {
      clearIntervalFn(intervalHandle);
      intervalHandle = null;
    }
  }

  await recompute(); // so /api/state has real data before the first poll ever runs

  // The enrichment walk starts here and is DELIBERATELY NOT AWAITED: the room
  // must be serving a full board the moment this function returns (measured:
  // ~7s, all of it the one-off Monte Carlo, and this adds none of it). It can
  // never reject -- `ingestYahooPlayers` swallows every failure by
  // construction -- so the promise is safe to hold and to hand out for tests
  // and verification harnesses that need the decoration to have landed.
  const enrichment = enrichBoard();

  return {
    requestListener, poll, markTaken, undo, getViewModel, startPolling, stopPolling,
    leagueKey, myTeamKey: myTeamKeyValue, enrichment,
  };
}

/**
 * Wrap `createDraftRoom` with a real `node:http` server bound to `host`
 * (default 127.0.0.1 -- design doc section 7: Yahoo cookies never leave the
 * machine, so this never binds anything but loopback). Returns
 * `{server, room, url, port, close}`.
 */
export async function startDraftRoomServer({ port = 8787, host = '127.0.0.1', ...roomOptions } = {}) {
  const room = await createDraftRoom(roomOptions);
  const server = http.createServer((req, res) => { room.requestListener(req, res); });
  await new Promise((resolve, reject) => {
    server.once('error', (e) => {
      // Ten minutes before a draft, the likeliest way this command fails is
      // that a room is ALREADY running -- re-run by hand, or left over from an
      // earlier start. Node's default is an EADDRINUSE stack trace with a
      // `_listen2` frame, which tells someone under time pressure nothing
      // about what to do. Every other failure in this module was built to
      // degrade into an instruction; this one escaped because it happens
      // before the server exists.
      if (e?.code === 'EADDRINUSE') {
        reject(new UsageError(
          `Port ${port} is already in use — a draft room may already be running.\n`
          + `Open http://${host}:${port}/ to check, or start this one on another port:\n`
          + `  tt draft-room --teams=${roomOptions.teams} --slot=${roomOptions.slot} --port=${port + 1}`,
        ));
        return;
      }
      if (e?.code === 'EACCES') {
        reject(new UsageError(
          `Not allowed to bind port ${port}. Ports below 1024 need root; pick a higher one:\n`
          + `  tt draft-room --teams=${roomOptions.teams} --slot=${roomOptions.slot} --port=8787`,
        ));
        return;
      }
      reject(e);
    });
    server.listen(port, host, () => resolve());
  });
  room.startPolling();
  const actualPort = server.address().port;
  const url = `http://${host}:${actualPort}/`;
  async function close() {
    room.stopPolling();
    await new Promise((resolve) => server.close(() => resolve()));
  }
  return { server, room, url, port: actualPort, close };
}
