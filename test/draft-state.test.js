import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  myPicks,
  nextPick,
  parseDraftResults,
  clockFromPending,
  createState,
  applyPicks,
  markTaken,
  undo,
} from '../src/draft-state.js';
import { normalize } from '../src/normalize.js';

const fixture = (n) =>
  JSON.parse(readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url), 'utf8'));

// --- myPicks: snake order ---------------------------------------------------
//
// Every assertion here is the LITERAL pick sequence, not "picks increase" --
// a monotonic check would pass for an off-by-one snake implementation just
// as easily as a correct one.

test('myPicks: teams=4 slot=2 produces the literal snake sequence from the design doc', () => {
  assert.deepEqual(myPicks(4, 2, 6), [2, 7, 10, 15, 18, 23]);
});

test('myPicks: teams=4 slot=1 wraps at the round1/round2 boundary (picks 8 and 9 back-to-back)', () => {
  // Slot 1 picks first in even rounds and last in odd rounds, so it takes
  // the LAST pick of round 1 (8) and the FIRST pick of round 2 (9) back to
  // back -- the classic snake-wrap trap at one end of the board.
  assert.deepEqual(myPicks(4, 1, 6), [1, 8, 9, 16, 17, 24]);
});

test('myPicks: teams=4 slot=4 (the last slot) wraps at the round0/round1 boundary (picks 4 and 5 back-to-back)', () => {
  // The highest slot number picks last in round 0 and first in round 1 --
  // the wrap at the OTHER end of the board from slot 1's.
  assert.deepEqual(myPicks(4, 4, 6), [4, 5, 12, 13, 20, 21]);
});

test('myPicks: teams=10 slot=10 (last slot, ten teams) wraps at the round0/round1 boundary', () => {
  assert.deepEqual(myPicks(10, 10, 4), [10, 11, 30, 31]);
});

test('myPicks: teams=10 slot=1 (first slot, ten teams) is first pick of every even round', () => {
  assert.deepEqual(myPicks(10, 1, 4), [1, 20, 21, 40]);
});

test('myPicks: single-team league picks every number in sequence', () => {
  assert.deepEqual(myPicks(1, 1, 5), [1, 2, 3, 4, 5]);
});

// --- nextPick ----------------------------------------------------------------

test('nextPick returns the smallest of my picks >= currentPick', () => {
  // myPicks(4,2,6) = [2, 7, 10, 15, 18, 23]
  assert.equal(nextPick(4, 2, 1, 6), 2);
  assert.equal(nextPick(4, 2, 3, 6), 7);
  assert.equal(nextPick(4, 2, 7, 6), 7, 'currentPick exactly on one of my picks returns that pick');
  assert.equal(nextPick(4, 2, 8, 6), 10);
});

test('nextPick returns null once my draft is over', () => {
  assert.equal(nextPick(4, 2, 24, 6), null);
});

test('nextPick returns the first pick when currentPick is before the draft starts', () => {
  assert.equal(nextPick(4, 2, 0, 6), 2);
});

// --- parseDraftResults: the risky, shape-unverified surface ------------------

test('parseDraftResults accepts an array of well-formed entries', () => {
  const payload = [
    { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' },
    { pick: 2, team_key: '470.l.1.t.2', player_key: '470.p.200' },
  ];
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.2', playerKey: '470.p.200' },
  ]);
  assert.deepEqual(malformed, []);
});

test('parseDraftResults accepts a single pick not wrapped in a list', () => {
  const payload = { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' };
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }]);
  assert.deepEqual(malformed, []);
});

test('parseDraftResults unwraps a single entry left under a wrapper key (e.g. {draft_result: {...}})', () => {
  // If there is only one completed pick, normalize()'s unwrap-on-collection
  // logic never triggers (there is no `count` sibling and no repeated-key
  // array to unwrap), so a single entry can arrive still wrapped.
  const payload = { draft_result: { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' } };
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }]);
  assert.deepEqual(malformed, []);
});

test('parseDraftResults coerces a numeric-string pick number rather than rejecting it', () => {
  const payload = { pick: '3', team_key: '470.l.1.t.1', player_key: '470.p.300' };
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 3, teamKey: '470.l.1.t.1', playerKey: '470.p.300' }]);
  assert.deepEqual(malformed, []);
});

