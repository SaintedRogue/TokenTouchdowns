import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createDraftRoom, startDraftRoomServer } from '../src/draft-room.js';
import { AnalyticsError } from '../src/analytics.js';
import { SessionExpiredError, YahooApiError } from '../src/client.js';
import { normalize } from '../src/normalize.js';

const fixture = async (n) =>
  normalize(JSON.parse(await readFile(new URL(`./fixtures/${n}.json`, import.meta.url), 'utf8')));

// For fixtures that are already plain JSON (not a Yahoo fantasy_content
// envelope) -- e.g. the real nflverse/Yahoo-names slices below -- so they
// must NOT go through normalize() (it expects an object, not an array, and
// would otherwise silently mangle a flat name/position/team map).
const jsonFixture = async (n) =>
  JSON.parse(await readFile(new URL(`./fixtures/${n}.json`, import.meta.url), 'utf8'));

// --- fixtures ----------------------------------------------------------------
//
// A small SYNTHETIC 6-player board -- big enough to exercise position
// filtering, taken/available, and roster-slot filling, small enough to read
// at a glance. Field names match analytics/src/tt/cli.py's real `board`
// subcommand output (see test/draft-room.test.js's own comment near
// `fakeAnalytics` for why this is a fake, not a mutant of production code).
const BOARD_PLAYERS = [
  { player_id: 'P1', name: 'Alpha Runner', position: 'RB', proj_points: 200, adp: 1, stdev: 0.5, vor: 50, tier: 1 },
  { player_id: 'P2', name: 'Bravo Wideout', position: 'WR', proj_points: 190, adp: 2, stdev: 0.6, vor: 45, tier: 1 },
  { player_id: 'P3', name: 'Charlie Passer', position: 'QB', proj_points: 300, adp: 5, stdev: 1.2, vor: 20, tier: 2 },
  { player_id: 'P4', name: 'Delta End', position: 'TE', proj_points: 120, adp: 8, stdev: 1.5, vor: 10, tier: 2 },
  { player_id: 'P5', name: 'Echo Runner', position: 'RB', proj_points: 150, adp: 10, stdev: 2, vor: 5, tier: 3 },
  { player_id: 'P6', name: 'Foxtrot Wideout', position: 'WR', proj_points: 140, adp: 12, stdev: 2, vor: 2, tier: 3 },
];

const LEAGUE_CONFIG = {
  leagueKey: '470.l.1', name: 'Test League', numTeams: 4, maxTeams: 10, draftStatus: 'predraft',
  rosterSlots: { QB: 1, RB: 2, WR: 1, TE: 1 },
  scoring: [],
};

/**
 * A fake `analytics` client (mirrors `createAnalyticsClient`'s shape:
 * `{run, runScript}`) -- no real spawn, no real Python, per the branch
 * instructions ("Inject BOTH fetch and spawn ... no network and no
 * subprocess"). `run('board', ...)` returns the fixture board ONCE, at
 * startup; `runScript` is a hand-written stand-in for
 * src/draft-room-recompute.py's own survival+recommend logic -- it does NOT
 * reimplement the real formulas (that would defeat the purpose: Python's own
 * pytest suite already covers `add_survival`/`recommend` and this feature
 * must not become a second implementation of either). It exists only to (a)
 * prove Node sends the right request shape, and (b) return a response Node's
 * OWN mapping/taken-flag logic can be asserted against deterministically.
 */
function fakeAnalytics({ boardPlayers = BOARD_PLAYERS, runScriptImpl, boardError, runScriptError } = {}) {
  const runCalls = [];
  const runScriptCalls = [];
  return {
    runCalls,
    runScriptCalls,
    async run(subcommand, opts) {
      runCalls.push({ subcommand, opts });
      if (boardError) throw boardError;
      return {
        season: 2026, teams: opts?.flags?.teams, slot: opts?.flags?.slot,
        pick: null, next_pick: null, adp_source: 'ffc', players: boardPlayers,
      };
    },
    async runScript(scriptPath, opts) {
      runScriptCalls.push({ scriptPath, opts });
      if (runScriptError) throw runScriptError;
      if (runScriptImpl) return runScriptImpl(opts.stdin);
      const { records, availableIds, n } = opts.stdin;
      const avail = new Set(availableIds ?? []);
      const board = records.map((r) => ({ ...r, p_gone_by_next: avail.has(r.player_id) ? 0.4 : null }));
      const ranked = records
        .filter((r) => avail.has(r.player_id))
        .slice().sort((a, b) => (b.vor ?? -Infinity) - (a.vor ?? -Infinity))
        .slice(0, n ?? 10)
        .map((r) => ({ ...r, p_gone_by_next: 0.4, expected_loss: (r.vor ?? 0) * 0.4 }));
      return { board, recommendations: ranked };
    },
  };
}

/** A fake Yahoo `client` (mirrors `createClient`'s `{get}` shape). Scripted
 * per-resource, one response (or thrown error) per call, consumed in order
 * so a test can script consecutive polls independently. */
function fakeClient(script = {}) {
  const calls = [];
  return {
    calls,
    async get(resource) {
      calls.push(resource);
      const key = Object.keys(script).find((k) => resource.startsWith(k));
      if (!key) throw new Error(`fakeClient: no script for resource "${resource}"`);
      const entry = script[key];
      const step = Array.isArray(entry) ? entry.shift() : entry;
      if (step instanceof Error) throw step;
      if (typeof step === 'function') return step();
      return step;
    },
  };
}

const TEAMS_RESPONSE = {
  league: {
    teams: [
      { team_key: '470.l.1.t.1', name: 'Someone Else', is_owned_by_current_login: 0 },
      { team_key: '470.l.1.t.2', name: 'Me', is_owned_by_current_login: 1 },
    ],
  },
};

// A synthetic `draft_results` payload for this fixture league (4 teams, 6
// rounds, snake), with the first `made` picks taken and the rest pending --
// exactly the shape Yahoo publishes from the very first poll (every pick
// slot for the whole draft, made or not; see src/draft-state.js). Made picks
// use player keys the crosswalk cannot resolve, which is fine here: the
// pick still counts and still advances the clock, which is all these tests
// need.
const TOTAL_PICKS = 24;
function draftResultsAfter(made) {
  const results = [];
  for (let pick = 1; pick <= TOTAL_PICKS; pick += 1) {
    const round = Math.floor((pick - 1) / 4) + 1;
    const inRound = (pick - 1) % 4;
    const slot = round % 2 === 1 ? inRound + 1 : 4 - inRound;
    const entry = { pick, round, team_key: `470.l.1.t.${slot}` };
    if (pick <= made) entry.player_key = `470.p.${1000 + pick}`;
    results.push(entry);
  }
  return { league: { draft_results: results } };
}

function emptyDraftResults() {
  return { league: { draft_results: [] } };
}

async function baseRoom(overrides = {}) {
  const client = overrides.client ?? fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': overrides.draftResultsScript ?? [emptyDraftResults()],
  });
  const analytics = overrides.analytics ?? fakeAnalytics(overrides.analyticsOpts);
  const room = await createDraftRoom({
    teams: 4, slot: 2, rounds: 6, league: '470.l.1',
    client, analytics,
    leagueConfig: overrides.leagueConfig ?? LEAGUE_CONFIG,
    crosswalk: overrides.crosswalk ?? new Map(),
    now: overrides.now,
    ...overrides.extra,
  });
  return { room, client, analytics };
}

