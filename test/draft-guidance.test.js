import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  CLIFFS, CLIFF_ORDER, WAITING_COST, URGENCY_NOTABLE, URGENCY_ALARM,
  positionAdpRanks, cliffStrip, waitingCost, urgencyLevel,
  runwayState, replacementLine, heroReasons,
} from '../src/draft-guidance.js';

// A tiny board shaped exactly like `tt.cli board`'s own "players" rows.
// ADP order deliberately disagrees with proj order so a test can tell which
// axis a rank was computed on.
const ROWS = [
  { player_id: 'rb1', name: 'Rb One', position: 'RB', proj_points: 210, vor: 50, tier: 1, adp: 2 },
  { player_id: 'rb2', name: 'Rb Two', position: 'RB', proj_points: 190, vor: 30, tier: 2, adp: 9 },
  { player_id: 'rb3', name: 'Rb Three', position: 'RB', proj_points: 170, vor: 0, tier: 3, adp: 40 },
  { player_id: 'rb4', name: 'Rb Four', position: 'RB', proj_points: 160, vor: -10, tier: 4, adp: 55 },
  { player_id: 'wr1', name: 'Wr One', position: 'WR', proj_points: 200, vor: 45, tier: 1, adp: 3 },
  { player_id: 'wr2', name: 'Wr Two', position: 'WR', proj_points: 150, vor: 0, tier: 2, adp: 30 },
  { player_id: 'qb1', name: 'Qb One', position: 'QB', proj_points: 300, vor: 40, tier: 1, adp: 20 },
  { player_id: 'qb2', name: 'Qb Two', position: 'QB', proj_points: 260, vor: 0, tier: 2, adp: 60 },
  { player_id: 'te1', name: 'Te One', position: 'TE', proj_points: 140, vor: 25, tier: 1, adp: 50 },
  { player_id: 'te2', name: 'Te Two', position: 'TE', proj_points: 110, vor: 0, tier: 2, adp: 90 },
  { player_id: 'nx', name: 'No Adp', position: 'RB', proj_points: 100, vor: -40, tier: 6, adp: null },
];

// --- the measured constants ---------------------------------------------
//
// These are transcriptions of docs/positional-value.md Q4, not derivations.
// The test exists so a typo in the transcription is a red test rather than a
// wrong number on a screen during a live draft.

test('CLIFFS transcribes docs/positional-value.md Q4 exactly, QB included', () => {
  assert.deepEqual(CLIFFS.RB, { afterRank: 12, drop: 68.0, se: 26.2, pick: 27 });
  assert.deepEqual(CLIFFS.WR, { afterRank: 12, drop: 41.7, se: 18.8, pick: 25 });
  assert.deepEqual(CLIFFS.TE, { afterRank: 6, drop: 37.8, se: 14.0, pick: 76 });
  // "QB has no cliff at all -- it is a gentle slope from QB1 to QB24, and no
  // step in it is distinguishable from noise."
  assert.equal(CLIFFS.QB, null);
});

test('the waiting-cost tables carry all 14 measured rounds for both measured team counts', () => {
  assert.equal(WAITING_COST[4].length, 14);
  assert.equal(WAITING_COST[10].length, 14);
  // Spot-check the two numbers the doc singles out.
  assert.equal(WAITING_COST[4][3].RB, 20.3); // round 4, "the largest anywhere in a 4-team draft"
  assert.equal(WAITING_COST[10][11].QB, 54.6); // round 12
});

// --- positional ADP rank -------------------------------------------------

test('positionAdpRanks ranks within a position by consensus ADP, the axis the cliffs are measured on', () => {
  const ranks = positionAdpRanks(ROWS);
  assert.equal(ranks.get('rb1'), 1);
  assert.equal(ranks.get('rb2'), 2);
  assert.equal(ranks.get('rb3'), 3);
  assert.equal(ranks.get('wr1'), 1);
  assert.equal(ranks.get('qb1'), 1);
  assert.equal(ranks.get('te1'), 1);
});

test('positionAdpRanks never invents a rank for a player with no ADP', () => {
  const ranks = positionAdpRanks(ROWS);
  assert.equal(ranks.get('nx'), null);
});

