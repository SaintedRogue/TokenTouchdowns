import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  CLIFFS, CLIFF_ORDER, WAITING_COST, URGENCY_NOTABLE, URGENCY_ALARM,
  positionAdpRanks, cliffStrip, waitingCost, urgencyLevel,
  runwayState, replacementLine, heroReasons,
  glossary, byeConflicts, vorShares, tierBands, adpFallLevel,
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

// =========================================================================
// MAKING THE PAGE SELF-EXPLAINING (this branch)
//
// The user's own words, looking at the live page: "Can you explain the
// analysis? I don't know what the acronyms mean. How am I to read
// everything?" Every definition the page shows is composed HERE, from THIS
// board's own numbers, for exactly the reason the rest of this module
// exists: a sentence composed in draft-room.html is a sentence nothing can
// check, and a wrong explanation on draft day is worse than no explanation.
// =========================================================================

// A board with proj_games, so the PROJ definition can be honest about what a
// projection actually is. QB deliberately has the highest projection AND the
// smallest VOR -- the exact shape that prompted the question.
const GLOSSARY_ROWS = [
  { player_id: 'qb1', name: 'Top Passer', position: 'QB', proj_points: 253.49, vor: 22.53, tier: 1, adp: 20, proj_games: 15 },
  { player_id: 'qb2', name: 'Second Passer', position: 'QB', proj_points: 247.94, vor: 16.98, tier: 1, adp: 30, proj_games: 15 },
  { player_id: 'qb3', name: 'Third Passer', position: 'QB', proj_points: 234.82, vor: 3.86, tier: 2, adp: 45, proj_games: 14 },
  { player_id: 'qb4', name: 'Last Starting Passer', position: 'QB', proj_points: 230.96, vor: 0, tier: 2, adp: 55, proj_games: 14 },
  { player_id: 'qb5', name: 'Backup Passer', position: 'QB', proj_points: 210.0, vor: -20.96, tier: 3, adp: 80, proj_games: 12 },
  { player_id: 'rb1', name: 'Top Runner', position: 'RB', proj_points: 239.81, vor: 52.08, tier: 1, adp: 7, proj_games: 13 },
  { player_id: 'rb2', name: 'Second Runner', position: 'RB', proj_points: 200.0, vor: 12.27, tier: 2, adp: 12, proj_games: 12 },
  { player_id: 'rb3', name: 'Last Starting Runner', position: 'RB', proj_points: 187.73, vor: 0, tier: 3, adp: 40, proj_games: 11 },
  { player_id: 'rb4', name: 'Bench Runner', position: 'RB', proj_points: 150.0, vor: -37.73, tier: 4, adp: 90, proj_games: 8 },
];

const GLOSSARY_ARGS = {
  rows: GLOSSARY_ROWS,
  replacement: replacementLine(GLOSSARY_ROWS),
  teams: 4,
  survivalPick: 2,
  adp: { totalDrafts: 718, type: 'Half-PPR', teams: 12 },
};

const termsById = (args = GLOSSARY_ARGS) =>
  new Map(glossary(args).map((t) => [t.id, t]));

test('glossary defines every abbreviation the page renders, so nothing on screen is unlearnable', () => {
  const ids = glossary(GLOSSARY_ARGS).map((t) => t.id);
  for (const id of ['proj', 'vor', 'stake', 'gone', 'tier', 'adp', 'adpDelta', 'posRank', 'bye', 'injury']) {
    assert.ok(ids.includes(id), `no definition for ${id} -- it appears on the page and cannot be looked up`);
  }
});

test('every glossary term carries the token as it is printed, an expanded label, and a plain-language sentence', () => {
  for (const t of glossary(GLOSSARY_ARGS)) {
    assert.ok(t.term && t.term.length > 0, `${t.id} has no printed token`);
    assert.ok(t.label && /[a-z]/.test(t.label), `${t.id} has no expanded label`);
    assert.ok(t.short && t.short.length > 20, `${t.id}'s definition is too short to be a definition`);
    assert.ok(/[.!]$/.test(t.short), `${t.id}'s definition is not a sentence: ${t.short}`);
  }
});