// --- the state contract --------------------------------------------------

test('createDraftRoom builds the board exactly once at startup, not per poll', async () => {
  const { room, analytics } = await baseRoom();
  assert.equal(analytics.runCalls.length, 1, 'board built once at startup');
  assert.equal(analytics.runCalls[0].subcommand, 'board');
  await room.poll();
  await room.poll();
  assert.equal(analytics.runCalls.length, 1, 'still exactly one board build after two polls');
});

test('GET /api/state view model matches the documented contract for a predraft league', async () => {
  const { room } = await baseRoom();
  const view = room.getViewModel();

  assert.equal(view.status.draftStatus, 'predraft');
  assert.equal(view.status.picksMade, 0);
  assert.equal(view.status.currentPick, 1);
  assert.equal(view.status.myNextPick, 2); // myPicks(4,2,6) -> [2,7,10,15,18,23]
  assert.equal(view.status.isMyTurn, false);
  assert.equal(view.status.onTheClock, null);
  assert.equal(view.status.lastPollAt, null);
  assert.equal(view.status.banner, null);
  assert.equal(typeof view.status.lastPollOk, 'boolean');
  assert.equal(typeof view.status.staleSeconds, 'number');
  // How much of the board is trustworthy (this branch's fix): predraft,
  // nothing has been drafted yet, so there is nothing to report a rate over
  // -- null, not 0 (0% would misleadingly read as "the crosswalk is broken").
  assert.deepEqual(view.status.identity, { matched: 0, unresolved: 0, total: 0, rate: null });

  assert.ok(Array.isArray(view.recommendations));
  assert.ok(Array.isArray(view.board));
  assert.ok(view.board.length > 0);
  for (const key of ['playerId', 'name', 'position', 'proj', 'vor', 'tier', 'adp', 'pGone', 'taken', 'takenBy']) {
    assert.ok(key in view.board[0], `board row is missing "${key}"`);
  }
  for (const row of view.recommendations) {
    for (const key of ['playerId', 'name', 'position', 'vor', 'tier', 'adp', 'pGone', 'expectedLoss']) {
      assert.ok(key in row, `recommendation row is missing "${key}"`);
    }
  }
  assert.ok(Array.isArray(view.roster.slots));
  assert.ok(Array.isArray(view.roster.bench));
});

test('recommendations come from analytics.runScript, not a JS reimplementation', async () => {
  const { room, analytics } = await baseRoom();
  const view = room.getViewModel();
  assert.equal(analytics.runScriptCalls.length, 1);
  assert.ok(view.recommendations.length > 0);
  assert.equal(view.recommendations[0].playerId, 'P1'); // highest VOR, per the fake's own ranking
});

test('the initial recompute call sends every available player id and my empty roster', async () => {
  const { analytics } = await baseRoom();
  const { stdin } = analytics.runScriptCalls[0].opts;
  assert.deepEqual(new Set(stdin.availableIds), new Set(['P1', 'P2', 'P3', 'P4', 'P5', 'P6']));
  assert.deepEqual(stdin.roster, []);
  assert.equal(stdin.pick, 1);
  assert.equal(stdin.nextPick, 2);
  assert.equal(stdin.teams, 4);
  assert.equal(stdin.config.leagueKey, '470.l.1');
});

// --- manual override + undo ------------------------------------------------

test('POST /api/taken equivalent (room.markTaken) removes a player and shifts recommendations', async () => {
  const { room, analytics } = await baseRoom();
  const before = room.getViewModel();
  assert.equal(before.recommendations[0].playerId, 'P1');

  await room.markTaken('P1');
  const after = room.getViewModel();

  const boardRow = after.board.find((r) => r.playerId === 'P1');
  assert.equal(boardRow.taken, true);
  assert.equal(boardRow.takenBy, null, 'manual marks have no known team');
  assert.ok(!after.recommendations.some((r) => r.playerId === 'P1'), 'taken player no longer recommended');

  const lastCall = analytics.runScriptCalls.at(-1);
  assert.ok(!lastCall.opts.stdin.availableIds.includes('P1'));
});

test('marking an unknown player id is a harmless no-op (draft-state.js is already idempotent here)', async () => {
  const { room } = await baseRoom();
  const before = room.getViewModel();
  await room.markTaken('does-not-exist');
  const after = room.getViewModel();
  assert.deepEqual(after.board.map((r) => r.taken), before.board.map((r) => r.taken));
});

test('room.undo reverts the most recent manual mark and recommendations recover', async () => {
  const { room } = await baseRoom();
  await room.markTaken('P1');
  assert.ok(!room.getViewModel().recommendations.some((r) => r.playerId === 'P1'));

  await room.undo();
  const after = room.getViewModel();
  assert.equal(after.board.find((r) => r.playerId === 'P1').taken, false);
  assert.equal(after.recommendations[0].playerId, 'P1');
});

test('undo with nothing to undo is a no-op, not an error', async () => {
  const { room } = await baseRoom();
  await room.undo();
  assert.equal(room.getViewModel().status.banner, null);
});

// --- malformed draft_results: the loud-banner, never-stale-as-live path ----

test('a malformed draft_results entry raises an error banner and does not touch state', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [{ league: { draft_results: [{ nonsense: true }] } }],
  });
  const before = room.getViewModel();
  await room.poll();
  const after = room.getViewModel();

  assert.equal(after.status.banner.level, 'error');
  assert.match(after.status.banner.message, /1/); // names the count
  assert.equal(after.status.picksMade, before.status.picksMade);
  assert.equal(after.status.lastPollOk, false);
  assert.equal(after.status.lastPollAt, null, 'never present a stale/bad poll as a fresh one');
});

test('a well-formed pick alongside a malformed one still raises the banner and applies nothing', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [{
      league: {
        draft_results: [
          { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' },
          { garbage: 'yes' },
        ],
      },
    }],
  });
  await room.poll();
  const after = room.getViewModel();
  assert.equal(after.status.banner.level, 'error');
  assert.equal(after.status.picksMade, 0, 'the whole malformed-shaped response is untrusted, not partially applied');
});

// --- poll failure: keep last good state, staleness grows -------------------

test('a failing Yahoo poll keeps the last good state and sets lastPollOk false', async () => {
  let clock = 1_000_000;
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [
      emptyDraftResults(), // first poll succeeds
      new YahooApiError('rate limited', 999), // second poll fails
    ],
  });
  const { room } = await baseRoom({ client, now: () => clock });
  await room.poll(); // succeeds, sets lastPollAt
  const goodPollAt = room.getViewModel().status.lastPollAt;
  assert.ok(goodPollAt);

  clock += 14_000;
  await room.poll(); // fails
  const view = room.getViewModel();
  assert.equal(view.status.lastPollOk, false);
  assert.equal(view.status.lastPollAt, goodPollAt, 'lastPollAt is frozen at the last GOOD poll');
  assert.ok(view.status.staleSeconds >= 14, `staleSeconds should have grown, got ${view.status.staleSeconds}`);
  assert.equal(view.status.banner.level, 'warn');
});