// --- the cliff strip -----------------------------------------------------

test('cliffStrip counts only players still AVAILABLE above the cliff', () => {
  const all = cliffStrip({ rows: ROWS, available: new Set(ROWS.map((r) => r.player_id)), teams: 4, rounds: 15 });
  const rb = all.find((c) => c.position === 'RB');
  assert.equal(rb.remaining, 4); // rb1..rb4 all sit inside RB12

  const some = cliffStrip({ rows: ROWS, available: new Set(['rb3', 'rb4', 'wr1']), teams: 4, rounds: 15 });
  assert.equal(some.find((c) => c.position === 'RB').remaining, 2);
  assert.equal(some.find((c) => c.position === 'WR').remaining, 1);
});

test('cliffStrip reports QB as a slope with no cliff, never a count to panic over', () => {
  const [, , , qb] = cliffStrip({ rows: ROWS, available: new Set(['qb1', 'qb2']), teams: 4, rounds: 15 });
  assert.equal(qb.position, 'QB');
  assert.equal(qb.hasCliff, false);
  assert.equal(qb.remaining, null);
});

test('cliffStrip orders the positions the way the design plan draws them', () => {
  const strip = cliffStrip({ rows: ROWS, available: new Set(), teams: 4, rounds: 15 });
  assert.deepEqual(strip.map((c) => c.position), CLIFF_ORDER);
  assert.deepEqual(CLIFF_ORDER, ['RB', 'WR', 'TE', 'QB']);
});

test('the TE cliff is unreachable in a 4-team draft and reachable in a deep one', () => {
  // 4 teams x 15 rounds = 60 picks; TE6 goes around pick 76.
  const shallow = cliffStrip({ rows: ROWS, available: new Set(), teams: 4, rounds: 15 });
  assert.equal(shallow.find((c) => c.position === 'TE').reachable, false);
  assert.equal(shallow.find((c) => c.position === 'RB').reachable, true);

  const deep = cliffStrip({ rows: ROWS, available: new Set(), teams: 14, rounds: 15 });
  assert.equal(deep.find((c) => c.position === 'TE').reachable, true);
});

test('cliffStrip caps remaining at the cliff rank, never counting the whole position', () => {
  const many = Array.from({ length: 30 }, (_, i) => ({
    player_id: `r${i}`, name: `R ${i}`, position: 'RB', proj_points: 200 - i, vor: 50 - i, tier: 1, adp: i + 1,
  }));
  const strip = cliffStrip({ rows: many, available: new Set(many.map((r) => r.player_id)), teams: 10, rounds: 15 });
  const rb = strip.find((c) => c.position === 'RB');
  assert.equal(rb.remaining, 12);
  assert.equal(rb.afterRank, 12);
});

// --- waiting cost, and the honesty rule ---------------------------------

test('waitingCost reads the 4-team table for a shallow league and the 10-team table for a deep one', () => {
  assert.equal(waitingCost({ position: 'RB', round: 4, teams: 4, rounds: 15 }).cost, 20.3);
  assert.equal(waitingCost({ position: 'RB', round: 4, teams: 4, rounds: 15 }).basisTeams, 4);
  assert.equal(waitingCost({ position: 'QB', round: 12, teams: 10, rounds: 15 }).cost, 54.6);
  assert.equal(waitingCost({ position: 'QB', round: 12, teams: 10, rounds: 15 }).basisTeams, 10);
});

test('waitingCost never interpolates a team count the study did not measure -- it names the table it used', () => {
  const deep = waitingCost({ position: 'RB', round: 4, teams: 14, rounds: 15 });
  assert.equal(deep.basisTeams, 10);
  assert.equal(deep.teams, 14);
  assert.equal(deep.cost, WAITING_COST[10][3].RB);
});

test('waitingCost is null in the final round -- there is no next turn to wait for', () => {
  assert.equal(waitingCost({ position: 'RB', round: 15, teams: 4, rounds: 15 }).cost, null);
});

test('waitingCost clamps a round past the 14 the study measured rather than inventing a row', () => {
  const r17 = waitingCost({ position: 'RB', round: 16, teams: 4, rounds: 20 });
  assert.equal(r17.cost, WAITING_COST[4][13].RB);
  assert.equal(r17.clamped, true);
});