test('parseDraftResults puts an entry missing player_key into pending (a future pick slot), NOT malformed -- the defect this module exists to fix', () => {
  // Verified live against a real 14-team Yahoo mock draft
  // (docs/draft-room-design.md 4.2): draft_results publishes every pick
  // slot for the WHOLE draft up front, and a slot nobody has drafted into
  // yet is entirely normal, not a shape mismatch. Getting this wrong means
  // the very first poll of a real draft reports 1 pick and 209 malformed
  // entries and never stops raising the banner -- see the module comment
  // above parseDraftResults for the full story.
  const payload = [
    { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' },
    { pick: 2, team_key: '470.l.1.t.2' }, // no player_key -- a future slot, not garbage
  ];
  const { picks, pending, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }]);
  assert.deepEqual(pending, [{ pick: 2, teamKey: '470.l.1.t.2' }]);
  assert.deepEqual(malformed, [], 'a pending slot must never raise the malformed-entry banner');
});

test('parseDraftResults carries `round` through onto both picks and pending entries when Yahoo supplies it', () => {
  const payload = [
    { pick: 1, round: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' },
    { pick: 15, round: 2, team_key: '470.l.1.t.14' }, // pending
  ];
  const { picks, pending } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 1, teamKey: '470.l.1.t.1', round: 1, playerKey: '470.p.100' }]);
  assert.deepEqual(pending, [{ pick: 15, teamKey: '470.l.1.t.14', round: 2 }]);
});

test('parseDraftResults puts an entry missing team_key into malformed', () => {
  const payload = [{ pick: 1, player_key: '470.p.100' }];
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, []);
  assert.equal(malformed.length, 1);
});

test('parseDraftResults puts an entry with no recognisable pick number into malformed', () => {
  const payload = [{ team_key: '470.l.1.t.1', player_key: '470.p.100' }];
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, []);
  assert.equal(malformed.length, 1);
});

test('parseDraftResults never throws on garbage entries and files them as malformed', () => {
  const payload = [null, 42, 'nonsense', [1, 2, 3], { pick: 1, team_key: 't', player_key: 'p' }];
  assert.doesNotThrow(() => parseDraftResults(payload));
  const { picks, malformed } = parseDraftResults(payload);
  assert.equal(picks.length, 1);
  assert.equal(malformed.length, 4);
});

test('parseDraftResults never throws on a bare garbage top-level payload', () => {
  assert.doesNotThrow(() => parseDraftResults('not an object'));
  assert.doesNotThrow(() => parseDraftResults(42));
  assert.doesNotThrow(() => parseDraftResults(true));
});

test('parseDraftResults returns empty picks/pending/malformed for null/undefined payload (no draft yet)', () => {
  assert.deepEqual(parseDraftResults(null), { picks: [], pending: [], malformed: [] });
  assert.deepEqual(parseDraftResults(undefined), { picks: [], pending: [], malformed: [] });
});

test('parseDraftResults returns empty picks/pending/malformed for an empty array', () => {
  assert.deepEqual(parseDraftResults([]), { picks: [], pending: [], malformed: [] });
});

test('parseDraftResults accepts the raw count-keyed collection shape too, defensively', () => {
  // In case a caller forwards a fragment straight from Yahoo without
  // running it through normalize() first -- the exact shape normalize.js
  // itself has to handle (see collectionToArray).
  const payload = {
    0: { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' },
    1: { pick: 2, team_key: '470.l.1.t.2', player_key: '470.p.200' },
    count: 2,
  };
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.2', playerKey: '470.p.200' },
  ]);
  assert.deepEqual(malformed, []);
});

// --- applyPicks: immutability, idempotency, reconciliation -------------------

function baseState() {
  return createState(['470.p.100', '470.p.200', '470.p.300', '470.p.400']);
}

test('createState starts with every given player available and an empty board', () => {
  const state = baseState();
  assert.equal(state.available.size, 4);
  assert.ok(state.available.has('470.p.100'));
  assert.equal(state.drafted.size, 0);
  assert.deepEqual(state.myRoster, []);
  assert.equal(state.currentPick, 1);
});

