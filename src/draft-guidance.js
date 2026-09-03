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
      // Who that top projection belongs to. The explainer has to be able to
      // say "Josh Allen projects the most points on this board and is still
      // not the pick" by NAME -- an unattributed 253 explains nothing.
      topName: ordered[0].name ?? null,
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

/* =========================================================================
 * MAKING THE PAGE SELF-EXPLAINING.
 *
 * The user's own words, looking at the live page: "Can you explain the
 * analysis? I don't know what the acronyms mean. How am I to read
 * everything?" That is a usability failure, not a feature request, and the
 * fix belongs HERE rather than in draft-room.html for the same reason
 * everything else in this module does: a definition composed in the page is
 * a definition nothing can check, and a WRONG definition on draft day is
 * worse than none. Everything below is either a definition of a term the
 * page prints, or a count/scale over rows the engine already produced.
 * Nothing here computes a projection, a VOR, a survival probability or a
 * ranking.
 * ====================================================================== */

/** Plain English for a position code, so a definition can say "quarterback"
 * to a reader who does not yet know that QB is one. */
const POSITION_WORDS = { QB: 'quarterback', RB: 'running back', WR: 'wide receiver', TE: 'tight end' };

const positionWord = (position, count = 1) => {
  const word = POSITION_WORDS[position] ?? String(position ?? 'player').toLowerCase();
  return count === 1 ? word : `${word}s`;
};

function ordinal(n) {
  if (!Number.isFinite(n)) return String(n);
  const rem100 = Math.abs(n) % 100;
  const rem10 = Math.abs(n) % 10;
  const suffix = rem100 >= 11 && rem100 <= 13 ? 'th'
    : rem10 === 1 ? 'st' : rem10 === 2 ? 'nd' : rem10 === 3 ? 'rd' : 'th';
  return `${n}${suffix}`;
}

/** Middle value of the finite numbers in `values`, or null when there are
 * none. Used for "the median expected games on this board", which is the
 * only honest way to say what a projection actually covers. */
function median(values) {
  const sorted = (values ?? []).filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  return sorted[Math.floor(sorted.length / 2)];
}

/**
 * THE GLOSSARY: one definition per abbreviation the page prints, written in
 * THIS board's own numbers.
 *
 * Two rules the tests enforce, because both were the actual defect:
 *
 *   1. NO GENERALITIES WHERE A REAL NUMBER EXISTS. "VOR is value over
 *      replacement" is what the page already implied and is exactly what the
 *      reader could not use. "This 4-team league starts 4 quarterbacks in
 *      all, so the last one who still starts is the 4th best -- Josh Allen
 *      projects the most points on the whole board and is still only 23
 *      clear of him" is a definition someone can act on 40 seconds before a
 *      pick.
 *   2. EVERY NUMBER PRINTED HERE MUST MATCH THE NUMBER ON SCREEN. The page
 *      renders integers, so every arithmetic step below is done on the
 *      ROUNDED figures a reader can actually see; a sentence whose numbers
 *      do not add up is a sentence that teaches the reader to distrust the
 *      page. Where rounding would break a subtraction (a top QB projecting
 *      253.49 against a last starter at 230.96 is 23 apart, but 253 - 231 is
 *      22), the sentence states the margin and never prints the pair as an
 *      arithmetic step.
 *
 * `detail` is null wherever the board cannot ground the example -- an empty
 * or partial board loses the worked example, never the definition.
 */