test('the poll loop never throws, even on an unexpected error type', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [new TypeError('boom, something truly unexpected')],
  });
  const { room } = await baseRoom({ client });
  await assert.doesNotReject(() => room.poll());
  assert.equal(room.getViewModel().status.lastPollOk, false);
});

// --- session expiry: loud banner naming the fix, manual override survives --

test('an expired session sets a banner naming the re-login command, and manual override keeps working', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [new SessionExpiredError('Yahoo returned 401')],
  });
  const { room } = await baseRoom({ client });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.banner.level, 'error');
  assert.match(view.status.banner.message, /tt login/);
  assert.equal(view.status.lastPollOk, false);

  // manual override still works after a dead session
  await room.markTaken('P2');
  assert.equal(room.getViewModel().board.find((r) => r.playerId === 'P2').taken, true);
});

// --- python engine failure ---------------------------------------------------

test('the python engine failing at startup fails the whole startup loudly', async () => {
  const analytics = fakeAnalytics({ boardError: new AnalyticsError('venv missing') });
  await assert.rejects(
    () => baseRoom({ analytics }),
    (e) => { assert.ok(e instanceof AnalyticsError); return true; },
  );
});

test('the python engine failing on a later recompute keeps the last good board and banners the error', async () => {
  const analytics = fakeAnalytics();
  const { room } = await baseRoom({ analytics });
  const before = room.getViewModel();
  assert.equal(before.recommendations[0].playerId, 'P1');

  const originalRunScript = analytics.runScript.bind(analytics);
  analytics.runScript = async () => { throw new AnalyticsError('recommend() blew up'); };

  await room.markTaken('P6'); // any state change triggers a recompute attempt
  const after = room.getViewModel();
  assert.deepEqual(after.recommendations, before.recommendations, 'stale-but-good recommendations are kept');
  assert.equal(after.status.banner.level, 'warn');
  assert.match(after.status.banner.message, /recommend\(\) blew up/);

  analytics.runScript = originalRunScript;
});

// --- applying real Yahoo picks + the yahoo<->engine crosswalk --------------

test('a well-formed Yahoo pick for MY team advances state and fills my roster', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [
      { league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.900' },
                                    { pick: 2, team_key: '470.l.1.t.2', player_key: '470.p.901' }] } },
    ],
    crosswalk: new Map([['900', { gsisId: 'P3', name: 'Charlie Passer', position: 'QB' }],
                         ['901', { gsisId: 'P1', name: 'Alpha Runner', position: 'RB' }]]),
  });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.picksMade, 2);
  assert.equal(view.status.currentPick, 3);
  assert.equal(view.board.find((r) => r.playerId === 'P3').takenBy, '470.l.1.t.1');
  assert.equal(view.board.find((r) => r.playerId === 'P1').takenBy, '470.l.1.t.2');
  assert.ok(view.roster.slots.some((s) => s.player && s.player.playerId === 'P1'));
});

test('an unresolvable yahoo player key still advances the pick count without corrupting the pool', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [
      { league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.999999' }] } },
    ],
    crosswalk: new Map(), // nothing resolves
  });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.picksMade, 1, 'the pick still counts');
  assert.equal(view.status.currentPick, 2, 'the clock still advances');
  // No board row is a match for the unresolved id, so nothing on the visible
  // board is (wrongly) marked taken -- design doc 4.2's documented degrade.
  assert.ok(view.board.every((r) => r.taken === false));
});

