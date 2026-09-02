/**
 * The draft room's VIEW LAYER, made testable.
 *
 * WHAT THIS IS NOT. Nothing here computes a projection, a VOR, a survival
 * probability or a ranking -- those are the Python engine's, and
 * src/draft-room.js's module docstring already forbids a second
 * implementation of any of them. This module only ever (a) transcribes
 * MEASURED CONSTANTS from `docs/positional-value.md`, (b) counts and ranks
 * rows the engine already produced, and (c) turns both into the short
 * sentences the page renders. It lives outside draft-room.html for one
 * reason: this repo has no DOM test infrastructure, so a sentence composed
 * in the page is a sentence nothing can check, and the whole point of the
 * redesign is that those sentences are correct on draft day.
 *
 * WHY THE PAGE WAS REBUILT AROUND THESE NUMBERS. The out-of-sample study in
 * `docs/positional-value.md` found that the effects a draft-day UI should
 * lead with are almost the reverse of what the old page showed:
 *
 *   - FILLING THE LINEUP is the single largest measured effect in this
 *     league: +63 to +80 actual points at 4 teams, larger than any
 *     positional-timing strategy in the study. A bare ADP drafter finishes
 *     with no tight end in 11 of 25 four-team drafts. -> `runwayState`.
 *   - POSITIONAL URGENCY is mostly a 10-team problem. "The largest waiting
 *     cost measured anywhere in a 4-team draft is 20 points; most rounds are
 *     under 10." At 10 teams the same table runs 20-55. -> `waitingCost` +
 *     `urgencyLevel`, whose alarm threshold sits deliberately ABOVE the
 *     4-team maximum so a calm 4-team board can never be painted as urgent.
 *     A page that always looks urgent teaches its user to ignore it.
 *   - THE CLIFFS ARE REAL AND THEY ARE COUNTABLE: RB and WR each fall away
 *     after roughly the 12th player at the position (-68.0 and -41.7 actual
 *     points), TE after the top 6 (-37.8), and QB not at all. -> `CLIFFS` +
 *     `cliffStrip`.
 *
 * EVERY CONSTANT BELOW IS A TRANSCRIPTION, NOT A DERIVATION, and
 * test/draft-guidance.test.js asserts the transcription so a typo is a red
 * test rather than a wrong number in front of someone with 60 seconds to
 * pick.
 */

/**
 * Where each position's board falls away, from `docs/positional-value.md`
 * Q4 ("Where each position actually falls away" / "What that means at the
 * table"). `afterRank` is a rank in PRESEASON CONSENSUS ADP within the
 * position -- the axis the study measured on, which is why `cliffStrip`
 * ranks by `adp` and not by `proj_points`. `drop` is the mean fall in
 * ACTUAL season points across the cliff, `se` its standard error, and
 * `pick` the overall pick that player typically goes at.
 *
 * `QB: null` is a finding, not a gap: "QB has no cliff at all -- it is a
 * gentle slope from QB1 to QB24, and no step in it is distinguishable from
 * noise." Rendering a QB cliff would be inventing one.
 */
export const CLIFFS = {
  RB: { afterRank: 12, drop: 68.0, se: 26.2, pick: 27 },
  WR: { afterRank: 12, drop: 41.7, se: 18.8, pick: 25 },
  TE: { afterRank: 6, drop: 37.8, se: 14.0, pick: 76 },
  QB: null,
};

/** The order the design plan draws the cliff strip in: the two reachable,
 * expensive cliffs first, the shallow one next, the non-cliff last. */
export const CLIFF_ORDER = ['RB', 'WR', 'TE', 'QB'];

/**
 * "Mean actual points lost by waiting one turn (2023-2025)", per round, from
 * `docs/positional-value.md`'s per-round tables. Keyed by the two team
 * counts the study actually ran a per-round table for. Index 0 is round 1.
 *
 * A negative number means waiting was BETTER than taking that position now.
 * The doc's own warning applies and is why `urgencyLevel`'s thresholds are
 * coarse: "on three seasons, NOT ONE ROUND has a statistically separable
 * winner." These are the best estimate available, not a settled ranking.
 */