export function glossary({ rows, replacement, teams, survivalPick, adp } = {}) {
  const board = rows ?? [];
  const rep = replacement ?? {};

  // --- PROJ: what a projection actually covers ---------------------------
  const medianGames = median(board.map((r) => r?.proj_games));
  const projDetail = medianGames === null ? null
    : `It is not a 17-game total. The projection already prices in injury risk: it is the points `
      + `he is expected to score across the games he is expected to PLAY, and the median across `
      + `this board is ${Math.round(medianGames)} games.`;

  // --- VOR: the worked example that answers the actual question ----------
  // Contrast the quarterback -- the position whose whole scoring curve sits
  // above everyone else's -- against whichever OTHER position this board
  // spreads widest, which is the pair that makes the paradox concrete.
  // The margin between the best player at a position and the last one who
  // still starts -- which IS that best player's own VOR, since replacement
  // level is defined as the points of the row whose VOR is 0. Rounded from
  // the exact difference, NEVER from the two rounded operands: 253.49 -
  // 230.96 is 23, while the displayed 253 - 231 is 22, and the board prints
  // 23. The panel must never contradict the column it is explaining.
  const spread = (p) => (rep[p] && Number.isFinite(rep[p].top) && Number.isFinite(rep[p].points)
    ? Math.round(rep[p].top - rep[p].points) : null);
  const qb = rep.QB ?? null;
  let foil = null;
  for (const position of Object.keys(rep)) {
    if (position === 'QB') continue;
    const s = spread(position);
    if (s === null) continue;
    if (foil === null || s > spread(foil)) foil = position;
  }
  let vorDetail = null;
  if (qb && foil && Number.isFinite(qb.top) && rep[foil] && qb.topName && rep[foil].topName) {
    const other = rep[foil];
    const qbVor = spread('QB');
    const otherVor = spread(foil);
    const qbTop = Math.round(qb.top);
    const otherTop = Math.round(other.top);
    const highest = board.reduce(
      (best, r) => (Number.isFinite(r?.proj_points) && (best === null || r.proj_points > best.proj_points) ? r : best),
      null,
    );
    const isBoardTop = highest && highest.name === qb.topName;
    vorDetail = `This ${teams}-team league starts ${qb.rank} ${positionWord('QB', qb.rank)} in all, `
      + `so the last one who still starts anywhere is the ${ordinal(qb.rank)} best. `
      + `${qb.topName} projects ${qbTop}${isBoardTop ? ', the most of anyone on this board' : ''}, `
      + `and is ${qbVor} clear of that line — so his VOR is ${qbVor}. `
      + `The same league starts ${other.rank} ${positionWord(foil, other.rank)}, and `
      + `${other.topName} projects ${otherTop} but is ${otherVor} clear of the ${ordinal(other.rank)} `
      + `— VOR ${otherVor}. He scores ${qbTop - otherTop} fewer points than ${qb.topName} `
      + `and is worth ${otherVor - qbVor} more, because you only ever start one `
      + `${positionWord('QB')} and the next one is nearly as good. There is no such backup `
      + `for a top ${positionWord(foil)}.`;
  }
  // Always said, grounded example or not: the bar is the one mark on this
  // page that could be misread ACROSS positions, which is the exact mistake
  // VOR exists to prevent.
  const barNote = 'The bar under each VOR is his share of the best VOR at HIS OWN position, in that '
    + 'position’s colour — so a quarterback’s bar can be longer than a wide receiver’s while his '
    + 'number is smaller.';
  vorDetail = vorDetail === null ? barNote : `${vorDetail} ${barNote}`;

  // --- ADP: name the feed, never a number nobody can trace ---------------
  const adpBits = [];
  if (adp?.totalDrafts) adpBits.push(`${adp.totalDrafts.toLocaleString('en-US')} real drafts`);
  if (adp?.type) adpBits.push(String(adp.type));
  if (adp?.teams) adpBits.push(`${adp.teams}-team`);
  const adpDetail = adpBits.length === 0
    ? 'This board reports whatever ADP feed it was built from; if a player has none, nothing is invented for him.'
    : `Averaged over ${adpBits.join(', ')} (Fantasy Football Calculator). A player the feed `
      + 'does not carry gets no ADP at all rather than a guessed one.';

  const at = Number.isFinite(survivalPick) ? `your next pick (#${survivalPick})` : 'your next pick';

  return [
    {
      id: 'proj', term: 'PROJ', label: 'Projected points',
      short: `Points he is projected to score over the whole season, under this league's own scoring rules.`,
      detail: projDetail,
    },
    {
      id: 'vor', term: 'VOR', label: 'Value over replacement',
      short: 'Points above the last starter at his position — the worst player at that position '
        + 'who still starts somewhere in this league, NOT a waiver-wire pickup.',
      detail: vorDetail,
    },
    {
      id: 'stake', term: 'AT STAKE', label: 'What passing on him costs',
      short: 'VOR × GONE: the points you lose by not taking him now.',
      detail: 'This is the number the recommendation is ranked on (nudged by which of your starting '
        + 'slots are still empty), and it is why a player with a lower VOR can rank above one with a '
        + 'higher VOR: a great player who is certain to still be there scores near zero, because you '
        + 'can wait and have him anyway.',
    },
    {
      id: 'gone', term: 'GONE', label: 'Chance he is taken first',
      short: `The chance somebody else takes him before ${at}.`,
      detail: 'Estimated from his own average draft position and how widely real drafts vary around '
        + 'it. A player with no ADP at all counts as certain to survive rather than as a coin flip.',
    },
    {
      id: 'tier', term: 'TIER', label: 'Interchangeable group',
      short: 'A group of players at one position who are close enough in value to be interchangeable.',
      detail: 'Same tier and same position: take whichever is cheapest, or whichever fills a slot you '
        + 'still need. Tiers break where the drop to the next player is unusually large for that '
        + 'position — the "reach now or wait a full round" gaps a drafter is already scanning for.',
    },
    {
      id: 'adp', term: 'ADP', label: 'Average draft position',
      short: 'Where he goes, on average, in real drafts — what he is expected to cost.',
      detail: adpDetail,
    },
    {
      id: 'adpDelta', term: 'VS ADP', label: 'Picks past his ADP',
      short: 'How far he has already fallen past his own ADP. +14 means he is still sitting here '
        + '14 picks after a typical draft takes him.',
      detail: `A player who has lasted a full turn of this ${teams}-team draft past his ADP is `
        + 'highlighted: he is the cheapest real value on the board, and the usual reason to look past '
        + 'the top of the list.',
    },
    {
      id: 'posRank', term: 'WR1 · RB5', label: 'Rank within his position',
      short: 'His rank among his own position by average draft position — RB5 is the fifth '
        + 'running back off the board in a typical draft.',
      detail: 'That is the axis the measured cliffs sit on, so the badge is filled in while he is '
        + 'still above his position’s cliff and hollow once he is past it.',
    },
    {
      id: 'bye', term: 'BYE', label: 'Bye week',
      short: 'The week his NFL team does not play, so he scores you nothing that week.',
      detail: 'Two of your starters on the same bye is one week you cannot field a full lineup; the '
        + 'lineup panel says so when it happens.',
    },
    {
      // Yahoo publishes far more than four of these -- Q, O, IR, IR-R, PUP-R,
      // NFI-R, NA, DNR and CEL all appear on a real board -- so this defines
      // the common ones and then points at the full wording rather than
      // pretending the list is closed. Yahoo sends `status_full` with every
      // one of them, and the page shows it: spelled out beside the flag on
      // the recommendation and in your lineup, and in the row's own tooltip
      // on the board.
      id: 'injury', term: 'Q · O · IR', label: 'Player status',
      short: 'Yahoo’s own flag on a player who is not simply available: Q questionable, O out, '
        + 'IR on injured reserve, NA not on an active roster, plus several reserve designations.',
      detail: 'Whatever the code, the full wording and the body part are spelled out next to it — '
        + 'on the recommendation, in your lineup, and in each board row’s tooltip. The projection '
        + 'already discounts him for the games he is expected to miss across a season; this is '
        + 'today’s news on top of that, and it is why the top recommendation can carry a flag.',
    },
  ];
}