test('THE HONESTY RULE: no round of a 4-team draft can ever reach the alarm level', () => {
  // "The largest waiting cost measured anywhere in a 4-team draft is 20
  // points; most rounds are under 10." A page that renders alarm colour in
  // this league is lying, so the threshold sits above the measured maximum.
  for (let round = 1; round <= 15; round += 1) {
    for (const position of ['QB', 'RB', 'WR', 'TE']) {
      const { cost } = waitingCost({ position, round, teams: 4, rounds: 15 });
      assert.notEqual(urgencyLevel(cost), 'alarm', `4 teams, round ${round}, ${position}`);
    }
  }
});

test('a 10-team draft does reach the alarm level, so the scale is not simply dead', () => {
  const { cost } = waitingCost({ position: 'QB', round: 12, teams: 10, rounds: 15 });
  assert.equal(urgencyLevel(cost), 'alarm');
});

test('urgencyLevel treats a negative or absent waiting cost as calm, not unknown-scary', () => {
  assert.equal(urgencyLevel(-30), 'calm');
  assert.equal(urgencyLevel(null), 'calm');
  assert.equal(urgencyLevel(URGENCY_NOTABLE - 0.1), 'calm');
  assert.equal(urgencyLevel(URGENCY_NOTABLE), 'notable');
  assert.equal(urgencyLevel(URGENCY_ALARM), 'alarm');
});

// --- the runway ----------------------------------------------------------

const SLOTS = [
  { slot: 'QB', player: null },
  { slot: 'RB', player: { playerId: 'rb1', name: 'Rb One', position: 'RB' } },
  { slot: 'RB', player: null },
  { slot: 'WR', player: null },
  { slot: 'W/R/T', player: null },
  { slot: 'K', player: null },
  { slot: 'DEF', player: null },
];

test('runwayState counts only slots the engine can actually recommend into', () => {
  const rw = runwayState({ slots: SLOTS, roundsRemaining: 10 });
  assert.deepEqual(rw.slots.map((s) => s.label), ['QB', 'RB', 'RB', 'WR', 'FLX']);
  assert.equal(rw.empty, 4);
  assert.equal(rw.filled, 1);
});

test('runwayState keeps the unprojected slots visible instead of silently dropping them', () => {
  const rw = runwayState({ slots: SLOTS, roundsRemaining: 10 });
  assert.deepEqual(rw.unprojected, ['K', 'DEF']);
});

test('runwayState reports slack -- the spare rounds beyond the bare minimum', () => {
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 10 }).slack, 6);
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 4 }).slack, 0);
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 2 }).slack, -2);
});

test('the runway is the one thing allowed to raise the alarm -- it is the largest measured effect', () => {
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 12 }).level, 'calm');
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 7 }).level, 'notable');
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 5 }).level, 'alarm');
  assert.equal(runwayState({ slots: SLOTS, roundsRemaining: 3 }).level, 'alarm');
});

test('a complete lineup is calm no matter how few rounds are left', () => {
  const full = SLOTS.map((s) => ({ ...s, player: { playerId: 'x', name: 'X', position: s.slot } }));
  const rw = runwayState({ slots: full, roundsRemaining: 1 });
  assert.equal(rw.empty, 0);
  assert.equal(rw.level, 'complete');
});

// --- replacement level, for the explainer -------------------------------

test('replacementLine reports where this league\'s own VOR crosses zero, per position', () => {
  const rep = replacementLine(ROWS);
  assert.equal(rep.RB.rank, 3);
  assert.equal(rep.RB.points, 170);
  assert.equal(rep.QB.rank, 2);
  assert.equal(rep.QB.points, 260);
  assert.equal(rep.TE.rank, 2);
});

test('replacementLine skips a position with no VOR at all rather than inventing one', () => {
  const rep = replacementLine([{ player_id: 'k1', name: 'K One', position: 'K', proj_points: 90, vor: null }]);
  assert.equal(rep.K, undefined);
});

// --- the hero sentence ---------------------------------------------------

