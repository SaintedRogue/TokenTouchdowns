import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  myPicks,
  nextPick,
  parseDraftResults,
  createState,
  applyPicks,
  markTaken,
  undo,
} from '../src/draft-state.js';

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

test('parseDraftResults puts an entry missing player_key into malformed, not silently dropped', () => {
  const payload = [
    { pick: 1, team_key: '470.l.1.t.1', player_key: '470.p.100' },
    { pick: 2, team_key: '470.l.1.t.2' }, // no player_key
  ];
  const { picks, malformed } = parseDraftResults(payload);
  assert.deepEqual(picks, [{ pick: 1, teamKey: '470.l.1.t.1', playerKey: '470.p.100' }]);
  assert.equal(malformed.length, 1);
  assert.deepEqual(malformed[0], { pick: 2, team_key: '470.l.1.t.2' });
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

test('parseDraftResults returns empty picks and malformed for null/undefined payload (no draft yet)', () => {
  assert.deepEqual(parseDraftResults(null), { picks: [], malformed: [] });
  assert.deepEqual(parseDraftResults(undefined), { picks: [], malformed: [] });
});

test('parseDraftResults returns empty picks and malformed for an empty array', () => {
  assert.deepEqual(parseDraftResults([]), { picks: [], malformed: [] });
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