/**
 * Weeks where two or more of your STARTERS are on a bye at once -- a week
 * you cannot field a full lineup, which is the same class of problem as an
 * unfilled slot and the reason the bye is worth showing at all.
 *
 * Bench players are deliberately not counted: a bench player's bye costs
 * nothing, and counting them would raise a flag on every roster.
 * A player whose bye we do not know is skipped rather than guessed at.
 */
export function byeConflicts(slots) {
  const byWeek = new Map();
  for (const s of slots ?? []) {
    const week = s?.player?.bye;
    if (!Number.isFinite(week)) continue;
    if (!byWeek.has(week)) byWeek.set(week, []);
    byWeek.get(week).push(s.player.name ?? 'unnamed');
  }
  return [...byWeek.entries()]
    .filter(([, players]) => players.length > 1)
    .sort((a, b) => a[0] - b[0])
    .map(([week, players]) => ({ week, players }));
}

/**
 * How much of its own position's bar each row fills, 0..1 -- the board's
 * per-row VOR bar.
 *
 * SCALED WITHIN POSITION, never across the whole board, for exactly the
 * reason VOR exists in the first place: comparing a quarterback's raw VOR
 * against a running back's is the mistake the whole page is built to
 * prevent, and one bar scaled across all positions would reinstate it
 * visually. A row worth nothing over replacement gets 0 (no bar drawn);
 * a row with no VOR at all gets null (nothing to draw and nothing implied).
 */