export const WAITING_COST = {
  4: [
    { QB: -0.1, RB: 4.0, WR: -0.0, TE: 4.0 },
    { QB: -4.8, RB: -16.7, WR: 4.9, TE: 5.8 },
    { QB: -6.3, RB: -4.5, WR: -2.4, TE: 1.1 },
    { QB: -16.5, RB: 20.3, WR: 2.5, TE: 0.1 },
    { QB: -0.9, RB: 8.5, WR: 9.4, TE: -0.9 },
    { QB: 17.8, RB: 3.9, WR: 9.1, TE: -5.5 },
    { QB: 8.8, RB: 2.6, WR: 4.1, TE: -7.0 },
    { QB: 4.9, RB: 7.1, WR: 5.0, TE: -13.3 },
    { QB: 6.7, RB: 7.6, WR: -5.0, TE: -2.9 },
    { QB: 10.7, RB: 10.0, WR: 3.8, TE: 4.1 },
    { QB: 6.7, RB: 0.5, WR: 10.7, TE: 5.2 },
    { QB: 16.2, RB: -3.2, WR: -0.8, TE: 9.0 },
    { QB: 2.8, RB: -2.3, WR: 4.1, TE: 2.8 },
    { QB: 12.3, RB: 2.4, WR: -3.1, TE: 9.7 },
  ],
  10: [
    { QB: -16.7, RB: -23.1, WR: 2.8, TE: 8.5 },
    { QB: 0.3, RB: 40.9, WR: 25.1, TE: -5.5 },
    { QB: 21.1, RB: 10.3, WR: 10.9, TE: -26.1 },
    { QB: 25.5, RB: 18.5, WR: 7.2, TE: 9.0 },
    { QB: 26.0, RB: -5.2, WR: 4.3, TE: 15.9 },
    { QB: 24.1, RB: 11.2, WR: -15.0, TE: 21.1 },
    { QB: -43.0, RB: 19.3, WR: -5.4, TE: 1.3 },
    { QB: -4.9, RB: 15.8, WR: 31.6, TE: 21.9 },
    { QB: 8.3, RB: -30.8, WR: -4.3, TE: -14.3 },
    { QB: -22.7, RB: 6.0, WR: 19.4, TE: -30.4 },
    { QB: 17.4, RB: 23.1, WR: -3.5, TE: 26.0 },
    { QB: 54.6, RB: 23.2, WR: -20.3, TE: -2.4 },
    { QB: -49.9, RB: -23.3, WR: 29.7, TE: -3.2 },
    { QB: -8.3, RB: 16.2, WR: 11.5, TE: 9.8 },
  ],
};

/** Above this many points of measured waiting cost, the page stops being
 * neutral about a position. Half the doc's stated "most rounds are under 10"
 * boundary would be noise; 10 IS that boundary. */
export const URGENCY_NOTABLE = 10;

/**
 * Above this many points, and only above it, the page is allowed to use its
 * one alarm colour. 25 sits DELIBERATELY ABOVE the largest waiting cost the
 * study measured anywhere in a 4-team draft (20.3, RB round 4), so in the
 * real league this threshold is unreachable and the board stays calm --
 * which is the honest rendering of "positional urgency is mostly a 10-team
 * problem". At 10 teams, where costs run 20-55, it fires normally.
 * test/draft-guidance.test.js asserts both halves of that.
 */
export const URGENCY_ALARM = 25;

/** Team counts the study ran a per-round table for. */
const MEASURED_TEAM_COUNTS = [4, 10];

/**
 * Which measured table to read for a league of `teams`. NEVER interpolates:
 * a 6-team league gets the 4-team table and is told so, rather than a number
 * nobody measured. Deep leagues clamp to 10 (the deepest measured) for the
 * same reason.
 */
export function basisTeamsFor(teams) {
  return teams <= 7 ? MEASURED_TEAM_COUNTS[0] : MEASURED_TEAM_COUNTS[1];
}

/**
 * What waiting one turn costs at `position` in `round`, in actual points.
 *
 * `cost` is null in the LAST round of the draft: there is no next turn, so
 * "what does waiting cost" has no answer, and rendering a stale number there
 * would be the page inventing urgency at the exact moment none can exist.
 * `clamped` is true when `round` ran past the 14 the study measured and the
 * last measured row was reused.
 */
export function waitingCost({ position, round, teams, rounds }) {
  const basisTeams = basisTeamsFor(teams);
  const table = WAITING_COST[basisTeams];
  const out = { position, round, teams, basisTeams, cost: null, clamped: false };
  if (!position || !Number.isFinite(round) || round < 1) return out;
  if (Number.isFinite(rounds) && round >= rounds) return out; // no next turn to wait for
  const idx = Math.min(round, table.length) - 1;
  out.clamped = round > table.length;
  const row = table[idx];
  out.cost = row && position in row ? row[position] : null;
  return out;
}

/** 'calm' | 'notable' | 'alarm' for a measured waiting cost. A negative or
 * missing cost is calm, never unknown-and-therefore-scary. */
export function urgencyLevel(cost) {
  if (cost === null || cost === undefined || !Number.isFinite(cost)) return 'calm';
  if (cost >= URGENCY_ALARM) return 'alarm';
  if (cost >= URGENCY_NOTABLE) return 'notable';
  return 'calm';
}