test('PROJ is honest that a projection is not a 17-game total: it already prices in expected games missed', () => {
  const proj = termsById().get('proj');
  const text = `${proj.short} ${proj.detail}`;
  assert.match(text, /season/i);
  assert.match(text, /17/, 'the reader has to be told it is NOT a 17-game total');
  // The median expected games across this board: 9 rows, middle value 13.
  assert.match(proj.detail, /\b13\b/, "the board's own median expected games, not a generality");
});

test('VOR is defined against the LAST STARTER, never a waiver-wire player (vor.py\'s own corrected docstring)', () => {
  const vor = termsById().get('vor');
  assert.match(vor.short, /last starter/i);
  assert.match(`${vor.short} ${vor.detail}`, /waiver/i, 'the wrong definition is the one the reader already has; say it is wrong');
});

test("VOR's explanation is grounded in THIS board's own numbers, and resolves the quarterback paradox by name", () => {
  const vor = termsById().get('vor');
  // 4 teams x 1 starting QB = the 4th QB is the last one who still starts.
  assert.match(vor.detail, /Top Passer/, 'the highest-projected player on the board, named');
  assert.match(vor.detail, /Top Runner/);
  assert.match(vor.detail, /\b253\b/, "the top QB's own projection");
  assert.match(vor.detail, /\b23\b/, "the top QB's own VOR");
  assert.match(vor.detail, /\b240\b/, "the top RB's own projection");
  assert.match(vor.detail, /\b52\b/, "the top RB's own VOR");
  assert.match(vor.detail, /\b4 quarterbacks\b/, '4 teams x 1 starting QB');
  assert.match(vor.detail, /\b3 running backs\b/, '4 teams x the RB starter share of this fixture');
});

test('VOR never prints a subtraction the rounded numbers on screen do not support', () => {
  const detail = termsById().get('vor').detail;
  // 253 - 231 = 22 by the rounded figures, but the board's own VOR is 23:
  // the sentence must never show that pair as an arithmetic step.
  assert.ok(!/253\D{1,12}231/.test(detail), `do not print an operand pair that does not subtract to the printed VOR: ${detail}`);
});

test('AT STAKE says outright that it is what the ranking sorts on -- the answer to "why is a lower VOR ranked higher?"', () => {
  const stake = termsById().get('stake');
  const text = `${stake.short} ${stake.detail}`;
  assert.match(stake.short, /VOR/);
  assert.match(text, /rank/i);
  assert.match(text, /lower VOR/i, 'the reader\'s actual confusion, named');
});