export function vorShares(rows) {
  const maxByPosition = new Map();
  for (const r of rows ?? []) {
    if (!r?.position || !Number.isFinite(r.vor)) continue;
    const best = maxByPosition.get(r.position);
    if (best === undefined || r.vor > best) maxByPosition.set(r.position, r.vor);
  }
  const shares = new Map();
  for (const r of rows ?? []) {
    if (!Number.isFinite(r?.vor)) { shares.set(r?.playerId, null); continue; }
    const max = maxByPosition.get(r.position);
    shares.set(r.playerId, !Number.isFinite(max) || max <= 0 ? 0 : Math.max(0, Math.min(1, r.vor / max)));
  }
  return shares;
}

/**
 * Which visual band each row belongs to, so a run of same-tier players at
 * one position reads as ONE GROUP rather than as four numbers a reader has
 * to compare by eye. `band` increments every time the tier changes within a
 * position, so its PARITY alternates and adjacent tiers are distinguishable;
 * `first` marks the row that opens a band, which is where the rule is drawn.
 *
 * Banding is per position and follows the row order it is given (the board's
 * own VOR order), because a tier only ever means anything against the other
 * players at that position -- an interleaved wide receiver must not split a
 * run of running backs. A missing tier is its own band rather than being
 * merged into the one above it: "we do not know" is not "the same".
 */
export function tierBands(rows) {
  const state = new Map(); // position -> {band, tier}
  const out = new Map();
  for (const r of rows ?? []) {
    if (!r?.position) { out.set(r?.playerId, { band: 0, first: true }); continue; }
    const prev = state.get(r.position);
    const tier = r.tier ?? null;
    const changed = prev === undefined || tier === null || prev.tier === null || prev.tier !== tier;
    const band = prev === undefined ? 0 : (changed ? prev.band + 1 : prev.band);
    state.set(r.position, { band, tier });
    out.set(r.playerId, { band, first: changed });
  }
  return out;
}

/**
 * How hard a player has fallen past his own ADP, SCALED TO THIS LEAGUE.
 *
 * 'past' is a full turn of the draft (`teams` picks): everyone has picked
 * once since a typical draft would have taken him and he is still sitting
 * there. 'far' is two full turns. Scaling by team count rather than by a
 * fixed number of picks is the same discipline `urgencyLevel` applies to
 * waiting cost: 8 picks past ADP is a genuine slide in a 4-team draft and
 * unremarkable in a 10-team one, and a page that flags both teaches its
 * reader to ignore the flag.
 *
 * A negative delta (he is going EARLIER than his ADP) is never a signal:
 * that is the market being keen, not a bargain.
 */
export function adpFallLevel(adpDelta, teams) {
  if (!Number.isFinite(adpDelta) || !Number.isFinite(teams) || teams <= 0) return 'none';
  if (adpDelta >= teams * 2) return 'far';
  if (adpDelta >= teams) return 'past';
  return 'none';
}