function heroFor(overrides = {}) {
  const strip = cliffStrip({ rows: ROWS, available: new Set(['rb3', 'rb4', 'wr1', 'qb1', 'te1']), teams: 4, rounds: 15 });
  return heroReasons({
    top: { name: 'Rb Three', position: 'RB', pGone: 0.9, posRank: 3, ...overrides.top },
    cliffs: overrides.cliffs ?? strip,
    runway: overrides.runway ?? runwayState({ slots: SLOTS, roundsRemaining: 10 }),
    myNextPick: overrides.myNextPick ?? 26,
    ...overrides.rest,
  });
}

test('heroReasons states conclusions, never raw inputs', () => {
  const reasons = heroFor();
  const text = reasons.map((r) => r.text).join(' | ');
  assert.ok(!/P\(/.test(text), text);
  assert.ok(!/tier \d/i.test(text), text);
});

test('the survival reason names the pick it would not reach', () => {
  assert.match(heroFor().find((r) => r.kind === 'survival').text, /#26/);
  assert.match(heroFor({ top: { pGone: 0.9 } }).find((r) => r.kind === 'survival').text, /^Won't reach/);
  assert.match(heroFor({ top: { pGone: 0.05 } }).find((r) => r.kind === 'survival').text, /^Should still be there/);
});

test('the cliff reason counts what is left above the cliff, and says so in fantasy vernacular', () => {
  // rb3 and rb4 are the only RBs left, both inside RB12.
  assert.match(heroFor().find((r) => r.kind === 'cliff').text, /2 RBs left above the cliff/);
});

test('the last player above a cliff is called exactly that', () => {
  const strip = cliffStrip({ rows: ROWS, available: new Set(['rb3']), teams: 4, rounds: 15 });
  const reasons = heroFor({ cliffs: strip });
  assert.match(reasons.find((r) => r.kind === 'cliff').text, /^Last RB above the cliff/);
});

test('a quarterback is never given a cliff reason, because QB has no cliff', () => {
  const reasons = heroFor({ top: { name: 'Qb One', position: 'QB', pGone: 0.4, posRank: 1 } });
  assert.match(reasons.find((r) => r.kind === 'cliff').text, /slope/);
});

test('the roster reason says which empty slot the pick fills', () => {
  assert.match(heroFor().find((r) => r.kind === 'need').text, /empty RB slot/);
});

test('the roster reason is honest when the pick is depth, not a need', () => {
  const full = SLOTS.map((s) => (s.slot === 'RB' || s.slot === 'W/R/T'
    ? { ...s, player: { playerId: 'x', name: 'X', position: 'RB' } } : s));
  const reasons = heroFor({ runway: runwayState({ slots: full, roundsRemaining: 10 }) });
  assert.match(reasons.find((r) => r.kind === 'need').text, /depth/i);
});

test('the runway reason is the plain sentence the study says is worth the most points', () => {
  assert.match(heroFor().find((r) => r.kind === 'runway').text, /10 rounds left for 4 slots/);
});

test('heroReasons returns nothing at all when there is no pick to make', () => {
  assert.deepEqual(heroReasons({ top: null, cliffs: [], runway: runwayState({ slots: [], roundsRemaining: 0 }), myNextPick: null }), []);
});

test('runwayState separates the unprojected slots still EMPTY from all of them', () => {
  const half = SLOTS.map((s) => (s.slot === 'K' ? { ...s, player: { playerId: 'k', name: 'A Kicker', position: 'K' } } : s));
  const rw = runwayState({ slots: half, roundsRemaining: 10 });
  assert.deepEqual(rw.unprojected, ['K', 'DEF']);
  assert.deepEqual(rw.unprojectedEmpty, ['DEF']);
});

test('the reason sentences use real punctuation, never ASCII stand-ins', () => {
  const all = [
    heroFor(),
    heroFor({ top: { name: 'Qb One', position: 'QB', pGone: 0.4, posRank: 1 } }),
    heroFor({ cliffs: cliffStrip({ rows: ROWS, available: new Set(), teams: 4, rounds: 15 }) }),
  ].flat();
  for (const r of all) assert.doesNotMatch(r.text, /--/, r.text);
});