// --- MEASURED DEFECT: Sleeper gsis_id alone resolves only ~22% of a real ---
// --- draft (see this branch's report) -- src/cli.js's resolveRosterIdentity
// --- already fixed the identical problem for `tt lineup`/`tt playoff` with a
// --- two-pass resolution (Sleeper gsis_id, then a name+position+team match
// --- against nflverse via the tested buildAdpIndex/matchAdp, src/identity.js
// --- -- see that function's own docstring). This proves draft-room reuses
// --- the SAME matcher, through the room's real poll -> resolveAndApply path,
// --- against the REAL captured draft_results shape (test/fixtures/
// --- league-draftresults-late.json, 131 real picks from a real Yahoo mock
// --- draft) -- not a synthetic stand-in.
//
// UNLIKE `tt lineup`'s roster resource, `draft_results` carries no player
// name at all -- only `player_key` -- so pass 2's query can only ever be the
// Sleeper crosswalk record's OWN name/position/team, never Yahoo's. Picks 2
// and 4 of this real draft (Josh Allen / Jonathan Taylor) are wired with a
// crosswalk mirroring their REAL Sleeper data one-for-one: Josh Allen carries
// a real gsis_id (resolves via pass 1); Jonathan Taylor is Sleeper's own
// documented gap -- present, but gsis_id: null (resolves via pass 2 only).
// Every other one of the 131 real yahoo ids is deliberately left out of the
// crosswalk, mirroring the real, measured situation for most of this draft.
test('the real captured LATE-draft payload: the two-pass crosswalk (Sleeper gsisId, then nflverse name fallback) resolves picks gsisId alone cannot, and every pick still counts whether it resolves or not', async () => {
  const { league } = await fixture('league-draftresults-late');
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-nflverse-'));
  try {
    const nflverseRosterPath = path.join(dir, 'nflverse_players.json');
    await writeFile(nflverseRosterPath, JSON.stringify([
      { playerId: '00-0036223', name: 'Jonathan Taylor', position: 'RB', team: 'IND' },
    ]));

    // '30977' and '32711' are pick 2 and pick 4's REAL yahoo player ids in
    // this capture (verified directly against test/fixtures/
    // league-draftresults-late.json).
    const crosswalk = new Map([
      ['30977', { gsisId: '00-0034857', name: 'Josh Allen', position: 'QB', team: 'BUF' }],
      ['32711', { gsisId: null, name: 'Jonathan Taylor', position: 'RB', team: 'IND' }],
    ]);

    const boardPlayers = [
      ...BOARD_PLAYERS,
      { player_id: '00-0034857', name: 'Josh Allen', position: 'QB', proj_points: 280, adp: 3, stdev: 1, vor: 30, tier: 1 },
      { player_id: '00-0036223', name: 'Jonathan Taylor', position: 'RB', proj_points: 260, adp: 4, stdev: 1, vor: 40, tier: 1 },
    ];

    const client = {
      calls: [],
      async get(resource) {
        this.calls.push(resource);
        if (resource.startsWith('league/470.l.1433972/teams')) {
          return {
            league: {
              teams: Array.from({ length: 14 }, (_, i) => ({
                team_key: `470.l.1433972.t.${i + 1}`,
                name: `Team ${i + 1}`,
                is_owned_by_current_login: i + 1 === 5 ? 1 : 0,
              })),
            },
          };
        }
        if (resource.startsWith('league/470.l.1433972/draftresults')) {
          return { league: { draft_results: league.draft_results } };
        }
        throw new Error(`unscripted resource: ${resource}`);
      },
    };
    const analytics = fakeAnalytics({ boardPlayers });
    const room = await createDraftRoom({
      teams: 14, slot: 5, rounds: 15, league: '470.l.1433972',
      client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk, nflverseRosterPath,
    });
    await room.poll();
    const { status, board } = room.getViewModel();

    // LEVEL assertions (the whole point of this defect): every one of the
    // 131 real made picks must still count, resolved or not --
    assert.equal(status.picksMade, 131, 'every made pick still counts, resolved or not (arithmetic never degrades)');
    // -- but exactly 2 of them (Josh Allen via gsisId, Jonathan Taylor via
    // the nflverse fallback) are trustworthy enough to strike off the board.
    assert.equal(status.identity.total, 131);
    assert.equal(status.identity.matched, 2, 'gsisId pass (Josh Allen) + nflverse fallback pass (Jonathan Taylor)');

    assert.equal(board.find((r) => r.playerId === '00-0034857').taken, true, 'Josh Allen resolved via gsisId (pass 1)');
    assert.equal(board.find((r) => r.playerId === '00-0036223').taken, true, 'Jonathan Taylor resolved via the nflverse name fallback (pass 2) -- gsisId alone leaves this player on the board, wrongly, on draft day');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --- THE ACTUAL ROOT CAUSE FIX: `draft_results` carries no player name at --
// --- all, only `player_key` -- so passes 1-2 above can only ever fire when
// --- Sleeper's own yahoo_id join already holds a record for that player,
// --- true for only 60/210 (28.6%) of a real draft (draft-room-crosswalk-
// --- report.md). PASS 3 asks Yahoo itself, via the bulk
// --- `players;player_keys=` resource (verified live against a real session:
// --- 25/50/100 keys requested -> 25/50/100 returned), batched at <=100 per
// --- request, and feeds Yahoo's own name/position/team into the SAME tested
// --- matchAdp/nflverseIndex matcher pass 2 already uses -- no new matching
// --- logic anywhere in this fix.
//
// Every piece of data driving this test is REAL, not synthetic: the full
// 210-pick draft_results capture (test/fixtures/league-draftresults-full.json,
// same real Yahoo mock draft as the LATE-payload test above, this time
// complete); the crosswalk is deliberately EMPTY, so passes 1-2 contribute
// NOTHING here and pass 3 is measured in isolation; the Yahoo names
// (test/fixtures/yahoo-players-full.json) were fetched live, moments before
// writing this test, from the real Yahoo session for all 210 real
// player_keys in this capture via this exact `players;player_keys=`
// resource; and the nflverse rows (test/fixtures/nflverse-players-full.json)
// are the exact subset of the real, checked-out analytics/data/
// nflverse_players.json that those 210 real names resolve against.
test("the real captured FULL 210-pick payload: batched Yahoo players;player_keys= (pass 3) resolves the picks an empty crosswalk cannot -- team defenses and kickers are the only residue, both excluded from the board by the analytics engine's own design (PROJECTABLE_POSITIONS is QB/RB/WR/TE only)", async () => {
  const { league } = await fixture('league-draftresults-full');
  const nflverseRecords = await jsonFixture('nflverse-players-full');
  const yahooNames = await jsonFixture('yahoo-players-full');

  // Every board row this test needs is exactly the 166 real players pass 3
  // resolves to -- proj/adp/vor are placeholders (this test is about
  // identity resolution, not ranking); see fakeAnalytics's own docstring for
  // why board-building logic itself is never reimplemented here.
  const boardPlayers = nflverseRecords.map((r, i) => ({
    player_id: r.playerId, name: r.name, position: r.position,
    proj_points: 100, adp: i + 1, stdev: 1, vor: 200 - i, tier: 1,
  }));

  const client = {
    calls: [],
    async get(resource) {
      this.calls.push(resource);
      if (resource.startsWith('league/470.l.1433972/teams')) {
        return {
          league: {
            teams: Array.from({ length: 14 }, (_, i) => ({
              team_key: `470.l.1433972.t.${i + 1}`,
              name: `Team ${i + 1}`,
              is_owned_by_current_login: i + 1 === 5 ? 1 : 0,
            })),
          },
        };
      }
      if (resource.startsWith('league/470.l.1433972/draftresults')) {
        return { league: { draft_results: league.draft_results } };
      }
      if (resource.startsWith('players;player_keys=')) {
        const keys = resource.slice('players;player_keys='.length).split(',');
        assert.ok(keys.length <= 100, `pass 3 must never request more than 100 keys at once, got ${keys.length}`);
        const players = keys
          .filter((k) => yahooNames[k])
          .map((k) => ({
            player_key: k,
            name: { full: yahooNames[k].name },
            display_position: yahooNames[k].position,
            editorial_team_abbr: yahooNames[k].team,
          }));
        return { players };
      }
      throw new Error(`unscripted resource: ${resource}`);
    },
  };
  const analytics = fakeAnalytics({ boardPlayers });
  const room = await createDraftRoom({
    teams: 14, slot: 5, rounds: 15, league: '470.l.1433972',
    client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(),
  });
  await room.poll();
  const { status, board } = room.getViewModel();

  const playersCalls = client.calls.filter((r) => r.startsWith('players;player_keys='));
  assert.equal(playersCalls.length, 3, 'all 210 new keys batched into ceil(210/100) = 3 requests, never one per pick');

  // LEVEL assertions (the whole point of this task): every one of the 210
  // real made picks must still count, resolved or not --
  assert.equal(status.picksMade, 210, 'every made pick still counts, resolved or not (arithmetic never degrades)');
  assert.equal(status.identity.total, 210);
  // -- and pass 3, entirely on its own (no Sleeper crosswalk at all), takes
  // resolution from 0 to 166/210 (79.0%): every real, currently-projectable
  // (QB/RB/WR/TE) player in this draft. The residue (44) is NOT rounded
  // away: 15 team defenses (never name-matched by buildAdpIndex's own
  // design) + 15 kickers + 14 real players with no history yet in the
  // nflverse export (rookies) -- all 30 DEF/K are additionally excluded from
  // the board itself by the Python engine's PROJECTABLE_POSITIONS, so no
  // identity fix, however complete, could ever mark them taken.
  assert.equal(status.identity.matched, 166, '210 - 15 DEF - 15 K - 14 not-yet-in-nflverse rookies');
  assert.equal(status.identity.unresolved, 44);

  assert.equal(board.find((r) => r.playerId === '00-0039139').taken, true, 'pick 1, Jahmyr Gibbs, resolved via pass 3 (Yahoo name -> nflverse match)');
  assert.equal(board.find((r) => r.playerId === '00-0034857').taken, true, 'pick 2, Josh Allen, resolved via pass 3');
  assert.ok(board.every((r) => r.taken), 'every one of the 166 board rows in this test is a real resolved pick -- none is left wrongly available');
});

// --- MECHANISM: caching, batching, and failure tolerance --------------------

test('a pick already resolved via Yahoo names is never re-fetched on a later poll (cached for the life of the room)', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-yahoo-names-cache-'));
  try {
    const nflverseRosterPath = path.join(dir, 'nflverse_players.json');
    await writeFile(nflverseRosterPath, JSON.stringify([
      { playerId: 'GIBBS', name: 'Jahmyr Gibbs', position: 'RB', team: 'DET' },
    ]));
    const client = fakeClient({
      'league/470.l.1/teams': TEAMS_RESPONSE,
      'league/470.l.1/draftresults': [
        { league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.40059' }] } },
        { league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.40059' }] } },
      ],
      'players;player_keys=470.p.40059': {
        players: [{ player_key: '470.p.40059', name: { full: 'Jahmyr Gibbs' }, display_position: 'RB', editorial_team_abbr: 'DET' }],
      },
    });
    const boardPlayers = [...BOARD_PLAYERS,
      { player_id: 'GIBBS', name: 'Jahmyr Gibbs', position: 'RB', proj_points: 300, adp: 1, stdev: 1, vor: 60, tier: 1 }];
    const analytics = fakeAnalytics({ boardPlayers });
    const room = await createDraftRoom({
      teams: 4, slot: 2, rounds: 6, league: '470.l.1', client, analytics,
      leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(), nflverseRosterPath,
    });

    await room.poll();
    assert.equal(client.calls.filter((r) => r.startsWith('players;player_keys=')).length, 1);
    assert.equal(room.getViewModel().status.identity.matched, 1);

    await room.poll(); // same pick, still made, on the very next poll
    assert.equal(client.calls.filter((r) => r.startsWith('players;player_keys=')).length, 1,
      'the second poll must not re-fetch an already-cached key -- a player\'s identity never changes mid-draft');
    assert.equal(room.getViewModel().status.identity.matched, 1);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('an incremental draft only ever fetches the keys that are NEW since the last poll, never re-requesting an already-seen one', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-yahoo-names-incremental-'));
  try {
    const nflverseRosterPath = path.join(dir, 'nflverse_players.json');
    await writeFile(nflverseRosterPath, JSON.stringify([
      { playerId: 'X1', name: 'Player One', position: 'RB', team: 'AAA' },
      { playerId: 'X2', name: 'Player Two', position: 'WR', team: 'BBB' },
    ]));
    const namesByKey = {
      '470.p.1': { name: 'Player One', position: 'RB', team: 'AAA' },
      '470.p.2': { name: 'Player Two', position: 'WR', team: 'BBB' },
    };
    let pollCount = 0;
    const client = {
      calls: [],
      async get(resource) {
        this.calls.push(resource);
        if (resource.startsWith('league/470.l.1/teams')) return TEAMS_RESPONSE;
        if (resource.startsWith('league/470.l.1/draftresults')) {
          pollCount += 1;
          const picks = pollCount === 1
            ? [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' }]
            : [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' },
                { pick: 2, team_key: '470.l.1.t.2', player_key: '470.p.2' }];
          return { league: { draft_results: picks } };
        }
        if (resource.startsWith('players;player_keys=')) {
          const keys = resource.slice('players;player_keys='.length).split(',');
          return {
            players: keys.filter((k) => namesByKey[k]).map((k) => ({
              player_key: k, name: { full: namesByKey[k].name },
              display_position: namesByKey[k].position, editorial_team_abbr: namesByKey[k].team,
            })),
          };
        }
        throw new Error(`unscripted resource: ${resource}`);
      },
    };
    const boardPlayers = [...BOARD_PLAYERS,
      { player_id: 'X1', name: 'Player One', position: 'RB', proj_points: 100, adp: 1, stdev: 1, vor: 10, tier: 1 },
      { player_id: 'X2', name: 'Player Two', position: 'WR', proj_points: 90, adp: 2, stdev: 1, vor: 8, tier: 1 }];
    const analytics = fakeAnalytics({ boardPlayers });
    const room = await createDraftRoom({
      teams: 4, slot: 2, rounds: 6, league: '470.l.1', client, analytics,
      leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(), nflverseRosterPath,
    });

    await room.poll();
    assert.deepEqual(client.calls.filter((r) => r.startsWith('players;player_keys=')), ['players;player_keys=470.p.1']);

    await room.poll();
    const allPlayersCalls = client.calls.filter((r) => r.startsWith('players;player_keys='));
    assert.deepEqual(allPlayersCalls, ['players;player_keys=470.p.1', 'players;player_keys=470.p.2'],
      'only the NEW key (470.p.2) is fetched on the second poll -- 470.p.1 stays cached, never re-requested');
    assert.equal(room.getViewModel().status.identity.matched, 2);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('a failing or rate-limited players;player_keys= fetch never throws -- the poll keeps the last good state, those picks stay unresolved, and arithmetic is unaffected; a later poll retries and can still recover', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-yahoo-names-failure-'));
  try {
    const nflverseRosterPath = path.join(dir, 'nflverse_players.json');
    await writeFile(nflverseRosterPath, JSON.stringify([
      { playerId: 'X1', name: 'Player One', position: 'RB', team: 'AAA' },
      { playerId: 'X2', name: 'Player Two', position: 'WR', team: 'BBB' },
    ]));
    const client = fakeClient({
      'league/470.l.1/teams': TEAMS_RESPONSE,
      'league/470.l.1/draftresults': {
        league: {
          draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' },
                            { pick: 2, team_key: '470.l.1.t.2', player_key: '470.p.2' }],
        },
      },
      // First poll's fetch is rate-limited outright; the second succeeds.
      'players;player_keys=': [
        new YahooApiError('rate limited', 999),
        {
          players: [
            { player_key: '470.p.1', name: { full: 'Player One' }, display_position: 'RB', editorial_team_abbr: 'AAA' },
            { player_key: '470.p.2', name: { full: 'Player Two' }, display_position: 'WR', editorial_team_abbr: 'BBB' },
          ],
        },
      ],
    });
    const boardPlayers = [...BOARD_PLAYERS,
      { player_id: 'X1', name: 'Player One', position: 'RB', proj_points: 100, adp: 1, stdev: 1, vor: 10, tier: 1 },
      { player_id: 'X2', name: 'Player Two', position: 'WR', proj_points: 90, adp: 2, stdev: 1, vor: 8, tier: 1 }];
    const analytics = fakeAnalytics({ boardPlayers });
    const room = await createDraftRoom({
      teams: 4, slot: 2, rounds: 6, league: '470.l.1', client, analytics,
      leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(), nflverseRosterPath,
    });

    await assert.doesNotReject(() => room.poll(), 'a rate-limited name fetch must never reject the poll itself');
    let view = room.getViewModel();
    assert.equal(view.status.picksMade, 2, 'both picks still counted even though the name fetch failed outright');
    assert.equal(view.status.lastPollOk, true, 'Yahoo draft data itself came back fine -- only the optional name enrichment failed');
    assert.equal(view.status.banner, null, 'no banner: an enrichment-only failure is not the loud, blocking kind design doc section 5 reserves for a broken draftresults poll');
    assert.equal(view.status.identity.matched, 0, 'nothing could resolve -- crosswalk empty, and the one fetch that could have supplied names failed');

    await room.poll(); // same two picks, still not cached -- retried, and this time it succeeds
    view = room.getViewModel();
    assert.equal(view.status.picksMade, 2);
    assert.equal(view.status.identity.matched, 2, 'a later poll retries an unresolved key and recovers once Yahoo answers');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --- roster/status shape -----------------------------------------------------

test('roster view lists every configured slot, filled or empty, plus a bench', async () => {
  const { room } = await baseRoom();
  const view = room.getViewModel();
  const slotNames = view.roster.slots.map((s) => s.slot);
  assert.deepEqual(slotNames, ['QB', 'RB', 'RB', 'WR', 'TE']); // from LEAGUE_CONFIG.rosterSlots
  assert.ok(view.roster.slots.every((s) => s.player === null), 'nobody drafted yet');
});

test('isMyTurn and onTheClock reflect whose pick it currently is', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [{ league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' }] } }],
    crosswalk: new Map(),
  });
  await room.poll(); // currentPick becomes 2 -- exactly my next pick (slot 2, teams 4)
  const view = room.getViewModel();
  assert.equal(view.status.currentPick, 2);
  assert.equal(view.status.myNextPick, 2);
  assert.equal(view.status.isMyTurn, true);
  assert.equal(view.status.onTheClock, '470.l.1.t.2');
});

// --- Yahoo's published draft order beats our own snake model (design doc ---
// --- 4.1/4.2 update): currentPick/onTheClock/myNextPick derive from
// --- draft_results' `pending` group whenever it's non-empty; the snake math
// --- (myPicks/nextPick) is only the fallback. These prove the room's OWN
// --- wiring (poll -> resolveAndApply -> applyPicks -> buildStatus) actually
// --- takes that path end to end, not just src/draft-state.js in isolation.

test('the room prefers Yahoo\'s pending-derived clock over the count-based fallback when they disagree (out-of-order poll)', async () => {
  // Pick 4 is still pending while pick 5 is already made (a slow poll
  // caught two picks in one response, out of order). The count-based
  // fallback would say currentPick=5/onTheClock unknown; Yahoo's own order
  // says pick 4 -- team 4 -- is still on the clock. If the room silently
  // fell through to the fallback here, onTheClock/currentPick below would
  // be wrong (or the snake fallback's onTheClock, which can only ever be
  // null or MY team, could never even report team 4 at all).
  const { room } = await baseRoom({
    draftResultsScript: [{
      league: {
        draft_results: [
          { pick: 1, round: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' },
          { pick: 2, round: 1, team_key: '470.l.1.t.2', player_key: '470.p.2' },
          { pick: 3, round: 1, team_key: '470.l.1.t.3', player_key: '470.p.3' },
          { pick: 5, round: 1, team_key: '470.l.1.t.1', player_key: '470.p.5' }, // pick 4 skipped
          { pick: 4, round: 1, team_key: '470.l.1.t.4' }, // pending -- no player_key
          { pick: 6, round: 2, team_key: '470.l.1.t.2' }, // pending
          { pick: 7, round: 2, team_key: '470.l.1.t.3' }, // pending
        ],
      },
    }],
    crosswalk: new Map(),
  });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.picksMade, 4, 'four made picks (1,2,3,5)');
  assert.equal(view.status.currentPick, 4, 'the lowest PENDING pick (4) wins, not drafted.size+1 (5)');
  assert.equal(view.status.currentRound, 1);
  assert.equal(view.status.onTheClock, '470.l.1.t.4', 'Yahoo names team 4 directly -- impossible via the snake fallback alone');
});

test('the room surfaces round alongside pick numbers when Yahoo supplies it (currentRound / myNextPickRound)', async () => {
  const { room } = await baseRoom({
    draftResultsScript: [{
      league: {
        draft_results: [
          { pick: 1, round: 1, team_key: '470.l.1.t.1', player_key: '470.p.1' },
          { pick: 2, round: 1, team_key: '470.l.1.t.2' }, // pending, mine (myTeamKey is 470.l.1.t.2)
          { pick: 3, round: 1, team_key: '470.l.1.t.3' }, // pending
        ],
      },
    }],
    crosswalk: new Map(),
  });
  await room.poll();
  const { status } = room.getViewModel();
  assert.equal(status.currentPick, 2);
  assert.equal(status.currentRound, 1);
  assert.equal(status.myNextPick, 2);
  assert.equal(status.myNextPickRound, 1);
});

test('the room falls back to snake-derived currentRound when draft_results carries no round (or is empty/unavailable)', async () => {
  // Predraft (design doc's FALLBACK case): pending is empty because no poll
  // has ever produced a draft_results entry at all yet.
  const { room } = await baseRoom(); // default: empty draft_results, teams=4
  const { status } = room.getViewModel();
  assert.equal(status.currentPick, 1);
  assert.equal(status.currentRound, 1, 'round 1 of a 4-team snake, computed from teams -- not Yahoo');
});

// --- the real captured payload, through the whole room, not just draft-state
// --- .js in isolation: this is DEFECT 1 (209-false-malformed) and DEFECT 2
// --- (derive order from Yahoo) exercised end to end, exactly as they'd hit
// --- production on draft day.

test('a real captured mid-draft poll (8 made / 202 pending, 14 teams) never raises the malformed banner and derives the correct on-the-clock team/round', async () => {
  const { league } = await fixture('league-draftresults-mid');
  const client = {
    calls: [],
    async get(resource) {
      this.calls.push(resource);
      if (resource.startsWith('league/470.l.1433972/teams')) {
        return {
          league: {
            teams: Array.from({ length: 14 }, (_, i) => ({
              team_key: `470.l.1433972.t.${i + 1}`,
              name: `Team ${i + 1}`,
              is_owned_by_current_login: i + 1 === 5 ? 1 : 0,
            })),
          },
        };
      }
      if (resource.startsWith('league/470.l.1433972/draftresults')) {
        return { league: { draft_results: league.draft_results } };
      }
      throw new Error(`unscripted resource: ${resource}`);
    },
  };
  const analytics = fakeAnalytics();
  const room = await createDraftRoom({
    teams: 14, slot: 5, rounds: 15, league: '470.l.1433972',
    client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(),
  });
  await room.poll();
  const { status } = room.getViewModel();

  assert.equal(status.banner, null, 'a real fully-pending-heavy draft must never trip the malformed banner');
  assert.equal(status.lastPollOk, true);
  assert.equal(status.picksMade, 8);
  // Verified directly against the capture (round 1: p1:t1 ... p14:t14):
  // pick 9 belongs to team 9, and team 5's round-1 pick (5) is already
  // made, so its next is round 2's mirrored slot, pick 24 (round 2 reverses:
  // p15:t14 p16:t13 ... p28:t1 -- team 5 sits at p24).
  assert.equal(status.currentPick, 9);
  assert.equal(status.currentRound, 1);
  assert.equal(status.onTheClock, '470.l.1433972.t.9');
  assert.equal(status.myNextPick, 24);
  assert.equal(status.myNextPickRound, 2);
});

// --- HTTP layer: binds to 127.0.0.1, serves the documented routes ----------

test('startDraftRoomServer binds to 127.0.0.1 only and serves GET /, /api/state, and 404s elsewhere', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [emptyDraftResults()],
  });
  const analytics = fakeAnalytics();
  const started = await startDraftRoomServer({
    port: 0, teams: 4, slot: 2, rounds: 6, league: '470.l.1',
    client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(),
    pollSeconds: 3600, // don't let the interval fire mid-test
  });
  try {
    assert.equal(started.server.address().address, '127.0.0.1');

    const html = await fetch(started.url);
    assert.equal(html.status, 200);
    assert.match(html.headers.get('content-type'), /html/);
    const htmlText = await html.text();
    assert.match(htmlText, /<title>/);
    // The page must actually render the resolution-rate number this branch
    // adds (design intent: "a silent 78% miss is what made this defect
    // invisible; a visible number makes it self-reporting") -- not just
    // carry it unused in the JSON.
    assert.match(htmlText, /id="identity"/);
    assert.match(htmlText, /renderIdentity/);

    const state = await fetch(new URL('/api/state', started.url));
    assert.equal(state.status, 200);
    const body = await state.json();
    assert.ok(body.status && body.board && body.roster);
    assert.ok(body.status.identity, '/api/state surfaces the identity-resolution rate under status');

    const missing = await fetch(new URL('/nope', started.url));
    assert.equal(missing.status, 404);
  } finally {
    await started.close();
  }
});

test('POST /api/taken over real HTTP marks a player and returns the updated view model', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [emptyDraftResults()],
  });
  const analytics = fakeAnalytics();
  const started = await startDraftRoomServer({
    port: 0, teams: 4, slot: 2, rounds: 6, league: '470.l.1',
    client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(),
    pollSeconds: 3600,
  });
  try {
    const res = await fetch(new URL('/api/taken', started.url), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playerId: 'P1' }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.board.find((r) => r.playerId === 'P1').taken, true);

    const undoRes = await fetch(new URL('/api/undo', started.url), { method: 'POST' });
    assert.equal(undoRes.status, 200);
    const undone = await undoRes.json();
    assert.equal(undone.board.find((r) => r.playerId === 'P1').taken, false);
  } finally {
    await started.close();
  }
});

test('POST /api/taken with no playerId is a 400, not a crash', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [emptyDraftResults()],
  });
  const analytics = fakeAnalytics();
  const started = await startDraftRoomServer({
    port: 0, teams: 4, slot: 2, rounds: 6, league: '470.l.1',
    client, analytics, leagueConfig: LEAGUE_CONFIG, crosswalk: new Map(),
    pollSeconds: 3600,
  });
  try {
    const res = await fetch(new URL('/api/taken', started.url), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    assert.equal(res.status, 400);
  } finally {
    await started.close();
  }
});

// --- crosswalk built from a real (cached) sleeper payload -------------------

test('createDraftRoom builds its crosswalk from the cached sleeper payload when none is injected', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-'));
  try {
    await writeFile(path.join(dir, 'sleeper.json'), JSON.stringify({
      fetchedAt: Date.now(),
      data: [{ yahooId: '900', gsisId: 'P3', name: 'Charlie Passer', position: 'QB' }],
    }));
    const client = fakeClient({
      'league/470.l.1/teams': TEAMS_RESPONSE,
      'league/470.l.1/draftresults': [
        { league: { draft_results: [{ pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.900' }] } },
      ],
    });
    const analytics = fakeAnalytics();
    const room = await createDraftRoom({
      teams: 4, slot: 2, rounds: 6, league: '470.l.1', client, analytics,
      leagueConfig: LEAGUE_CONFIG, cacheDir: dir,
    });
    await room.poll();
    const view = room.getViewModel();
    assert.equal(view.board.find((r) => r.playerId === 'P3').takenBy, '470.l.1.t.1');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('createDraftRoom degrades to an empty crosswalk when no sleeper cache exists, without throwing', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'tt-draft-room-empty-'));
  try {
    const client = fakeClient({
      'league/470.l.1/teams': TEAMS_RESPONSE,
      'league/470.l.1/draftresults': [emptyDraftResults()],
    });
    const analytics = fakeAnalytics();
    // No `crosswalk` option at all -- must fall back to reading (a
    // nonexistent) sleeper.json from `cacheDir`, not throw.
    const room = await createDraftRoom({
      teams: 4, slot: 2, rounds: 6, league: '470.l.1', client, analytics,
      leagueConfig: LEAGUE_CONFIG, cacheDir: dir,
    });
    assert.equal(room.getViewModel().status.draftStatus, 'predraft');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// --- the guidance block (the redesign) ---------------------------------------
//
// docs/positional-value.md measured that filling the lineup is worth +63 to
// +80 actual points in this 4-team league -- more than any positional
// strategy -- while the largest waiting cost anywhere in a 4-team draft is 20
// points. The old page led with a big red survival percentage (the small
// effect) and buried roster completion (the large one) in a grey panel.
// `guidance` is what inverts that, and these tests hold the inversion in
// place. The arithmetic itself lives in src/draft-guidance.js, unit-tested
// there; what is asserted here is that the room WIRES it to real state.

test('/api/state carries a guidance block: hero, cliffs, runway, urgency, replacement', async () => {
  const { room } = await baseRoom();
  const { guidance } = room.getViewModel();
  for (const key of ['hero', 'cliffs', 'runway', 'urgency', 'replacement', 'league']) {
    assert.ok(key in guidance, `guidance is missing "${key}"`);
  }
  assert.deepEqual(guidance.cliffs.map((c) => c.position), ['RB', 'WR', 'TE', 'QB']);
  assert.deepEqual(guidance.league, { teams: 4, rounds: 6, slot: 2 });
});

test('the hero is the engine\'s own top recommendation -- the page never re-ranks', async () => {
  const { room } = await baseRoom();
  const view = room.getViewModel();
  assert.equal(view.guidance.hero.playerId, view.recommendations[0].playerId);
  assert.equal(view.guidance.hero.name, view.recommendations[0].name);
  assert.equal(view.guidance.hero.reasons.length, 4);
  assert.deepEqual(view.guidance.hero.reasons.map((r) => r.kind), ['cliff', 'survival', 'need', 'runway']);
});

test('the cliff strip counts only players still on the board, and drains as picks are made', async () => {
  const { room } = await baseRoom();
  const before = room.getViewModel().guidance.cliffs.find((c) => c.position === 'RB');
  assert.equal(before.remaining, 2); // P1 and P5, both inside RB12
  await room.markTaken('P1');
  const after = room.getViewModel().guidance.cliffs.find((c) => c.position === 'RB');
  assert.equal(after.remaining, 1);
  assert.equal(after.afterRank, 12);
  assert.equal(after.drop, 68.0);
});

test('THE HONESTY RULE, end to end: this 4-team room never renders an alarm-level urgency', async () => {
  // Not a restatement of the unit test -- this drives the ROOM through every
  // round of its own 24-pick draft, via Yahoo's own draft_results, and
  // asserts the urgency level it actually publishes at each one.
  const script = [];
  for (let n = 0; n <= TOTAL_PICKS; n += 1) script.push(draftResultsAfter(n));
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': script,
  });
  const { room } = await baseRoom({ client });
  const seen = new Set();
  for (let n = 0; n <= TOTAL_PICKS; n += 1) {
    // eslint-disable-next-line no-await-in-loop
    await room.poll();
    const { guidance, status } = room.getViewModel();
    seen.add(status.currentRound);
    assert.notEqual(guidance.urgency.level, 'alarm',
      `round ${status.currentRound}: ${JSON.stringify(guidance.urgency)}`);
  }
  assert.ok(seen.has(6), `never actually reached the last round (saw ${[...seen]})`);
});

test('urgency names the measured table it read and never interpolates the room\'s own team count', async () => {
  const { room } = await baseRoom({ extra: { teams: 14, slot: 3 } });
  const { urgency } = room.getViewModel().guidance;
  assert.equal(urgency.teams, 14);
  assert.equal(urgency.basisTeams, 10);
  assert.ok(Number.isFinite(urgency.byPosition.RB));
});

test('the runway counts unfilled starting slots and leaves K/DEF out of the count', async () => {
  const { room } = await baseRoom({
    extra: { rounds: 12 },
    leagueConfig: { ...LEAGUE_CONFIG, rosterSlots: { QB: 1, RB: 2, WR: 1, TE: 1, K: 1, DEF: 1 } },
  });
  const { runway } = room.getViewModel().guidance;
  assert.equal(runway.empty, 5);
  assert.deepEqual(runway.unprojected, ['K', 'DEF']);
  assert.equal(runway.roundsRemaining, 12);
  // Seven spare rounds for five slots: nothing to shout about, and the page
  // must not shout. A board that always looks urgent teaches its user to
  // ignore it.
  assert.equal(runway.level, 'calm');
});

test('the runway raises the alarm when the rounds left stop covering the empty slots', async () => {
  // rounds=5, five empty projected slots -> slack 0, the one thing in this
  // league genuinely worth 63-80 points.
  const { room } = await baseRoom({
    extra: { rounds: 5 },
    leagueConfig: { ...LEAGUE_CONFIG, rosterSlots: { QB: 1, RB: 2, WR: 1, TE: 1, K: 1, DEF: 1 } },
  });
  const { runway } = room.getViewModel().guidance;
  assert.equal(runway.slack, 0);
  assert.equal(runway.level, 'alarm');
});

test('status publishes rounds remaining, which the runway is measured against', async () => {
  const { room } = await baseRoom();
  assert.equal(room.getViewModel().status.roundsRemaining, 6);
});

test('board rows carry positional rank and the ADP-vs-now delta', async () => {
  const { room } = await baseRoom();
  const view = room.getViewModel();
  for (const key of ['posRank', 'adpDelta', 'aboveCliff']) {
    assert.ok(key in view.board[0], `board row is missing "${key}"`);
  }
  const p5 = view.board.find((r) => r.playerId === 'P5'); // RB, adp 10, second RB
  assert.equal(p5.posRank, 2);
  assert.equal(p5.aboveCliff, true);
  // currentPick 1, adp 10 -> nine picks EARLIER than his ADP.
  assert.equal(p5.adpDelta, -9);
});

test('the ADP delta turns positive once a player outlasts his own ADP -- he is falling', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [draftResultsAfter(11)],
  });
  const { room } = await baseRoom({ client });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.currentPick, 12);
  const p4 = view.board.find((r) => r.playerId === 'P4'); // adp 8, still on the board
  assert.equal(p4.taken, false);
  assert.equal(p4.adpDelta, 4); // four picks past his own ADP
});

test('recommendations carry the same depth the board does, projection included', async () => {
  const { room } = await baseRoom();
  for (const row of room.getViewModel().recommendations) {
    for (const key of ['proj', 'posRank', 'adpDelta', 'expectedLoss', 'fillsNeed']) {
      assert.ok(key in row, `recommendation row is missing "${key}"`);
    }
  }
});

test('replacement level is read off this board\'s own VOR, for the explainer panel', async () => {
  const { room } = await baseRoom();
  const { replacement } = room.getViewModel().guidance;
  // BOARD_PLAYERS has no vor==0 row, so the closest to zero stands in: the
  // point is that the number comes from the engine's column, not a formula.
  assert.equal(replacement.RB.rank, 2);
  assert.equal(replacement.RB.points, 150);
  assert.ok(replacement.QB.points > replacement.RB.points); // the VOR story, on this league's own board
});

test('guidance survives a postdraft state without throwing or inventing a hero', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [draftResultsAfter(TOTAL_PICKS)],
  });
  const { room } = await baseRoom({ client });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.draftStatus, 'postdraft');
  assert.equal(view.guidance.hero, null);
  assert.equal(view.guidance.urgency.level, 'calm');
  assert.equal(view.guidance.runway.roundsRemaining, 0);
});