/**
 * `player_id -> rank within position by consensus ADP` (1 = earliest), the
 * axis `CLIFFS` is measured on. A row with no `adp` gets `null`, never a
 * guessed rank -- the same "never invent an ADP number" rule
 * analytics/src/tt/cli.py's own `_load_adp` states.
 */
export function positionAdpRanks(rows) {
  const byPosition = new Map();
  for (const r of rows ?? []) {
    if (!r?.position) continue;
    if (!byPosition.has(r.position)) byPosition.set(r.position, []);
    byPosition.get(r.position).push(r);
  }
  const ranks = new Map();
  for (const group of byPosition.values()) {
    const ranked = group.filter((r) => Number.isFinite(r.adp)).sort((a, b) => a.adp - b.adp);
    for (const r of group) ranks.set(r.player_id, null);
    ranked.forEach((r, i) => ranks.set(r.player_id, i + 1));
  }
  return ranks;
}

/**
 * The signature element: how much of each position's pre-cliff shelf is
 * still on the board. `remaining` counts players still in `available` whose
 * consensus-ADP rank within the position is at or above the cliff -- "3 RBs
 * left before the cliff", the sentence a serious drafter draws on a cheat
 * sheet by hand and no fantasy app renders.
 *
 * `reachable` is whether the cliff can be hit at all in a draft of
 * `teams * rounds` picks. In the real 4-team league TE6 goes around pick 76
 * against a 60-pick draft, so the TE cliff is NOT reachable and the page
 * must not dress it as pressure -- "there is no scenario in which a 4-team
 * drafter has to reach for a tight end."
 */
export function cliffStrip({ rows, available, teams, rounds }) {
  const ranks = positionAdpRanks(rows);
  const totalPicks = (teams ?? 0) * (rounds ?? 0);
  const byPosition = new Map();
  for (const r of rows ?? []) {
    if (!r?.position) continue;
    if (!byPosition.has(r.position)) byPosition.set(r.position, []);
    byPosition.get(r.position).push(r);
  }
  return CLIFF_ORDER.map((position) => {
    const cliff = CLIFFS[position];
    if (!cliff) {
      return {
        position, hasCliff: false, afterRank: null, drop: null, se: null,
        pick: null, remaining: null, gone: null, reachable: null,
        note: 'no cliff, a slope',
      };
    }
    let remaining = 0;
    for (const r of byPosition.get(position) ?? []) {
      const rank = ranks.get(r.player_id);
      if (rank === null || rank > cliff.afterRank) continue;
      if (available?.has(r.player_id)) remaining += 1;
    }
    return {
      position,
      hasCliff: true,
      afterRank: cliff.afterRank,
      drop: cliff.drop,
      se: cliff.se,
      pick: cliff.pick,
      remaining,
      gone: cliff.afterRank - remaining,
      reachable: totalPicks > 0 ? cliff.pick <= totalPicks : null,
      note: null,
    };
  });
}

/**
 * Yahoo roster-slot label -> the short label the runway row draws. Purely a
 * display mapping (src/draft-room.js keeps the matching `FLEX_ELIGIBLE`
 * eligibility table for the same display reason).
 */
const FLEX_LABELS = { 'W/R/T': 'FLX', 'W/R': 'W/R', 'Q/W/R/T': 'SFLX' };
const GUIDED_SLOTS = new Set(['QB', 'RB', 'WR', 'TE', ...Object.keys(FLEX_LABELS)]);

/**
 * The largest measured effect in this league, rendered as a row of slots:
 * how many mandatory starting slots are still empty, and how many rounds are
 * left to fill them. Simply not finishing with an empty QB or TE slot is
 * worth 63-80 actual points at 4 teams -- more than any positional strategy
 * in the study -- which is why this, and not a survival percentage, is what
 * the page is allowed to shout about.
 *
 * K and DEF are reported separately in `unprojected` rather than counted:
 * the engine projects neither, so it can never recommend into those slots
 * and counting them would manufacture permanent, unactionable urgency. They
 * stay visible because the user still has to draft them.
 *
 * `slack` mirrors the engine's own `_need_urgency` (analytics/src/tt/
 * draft.py): spare rounds beyond the bare minimum of taking one needed
 * position every remaining round. It is allowed to go negative, which is
 * strictly worse than zero and should read that way.
 */
