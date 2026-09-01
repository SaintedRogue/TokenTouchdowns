import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createDraftRoom, startDraftRoomServer } from '../src/draft-room.js';
import { AnalyticsError } from '../src/analytics.js';
import { SessionExpiredError, YahooApiError } from '../src/client.js';

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
    assert.match(await html.text(), /<title>/);

    const state = await fetch(new URL('/api/state', started.url));
    assert.equal(state.status, 200);
    const body = await state.json();
    assert.ok(body.status && body.board && body.roster);

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
