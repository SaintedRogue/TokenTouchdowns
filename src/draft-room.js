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
 */
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  myPicks, nextPick, parseDraftResults, clockFromPending, createState, applyPicks,
  markTaken as markTakenState, undo as undoState,
} from './draft-state.js';
import { SessionExpiredError, YahooApiError } from './client.js';
import { buildCrosswalk, lookupByYahooKey } from './identity.js';
import { recordsOf } from './sources/index.js';
import { readCache } from './cache.js';
import { myTeamKey as fetchMyTeamKey, resolveLeagueKey as fetchLeagueKey } from './cli.js';

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
  myTeamKey: myTeamKeyOverride = undefined,
  now = () => Date.now(),
  recommendCount = DEFAULT_RECOMMEND_N,
  boardViewLimit = DEFAULT_BOARD_VIEW_LIMIT,
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

  function myNextPickNow() {
    return deriveClock().myNextPick;
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
    const myNext = myNextPickNow();
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
          nextPick: myNext,
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

  /** Fold Yahoo `draft_results` picks into state, translating each Yahoo
   * player_key to the engine's player_id via the crosswalk (design doc
   * section 4.2). An unresolved key is passed through unchanged: it still
   * advances the pick count and `currentPick` (applyPicks records it in
   * `drafted` regardless), it just can never match a board row -- an
   * identity gap degrades the recommendation, never the pool. */
  function resolveAndApply(picks, pending) {
    const resolved = picks.map((p) => {
      const xw = lookupByYahooKey(crosswalk, p.playerKey);
      return xw?.gsisId ? { ...p, playerKey: xw.gsisId } : p;
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

  function buildTakenByMap() {
    const m = new Map();
    for (const entry of draftState.drafted.values()) m.set(entry.playerId, entry.teamKey);
    return m;
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
      };
    });
    rows.sort((a, b) => (b.vor ?? -Infinity) - (a.vor ?? -Infinity));
    return rows.slice(0, boardViewLimit);
  }

  function buildRecommendationsView() {
    return (recomputeCache.recommendations ?? []).map((r) => ({
      playerId: r.player_id,
      name: r.name,
      position: r.position,
      vor: r.vor ?? null,
      tier: r.tier ?? null,
      adp: r.adp ?? null,
      pGone: r.p_gone_by_next ?? null,
      expectedLoss: r.expected_loss ?? null,
    }));
  }

  function buildRosterView() {
    const slotDefs = [];
    for (const [pos, count] of Object.entries(leagueConfigDict.rosterSlots ?? {})) {
      for (let i = 0; i < count; i += 1) slotDefs.push(pos);
    }
    const remaining = draftState.myRoster.map((id) => {
      const rec = boardByPlayerId.get(id);
      return { playerId: id, name: rec?.name ?? id, position: rec?.position ?? null };
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
      onTheClock: clock.onTheClock,
      isMyTurn,
      lastPollAt: lastPollAt === null ? null : new Date(lastPollAt).toISOString(),
      lastPollOk,
      staleSeconds: lastPollAt === null ? 0 : Math.max(0, Math.floor((now() - lastPollAt) / 1000)),
      banner: currentBanner(),
    };
  }

  function getViewModel() {
    return {
      status: buildStatus(),
      recommendations: buildRecommendationsView(),
      board: buildBoardView(),
      roster: buildRosterView(),
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

  return {
    requestListener, poll, markTaken, undo, getViewModel, startPolling, stopPolling,
    leagueKey, myTeamKey: myTeamKeyValue,
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
    server.once('error', reject);
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