// --- the survival horizon while you are ON THE CLOCK -------------------------
//
// A defect the redesign's own rendering exposed: `deriveClock` correctly
// reports myNextPick === currentPick while you are on the clock (that is what
// `isMyTurn` is built from), and `recompute` used to pass that straight
// through as `nextPick`. `tt.survival.add_survival` rejects `next_pick <=
// pick` outright, so EVERY one of the user's own turns raised the recompute
// banner and froze the recommendations at exactly the moment the tool is
// being used.

test('on the clock, survival is measured to your FOLLOWING turn, not to the pick you are making', async () => {
  // myPicks(4, 2, 6) = [2, 7, 10, 15, 18, 23]; one pick made -> I am on #2.
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [draftResultsAfter(1)],
  });
  const { room, analytics } = await baseRoom({ client });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.isMyTurn, true);
  assert.equal(view.status.myNextPick, 2);

  const { stdin } = analytics.runScriptCalls.at(-1).opts;
  assert.equal(stdin.pick, 2);
  assert.equal(stdin.nextPick, 7, 'must look FORWARD to my next turn, not at this pick');
  assert.equal(view.status.banner, null, 'the recompute must not fail on my own turn');
});

test('the pick survival is measured to is published, and the hero sentence names it', async () => {
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [draftResultsAfter(1)],
  });
  const { room } = await baseRoom({ client });
  await room.poll();
  const view = room.getViewModel();
  assert.equal(view.status.survivalPick, 7);
  const survival = view.guidance.hero.reasons.find((r) => r.kind === 'survival');
  assert.match(survival.text, /#7/);
  assert.doesNotMatch(survival.text, /#2\b/);
});

test('off the clock, the survival horizon is simply your next pick', async () => {
  const { room } = await baseRoom();
  const view = room.getViewModel();
  assert.equal(view.status.myNextPick, 2);
  assert.equal(view.status.survivalPick, 2);
});

test('on your last pick of the draft, survival runs to the end of the draft', async () => {
  // 22 picks made -> pending starts at #23, my last pick. There is no
  // following turn, so everyone not taken now really is gone.
  const client = fakeClient({
    'league/470.l.1/teams': TEAMS_RESPONSE,
    'league/470.l.1/draftresults': [draftResultsAfter(22)],
  });
  const { room, analytics } = await baseRoom({ client });
  await room.poll();
  assert.equal(room.getViewModel().status.currentPick, 23);
  assert.equal(room.getViewModel().status.survivalPick, 25); // teams * rounds + 1
  assert.equal(analytics.runScriptCalls.at(-1).opts.stdin.nextPick, 25);
});