test('GONE is measured to the pick the page is actually measuring to, not "your next pick" in the abstract', () => {
  assert.match(termsById().get('gone').short, /#2\b/);
  const noPick = termsById({ ...GLOSSARY_ARGS, survivalPick: null }).get('gone');
  assert.match(noPick.short, /next pick/i);
  assert.ok(!/#/.test(noPick.short), 'never invent a pick number when there is no next pick');
});

test('ADP reports the feed the board was actually built from, drafts counted, never a hardcoded number', () => {
  const adp = termsById().get('adp');
  assert.match(adp.detail, /718/);
  assert.match(adp.detail, /Half-PPR/);
  const noMeta = termsById({ ...GLOSSARY_ARGS, adp: null }).get('adp');
  assert.ok(noMeta.short.length > 20);
  assert.ok(!/\d/.test(noMeta.detail ?? ''), 'with no feed metadata, invent no draft count');
});

test('TIER says what to DO with two players in the same tier, not merely what a tier is', () => {
  const tier = termsById().get('tier');
  assert.match(`${tier.short} ${tier.detail}`, /interchangeable/i);
  assert.match(`${tier.short} ${tier.detail}`, /cheap|slot/i);
});

test('the positional rank badge is defined on the axis it is actually computed on -- ADP, not points', () => {
  const rank = termsById().get('posRank');
  assert.match(rank.term, /WR1|RB5/);
  assert.match(`${rank.short} ${rank.detail}`, /average draft position|ADP/i);
  assert.match(`${rank.short} ${rank.detail}`, /cliff/i, 'it is the axis the cliffs are measured on');
});

test('VS ADP is defined as the bargain signal it is, scaled to this league\'s own turn length', () => {
  const d = termsById().get('adpDelta');
  assert.match(`${d.short} ${d.detail}`, /fallen|past/i);
  assert.match(d.detail, /\b4\b/, 'a full turn of a 4-team draft');
});

test('bye and injury are defined, because the page now shows both', () => {
  const bye = termsById().get('bye');
  assert.match(bye.short, /week/i);
  assert.match(`${bye.short} ${bye.detail}`, /two|2/i);
  const inj = termsById().get('injury');
  assert.match(inj.term, /Q/);
  assert.match(`${inj.short} ${inj.detail}`, /questionable/i);
});

test('glossary degrades to definitions with no worked example rather than throwing on an empty board', () => {
  const terms = glossary({ rows: [], replacement: {}, teams: 4, survivalPick: null, adp: null });
  assert.ok(terms.length >= 10);
  for (const t of terms) assert.ok(t.short && t.short.length > 20, `${t.id} lost its definition`);
});

// --- bye conflicts -------------------------------------------------------

test('byeConflicts finds two STARTERS sharing a bye week -- a week you cannot field a lineup', () => {
  const conflicts = byeConflicts([
    { slot: 'QB', player: { name: 'A Passer', bye: 7 } },
    { slot: 'WR', player: { name: 'A Wideout', bye: 6 } },
    { slot: 'RB', player: { name: 'A Runner', bye: 6 } },
    { slot: 'TE', player: null },
  ]);
  assert.equal(conflicts.length, 1);
  assert.equal(conflicts[0].week, 6);
  assert.deepEqual(conflicts[0].players, ['A Wideout', 'A Runner']);
});

test('byeConflicts reports three on the same week as one conflict, not three pairs', () => {
  const conflicts = byeConflicts([
    { slot: 'WR', player: { name: 'One', bye: 9 } },
    { slot: 'WR', player: { name: 'Two', bye: 9 } },
    { slot: 'RB', player: { name: 'Three', bye: 9 } },
  ]);
  assert.equal(conflicts.length, 1);
  assert.equal(conflicts[0].players.length, 3);
});

test('byeConflicts is silent about a bye we do not know, and about a lone starter on a week', () => {
  assert.deepEqual(byeConflicts([
    { slot: 'WR', player: { name: 'One', bye: null } },
    { slot: 'RB', player: { name: 'Two', bye: null } },
    { slot: 'TE', player: { name: 'Three', bye: 4 } },
  ]), []);
  assert.deepEqual(byeConflicts(null), []);
});

// --- the quiet visuals ---------------------------------------------------

test('vorShares scales each bar WITHIN its own position, so a bar compares like with like', () => {
  const shares = vorShares([
    { playerId: 'a', position: 'RB', vor: 50 },
    { playerId: 'b', position: 'RB', vor: 25 },
    { playerId: 'c', position: 'QB', vor: 20 },
    { playerId: 'd', position: 'QB', vor: 10 },
  ]);
  assert.equal(shares.get('a'), 1);
  assert.equal(shares.get('b'), 0.5);
  // The best QB fills his own position's bar, even though 20 < 50.
  assert.equal(shares.get('c'), 1);
  assert.equal(shares.get('d'), 0.5);
});

test('vorShares draws no bar at all for a player worth nothing over replacement', () => {
  const shares = vorShares([
    { playerId: 'a', position: 'RB', vor: 40 },
    { playerId: 'z', position: 'RB', vor: 0 },
    { playerId: 'n', position: 'RB', vor: -12 },
    { playerId: 'x', position: 'RB', vor: null },
  ]);
  assert.equal(shares.get('z'), 0);
  assert.equal(shares.get('n'), 0);
  assert.equal(shares.get('x'), null);
});

test('tierBands group consecutive rows of the same tier within a position, so "interchangeable" is seen', () => {
  const bands = tierBands([
    { playerId: 'r1', position: 'RB', tier: 1 },
    { playerId: 'w1', position: 'WR', tier: 1 },
    { playerId: 'r2', position: 'RB', tier: 1 },
    { playerId: 'r3', position: 'RB', tier: 2 },
    { playerId: 'w2', position: 'WR', tier: 2 },
  ]);
  // Interleaved WRs must not break the RB band: banding is per position.
  assert.equal(bands.get('r1').band, bands.get('r2').band);
  assert.notEqual(bands.get('r2').band, bands.get('r3').band);
  assert.equal(bands.get('r1').first, true);
  assert.equal(bands.get('r2').first, false);
  assert.equal(bands.get('r3').first, true);
  assert.equal(bands.get('w1').band, bands.get('r1').band, 'each position starts its own banding at the same parity');
});

test('tierBands alternate parity so adjacent tiers are distinguishable', () => {
  const bands = tierBands([
    { playerId: 'a', position: 'RB', tier: 1 },
    { playerId: 'b', position: 'RB', tier: 2 },
    { playerId: 'c', position: 'RB', tier: 3 },
  ]);
  assert.notEqual(bands.get('a').band % 2, bands.get('b').band % 2);
  assert.notEqual(bands.get('b').band % 2, bands.get('c').band % 2);
});

test('tierBands treat a missing tier as its own group rather than merging it into the one above', () => {
  const bands = tierBands([
    { playerId: 'a', position: 'RB', tier: 1 },
    { playerId: 'b', position: 'RB', tier: null },
    { playerId: 'c', position: 'RB', tier: 1 },
  ]);
  assert.notEqual(bands.get('a').band, bands.get('b').band);
  assert.notEqual(bands.get('b').band, bands.get('c').band);
});

test('adpFallLevel scales to the league: a falling player is one who lasted a full turn past his own ADP', () => {
  // 4 teams: a full turn is 4 picks, two turns is 8.
  assert.equal(adpFallLevel(3, 4), 'none');
  assert.equal(adpFallLevel(4, 4), 'past');
  assert.equal(adpFallLevel(7, 4), 'past');
  assert.equal(adpFallLevel(8, 4), 'far');
  // 10 teams: the same absolute fall is unremarkable in a deeper league.
  assert.equal(adpFallLevel(7, 10), 'none');
  assert.equal(adpFallLevel(10, 10), 'past');
});

test('adpFallLevel never flags a player going EARLIER than his ADP, or one with no ADP at all', () => {
  assert.equal(adpFallLevel(-9, 4), 'none');
  assert.equal(adpFallLevel(0, 4), 'none');
  assert.equal(adpFallLevel(null, 4), 'none');
  assert.equal(adpFallLevel(12, null), 'none');
});

test('replacementLine also names the best player at the position, so the explainer can say who it is', () => {
  const rep = replacementLine(GLOSSARY_ROWS);
  assert.equal(rep.QB.topName, 'Top Passer');
  assert.equal(rep.RB.topName, 'Top Runner');
  assert.equal(rep.QB.name, 'Last Starting Passer');
});

test('the VOR entry explains the per-row bar too, because a bar is the one mark that invites the cross-position comparison VOR exists to prevent', () => {
  const grounded = termsById().get('vor').detail;
  assert.match(grounded, /bar/i);
  assert.match(grounded, /own position/i);
  // And it is said even when the board is too thin to ground the example.
  const bare = glossary({ rows: [], replacement: {}, teams: 4 }).find((t) => t.id === 'vor');
  assert.match(bare.detail, /bar/i);
});