test('applyPicks removes drafted players from available and records them in drafted', () => {
  const state = baseState();
  const picks = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  const next = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.equal(next.available.has('470.p.100'), false);
  assert.equal(next.drafted.get(1).playerId, '470.p.100');
  assert.equal(next.drafted.get(1).teamKey, '470.l.1.t.1');
});

test('applyPicks never mutates the input state (immutability)', () => {
  const state = baseState();
  const snapshotAvailable = new Set(state.available);
  const picks = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(state.available, snapshotAvailable, 'original available set must be untouched');
  assert.equal(state.drafted.size, 0, 'original drafted map must be untouched');
  assert.equal(state.currentPick, 1, 'original currentPick must be untouched');
});

test('applyPicks derives currentPick from the count of distinct picks, not the max pick number seen', () => {
  // Yahoo may deliver picks out of order: pick 5 arrives before pick 2.
  // A max-based counter would report currentPick=6 after only two picks
  // are actually known; the correct count-based value is 3.
  const state = baseState();
  const picks = [
    { pick: 5, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.2', playerKey: '470.p.200' },
  ];
  const next = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.equal(next.currentPick, 3, 'two distinct picks known -> currentPick is 3, not 6');
});

// --- currentPick precedence: Yahoo's published order beats our count -------
//
// These prove the derive-from-Yahoo path is actually EXERCISED, not just
// coincidentally equal to the fallback: pick 4 is still pending while pick
// 5 is already made (a poll caught picks out of order -- see applyPicks's
// own docstring), so the count-based fallback (drafted.size+1 = 5) and the
// pending-based Yahoo answer (the lowest pending pick = 4) genuinely
// disagree. If the code silently fell through to the count-based fallback
// this test would see 5, not 4, and fail.

test('applyPicks prefers the lowest pending pick over drafted.size+1 when draft_results supplies `pending` (Yahoo out-of-order delivery)', () => {
  const state = baseState();
  const picks = [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.2', playerKey: '470.p.200' },
    { pick: 3, teamKey: '470.l.1.t.3', playerKey: '470.p.300' },
    { pick: 5, teamKey: '470.l.1.t.1', playerKey: '470.p.400' }, // pick 4 not yet made
  ];
  const pending = [
    { pick: 4, teamKey: '470.l.1.t.4' },
    { pick: 6, teamKey: '470.l.1.t.2' },
  ];
  const next = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4', pending });
  assert.equal(next.currentPick, 4, 'the lowest PENDING pick (4) wins, not drafted.size+1 (5)');
});

test('applyPicks falls back to drafted.size+1 when `pending` is empty or not supplied', () => {
  const state = baseState();
  const picks = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  const withoutPending = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.equal(withoutPending.currentPick, 2);
  const withEmptyPending = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4', pending: [] });
  assert.equal(withEmptyPending.currentPick, 2);
});

test('applyPicks stores `pending` on the returned state so callers (draft-room.js) can derive the clock from it', () => {
  const state = baseState();
  const pending = [{ pick: 2, teamKey: '470.l.1.t.2' }];
  const next = applyPicks(state, [], { myTeamKey: '470.l.1.t.4', pending });
  assert.deepEqual(next.pending, pending);
});

// --- clockFromPending: deriving onTheClock/myNextPick from Yahoo's order ---

test('clockFromPending returns null (the fall-back-to-snake-math signal) for empty or missing pending', () => {
  assert.equal(clockFromPending([], '470.l.1.t.4'), null);
  assert.equal(clockFromPending(null, '470.l.1.t.4'), null);
  assert.equal(clockFromPending(undefined, '470.l.1.t.4'), null);
});

test('clockFromPending picks the lowest pending pick as onTheClock/currentRound, regardless of array order', () => {
  const pending = [
    { pick: 20, teamKey: '470.l.1.t.9', round: 2 },
    { pick: 15, teamKey: '470.l.1.t.14', round: 2 }, // lowest -- out of order in the array on purpose
    { pick: 21, teamKey: '470.l.1.t.8', round: 2 },
  ];
  const clock = clockFromPending(pending, '470.l.1.t.4');
  assert.equal(clock.currentPick, 15);
  assert.equal(clock.currentRound, 2);
  assert.equal(clock.onTheClock, '470.l.1.t.14');
});

test('clockFromPending finds the lowest pending pick that belongs to myTeamKey specifically, not just the lowest overall', () => {
  const pending = [
    { pick: 15, teamKey: '470.l.1.t.14', round: 2 },
    { pick: 20, teamKey: '470.l.1.t.4', round: 2 }, // mine, but not the overall-lowest
    { pick: 34, teamKey: '470.l.1.t.4', round: 3 }, // mine, but not my NEXT one
  ];
  const clock = clockFromPending(pending, '470.l.1.t.4');
  assert.equal(clock.onTheClock, '470.l.1.t.14', 'someone else is on the clock right now');
  assert.equal(clock.myNextPick, 20, 'my own lowest pending pick, not the overall lowest');
  assert.equal(clock.myNextPickRound, 2);
});

test('clockFromPending returns myNextPick null when none of the pending slots belong to myTeamKey (my draft is over)', () => {
  const pending = [{ pick: 15, teamKey: '470.l.1.t.14', round: 2 }];
  const clock = clockFromPending(pending, '470.l.1.t.4');
  assert.equal(clock.myNextPick, null);
  assert.equal(clock.myNextPickRound, null);
  assert.equal(clock.onTheClock, '470.l.1.t.14', 'other teams can still be mid-draft after mine ends');
});

// --- the real captured payload: mid-draft, some made, mostly pending -------
//
// Verified live against a real 14-team, 15-round Yahoo mock draft
// (scrubbed per this repo's standing rule -- see tools/capture-fixtures.mjs
// -- league id and name replaced with neutral placeholders; player keys are
// public Yahoo ids and are left as captured). This is the exact shape that
// broke parseDraftResults before this fix: 210 entries from the very first
// poll, most of them pending. Run through normalize() first, exactly as
// src/client.js does before draft-room.js ever sees the response, so this
// exercises the REAL pipeline end to end, not a hand-shaped stand-in.

test('the real captured mid-draft payload: 8 made, 202 pending, ZERO malformed', () => {
  const { league } = normalize(fixture('league-draftresults-mid'));
  const { picks, pending, malformed } = parseDraftResults(league.draft_results);
  assert.equal(picks.length, 8);
  assert.equal(pending.length, 202);
  assert.deepEqual(malformed, [], 'a real, fully-pending-heavy draft must never trip the malformed banner');
});

test('the real captured mid-draft payload: applyPicks + clockFromPending derive the correct on-the-clock team and my next pick', () => {
  const { league } = normalize(fixture('league-draftresults-mid'));
  const { picks, pending } = parseDraftResults(league.draft_results);
  const state = applyPicks(createState([]), picks, { myTeamKey: '470.l.1433972.t.5', pending });

  // 8 picks made -> pick 9 is next; round 2 starts reversing at pick 15
  // (verified against the capture: p15:t14 p16:t13 ... p28:t1).
  assert.equal(state.currentPick, 9);
  const clock = clockFromPending(state.pending, '470.l.1433972.t.5');
  assert.equal(clock.currentPick, 9);
  assert.equal(clock.currentRound, 1);
  assert.equal(clock.onTheClock, '470.l.1433972.t.9', 'pick 9, round 1 belongs to team 9');
  // Team 5 picked 1st-round pick 5 already (made); its next is round 2's
  // mirrored slot -- pick (15 + (14 - 5)) = 24 by the verified reversal.
  assert.equal(clock.myNextPick, 24);
  assert.equal(clock.myNextPickRound, 2);
});

// --- the real captured payload, further along (131/210 picks made) ---------
//
// A second, later capture of the SAME live mock draft, well past round 9 --
// the fuller companion fixture the task asks for when a later capture is
// available. Exercises the identical pick/pending split at a very different
// point in the draft (mid-round, not a clean round boundary), and confirms
// `myNextPick` correctly differs per team depending on where each team's
// upcoming picks fall relative to the current one.

test('the real captured LATE-draft payload (131 made / 79 pending): still zero malformed, and onTheClock/myNextPick derive correctly for several different teams', () => {
  const { league } = normalize(fixture('league-draftresults-late'));
  const { picks, pending, malformed } = parseDraftResults(league.draft_results);
  assert.equal(picks.length, 131);
  assert.equal(pending.length, 79);
  assert.deepEqual(malformed, []);

  // Team 9 is on the clock at pick 132 (round 10) -- verified directly
  // against the capture. Each other team's myNextPick is its own soonest
  // still-pending slot, which differs team to team.
  for (const [teamNum, expected] of [
    [1, { myNextPick: 140, myNextPickRound: 10 }],
    [5, { myNextPick: 136, myNextPickRound: 10 }],
    [9, { myNextPick: 132, myNextPickRound: 10 }], // on the clock right now
    [14, { myNextPick: 154, myNextPickRound: 11 }],
  ]) {
    const myTeamKey = `470.l.1433972.t.${teamNum}`;
    const state = applyPicks(createState([]), picks, { myTeamKey, pending });
    assert.equal(state.currentPick, 132, `currentPick is the same for every team (team ${teamNum})`);
    const clock = clockFromPending(state.pending, myTeamKey);
    assert.equal(clock.currentRound, 10);
    assert.equal(clock.onTheClock, '470.l.1433972.t.9');
    assert.equal(clock.myNextPick, expected.myNextPick, `team ${teamNum} myNextPick`);
    assert.equal(clock.myNextPickRound, expected.myNextPickRound, `team ${teamNum} myNextPickRound`);
  }
});

test('applyPicks builds myRoster from picks belonging to myTeamKey, in pick order', () => {
  const state = baseState();
  const picks = [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.4', playerKey: '470.p.200' },
    { pick: 3, teamKey: '470.l.1.t.4', playerKey: '470.p.300' },
  ];
  const next = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(next.myRoster, ['470.p.200', '470.p.300']);
});

test('applyPicks orders myRoster by pick number, not by the order picks were delivered in', () => {
  // Yahoo (or a re-poll) can deliver a later pick before an earlier one.
  // myRoster must still read pick 2 before pick 6, regardless of arrival
  // order, since it is a roster listing, not an arrival log.
  const state = baseState();
  const picks = [
    { pick: 6, teamKey: '470.l.1.t.4', playerKey: '470.p.400' },
    { pick: 2, teamKey: '470.l.1.t.4', playerKey: '470.p.200' },
  ];
  const next = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(next.myRoster, ['470.p.200', '470.p.400']);
});

test('applyPicks is idempotent: re-delivering the exact same picks changes nothing further', () => {
  // The poller re-delivers picks it has already seen every few seconds for
  // the length of a two-hour draft -- this is not an edge case, it is the
  // normal operating mode.
  const state = baseState();
  const picks = [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.4', playerKey: '470.p.200' },
  ];
  const once = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  const twice = applyPicks(once, picks, { myTeamKey: '470.l.1.t.4' });
  const thrice = applyPicks(twice, picks, { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(twice.drafted, once.drafted);
  assert.deepEqual(twice.available, once.available);
  assert.deepEqual(twice.myRoster, once.myRoster);
  assert.equal(twice.currentPick, once.currentPick);
  assert.deepEqual(thrice, twice, 'hammering the same picks a third time is still a no-op');
});

test('applyPicks hammered with overlapping re-polls (new picks mixed with already-seen ones) converges to the same state as applying everything once', () => {
  const state = baseState();
  const poll1 = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  const poll2 = [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.4', playerKey: '470.p.200' },
  ];
  const poll3 = [
    { pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' },
    { pick: 2, teamKey: '470.l.1.t.4', playerKey: '470.p.200' },
    { pick: 3, teamKey: '470.l.1.t.2', playerKey: '470.p.300' },
  ];
  let s = state;
  for (const poll of [poll1, poll2, poll3, poll3, poll2, poll1]) {
    s = applyPicks(s, poll, { myTeamKey: '470.l.1.t.4' });
  }
  const straightLine = applyPicks(state, poll3, { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(s.drafted, straightLine.drafted);
  assert.deepEqual(s.available, straightLine.available);
  assert.deepEqual(s.myRoster, straightLine.myRoster);
  assert.equal(s.currentPick, straightLine.currentPick);
});

// --- markTaken / undo: manual override ---------------------------------------

test('markTaken removes a player from available', () => {
  const state = baseState();
  const next = markTaken(state, '470.p.100');
  assert.equal(next.available.has('470.p.100'), false);
});

test('markTaken never mutates the input state', () => {
  const state = baseState();
  markTaken(state, '470.p.100');
  assert.equal(state.available.has('470.p.100'), true);
});

test('markTaken on an already-unavailable player is a no-op (idempotent)', () => {
  const state = baseState();
  const once = markTaken(state, '470.p.100');
  const twice = markTaken(once, '470.p.100');
  assert.deepEqual(twice.available, once.available);
  assert.deepEqual(twice.manualMarks, once.manualMarks);
});

test('undo reverts the most recent manual mark, restoring the player to available', () => {
  const state = baseState();
  const marked = markTaken(state, '470.p.100');
  const reverted = undo(marked);
  assert.equal(reverted.available.has('470.p.100'), true);
});

test('undo reverts only the SINGLE most recent manual mark, not all of them', () => {
  const state = baseState();
  let s = markTaken(state, '470.p.100');
  s = markTaken(s, '470.p.200');
  s = undo(s);
  assert.equal(s.available.has('470.p.100'), false, 'earlier manual mark stays in place');
  assert.equal(s.available.has('470.p.200'), true, 'most recent manual mark is reverted');
});

test('undo with no manual marks is a no-op and never touches Yahoo-derived picks', () => {
  const state = baseState();
  const picks = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  const afterPick = applyPicks(state, picks, { myTeamKey: '470.l.1.t.4' });
  const reverted = undo(afterPick);
  assert.equal(reverted.available.has('470.p.100'), false, 'a real Yahoo pick must never be undone');
  assert.deepEqual(reverted.drafted, afterPick.drafted);
  assert.deepEqual(reverted, afterPick, 'undo is a total no-op when there is nothing manual to revert');
});

test('a Yahoo pick for a manually-marked player reconciles to ONE entry, and undo can no longer touch it', () => {
  const state = baseState();
  const manual = markTaken(state, '470.p.100');
  assert.equal(manual.manualMarks.length, 1);

  // Yahoo now confirms the real pick for the same player.
  const picks = [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }];
  const reconciled = applyPicks(manual, picks, { myTeamKey: '470.l.1.t.4' });

  assert.equal(reconciled.drafted.size, 1, 'exactly one drafted entry, not a manual one plus a Yahoo one');
  assert.equal(reconciled.manualMarks.length, 0, 'the manual mark is cleared once Yahoo confirms the pick');
  assert.equal(reconciled.available.has('470.p.100'), false);

  // Since the manual mark is gone, undo must now be a no-op -- it must
  // never undo real Yahoo history.
  const afterUndo = undo(reconciled);
  assert.equal(afterUndo.available.has('470.p.100'), false, 'undo must not resurrect a real Yahoo pick');
  assert.deepEqual(afterUndo, reconciled);
});

test('undo after a manual mark that was later confirmed by Yahoo, with an EARLIER unrelated manual mark still pending, reverts the earlier one (not the confirmed pick)', () => {
  const state = baseState();
  let s = markTaken(state, '470.p.200'); // manual mark #1
  s = markTaken(s, '470.p.100'); // manual mark #2
  // Yahoo confirms player 100's real pick -- reconciliation should drop
  // ONLY manual mark #2, leaving #1 as the most recent manual mark.
  s = applyPicks(s, [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }], { myTeamKey: '470.l.1.t.4' });
  assert.deepEqual(s.manualMarks, ['470.p.200']);
  const reverted = undo(s);
  assert.equal(reverted.available.has('470.p.200'), true, 'the remaining manual mark is what undo reverts');
  assert.equal(reverted.available.has('470.p.100'), false, 'the Yahoo-confirmed pick is untouched');
});