export function runwayState({ slots, roundsRemaining }) {
  const guided = [];
  const unprojected = [];
  const unprojectedEmpty = [];
  for (const s of slots ?? []) {
    if (!GUIDED_SLOTS.has(s.slot)) {
      unprojected.push(s.slot);
      if (!s.player) unprojectedEmpty.push(s.slot);
      continue;
    }
    guided.push({
      label: FLEX_LABELS[s.slot] ?? s.slot,
      slot: s.slot,
      filled: Boolean(s.player),
      player: s.player ? s.player.name : null,
      position: s.player ? s.player.position : null,
    });
  }
  const empty = guided.filter((s) => !s.filled).length;
  const rounds = Number.isFinite(roundsRemaining) ? roundsRemaining : 0;
  const slack = rounds - empty;
  let level;
  if (empty === 0) level = 'complete';
  else if (slack <= 1) level = 'alarm';
  else if (slack <= 3) level = 'notable';
  else level = 'calm';
  return {
    slots: guided, unprojected, unprojectedEmpty, empty, filled: guided.length - empty,
    roundsRemaining: rounds, slack, level,
  };
}

/**
 * Where THIS league's own VOR crosses zero, per position -- the last player
 * who still starts somewhere. Read straight off the engine's own `vor`
 * column (verified in the study to be exactly 0.0 at that rank), never
 * recomputed from roster shape, so the explainer panel can be grounded in
 * this board's real numbers instead of a generality.
 */
export function replacementLine(rows) {
  const byPosition = new Map();
  for (const r of rows ?? []) {
    if (!r?.position || !Number.isFinite(r.vor) || !Number.isFinite(r.proj_points)) continue;
    if (!byPosition.has(r.position)) byPosition.set(r.position, []);
    byPosition.get(r.position).push(r);
  }
  const out = {};
  for (const [position, group] of byPosition) {
    const ordered = group.slice().sort((a, b) => b.proj_points - a.proj_points);
    let best = 0;
    for (let i = 1; i < ordered.length; i += 1) {
      if (Math.abs(ordered[i].vor) < Math.abs(ordered[best].vor)) best = i;
    }
    out[position] = {
      rank: best + 1,
      points: ordered[best].proj_points,
      name: ordered[best].name ?? null,
      top: ordered[0].proj_points,
    };
  }
  return out;
}

function survivalReason(pGone, myNextPick) {
  const at = myNextPick === null || myNextPick === undefined ? 'your next pick' : `#${myNextPick}`;
  if (!Number.isFinite(pGone)) return `No read on whether he lasts to ${at}`;
  if (pGone >= 0.8) return `Won't reach ${at}`;
  if (pGone >= 0.5) return `Probably gone by ${at}`;
  if (pGone >= 0.2) return `May not reach ${at}`;
  return `Should still be there at ${at}`;
}

function cliffReason(position, cliffs) {
  const cliff = (cliffs ?? []).find((c) => c.position === position);
  if (!cliff) return `${position} is not a position this league starts`;
  if (!cliff.hasCliff) return `${position} has no cliff \u2014 it is a slope, so waiting is cheap`;
  if (cliff.remaining <= 0) return `Past the ${position} cliff \u2014 the shelf is empty`;
  if (cliff.remaining === 1) return `Last ${position} above the cliff (\u2212${cliff.drop.toFixed(0)} after him)`;
  const tail = cliff.reachable === false ? ', and this draft ends before it' : '';
  return `${cliff.remaining} ${position}s left above the cliff${tail}`;
}

function needReason(position, runway) {
  const empty = (runway?.slots ?? []).filter((s) => !s.filled);
  const direct = empty.find((s) => s.slot === position);
  if (direct) return `Fills your empty ${position} slot`;
  const flex = empty.find((s) => FLEX_LABELS[s.slot]);
  if (flex) return `Fills your empty ${flex.label} slot`;
  if (empty.length === 0) return 'Your starting lineup is already complete \u2014 this is depth';
  return `You have enough ${position}s \u2014 this is depth, not a need`;
}

/**
 * The hero, as a sentence rather than a metric row: four short conclusions
 * about the pick the ENGINE already ranked first. This function never
 * reorders, re-scores or second-guesses that ranking -- it explains it.
 *
 * Copy rule, enforced by test: conclusions, not inputs. "Won't reach #26",
 * not "P(GONE) 100%"; "Last RB above the cliff", not "tier 3".
 */
export function heroReasons({ top, cliffs, runway, myNextPick }) {
  if (!top) return [];
  return [
    { kind: 'cliff', text: cliffReason(top.position, cliffs) },
    { kind: 'survival', text: survivalReason(top.pGone, myNextPick) },
    { kind: 'need', text: needReason(top.position, runway) },
    {
      kind: 'runway',
      text: runway && runway.empty > 0
        ? `${runway.roundsRemaining} rounds left for ${runway.empty} slots`
        : `${runway?.roundsRemaining ?? 0} rounds left, lineup already full`,
    },
  ];
}
